"""In-memory fake camera backend (no SDK, for tests and CI).

Implements the :class:`CameraBackend` seam with an in-memory node table and
synthetic mono ``uint8`` frames, so the backend-selection layer, the persistence
generalization, and the shared controller/web logic can be exercised without any
camera hardware or vendor SDK (PySpin has no software emulator like Basler's
``PYLON_CAMEMU``). Frames are software-trigger driven, exactly like the rig: a
``retrieve`` returns an image only once ``trigger_once`` has fired, so the same
PreciseTimer that drives the real cameras drives the fake.

``enumerate_fake`` reads serials from ``OCTACAM_FAKE_CAMERAS`` (default
``"FAKE-0,FAKE-1"``), mirroring how ``PYLON_CAMEMU=N`` summons emulated Basler
cameras.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable

import numpy as np

from octacam.cameras.base import BackendError, Frame, NodeInfo

log = logging.getLogger("octacam")

FAKE_CAMERAS_ENV = "OCTACAM_FAKE_CAMERAS"
_DEFAULT_SERIALS = "FAKE-0,FAKE-1"


def _default_nodes() -> dict[str, dict]:
    """A fresh editable-parameter table with realistic bounds and types.

    Width/Height/OffsetX/OffsetY are integers (so the shared int-snapping path
    is exercised); ExposureTime/Gain are floats.
    """
    return {
        "width": {"value": 1920, "min": 16, "max": 1920, "inc": 16, "unit": "px"},
        "height": {"value": 1200, "min": 16, "max": 1200, "inc": 16, "unit": "px"},
        "exposure": {
            "value": 5000.0,
            "min": 20.0,
            "max": 1_000_000.0,
            "inc": 1.0,
            "unit": "us",
        },
        "gain": {"value": 0.0, "min": 0.0, "max": 24.0, "inc": 0.1, "unit": "dB"},
        "offset_x": {"value": 0, "min": 0, "max": 1904, "inc": 4, "unit": "px"},
        "offset_y": {"value": 0, "min": 0, "max": 1184, "inc": 2, "unit": "px"},
    }


class FakeBackend:
    """A single in-memory camera driven by software triggers."""

    extension = "fake"

    def __init__(self, serial: str):
        self._serial = serial
        self._open = False
        self._grabbing = False
        self._nodes = _default_nodes()
        self._frame_index = 0
        self._pending = 0
        self._cond = threading.Condition()
        self._original_trigger_source = "Line1"

    @property
    def serial_number(self) -> str:
        return self._serial

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self.stop_grab()
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def is_grabbing(self) -> bool:
        return self._grabbing

    def width(self) -> int:
        return int(self._nodes["width"]["value"])

    def height(self) -> int:
        return int(self._nodes["height"]["value"])

    # ----------------------------------------------------- sensor parameters

    def read_node(self, name: str) -> NodeInfo:
        try:
            node = self._nodes[name]
        except KeyError as e:
            raise BackendError(f"unknown node: {name}") from e
        return NodeInfo(
            value=node["value"],
            min=node["min"],
            max=node["max"],
            inc=node["inc"],
            unit=node["unit"],
            writable=self._open,
        )

    def write_node(self, name: str, value: float) -> None:
        if name not in self._nodes:
            raise BackendError(f"unknown node: {name}")
        self._nodes[name]["value"] = value

    def load_params(self, config_str: str) -> None:
        if not config_str:
            return
        try:
            data = json.loads(config_str)
        except (ValueError, TypeError) as e:
            raise BackendError(f"invalid fake parameters: {e}") from e
        if not isinstance(data, dict):
            raise BackendError("invalid fake parameters: expected an object")
        for name, value in (data.get("params") or {}).items():
            if name in self._nodes:
                self._nodes[name]["value"] = value
        self._original_trigger_source = data.get(
            "trigger_source", self._original_trigger_source
        )

    def save_params(self) -> str:
        # Mirror the Basler normalization: a saved snapshot ships with the
        # FrameStart trigger Off and the originally-loaded source, not the
        # live-preview Software override.
        data = {
            "params": {name: node["value"] for name, node in self._nodes.items()},
            "trigger_mode": "Off",
            "trigger_source": self._original_trigger_source,
        }
        return json.dumps(data, indent=2) + "\n"

    # ----------------------------------------------------------- triggering

    def enable_frame_trigger(self) -> None:
        pass  # the fake is always software-trigger ready

    def set_trigger_source(self, use_software: bool) -> None:
        pass

    def begin_software_trigger_preview(self) -> None:
        pass

    def trigger_once(self) -> None:
        with self._cond:
            if self._grabbing:
                self._pending += 1
                self._cond.notify()

    # ------------------------------------------------------------- grabbing

    def start_grab_preview(self) -> None:
        with self._cond:
            self._pending = 0
            self._grabbing = True

    def start_grab_record(self) -> bool:
        self.start_grab_preview()
        return True

    def stop_grab(self) -> None:
        with self._cond:
            self._grabbing = False
            self._cond.notify_all()

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        with self._cond:
            if self._pending <= 0 and self._grabbing:
                self._cond.wait(timeout_ms / 1000.0)
            if self._pending <= 0 or not self._grabbing:
                return None
            self._pending -= 1
            self._frame_index += 1
            index = self._frame_index
            width = int(self._nodes["width"]["value"])
            height = int(self._nodes["height"]["value"])
        array = _render(width, height, index) if wants_array() else None
        return (array, time.time_ns())


def _render(width: int, height: int, index: int) -> np.ndarray:
    """A cheap, owned mono frame whose content advances with the frame index."""
    return np.full((height, width), index % 256, dtype=np.uint8)


def _available_serials() -> list[str]:
    raw = os.environ.get(FAKE_CAMERAS_ENV, _DEFAULT_SERIALS)
    return [s.strip() for s in raw.split(",") if s.strip()]


def enumerate_fake(requested_serials: list[str] | None = None):
    """Return ``[(serial, serial), ...]`` for the configured fake cameras.

    Mirrors :func:`octacam.cameras.basler.enumerate_basler`: with no requested
    serials, every available fake camera is returned (sorted); otherwise the
    listed serials are returned in order, warning about any not available. The
    handle is just the serial string (the FakeBackend needs nothing more).
    """
    available = _available_serials()
    if not available:
        return []
    log.info("Detected %d camera(s)", len(available))
    final = sorted(available) if not requested_serials else list(requested_serials)
    out = []
    for serial in final:
        if serial not in available:
            log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, serial))
    return out
