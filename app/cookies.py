"""YouTube login cookies: read from COOKIE_FILE, hand to yt-dlp, and keep fresh.

The admin places a Netscape cookies.txt export at COOKIE_FILE; a file replaced while the API runs is read again
before the next use. YouTube rotates session cookies (e.g. __Secure-*PSIDTS) as they are used, and an unused or
stale set signs the account out. So cookies live in memory under one lock: each yt-dlp run gets a copy and its
updates are merged back, a periodic refresh visits youtube.com to pick up rotations, and every change is written
atomically back to COOKIE_FILE so a restart keeps the session.
"""

import asyncio
import logging
import os
import threading
import time
import urllib.request
import weakref
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

from yt_dlp.cookies import YoutubeDLCookieJar

log = logging.getLogger(__name__)

# Domains whose cookies make up a YouTube login. Others (e.g. Discord CDN) are not kept.
_LOGIN_DOMAINS = ("youtube.com", "google.com")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


def _is_login_cookie(cookie: Cookie) -> bool:
    domain = cookie.domain.lstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in _LOGIN_DOMAINS)


def _mtime(path: Path) -> int | None:
    """None when there is no file. Other errors (e.g. an unreadable directory) raise OSError."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


class CookieStore:
    def __init__(self, path: Path):
        self.path = path
        self._jar = YoutubeDLCookieJar()
        self._lock = threading.Lock()
        # What each run's jar was given, so merge_from keeps only what that run changed.
        self._given: weakref.WeakKeyDictionary[CookieJar, dict] = weakref.WeakKeyDictionary()
        # The file's mtime when it was last read or written here; a different one means the admin replaced it.
        self._seen: int | None = None
        self.logged_in: bool | None = None
        self.last_refresh: float | None = None

    def __len__(self) -> int:
        return len(self._jar)

    def load(self) -> bool:
        """Reads COOKIE_FILE again if it changed since it was last read or written. Whether it was read."""
        with self._lock:
            return self._reload()

    def _reload(self) -> bool:
        try:
            mtime = _mtime(self.path)
            if mtime == self._seen:
                return False
            self._seen = mtime
            jar = YoutubeDLCookieJar()
            if mtime is None:
                log.info("No YouTube cookies; place a cookies.txt at %s to log in", self.path)
            else:
                # YoutubeDLCookieJar (unlike the stdlib one) keeps the "#HttpOnly_" lines browsers export for SID & co.
                jar.load(str(self.path), ignore_discard=True, ignore_expires=True)
        except OSError as error:
            # An unreadable or wrong-format file keeps the previous cookies; the next save writes those back over it.
            log.error("Could not read cookies from %s (needs a Netscape cookies.txt export): %s", self.path, error)
            return False
        self._jar = jar
        self.logged_in = None
        log.info("Loaded %d cookies from %s", len(jar), self.path)
        return True

    def copy_into(self, jar: CookieJar) -> None:
        with self._lock:
            self._reload()
            self._given[jar] = {(c.domain, c.path, c.name): c.value for c in self._jar}
            for cookie in self._jar:
                jar.set_cookie(cookie)

    def merge_from(self, jar: CookieJar) -> None:
        """Keeps the rotated values a yt-dlp run received, unless the admin placed new cookies meanwhile. Only those:
        a run that started before another one's rotation still holds the old value, and writing that back would undo
        it (and sign the account out)."""
        with self._lock:
            given = self._given.pop(jar, {})
            if self._reload():
                return
            for cookie in jar:
                if _is_login_cookie(cookie) and given.get((cookie.domain, cookie.path, cookie.name)) != cookie.value:
                    self._jar.set_cookie(cookie)
            self._save()

    async def refresh(self) -> bool | None:
        """Visits youtube.com with the cookies, keeping any it rotates. Whether they are logged in; None if offline."""
        try:
            return await asyncio.to_thread(self._refresh)
        except OSError as error:
            log.warning("Could not reach YouTube to refresh cookies: %s", error)
            return None

    def _refresh(self) -> bool | None:
        with self._lock:
            self._reload()
            if not len(self._jar):
                return None
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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            self._jar.save(str(tmp), ignore_discard=True, ignore_expires=True)
            os.replace(tmp, self.path)
        except OSError as error:
            # Playback goes on with the cookies in memory; only a restart loses the rotated values.
            log.warning("Could not save cookies to %s: %s", self.path, error)
            return
        self._seen = _mtime(self.path)


async def refresh_forever(store: CookieStore, minutes: int) -> None:
    while True:
        try:
            logged_in = await store.refresh()
            if logged_in is False:
                log.warning("YouTube cookies are not logged in; place a new cookies.txt at %s", store.path)
            elif logged_in:
                log.info("YouTube cookies are logged in")
        except Exception:
            log.exception("YouTube cookie refresh failed")
        await asyncio.sleep(minutes * 60)
