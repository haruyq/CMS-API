"""Small metadata-only native playback endpoint."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response

from ..native import NativeError

router = APIRouter()


@router.get("/resolve")
async def resolve(request: Request, response: Response, url: str, profile: Literal["native_mp4", "best"] = "native_mp4"):
    # Signed origin URLs must not become an intermediary's shared response cache.
    response.headers["Cache-Control"] = "no-store"
    try:
        return await request.app.state.native.resolve(url, profile)
    except PermissionError:
        raise HTTPException(403, "This host is not allowed") from None
    except NativeError as error:
        raise HTTPException(502, str(error)) from None
