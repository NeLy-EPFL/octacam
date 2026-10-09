"""The GUI's one WebSocket: the connect handshake, incoming view and plugin
messages, and the telemetry it streams. One socket carries everything (preview
JPEGs, JSON, plugin messages), so the GUI works through a plain `ssh -L`
forward and stays under the browser's per-host connection limit.
"""

import asyncio
import contextlib
import dataclasses
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from octacam.controller import RecordingController
from octacam.web.hub import EVENT_BACKLOG_REPLAY, Client, Hub, to_json
from octacam.web.preview import parse_views
from octacam.web.state import AppState

TELEMETRY_INTERVAL_S = 0.5


async def telemetry_loop(hub: Hub, controller: RecordingController) -> None:
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(TELEMETRY_INTERVAL_S)
        if not hub.clients:
            continue
        snapshot = await loop.run_in_executor(None, controller.snapshot)
        hub.broadcast("telemetry", snapshot)


def router(state: AppState) -> APIRouter:
    api = APIRouter()
    controller, hub = state.controller, state.hub
    plugins = controller.plugins

    @api.websocket("/api/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        client = Client(ws)
        hub.connect(client)
        sender = asyncio.create_task(client.sender())
        loop = asyncio.get_running_loop()
        try:
            # The descriptor is built and queued with no await between, so an init
            # that finishes later reaches this client after it, never before.
            client.queue("system", to_json("system", state.system_descriptor()))
            snapshot = await loop.run_in_executor(None, controller.snapshot)
            client.queue("state", to_json("state", snapshot))
            settings = dataclasses.asdict(controller.get_settings())
            client.queue("settings", to_json("settings", settings))
            # Replay the last benchmark report and recent events.
            last_diag = controller.get_last_diagnostic()
            if last_diag:
                client.queue("diagnostics", to_json("diagnostics", last_diag))
            for event in list(controller.events)[-EVENT_BACKLOG_REPLAY:]:
                client.queue("event", to_json("event", event))
            while True:
                text = await ws.receive_text()
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                # "view" is the core's: cheap dict work, handled inline, never
                # offered to plugins.
                if isinstance(message, dict) and message.get("type") == "view":
                    client.views.update(parse_views(message))
                    continue
                # In the executor: a plugin's hook may block on I/O. A raising
                # hook is logged, so a bad message cannot kill the socket.
                await loop.run_in_executor(
                    None, plugins.on_ws_message, message, client.id
                )
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(client)
            # E.g. the flywheel stops a jog this client owned, so a dropped socket
            # cannot leave the motor spinning. In the executor: it may block.
            await loop.run_in_executor(None, plugins.on_ws_disconnect, client.id)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    return api
