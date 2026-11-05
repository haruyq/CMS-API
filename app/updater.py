"""Keeps yt-dlp current: YouTube changes often, and an old yt-dlp soon stops resolving it.

yt-dlp is imported as a library, so a new version only takes effect in a new process. After an update the
server waits until no stream is playing, then stops itself; Docker's restart policy starts it again.
"""

import asyncio
import importlib.metadata
import logging
import os
import signal
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)


async def update_forever(hours: float, is_idle: Callable[[], bool]) -> None:
    while True:
        try:
            if version := await _upgrade():
                log.info("yt-dlp updated to %s; restarting once no stream is playing", version)
                while not is_idle():
                    await asyncio.sleep(10)
                os.kill(os.getpid(), signal.SIGTERM)
                return
        except Exception:
            log.exception("yt-dlp update failed")
        await asyncio.sleep(hours * 3600)


async def _upgrade() -> str | None:
    """Upgrades yt-dlp (and its YouTube JS solver) in this venv. The new version, or None if unchanged."""
    before = importlib.metadata.version("yt-dlp")
    process = await asyncio.create_subprocess_exec(
        "uv", "pip", "install", "--python", sys.executable, "--upgrade", "yt-dlp[default]",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode:
        log.warning("yt-dlp update failed: %s", output.decode("utf-8", "replace")[-500:])
        return None
    after = importlib.metadata.version("yt-dlp")
    return after if after != before else None
