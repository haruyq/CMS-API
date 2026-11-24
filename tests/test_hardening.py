import asyncio
import copy
import sys

from yt_dlp.cookies import YoutubeDLCookieJar

from app import audio
from app.cookies import CookieStore


def test_ffprobe_gets_the_same_protocol_whitelist_as_ffmpeg(monkeypatch):
    seen = []
    monkeypatch.setattr(audio.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or (_ for _ in ()).throw(OSError()))
    audio._probe_duration("https://cdn.discordapp.com/a.m3u8")
    assert seen[0][seen[0].index("-protocol_whitelist") + 1] == audio.PROTOCOLS
    assert "file" not in audio.PROTOCOLS.split(",")


def _store(tmp_path, value):
    path = tmp_path / "c.txt"
    path.write_text("# Netscape HTTP Cookie File\n" + "".join(
        f".youtube.com\tTRUE\t/\tTRUE\t1999999999\t{name}\t{v}\n" for name, v in [("SID", "1"), ("__Secure-1PSIDTS", value)]),
        encoding="utf-8")
    store = CookieStore(path)
    store.load()
    return store


def _rotate(jar, value):
    """What yt-dlp does on a rotation: a new Cookie object replaces the old one in the run's jar."""
    old = next(c for c in jar if c.name == "__Secure-1PSIDTS")
    new = copy.copy(old)
    new.value = value
    jar.set_cookie(new)


def _value(store, name):
    return next(c.value for c in store._jar if c.name == name)


def test_a_run_that_did_not_rotate_a_cookie_does_not_undo_another_runs_rotation(tmp_path):
    store = _store(tmp_path, "old")
    a, b = YoutubeDLCookieJar(), YoutubeDLCookieJar()
    store.copy_into(a)
    store.copy_into(b)
    _rotate(a, "new")  # run A receives a rotated value
    store.merge_from(a)
    store.merge_from(b)  # run B never touched it
    assert _value(store, "__Secure-1PSIDTS") == "new"


def test_a_cookie_a_run_rotated_is_still_kept(tmp_path):
    store = _store(tmp_path, "old")
    jar = YoutubeDLCookieJar()
    store.copy_into(jar)
    _rotate(jar, "new")
    store.merge_from(jar)
    assert _value(store, "__Secure-1PSIDTS") == "new"


def test_closing_a_stream_that_was_never_read_frees_its_slot(monkeypatch):
    script = "import sys,time;sys.stdout.buffer.write(b'x'*16);sys.stdout.flush();time.sleep(30)"
    monkeypatch.setattr(audio, "_ffmpeg_args", lambda *a, **k: [sys.executable, "-c", script])

    async def run():
        streamer = audio.Streamer(None)
        source = audio.Source("https://media.example/a", {}, None, None)
        stream = await streamer.open("https://youtube.com/x", 0, source=source)
        assert streamer.active == 1
        await stream.aclose()  # as Starlette's background task does when the client left before the first read
        return streamer.active

    assert asyncio.run(run()) == 0


def test_a_stream_read_to_the_end_yields_everything_once_and_frees_its_slot(monkeypatch):
    script = "import sys;sys.stdout.buffer.write(b'a'*16);sys.stdout.flush();sys.stdout.buffer.write(b'b'*8)"
    monkeypatch.setattr(audio, "_ffmpeg_args", lambda *a, **k: [sys.executable, "-c", script])

    async def run():
        streamer = audio.Streamer(None)
        source = audio.Source("https://media.example/a", {}, None, None)
        stream = await streamer.open("https://youtube.com/x", 0, source=source)
        data = b"".join([chunk async for chunk in stream])
        await stream.aclose()
        return data, streamer.active

    data, active = asyncio.run(run())
    assert data == b"a" * 16 + b"b" * 8 and active == 0
