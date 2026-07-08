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
import re
import signal
import struct
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
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.websockets import WebSocketState

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
# A plugin name is interpolated into its asset mount path and the client-side
# import() URL, so only allow names that can't escape the /plugins/ prefix.
_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
TELEMETRY_INTERVAL_S = 0.5
# Longest preview edge for a normal (unfocused) tile. A client's adaptive
# request may only make its preview *coarser* than this; going finer than the
# baseline is reserved for a focused tile (maximized or zoomed, see _ViewSpec).
PREVIEW_MAX_DIM = 640
# While recording, a focused tile's preview is capped here rather than allowed
# up to full sensor resolution, so a large preview JPEG encode can't starve the
# recording writer's CPU. Idle, a focused tile may go all the way to sensor res.
PREVIEW_FOCUS_MAX_DIM_RECORDING = 1280
# Safety bound: at most this many distinct resolution variants of ONE camera are
# encoded per tick. Beyond it, the sharpest (most expensive) extra requests are
# coalesced onto the sharpest kept variant, so many clients each asking for a
# different resolution of the same camera can't multiply encode cost without
# bound. In normal use (clients with similar layouts) there is one variant.
MAX_PREVIEW_VARIANTS_PER_CAMERA = 4
JPEG_QUALITY = 75
# Preview frame header, version 2 (little-endian):
#   u8  version (2) | u8 kind (1) | u8 camera | u8 flags(bit0=recording)
#   u32 frame number | u64 timestamp ns | f32 fps | u32 dropped total
#   u16 crop_x | u16 crop_y | u16 crop_w | u16 crop_h   (sensor px covered)
#   u16 sensor_w | u16 sensor_h                          (full sensor size)
# The crop rect lets the client place a server-side crop (a zoomed region) at
# the right spot under its display transform; for an un-cropped frame it is the
# whole sensor (0, 0, sensor_w, sensor_h). Bumping the version byte from 1 keeps
# the client's version check meaningful (an old client fails closed).
FRAME_HEADER = struct.Struct("<BBBBIQfIHHHHHH")
FRAME_VERSION = 2


@dataclasses.dataclass(frozen=True)
class _ViewSpec:
    """What one client currently needs for one camera (sent up over the WS).

    ``want`` is False for a camera hidden behind another client's maximized
    tile — the server then skips grabbing and encoding it entirely for that
    client. ``need`` is the longest source edge (in device pixels) the client
    can actually display; ``full`` marks a focused tile (maximized/zoomed) that
    is allowed to exceed the ``PREVIEW_MAX_DIM`` baseline up to sensor
    resolution. ``crop`` (x, y, w, h in sensor px) asks the server to send only
    that sub-rectangle — a zoomed-in region delivered at full detail for the
    cost of a small frame, instead of the whole sensor. A default spec
    reproduces the pre-feature behavior exactly, so a client that never sends a
    view message is byte-for-byte unchanged."""

    want: bool = True
    need: int | None = None  # requested longest source edge in px; None = baseline
    full: bool = False  # focused tile may request finer than the baseline
    crop: tuple[int, int, int, int] | None = None  # (x, y, w, h) in sensor px


_DEFAULT_VIEW = _ViewSpec()


def _parse_crop(crop) -> tuple[int, int, int, int] | None:
    """Parse a {"x","y","w","h"} crop dict into an int tuple, or None when
    absent/malformed. Width/height must be positive and the origin non-negative;
    the rect is clamped to the live sensor size later by _clamp_crop."""
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
    """Clamp a requested crop to the sensor, returning the whole sensor when
    there is no crop. numpy would silently clip an out-of-range slice, so pin
    the rect here and report the clamped geometry to the client (in the frame
    header) — the client positions the crop from what was actually sent, never
    from what it asked for."""
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
    """Integer decimation factor for the region being encoded (the whole sensor,
    or a crop of it) given a client's view spec.

    A default/legacy spec (no ``need``) returns today's baseline factor,
    ``ceil(sensor_long / PREVIEW_MAX_DIM)``, so unspecified clients are
    unchanged. A normal tile may only go *coarser* than the baseline (a small
    tile sends less data); a focused tile may go *finer*, down to 1:1, bounded
    to ``PREVIEW_FOCUS_MAX_DIM_RECORDING`` while recording. ``region_long`` is
    the crop's long edge when cropping, else the sensor's."""
    sensor_long = max(sensor_long, 1)
    region_long = max(region_long, 1)
    baseline = max(1, math.ceil(sensor_long / PREVIEW_MAX_DIM))
    if spec.need is None:
        return baseline
    need = max(1, spec.need)
    if not spec.full:
        # Unfocused tile (never cropped): honor a smaller need (coarser
        # preview), but never sharper than today's baseline — this keeps a
        # single HiDPI (devicePixelRatio > 1) client from silently upgrading
        # every camera.
        return max(baseline, max(1, round(region_long / need)))
    # Focused tile: may exceed the baseline, down to 1:1 on the region. round()
    # matches the requested resolution while biasing toward full detail (send a
    # little extra rather than a little soft).
    need = min(need, region_long)  # never finer than the pixels that exist
    factor = max(1, round(region_long / need))
    if recording:
        # A *hard* resolution ceiling while recording, so a large preview encode
        # can't starve the writer's CPU — ceil (a true upper bound) rather than
        # the need-matching round above, which could leave the effective
        # resolution up to ~1.5x over the cap for mid-band region sizes.
        factor = max(factor, math.ceil(region_long / PREVIEW_FOCUS_MAX_DIM_RECORDING))
    return factor


class SettingsPatch(BaseModel):
    """Partial update for RecordingSettings; unknown keys are rejected (422).

    Cross-field rules (fps > 0, known save_method, …) stay in
    RecordingController.update_settings — only the fields actually sent are
    forwarded, via model_dump(exclude_unset=True). Editing record_directory or
    relative_directory recomposes save_dir server-side (the two halves of the
    split save path). The transcode_*/transfer_* fields are the Process
    section's post-recording knobs, baked into each recording's config snapshot
    for `octacam process`."""

    model_config = ConfigDict(extra="forbid")

    fps: float | None = None
    duration_s: float | None = None
    save_dir: str | None = None
    record_directory: str | None = None
    relative_directory: str | None = None
    trigger_source: str | None = None
    save_method: str | None = None
    ffmpeg_params: str | None = None
    record_form: str | None = None
    save_frame_timestamps: bool | None = None
    transcode_ffmpeg_params: str | None = None
    transfer_directory: str | None = None
    transfer_checksum: bool | None = None


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


class CameraFeaturePatch(BaseModel):
    """Set one full-node-map feature by GenApi node name on one camera or all.

    ``value`` is untyped (JSON number/bool/string): the backend coerces it to
    the node's GenApi type, so an enum sends its symbolic string, a bool sends
    ``true``/``false``, and int/float send a number."""

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
    center_x: bool = False
    center_y: bool = False

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
        # state (e.g. the flywheel jog) to the socket that owns it.
        self.id = next(_Client._next_id)
        self.frames: dict[int, bytes] = {}
        self.texts: dict[str, str] = {}
        self.events: deque[str] = deque(maxlen=50)  # events are not dropped
        self.wakeup = asyncio.Event()
        # Per-camera-index display request (resolution / paused). Absent => the
        # default spec, which reproduces the pre-feature preview exactly. Only
        # read/written on the event-loop thread (receive loop + preview loop),
        # so it needs no lock.
        self.views: dict[int, _ViewSpec] = {}

    def view_for(self, camera_index: int) -> _ViewSpec:
        return self.views.get(camera_index, _DEFAULT_VIEW)

    def apply_view(self, message: dict) -> None:
        """Update per-camera view specs from a ``{"type": "view", ...}`` message.

        Tolerant of missing/garbage fields so a malformed message can never
        raise inside the receive loop and tear down the socket; a bad per-camera
        entry is simply skipped."""
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
        """True when this client has no preview frame still waiting to be sent
        for ``camera_index``.

        The sender swaps the whole pending-frame dict out atomically before
        draining it, so an entry lingers here only while a frame is queued but
        not yet handed to the socket. A slow client (or a stalled ssh -L tunnel)
        leaves the previous frame pending; encoding another would just overwrite
        it (newest-only) and waste CPU. The preview loop uses this to pace
        encoding to what clients can actually consume."""
        return camera_index not in self.frames

    async def sender(self) -> None:
        # A client can vanish mid-send (browser tab closed, SSH tunnel
        # dropped) or the socket can be closed from under us on server
        # shutdown. A client-initiated disconnect surfaces as
        # WebSocketDisconnect, but a send issued after the connection has
        # already closed (e.g. uvicorn sent the close frame) surfaces from
        # the ASGI layer as a bare RuntimeError ("Unexpected ASGI message
        # 'websocket.send', after sending 'websocket.close'..."). Swallow
        # both so the task ends cleanly instead of dying with an exception
        # that the endpoint's teardown `await sender` would re-raise and
        # crash the ASGI app with.
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            while True:
                await self.wakeup.wait()
                self.wakeup.clear()
                # Don't even attempt a doomed send if the peer is gone; the
                # RuntimeError suppression above is only the backstop for the
                # narrow race where the socket closes mid-batch.
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
            clients = list(self.clients)
            if not clients:
                continue
            recording = self.controller.recording_active
            # Per camera, group the clients that are both drained (newest-only
            # backpressure, is_ready_for) AND still want it by the (crop region,
            # decimation factor) each one needs. Each distinct variant is encoded
            # once and shared by every client asking for it, so encode cost
            # tracks the number of *distinct views* on screen, not the number of
            # clients. A camera no ready client wants (hidden behind someone's
            # maximized tile) is skipped entirely — no grab, no encode. A slow or
            # stalled ssh -L tunnel still can't make the rig burn CPU on previews
            # nobody is keeping up with.
            jobs = []  # (index, camera, frame, {(region, factor): [clients]})
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
                jobs.append((index, camera, frame, groups))
            if not jobs:
                continue
            messages = await loop.run_in_executor(
                None, self._encode_jobs, jobs, recording
            )
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
        """Bound distinct (region, factor) variants of one camera to the safety
        cap. Keeps the cheapest few and demotes the rest to the shared
        full-frame baseline variant — a correct whole-sensor image for those
        clients (their client-side zoom still magnifies it), just without the
        server-cropped detail. Two different crops are never merged into one
        encode (that would show a client the wrong region); demotion only ever
        widens a crop back to the full frame."""
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

    def _encode_jobs(
        self, jobs, recording: bool
    ) -> list[tuple[int, bytes, list["_Client"]]]:
        import cv2

        flags = 1 if recording else 0
        messages = []
        for index, camera, frame, groups in jobs:
            # One monotonic frame number and one telemetry snapshot per camera
            # per tick, shared across that camera's variants.
            frame_h, frame_w = frame.shape
            count = self._frame_counters.get(index, 0) + 1
            self._frame_counters[index] = count
            timestamp = time.time_ns()
            fps = camera.resulting_fps
            dropped = camera.dropped_count
            for (region, factor), group in groups.items():
                # Re-clamp against the frame actually popped (it may differ from
                # camera.width/height for one frame across a live geometry
                # change); numpy would silently clip otherwise.
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

    # Give plugins that support real-time WS push a broadcast callback.
    # Checked by duck-typing so core stays decoupled from concrete plugin classes.
    for plugin in plugins.plugins:
        if hasattr(plugin, "set_broadcast"):
            plugin.set_broadcast(state.broadcast_threadsafe)

    # Plugins may ship their own static web assets (JS/CSS) co-located with
    # their Python. Resolve them once so the asset mount (below) and the
    # /api/system descriptor (in get_system) can never disagree about which
    # plugins serve a UI. The name lands in a URL/mount path, so it is
    # validated; a missing dir or a raising hook just means "no UI".
    plugin_web: dict[str, Path] = {}
    for plugin in plugins.plugins:
        try:
            adir = plugin.web_assets() if hasattr(plugin, "web_assets") else None
        except Exception:
            log.exception("Plugin %s web_assets() failed", getattr(plugin, "name", "?"))
            adir = None
        if adir is None:
            continue
        name = plugin.name
        if not _PLUGIN_NAME_RE.match(name):
            log.warning("Plugin %r: name is not URL-safe; not serving its assets", name)
            continue
        adir = Path(adir)
        if not adir.is_dir():
            log.warning(
                "Plugin %r: web_assets dir %s does not exist; skipping", name, adir
            )
            continue
        plugin_web[name] = adir

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
                    # Live ROI-centering state, so the save dialog can persist it
                    # for a camera whose Camera tab was never opened this session.
                    "center_x": camera.center_x,
                    "center_y": camera.center_y,
                }
            )
        # Tell the SPA which plugins ship a UI bundle (and where), so app.js can
        # dynamically import each plugin's <name>.js from its own folder instead
        # of statically importing every plugin by name. Nested under the
        # existing per-plugin object, so the top-level shape is unchanged.
        plugins_status = state.plugins.status()
        for name, adir in plugin_web.items():
            entry = plugins_status.get(name)
            if entry is None:
                continue
            web = {"module": f"/plugins/{name}/{name}.js"}
            if (adir / f"{name}.css").is_file():
                web["css"] = f"/plugins/{name}/{name}.css"
            entry["web"] = web
        return {
            "version": octacam.__version__,
            "config_dir": config_dir,
            "plugins": plugins_status,
            "display_refresh_interval_ms": (
                state.config.gui.display_refresh_interval_ms
            ),
            "formats": [
                {"save_method": save_method, "label": video_format.label}
                for save_method, video_format in FORMATS.items()
            ],
            "cameras": cameras,
        }

    @app.get("/api/serial/ports")
    def get_serial_ports():
        """Detected serial ports, for the plugin tabs' port picker.

        Enumeration only (never opens a port), so it is safe to call while a
        board is armed. Microcontroller-class ports are flagged so the UI can
        surface the plausible Arduino candidates first."""
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
            config_dir, controller.camera_system.extensions
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

    # ---------------------------------------------- full device node map (tab)

    def _broadcast_features_dirty(result: dict) -> None:
        """Ping every client that a camera's feature list changed so it refetches.

        The list is large and a change can touch many nodes, so instead of
        broadcasting the whole list the server nudges clients to re-GET
        ``/features`` for that camera (only if they are showing it)."""
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
        try:
            result = controller.set_camera_feature(
                index, patch.name, patch.value, patch.scope
            )
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        _broadcast_features_dirty(result)
        return result

    @app.post("/api/cameras/{index}/features/reset")
    def reset_camera_feature(index: int, payload: CameraFeatureReset):
        if not config_dir:
            raise HTTPException(400, "No config directory is set for this session")
        pfs_by_serial = config_writer.read_pfs_files(
            config_dir, controller.camera_system.extensions
        )
        try:
            result = controller.reset_camera_feature(
                index, payload.name, pfs_by_serial, payload.scope
            )
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        _broadcast_features_dirty(result)
        return result

    @app.post("/api/cameras/{index}/commands")
    def run_camera_command(index: int, payload: CameraCommandRequest):
        try:
            result = controller.execute_camera_command(index, payload.name)
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        _broadcast_features_dirty(result)
        return result

    @app.put("/api/cameras/{index}/center")
    def put_camera_center(index: int, patch: CameraCenterPatch):
        try:
            result = controller.set_camera_center(
                index, patch.axis, patch.enabled, patch.scope
            )
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e)) from None
        _broadcast_features_dirty(result)
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

    @app.post("/api/diagnostics/run")
    def run_diagnostic(payload: DiagnosticRunRequest | None = None):
        # A benchmark pauses preview and drives the cameras for a few seconds, then
        # broadcasts its report (WS "diagnostics") and resumes preview. Returns 202
        # immediately; 409 if a recording/benchmark/reconfigure is already active.
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

    @app.get("/api/diagnostics/last")
    def last_diagnostic():
        return controller.get_last_diagnostic() or {}

    @app.post("/api/shutdown")
    def shutdown(background_tasks: BackgroundTasks):
        # Shutting down releases the cameras for everyone, so refuse while a
        # recording is in progress rather than discarding it (controller.close
        # aborts). The background task runs after the 202 is flushed, so the
        # client always learns the request was accepted before the server dies.
        if controller.recording_active or controller.diagnosing:
            raise HTTPException(
                409,
                "Stop the recording or benchmark before shutting down the server",
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
            # Replay the last benchmark report so a (re)connecting browser shows
            # it immediately instead of a blank Benchmark tab.
            last_diag = controller.get_last_diagnostic()
            if last_diag:
                client.queue_text(
                    "diagnostics", json.dumps({"type": "diagnostics", **last_diag})
                )
            while True:
                text = await ws.receive_text()
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                # A "view" message is core-owned (per-camera preview resolution /
                # pause). Handle it inline on the event-loop thread — it is cheap
                # in-memory dict work, unlike plugin hooks which may block on I/O
                # — and don't pass it to plugins.
                if isinstance(message, dict) and message.get("type") == "view":
                    client.apply_view(message)
                    continue
                # Hand the message to plugins (e.g. flywheel jog); the first
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
            # Let plugins react to this socket closing (e.g. the flywheel jog
            # clock stops if this client owned it, so a dropped connection
            # can't leave the motor spinning — but another client's jog is
            # left untouched). Off the event loop in case the hook blocks.
            await loop.run_in_executor(
                None, state.plugins.dispatch, "on_ws_disconnect", client.id
            )
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    # Plugin-contributed REST endpoints (e.g. flywheel's /api/serial/command).
    # Registered before the static catch-all mount at "/".
    for plugin in plugins.plugins:
        router = plugin.api_router()
        if router is not None:
            app.include_router(router)

    # Per-plugin static assets at /plugins/<name>/. MUST be mounted before the
    # "/" catch-all below: it has html=True (SPA fallback), so a /plugins/ path
    # reaching it would return index.html (200, text/html) and the browser
    # would refuse to run it as a module. No html=True here — a missing plugin
    # asset must 404, not silently fall through to the SPA.
    for name, adir in plugin_web.items():
        app.mount(
            f"/plugins/{name}",
            StaticFiles(directory=adir),
            name=f"plugin-{name}",
        )

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
