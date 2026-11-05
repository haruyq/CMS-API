"""Resolve native media metadata; playback bytes go straight from origin to VLC.

No proxy asset IDs, media downloads, or ffmpeg processes are created here. Pages an
extension plays keep using its private relay (see extension.py). Shared account cookies
and Authorization headers are never exported to a game client.
"""

import asyncio
import math
import re
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import config, extension, hls, providers, tver
from .audio import is_allowed

# A live stream's playlist URLs can be short-lived tokens; VOD reuses its lookup as long as /stream's does.
_LIVE_TTL = 60
_MAX_CACHE = 128
_MAX_PLAYLIST = 1024 * 1024
_HEADERS = {"user-agent", "referer", "origin", "accept", "accept-language"}
_OPTIONS = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bestvideo+bestaudio/best/bestaudio",
            "format_sort": ["res", "fps", "br"], "format_sort_force": True,
            "logger": providers._QuietExtractorLogger()}


class NativeError(Exception):
    """A safe, public explanation without account details or signed URLs."""


def _extension(fmt):
    return str(fmt.get("ext") or urlsplit(fmt.get("url") or "").path.rsplit(".", 1)[-1]).lower()


def _is_audio(fmt):
    return fmt.get("vcodec") == "none" or fmt.get("vcodec") is None and _extension(fmt) in {
        "mp3", "m4a", "aac", "flac", "wav", "ogg", "opus"}


def _is_hls(fmt):
    return "m3u8" in (fmt.get("protocol") or "") or urlsplit(fmt.get("url") or "").path.lower().endswith(".m3u8")


def _quality(fmt):
    return _number(fmt.get("height")), _number(fmt.get("width")), _number(fmt.get("fps"))


def _mp4_audio(fmt):
    ext = _extension(fmt)
    return _is_audio(fmt) and (ext in {"m4a", "mp4"} or ext not in {"aac", "mp3", "flac", "wav", "ogg", "opus", "webm"}
                               and str(fmt.get("acodec") or "").startswith("mp4a"))


def _compatible_audio(fmt, hls_video):
    if _is_hls(fmt):
        return hls_video
    return not hls_video and _mp4_audio(fmt) and "dash_segments" not in (fmt.get("protocol") or "")


def _compatible_selection(info, selected):
    """Keep maximum video resolution/FPS; prefer an indexed native MP4 container.

    VLC 3 can decode a WebM file, but this client's generated split DASH has no
    EBML Cues index implementation. Selecting a smaller MP4 would hide a quality
    downgrade, so an origin without an equal-quality compatible format fails.
    """
    chosen_video = next((fmt for fmt in selected if fmt.get("url") and not _is_audio(fmt)), None)
    if chosen_video is None:
        return selected
    candidates = [fmt for fmt in list(info.get("formats") or []) + selected
                  if fmt.get("url") and not fmt.get("has_drm") and not _is_audio(fmt)]
    if not candidates:
        raise NativeError("No unprotected native video format is available")
    top = max(_quality(fmt) for fmt in candidates)
    equal = [fmt for fmt in candidates if _quality(fmt) == top]
    compatible = [fmt for fmt in equal if _is_hls(fmt) or
                  _extension(fmt) == "mp4" and "dash_segments" not in (fmt.get("protocol") or "")]
    if not compatible:
        # A muxed native file needs no generated DASH index and remains playable.
        compatible = [fmt for fmt in equal if fmt.get("acodec") not in {None, "none"}
                      and "dash_segments" not in (fmt.get("protocol") or "")]
    if not compatible:
        raise NativeError("Maximum-quality video has no compatible native MP4/HLS format at the same resolution and frame rate")
    video = chosen_video if chosen_video in compatible else max(compatible, key=lambda fmt: _number(fmt.get("tbr")))
    if video.get("acodec") != "none":
        return [video]
    selected_audio = next((fmt for fmt in selected if fmt.get("url") and _is_audio(fmt)), None)
    if selected_audio is None:
        return [video]
    language = selected_audio.get("language") or "und"
    audio_formats = [fmt for fmt in list(info.get("formats") or []) + selected
                     if fmt.get("url") and not fmt.get("has_drm") and _compatible_audio(fmt, _is_hls(video))
                     and (fmt.get("language") or "und") == language]
    if not audio_formats:
        raise NativeError("Maximum-quality video requires an audio format whose native DASH index is unsupported")
    audio = max(audio_formats, key=lambda fmt: _number(fmt.get("tbr") or fmt.get("abr")))
    return [video, audio]


def _url(url: str) -> str:
    try:
        p = urlsplit(url)
        if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
                or p.port == 0 or any(ord(c) < 32 for c in url)):
            raise ValueError
    except (TypeError, ValueError):
        raise NativeError("The media origin does not provide a native HTTP playback URL") from None
    return url


def check_source(url: str) -> str:
    url = url.strip()
    if not is_allowed(url):
        raise PermissionError("This host is not allowed")
    return _url(url)


def _headers(headers: dict) -> dict[str, str]:
    # Passing the API's account cookies to a client would give it that account.
    # CDN bearer authorization is normally already scoped in the signed URL.
    return {str(k): str(v) for k, v in headers.items()
            if str(k).lower() in _HEADERS and isinstance(v, (str, int, float))
            and "\r" not in str(v) and "\n" not in str(v)}


def _number(value, default=0):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else default


def _range(value) -> str | None:
    if isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end:
            return f"{start}-{end}"
    if isinstance(value, str) and re.fullmatch(r"\d+-\d+", value):
        start, end = map(int, value.split("-"))
        return value if start <= end else None
    return None


def _track(info: dict, fmt: dict) -> dict:
    if fmt.get("has_drm"):
        raise NativeError("This media format requires DRM and cannot be played natively")
    url = _url(fmt["url"])
    acodec, vcodec = fmt.get("acodec"), fmt.get("vcodec")
    ext = str(fmt.get("ext") or "mp4").lower()
    audio_only = vcodec == "none" or vcodec is None and ext in {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus"}
    kind = "audio" if audio_only else "video" if acodec == "none" else "muxed"
    protocol = fmt.get("protocol") or urlsplit(url).scheme
    ishls = "m3u8" in protocol or urlsplit(url).path.lower().endswith(".m3u8")
    suffix = {"mp3": "mpeg", "m4a": "mp4", "opus": "ogg"}.get(ext, ext)
    mime = ("audio/" if kind == "audio" else "video/") + suffix
    codecs = [c for c in ([acodec] if kind == "audio" else [vcodec, acodec]) if c and c != "none"]
    result = {"url": url, "headers": _headers((info.get("http_headers") or {}) | (fmt.get("http_headers") or {})),
              "kind": kind, "mime": mime, "codec": ",".join(codecs),
              "width": int(_number(fmt.get("width"))), "height": int(_number(fmt.get("height"))),
              "fps": _number(fmt.get("fps")), "bandwidth": max(1, int(_number(fmt.get("tbr") or fmt.get("abr"), 1000) * 1000)),
              "language": fmt.get("language") or "und", "label": fmt.get("format_note") or fmt.get("language") or "",
              "hls": ishls, "protocol": protocol, "format_id": str(fmt.get("format_id") or ""),
              "index_range": _range(fmt.get("indexRange") or fmt.get("index_range")),
              "init_range": _range(fmt.get("initRange") or fmt.get("init_range"))}
    if fmt.get("manifest_url"):
        result["manifest_url"] = _url(fmt["manifest_url"])
    return result


def _provider_track(track: providers.Track) -> dict:
    return {"url": _url(track.url), "headers": _headers(track.headers), "kind": track.kind,
            "mime": track.mime, "codec": track.codec, "width": track.width, "height": track.height,
            "fps": 0, "bandwidth": track.bandwidth, "language": track.language, "label": track.label,
            "hls": track.hls, "protocol": "m3u8_native" if track.hls else urlsplit(track.url).scheme,
            "index_range": None, "init_range": None, "format_id": ""}


def _subtitle(info: dict, fmt: dict, language: str) -> dict:
    return {"url": _url(fmt["url"]), "headers": _headers((info.get("http_headers") or {}) | (fmt.get("http_headers") or {})),
            "language": language, "label": fmt.get("name") or language,
            "format": fmt.get("ext") or "vtt"}


def _response(title, duration, live, video, audio=(), subtitles=(), profile="native_mp4") -> dict:
    duration = _number(duration, None)
    return {"version": 1, "mode": "native", "relay_required": False, "title": title,
            "duration": duration if not live and duration and duration > 0 else None,
            "live": bool(live), "video": video, "audio": list(audio), "subtitles": list(subtitles),
            "manifest": None, "expires_in": _LIVE_TTL if live else config.RESOLVE_CACHE_SECONDS, "available_at": 0,
            "selection": {"profile": profile, "video": "maximum_resolution_fps", "transcoded": False,
                          "audio": "best_m4a_per_language" if profile == "native_mp4" else "best_per_language"}}


def _extract(url: str, cookies, profile="native_mp4") -> dict:
    if extension.handles(url):
        # Do not even contact the private extension, whose address and token never leave this API.
        return {"version": 1, "mode": "relay", "relay_required": True,
                "live": extension.is_live_url(url), "duration": None, "title": None,
                "video": None, "audio": [], "subtitles": [], "manifest": None, "expires_in": _LIVE_TTL,
                "available_at": 0, "selection": {"profile": "extension", "transcoded": False}}
    if tver.is_tver_url(url):
        try:
            source = tver.resolve(url)
        except (tver.TverError, OSError, ValueError, KeyError):
            raise NativeError("TVer could not resolve this program; check availability and region") from None
        video = _provider_track(providers.Track(source.url, source.headers, "muxed", "video/mp4", hls=True))
        return _hls(_response(source.title, source.duration, source.live, video, profile=profile))
    normalized = providers.normalize_url(url)
    if normalized:
        url = normalized
    # Only CCTV's current official live metadata needs a custom extractor.
    # Other provider pages share the same all-format quality selection as
    # YouTube, so the proxy's MP4-first selector cannot silently lower quality.
    parsed = urlsplit(url)
    if normalized and providers._CCTV_LIVE.fullmatch(parsed.path) and providers._CCTV.fullmatch(parsed.hostname):
        try:
            source = providers.extract(url, cookies)
        except providers.ProviderError as error:
            raise NativeError(str(error)) from None
        subtitles = [{"url": _url(s.url), "headers": _headers(s.headers), "language": s.language,
                      "label": s.label, "format": "bilibili_json" if s.json_caption else "vtt"}
                     for s in source.subtitles]
        if profile == "native_mp4" and source.video.kind == "video" and not source.video.hls and source.video.mime != "video/mp4":
            raise NativeError("Maximum-quality video needs a native container index this client does not support")
        return _hls(_response(source.title, source.duration, source.live, _provider_track(source.video),
                              [_provider_track(a) for a in source.audio], subtitles, profile=profile))
    try:
        with YoutubeDL(_OPTIONS) as ydl:
            cookies.copy_into(ydl.cookiejar)
            info = ydl.extract_info(url, download=False)
            cookies.merge_from(ydl.cookiejar)
            if not info or info.get("_type") in {"playlist", "multi_video"}:
                raise NativeError("Specify one playable video or audio source")
            if info.get("has_drm"):
                raise NativeError("This media requires DRM and cannot be played natively")
            selected = info.get("requested_formats") or [info]
            if profile == "native_mp4":
                selected = _compatible_selection(info, selected)
            tracks = [_track(info, f) for f in selected if f.get("url")]
            video = next((t for t in tracks if t["kind"] != "audio"), None)
            audio = [t for t in tracks if t["kind"] == "audio"]
            if not tracks:
                raise NativeError("No native media format is available")
            if video is not None and video["kind"] == "muxed":
                audio = []
            # Preserve the selected best audio, then one highest-bitrate stream
            # for every other available language. Never add audio to muxed video.
            languages = {t["language"] for t in audio}
            if video is None or video["kind"] == "video":
                for fmt in sorted(info.get("formats") or [], key=lambda f: _number(f.get("tbr") or f.get("abr")), reverse=True):
                    language = fmt.get("language") or "und"
                    if (fmt.get("vcodec") == "none" and fmt.get("url") and not fmt.get("has_drm") and language not in languages
                            and (profile != "native_mp4" or _compatible_audio(fmt, video is not None and video["hls"]))):
                        audio.append(_track(info, fmt)); languages.add(language)
            captions = []
            for language, formats in (info.get("subtitles") or {}).items():
                fmt = next((f for f in formats if f.get("url") and f.get("ext") in {"vtt", "srt", "ttml", "json"}), None)
                if fmt:
                    caption = _subtitle(info, fmt, language)
                    if caption["format"] == "json" and (parsed.hostname == "b23.tv" or
                            (parsed.hostname or "").endswith("bilibili.com")):
                        caption["format"] = "bilibili_json"
                    captions.append(caption)
            result = _hls(_response(info.get("title"), info.get("duration"), info.get("is_live"), video, audio, captions, profile=profile))
            result["available_at"] = _number(info.get("available_at"))
            return result
    except DownloadError:
        # The extractor's full exception may include account or signed URL data.
        raise NativeError("The origin could not resolve this media; check access, availability and region") from None


def _attributes(line: str) -> dict[str, str]:
    return {m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
            for m in re.finditer(r'(?:^|[:,])([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', line)}


def _best(master: str, base: str) -> hls.Variant | None:
    lines = [line.strip() for line in master.splitlines()]
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _attributes(line)
        uri = next((value for value in lines[i + 1:] if value and not value.startswith("#")), None)
        if uri is None:
            continue
        groups = {attrs.get("AUDIO"), attrs.get("SUBTITLES")}
        renditions = []
        for value in lines:
            if value.startswith("#EXT-X-MEDIA:") and _attributes(value).get("GROUP-ID") in groups:
                renditions.append(re.sub(r'URI="([^"]*)"', lambda m: 'URI="' + _url(urljoin(base, m.group(1))) + '"', value))
        resolution = re.fullmatch(r"(\d+)x(\d+)", attrs.get("RESOLUTION", ""))
        height, width = (int(resolution[2]), int(resolution[1])) if resolution else (0, 0)
        try:
            rank = height, width, float(attrs.get("FRAME-RATE", "0")), int(attrs.get("BANDWIDTH", "0"))
        except ValueError:
            rank = height, width, 0, 0
        variants.append((rank, hls.Variant(_url(urljoin(base, uri)), line, tuple(renditions))))
    return max(variants, key=lambda x: x[0])[1] if variants else None


def _hls(result: dict) -> dict:
    video = result["video"]
    if video is None or not video["hls"]:
        return result
    try:
        with providers._http(video["url"], video["headers"]) as response:
            data = response.read(_MAX_PLAYLIST + 1)
            base = getattr(response, "url", None) or video["url"]
        if len(data) > _MAX_PLAYLIST:
            raise NativeError("The HLS playlist is too large")
        text = data.decode("utf-8-sig")
        if not text.lstrip().startswith("#EXTM3U"):
            raise NativeError("The origin did not return an HLS playlist")
        if re.search(r"METHOD=SAMPLE-AES", text) or any(
                value != "identity" for value in re.findall(r'KEYFORMAT="([^"]*)"', text)):
            raise NativeError("This HLS stream requires DRM and cannot be played natively")
        variant = _best(text, base)
        if variant is not None:
            video["url"] = variant.uri
            attrs = _attributes(variant.stream_inf)
            resolution = re.fullmatch(r"(\d+)x(\d+)", attrs.get("RESOLUTION", ""))
            if resolution:
                video["width"], video["height"] = map(int, resolution.groups())
            result["manifest"] = {"type": "hls", "content": hls.only(variant)}
            for line in variant.renditions:
                attrs = _attributes(line)
                if not attrs.get("URI"):
                    continue
                typ = attrs.get("TYPE")
                if typ == "AUDIO":
                    audio = dict(video) | {"url": attrs["URI"], "kind": "audio", "mime": "audio/mp4",
                                          "width": 0, "height": 0, "codec": "", "language": attrs.get("LANGUAGE", "und"),
                                          "label": attrs.get("NAME", attrs.get("LANGUAGE", ""))}
                    if not any(t["url"] == audio["url"] for t in result["audio"]):
                        result["audio"].append(audio)
                    video["kind"] = "video"
                elif typ == "SUBTITLES":
                    subtitle = {"url": attrs["URI"], "headers": dict(video["headers"]), "language": attrs.get("LANGUAGE", "und"),
                                "label": attrs.get("NAME", attrs.get("LANGUAGE", "")), "format": "hls_vtt"}
                    if not any(s["url"] == subtitle["url"] for s in result["subtitles"]):
                        result["subtitles"].append(subtitle)
        if result["manifest"] is None and video["kind"] == "video" and result["audio"]:
            if not all(t["hls"] for t in result["audio"]):
                raise NativeError("This origin mixes HLS video with unsupported external audio")
            lines = ["#EXTM3U"]
            for index, track in enumerate(result["audio"]):
                label = re.sub(r'["\r\n]', "", track["label"] or track["language"])
                language = re.sub(r'["\r\n]', "", track["language"])
                lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{label}",LANGUAGE="{language}",'
                             f'DEFAULT={"YES" if index == 0 else "NO"},AUTOSELECT=YES,URI="{track["url"]}"')
            lines += [f'#EXT-X-STREAM-INF:BANDWIDTH={video["bandwidth"]},AUDIO="audio"', video["url"]]
            result["manifest"] = {"type": "hls", "content": "\n".join(lines) + "\n"}
        # Media playlists keep refreshing at the origin. Never embed a stale live
        # segment window into a local file that VLC cannot refresh.
        return result
    except (OSError, UnicodeError, ValueError):
        raise NativeError("The origin's HLS playlist could not be read") from None


class NativeResolver:
    def __init__(self, cookies):
        self.cookies = cookies
        self._cache = OrderedDict()

    async def resolve(self, source: str, profile="native_mp4") -> dict:
        """One lookup per URL serves every player of it: the media URLs are the API's own (yt-dlp runs here), the
        same whichever client asks, and each game client asks again whenever it reopens a record."""
        if profile not in {"native_mp4", "best"}:
            raise NativeError("Unknown native playback profile")
        url = check_source(source)
        key = url, profile
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry is None or entry[0] <= now:
            entry = now + config.RESOLVE_CACHE_SECONDS, asyncio.create_task(asyncio.to_thread(_extract, url, self.cookies, profile))
            self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHE:
            self._cache.popitem(last=False)
        try:
            result = await asyncio.shield(entry[1])
        except Exception:
            if self._cache.get(key) is entry:
                del self._cache[key]
            raise
        if result.get("live") and self._cache.get(key) is entry and entry[0] > now + _LIVE_TTL:
            self._cache[key] = now + _LIVE_TTL, entry[1]
        return result
