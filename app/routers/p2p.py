"""Signaling for direct peer-to-peer connections between players (protocol 1, see README).

The Minecraft server opens a room with P2P_SERVER_TOKEN and gets one ticket per player; players then exchange WebRTC
offers, answers and ICE candidates over /signal. Nothing else passes through: no TURN, host-viewer edges only, direct
candidates only, and small rate-limited messages, so this API never carries media. Clients measure each connection
themselves; bit rates are only taken from CMS 1.2.3 and earlier, which still pace at budget_bps.
"""

import asyncio
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from .. import config

MAX_ROOMS = 64
MAX_VIEWERS = 16
# Seconds a ticket waits for its WebSocket, at first and again whenever it loses one: signaling drops (a proxy in
# between cuts it) while the direct connections it set up carry on. A host that stays away closes the room; a viewer
# that does is removed.
TICKET_SECONDS = 120
MAX_MESSAGE_BYTES = 64 * 1024
MAX_SDP_BYTES = 48 * 1024
MAX_CANDIDATE_CHARS = 2048
# Messages and bytes one socket may send per window, and forward to any one peer. The host's whole allowance is this
# times its connected viewers.
RATE_WINDOW_SECONDS = 10
RATE_MESSAGES = 64
RATE_BYTES = 128 * 1024
# Messages waiting for one slow reader before it is dropped, so it cannot hold up the others.
OUTBOX_MESSAGES = 256
MAX_BPS = 100_000_000_000

_FIELDS = {
    "offer": {"type", "to", "sdp"},
    "answer": {"type", "to", "sdp"},
    "ice": {"type", "to", "candidate", "sdp_mid", "sdp_mline_index"},
}
_DIRECT = {"host", "srflx", "prflx"}

Bps = Annotated[int, Field(strict=True, gt=0, le=MAX_BPS)]


class _HostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: uuid.UUID
    upload_bps: Bps | None = None
    download_bps: Bps | None = None


class _ViewerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    peer_id: uuid.UUID
    upload_bps: Bps | None = None
    download_bps: Bps | None = None


@dataclass(eq=False)
class _Peer:
    id: str
    role: str
    upload_bps: int | None
    download_bps: int | None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    socket: WebSocket | None = None
    # Whether its WebSocket ever opened: it stays in `peers` while it waits for the next, so the host keeps its viewers.
    joined: bool = False
    expiry: asyncio.TimerHandle | None = None
    replaced: asyncio.Event = field(default_factory=asyncio.Event)
    outbox: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(OUTBOX_MESSAGES))
    gone: asyncio.Event = field(default_factory=asyncio.Event)
    farewell: dict | None = None
    close_code: int = 1000


@dataclass(eq=False)
class _Room:
    id: str
    host: _Peer
    viewers: dict[str, _Peer] = field(default_factory=dict)


_rooms: dict[str, _Room] = {}


def budget_bps(host_upload_bps: int | None, viewer_download_bps: list[int | None]) -> int:
    """Payload each connected viewer may receive: 65% of the host's upload shared out, capped by the slowest viewer.

    0 without viewers, or when anyone did not report a rate (clients that measure their connections themselves).
    """
    if not viewer_download_bps or host_upload_bps is None or None in viewer_download_bps:
        return 0
    # 0.65 is 13/20; integer division floors exactly.
    return min(host_upload_bps * 13 // (20 * len(viewer_download_bps)), min(viewer_download_bps) * 13 // 20)


def _server(authorization: Annotated[str, Header()] = "") -> None:
    if not config.P2P_SERVER_TOKEN:
        raise HTTPException(503, "Set P2P_SERVER_TOKEN to enable P2P")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {config.P2P_SERVER_TOKEN}".encode()):
        raise HTTPException(401, "Wrong server token", headers={"WWW-Authenticate": "Bearer"})


def _ticket(room: _Room, peer: _Peer, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"protocol": 1, "session_id": room.id, "peer_id": peer.id, "host_id": room.host.id, "role": peer.role,
            "token": peer.token, "stun_urls": list(config.P2P_STUN_URLS)}


def _live(room: _Room) -> bool:
    return _rooms.get(room.id) is room


def _member(room: _Room, peer: _Peer) -> bool:
    return _live(room) and (peer is room.host or room.viewers.get(peer.id) is peer)


def _joined(room: _Room) -> list[_Peer]:
    return [viewer for viewer in room.viewers.values() if viewer.joined]


def _send(peer: _Peer, message: dict) -> None:
    try:
        peer.outbox.put_nowait(json.dumps(message))
    except asyncio.QueueFull:
        _kick(peer, _error("Too slow to receive signaling"), 1008)


def _kick(peer: _Peer, farewell: dict | None, code: int = 1000) -> None:
    if not peer.gone.is_set():
        peer.farewell, peer.close_code = farewell, code
        peer.gone.set()


def _error(message: str) -> dict:
    return {"type": "error", "message": message}


def _entry(peer: _Peer) -> dict:
    rates = {"upload_bps": peer.upload_bps, "download_bps": peer.download_bps}
    return {"peer_id": peer.id, "role": peer.role} | {key: value for key, value in rates.items() if value is not None}


def _broadcast(room: _Room) -> None:
    viewers = _joined(room)
    budget = budget_bps(room.host.upload_bps, [viewer.download_bps for viewer in viewers])
    host = [_entry(room.host)] if room.host.joined else []
    if room.host.socket is not None:
        _send(room.host, {"type": "peers", "host_id": room.host.id, "peers": host + [_entry(v) for v in viewers],
                          "budget_bps": budget})
    # Viewers only learn of the host: they never signal each other.
    for viewer in viewers:
        if viewer.socket is not None:
            _send(viewer, {"type": "peers", "host_id": room.host.id, "peers": host, "budget_bps": budget})


def _close_room(room: _Room, message: str) -> None:
    if _live(room):
        del _rooms[room.id]
    for peer in (room.host, *room.viewers.values()):
        _kick(peer, {"type": "closed", "message": message})


def _remove_viewer(room: _Room, peer_id: str, message: str) -> None:
    viewer = room.viewers.pop(peer_id, None)
    if viewer is not None:
        _kick(viewer, {"type": "closed", "message": message})
        if viewer.joined:
            _broadcast(room)


def _expire(room: _Room, peer: _Peer) -> None:
    if peer.socket is None and _member(room, peer):
        if peer is room.host:
            _close_room(room, "The host left" if peer.joined else "The host did not connect in time")
        else:
            _remove_viewer(room, peer.id, "You left the session")


def _wait(room: _Room, peer: _Peer) -> None:
    """Gives `peer` TICKET_SECONDS from now to open a WebSocket."""
    if peer.expiry is not None:
        peer.expiry.cancel()
    peer.expiry = asyncio.get_running_loop().call_later(TICKET_SECONDS, _expire, room, peer)


def _issue(room: _Room, peer: _Peer, response: Response) -> dict:
    _wait(room, peer)
    return _ticket(room, peer, response)


def _uuid(value) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid peer id")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError("Invalid peer id") from None


def _check_candidate(candidate) -> None:
    if not (isinstance(candidate, str) and len(candidate) <= MAX_CANDIDATE_CHARS and candidate.isprintable()
            and candidate.startswith("candidate:")):
        raise ValueError("Invalid ICE candidate")
    parts = candidate.split()
    if len(parts) < 8 or parts[6] != "typ":
        raise ValueError("Invalid ICE candidate")
    if parts[7] not in _DIRECT:
        raise ValueError("Only direct ICE candidates are allowed")


def _check_sdp(sdp) -> None:
    if not (isinstance(sdp, str) and sdp.isascii() and len(sdp) <= MAX_SDP_BYTES and sdp.startswith("v=0")):
        raise ValueError("Invalid SDP")
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            _check_candidate(line[2:])


def _route(room: _Room, peer: _Peer, text: str) -> tuple[_Peer, dict]:
    """The connected peer a client's message goes to, and what it receives; ValueError says why it cannot go."""
    try:
        message = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("Message is not JSON") from None
    kind = message.get("type") if isinstance(message, dict) else None
    if not isinstance(kind, str) or kind not in _FIELDS:
        raise ValueError("Unknown message type")
    if message.keys() != _FIELDS[kind]:
        raise ValueError(f"{kind} messages have exactly the fields {', '.join(sorted(_FIELDS[kind]))}")
    to = _uuid(message["to"])
    if peer is room.host:
        if kind == "answer":
            raise ValueError("The host does not answer")
        target = room.viewers.get(to)
    else:
        if kind == "offer":
            raise ValueError("Only the host offers")
        if to != room.host.id:
            raise ValueError("Viewers only signal the host")
        target = room.host
    if target is None or target.socket is None:
        raise ValueError("That peer is not connected")
    if kind == "ice":
        _check_candidate(message["candidate"])
        mid, index = message["sdp_mid"], message["sdp_mline_index"]
        if not (isinstance(mid, str) and len(mid) <= 64 and mid.isprintable()):
            raise ValueError("Invalid sdp_mid")
        if type(index) is not int or not 0 <= index < 1024:
            raise ValueError("Invalid sdp_mline_index")
    else:
        _check_sdp(message["sdp"])
    return target, {"type": kind, "from": peer.id} | {k: v for k, v in message.items() if k not in ("type", "to")}


async def _receive(websocket: WebSocket, room: _Room, peer: _Peer) -> None:
    window, count, size, edges = time.monotonic(), 0, 0, {}
    while True:
        message = await websocket.receive()
        # Removed, replaced or its room closed while waiting: nothing more of it goes anywhere.
        if (message["type"] == "websocket.disconnect" or peer.gone.is_set() or not _member(room, peer)
                or peer.socket is not websocket):
            return
        text = message.get("text")
        if text is None:
            return _kick(peer, _error("Binary messages are not accepted"), 1003)
        length = len(text.encode())
        if length > MAX_MESSAGE_BYTES:
            return _kick(peer, _error("Message is too large"), 1009)
        if time.monotonic() - window > RATE_WINDOW_SECONDS:
            window, count, size, edges = time.monotonic(), 0, 0, {}
        count, size = count + 1, size + length
        scale = max(1, len(_joined(room))) if peer is room.host else 1
        if count > RATE_MESSAGES * scale or size > RATE_BYTES * scale:
            return _kick(peer, _error("Too many signaling messages"), 1008)
        try:
            target, forward = _route(room, peer, text)
        except ValueError as error:
            _send(peer, _error(str(error)))
        else:
            sent, sent_bytes = edges.get(target.id, (0, 0))
            sent, sent_bytes = edges[target.id] = sent + 1, sent_bytes + length
            if sent > RATE_MESSAGES or sent_bytes > RATE_BYTES:
                return _kick(peer, _error("Too many signaling messages"), 1008)
            _send(target, forward)


async def _pump(websocket: WebSocket, outbox: asyncio.Queue) -> None:
    while True:
        await websocket.send_text(await outbox.get())


def _authenticate(room: _Room | None, authorization: str) -> _Peer | None:
    if room is None or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").encode()
    for peer in (room.host, *room.viewers.values()):
        if secrets.compare_digest(peer.token.encode(), token):
            return peer
    return None


router = APIRouter(prefix="/p2p")


@router.get("/config")
async def p2p_config() -> dict:
    return {"protocol": 1, "enabled": bool(config.P2P_SERVER_TOKEN), "stun_urls": list(config.P2P_STUN_URLS)}


@router.post("/sessions", dependencies=[Depends(_server)])
async def open_session(body: _HostRequest, response: Response) -> dict:
    if len(_rooms) >= MAX_ROOMS:
        raise HTTPException(503, "Too many P2P sessions")
    room = _Room(secrets.token_urlsafe(16), _Peer(str(body.host_id), "host", body.upload_bps, body.download_bps))
    _rooms[room.id] = room
    return _issue(room, room.host, response)


@router.post("/sessions/{session_id}/peers", dependencies=[Depends(_server)])
async def add_viewer(session_id: str, body: _ViewerRequest, response: Response) -> dict:
    room = _rooms.get(session_id)
    if room is None:
        raise HTTPException(404, "No such P2P session")
    peer_id = str(body.peer_id)
    if peer_id == room.host.id or peer_id in room.viewers:
        raise HTTPException(409, "That player is already in this session")
    if len(room.viewers) >= MAX_VIEWERS:
        raise HTTPException(409, "This session is full")
    viewer = room.viewers[peer_id] = _Peer(peer_id, "viewer", body.upload_bps, body.download_bps)
    return _issue(room, viewer, response)


# Deletes succeed when there is nothing left to delete, so cleanup can simply be repeated.
@router.delete("/sessions/{session_id}", status_code=204, dependencies=[Depends(_server)])
async def close_session(session_id: str) -> None:
    if room := _rooms.get(session_id):
        _close_room(room, "The session was closed")


@router.delete("/sessions/{session_id}/peers/{peer_id}", status_code=204, dependencies=[Depends(_server)])
async def remove_peer(session_id: str, peer_id: uuid.UUID) -> None:
    if room := _rooms.get(session_id):
        if str(peer_id) == room.host.id:
            _close_room(room, "The host left")
        else:
            _remove_viewer(room, str(peer_id), "You were removed from the session")


@router.websocket("/sessions/{session_id}/signal")
async def signal(websocket: WebSocket, session_id: str) -> None:
    room = _rooms.get(session_id)
    peer = _authenticate(room, websocket.headers.get("authorization", ""))
    # Refused before accepting, which the client sees as HTTP 403.
    if peer is None:
        await websocket.close(1008)
        return
    # A ticket's newest socket is its only one: a client opens another once it lost the last, often before this API
    # can tell that one is dead. What the lost one never took is dropped with it.
    peer.replaced.set()
    replaced = peer.replaced = asyncio.Event()
    peer.socket, peer.joined, peer.outbox = websocket, True, asyncio.Queue(OUTBOX_MESSAGES)
    if peer.expiry is not None:
        peer.expiry.cancel()
    tasks = []
    try:
        await websocket.accept()
        tasks = [asyncio.create_task(_pump(websocket, peer.outbox)),
                 asyncio.create_task(_receive(websocket, room, peer)), asyncio.create_task(peer.gone.wait()),
                 asyncio.create_task(replaced.wait())]
        _broadcast(room)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Membership changes before any await: a cancelled handler must still leave the room.
        latest = peer.socket is websocket
        if latest:
            peer.socket = None
            if not peer.gone.is_set():
                # Lost, not left: the server says when a player leaves (DELETE); until then its ticket waits again.
                _wait(room, peer)
            elif peer is room.host:
                _close_room(room, "The host left")
            elif _member(room, peer):
                _remove_viewer(room, peer.id, "You left the session")
        for task in tasks:
            task.cancel()
        # A lost socket has nobody left to tell.
        if not latest or peer.gone.is_set():
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                if latest and peer.farewell is not None:
                    await asyncio.wait_for(websocket.send_text(json.dumps(peer.farewell)), 2)
                await asyncio.wait_for(websocket.close(peer.close_code if latest else 1000), 2)
            except Exception:
                pass  # The client is already gone.
