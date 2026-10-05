"""FastAPI backend for the octacam web GUI.

One process serves the SPA, a REST control plane and one WebSocket carrying
preview JPEGs (binary), telemetry/state/event JSON and plugin messages such as
the flywheel jog. One socket works through a plain `ssh -L` forward and stays
under the browser's per-host connection limit. Preview frames follow the display
refresh rate, go newest-only to each client, and are encoded only for a client.
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
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.websockets import WebSocketState

import octacam
from octacam import config_writer, updates
from octacam.config import (
    ConfigError,
    OctacamConfig,
    find_config_file,
    parse_config,
    safe_segment,
)
from octacam.controller import RecordingController, StartResult
from octacam.transform import RECORDING_INFO_DIRNAME
from octacam.writer import FORMATS, NVENC_H264_PARAMS, nvenc_max_sessions

log = logging.getLogger("octacam")

STATIC_DIR = Path(__file__).parent / "static"
TELEMETRY_INTERVAL_S = 0.5
# Recent controller events replayed to a (re)connecting client's log; also each
# client's event-queue bound, so a replay is never truncated.
EVENT_BACKLOG_REPLAY = 50
# Longest preview edge of an unfocused tile; only a focused (maximized or
# zoomed) tile may go finer.
PREVIEW_MAX_DIM = 640
# A focused tile's cap while recording, so a preview encode cannot starve the writers.
PREVIEW_FOCUS_MAX_DIM_RECORDING = 1280
# Distinct resolutions of one camera encoded per tick; extra requests fall back
# to the baseline (_cap_variants), so clients cannot multiply the encode cost.
MAX_PREVIEW_VARIANTS_PER_CAMERA = 4
JPEG_QUALITY = 75
# Preview frame header, version 2 (little-endian; ws.js rejects other versions):
#   u8  version (2) | u8 kind (1) | u8 camera | u8 flags(bit0=recording)
#   u32 frame number | u64 timestamp ns | f32 fps | u32 dropped total
#   u16 crop_x | u16 crop_y | u16 crop_w | u16 crop_h   (sensor px covered)
#   u16 sensor_w | u16 sensor_h                          (full sensor size)
# The crop rect (the whole sensor when uncropped) lets the client place the
# image under its display transform.
FRAME_HEADER = struct.Struct("<BBBBIQfIHHHHHH")
FRAME_VERSION = 2


class _NoCacheStaticFiles(StaticFiles):
    """Static files with ``Cache-Control: no-cache``: the assets are unversioned,
    so a reload must revalidate (ETags still make an unchanged file a cheap 304)."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@dataclasses.dataclass(frozen=True)
class _ViewSpec:
    """What one client needs of one camera; the default is the baseline preview.

    ``want`` False skips the camera for that client (hidden behind a maximized
    tile). ``need`` is the longest source edge in px it can display (None: the
    baseline); ``full`` marks a focused tile, which may exceed
    ``PREVIEW_MAX_DIM``; ``crop`` (x, y, w, h in sensor px) asks for that region
    only."""

    want: bool = True
    need: int | None = None
    full: bool = False
    crop: tuple[int, int, int, int] | None = None


_DEFAULT_VIEW = _ViewSpec()


def _parse_crop(crop) -> tuple[int, int, int, int] | None:
    """A {"x","y","w","h"} dict as an int tuple; None if absent, malformed or empty."""
    if not isinstance(crop, dict):
        return None
    try:
        x, y = int(crop["x"]), int(crop["y"])
        w, h = int(crop["w"]), int(crop["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or x < 0 or y < 0:
        return None
    return (x, y, w, h)


def _clamp_crop(
    crop: tuple[int, int, int, int] | None, width: int, height: int
) -> tuple[int, int, int, int]:
    """Clamp a crop to the sensor (the whole sensor for None). The header carries
    the clamped rect: the client places what was sent, not what it asked for."""
    if crop is None:
        return (0, 0, width, height)
    x, y, w, h = crop
    x = max(0, min(int(x), max(0, width - 1)))
    y = max(0, min(int(y), max(0, height - 1)))
    w = max(1, min(int(w), width - x))
    h = max(1, min(int(h), height - y))
    return (x, y, w, h)


def _preview_factor(
    sensor_long: int, region_long: int, spec: _ViewSpec, recording: bool
) -> int:
    """Integer decimation of the encoded region (the sensor, or a crop of it).

    Without ``need``: the baseline ``ceil(sensor_long / PREVIEW_MAX_DIM)``. An
    unfocused tile may only go coarser; a focused one may go down to 1:1, capped
    at ``PREVIEW_FOCUS_MAX_DIM_RECORDING`` while recording."""
    sensor_long = max(sensor_long, 1)
    region_long = max(region_long, 1)
    baseline = max(1, math.ceil(sensor_long / PREVIEW_MAX_DIM))
    if spec.need is None:
        return baseline
    need = max(1, spec.need)
    if not spec.full:
        # Never sharper than the baseline, or one HiDPI client would upgrade
        # every camera.
        return max(baseline, max(1, round(region_long / need)))
    # round() matches the request, erring toward full detail.
    need = min(need, region_long)
    factor = max(1, round(region_long / need))
    if recording:
        # ceil, not round: a hard cap (round could overshoot it by up to ~1.5x).
        factor = max(factor, math.ceil(region_long / PREVIEW_FOCUS_MAX_DIM_RECORDING))
    return factor


class SaveDirValidateRequest(BaseModel):
    path: str

    @field_validator("path")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path is required")
        return value


class BrowseRequest(BaseModel):
    """A directory to list for the save-dir picker; blank means the save dir."""

    model_config = ConfigDict(extra="forbid")

    path: str = ""


class RecordingStartRequest(BaseModel):
    confirm_overwrite: bool = False
    plugin_params: dict | None = None


class ShutdownRequest(BaseModel):
    process_after: bool = False


class DiagnosticRunRequest(BaseModel):
    """Parameters for a Benchmark run (octacam.diagnostics)."""

    model_config = ConfigDict(extra="forbid")

    # None targets the current Record-tab fps; a value overrides it for this run.
    target_fps: float | None = None
    duration_s: float = 5.0
    find_max: bool = True
    sink: Literal["config", "null"] = "config"

    @field_validator("duration_s")
    @classmethod
    def _duration_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("duration_s must be > 0")
        return value

    @field_validator("target_fps")
    @classmethod
    def _fps_positive(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("target_fps must be > 0")
        return value


class CameraFeaturePatch(BaseModel):
    """Set one node-map feature by GenApi name; the backend coerces ``value`` to
    the node's type (an enum sends its symbol)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: Any
    scope: Literal["selected", "all"] = "selected"


class CameraFeatureReset(BaseModel):
    """Reset one node-map feature to its config value (else factory default)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    scope: Literal["selected", "all"] = "selected"


class CameraCommandRequest(BaseModel):
    """Execute one command node (e.g. TimestampLatch) on the selected camera."""

    model_config = ConfigDict(extra="forbid")

    name: str


class CameraCenterPatch(BaseModel):
    """Toggle ROI auto-centering on an axis for one camera or all."""

    model_config = ConfigDict(extra="forbid")

    axis: Literal["x", "y"]
    enabled: bool
    scope: Literal["selected", "all"] = "selected"


class CameraNamePatch(BaseModel):
    """Rename one camera (validated and applied by the controller)."""

    model_config = ConfigDict(extra="forbid")

    name: str


class CameraTransformPatch(BaseModel):
    """One camera's live display transform (negative scale = flip), sent on every
    rotate/flip so a "display"-form recording bakes in what the operator sees."""

    model_config = ConfigDict(extra="forbid")

    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0


class CameraDisplayParams(BaseModel):
    """A camera's display state to save; defaults mirror CameraConfig (-1: unset)."""

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
    center_x: bool = False
    center_y: bool = False

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str | None) -> str | None:
        # A video filename stem: the live rename's rules apply.
        return safe_segment(value, "camera name") if value is not None else None


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
        # Two cameras sharing a name would write to the same video file.
        names = [c.name for c in self.cameras if c.name]
        if len(names) != len(set(names)):
            raise ValueError("Camera names must be unique")
        return self


class _Client:
    """One WebSocket's send state. Frames and texts are newest-only (a slow client
    gets fewer updates, never a backlog); events queue."""

    _next_id = itertools.count(1)

    def __init__(self, ws: WebSocket):
        self.ws = ws
        # Lets a plugin scope per-connection state (the flywheel jog) to this socket.
        self.id = next(_Client._next_id)
        self.frames: dict[int, bytes] = {}
        self.texts: dict[str, str] = {}
        self.events: deque[str] = deque(maxlen=EVENT_BACKLOG_REPLAY)
        self.wakeup = asyncio.Event()
        # Event-loop thread only, so no lock.
        self.views: dict[int, _ViewSpec] = {}

    def view_for(self, camera_index: int) -> _ViewSpec:
        return self.views.get(camera_index, _DEFAULT_VIEW)

    def apply_view(self, message: dict) -> None:
        """Update view specs from a ``{"type": "view"}`` message. A malformed entry
        is skipped, never raised: that would tear down the socket."""
        cameras = message.get("cameras")
        if not isinstance(cameras, dict):
            return
        for key, spec in cameras.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(spec, dict):
                continue
            need = spec.get("need")
            if need is not None:
                try:
                    need = int(need)
                except (TypeError, ValueError):
                    need = None
                else:
                    if need <= 0:
                        need = None
            self.views[index] = _ViewSpec(
                want=bool(spec.get("want", True)),
                need=need,
                full=bool(spec.get("full", False)),
                crop=_parse_crop(spec.get("crop")),
            )

    def queue_frame(self, camera_index: int, message: bytes) -> None:
        self.frames[camera_index] = message
        self.wakeup.set()

    def queue_text(self, kind: str, message: str) -> None:
        self.texts[kind] = message
        self.wakeup.set()

    def queue_event(self, message: str) -> None:
        self.events.append(message)
        self.wakeup.set()

    def is_ready_for(self, camera_index: int) -> bool:
        """True when no frame for this camera is pending; the preview loop encodes
        only for ready clients, so a stalled client costs no CPU."""
        return camera_index not in self.frames

    async def sender(self) -> None:
        # A send after the socket closed raises a bare RuntimeError from the ASGI
        # layer. End quietly on it and on a disconnect: the endpoint's teardown
        # `await sender` would re-raise either into the ASGI app.
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
        config_dir: str = "",
    ):
        self.controller = controller
        # `config` is live (a save replaces it); `raw_config` is the parsed TOML a
        # save patches, so [gui], [[plugins]] and the save-dir template survive it.
        self.config = config
        self.config_dir = config_dir
        self.raw_config = (
            config_writer.load_raw_config(config_dir) if config_dir else {}
        )
        self.plugins = controller.plugins
        # {name: assets dir} of the plugins with a web UI, set once by create_app.
        self.plugin_web: dict[str, Path] = {}
        # Set by POST /api/shutdown ("Shut down & process"); cli.gui's teardown then
        # starts a detached processing job.
        self.process_after = False
        self.clients: set[_Client] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._frame_counters: dict[int, int] = {}
        # The update banner's notice; None until the background check returns.
        self._update_notice: updates.UpdateNotice | None = None

    def refresh_update_notice(self) -> None:
        """Populate _update_notice from a PyPI check. Fail-soft; never raises."""
        try:
            self._update_notice = updates.check()
        except Exception:
            log.debug("update check failed", exc_info=True)

    def update_status(self) -> dict | None:
        """The update notice for /api/system, or None if not (yet) known."""
        return self._update_notice.as_dict() if self._update_notice else None

    def system_descriptor(self) -> dict:
        """The /api/system payload, also pushed as the WS ``system`` message.

        ``ready`` is False (``cameras: []``) while the init thread opens the
        cameras; it pushes a fresh descriptor once they are attached."""
        controller = self.controller
        config_by_serial = {c.serial_number: c for c in self.config.cameras}
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
                        key: getattr(camera_config, key) if camera_config else default
                        for key, default in (
                            ("scale_x", 1.0),
                            ("scale_y", 1.0),
                            ("rotation_deg", 0.0),
                        )
                    },
                    # So a save covers cameras whose Camera tab was never opened.
                    "center_x": camera.center_x,
                    "center_y": camera.center_y,
                }
            )
        # Where app.js imports each plugin's UI bundle from.
        plugins_status = self.plugins.status()
        for name, adir in self.plugin_web.items():
            entry = plugins_status.get(name)
            if entry is None:
                continue
            web = {"module": f"/plugins/{name}/{name}.js"}
            if (adir / f"{name}.css").is_file():
                web["css"] = f"/plugins/{name}/{name}.css"
            entry["web"] = web
        return {
            "version": octacam.__version__,
            "update": self.update_status(),
            "ready": controller.ready,
            # Why the init opened no camera, instead of an endless placeholder.
            "init_error": controller.init_error,
            # Configured cameras that did not open: a 7-of-8 rig must not look
            # like a healthy one with a smaller grid.
            "missing_cameras": [
                {"serial": serial, "reason": reason}
                for serial, reason in sorted(controller.camera_system.missing.items())
            ],
            "config_dir": self.config_dir,
            "plugins": plugins_status,
            # A loaded plugin can drive the trigger: offer "managed".
            "managed_trigger_available": controller.managed_trigger_available,
            "display_refresh_interval_ms": (
                self.config.gui.display_refresh_interval_ms
            ),
            "theme": self.config.gui.theme,
            "formats": [
                {"save_method": save_method, "label": video_format.label}
                for save_method, video_format in FORMATS.items()
            ],
            "cameras": cameras,
        }

    def broadcast_system(self) -> None:
        """Push a fresh descriptor to every browser (the init thread, once the
        cameras are attached)."""
        self.broadcast_threadsafe("system", self.system_descriptor())

    # ------------------------------------------------------- broadcasting

    def _broadcast_text(self, kind: str, message: str) -> None:
        for client in list(self.clients):
            client.queue_text(kind, message)

    def _broadcast_event(self, message: str) -> None:
        for client in list(self.clients):
            client.queue_event(message)

    def broadcast_presence(self) -> None:
        """Tell every browser how many are connected (any of them can drive the
        rig). Event-loop thread only."""
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
            clients = list(self.clients)
            if not clients:
                continue
            recording = self.controller.recording_active
            # Group the ready clients that want each camera by (crop, factor):
            # each variant is encoded once and shared, and a camera no ready
            # client wants is neither popped nor encoded.
            jobs = []
            for index, camera in enumerate(self.controller.camera_system):
                width, height = camera.width, camera.height
                sensor_long = max(width, height)
                groups: dict[tuple, list[_Client]] = {}
                for client in clients:
                    if not client.is_ready_for(index):
                        continue
                    spec = client.view_for(index)
                    if not spec.want:
                        continue
                    region = _clamp_crop(spec.crop, width, height)
                    region_long = max(region[2], region[3])
                    factor = _preview_factor(sensor_long, region_long, spec, recording)
                    groups.setdefault((region, factor), []).append(client)
                if not groups:
                    continue
                self._cap_variants(groups, width, height, sensor_long)
                frame = camera.frame_for_display.pop()
                if frame is None:
                    continue
                # Frame number and telemetry are taken here, on the event-loop
                # thread, so the encode workers mutate nothing.
                count = self._frame_counters.get(index, 0) + 1
                self._frame_counters[index] = count
                jobs.append((
                    index,
                    frame,
                    groups,
                    count,
                    time.time_ns(),
                    camera.resulting_fps,
                    camera.dropped_count,
                ))
            if not jobs:
                continue
            # One executor task per camera: cv2.imencode releases the GIL, so a
            # tick costs the slowest camera, not the sum (eight focused 2048²
            # cameras: 85 ms serial vs 14 ms parallel, against a 33 ms refresh).
            flags = 1 if recording else 0
            batches = await asyncio.gather(*[
                loop.run_in_executor(None, self._encode_camera, job, flags)
                for job in jobs
            ])
            for messages in batches:
                for camera_index, message, group in messages:
                    for client in group:
                        client.queue_frame(camera_index, message)

    @staticmethod
    def _cap_variants(
        groups: dict[tuple, list["_Client"]],
        width: int,
        height: int,
        sensor_long: int,
    ) -> None:
        """Keep the cheapest variants and demote the rest to the whole-sensor
        baseline. Two different crops are never merged (a client would see the
        wrong region): demotion only widens a crop to the full frame."""
        if len(groups) <= MAX_PREVIEW_VARIANTS_PER_CAMERA:
            return

        def output_pixels(key):  # encode cost proxy
            (_, _, w, h), factor = key
            return (w // factor + 1) * (h // factor + 1)

        cheapest = sorted(groups, key=output_pixels)
        keep = set(cheapest[: MAX_PREVIEW_VARIANTS_PER_CAMERA - 1])
        baseline = ((0, 0, width, height), max(1, math.ceil(sensor_long / PREVIEW_MAX_DIM)))
        bucket = groups.setdefault(baseline, [])
        for key in cheapest[MAX_PREVIEW_VARIANTS_PER_CAMERA - 1 :]:
            if key in keep or key == baseline:
                continue
            bucket.extend(groups.pop(key))

    @staticmethod
    def _encode_camera(job, flags: int) -> list[tuple[int, bytes, list["_Client"]]]:
        """Encode every variant of one camera's frame, on an executor thread.
        Everything arrives by value: it touches no shared state or camera."""
        import cv2

        index, frame, groups, count, timestamp, fps, dropped = job
        frame_h, frame_w = frame.shape
        messages = []
        for (region, factor), group in groups.items():
            # The popped frame can differ from camera.width/height across a
            # geometry change, and numpy would clip silently.
            x, y, w, h = _clamp_crop(region, frame_w, frame_h)
            whole = (x, y, w, h) == (0, 0, frame_w, frame_h)
            if factor > 1 or not whole:
                sub = np.ascontiguousarray(frame[y : y + h : factor, x : x + w : factor])
            else:
                sub = np.ascontiguousarray(frame)
            ok, jpeg = cv2.imencode(
                ".jpg", sub, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if not ok:
                continue
            header = FRAME_HEADER.pack(
                FRAME_VERSION, 1, index, flags, count, timestamp, fps, dropped,
                x, y, w, h, frame_w, frame_h,
            )
            messages.append((index, header + jpeg.tobytes(), group))
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
    """SIGINT ourselves: uvicorn shuts down gracefully, then cli.gui's ``finally``
    releases the hardware."""
    os.kill(os.getpid(), signal.SIGINT)


@contextlib.contextmanager
def _http_errors():
    """Map a controller error to HTTP: no such camera 404, busy 409, bad value 422.
    A route with a narrower contract maps only its own errors, so an unexpected one
    stays a logged 500."""
    try:
        yield
    except IndexError as e:
        raise HTTPException(404, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e)) from None


def create_app(
    controller: RecordingController,
    config: OctacamConfig,
    config_dir: str = "",
    shutdown_callback: Callable[[], None] = _default_shutdown,
) -> FastAPI:
    """The GUI's app for ``controller``, serving the plugins it was built with."""
    state = _AppState(controller, config, config_dir)
    plugins = state.plugins
    plugins.attach(broadcast=state.broadcast_threadsafe)

    # Resolved once, so the mounts and /api/system agree on which plugins have a
    # UI; a missing dir means none.
    plugin_web: dict[str, Path] = {}
    for plugin in plugins.plugins:
        adir, name = plugin.web_dir, plugin.name
        if adir is None:
            continue
        if not adir.is_dir():
            log.warning("Plugin %r: web_dir %s does not exist; skipping", name, adir)
            continue
        plugin_web[name] = adir
    state.plugin_web = plugin_web

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

    app.state.app_state = state  # read by cli.gui
    # GUI load never waits on the PyPI check.
    threading.Thread(
        target=state.refresh_update_notice,
        name="octacam-update-check",
        daemon=True,
    ).start()

    def _require_config_dir() -> Path:
        if not config_dir:
            raise HTTPException(400, "No config directory is set for this session")
        return Path(config_dir)

    # Handlers are sync `def`: FastAPI runs them in its thread pool, so blocking
    # SDK, serial and filesystem calls never stall the preview WebSocket's loop.

    @app.get("/api/system")
    def get_system():
        return state.system_descriptor()

    @app.get("/api/serial/ports")
    def get_serial_ports():
        """Serial ports for the plugin tabs' picker; never opens one, so it is safe
        while a board is armed."""
        from octacam import serial_ports

        return {
            "ports": [
                {
                    "device": p.device,
                    "board_name": p.board_name,
                    "vid_pid": p.vid_pid,
                    "serial_number": p.serial_number,
                    "likely_arduino": p.likely_arduino,
                    "likely_microcontroller": p.likely_microcontroller,
                }
                for p in serial_ports.list_serial_ports()
            ]
        }

    @app.get("/api/state")
    def get_state():
        return controller.snapshot()

    @app.get("/api/settings")
    def get_settings():
        return dataclasses.asdict(controller.get_settings())

    @app.put("/api/settings")
    def put_settings(patch: dict[str, Any]):
        """Apply the fields sent; an unknown key or bad value answers 422 with
        the controller's message (RecordingSettings.updated)."""
        try:
            updated = controller.update_settings(**patch)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        settings = dataclasses.asdict(updated)
        state.broadcast_threadsafe("settings", settings)
        return settings

    @app.get("/api/nvenc/capabilities")
    def get_nvenc_capabilities():
        """NVENC capability and the detected session cap (what ``auto`` resolves
        to). The first call runs the cached probe, which loads the GPU, so the
        client asks only once nvenc is selected."""
        detected = nvenc_max_sessions()
        return {
            "available": detected is not None and detected > 0,
            "max_sessions": detected,
            "encoder": "h264_nvenc",
            "default_params": NVENC_H264_PARAMS,
        }

    @app.put("/api/cameras/{index}/name")
    def put_camera_name(index: int, patch: CameraNamePatch):
        with _http_errors():
            result = controller.set_camera_name(index, patch.name)
        # The `:index` suffix keeps one pending rename per camera (newest-only).
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

    # ---------------------------------------------- full device node map (tab)

    def _broadcast_features_dirty(result: dict) -> None:
        """Tell clients to re-GET a changed camera's features (too large to push)."""
        for entry in result["updated"]:
            index = entry["index"]
            state.broadcast_threadsafe(
                f"camera_features_dirty:{index}",
                {"type": "camera_features_dirty", "index": index,
                 "center_x": entry["center_x"], "center_y": entry["center_y"]},
            )

    @app.get("/api/cameras/{index}/features")
    def get_camera_features(index: int):
        try:
            return controller.read_camera_features(index)
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None

    @app.put("/api/cameras/{index}/features")
    def put_camera_feature(index: int, patch: CameraFeaturePatch):
        with _http_errors():
            result = controller.set_camera_feature(
                index, patch.name, patch.value, patch.scope
            )
        _broadcast_features_dirty(result)
        return result

    @app.post("/api/cameras/{index}/features/reset")
    def reset_camera_feature(index: int, payload: CameraFeatureReset):
        pfs_by_serial = config_writer.read_pfs_files(
            _require_config_dir(), controller.camera_system.extensions
        )
        with _http_errors():
            result = controller.reset_camera_feature(
                index, payload.name, pfs_by_serial, payload.scope
            )
        _broadcast_features_dirty(result)
        return result

    @app.post("/api/cameras/{index}/commands")
    def run_camera_command(index: int, payload: CameraCommandRequest):
        with _http_errors():
            result = controller.execute_camera_command(index, payload.name)
        _broadcast_features_dirty(result)
        return result

    @app.put("/api/cameras/{index}/center")
    def put_camera_center(index: int, patch: CameraCenterPatch):
        with _http_errors():
            result = controller.set_camera_center(
                index, patch.axis, patch.enabled, patch.scope
            )
        _broadcast_features_dirty(result)
        return result

    def _preset_anchor() -> Path:
        """The directory a config saved as new goes next to: the config dir, or the
        recording folder for a session relaunched from a recording."""
        active = Path(config_dir)
        return active.parent if active.name == RECORDING_INFO_DIRNAME else active

    @app.post("/api/config/save")
    def save_config(req: SaveConfigRequest):
        active = _require_config_dir()
        if not req.save_sensor and not req.save_display:
            raise HTTPException(422, "Nothing to save: enable sensor and/or display")
        # A node-map snapshot would contend with the record grab loops.
        if controller.recording_active:
            raise HTTPException(409, "Cannot save the config while recording")

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
                    _preset_anchor(), req.name or "", overwrite=req.overwrite
                )
                target.mkdir(parents=True, exist_ok=True)
                config_writer.copy_auxiliary_pfs(
                    active, target, set(pfs), controller.camera_system.extensions
                )
            if req.save_sensor:
                config_writer.write_pfs_files(
                    target, pfs, controller.camera_system.extension_by_serial()
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

        # A saved active config goes live ("new" is write-only), with its display
        # transforms, which recordings bake in.
        if req.target == "active" and req.save_display and doc is not None:
            state.raw_config = doc
            try:
                state.config = parse_config(find_config_file(active))
            except ConfigError as e:
                # Unexpected (written from a validated document); the save succeeded.
                log.error("Saved config did not parse back; keeping the live one: %s", e)
            else:
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

    @app.post("/api/diagnostics/run")
    def run_diagnostic(payload: DiagnosticRunRequest | None = None):
        # The report arrives later as a WS "diagnostics" message.
        payload = payload or DiagnosticRunRequest()
        result = controller.run_diagnostic(
            target_fps=payload.target_fps,
            duration_s=payload.duration_s,
            find_max=payload.find_max,
            sink=payload.sink,
        )
        body = {"status": result.status, "message": result.message}
        return JSONResponse(body, status_code=202 if result.ok else 409)

    @app.post("/api/diagnostics/cancel")
    def cancel_diagnostic():
        controller.cancel_diagnostic()
        return JSONResponse({"status": "ok"}, status_code=202)

    @app.post("/api/shutdown")
    def shutdown(background_tasks: BackgroundTasks, body: ShutdownRequest | None = None):
        # Closing would abort the take. The callback runs after the 202 is sent, so
        # the client learns the request was accepted before the server dies.
        if controller.recording_active or controller.diagnosing:
            raise HTTPException(
                409,
                "Stop the recording or benchmark before shutting down the server",
            )
        state.process_after = bool(body and body.process_after)
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
            # The descriptor is built and queued with no await between, so an init
            # that finishes later reaches this client after it, never before.
            client.queue_text(
                "system", json.dumps({"type": "system", **state.system_descriptor()})
            )
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
            # Replay the last benchmark report and recent events.
            last_diag = controller.get_last_diagnostic()
            if last_diag:
                client.queue_text(
                    "diagnostics", json.dumps({"type": "diagnostics", **last_diag})
                )
            for event in list(controller.events)[-EVENT_BACKLOG_REPLAY:]:
                client.queue_event(json.dumps({"type": "event", **event}))
            while True:
                text = await ws.receive_text()
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                # "view" is the core's: cheap dict work, handled inline, never
                # offered to plugins.
                if isinstance(message, dict) and message.get("type") == "view":
                    client.apply_view(message)
                    continue
                # In the executor: a plugin's hook may block on I/O. A raising
                # hook is logged, so a bad message cannot kill the socket.
                await loop.run_in_executor(
                    None, state.plugins.on_ws_message, message, client.id
                )
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(client)
            state.broadcast_presence()
            # E.g. the flywheel stops a jog this client owned, so a dropped socket
            # cannot leave the motor spinning. In the executor: it may block.
            await loop.run_in_executor(
                None, state.plugins.on_ws_disconnect, client.id
            )
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    # Plugin REST routes, registered before the "/" catch-all.
    for plugin in plugins.plugins:
        router = plugin.api_router()
        if router is not None:
            app.include_router(router)

    # Before the "/" catch-all, whose html=True fallback would answer a /plugins/
    # path with index.html (not runnable as a module). No html=True here, so a
    # missing plugin asset 404s.
    for name, adir in plugin_web.items():
        app.mount(
            f"/plugins/{name}",
            _NoCacheStaticFiles(directory=adir),
            name=f"plugin-{name}",
        )

    if STATIC_DIR.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
