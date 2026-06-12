"""FastAPI backend for the octacam web GUI.

One process serves the SPA (static files), a small REST control plane, and a
single WebSocket that multiplexes everything dynamic: JPEG preview frames for
all cameras (binary), telemetry/state/event JSON (text), and stepper jog
commands (client to server). One socket keeps the whole UI usable through a
plain `ssh -L` port forward and avoids the browser's ~6-connections-per-host
limit that 8 MJPEG streams would hit.

Preview frames are throttled to the configured display refresh rate, sent
newest-only per client (a slow client or tunnel just sees fewer frames), and
nothing is encoded when no client is connected.
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import math
import struct
import time
from collections import deque
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import octacam
from octacam.config import OctacamConfig
from octacam.controller import RecordingController, StartResult
from octacam.serial_link import Command, SerialLink
from octacam.writer import FORMATS

log = logging.getLogger("octacam")

STATIC_DIR = Path(__file__).parent / "static"
TELEMETRY_INTERVAL_S = 0.5
PREVIEW_MAX_DIM = 640  # longest preview edge after downscaling
JPEG_QUALITY = 75
COMMAND_FIELDS = (
    "n_steps",
    "step_interval_us",
    "rest_duration_ms",
    "n_repeats",
    "init_wait_duration_s",
)
# u8 version | u8 kind | u8 camera | u8 flags(bit0=recording) |
# u32 frame number | u64 timestamp ns | f32 fps | u32 dropped total
FRAME_HEADER = struct.Struct("<BBBBIQfI")


class _Client:
    """Per-WebSocket send state with newest-only backpressure.

    The broadcaster overwrites `frames[camera]` / `texts[type]`; the sender
    task swaps them out and transmits. While a slow send is in flight, newer
    frames simply replace the pending ones - the client always gets the
    latest state, never a growing backlog.
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.frames: dict[int, bytes] = {}
        self.texts: dict[str, str] = {}
        self.events: deque[str] = deque(maxlen=50)  # events are not dropped
        self.wakeup = asyncio.Event()

    def queue_frame(self, camera_index: int, message: bytes) -> None:
        self.frames[camera_index] = message
        self.wakeup.set()

    def queue_text(self, kind: str, message: str) -> None:
        self.texts[kind] = message
        self.wakeup.set()

    def queue_event(self, message: str) -> None:
        self.events.append(message)
        self.wakeup.set()

    async def sender(self) -> None:
        while True:
            await self.wakeup.wait()
            self.wakeup.clear()
            frames, self.frames = self.frames, {}
            texts, self.texts = self.texts, {}
            events = list(self.events)
            self.events.clear()
            for message in texts.values():
                await self.ws.send_text(message)
            for message in events:
                await self.ws.send_text(message)
            for message in frames.values():
                await self.ws.send_bytes(message)


class _AppState:
    def __init__(
        self,
        controller: RecordingController,
        config: OctacamConfig,
        serial_link: SerialLink | None,
    ):
        self.controller = controller
        self.config = config
        self.serial_link = serial_link
        self.clients: set[_Client] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._frame_counters: dict[int, int] = {}

    # ------------------------------------------------------- broadcasting

    def _broadcast_text(self, kind: str, message: str) -> None:
        for client in list(self.clients):
            client.queue_text(kind, message)

    def _broadcast_event(self, message: str) -> None:
        for client in list(self.clients):
            client.queue_event(message)

    def broadcast_threadsafe(self, kind: str, payload: dict) -> None:
        """Push controller/state updates from non-asyncio threads."""
        loop = self.loop
        if loop is None or loop.is_closed() or not self.clients:
            return
        message = json.dumps({"type": kind, **payload})
        if kind == "event":
            loop.call_soon_threadsafe(self._broadcast_event, message)
        else:
            loop.call_soon_threadsafe(self._broadcast_text, kind, message)

    def on_controller_event(self, kind: str, payload: dict) -> None:
        self.broadcast_threadsafe(kind, payload)

    # ----------------------------------------------------- background tasks

    async def preview_loop(self) -> None:
        interval = max(self.config.gui.display_refresh_interval_ms, 10) / 1000
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(interval)
            if not self.clients:
                continue
            grabbed = []
            for index, camera in enumerate(self.controller.camera_system):
                frame = camera.frame_for_display.pop()
                if frame is not None:
                    grabbed.append((index, camera, frame))
            if not grabbed:
                continue
            messages = await loop.run_in_executor(
                None, self._encode_batch, grabbed
            )
            for camera_index, message in messages:
                for client in list(self.clients):
                    client.queue_frame(camera_index, message)

    def _encode_batch(self, grabbed) -> list[tuple[int, bytes]]:
        import cv2

        flags = 1 if self.controller.recording_active else 0
        messages = []
        for index, camera, frame in grabbed:
            height, width = frame.shape
            factor = max(1, math.ceil(max(width, height) / PREVIEW_MAX_DIM))
            small = np.ascontiguousarray(frame[::factor, ::factor])
            ok, jpeg = cv2.imencode(
                ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if not ok:
                continue
            count = self._frame_counters.get(index, 0) + 1
            self._frame_counters[index] = count
            header = FRAME_HEADER.pack(
                1,
                1,
                index,
                flags,
                count,
                time.time_ns(),
                camera.resulting_fps,
                camera.dropped_count,
            )
            messages.append((index, header + jpeg.tobytes()))
        return messages

    async def telemetry_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(TELEMETRY_INTERVAL_S)
            if not self.clients:
                continue
            snapshot = await loop.run_in_executor(
                None, self.controller.snapshot
            )
            self._broadcast_text(
                "telemetry", json.dumps({"type": "telemetry", **snapshot})
            )


def _parse_command(payload) -> Command:
    try:
        return Command(**{field: int(payload[field]) for field in COMMAND_FIELDS})
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "Invalid stepper command") from None


def create_app(
    controller: RecordingController,
    config: OctacamConfig,
    serial_link: SerialLink | None = None,
    config_dir: str = "",
) -> FastAPI:
    state = _AppState(controller, config, serial_link)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        state.loop = asyncio.get_running_loop()
        controller.add_listener(state.on_controller_event)
        tasks = [
            asyncio.create_task(state.preview_loop()),
            asyncio.create_task(state.telemetry_loop()),
        ]
        yield
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks)

    app = FastAPI(title="octacam", version=octacam.__version__, lifespan=lifespan)

    # Handlers are sync `def` on purpose: FastAPI runs them in its thread
    # pool, so blocking pylon/serial/filesystem calls never stall the
    # event loop that pumps the preview WebSocket.

    @app.get("/api/system")
    def get_system():
        config_by_serial = {c.serial_number: c for c in config.cameras}
        cameras = []
        for index, camera in enumerate(controller.camera_system):
            camera_config = config_by_serial.get(camera.serial_number)
            cameras.append(
                {
                    "index": index,
                    "serial": camera.serial_number,
                    "name": camera.name,
                    "width": camera.width,
                    "height": camera.height,
                    "layout": {
                        key: getattr(camera_config, key) if camera_config else -1.0
                        for key in (
                            "window_x",
                            "window_y",
                            "window_width",
                            "window_height",
                        )
                    },
                    "transform": {
                        key: getattr(camera_config, key)
                        if camera_config
                        else default
                        for key, default in (
                            ("scale_x", 1.0),
                            ("scale_y", 1.0),
                            ("rotation_deg", 0.0),
                        )
                    },
                }
            )
        return {
            "version": octacam.__version__,
            "config_dir": config_dir,
            "serial_available": serial_link is not None and serial_link.is_open,
            "display_refresh_interval_ms": (
                config.gui.display_refresh_interval_ms
            ),
            "formats": [
                {"codec": codec, "label": video_format.label}
                for codec, video_format in FORMATS.items()
            ],
            "cameras": cameras,
        }

    @app.get("/api/state")
    def get_state():
        return controller.snapshot()

    @app.get("/api/settings")
    def get_settings():
        return dataclasses.asdict(controller.get_settings())

    @app.put("/api/settings")
    def put_settings(changes: dict = Body(...)):
        try:
            updated = controller.update_settings(**changes)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        settings = dataclasses.asdict(updated)
        state.broadcast_threadsafe("settings", settings)
        return settings

    @app.post("/api/save-dir/validate")
    def validate_save_dir(payload: dict = Body(...)):
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(422, "path is required")
        return controller.validate_save_dir(path)

    @app.post("/api/recording/start")
    def start_recording(payload: dict = Body(default_factory=dict)):
        arduino_command = None
        if payload.get("arduino_command"):
            arduino_command = _parse_command(payload["arduino_command"])
        result = controller.start_recording(
            confirm_overwrite=bool(payload.get("confirm_overwrite")),
            arduino_command=arduino_command,
        )
        body = {"status": result.status, "message": result.message}
        if result.ok:
            return JSONResponse(body, status_code=202)
        if result.status in (StartResult.NEEDS_CONFIRM, StartResult.BUSY):
            return JSONResponse(body, status_code=409)
        return JSONResponse(body, status_code=500)

    @app.post("/api/recording/stop")
    def stop_recording():
        controller.stop_recording(abort=False)
        return JSONResponse({"status": "ok"}, status_code=202)

    @app.post("/api/recording/abort")
    def abort_recording():
        controller.stop_recording(abort=True)
        return JSONResponse({"status": "ok"}, status_code=202)

    @app.post("/api/serial/command")
    def serial_command(payload: dict = Body(...)):
        if serial_link is None or not serial_link.is_open:
            raise HTTPException(503, "Serial port not available")
        serial_link.write_command(_parse_command(payload))
        return {"status": "ok"}

    @app.websocket("/api/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        client = _Client(ws)
        state.clients.add(client)
        sender = asyncio.create_task(client.sender())
        loop = asyncio.get_running_loop()
        try:
            snapshot = await loop.run_in_executor(None, controller.snapshot)
            client.queue_text("state", json.dumps({"type": "state", **snapshot}))
            client.queue_text(
                "settings",
                json.dumps(
                    {
                        "type": "settings",
                        **dataclasses.asdict(controller.get_settings()),
                    }
                ),
            )
            while True:
                text = await ws.receive_text()
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if message.get("type") == "jog":
                    n_steps = message.get("n_steps")
                    if (
                        n_steps in (-1, 0, 1)
                        and serial_link is not None
                        and serial_link.is_open
                    ):
                        await loop.run_in_executor(
                            None,
                            serial_link.write_command,
                            Command(n_steps=n_steps),
                        )
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(client)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    if STATIC_DIR.is_dir():
        app.mount(
            "/", StaticFiles(directory=STATIC_DIR, html=True), name="static"
        )

    return app
