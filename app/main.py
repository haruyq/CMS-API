"""CMS API: run with `uvicorn app.main:app`. Single process on purpose: the caches and cookies live in memory."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config, extension
from .audio import Resolver, Streamer
from .cookies import CookieStore, refresh_forever
from .native import NativeResolver
from .routers import diagnostics, native, media, youtube_auth, provider_media, p2p
from .updater import update_forever
from .video import Video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cookies = CookieStore(config.COOKIE_FILE)
    cookies.load()
    app.state.cookies = cookies
    app.state.resolver = Resolver(cookies)
    app.state.native = NativeResolver(cookies)
    app.state.streamer = Streamer(app.state.resolver)
    app.state.video = Video(app.state.resolver)
    tasks = [asyncio.create_task(refresh_forever(cookies, config.COOKIE_REFRESH_MINUTES))]
    if config.EXTENSION_URL:
        tasks.append(asyncio.create_task(extension.keep_patterns()))
    if config.YTDLP_UPDATE_HOURS > 0:
        tasks.append(asyncio.create_task(update_forever(config.YTDLP_UPDATE_HOURS, lambda: app.state.streamer.idle)))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Create: Media Systems API", lifespan=lifespan)
app.include_router(media.router)
app.include_router(native.router)
app.include_router(youtube_auth.router)
app.include_router(provider_media.router)
app.include_router(diagnostics.router)
app.include_router(p2p.router)
