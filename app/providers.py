"""CCTV, bilibili and iwara through yt-dlp and a credential-preserving media proxy.

Split bilibili MP4 tracks stay separate in MPEG-DASH. This keeps native seeking
and the original quality, instead of transcoding a second real-time copy.
"""

import asyncio
from collections import OrderedDict
import json
import math
import re
import struct
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import hls

_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "live.bilibili.com", "b23.tv",
          "iwara.tv", "www.iwara.tv", "ecchi.iwara.tv"}
_CCTV = re.compile(r"(?:[a-z0-9-]+\.)*(?:cctv\.com|cntv\.cn|cntv\.com)")
_CCTV_LIVE = re.compile(r"/live/(cctv(?:[1-9]|1[0-7])(?:plus|[a-z])?|cgtn(?:[a-z]*)?)/?")
_NS = "urn:mpeg:dash:schema:mpd:2011"
_MAX_ASSETS = 2000
_YDL_OPTIONS = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "format_sort": ["res", "fps", "vcodec:h264", "acodec:aac", "br"]}
ET.register_namespace("", _NS)


class ProviderError(Exception):
    pass


class _QuietExtractorLogger:
    # yt-dlp errors can contain signed CDN URLs or authenticated request details.
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def normalize_url(url: str) -> str | None:
    try:
        p = urlsplit(url.strip())
        host = (p.hostname or "").lower()
        if p.scheme not in ("http", "https") or p.username or p.password or p.port not in (None, 80, 443):
            return None
        if host not in _HOSTS and not _CCTV.fullmatch(host):
            return None
        path, query = p.path, p.query
        if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
            # Bilibili now shares locale-prefixed pages (e.g. /ja/video/BV...).
            # The extractor expects the canonical content path; page selection
            # such as ?p=2 stays intact while known share trackers are removed.
            path = re.sub(r"^/[A-Za-z]{2}(?:-[A-Za-z]{2})?/(?=video/|bangumi/|festival/)", "/", path)
            trackers = {"spm", "spm_id_from", "from_spmid", "vd_source", "share_source", "share_medium",
                        "share_plat", "share_session_id", "share_tag", "share_times", "timestamp", "unique_k"}
            query = urlencode([(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                               if k.lower() not in trackers and not k.lower().startswith("utm_")])
        if host.endswith("iwara.tv") and not re.fullmatch(r"/videos?/[A-Za-z0-9]+(?:/[^?#]*)?", p.path):
            return None
        if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"} and not re.match(
                r"/(?:video/(?:[bB][vV][A-Za-z0-9]+|[aA][vV][0-9]+)|bangumi/play/(?:ep|ss)[0-9]+|festival/)", path):
            return None
        if host == "live.bilibili.com" and not re.fullmatch(r"/(?:blanc/)?[0-9]+/?", p.path):
            return None
        if host == "b23.tv" and not re.fullmatch(r"/[A-Za-z0-9]+/?", p.path):
            return None
        return urlunsplit(("https", host, path, query, ""))
    except (TypeError, ValueError):
        return None


def is_provider_url(url: str) -> bool:
    return normalize_url(url) is not None


def is_live_url(url: str) -> bool:
    normalized = normalize_url(url)
    if not normalized:
        return False
    p = urlsplit(normalized)
    return p.hostname == "live.bilibili.com" or bool(_CCTV_LIVE.fullmatch(p.path))


@dataclass(frozen=True)
class Track:
    url: str
    headers: dict[str, str]
    kind: str  # video, audio, muxed
    mime: str
    codec: str = ""
    bandwidth: int = 1_000_000
    width: int = 0
    height: int = 0
    language: str = "und"
    label: str = ""
    hls: bool = False


@dataclass(frozen=True)
class Subtitle:
    url: str
    headers: dict[str, str]
    language: str
    label: str
    json_caption: bool = False


@dataclass(frozen=True)
class Media:
    title: str | None
    duration: float | None
    live: bool
    video: Track
    audio: tuple[Track, ...] = ()
    subtitles: tuple[Subtitle, ...] = ()


def _headers(info, fmt, cookies) -> dict[str, str]:
    headers = {str(k): str(v) for k, v in ((info.get("http_headers") or {}) | (fmt.get("http_headers") or {})).items()
               if isinstance(v, (str, int, float))}
    cookie = cookies.get_cookie_header(fmt["url"])
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _track(info, fmt, cookiejar) -> Track:
    acodec, vcodec = fmt.get("acodec"), fmt.get("vcodec")
    kind = "audio" if vcodec == "none" else "video" if acodec == "none" else "muxed"
    audio = kind == "audio"
    ext = fmt.get("ext") or "mp4"
    mime = ("audio/" if audio else "video/") + ("webm" if ext == "webm" else "mp4")
    codecs = [c for c in ([acodec] if audio else [vcodec, acodec]) if c and c != "none"]
    return Track(fmt["url"], _headers(info, fmt, cookiejar), kind, mime, ",".join(codecs),
                 max(1, int((fmt.get("tbr") or fmt.get("abr") or 1000) * 1000)),
                 int(fmt.get("width") or 0), int(fmt.get("height") or 0),
                 fmt.get("language") or "und", fmt.get("format_note") or fmt.get("language") or "",
                 "m3u8" in (fmt.get("protocol") or "") or ".m3u8" in urlsplit(fmt["url"]).path)


def extract(url: str, cookies) -> Media:
    normalized = normalize_url(url)
    if normalized is None:
        raise ProviderError("対応するCCTV・bilibili・iwaraの動画URLを指定してください")
    if _CCTV_LIVE.fullmatch(urlsplit(normalized).path) and _CCTV.fullmatch(urlsplit(normalized).hostname):
        return _cctv_live(normalized)
    try:
        with YoutubeDL(_YDL_OPTIONS | {"logger": _QuietExtractorLogger()}) as ydl:
            cookies.copy_into(ydl.cookiejar)
            info = ydl.extract_info(normalized, download=False)
            cookies.merge_from(ydl.cookiejar)
            if not info or info.get("_type") in {"playlist", "multi_video"}:
                raise ProviderError("再生する動画を1本指定してください")
            selected = info.get("requested_formats") or [info]
            tracks = [_track(info, f, ydl.cookiejar) for f in selected if f.get("url")]
            video = next((t for t in tracks if t.kind != "audio"), None)
            if video is None:
                raise ProviderError("再生できる映像形式がありません")
            audio = [t for t in tracks if t.kind == "audio"] if video.kind == "video" else []
            # Keep one best alternative for every available audio language.
            languages = {t.language for t in audio}
            candidates = sorted(info.get("formats") or [], key=lambda f: f.get("tbr") or f.get("abr") or 0, reverse=True)
            for fmt in candidates if video.kind == "video" else []:
                language = fmt.get("language") or "und"
                if fmt.get("vcodec") == "none" and fmt.get("url") and language not in languages and fmt.get("ext") in {"m4a", "mp4"}:
                    audio.append(_track(info, fmt, ydl.cookiejar)); languages.add(language)
            captions = []
            for language, formats in (info.get("subtitles") or {}).items():
                fmt = next((f for f in formats if f.get("url") and f.get("ext") in {"vtt", "json"}), None)
                if fmt:
                    captions.append(Subtitle(fmt["url"], _headers(info, fmt, ydl.cookiejar), language,
                                             fmt.get("name") or language, fmt.get("ext") == "json"))
            duration = info.get("duration")
            duration = float(duration) if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else None
            return Media(info.get("title"), None if info.get("is_live") else duration, bool(info.get("is_live")),
                         video, tuple(audio), tuple(captions))
    except DownloadError as error:
        # yt-dlp messages may include signed resource URLs; keep those out of clients/logs.
        message = str(error).lower()
        if any(word in message for word in ["login", "cookie", "premium", "private", "authentication"]):
            raise ProviderError("この動画にはログインまたは視聴権限が必要です") from None
        if "region" in message or "geo" in message:
            raise ProviderError("この動画はサーバーの接続地域では視聴できません") from None
        raise ProviderError("配信元から動画を取得できませんでした。URLと公開状態を確認してください") from None


def _cctv_live(url: str) -> Media:
    channel = _CCTV_LIVE.fullmatch(urlsplit(url).path)[1]
    endpoint = "https://vdn.live.cntv.cn/api2/liveHtml5.do?channel=pa://cctv_p2p_hd" + channel + "&client=html5"
    headers = {"Referer": url, "User-Agent": "Mozilla/5.0"}
    try:
        with urllib.request.urlopen(urllib.request.Request(endpoint, headers=headers), timeout=20) as response:
            text = response.read(256 * 1024).decode()
        # The endpoint may append JavaScript after the assignment. Decode only
        # the JSON object, never execute or eval that response.
        match = re.search(r"\bhtml5VideoData\s*=\s*'", text)
        data = json.JSONDecoder().raw_decode(text[match.end():])[0] if match else json.loads(text)
        if data.get("ack") != "yes" or str(data.get("public")) != "1":
            raise ProviderError("CCTVライブは現在視聴できないか、接続地域で制限されています")
        # hls6 is the audio-only feed. The official HTML5 player uses hls5
        # where that is available; never substitute a DRM endpoint.
        choices = data.get("hls_url") or {}
        stream = None
        for index in [5, 4, 3, 2, 1]:
            candidate = choices.get("hls" + str(index))
            if (not isinstance(candidate, str) or not candidate.startswith(("https://", "http://"))
                    or re.search(r"(?:drm=1|drmld)", candidate)
                    or urlsplit(candidate).path.lower().endswith((".jpg", ".jpeg", ".png", ".gif"))):
                continue
            try:
                with _http(candidate, headers) as response:
                    head = response.read(4096).decode("utf-8-sig", "replace").lstrip()
                if head.startswith("#EXTM3U") and "SAMPLE-AES" not in head and "KEYFORMAT=" not in head:
                    stream = candidate
                    break
            except OSError:
                continue
        if stream is None:
            raise ProviderError("CCTVのライブ映像は現在提供されていません。番組・接続地域の制限などを確認してください")
        return Media(channel.upper() + " ライブ", None, True, Track(stream, headers, "muxed", "video/mp4", hls=True))
    except (OSError, ValueError, KeyError) as error:
        raise ProviderError("CCTVライブの配信情報を取得できませんでした") from None


class _MediaRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        original, target = urlsplit(req.full_url), urlsplit(newurl)
        if redirected is not None and (original.scheme, original.netloc) != (target.scheme, target.netloc):
            for name in list(redirected.headers):
                if name.lower() in {"cookie", "authorization"}:
                    redirected.remove_header(name)
        return redirected


def _http(url: str, headers: dict, range_header: str | None = None):
    sent = headers | {"Accept-Encoding": "identity"}
    if range_header:
        sent["Range"] = range_header
    return urllib.request.build_opener(_MediaRedirect()).open(urllib.request.Request(url, headers=sent), timeout=25)


def _index_data(track: Track):
    """Find initialization and SIDX in one bounded read, shared by both APIs."""
    try:
        with _http(track.url, track.headers, "bytes=0-262143") as response:
            data = response.read(262144)
    except OSError:
        raise ProviderError("DASH映像の初期化情報を取得できませんでした") from None
    position, init_end = 0, 0
    while position + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, position)
        if size == 1:
            if position + 16 > len(data):
                break
            size = struct.unpack_from(">Q", data, position + 8)[0]
        if size < 8 or position + size > len(data):
            break
        if kind == b"moov":
            init_end = position + size
        if kind == b"sidx" and init_end:
            return data, f"0-{init_end - 1}", f"{position}-{position + size - 1}", position, position + size - 1
        position += size
    raise ProviderError("この分離映像形式のDASHインデックスには対応していません")


def segment_base(track: Track) -> tuple[str, str]:
    """Compatibility helper for callers that need the initialization/index ranges."""
    _, init, index, _, _ = _index_data(track)
    return init, index


def parse_sidx(data: bytes, start: int, end: int) -> dict:
    """Materialize SIDX references for VLC 3's SegmentList bootstrap.

    VLC 3's SegmentTracker asks for a media segment before loading SegmentBase's
    index. Explicit ranges break that circular dependency while keeping the
    original encoded bytes and seeking timeline unchanged.
    """
    try:
        if not 0 <= start <= end < len(data):
            raise ValueError
        size, kind = struct.unpack_from(">I4s", data, start)
        position = start + 8
        if size == 1:
            size = struct.unpack_from(">Q", data, position)[0]
            position += 8
        if kind != b"sidx" or size != end - start + 1:
            raise ValueError
        version = data[position]
        position += 4  # version and flags
        _, timescale = struct.unpack_from(">II", data, position)
        position += 8
        if not timescale or version not in {0, 1}:
            raise ValueError
        encoding = ">II" if version == 0 else ">QQ"
        clock, first_offset = struct.unpack_from(encoding, data, position)
        position += 8 if version == 0 else 16
        _, count = struct.unpack_from(">HH", data, position)
        position += 4
        if not count or position + count * 12 > end + 1:
            raise ValueError
        offset = end + 1 + first_offset
        segments = []
        for _ in range(count):
            reference, duration, _ = struct.unpack_from(">III", data, position)
            position += 12
            if reference & 0x80000000 or not reference or not duration:
                raise ValueError
            if max(offset + reference, clock + duration) > (1 << 63) - 1:
                raise ValueError
            segments.append({"offset": offset, "size": reference, "time": clock, "duration": duration})
            offset += reference
            clock += duration
        return {"timescale": timescale, "segments": segments}
    except (ValueError, IndexError, struct.error):
        raise ProviderError("このDASHインデックスは壊れているか、未対応の階層形式です") from None


def segment_index(track: Track) -> tuple[str, dict]:
    data, init, _, start, end = _index_data(track)
    return init, parse_sidx(data, start, end)


@dataclass
class Asset:
    url: str
    headers: dict[str, str]
    kind: str
    seen: float = field(default_factory=time.monotonic)


class Videos:
    def __init__(self, resolver):
        self.resolver = resolver
        self.assets: OrderedDict[str, Asset] = OrderedDict()
        self.identities: dict[tuple, str] = {}

    def resource(self, url, headers, kind, base):
        identity = (url, tuple(sorted(headers.items())), kind)
        key = self.identities.get(identity)
        if key is None:
            key = uuid.uuid4().hex
            self.identities[identity] = key
            self.assets[key] = Asset(url, headers, kind)
            while len(self.assets) > _MAX_ASSETS:
                old = next(iter(self.assets)); asset = self.assets.pop(old)
                self.identities.pop((asset.url, tuple(sorted(asset.headers.items())), asset.kind), None)
        else:
            self.assets[key].seen = time.monotonic()
            self.assets.move_to_end(key)
        return base.rstrip("/") + "/video/provider/" + key + (".m3u8" if kind == "hls" else ".vtt" if kind in {"vtt", "json"} else ".mp4" if kind == "mp4" else ".bin")

    async def open(self, url, base) -> tuple[str, str]:
        media = await self.resolver.provider(url)
        if media.video.hls:
            if media.audio:
                if any(not track.hls for track in media.audio):
                    raise ProviderError("このHLS映像に付随する分離音声形式には対応していません")
                lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
                def quoted(value):
                    return '"' + str(value).replace('"', "").replace("\r", " ").replace("\n", " ") + '"'
                for index, track in enumerate(media.audio):
                    attributes = ['TYPE=AUDIO', 'GROUP-ID="provider-audio"', 'NAME=' + quoted(track.label or track.language or str(index)),
                                  'DEFAULT=' + ("YES" if index == 0 else "NO"), 'AUTOSELECT=YES',
                                  'URI=' + quoted(self.resource(track.url, track.headers, "hls", base))]
                    if track.language != "und": attributes.append("LANGUAGE=" + quoted(track.language))
                    lines.append("#EXT-X-MEDIA:" + ",".join(attributes))
                attributes = ["BANDWIDTH=" + str(media.video.bandwidth + max(track.bandwidth for track in media.audio)),
                              'AUDIO="provider-audio"']
                if media.video.width and media.video.height:
                    attributes.append(f"RESOLUTION={media.video.width}x{media.video.height}")
                codec = ",".join(c for c in [media.video.codec, media.audio[0].codec] if c)
                if codec: attributes.append("CODECS=" + quoted(codec))
                lines += ["#EXT-X-STREAM-INF:" + ",".join(attributes),
                          self.resource(media.video.url, media.video.headers, "hls", base)]
                return "application/vnd.apple.mpegurl", "\n".join(lines) + "\n"
            return "redirect", self.resource(media.video.url, media.video.headers, "hls", base)
        if media.video.kind == "muxed" or not media.audio:
            return "redirect", self.resource(media.video.url, media.video.headers, "mp4", base)
        if media.duration is None or media.live:
            raise ProviderError("この分離形式のライブ映像には対応していません")
        root = ET.Element("MPD", xmlns=_NS, type="static", profiles="urn:mpeg:dash:profile:isoff-on-demand:2011",
                          mediaPresentationDuration=f"PT{media.duration:.3f}S", minBufferTime="PT1S")
        period = ET.SubElement(root, "Period", start="PT0S", duration=f"PT{media.duration:.3f}S")
        tracks = [media.video, *media.audio]
        ranges = await asyncio.gather(*(asyncio.to_thread(segment_index, track) for track in tracks))
        for index, (track, (init, index_data)) in enumerate(zip(tracks, ranges)):
            attrs = {"id": str(index), "contentType": track.kind, "mimeType": track.mime, "segmentAlignment": "true"}
            if track.language != "und": attrs["lang"] = track.language
            adaptation = ET.SubElement(period, "AdaptationSet", attrs)
            if track.label: ET.SubElement(adaptation, "Label").text = track.label
            rep_attrs = {"id": str(index), "bandwidth": str(track.bandwidth)}
            if track.codec: rep_attrs["codecs"] = track.codec
            if track.width: rep_attrs["width"] = str(track.width)
            if track.height: rep_attrs["height"] = str(track.height)
            representation = ET.SubElement(adaptation, "Representation", rep_attrs)
            ET.SubElement(representation, "BaseURL").text = self.resource(track.url, track.headers, "mp4", base)
            segment = ET.SubElement(representation, "SegmentList", timescale=str(index_data["timescale"]))
            ET.SubElement(segment, "Initialization", range=init)
            timeline = ET.SubElement(segment, "SegmentTimeline")
            for entry in index_data["segments"]:
                ET.SubElement(timeline, "S", t=str(entry["time"]), d=str(entry["duration"]))
            for entry in index_data["segments"]:
                ET.SubElement(segment, "SegmentURL", mediaRange=f'{entry["offset"]}-{entry["offset"] + entry["size"] - 1}')
        for index, subtitle in enumerate(media.subtitles):
            adaptation = ET.SubElement(period, "AdaptationSet", id="text" + str(index), contentType="text", mimeType="text/vtt", lang=subtitle.language)
            ET.SubElement(adaptation, "Label").text = subtitle.label
            rep = ET.SubElement(adaptation, "Representation", id="subtitle" + str(index), bandwidth="256")
            ET.SubElement(rep, "BaseURL").text = self.resource(subtitle.url, subtitle.headers, "json" if subtitle.json_caption else "vtt", base)
        return "application/dash+xml", ET.tostring(root, encoding="unicode")

    def get(self, key, extension):
        asset = self.assets.get(key)
        expected = "m3u8" if asset and asset.kind == "hls" else "vtt" if asset and asset.kind in {"vtt", "json"} else "mp4" if asset and asset.kind == "mp4" else "bin"
        if asset is None or extension != expected:
            raise ProviderError("再生リソースが期限切れです。動画を開き直してください")
        asset.seen = time.monotonic()
        self.assets.move_to_end(key)
        return asset

    def playlist(self, asset, base):
        try:
            with _http(asset.url, asset.headers) as response:
                text = response.read(2 * 1024 * 1024).decode("utf-8-sig")
        except OSError:
            raise ProviderError("配信プレイリストを取得できませんでした") from None
        if not text.startswith("#EXTM3U"):
            raise ProviderError("配信元のプレイリストが正しくありません")
        best = hls.best_variant(text, asset.url)
        if best: text = hls.only(best)
        if "SAMPLE-AES" in text or "KEYFORMAT=" in text:
            raise ProviderError("この暗号化配信には対応していません")
        def rewrite(value):
            target = urljoin(asset.url, value)
            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                raise ProviderError("配信リソースのURLが正しくありません")
            headers = asset.headers.copy()
            if parsed.netloc != urlsplit(asset.url).netloc:
                headers = {k: v for k, v in headers.items() if k.lower() not in {"cookie", "authorization"}}
            kind = "hls" if ".m3u8" in parsed.path else "vtt" if ".vtt" in parsed.path else "bin"
            return self.resource(target, headers, kind, base)
        lines = []
        for line in text.splitlines():
            if line.startswith("#"): line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + rewrite(m[1]) + '"', line)
            elif line.strip(): line = rewrite(line.strip())
            lines.append(line)
        return "\n".join(lines) + "\n"


def captions(asset) -> bytes:
    try:
        with _http(asset.url, asset.headers) as response:
            data = response.read(2 * 1024 * 1024)
        if asset.kind == "vtt": return data
        body = json.loads(data).get("body", [])
        lines = ["WEBVTT", ""]
        def stamp(value):
            ms = max(0, round(float(value) * 1000)); seconds, ms = divmod(ms, 1000); minutes, seconds = divmod(seconds, 60); hours, minutes = divmod(minutes, 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"
        for index, cue in enumerate(body):
            lines += [str(index + 1), f"{stamp(cue['from'])} --> {stamp(cue['to'])}", str(cue["content"]).replace("-->", "→"), ""]
        return "\n".join(lines).encode()
    except (OSError, ValueError, KeyError, TypeError):
        raise ProviderError("字幕を取得できませんでした") from None
