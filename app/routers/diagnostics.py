"""Playback diagnostics the mod's clients send (see the mod's API.md), kept as JSON lines for the admin to read."""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import config
from .youtube_auth import _admin

_MAX_BODY_BYTES = 512 * 1024

router = APIRouter(prefix="/diagnostics")


@router.post("", status_code=204)
async def receive(request: Request) -> Response:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(413, "Report is too large")
    try:
        report = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Report is not JSON") from None
    if not isinstance(report, dict):
        raise HTTPException(400, "Report is not a JSON object")
    path = config.DIAGNOSTICS_FILE
    # ponytail: one file with a size cap; rotate it if reports are kept for longer than one investigation.
    if path.exists() and path.stat().st_size > config.DIAGNOSTICS_MAX_MB * 1024 * 1024:
        raise HTTPException(507, "Diagnostics storage is full")
    report["receivedAt"] = datetime.now(timezone.utc).isoformat()
    # Records' audio goes through this API: whether its own YouTube login worked then (None: unchecked).
    cookies = getattr(request.app.state, "cookies", None)
    report["apiYoutubeLoggedIn"] = cookies.logged_in if cookies is not None else None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(report, ensure_ascii=False) + "\n")
    return Response(status_code=204)


@router.get("", dependencies=[Depends(_admin)])
async def download() -> Response:
    # Read once, not stat then open (FileResponse): a report appended meanwhile made it send more than it announced.
    try:
        body = await asyncio.to_thread(config.DIAGNOSTICS_FILE.read_bytes)
    except FileNotFoundError:
        raise HTTPException(404, "No diagnostics yet") from None
    return Response(body, media_type="application/x-ndjson",
                    headers={"Content-Disposition": 'attachment; filename="diagnostics.jsonl"'})
