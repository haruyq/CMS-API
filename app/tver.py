"""TVer episodes and live broadcasts as HLS: for /video, and for live audio (yt-dlp only handles episodes).

Same route as the WaterMedia TVer plugin: TVer's metadata names a STREAKS project and media, STREAKS lists the
HLS sources, and a live source with server-side ads needs a session query added to its URL.
"""

import datetime
import json
import re
import urllib.request
from dataclasses import dataclass

_URL = re.compile(
    r"https?://(?:www\.)?tver\.jp/(?:episodes?/(?P<episode>ep[a-zA-Z0-9]+)"
    r"|live/(?:(?P<channel>ntv|ex|tbs|tx|cx)|(?P<kind>simul|special)/(?P<id>le[a-zA-Z0-9]+)))/*(?:[?#].*)?"
)
_CHANNELS = {"ntv": "日テレ", "ex": "テレビ朝日", "tbs": "TBS", "tx": "テレ東", "cx": "フジテレビ"}
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
_HLS_TYPES = {"application/x-mpegurl", "application/vnd.apple.mpegurl"}
_TIMEOUT = 15


class TverError(Exception):
    """TVer says this can't be played (expired, not started, login needed, DRM, ...)."""


@dataclass(frozen=True)
class Hls:
    url: str
    headers: dict[str, str]
    title: str
    live: bool
    duration: float | None = None
    # An authenticated/private origin (an extension, see app/extension.py): proxy its media so
    # clients never receive the origin address or its Authorization header.
    proxy: bool = False


def is_tver_url(url: str) -> bool:
    return _URL.fullmatch(url.strip()) is not None


def is_live_url(url: str) -> bool:
    match = _URL.fullmatch(url.strip())
    return match is not None and not match["episode"]


def resolve(url: str) -> Hls:
    """The HLS master playlist (video and audio) of a TVer episode or live broadcast URL."""
    match = _URL.fullmatch(url.strip())
    headers = {"User-Agent": _USER_AGENT, "Origin": "https://tver.jp", "Referer": url.strip()}
    live = not match["episode"]
    api_key = None
    if channel := match["channel"]:
        project, media, title = f"tver-simul-{channel}", f"ref:simul-{channel}", _CHANNELS[channel]
    else:
        path, field = (("v2/episodes/", "episode") if match["episode"]
                       else ("v1/realtime_episodes/", "realtime_episode") if match["kind"] == "simul"
                       else ("v2/live_episodes/", "live_episode"))
        content = _json(f"https://contents-api.tver.jp/contents/api/{path}{match['episode'] or match['id']}",
                        headers | {"x-tver-platform-type": "web"})[field]
        if content.get("is_available") is False:
            raise TverError("This TVer program is unavailable or has expired")
        start_at = content.get("start_at")
        if live and start_at and datetime.datetime.fromisoformat(start_at) > datetime.datetime.now(datetime.UTC):
            raise TverError("This TVer live program has not started yet")
        title = content["title"]
        if match["episode"]:
            streaks = content["player_metadata"]["streaks"]
            project, media = streaks["project_id"], streaks["ovp_player_callback_id"]
        else:
            streaks = content["live_video"] if match["kind"] == "simul" else content["player_metadata"]
            project, media, api_key = streaks["project_id"], streaks["media_id"], streaks.get("api_key")
    if not re.fullmatch(r"[\w-]+", project) or not re.fullmatch(r"[\w:-]+", media):
        raise TverError("Invalid STREAKS metadata")
    if not api_key:
        # TVer rotates between six keys by the month in Japan (UTC+9 all year).
        month = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).month
        keys = _json("https://player.tver.jp/player/streaks_info_v2.json", headers)[project]["api_key"]
        api_key = keys[f"key0{month % 6 or 6}"]

    playback = _json(f"https://playback.api.streaks.jp/v1/projects/{project}/medias/{media}",
                     headers | {"X-Streaks-Api-Key": api_key, "Accept": "application/json"})
    live = playback.get("type") in ("live", "linear")
    drm = False
    for source in playback.get("sources", []):
        if source.get("key_systems") not in (None, {}):
            drm = True
            continue
        ssai = isinstance(source.get("ssai"), dict)
        if source.get("type", "").lower() not in _HLS_TYPES or ssai and not live:
            continue
        src = source["src"]
        if ssai:
            session = _json(f"https://ssai.api.streaks.jp/v1/projects/{project}/medias/{playback['id']}/ssai/session",
                            headers | {"Content-Type": "application/json"}, {"id": source["id"]})
            if not session:
                raise TverError("Live playback session missing")
            src += ("&" if "?" in src else "?") + session[0]["query"].lstrip("?&")
        return Hls(src, headers, title, live)
    raise TverError("This program uses DRM, which can't be played" if drm else "No playable HLS source")


def _json(url: str, headers: dict[str, str], body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=_TIMEOUT) as response:
        return json.load(response)
