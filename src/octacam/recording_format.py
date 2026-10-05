"""A recording on disk: its layout, how recordings are found, and the summary and
timestamps schema.

A recording folder holds its videos; everything else (summary, timestamps,
config snapshot, camera parameter files) goes in its ``octacam_recording``
subfolder. Older recordings keep those flat beside the videos and are read
forever: every reader goes through :func:`recording_info_dir`.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from octacam.ffmpeg import encoder_of, nvenc_max_sessions

if TYPE_CHECKING:
    from octacam.config import RecordingSettings
    from octacam.pulses import PulseClock

RECORDING_SUMMARY_FILENAME = "recording_summary.json"
# Every camera's per-frame series (opt-in: record.save_timestamps).
TIMESTAMPS_FILENAME = "timestamps.npz"
# The rig config with the live settings, beside each camera's parameter file
# (``<serial>.<ext>``): a config directory a session can relaunch from.
CONFIG_SNAPSHOT_FILENAME = "octacam_config.toml"
# Every backend's parameter-file suffix (``CameraBackend.extension``), here so
# the transfer step needs no SDK import; tests/test_backends.py keeps it in step.
PARAM_FILE_EXTENSIONS = ("pfs", "txt", "fake")
RECORDING_INFO_DIRNAME = "octacam_recording"

SCHEMA_VERSION = 4
# Summary lists of per-pulse indices are capped at this length (the full series
# is in timestamps.npz); the counts next to them are never capped.
SUMMARY_INDEX_LIMIT = 1000


# ------------------------------------------------------------------ layout


def recording_info_dir(folder: str | Path) -> Path:
    """The directory holding *folder*'s metadata: the ``octacam_recording``
    subfolder, or *folder* for an older flat recording.

    The summary decides, and the subfolder's wins over a flat one (an older take
    in the same folder). Without a summary, the subfolder if it exists.
    """
    folder = Path(folder)
    nested = folder / RECORDING_INFO_DIRNAME
    if (nested / RECORDING_SUMMARY_FILENAME).is_file():
        return nested
    if (folder / RECORDING_SUMMARY_FILENAME).is_file():
        return folder
    return nested if nested.is_dir() else folder


def recording_summary_path(folder: str | Path) -> Path:
    """Where the recording in *folder* keeps its summary (either layout)."""
    return recording_info_dir(folder) / RECORDING_SUMMARY_FILENAME


def is_recording_dir(folder: str | Path) -> bool:
    """Whether *folder* has a summary (either layout); an ``octacam_recording``
    subfolder is never a recording folder itself."""
    folder = Path(folder)
    if folder.name == RECORDING_INFO_DIRNAME:
        return False
    return recording_summary_path(folder).is_file()


def recording_folder(path: str | Path) -> Path:
    """The recording folder *path* names: a summary file or an
    ``octacam_recording`` subfolder stands for the folder around it, anything
    else for itself (pure path arithmetic)."""
    path = Path(path)
    if path.name == RECORDING_SUMMARY_FILENAME:
        path = path.parent
    return path.parent if path.name == RECORDING_INFO_DIRNAME else path


def walk_folders(root: Path) -> list[Path]:
    """*root* and every directory beneath it, sorted, never entering an
    ``octacam_recording`` subfolder (metadata: never a recording, never loose
    videos) or a symlinked directory."""
    found = []
    for directory, subdirs, _files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d != RECORDING_INFO_DIRNAME]
        found.append(Path(directory))
    return sorted(found)


def find_recordings(paths, recursive: bool) -> list[Path]:
    """The recording folders *paths* name (see :func:`recording_folder`), and
    with *recursive* every one beneath them; deduplicated in the order found."""
    found: dict[Path, Path] = {}
    for raw in paths:
        folder = recording_folder(raw)
        candidates = walk_folders(folder) if recursive else [folder]
        for candidate in candidates:
            if is_recording_dir(candidate):
                found.setdefault(candidate.resolve(), candidate)
    return list(found.values())


# ----------------------------------------------------------------- summary


def read_summary(folder: str | Path) -> dict:
    """*folder*'s summary (either layout). Raises OSError when it cannot be read
    and ValueError when it is not a JSON object."""
    summary = json.loads(recording_summary_path(folder).read_text())
    if not isinstance(summary, dict):
        raise ValueError("not a JSON object")
    return summary


def write_summary(folder: str | Path, summary: dict) -> Path:
    """Write *summary* into *folder*'s ``octacam_recording`` subfolder (writers
    always use it); return its path."""
    path = Path(folder) / RECORDING_INFO_DIRNAME / RECORDING_SUMMARY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2) + "\n")
    return path


def write_timestamps(folder: str | Path, arrays: dict[str, np.ndarray]) -> Path:
    """Write :func:`build_timestamps_arrays` output into *folder*'s
    ``octacam_recording`` subfolder; return its path."""
    path = Path(folder) / RECORDING_INFO_DIRNAME / TIMESTAMPS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **arrays)
    return path


_DROPPED_FRAMES_NOTE = (
    "Every frame is assigned to the trigger pulse that exposed it, from the "
    "camera's hardware timestamps (or, under a software trigger, the trigger's "
    "sequence number). `missed_pulses` lists pulses the camera delivered no frame "
    "for (a missed trigger, or a frame lost in transport — `stream` shows the "
    "SDK's own loss counters) and `writer_dropped` counts frames the writer queue "
    "could not accept. On an octacam-driven train (software/managed) both are "
    "filled with the previous frame, so video frame k is pulse k in every camera "
    "and `dropped`/`dropped_indices` count/list those filled frames; "
    "timestamps.npz marks them per frame (`dropped`, `missed`) with each frame's "
    "`pulse_index`. On an external trigger missed pulses are reported only. "
    "`writer_skipped` counts frames the writer could not accept that were skipped "
    "instead of filled (a sustained encoder or disk shortfall): after one, video "
    "frame k is no longer pulse k, so map frames to pulses with `pulse_index`. "
    "`late_pulse_indices` are frames exposed markedly after their pulse."
)

_TIMESTAMP_NOTE = (
    "Per-camera `timestamp_source` records where each camera's per-frame "
    "timestamps came from. `hardware` = the camera/SDK timestamp (a free-running "
    "counter with a per-camera epoch — precise for relative timing, but NOT "
    "wall-clock and NOT aligned across cameras). `host` = host `time.time_ns()` "
    "(UTC wall clock; used when the backend supplies none). The full per-frame "
    f"series is written to {TIMESTAMPS_FILENAME} when save_timestamps is on."
)


def _timestamp_source(frames: int, host_fallback_count: int) -> str | None:
    """``"hardware"``, ``"host"`` (a backend without timestamps, e.g.
    pycameleon) or ``"mixed"`` (stray zero timestamps); None without frames."""
    if frames <= 0:
        return None
    if host_fallback_count <= 0:
        return "hardware"
    if host_fallback_count >= frames:
        return "host"
    return "mixed"


def _capped(indices: list[int]) -> list[int]:
    return list(indices[:SUMMARY_INDEX_LIMIT])


def build_recording_summary(
    settings: RecordingSettings,
    cameras,
    start_wall_ns: int,
    aborted: bool,
    pulse_clock: PulseClock | None = None,
    sync: dict | None = None,
    primed_pulses: int = 0,
    completed: bool | None = None,
) -> dict:
    """The recording_summary.json payload, from finished camera stats (no I/O).

    ``transform_applied`` is true only when the transform was baked into the
    video (display form and a non-identity transform)."""
    extension = settings.video_format().extension
    start_iso = (
        datetime.datetime.fromtimestamp(
            start_wall_ns / 1e9, tz=datetime.timezone.utc
        ).isoformat()
        if start_wall_ns
        else None
    )
    cams = []
    for camera in cameras:
        transform = camera.display_transform
        applied = settings.record_form == "display" and not transform.is_identity
        size = camera.recorded_frame_size
        missed, late = camera.missed_pulses, camera.late_pulses
        cams.append(
            {
                "name": camera.name,
                "serial": camera.serial_number,
                "file": f"{camera.name}.{extension}",
                "width": size[0] if size else None,
                "height": size[1] if size else None,
                "pixel_format": camera.pixel_format,
                "fps": round(camera.mean_fps, 3),
                "frames": camera.frames_recorded,
                "dropped": camera.dropped_count,
                "dropped_indices": _capped(camera.dropped_indices),
                "missed_pulses": len(missed),
                "missed_pulse_indices": _capped(missed),
                "writer_dropped": camera.writer_dropped,
                "writer_skipped": camera.writer_skipped,
                "writer_skipped_pulse_indices": _capped(camera.writer_skipped_pulses),
                "late_frames": len(late),
                "late_pulse_indices": _capped(late),
                "extra_frames": camera.extra_frames,
                "primed_frames": camera.primed_frames,
                "clock_mismatch": camera.clock_mismatch,
                "timestamp_glitches": camera.timestamp_glitches,
                "unclocked_frames": camera.unclocked_frames,
                "stream": camera.stream_statistics,
                "start_offset_pulses": (sync or {}).get("start_offsets", {}).get(
                    camera.name
                ),
                "start_timestamp_ns": camera.start_timestamp_ns,
                "timestamp_source": _timestamp_source(
                    camera.frames_recorded, camera.host_fallback_count
                ),
                "host_fallback_count": camera.host_fallback_count,
                "writer_failed": camera.writer_failed,
                "transform": transform.to_dict(),
                "transform_applied": applied,
            }
        )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "start_time": start_iso,
        "start_time_ns": start_wall_ns or None,
        "aborted": aborted,
        # Ran to its end, not stopped: octacam check tells an early stop from
        # cameras that disagree by it.
        "completed": completed,
        "fps_target": settings.fps,
        "duration_s": settings.duration_s,
        "trigger_source": settings.trigger_source,
        "save_method": settings.save_method,
        # The args of the encoder used (nvenc's for save_method="nvenc").
        "ffmpeg_params": settings.video_format().ffmpeg_params,
        "record_form": settings.record_form,
        # Resolved now, so a transfer on a later day never re-templates the date.
        "relative_directory": settings.relative_save_dir(),
        "pulse_train": (
            {**pulse_clock.to_dict(), "primed": primed_pulses}
            if pulse_clock is not None
            else None
        ),
        "sync": {
            "ok": (sync or {}).get("ok", True),
            "warnings": list((sync or {}).get("warnings", [])),
            "notes": list((sync or {}).get("notes", [])),
        },
        "dropped_frames_note": _DROPPED_FRAMES_NOTE,
        "timestamp_note": _TIMESTAMP_NOTE,
        "cameras": cams,
    }
    if settings.save_method == "nvenc":
        # Cameras beyond the cap encoded on CPU. Auto (None) resolves to the cap
        # the off-lock warm-up cached for this encoder: never a fresh GPU probe.
        cap = settings.max_nvenc_sessions
        if cap is None:
            encoder = encoder_of(settings.video_format().ffmpeg_params) or "h264_nvenc"
            cap = nvenc_max_sessions(encoder)
        summary["max_nvenc_sessions"] = cap
    return summary


def build_timestamps_arrays(cameras) -> dict[str, np.ndarray]:
    """The ``timestamps.npz`` arrays, from finished camera stats (no I/O).

    Per camera, one entry per video frame: ``"<name>/timestamp_ns"`` (int64; a
    fill carries the time its pulse was due), ``"<name>/dropped"`` (bool: a
    fill), ``"<name>/missed"`` (bool: of those, a pulse the camera never
    delivered), ``"<name>/pulse_index"`` (int64) and ``"<name>/arrival_ns"``
    (int64: host wall-clock delivery, 0 for a fill). A camera's series are
    truncated to their shortest."""
    arrays: dict[str, np.ndarray] = {}
    for camera in cameras:
        series = {
            "timestamp_ns": (camera.frame_timestamps, np.int64),
            "dropped": (camera.frame_dropped, bool),
            "missed": (camera.frame_missed, bool),
            "pulse_index": (camera.frame_pulse_index, np.int64),
            "arrival_ns": (camera.frame_arrival_ns, np.int64),
        }
        n = min(len(values) for values, _dtype in series.values())
        for key, (values, dtype) in series.items():
            arrays[f"{camera.name}/{key}"] = np.asarray(values[:n], dtype=dtype)
    return arrays
