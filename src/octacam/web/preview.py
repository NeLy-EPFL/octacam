"""Live preview over the GUI WebSocket: per-client view specs, and the loop that
encodes each camera's newest frame once per variant its ready clients need."""

import asyncio
import dataclasses
import math
import struct
import time
from typing import TYPE_CHECKING

import numpy as np

from octacam.web.hub import Client, Hub

if TYPE_CHECKING:
    from octacam.controller import RecordingController

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

Rect = tuple[int, int, int, int]  # x, y, w, h in sensor px
Variant = tuple[Rect, int]  # (region, decimation factor)


@dataclasses.dataclass(frozen=True)
class ViewSpec:
    """One client's need of one camera (the default: the baseline preview).
    ``want`` False skips it (hidden behind a maximized tile); ``need`` is the
    longest source edge it can show, in px; ``full`` marks a focused tile, which
    may exceed ``PREVIEW_MAX_DIM``; ``crop`` asks for that region only."""

    want: bool = True
    need: int | None = None
    full: bool = False
    crop: Rect | None = None


DEFAULT_VIEW = ViewSpec()


def parse_views(message: dict) -> dict[int, ViewSpec]:
    """The view specs a ``{"type": "view"}`` message sets, by camera index. A
    malformed entry is skipped, never raised: that would tear down the socket."""
    cameras = message.get("cameras")
    if not isinstance(cameras, dict):
        return {}
    views = {}
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
        views[index] = ViewSpec(
            want=bool(spec.get("want", True)),
            need=need,
            full=bool(spec.get("full", False)),
            crop=_parse_crop(spec.get("crop")),
        )
    return views


def _parse_crop(crop) -> Rect | None:
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


def _clamp_crop(crop: Rect | None, width: int, height: int) -> Rect:
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
    sensor_long: int, region_long: int, spec: ViewSpec, recording: bool
) -> int:
    """Integer decimation of the encoded region (the sensor, or a crop of it):
    without ``need`` the baseline; an unfocused tile only coarser; a focused one
    down to 1:1, capped while recording."""
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


def _cap_variants(
    groups: dict[Variant, list[Client]], width: int, height: int, sensor_long: int
) -> None:
    """Keep the cheapest variants and demote the rest to the whole-sensor
    baseline. Two different crops are never merged (a client would see the
    wrong region): demotion only widens a crop to the full frame."""
    if len(groups) <= MAX_PREVIEW_VARIANTS_PER_CAMERA:
        return

    def output_pixels(key: Variant) -> int:  # encode cost proxy
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


def _variants(
    clients: list[Client], index: int, width: int, height: int, recording: bool
) -> dict[Variant, list[Client]]:
    """The ready clients that want camera ``index``, grouped by the variant they
    need (each is encoded once and shared), capped per camera."""
    sensor_long = max(width, height)
    groups: dict[Variant, list[Client]] = {}
    for client in clients:
        if not client.is_ready_for(index):
            continue
        spec = client.views.get(index, DEFAULT_VIEW)
        if not spec.want:
            continue
        region = _clamp_crop(spec.crop, width, height)
        factor = _preview_factor(sensor_long, max(region[2], region[3]), spec, recording)
        groups.setdefault((region, factor), []).append(client)
    _cap_variants(groups, width, height, sensor_long)
    return groups


@dataclasses.dataclass(frozen=True)
class EncodeJob:
    """One camera's frame and everything its encode needs, taken on the event
    loop, so the executor thread touches no shared state or camera."""

    camera: int
    frame: np.ndarray
    groups: dict[Variant, list[Client]]
    number: int  # preview frame number
    timestamp_ns: int
    fps: float
    dropped: int
    recording: bool


def _encode_camera(job: EncodeJob) -> list[tuple[bytes, list[Client]]]:
    """Encode each variant of the job's frame; each message with its clients."""
    import cv2

    frame = job.frame
    frame_h, frame_w = frame.shape
    messages = []
    for (region, factor), group in job.groups.items():
        # The popped frame can differ from camera.width/height across a
        # geometry change, and numpy would clip silently.
        x, y, w, h = _clamp_crop(region, frame_w, frame_h)
        whole = (x, y, w, h) == (0, 0, frame_w, frame_h)
        if factor > 1 or not whole:
            sub = np.ascontiguousarray(frame[y : y + h : factor, x : x + w : factor])
        else:
            sub = np.ascontiguousarray(frame)
        ok, jpeg = cv2.imencode(".jpg", sub, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            continue
        header = FRAME_HEADER.pack(
            FRAME_VERSION, 1, job.camera, int(job.recording), job.number,
            job.timestamp_ns, job.fps, job.dropped, x, y, w, h, frame_w, frame_h,
        )
        messages.append((header + jpeg.tobytes(), group))
    return messages


async def preview_loop(
    hub: Hub, controller: "RecordingController", interval_s: float
) -> None:
    """Every ``interval_s``, send each camera's newest frame to the clients ready
    for it (a camera no ready client wants is neither popped nor encoded)."""
    loop = asyncio.get_running_loop()
    numbers: dict[int, int] = {}
    while True:
        await asyncio.sleep(interval_s)
        clients = list(hub.clients)
        if not clients:
            continue
        recording = controller.recording_active
        jobs = []
        for index, camera in enumerate(controller.camera_system):
            groups = _variants(clients, index, camera.width, camera.height, recording)
            if not groups:
                continue
            frame = camera.frame_for_display.pop()
            if frame is None:
                continue
            numbers[index] = numbers.get(index, 0) + 1
            jobs.append(EncodeJob(
                index, frame, groups, numbers[index], time.time_ns(),
                camera.resulting_fps, camera.dropped_count, recording,
            ))
        if not jobs:
            continue
        # One executor task per camera: cv2.imencode releases the GIL, so a tick
        # costs the slowest camera, not the sum (8 focused 2048²: 85 vs 14 ms).
        batches = await asyncio.gather(
            *[loop.run_in_executor(None, _encode_camera, job) for job in jobs]
        )
        for job, messages in zip(jobs, batches, strict=True):
            for message, group in messages:
                for client in group:
                    client.queue_frame(job.camera, message)
