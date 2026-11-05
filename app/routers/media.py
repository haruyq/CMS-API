"""The endpoints the CMS mod calls (spec: the mod's API.md)."""

import logging

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .. import extension, tver, providers
from .provider_media import open_video
from ..audio import Busy, SourceError, is_allowed
from ..video import SESSION_ID

log = logging.getLogger(__name__)
router = APIRouter()


def _check(url: str) -> str:
    if not is_allowed(url):
        raise HTTPException(403, "This host is not allowed")
    return url.strip()


def _to_extension(endpoint: str, request: Request) -> RedirectResponse:
    """An extension page's /video or /stream, which the extension serves itself (see extension.client_url)."""
    params = {k: v for k, v in request.query_params.items() if k not in ("exp", "sig")}
    try:
        return RedirectResponse(extension.client_url(endpoint, params), status_code=302)
    except extension.ExtensionError as error:
        raise HTTPException(503, str(error))


def _session(session: str | None) -> str | None:
    if session is not None and not SESSION_ID.fullmatch(session):
        raise HTTPException(400, "Invalid session")
    return session


@router.get("/stream")
async def stream(
    request: Request, url: str, start: float = Query(0, ge=0), channel: Literal["left", "right"] | None = None,
    session: str | None = None, format: Literal["pcm", "ogg"] = "pcm",
) -> Response:
    """Raw PCM s16le / 48 kHz / mono (or that as Ogg Vorbis, with `format=ogg`), from `start` seconds in; the whole
    mix, or one side with `channel`. A live TVer URL with the `session` of its /video plays from `start` seconds
    after that video started."""
    url, session = _check(url), _session(session)
    if extension.handles(url):
        return _to_extension("stream", request)
    try:
        source = synced = None
        if tver.is_live_url(url) or providers.is_live_url(url):
            provider = await request.app.state.resolver.provider(url) if providers.is_live_url(url) else None
            if provider is not None and (not provider.video.hls or provider.audio):
                source = await request.app.state.resolver.resolve(url)
            else:
                local = f"http://127.0.0.1:{request.scope['server'][1]}"
                video = request.app.state.video
                synced = session and await video.live_audio(url, session, start, local)
                source = synced or await video.live_now(url, local)
        audio = await request.app.state.streamer.open(url, start, channel, source, format == "ogg")
    except Busy:
        raise HTTPException(503, "Too many streams")
    except SourceError as error:
        log.warning("Could not stream %s: %s", url, error)
        raise HTTPException(502, str(error))

    async def close() -> None:
        await audio.aclose()

    # When the listener hangs up, Starlette just stops reading the stream and never closes it, so its cleanup
    # (killing ffmpeg, freeing the slot) never ran; MAX_STREAMS hang-ups later every request got 503. The
    # background task runs once the response is over, whichever way, and closes it.
    return StreamingResponse(audio, media_type="audio/ogg" if format == "ogg" else "application/octet-stream",
                             background=BackgroundTask(close),
                             # Tells the mod this live audio is cut to its session's video, so it may follow it.
                             headers={"X-Video-Sync": "1"} if synced else None)


@router.get("/extension/patterns")
async def extension_patterns() -> dict[str, list[str]]:
    """The pages the extension plays (whole-URL regular expressions), which the mod sends to /video and /stream."""
    return extension.patterns()


@router.get("/info")
async def info(request: Request, url: str) -> dict[str, float | str | None]:
    """Track length in seconds (null for live or unknown) and title. Also tells the mod the URL is playable."""
    try:
        source = await request.app.state.resolver.resolve(_check(url))
    except SourceError as error:
        log.warning("Could not resolve %s: %s", url, error)
        raise HTTPException(502, str(error))
    return {"duration": source.duration, "title": source.title}


_HLS = "application/vnd.apple.mpegurl"


@router.get("/video")
async def video(request: Request, url: str, session: str | None = None) -> Response:
    """HLS video of a TVer URL (an extension's page: from the extension itself), for players that cannot read those pages (plain WaterMedia):
    a master playlist offering only the best quality (players otherwise start low and stay there), or, for a live
    broadcast in a `session`, that quality's playlist itself (see app/video.py). TVer's playlist URLs expire, so
    they are looked up again after RESOLVE_CACHE_SECONDS."""
    url, session = _check(url), _session(session)
    if extension.handles(url):
        return _to_extension("video", request)
    if providers.is_provider_url(url):
        return await open_video(request, url, session)
    if not tver.is_tver_url(url):
        raise HTTPException(400, "Only TVer, CCTV, bilibili, iwara and extension video URLs are supported")
    try:
        if session and tver.is_live_url(url):
            return Response(await request.app.state.video.live_playlist(url, session), media_type=_HLS)
        master = await request.app.state.video.master(url)
        if master is None:
            return RedirectResponse((await request.app.state.resolver.video(url)).url, status_code=302)
    except SourceError as error:
        log.warning("Could not resolve video %s: %s", url, error)
        raise HTTPException(502, str(error))
    return Response(master, media_type=_HLS)


@router.get("/video/asset/{key}.{extension}")
async def video_asset(request: Request, key: str, extension: str) -> Response:
    """A private provider resource; its origin and credentials stay in the API."""
    try:
        data, media_type = await request.app.state.video.asset(key, extension)
        return Response(data, media_type=media_type, headers={"Cache-Control": "no-store"})
    except SourceError as error:
        raise HTTPException(404, str(error))


@router.get("/video/segment")
async def video_segment(request: Request, session: str, seq: int) -> RedirectResponse:
    """A segment of a live /video playlist: redirects to the real one, noting where the viewer's video started."""
    try:
        return RedirectResponse(request.app.state.video.segment(_session(session), seq), status_code=302)
    except SourceError as error:
        raise HTTPException(404, str(error))


@router.get("/video/audio/{key}.m3u8")
async def video_audio(request: Request, key: str, first: int | None = Query(None, alias="from")) -> Response:
    """A live audio playlist (from segment `from` on, if given): the input of the API's own ffmpeg for live audio."""
    try:
        return Response(await request.app.state.video.audio_playlist(key, first), media_type=_HLS)
    except SourceError as error:
        raise HTTPException(404, str(error))


@router.get("/video/audio/{key}/{seq}.ts")
async def video_audio_segment(request: Request, key: str, seq: int) -> Response:
    """A segment of a live audio playlist, downloaded once for all the streams reading it."""
    try:
        return Response(await request.app.state.video.audio_segment(key, seq), media_type="video/mp2t")
    except SourceError as error:
        raise HTTPException(404, str(error))
