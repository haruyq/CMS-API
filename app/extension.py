import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from . import config
from .tver import Hls

log = logging.getLogger(__name__)
_ID = re.compile(r"[a-f0-9]{32}")
PATTERN_REFRESH_SECONDS = 60
# How long a client's link to the extension works; a display that plays one page longer opens it again.
LINK_SECONDS = 24 * 3600

_raw: dict[str, list[str]] = {"urls": [], "live": []}
_compiled: dict[str, list[re.Pattern]] = {"urls": [], "live": []}


class ExtensionError(Exception):
    """An extension failure safe to show to a player."""


def use_patterns(data) -> None:
    """Route by `data`, the extension's GET /patterns answer."""
    global _raw, _compiled
    raw = {key: [p for p in data.get(key, []) if isinstance(p, str)] for key in ("urls", "live")}
    _compiled = {key: [re.compile(p) for p in patterns] for key, patterns in raw.items()}
    _raw = raw


def patterns() -> dict[str, list[str]]:
    return _raw


def handles(url: str) -> bool:
    return any(p.fullmatch(url.strip()) for p in _compiled["urls"])


def is_live_url(url: str) -> bool:
    return handles(url) and any(p.fullmatch(url.strip()) for p in _compiled["live"])


def _base() -> str:
    base = config.EXTENSION_URL.rstrip("/")
    if not base or not config.EXTENSION_TOKEN:
        raise ExtensionError("拡張APIが設定されていません。APIのREADMEを確認してください")
    try:
        parts = urlsplit(base)
        valid = (parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
                 and not (parts.username or parts.password or parts.query or parts.fragment))
    except ValueError:
        valid = False
    if not valid:
        raise ExtensionError("EXTENSION_URLの設定が正しくありません")
    return base


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.EXTENSION_TOKEN}"}


def _get_patterns() -> None:
    with urllib.request.urlopen(urllib.request.Request(_base() + "/patterns", headers=_headers()), timeout=10) as r:
        use_patterns(json.load(r))


async def keep_patterns() -> None:
    """Read the extension's patterns now and every PATTERN_REFRESH_SECONDS, keeping the last ones while it is down."""
    while True:
        try:
            await asyncio.to_thread(_get_patterns)
        except (OSError, ValueError, AttributeError, re.error, ExtensionError) as error:
            log.warning("Could not read the extension's URL patterns: %s", error)
        await asyncio.sleep(PATTERN_REFRESH_SECONDS)


def client_url(endpoint: str, params: dict[str, str]) -> str:
    """Where a client gets an extension page's /video or /stream: from the extension itself, so its video and audio
    do not pass through this API. `exp` and `sig` (an HMAC under the shared token) let it check the link came from
    here."""
    public = config.EXTENSION_PUBLIC_URL.rstrip("/")
    if not public or not config.EXTENSION_TOKEN:
        raise ExtensionError("拡張APIの公開URLが設定されていません。APIのREADMEを確認してください")
    expires = int(time.time()) + LINK_SECONDS
    signature = hmac.new(config.EXTENSION_TOKEN.encode(), f"{endpoint}\n{params['url']}\n{expires}".encode(),
                         hashlib.sha256).hexdigest()
    return f"{public}/{endpoint}?" + urlencode({**params, "exp": expires, "sig": signature})


def resolve(url: str) -> Hls:
    """Ask the extension for a playable HLS source.

    Only an opaque capture ID comes back. Never accept a URL or headers supplied by the extension's response; the
    network destination and token are administrator configuration, and its media path is constructed here.
    """
    base = _base()
    request = urllib.request.Request(base + "/resolve", json.dumps({"url": url.strip()}).encode(),
                                     _headers() | {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=70) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        # Read only the extension's intentional player-facing message, not arbitrary HTML, headers, or
        # authentication material from a reverse proxy.
        try:
            detail = json.loads(error.read(8192)).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ExtensionError(detail if isinstance(detail, str) else "拡張APIが再生を開始できませんでした") from error
    except (OSError, ValueError) as error:
        raise ExtensionError("拡張APIに接続できません。起動状態を確認してください") from error
    try:
        capture = data["id"]
        live = data["live"]
        duration = data.get("duration")
        if (not isinstance(capture, str) or not _ID.fullmatch(capture) or not isinstance(live, bool)
                or duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                                             or not 0 < duration < 24 * 3600)):
            raise ValueError
        title = data.get("title")
        return Hls(base + f"/media/{capture}/index.m3u8", _headers(), title if isinstance(title, str) else "", live,
                   duration=None if live else duration, proxy=True)
    except (KeyError, TypeError, ValueError) as error:
        raise ExtensionError("拡張APIの応答が正しくありません") from error
