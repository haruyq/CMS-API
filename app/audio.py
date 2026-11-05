"""Resolves a source URL with yt-dlp and streams it through ffmpeg as the PCM format CMS plays."""

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import config, extension, tver, providers
from .cookies import CookieStore

log = logging.getLogger(__name__)

# Raw s16le, 48 kHz, mono: what the mod expects (see the mod's API.md).
PCM_FORMAT = ("-f", "s16le", "-ac", "1", "-ar", "48000")
# format=ogg: the same audio as Ogg Vorbis, ~80 kbit/s instead of PCM's 768; the mod decodes it with Minecraft's own
# Vorbis decoder.
OGG_FORMAT = ("-c:a", "libvorbis", "-q:a", "4", "-f", "ogg", "-ac", "1", "-ar", "48000")
# channel=left/right: one side of the stereo mix, for a speaker pair. Mono sources are upmixed first,
# so both sides get the sound; still delivered as mono, so each speaker stays a positional source.
CHANNEL_FILTERS = {
    "left": "aformat=channel_layouts=stereo,pan=mono|c0=c0",
    "right": "aformat=channel_layouts=stereo,pan=mono|c0=c1",
}
PCM_BYTES_PER_SECOND = 48_000 * 2
# An ffmpeg error this close to the known end of the track is about the tail (trailing packets, the input
# connection dropping after the last byte), not lost audio, so the stream still ends normally.
END_TOLERANCE_SECONDS = 3.0
CHUNK_BYTES = 64 * 1024
_YDL_OPTIONS = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": "bestaudio/best"}


class SourceError(Exception):
    """The URL could not be resolved or decoded."""


class Busy(Exception):
    """All MAX_STREAMS slots are in use."""


@dataclass(frozen=True)
class Source:
    media_url: str
    headers: dict[str, str]
    duration: float | None
    title: str | None
    # Unix time the media URL starts working. YouTube holds it back for the length of its pre-roll ad
    # (or until the ad could be skipped) and answers 403 before then; yt-dlp's own downloader waits too.
    available_at: float = 0
    # A live broadcast: always played from where it is now, so a start offset is ignored.
    live: bool = False
    # Live, from a playlist cut to begin at the wanted segment: seconds to drop from that segment's start.
    trim: float | None = None


def is_allowed(url: str) -> bool:
    """http(s) URL on an allowed host or its subdomain. Mirrors the mod's UrlPolicy."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and any(
        host == allowed or host.endswith("." + allowed) for allowed in config.ALLOWED_HOSTS
    )


class Resolver:
    """Source lookups (yt-dlp, or TVer's own for live and video), cached per URL. Concurrent requests for one
    URL share a single lookup."""

    def __init__(self, cookies: CookieStore):
        self._cookies = cookies
        # ponytail: unbounded until entries expire; an LRU cap if many distinct URLs arrive within the TTL.
        self._cache: dict[tuple[str, str], tuple[float, asyncio.Task]] = {}

    async def resolve(self, url: str) -> Source:
        """The audio of `url`, for /stream and /info."""
        if providers.is_provider_url(url):
            media = await self.provider(url)
            track = media.audio[0] if media.audio else media.video
            return Source(track.url, track.headers, media.duration, media.title, live=media.live)
        if extension.handles(url):
            # An extension may stop idle playback. A short cache rechecks its state
            # before the much longer yt-dlp cache could outlive that playback.
            hls = await self._cached(("extension", url.strip()), _extension_video, ttl=5)
            return Source(hls.url, hls.headers, hls.duration, hls.title, live=hls.live,
                          trim=None if hls.live else 0)
        return await self._cached(("audio", url), self._extract)

    async def video(self, url: str) -> tver.Hls:
        """The HLS playlist (video and audio) of a TVer or provider URL, for /video."""
        if providers.is_provider_url(url):
            media = await self.provider(url)
            if not media.video.hls:
                raise SourceError("This source uses the provider media endpoint")
            return tver.Hls(media.video.url, media.video.headers, media.title, media.live, media.duration, True)
        return await self._cached(("video", url), _tver_video)

    async def provider(self, url: str) -> providers.Media:
        normalized = providers.normalize_url(url)
        if normalized is None:
            raise SourceError("Unsupported provider video URL")
        try:
            return await self._cached(("provider", normalized), lambda value: providers.extract(value, self._cookies),
                                      ttl=60 if providers.is_live_url(normalized) else None)
        except providers.ProviderError as error:
            raise SourceError(str(error)) from error

    async def _cached(self, key: tuple[str, str], lookup, ttl: float | None = None):
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry is None or entry[0] < now:
            self._cache = {k: v for k, v in self._cache.items() if v[0] >= now}
            entry = (now + (config.RESOLVE_CACHE_SECONDS if ttl is None else ttl),
                     asyncio.create_task(asyncio.to_thread(lookup, key[1])))
            self._cache[key] = entry
        try:
            # shield: one listener disconnecting must not cancel the lookup others are waiting on.
            return await asyncio.shield(entry[1])
        except Exception:
            if self._cache.get(key) is entry:
                del self._cache[key]
            raise

    def _extract(self, url: str) -> Source:
        if tver.is_live_url(url):
            hls = _tver_video(url)
            return Source(hls.url, hls.headers, None, hls.title, live=True)
        with YoutubeDL(_YDL_OPTIONS) as ydl:
            self._cookies.copy_into(ydl.cookiejar)
            try:
                info = ydl.extract_info(url, download=False)
            except DownloadError as error:
                raise SourceError(str(error).removeprefix("ERROR: ")) from error
            self._cookies.merge_from(ydl.cookiejar)
        if not info.get("url"):
            raise SourceError("No playable audio format")
        duration = None if info.get("is_live") else info.get("duration")
        if duration is None and not info.get("is_live"):
            duration = _probe_duration(info["url"])
        return Source(info["url"], info.get("http_headers") or {}, duration, info.get("title"),
                      info.get("available_at") or 0, bool(info.get("is_live")))


def _extension_video(url: str) -> tver.Hls:
    try:
        return extension.resolve(url)
    except extension.ExtensionError as error:
        raise SourceError(str(error)) from error


def _tver_video(url: str) -> tver.Hls:
    try:
        return tver.resolve(url)
    except tver.TverError as error:
        raise SourceError(str(error)) from error
    except (OSError, KeyError, ValueError) as error:  # TVer or STREAKS answered something unexpected
        raise SourceError(f"TVer lookup failed: {error}") from error


def _probe_duration(media_url: str) -> float | None:
    """Direct files (e.g. Discord attachments) carry no duration in yt-dlp's info; ask ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", media_url],
            capture_output=True, timeout=15, check=True,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except (subprocess.SubprocessError, OSError, KeyError, ValueError):
        return None


class Streamer:
    def __init__(self, resolver: Resolver):
        self._resolver = resolver
        self._slots = asyncio.Semaphore(config.MAX_STREAMS)
        self.active = 0

    @property
    def idle(self) -> bool:
        return self.active == 0

    def _release(self) -> None:
        self.active -= 1
        self._slots.release()

    async def open(self, url: str, start: float, channel: str | None = None,
                   source: Source | None = None, ogg: bool = False) -> AsyncIterator[bytes]:
        """Starts ffmpeg and waits for the first audio, so failures surface as an error status, not an empty 200.
        `source`, if given, is played instead of looking `url` up."""
        if self._slots.locked():
            raise Busy
        await self._slots.acquire()
        self.active += 1
        try:
            source = source or await self._resolver.resolve(url)
            # Only the first requests after a lookup wait; the cache serves the URL once it works.
            if (wait := source.available_at - time.time()) > 0:
                await asyncio.sleep(wait)
            process = await asyncio.create_subprocess_exec(
                *_ffmpeg_args(source, start, channel, ogg), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except BaseException:
            self._release()
            raise
        try:
            first = await process.stdout.read(CHUNK_BYTES)
        except BaseException:  # the listener gave up while ffmpeg was still connecting
            process.kill()
            self._release()  # before the await, which a cancelled task doesn't get past (see _pipe)
            await process.wait()
            raise
        if not first:
            error = (await process.stderr.read()).decode("utf-8", "replace").strip()
            await process.wait()
            self._release()
            if source.duration is not None and start >= source.duration - END_TOLERANCE_SECONDS:
                # A client resuming a broken-off stream right at the end: nothing left is the right answer.
                return _nothing()
            raise SourceError(error[-500:] or "ffmpeg produced no audio")
        expected = None if source.duration is None else max(source.duration - start, 0)
        return self._pipe(process, first, expected, ogg)

    async def _pipe(self, process: asyncio.subprocess.Process, first: bytes,
                    expected_seconds: float | None = None, ogg: bool = False) -> AsyncIterator[bytes]:
        try:
            yield first
            sent, tail, ogg_seconds = len(first), first[-13:], ogg and _ogg_seconds(first) or 0.0
            while chunk := await process.stdout.read(CHUNK_BYTES):
                sent += len(chunk)
                if ogg:
                    ogg_seconds = _ogg_seconds(tail + chunk) or ogg_seconds
                    tail = chunk[-13:]  # an Ogg page header cut off at the end of the chunk, up to its granule
                yield chunk
            if await process.wait() != 0:
                error = (await process.stderr.read()).decode("utf-8", "replace").strip()[-500:]
                seconds = ogg_seconds if ogg else sent / PCM_BYTES_PER_SECOND
                if expected_seconds is not None and seconds >= expected_seconds - END_TOLERANCE_SECONDS:
                    log.info("ffmpeg exited %d at the end of the track (%.1fs of %.1fs): %s",
                             process.returncode, seconds, expected_seconds, error)
                    return
                log.warning("ffmpeg failed after %.1fs of audio: %s", seconds, error)
                # Raising after the headers went out makes the server drop the connection mid-body,
                # which the mod reports as an error during playback instead of a silent early end.
                raise SourceError(error or f"ffmpeg exited with {process.returncode}")
        finally:
            # Runs when the track ends and when the listener disconnects (the mod closes on stop). Free the slot
            # before awaiting anything: on a disconnect this runs inside a cancelled task, where every await is
            # cancelled again, so nothing after the first await would run.
            if process.returncode is None:
                process.kill()
            self._release()
            await process.wait()


async def _nothing() -> AsyncIterator[bytes]:
    return
    yield


def _ogg_seconds(data: bytes) -> float | None:
    """Audio time reached by the last Ogg page whose header is in `data` (its granule position, in 48 kHz samples);
    None if there is none."""
    end = len(data)
    while (i := data.rfind(b"OggS", 0, end)) >= 0:
        granule = int.from_bytes(data[i + 6:i + 14], "little", signed=True)
        if i + 14 <= len(data) and granule >= 0:  # -1: no packet ends on this page
            return granule / 48_000
        end = i
    return None


def _ffmpeg_args(source: Source, start: float, channel: str | None = None, ogg: bool = False) -> list[str]:
    headers = "".join(f"{name}: {value}\r\n" for name, value in source.headers.items())
    seek = ["-ss", f"{start:.3f}"] if start > 0 and not source.live else []
    # A cut live playlist: start at its first segment (not ffmpeg's default of three from the end), then trim.
    cut = (["-live_start_index", "0"], ["-ss", f"{source.trim:.3f}"]) if source.trim is not None else ([], [])
    # TVer splices its ads into a live playlist as other MPEG-TS streams (new PIDs); ffmpeg only outputs the streams
    # it started with, so the audio stopped dead at the first ad. This carries the old ones over to the new PIDs.
    # HLS only: other inputs reject the option.
    hls = ["-seg_format_options", "merge_pmt_versions=1"] if ".m3u8" in urlsplit(source.media_url).path else []
    filters = [CHANNEL_FILTERS[channel]] if channel else []
    if ogg:
        # Match PCM downmix headroom: float Vorbis otherwise adds 3 dB
        # to correlated stereo and can clip the speaker decoder.
        if channel is None:
            filters.append("aresample=ochl=mono:rematrix_maxval=1")
        # Timestamps from the sample count: they jump at every ad splice, which raw PCM ignores but the Vorbis
        # encoder stalled on.
        filters.append("asetpts=N/SR/TB")
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        # Network inputs only; keeps a crafted playlist from reading local files.
        "-protocol_whitelist", "http,https,tls,tcp,crypto,hls,data",
        # The client reads at playback speed, so the input connection idles and can be dropped; reconnect.
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
        *(["-headers", headers] if headers else []),
        *hls, *seek, *cut[0], "-i", source.media_url, *cut[1],
        "-vn", *(["-af", ",".join(filters)] if filters else []), *(OGG_FORMAT if ogg else PCM_FORMAT), "pipe:1",
    ]
