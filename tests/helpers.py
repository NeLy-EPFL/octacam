"""Small utilities shared by the test modules."""

import time
from collections.abc import Callable

from octacam.cameras.take import CameraStats
from octacam.transform import DisplayTransform


def wait_until(
    predicate: Callable[[], object], timeout: float = 5.0, interval: float = 0.005
) -> bool:
    """Poll `predicate` until it is truthy; False if `timeout` seconds pass first."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def camera_stats(name: str = "cam0", *, frames: int = 0, **fields) -> CameraStats:
    """A full-field CameraStats: `frames` clean rows (row k is pulse k), every
    counter 0 and no fault, unless overridden.
    """
    defaults = {
        "name": name,
        "serial": name,
        "transform": DisplayTransform(),
        "frame_size": (320, 240),
        "pixel_format": "Mono8",
        "timestamp_ns": [1_000_000 * (k + 1) for k in range(frames)],
        "pulse_index": list(range(frames)),
        "dropped": [False] * frames,
        "missed": [False] * frames,
        "arrival_ns": [0] * frames,
        "missed_pulses": [],
        "late_pulses": [],
        "timestamp_glitches": [],
        "clock_mismatch": False,
        "writer_dropped": 0,
        "writer_skipped": [],
        "extra_frames": 0,
        "primed_frames": 0,
        "unclocked_frames": 0,
        "host_fallback_count": 0,
        "stream": {},
        "writer_failed": False,
    }
    return CameraStats(**{**defaults, **fields})


def plugin_doctor_text(config_dir) -> str:
    """The plugins section of `octacam doctor` for a rig config, as one line of
    text: the plugins' own checks, without the camera enumeration and serial
    probes the whole command runs (which depend on the machine's USB bus).
    """
    from pathlib import Path

    from octacam.cli.doctor import _doctor_plugins, _Report
    from octacam.config import load_config_dir

    report = _Report()
    _doctor_plugins(report, load_config_dir(Path(config_dir)), Path(config_dir))
    return " ".join(text for _title, items in report.sections for _s, text in items)
