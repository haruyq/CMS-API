"""Links' titles, kept in TITLES_FILE across restarts so a disc shows them without anyone resolving its links again."""

import json
import logging
import os

from . import config

log = logging.getLogger(__name__)
# Longest title kept; players send them (POST /title), so a long one is cut.
MAX_LENGTH = 300
_titles: dict[str, str] | None = None


def _all() -> dict[str, str]:
    global _titles
    if _titles is None:
        try:
            _titles = json.loads(config.TITLES_FILE.read_text("utf-8"))
        except FileNotFoundError:
            _titles = {}
        except ValueError:
            log.warning("%s is not JSON; starting with no titles", config.TITLES_FILE)
            _titles = {}
    return _titles


def get(url: str) -> str | None:
    return _all().get(url.strip())


def put(url: str, title: str | None) -> None:
    """Keeps `url`'s `title` (nothing for none), saving them all if it is new."""
    title = title and title.strip()[:MAX_LENGTH]
    titles = _all()
    if not title or titles.get(url.strip()) == title:
        return
    titles[url.strip()] = title
    # ponytail: rewrites the whole file per new title; a database if titles reach the hundreds of thousands.
    path = config.TITLES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(titles, ensure_ascii=False), "utf-8")
    os.replace(temporary, path)
