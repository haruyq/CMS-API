import os

from yt_dlp.cookies import YoutubeDLCookieJar

from app.cookies import CookieStore


def _place(path, value, mtime):
    path.write_text("# Netscape HTTP Cookie File\n"
                    f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\t{value}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _sid(store):
    jar = YoutubeDLCookieJar()
    store.copy_into(jar)
    return {c.name: c.value for c in jar}.get("SID")


def test_placed_file_wins_over_rotations(tmp_path):
    path = tmp_path / "cookies.txt"
    store = CookieStore(path)
    assert store.load() is False and len(store) == 0

    _place(path, "first", 1_000_000)
    assert _sid(store) == "first"

    rotated = YoutubeDLCookieJar()
    store.copy_into(rotated)
    next(iter(rotated)).value = "rotated"
    store.merge_from(rotated)
    assert store.load() is False  # its own write is not read back as a new file
    assert _sid(CookieStore(path)) == "rotated"

    # The admin places new cookies while a yt-dlp run still holds the old ones: the run's values are dropped.
    _place(path, "second", 2_000_000)
    store.merge_from(rotated)
    assert _sid(store) == "second"

    path.write_text("[{\"name\": \"SID\"}]", encoding="utf-8")  # wrong format keeps the previous cookies
    assert _sid(store) == "second"

    path.unlink()
    assert _sid(store) is None
