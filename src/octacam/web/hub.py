"""The GUI's connected browsers and the one way to push messages to them."""

import asyncio
import contextlib
import itertools
import json
from collections import deque
from collections.abc import Hashable
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

if TYPE_CHECKING:
    from octacam.web.preview import ViewSpec

# Each client's event-queue bound, and how many recent controller events the
# WebSocket handshake replays (ws.py): equal, so a replay is never truncated.
EVENT_BACKLOG_REPLAY = 50


class Client:
    """One WebSocket's send state. Frames and texts are newest-only (a slow client
    gets fewer updates, never a backlog); events queue.
    """

    _next_id = itertools.count(1)

    def __init__(self, ws: WebSocket):
        self.ws = ws
        # Lets a plugin scope per-connection state (the flywheel jog) to this socket.
        self.id = next(Client._next_id)
        self.frames: dict[int, bytes] = {}
        self.texts: dict[tuple[str, Hashable], str] = {}
        self.events: deque[str] = deque(maxlen=EVENT_BACKLOG_REPLAY)
        self.wakeup = asyncio.Event()
        # The preview's view spec per camera; event-loop thread only, so no lock.
        self.views: dict[int, ViewSpec] = {}

    def queue(self, type: str, text: str, key: Hashable = None) -> None:
        """Keep the newest message per `(type, key)`; every event queues (a log)."""
        if type == "event":
            self.events.append(text)
        else:
            self.texts[type, key] = text
        self.wakeup.set()

    def queue_frame(self, camera_index: int, message: bytes) -> None:
        self.frames[camera_index] = message
        self.wakeup.set()

    def is_ready_for(self, camera_index: int) -> bool:
        """True when no frame for this camera is pending; the preview loop encodes
        only for ready clients, so a stalled client costs no CPU.
        """
        return camera_index not in self.frames

    async def sender(self) -> None:
        # A send after close raises a bare RuntimeError (ASGI). End quietly on it
        # and on a disconnect, or the endpoint's `await sender` re-raises it.
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            while True:
                await self.wakeup.wait()
                self.wakeup.clear()
                if self.ws.client_state != WebSocketState.CONNECTED:
                    return
                frames, self.frames = self.frames, {}
                texts, self.texts = self.texts, {}
                events = list(self.events)
                self.events.clear()
                for text in [*texts.values(), *events]:
                    await self.ws.send_text(text)
                for data in frames.values():
                    await self.ws.send_bytes(data)


def to_json(type: str, payload: dict) -> str:
    return json.dumps({"type": type, **payload})


class Hub:
    """The connected clients. `loop` is the server's event loop, set by the
    app's lifespan; until then nothing is published.
    """

    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, type: str, payload: dict, key: Hashable = None) -> None:
        """Queue a message for every client, from any thread (`Client.queue`)."""
        loop = self.loop
        if loop is None or loop.is_closed() or not self.clients:
            return
        loop.call_soon_threadsafe(self._queue, type, to_json(type, payload), key)

    def broadcast(self, type: str, payload: dict) -> None:
        """`publish` from the event-loop thread, queued at once."""
        self._queue(type, to_json(type, payload), None)

    def _queue(self, type: str, text: str, key: Hashable) -> None:
        for client in list(self.clients):
            client.queue(type, text, key)

    def connect(self, client: Client) -> None:
        """Add a client and tell every one how many are connected (any of them can
        drive the rig). Event-loop thread only, like `disconnect`.
        """
        self.clients.add(client)
        self.broadcast("presence", {"clients": len(self.clients)})

    def disconnect(self, client: Client) -> None:
        self.clients.discard(client)
        self.broadcast("presence", {"clients": len(self.clients)})
