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
import itertools
import json
import logging
import math
import os
import signal
import struct
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import octacam
from octacam import config_writer
from octacam.config import OctacamConfig, find_config_file, parse_config
from octacam.controller import (
    RecordingController,
    StartResult,
    sanitize_camera_name,
)
from octacam.plugins.base import PluginManager
from octacam.writer import FORMATS

log = logging.getLogger("octacam")

STATIC_DIR = Path(__file__).parent / "static"
TELEMETRY_INTERVAL_S = 0.5
PREVIEW_MAX_DIM = 640  # longest preview edge after downscaling
JPEG_QUALITY = 75
# u8 version | u8 kind | u8 camera | u8 flags(bit0=recording) |
# u32 frame number | u64 timestamp ns | f32 fps | u32 dropped total
FRAME_HEADER = struct.Struct("<BBBBIQfI")


class SettingsPatch(BaseModel):
    """Partial update for RecordingSettings; unknown keys are rejected (422).

    Cross-field rules (fps > 0, known codec, …) stay in
    RecordingController.update_settings — only the fields actually sent are
    forwarded, via model_dump(exclude_unset=True)."""

    model_config = ConfigDict(extra="forbid")

    fps: float | None = None
    duration_s: float | None = None
    save_dir: str | None = None
    trigger_source: str | None = None
    codec: str | None = None
    crf: int | None = None
    preset: str | None = None
    pix_fmt: str | None = None
    remux_mp4: bool | None = None
    x264_params: str | None = None
    record_form: str | None = None
    save_frame_timestamps: bool | None = None


class SaveDirValidateRequest(BaseModel):
    path: str

    @field_validator("path")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path is required")
        return value


class BrowseRequest(BaseModel):
    """List a server-side directory's subfolders for the save-dir picker.

    A blank path is allowed (and means "open at the current save directory"),
    unlike SaveDirValidateRequest which requires one."""

    model_config = ConfigDict(extra="forbid")

    path: str = ""


class RecordingStartRequest(BaseModel):
    confirm_overwrite: bool = False
    plugin_params: dict | None = None


class CameraParamPatch(BaseModel):
    """Set one sensor parameter on the selected camera or all cameras."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: float
    scope: Literal["selected", "all"] = "selected"


class CameraParamReset(BaseModel):
    """Reset the selected camera's (or all cameras') params to the config."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["selected", "all"] = "selected"


class CameraNamePatch(BaseModel):
    """Rename one camera (validated and applied by the controller)."""

    model_config = ConfigDict(extra="forbid")

    name: str


class CameraTransformPatch(BaseModel):
    """One camera's live display transform (negative scale = flip).

    The View tab sends this on every rotate/flip so a "display"-form recording
    bakes in exactly what the operator sees, without needing a config save."""

    model_config = ConfigDict(extra="forbid")

    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0


class CameraDisplayParams(BaseModel):
    """A camera's composed display state, sent up by the browser to be saved.

    Defaults mirror CameraConfig so an unconfigured camera (window_* = -1 =
    "unset") round-trips through the tolerant loader unchanged."""

    model_config = ConfigDict(extra="forbid")

    serial: str
    name: str | None = None
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0
    window_x: float = -1.0
    window_y: float = -1.0
    window_width: float = -1.0
    window_height: float = -1.0

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str | None) -> str | None:
        # The name becomes a video filename stem, so hold a saved name to the
        # same rules as the live-rename endpoint (controller.set_camera_name).
        return sanitize_camera_name(value) if value is not None else None


class SaveConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["active", "new"] = "active"
    name: str | None = None  # required when target == "new"
    overwrite: bool = False  # only meaningful for target == "new"
    save_sensor: bool = True  # write <serial>.pfs files
    save_display: bool = True  # write octacam_config.toml
    cameras: list[CameraDisplayParams] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "SaveConfigRequest":
        # Two cameras sharing a name would write to the same video file, so
        # reject duplicates at the save boundary (the loader drops them too,
        # but only incidentally). Names are already sanitized per-field above.
        names = [c.name for c in self.cameras if c.name]
        if len(names) != len(set(names)):
            raise ValueError("Camera names must be unique")
        return self


class _Client:
    """Per-WebSocket send state with newest-only backpressure.

    The broadcaster overwrites `frames[camera]` / `texts[type]`; the sender
    task swaps them out and transmits. While a slow send is in flight, newer
    frames simply replace the pending ones - the client always gets the
    latest state, never a growing backlog.
    """

    _next_id = itertools.count(1)

    def __init__(self, ws: WebSocket):
        self.ws = ws
        # Stable per-connection id so plugins can scope transient per-client
        # state (e.g. the Arduino jog) to the socket that owns it.
        self.id = next(_Client._next_id)
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
        # A client can vanish mid-send (browser tab closed, SSH tunnel
        # dropped). Starlette surfaces that as WebSocketDisconnect from the
        # send_* calls; swallow it so the task ends cleanly instead of dying
        # with an exception that the endpoint's teardown `await sender` would
        # re-raise and crash the ASGI app with.
        with contextlib.suppress(WebSocketDisconnect):
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
        plugins: PluginManager,
        config_dir: str = "",
    ):
        self.controller = controller
        # `config` is the live source of truth (a save replaces it); `raw_config`
        # is the raw parsed TOML the writer patches so [gui]/[[plugins]] and the
        # strftime save-dir template survive a save verbatim.
        self.config = config
        self.config_dir = config_dir
        self.raw_config = (
            config_writer.load_raw_config(config_dir) if config_dir else {}
        )
        self.plugins = plugins
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

    def broadcast_presence(self) -> None:
        """Tell every connected browser how many are currently connected.

        Control is shared (any browser can drive the rig), so a presence count
        lets an operator see they are not alone before changing settings or
        shutting the server down. Called on the event-loop thread from the
        WebSocket handler, so the synchronous queue-to-clients path is safe.
        """
        self._broadcast_text(
            "presence",
            json.dumps({"type": "presence", "clients": len(self.clients)}),
        )

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
            messages = await loop.run_in_executor(None, self._encode_batch, grabbed)
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
            snapshot = await loop.run_in_executor(None, self.controller.snapshot)
            self._broadcast_text(
                "telemetry", json.dumps({"type": "telemetry", **snapshot})
            )


def _default_shutdown() -> None:
    """Stop the server. uvicorn already installs a SIGINT handler, so this
    triggers its graceful shutdown -> lifespan teardown -> the cleanup in the
    `finally` block of cli.gui (cameras released, ffmpeg finalized, serial
    closed)."""
    os.kill(os.getpid(), signal.SIGINT)


def create_app(
    controller: RecordingController,
    config: OctacamConfig,
    plugins: PluginManager | None = None,
    config_dir: str = "",
    shutdown_callback: Callable[[], None] = _default_shutdown,
) -> FastAPI:
    plugins = plugins if plugins is not None else PluginManager([])
    state = _AppState(controller, config, plugins, config_dir)

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
        config_by_serial = {c.serial_number: c for c in state.config.cameras}
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
                    "params": camera.read_params(),
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
                        key: getattr(camera_config, key) if camera_config else default
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
            "plugins": state.plugins.status(),
            "display_refresh_interval_ms": (
                state.config.gui.display_refresh_interval_ms
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
    def put_settings(patch: SettingsPatch):
        try:
            updated = controller.update_settings(**patch.model_dump(exclude_unset=True))
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        settings = dataclasses.asdict(updated)
        state.broadcast_threadsafe("settings", settings)
        return settings

    @app.get("/api/cameras/{index}/params")
    def get_camera_params(index: int):
        try:
            return controller.read_camera_params(index)
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None

    @app.put("/api/cameras/{index}/params")
    def put_camera_params(index: int, patch: CameraParamPatch):
        try:
            result = controller.set_camera_param(
                index, patch.name, patch.value, patch.scope
            )
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        # Push the new values to every client, deduped per camera index so a
        # fast slider drag collapses to the latest (newest-only queue_text).
        for entry in result["updated"]:
            state.broadcast_threadsafe(
                f"camera_params:{entry['index']}", {"type": "camera_params", **entry}
            )
        return result

    @app.put("/api/cameras/{index}/name")
    def put_camera_name(index: int, patch: CameraNamePatch):
        try:
            result = controller.set_camera_name(index, patch.name)
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        # Broadcast so every browser's grid tile and camera picker relabel; the
        # `:index` suffix dedups per camera in the newest-only send queue.
        state.broadcast_threadsafe(
            f"camera_name:{result['index']}", {"type": "camera_name", **result}
        )
        return result

    @app.put("/api/cameras/{index}/transform")
    def put_camera_transform(index: int, patch: CameraTransformPatch):
        try:
            return controller.set_camera_transform(
                index, patch.scale_x, patch.scale_y, patch.rotation_deg
            )
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    @app.post("/api/cameras/{index}/params/reset")
    def reset_camera_params(index: int, payload: CameraParamReset | None = None):
        if not config_dir:
            raise HTTPException(400, "No config directory is set for this session")
        payload = payload or CameraParamReset()
        pfs_by_serial = config_writer.read_pfs_files(
            config_dir, controller.camera_system.extension
        )
        try:
            result = controller.reset_camera_params(index, pfs_by_serial, payload.scope)
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except FileNotFoundError as e:
            raise HTTPException(422, str(e)) from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:  # a .pfs the device rejects
            raise HTTPException(422, str(e)) from None
        for entry in result["updated"]:
            state.broadcast_threadsafe(
                f"camera_params:{entry['index']}", {"type": "camera_params", **entry}
            )
        return result

    @app.get("/api/config/configs")
    def list_configs():
        if not config_dir:
            return {"active": "", "configs": []}
        parent = Path(config_dir).parent
        try:
            names = sorted(
                p.name for p in parent.iterdir() if (p / "octacam_config.toml").exists()
            )
        except OSError:
            names = []
        return {"active": Path(config_dir).name, "configs": names}

    @app.post("/api/config/save")
    def save_config(req: SaveConfigRequest):
        if not config_dir:
            raise HTTPException(400, "No config directory is set for this session")
        if not req.save_sensor and not req.save_display:
            raise HTTPException(422, "Nothing to save: enable sensor and/or display")
        # Refuse while recording: a full nodemap snapshot would contend with the
        # record grab loop, and the operator should not reshape config mid-trial.
        if controller.recording_active:
            raise HTTPException(409, "Cannot save the config while recording")

        active = Path(config_dir)
        try:
            pfs = controller.export_camera_params() if req.save_sensor else {}
            doc = (
                config_writer.merge_camera_display(
                    state.raw_config, [c.model_dump() for c in req.cameras]
                )
                if req.save_display
                else None
            )
            if req.target == "active":
                target = active
            else:
                target = config_writer.resolve_new_config_dir(
                    active, req.name or "", overwrite=req.overwrite
                )
                target.mkdir(parents=True, exist_ok=True)
                config_writer.copy_auxiliary_pfs(
                    active, target, set(pfs), controller.camera_system.extension
                )
            if req.save_sensor:
                config_writer.write_pfs_files(
                    target, pfs, controller.camera_system.extension
                )
            if req.save_display and doc is not None:
                config_writer.write_config(target, doc)
        except RuntimeError as e:  # recording started between the check and save
            raise HTTPException(409, str(e)) from None
        except ValueError as e:  # invalid new-config name
            raise HTTPException(422, str(e)) from None
        except FileExistsError as e:
            raise HTTPException(
                409, f"Config already exists: {e}. Resend with overwrite=true."
            ) from None
        except PermissionError as e:
            raise HTTPException(403, f"Config directory is not writable: {e}") from None
        except OSError as e:
            raise HTTPException(500, f"Failed to write config: {e}") from None

        # Adopt the just-saved layout as the live config so /api/system reflects
        # it immediately (only for the active dir; "new" is write-only), and
        # refresh the live per-camera display transforms from it so a saved
        # rotation/flip keeps baking into recordings.
        if req.target == "active" and req.save_display and doc is not None:
            state.raw_config = doc
            state.config = parse_config(find_config_file(active))
            controller.camera_system.apply_display_config(state.config.cameras)

        return {
            "status": "ok",
            "config_dir": str(target),
            "target": req.target,
            "cameras_written": sorted(pfs),
        }

    @app.post("/api/save-dir/validate")
    def validate_save_dir(payload: SaveDirValidateRequest):
        return controller.validate_save_dir(payload.path)

    @app.post("/api/browse")
    def browse(payload: BrowseRequest | None = None):
        payload = payload or BrowseRequest()
        return controller.browse_directory(payload.path)

    @app.post("/api/recording/start")
    def start_recording(payload: RecordingStartRequest | None = None):
        payload = payload or RecordingStartRequest()
        result = controller.start_recording(
            confirm_overwrite=payload.confirm_overwrite,
            plugin_params=payload.plugin_params,
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

    @app.post("/api/shutdown")
    def shutdown(background_tasks: BackgroundTasks):
        # Shutting down releases the cameras for everyone, so refuse while a
        # recording is in progress rather than discarding it (controller.close
        # aborts). The background task runs after the 202 is flushed, so the
        # client always learns the request was accepted before the server dies.
        if controller.recording_active:
            raise HTTPException(
                409, "Stop the recording before shutting down the server"
            )
        background_tasks.add_task(shutdown_callback)
        return JSONResponse({"status": "shutting_down"}, status_code=202)

    @app.websocket("/api/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        client = _Client(ws)
        state.clients.add(client)
        state.broadcast_presence()
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
                # Hand the message to plugins (e.g. Arduino jog); the first
                # one to claim it wins. Run in the executor so a plugin's
                # blocking I/O never stalls the event loop. The client id lets
                # a plugin scope per-connection state to the owning socket.
                for plugin in state.plugins.plugins:
                    handled = await loop.run_in_executor(
                        None, plugin.on_ws_message, message, client.id
                    )
                    if handled:
                        break
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(client)
            state.broadcast_presence()
            # Let plugins react to this socket closing (e.g. the Arduino jog
            # clock stops if this client owned it, so a dropped connection
            # can't leave the motor spinning — but another client's jog is
            # left untouched). Off the event loop in case the hook blocks.
            await loop.run_in_executor(
                None, state.plugins.dispatch, "on_ws_disconnect", client.id
            )
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    # Plugin-contributed REST endpoints (e.g. Arduino's /api/serial/command).
    # Registered before the static catch-all mount at "/".
    for plugin in plugins.plugins:
        router = plugin.api_router()
        if router is not None:
            app.include_router(router)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
