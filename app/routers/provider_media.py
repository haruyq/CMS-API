"""Native-quality provider video manifests and bounded, range-aware origin proxy."""

import asyncio
import re
import urllib.error

import anyio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app import providers
from app.audio import SourceError

router = APIRouter()


def videos(request):
    state = request.app.state
    if not hasattr(state, "provider_videos"):
        state.provider_videos = providers.Videos(state.resolver)
    return state.provider_videos


async def open_video(request, url, session=None):
    try:
        if session and providers.is_live_url(url):
            media = await request.app.state.resolver.provider(url)
            if media.video.hls and not media.audio:
                return Response(await request.app.state.video.live_playlist(url, session), media_type="application/vnd.apple.mpegurl")
        kind, body = await videos(request).open(url, str(request.base_url))
        return RedirectResponse(body, status_code=302) if kind == "redirect" else Response(body, media_type=kind,
                                                                                       headers={"Cache-Control": "no-store"})
    except (providers.ProviderError, SourceError) as error:
        raise HTTPException(502, str(error)) from error


@router.get("/video/provider/{key}.{extension}")
async def asset(request: Request, key: str, extension: str):
    manager = videos(request)
    try:
        entry = manager.get(key, extension)
        if entry.kind == "hls":
            text = await asyncio.to_thread(manager.playlist, entry, str(request.base_url))
            return Response(text, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
        if entry.kind in {"vtt", "json"}:
            body = await asyncio.to_thread(providers.captions, entry)
            return Response(body, media_type="text/vtt", headers={"Cache-Control": "no-store"})
    except providers.ProviderError as error:
        raise HTTPException(404, str(error)) from error
    requested = request.headers.get("Range")
    if requested and not re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", requested):
        raise HTTPException(416, "Only a single byte range is supported")
    try:
        origin = await asyncio.to_thread(providers._http, entry.url, entry.headers, requested)
    except urllib.error.HTTPError as error:
        status = 416 if error.code == 416 else 502
        raise HTTPException(status, "配信元の映像データを取得できませんでした") from None
    except OSError:
        raise HTTPException(502, "配信元に接続できませんでした") from None
    headers = {name: origin.headers[name] for name in ["Content-Range", "Content-Length", "Accept-Ranges", "ETag", "Last-Modified"]
               if origin.headers.get(name) is not None}
    headers["Cache-Control"] = "no-store"
    async def close():
        # A disconnect can cancel the pending origin.read. Close off the event
        # loop and shield it from Starlette/AnyIO's repeated cancellation.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(origin.close)
    async def chunks():
        try:
            while data := await asyncio.to_thread(origin.read, 64 * 1024):
                yield data
        finally:
            await close()
    return StreamingResponse(chunks(), status_code=origin.status,
                             media_type=origin.headers.get("Content-Type") or "application/octet-stream",
                             headers=headers, background=BackgroundTask(close))
