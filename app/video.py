"""TVer and extension video for WaterMedia (/video), and live audio aligned to that video.

On demand, /video redirects to the program's best variant. Live, it hands out the best variant's playlist itself,
each segment pointing back here (a redirect to the real one): so the API sees which segment a viewer's player
started with, and so the broadcast time its video clock counts from. Audio asked for in the same session is then
cut from that same broadcast, `start` seconds after it, which a player's own choice of where to join a live
stream would otherwise put seconds apart.

Live audio is read from the lowest-quality variant: TVer muxes the same audio, bit for bit, into every quality, and
the lowest is a third of the bytes of the best. Its segments come through here too, each downloaded from TVer once
however many streams read it (every speaker, of every player, in every session, on the same broadcast).
"""

import asyncio
import dataclasses
import hashlib
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from . import hls, tver
from .audio import Resolver, Source, SourceError

SESSION_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
_SESSION_IDLE_SECONDS = 600
_MAX_SESSIONS = 500
# A live playlist changes once a segment (a few seconds); viewers and audio streams all reload it.
_PLAYLIST_CACHE_SECONDS = 1.0
# Segments remembered after they leave the live playlist (TVer lists only the last ~20 s, while a player joins
# ~12 s behind and the audio is asked for seconds after that); the CDN keeps them well beyond this.
_HISTORY_SEGMENTS = 60
# ponytail: oldest-first by count; TVer's lowest quality is ~400 KB per 4 s segment, so ~50 MB. A byte cap if
# much bigger segments ever come through.
_SEGMENT_CACHE = 120
_TIMEOUT = 15


@dataclass
class _Session:
    url: str
    variant: str
    headers: dict[str, str]
    # The lowest-quality variant, which the session's audio is cut from.
    audio: str
    proxy: bool = False
    # The earliest segment the viewer's player fetched: (sequence, broadcast time it starts at). Players fetch the
    # last segment first to learn the format, then go back to where they start, so it is the lowest, not the first.
    first: tuple[int, float] | None = None
    uris: dict[int, str] = field(default_factory=dict)
    seen: float = field(default_factory=time.monotonic)


class Video:
    def __init__(self, resolver: Resolver):
        self._resolver = resolver
        self._sessions: dict[str, _Session] = {}
        self._variants: dict[str, tuple[hls.Variant | None, hls.Variant | None]] = {}
        self._playlists: dict[str, tuple[float, hls.MediaPlaylist]] = {}
        self._history: dict[str, dict[int, hls.Segment]] = {}
        # Audio playlists the API's own ffmpeg reads: short key -> (variant playlist URL, headers).
        self._audio: dict[str, tuple[str, dict[str, str]]] = {}
        self._segments: dict[str, asyncio.Task[bytes]] = {}
        self._assets: dict[str, tuple[str, dict[str, str], bool]] = {}
        self._asset_ids: dict[tuple[str, tuple], str] = {}

    async def master(self, url: str) -> str | None:
        """The best quality's playlist. Private/browser media is proxied; None
        permits a redirect only for a public, single-quality source."""
        (best, _), source = await self.variants(url)
        if source.proxy:
            try:
                text = hls.only(best) if best else await asyncio.to_thread(_get, source.url, source.headers)
            except OSError as error:
                raise SourceError("Private video playlist is no longer available") from error
            return self._proxy_playlist(text, source.url, source.headers)
        return hls.only(best) if best else None

    async def variants(self, url: str) -> tuple[tuple[hls.Variant | None, hls.Variant | None], tver.Hls]:
        """The best and the lowest variant of a TVer URL's master playlist, which lists several qualities."""
        source = await self._resolver.video(url)
        if source.url not in self._variants:
            try:
                master = await asyncio.to_thread(_get, source.url, source.headers)
            except OSError as error:
                raise SourceError(f"Video playlist failed: {error}") from error
            if len(self._variants) > 200:  # one per resolved TVer URL; plenty
                self._variants.clear()
            self._variants[source.url] = (hls.best_variant(master, source.url),
                                          hls.best_variant(master, source.url, lowest=True))
        return self._variants[source.url], source

    async def live_playlist(self, url: str, session: str) -> str:
        """The live playlist for a viewer, with every segment fetched through `segment` below."""
        entry = await self._session(url, session)
        playlist = await self._playlist(entry.variant, entry.headers)
        entry.uris = {s.sequence: s.uri for s in playlist.segments}
        return playlist.render(lambda s: True, lambda s: f"video/segment?session={session}&seq={s.sequence}")

    def segment(self, session: str, sequence: int) -> str:
        """The real address of a segment a viewer's player asks for, noting where its video starts."""
        entry = self._sessions.get(session)
        if entry is None or sequence not in entry.uris:
            raise SourceError("Unknown segment")
        entry.seen = time.monotonic()
        if entry.first is None or sequence < entry.first[0]:
            starts_at = self._starts_at(entry, sequence)
            if starts_at is not None:
                entry.first = (sequence, starts_at)
        uri = entry.uris[sequence]
        # Relative to /video/segment, preserving an API mounted under a prefix.
        return "../" + self._asset_url(uri, entry.headers, entry.variant) if entry.proxy else uri

    async def asset(self, key: str, extension: str) -> tuple[bytes, str]:
        """A media resource on a private origin, addressed by an opaque capability.

        Never accept a client-supplied origin URL or forward a client's headers.
        Rewriting child playlists keeps the relay address and token server-side.
        """
        entry = self._assets.get(key)
        if entry is None:
            raise SourceError("Unknown video resource")
        url, headers, playlist = entry
        if extension != ("m3u8" if playlist else "ts"):
            raise SourceError("Unknown video resource")
        try:
            data = await asyncio.to_thread(_fetch, url, headers)
        except OSError as error:
            raise SourceError("Video resource is no longer available") from error
        if playlist:
            data = self._proxy_playlist(data.decode("utf-8"), url, headers, "../asset/").encode()
        return data, "application/vnd.apple.mpegurl" if playlist else "video/mp2t"

    def _asset_url(self, url: str, headers: dict[str, str], base: str, prefix: str = "video/asset/") -> str:
        parsed, origin = urlsplit(url), urlsplit(base)
        # In particular, never send an extension's bearer token to a URL
        # from a malicious/invalid absolute URI in a playlist.
        if (parsed.scheme not in ("http", "https") or parsed.username or parsed.password
                or (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc)):
            raise SourceError("Private video playlist refers to a different origin")
        identity = (url, tuple(sorted(headers.items())))
        key = self._asset_ids.get(identity)
        if key is None:
            key = uuid.uuid4().hex
            self._asset_ids[identity] = key
            self._assets[key] = (url, headers, parsed.path.endswith(".m3u8"))
            while len(self._assets) > 10_000:
                old = next(iter(self._assets))
                old_url, old_headers, _ = self._assets.pop(old)
                self._asset_ids.pop((old_url, tuple(sorted(old_headers.items()))), None)
        return prefix + key + (".m3u8" if self._assets[key][2] else ".ts")

    def _proxy_playlist(self, text: str, base: str, headers: dict[str, str], prefix: str = "video/asset/") -> str:
        if not text.lstrip().startswith("#EXTM3U"):
            raise SourceError("Invalid private video playlist")

        def uri(value: str) -> str:
            return self._asset_url(urljoin(base, value), headers, base, prefix)

        lines = []
        for line in text.splitlines():
            if line.startswith("#"):
                line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{uri(m[1])}"', line)
            elif line.strip():
                line = uri(line.strip())
            lines.append(line)
        return "\n".join(lines) + "\n"

    async def live_audio(self, url: str, session: str, start: float, local_base: str) -> Source | None:
        """Audio from `start` seconds after the viewer's video started; None before its player has begun, or when
        that point isn't among the segments known (the game was paused for long, and the playlist went stale)."""
        entry = self._sessions.get(session)
        if entry is None or entry.url != url or entry.first is None:
            return None
        entry.seen = time.monotonic()
        target = entry.first[1] + start
        # Every quality is cut at the same broadcast times, so the video's clock places the audio variant too.
        playlist = await self._recent(entry.audio, entry.headers)
        timed = [s for s in playlist.segments if s.starts_at is not None]
        if not timed or not timed[0].starts_at <= target < timed[-1].starts_at + timed[-1].duration:
            return None
        segment = next(s for s in reversed(timed) if s.starts_at <= target)
        trim = target - segment.starts_at
        playlist_url = self._audio_url(entry.audio, entry.headers, local_base, segment.sequence)
        return Source(playlist_url, entry.headers, None, None, live=True, trim=trim)

    async def live_now(self, url: str, local_base: str) -> Source:
        """Audio of the broadcast as it is now, for a stream outside a /video session."""
        (_, lowest), source = await self.variants(url)
        playlist_url = self._audio_url(lowest.uri if lowest else source.url, source.headers, local_base)
        return Source(playlist_url, source.headers, None, None, live=True)

    async def audio_playlist(self, key: str, first: int | None) -> str:
        """An audio playlist for the API's own ffmpeg (see `live_audio`, `live_now`): from segment `first` on, or
        the live playlist as it is, each segment fetched through `audio_segment`."""
        variant, headers = self._audio_entry(key)
        if first is None:
            playlist, keep = await self._playlist(variant, headers), lambda s: True
        else:
            playlist, keep = await self._recent(variant, headers), lambda s: s.sequence >= first
        # A path ending in .ts: ffmpeg's HLS reader refuses segments without a media extension.
        return playlist.render(keep, lambda s: f"{key}/{s.sequence}.ts")

    async def audio_segment(self, key: str, sequence: int) -> bytes:
        """A segment of an audio playlist, downloaded once for every stream that reads it."""
        variant, headers = self._audio_entry(key)
        segment = self._history.get(variant, {}).get(sequence)
        if segment is None:
            raise SourceError("Unknown segment")
        task = self._segments.get(segment.uri)
        if task is None:
            task = self._segments[segment.uri] = asyncio.create_task(asyncio.to_thread(_fetch, segment.uri, headers))
            while len(self._segments) > _SEGMENT_CACHE:
                del self._segments[next(iter(self._segments))]
        try:
            # shield: one stream hanging up must not cancel the download the others are waiting on.
            return await asyncio.shield(task)
        except OSError as error:
            if self._segments.get(segment.uri) is task:
                del self._segments[segment.uri]
            raise SourceError(f"Segment failed: {error}") from error

    def _audio_url(self, variant: str, headers: dict[str, str], local_base: str, first: int | None = None) -> str:
        key = hashlib.sha256(variant.encode()).hexdigest()[:16]
        if len(self._audio) > 200:  # one per resolved TVer URL; drop those no stream reloaded for a minute
            self._audio = {k: v for k, v in self._audio.items() if v[0] in self._playlists}
        self._audio[key] = (variant, headers)
        return f"{local_base}/video/audio/{key}.m3u8" + (f"?from={first}" if first is not None else "")

    def _audio_entry(self, key: str) -> tuple[str, dict[str, str]]:
        entry = self._audio.get(key)
        if entry is None:
            raise SourceError("Unknown audio playlist")
        return entry

    async def _session(self, url: str, session: str) -> _Session:
        entry = self._sessions.get(session)
        if entry is None or entry.url != url:
            (best, lowest), source = await self.variants(url)
            now = time.monotonic()
            self._sessions = {k: v for k, v in self._sessions.items() if now - v.seen < _SESSION_IDLE_SECONDS}
            if len(self._sessions) >= _MAX_SESSIONS:
                del self._sessions[min(self._sessions, key=lambda k: self._sessions[k].seen)]
            entry = self._sessions[session] = _Session(url, best.uri if best else source.url, source.headers,
                                                       lowest.uri if lowest else source.url, proxy=source.proxy)
        entry.seen = time.monotonic()
        return entry

    async def _playlist(self, variant: str, headers: dict[str, str]) -> hls.MediaPlaylist:
        cached = self._playlists.get(variant)
        if cached is None or time.monotonic() - cached[0] > _PLAYLIST_CACHE_SECONDS:
            try:
                text = await asyncio.to_thread(_get, variant, headers)
            except OSError as error:
                raise SourceError(f"Live playlist failed: {error}") from error
            cached = (time.monotonic(), hls.parse_media(text, variant))
            now = cached[0]
            self._playlists = {k: v for k, v in self._playlists.items() if now - v[0] < 60}
            self._history = {k: v for k, v in self._history.items() if k in self._playlists}
            self._playlists[variant] = cached
            history = self._history.setdefault(variant, {})
            history.update((s.sequence, s) for s in cached[1].segments)
            for old in sorted(history)[:-_HISTORY_SEGMENTS]:
                del history[old]
        return cached[1]

    async def _recent(self, variant: str, headers: dict[str, str]) -> hls.MediaPlaylist:
        """The live playlist, with the segments that already left it put back in front."""
        playlist = await self._playlist(variant, headers)
        segments = tuple(seg for _, seg in sorted(self._history.get(variant, {}).items()))
        if not playlist.segments or not segments:
            return playlist
        earlier = [s for s in segments if s.sequence < playlist.segments[0].sequence]
        return dataclasses.replace(playlist, segments=segments, discontinuity_sequence=max(
            playlist.discontinuity_sequence - sum(s.discontinuity for s in earlier), 0))

    def _starts_at(self, entry: _Session, sequence: int) -> float | None:
        cached = self._playlists.get(entry.variant)
        segment = next((s for s in cached[1].segments if s.sequence == sequence), None) if cached else None
        return segment.starts_at if segment else None


def _fetch(url: str, headers: dict[str, str]) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=_TIMEOUT) as response:
        return response.read()


def _get(url: str, headers: dict[str, str]) -> str:
    return _fetch(url, headers).decode("utf-8", "replace")
