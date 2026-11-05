"""YouTube login cookies: import, persist, hand to yt-dlp, and keep fresh.

YouTube rotates session cookies (e.g. __Secure-*PSIDTS) as they are used, and an unused or stale set signs the
account out. So cookies live in memory under one lock: each yt-dlp run gets a
copy and its updates are merged back, a periodic refresh visits youtube.com to pick up rotations, and every
change is written atomically to COOKIE_FILE so a restart keeps the session.
"""

import asyncio
import logging
import os
import tempfile
import threading
import time
import urllib.request
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

from yt_dlp.cookies import YoutubeDLCookieJar

log = logging.getLogger(__name__)

# Domains whose cookies make up a YouTube login. Others (e.g. Discord CDN) are not kept.
_LOGIN_DOMAINS = ("youtube.com", "google.com")
_YEAR_AND_A_MONTH = 400 * 24 * 3600
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


def parse(text: str) -> YoutubeDLCookieJar:
    """Reads a Netscape cookies.txt export, or a `Cookie:` header copied from the browser's dev tools."""
    jar = YoutubeDLCookieJar()
    text = text.strip()
    if "\t" in text:
        # YoutubeDLCookieJar (unlike the stdlib one) keeps the "#HttpOnly_" lines browsers export for SID & co.
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as file:
            file.write(text + "\n")
        try:
            jar.load(file.name, ignore_discard=True, ignore_expires=True)
        finally:
            os.unlink(file.name)
        return jar
    header = text.removeprefix("cookie:").removeprefix("Cookie:")
    expires = int(time.time()) + _YEAR_AND_A_MONTH
    for pair in header.split(";"):
        name, sep, value = pair.strip().partition("=")
        if sep and name:
            jar.set_cookie(Cookie(
                0, name, value, None, False, ".youtube.com", True, True, "/", True,
                name.startswith("__Secure-"), expires, False, None, None, {},
            ))
    return jar


def _is_login_cookie(cookie: Cookie) -> bool:
    domain = cookie.domain.lstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in _LOGIN_DOMAINS)


class CookieStore:
    def __init__(self, path: Path):
        self.path = path
        self._jar = YoutubeDLCookieJar()
        self._lock = threading.Lock()
        self.logged_in: bool | None = None
        self.last_refresh: float | None = None

    def __len__(self) -> int:
        return len(self._jar)

    def load(self) -> None:
        if self.path.exists():
            self._jar.load(str(self.path), ignore_discard=True, ignore_expires=True)
            log.info("Loaded %d cookies from %s", len(self._jar), self.path)

    def copy_into(self, jar: CookieJar) -> None:
        with self._lock:
            for cookie in self._jar:
                jar.set_cookie(cookie)

    def merge_from(self, jar: CookieJar) -> None:
        """Keeps the rotated values a yt-dlp run received."""
        with self._lock:
            for cookie in jar:
                if _is_login_cookie(cookie):
                    self._jar.set_cookie(cookie)
            self._save()

    def replace(self, jar: YoutubeDLCookieJar) -> None:
        with self._lock:
            self._jar = jar
            self.logged_in = None
            self._save()

    async def refresh(self) -> bool | None:
        """Visits youtube.com with the cookies, keeping any it rotates. Whether they are logged in; None if offline."""
        try:
            return await asyncio.to_thread(self._refresh)
        except OSError as error:
            log.warning("Could not reach YouTube to refresh cookies: %s", error)
            return None

    def _refresh(self) -> bool:
        with self._lock:
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
            request = urllib.request.Request("https://www.youtube.com/", headers={"User-Agent": _USER_AGENT})
            with opener.open(request, timeout=20) as response:
                page = response.read(4 * 1024 * 1024).decode("utf-8", "replace")
            self.logged_in = '"LOGGED_IN":true' in page
            self.last_refresh = time.time()
            self._save()
            return self.logged_in

    def _save(self) -> None:
        # Write-then-rename, so a crash mid-save never leaves a truncated cookie file.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        self._jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
        os.replace(tmp, self.path)


async def refresh_forever(store: CookieStore, minutes: int) -> None:
    while True:
        if len(store):
            try:
                if await store.refresh() is False:
                    log.warning("YouTube cookies are no longer logged in; import new ones at /auth/youtube")
            except Exception:
                log.exception("YouTube cookie refresh failed")
        await asyncio.sleep(minutes * 60)
