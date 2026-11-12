import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, titles
from app.audio import Source, SourceError
from app.routers import media

URL = "https://www.youtube.com/watch?v=title"


class Resolver:
    def __init__(self):
        self.asked = []

    async def resolve(self, url):
        self.asked.append(url)
        if url.endswith("broken"):
            raise SourceError("gone")
        return Source("https://media.example/a", {}, 60.0, "From the API", live=False)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TITLES_FILE", tmp_path / "titles.json")
    monkeypatch.setattr(titles, "_titles", None)
    app = FastAPI()
    app.include_router(media.router)
    app.state.resolver = Resolver()
    with TestClient(app) as client:
        yield client


def test_a_title_a_player_found_is_kept_and_not_looked_up(client):
    assert client.post("/title", params={"url": URL}, json={"title": "From a player"}).status_code == 204
    assert client.get("/title", params={"url": URL}).json() == {"title": "From a player"}
    assert client.app.state.resolver.asked == []


def test_an_unknown_title_is_looked_up_once_and_kept_across_restarts(client, monkeypatch):
    assert client.get("/title", params={"url": URL}).json() == {"title": "From the API"}
    monkeypatch.setattr(titles, "_titles", None)
    assert client.get("/title", params={"url": URL}).json() == {"title": "From the API"}
    assert client.app.state.resolver.asked == [URL]
    # A player can't change a title the API keeps.
    client.post("/title", params={"url": URL}, json={"title": "Renamed"})
    assert client.get("/title", params={"url": URL}).json() == {"title": "From the API"}


def test_a_link_that_cant_be_played_has_no_title(client):
    assert client.get("/title", params={"url": URL + "broken"}).json() == {"title": None}
    assert client.get("/title", params={"url": "https://evil.example/a"}).status_code == 403
    assert client.post("/title", params={"url": "https://evil.example/a"}, json={"title": "x"}).status_code == 403
