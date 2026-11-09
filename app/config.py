"""Settings, read once from the environment (see .env.example)."""

import os
from pathlib import Path


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in os.getenv(name, default).split(",") if item.strip())


# Same default as the mod's allowedHosts; subdomains are allowed too.
ALLOWED_HOSTS = _list("ALLOWED_HOSTS", "youtube.com,youtu.be,discordapp.com,discordapp.net,tver.jp,cctv.com,cntv.cn,cntv.com,bilibili.com,b23.tv,iwara.tv")
# Password for the /auth/youtube pages (HTTP Basic, any username). Empty disables them.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
COOKIE_FILE = Path(os.getenv("COOKIE_FILE", "/data/cookies.txt"))
# Where clients' playback diagnostics (POST /diagnostics) are kept, and the size (MB) past which new ones are refused.
DIAGNOSTICS_FILE = Path(os.getenv("DIAGNOSTICS_FILE", "/data/diagnostics.jsonl"))
DIAGNOSTICS_MAX_MB = int(os.getenv("DIAGNOSTICS_MAX_MB", "200"))
COOKIE_REFRESH_MINUTES = int(os.getenv("COOKIE_REFRESH_MINUTES", "60"))
# Concurrent ffmpeg processes; each in-game listener holds one while streaming.
MAX_STREAMS = int(os.getenv("MAX_STREAMS", "32"))
# How long a resolved media URL is reused. YouTube's expire after about 6 hours.
RESOLVE_CACHE_SECONDS = int(os.getenv("RESOLVE_CACHE_SECONDS", "1800"))
# Hours between yt-dlp updates (at start, then on this interval). 0 turns updating off; compose.yaml sets 24.
YTDLP_UPDATE_HOURS = float(os.getenv("YTDLP_UPDATE_HOURS", "0"))
# Optional extension that plays pages this API does not (see app/extension.py). Keep the token private; only the
# API contacts it.
EXTENSION_URL = os.getenv("EXTENSION_URL", "")
EXTENSION_TOKEN = os.getenv("EXTENSION_TOKEN", "")
# Where Minecraft clients reach the extension; its /video and /stream go there instead of through this API.
EXTENSION_PUBLIC_URL = os.getenv("EXTENSION_PUBLIC_URL", "")
# Direct P2P signaling (/p2p). Shared only between this API and the Minecraft server, never players. Empty disables it.
P2P_SERVER_TOKEN = os.getenv("P2P_SERVER_TOKEN", "")
# STUN servers players use to find their public address. Empty is enough on a LAN. Never TURN: media stays direct.
P2P_STUN_URLS = _list("P2P_STUN_URLS", "")
if any(not url.startswith("stun:") for url in P2P_STUN_URLS):
    raise ValueError("P2P_STUN_URLS takes only stun: URLs")
