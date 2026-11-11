import asyncio
import json
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import config
from app.routers import p2p

SERVER = {"Authorization": "Bearer server-secret"}
HOST, VIEWER, OTHER = (str(uuid.uuid4()) for _ in range(3))
SDP = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=candidate:1 1 udp 2122260223 192.168.1.2 5000 typ host\r\n"
CANDIDATE = "candidate:1 1 udp 2122260223 192.168.1.2 5000 typ host generation 0"
RELAY = "candidate:2 1 udp 41885439 203.0.113.5 3478 typ relay raddr 0.0.0.0 rport 0"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "P2P_SERVER_TOKEN", "server-secret")
    monkeypatch.setattr(config, "P2P_STUN_URLS", ("stun:stun.example:3478",))
    p2p._rooms.clear()
    app = FastAPI()
    app.include_router(p2p.router)
    with TestClient(app) as client:
        yield client
    p2p._rooms.clear()


def open_room(client, upload_bps=10_000_000):
    response = client.post("/p2p/sessions", headers=SERVER,
                           json={"host_id": HOST, "upload_bps": upload_bps, "download_bps": 50_000_000})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def join(client, room, peer_id=VIEWER, download_bps=4_000_000):
    return client.post(f"/p2p/sessions/{room['session_id']}/peers", headers=SERVER,
                       json={"peer_id": peer_id, "upload_bps": 1_000_000, "download_bps": download_bps})


def connect(client, ticket, token=None):
    return client.websocket_connect(f"/p2p/sessions/{ticket['session_id']}/signal",
                                    headers={"Authorization": f"Bearer {token or ticket['token']}"})


def test_budget():
    assert p2p.budget_bps(10_000_000, []) == 0
    assert p2p.budget_bps(10_000_000, [4_000_000]) == 2_600_000
    assert p2p.budget_bps(10_000_000, [50_000_000, 50_000_000]) == 3_250_000
    assert p2p.budget_bps(100_000_000_000, [3]) == 1
    assert p2p.budget_bps(None, [4_000_000]) == 0
    assert p2p.budget_bps(10_000_000, [4_000_000, None]) == 0


def test_rooms_without_rates(client):
    """Current clients measure their connections themselves and report no rates."""
    host_ticket = client.post("/p2p/sessions", headers=SERVER, json={"host_id": HOST}).json()
    viewer_ticket = client.post(f"/p2p/sessions/{host_ticket['session_id']}/peers", headers=SERVER,
                                json={"peer_id": VIEWER}).json()
    with connect(client, host_ticket) as host:
        assert host.receive_json()["peers"] == [{"peer_id": HOST, "role": "host"}]
        with connect(client, viewer_ticket) as viewer:
            joined = host.receive_json()
            assert joined["peers"] == [{"peer_id": HOST, "role": "host"}, {"peer_id": VIEWER, "role": "viewer"}]
            assert joined["budget_bps"] == 0 and viewer.receive_json()["budget_bps"] == 0


def test_config_and_server_auth(client, monkeypatch):
    assert client.get("/p2p/config").json() == {"protocol": 1, "enabled": True, "stun_urls": ["stun:stun.example:3478"]}
    body = {"host_id": HOST, "upload_bps": 1, "download_bps": 1}
    assert client.post("/p2p/sessions", json=body).status_code == 401
    assert client.post("/p2p/sessions", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    for bad in ({"upload_bps": 0}, {"upload_bps": 1.5}, {"upload_bps": True}, {"upload_bps": "1"},
                {"upload_bps": 100_000_000_001}, {"host_id": "player"}, {"extra": 1}):
        assert client.post("/p2p/sessions", json=body | bad, headers=SERVER).status_code == 422
    monkeypatch.setattr(config, "P2P_SERVER_TOKEN", "")
    assert client.get("/p2p/config").json()["enabled"] is False
    assert client.post("/p2p/sessions", json=body, headers=SERVER).status_code == 503


def test_tickets(client):
    host = open_room(client)
    assert host["role"] == "host" and host["peer_id"] == host["host_id"] == HOST
    assert host["stun_urls"] == ["stun:stun.example:3478"] and host["protocol"] == 1
    viewer = join(client, host).json()
    assert viewer["role"] == "viewer" and viewer["session_id"] == host["session_id"] and viewer["token"] != host["token"]
    assert join(client, host).status_code == 409
    assert join(client, host, HOST).status_code == 409
    assert join(client, {"session_id": "missing"}).status_code == 404


def test_signaling(client, monkeypatch):
    host_ticket = open_room(client)
    viewer_ticket = join(client, host_ticket).json()
    with connect(client, host_ticket) as host:
        assert host.receive_json()["peers"] == [
            {"peer_id": HOST, "role": "host", "upload_bps": 10_000_000, "download_bps": 50_000_000}]
        with connect(client, viewer_ticket) as viewer:
            joined = host.receive_json()
            assert [peer["peer_id"] for peer in joined["peers"]] == [HOST, VIEWER]
            assert joined["budget_bps"] == 2_600_000
            seen = viewer.receive_json()
            assert [peer["peer_id"] for peer in seen["peers"]] == [HOST] and seen["budget_bps"] == 2_600_000

            host.send_json({"type": "offer", "to": VIEWER, "sdp": SDP})
            assert viewer.receive_json() == {"type": "offer", "from": HOST, "sdp": SDP}
            viewer.send_json({"type": "answer", "to": HOST, "sdp": SDP})
            assert host.receive_json() == {"type": "answer", "from": VIEWER, "sdp": SDP}
            ice = {"type": "ice", "candidate": CANDIDATE, "sdp_mid": "0", "sdp_mline_index": 0}
            viewer.send_json(ice | {"to": HOST})
            assert host.receive_json() == ice | {"from": VIEWER}

            for bad, error in [
                (ice | {"to": HOST, "candidate": RELAY}, "Only direct ICE candidates are allowed"),
                ({"type": "offer", "to": HOST, "sdp": SDP}, "Only the host offers"),
                ({"type": "answer", "to": OTHER, "sdp": SDP}, "Viewers only signal the host"),
                ({"type": "answer", "to": HOST, "sdp": SDP + "a=" + RELAY + "\r\n"}, "Only direct ICE candidates are allowed"),
                ({"type": "answer", "to": HOST, "sdp": "x" * 100}, "Invalid SDP"),
                ({"type": "answer", "to": HOST, "sdp": SDP, "data": "x"}, "answer messages have exactly the fields sdp, to, type"),
                ({"type": "data", "to": HOST}, "Unknown message type"),
                ({"type": [], "to": HOST}, "Unknown message type"),
                ({"type": {}, "to": HOST}, "Unknown message type"),
            ]:
                viewer.send_json(bad)
                assert viewer.receive_json() == {"type": "error", "message": error}
            viewer.send_json(ice | {"to": HOST})
            assert host.receive_json() == ice | {"from": VIEWER}
            host.send_json({"type": "answer", "to": VIEWER, "sdp": SDP})
            assert host.receive_json()["message"] == "The host does not answer"
            host.send_json(ice | {"to": OTHER})
            assert host.receive_json()["message"] == "That peer is not connected"
            monkeypatch.setattr(p2p, "TICKET_SECONDS", 0.1)
        # The viewer's socket is gone: it leaves once its ticket has waited for another in vain.
        left = host.receive_json()
        assert [peer["peer_id"] for peer in left["peers"]] == [HOST] and left["budget_bps"] == 0


def test_socket_auth(client):
    host_ticket = open_room(client)
    viewer_ticket = join(client, host_ticket).json()
    with pytest.raises(WebSocketDisconnect):
        with connect(client, host_ticket, "wrong"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/p2p/sessions/{host_ticket['session_id']}/signal?token={host_ticket['token']}"):
            pass
    other_room = client.post("/p2p/sessions", headers=SERVER,
                             json={"host_id": OTHER, "upload_bps": 1, "download_bps": 1}).json()
    with pytest.raises(WebSocketDisconnect):
        with connect(client, other_room, viewer_ticket["token"]):
            pass


def test_newest_socket_replaces_the_last(client):
    """A client that lost its socket opens another, often before this API can tell the lost one is dead."""
    host_ticket = open_room(client)
    with connect(client, host_ticket) as stale:
        stale.receive_json()
        with connect(client, host_ticket) as host:
            assert host.receive_json()["type"] == "peers"
            with pytest.raises(WebSocketDisconnect):
                stale.receive_json()
            assert join(client, host_ticket).status_code == 200


def test_lost_signaling_keeps_the_room(client, monkeypatch):
    """A proxy cut the host's socket: it comes back with its ticket, to the room and the viewer it had."""
    host_ticket = open_room(client)
    viewer_ticket = join(client, host_ticket).json()

    def ids(message):
        return [peer["peer_id"] for peer in message["peers"]]

    with connect(client, viewer_ticket) as viewer:
        assert viewer.receive_json()["peers"] == []
        with connect(client, host_ticket) as host:
            assert ids(host.receive_json()) == [HOST, VIEWER] and ids(viewer.receive_json()) == [HOST]
            monkeypatch.setattr(p2p, "TICKET_SECONDS", 0.3)
        with connect(client, host_ticket) as host:
            assert ids(host.receive_json()) == [HOST, VIEWER] and ids(viewer.receive_json()) == [HOST]
            # Long past the wait its lost socket started: that ended when this one opened.
            time.sleep(0.6)
            host.send_json({"type": "offer", "to": VIEWER, "sdp": SDP})
            assert viewer.receive_json() == {"type": "offer", "from": HOST, "sdp": SDP}
            assert join(client, host_ticket, OTHER).status_code == 200


def test_host_staying_away_closes_room(client, monkeypatch):
    host_ticket = open_room(client)
    viewer_ticket = join(client, host_ticket).json()
    with connect(client, viewer_ticket) as viewer:
        assert viewer.receive_json()["peers"] == []
        with connect(client, host_ticket) as host:
            host.receive_json()
            assert viewer.receive_json()["peers"][0]["peer_id"] == HOST
            monkeypatch.setattr(p2p, "TICKET_SECONDS", 0.1)
        assert viewer.receive_json() == {"type": "closed", "message": "The host left"}
        with pytest.raises(WebSocketDisconnect):
            viewer.receive_json()
    assert join(client, host_ticket, OTHER).status_code == 404


def test_server_revokes(client):
    host_ticket = open_room(client)
    viewer_ticket = join(client, host_ticket).json()
    session = f"/p2p/sessions/{host_ticket['session_id']}"
    with connect(client, host_ticket) as host:
        host.receive_json()
        with connect(client, viewer_ticket) as viewer:
            host.receive_json(), viewer.receive_json()
            assert client.delete(f"{session}/peers/{VIEWER}", headers=SERVER).status_code == 204
            assert viewer.receive_json()["type"] == "closed"
            assert host.receive_json()["budget_bps"] == 0
        with pytest.raises(WebSocketDisconnect):
            with connect(client, viewer_ticket):
                pass
        assert client.delete(session, headers=SERVER).status_code == 204
        assert host.receive_json() == {"type": "closed", "message": "The session was closed"}
    assert client.delete(session, headers=SERVER).status_code == 204
    assert client.delete(session).status_code == 401


def test_unused_tickets_expire(client, monkeypatch):
    monkeypatch.setattr(p2p, "TICKET_SECONDS", 0.1)
    host_ticket = open_room(client)
    time.sleep(0.5)
    assert join(client, host_ticket).status_code == 404


def test_unused_viewer_ticket_expires(client, monkeypatch):
    monkeypatch.setattr(p2p, "TICKET_SECONDS", 0.1)
    host_ticket = open_room(client)
    with connect(client, host_ticket) as host:
        host.receive_json()
        assert join(client, host_ticket).status_code == 200
        time.sleep(0.5)
        assert join(client, host_ticket).status_code == 200


def test_host_cannot_concentrate_rate_on_one_viewer(client):
    host_ticket = open_room(client)
    with connect(client, host_ticket) as host, connect(client, join(client, host_ticket).json()) as viewer:
        host.receive_json(), host.receive_json(), viewer.receive_json()
        with connect(client, join(client, host_ticket, OTHER).json()) as other:
            host.receive_json(), viewer.receive_json(), other.receive_json()
            # 3 of these exceed one edge's RATE_BYTES, though not the host's allowance for 2 viewers.
            big = "v=0\r\nx=" + "A" * (p2p.MAX_SDP_BYTES - 7)
            for _ in range(3):
                host.send_json({"type": "offer", "to": VIEWER, "sdp": big})
            assert host.receive_json() == {"type": "error", "message": "Too many signaling messages"}
            assert [viewer.receive_json()["sdp"] == big for _ in range(2)] == [True, True]


@pytest.mark.parametrize("revoke", ["remove", "replace", "close"])
def test_revoked_peer_forwards_nothing(revoke):
    async def run():
        room = p2p._Room("room", p2p._Peer(HOST, "host", 1, 1))
        viewer = room.viewers[VIEWER] = p2p._Peer(VIEWER, "viewer", 1, 1)
        room.host.socket = viewer.socket = object()
        p2p._rooms["room"] = room
        answer = {"type": "websocket.receive", "text": json.dumps({"type": "answer", "to": HOST, "sdp": SDP})}

        class Socket:
            async def receive(self):
                # The server revokes the viewer while its message is on the way.
                if revoke == "close":
                    p2p._close_room(room, "closed")
                else:
                    p2p._remove_viewer(room, VIEWER, "removed")
                    if revoke == "replace":
                        room.viewers[VIEWER] = p2p._Peer(VIEWER, "viewer", 1, 1)
                return answer

        viewer.socket = Socket()
        await p2p._receive(viewer.socket, room, viewer)
        while not room.host.outbox.empty():
            assert json.loads(room.host.outbox.get_nowait())["type"] == "peers"

    try:
        asyncio.run(run())
    finally:
        p2p._rooms.clear()


def test_abuse_closes_socket(client):
    host_ticket = open_room(client)
    with connect(client, host_ticket) as host:
        host.receive_json()
        host.send_bytes(b"media")
        assert host.receive_json()["message"] == "Binary messages are not accepted"
        with pytest.raises(WebSocketDisconnect):
            host.receive_json()
    host_ticket = open_room(client)
    with connect(client, host_ticket) as host:
        host.receive_json()
        host.send_text("x" * (p2p.MAX_MESSAGE_BYTES + 1))
        assert host.receive_json()["message"] == "Message is too large"
    host_ticket = open_room(client)
    with connect(client, host_ticket) as host:
        host.receive_json()
        for _ in range(p2p.RATE_MESSAGES + 1):
            host.send_text("{}")
        messages = [host.receive_json()["message"] for _ in range(p2p.RATE_MESSAGES + 1)]
        assert messages[-1] == "Too many signaling messages"


def test_slow_reader_is_dropped():
    peer = p2p._Peer(VIEWER, "viewer", 1, 1)
    peer.outbox = asyncio.Queue(1)
    p2p._send(peer, {"type": "peers"})
    assert not peer.gone.is_set()
    p2p._send(peer, {"type": "peers"})
    assert peer.gone.is_set() and peer.close_code == 1008
