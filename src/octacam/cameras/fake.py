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

import logging
import os
import time
from collections.abc import Callable

import numpy as np

from octacam.cameras._genicam_config import apply_config, dump_config
from octacam.cameras._trigger_handoff import SoftwareTriggerHandoff
from octacam.cameras.base import PARAM_NODES, BackendError, Frame, NodeInfo

log = logging.getLogger("octacam")

FAKE_CAMERAS_ENV = "OCTACAM_FAKE_CAMERAS"
_DEFAULT_SERIALS = "FAKE-0,FAKE-1"

# The fake speaks snake_case params internally; the native TSV config uses SFNC
# node names, so map the six persisted nodes back (Width -> width, ...). Nodes
# the fake does not model are simply skipped by the config applier/serialiser.
_INT_PARAMS = frozenset({"width", "height", "offset_x", "offset_y"})
_SFNC_TO_PARAM = {sfnc: snake for snake, sfnc in PARAM_NODES.items()}


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


class FakeBackend(SoftwareTriggerHandoff):
    """A single in-memory camera driven by software triggers."""

    extension = "fake"

    def __init__(self, serial: str):
        self._serial = serial
        self._open = False
        self._nodes = _default_nodes()
        self._frame_index = 0
        self._init_trigger_handoff()
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

    # Typed-setter seam used by the native-TSV config applier (_genicam_config).
    # The fake models only the six PARAM_NODES (as floats/ints) plus a stored
    # TriggerSource; every other node raises/returns-None so the applier and
    # serialiser skip it, exactly as a real camera skips a node it lacks.
    def _set_enum(self, name: str, value: str) -> None:
        if name == "TriggerSource":
            self._original_trigger_source = value
            return
        raise BackendError(f"fake has no enumeration {name}")

    def _get_enum(self, name: str) -> str | None:
        return self._original_trigger_source if name == "TriggerSource" else None

    def _set_bool(self, name: str, value: bool) -> None:
        raise BackendError(f"fake has no boolean {name}")

    def _get_bool(self, name: str) -> bool | None:
        return None

    def _set_number(self, name: str, value: float, is_int: bool) -> None:
        param = _SFNC_TO_PARAM.get(name)
        if param is None or param not in self._nodes:
            raise BackendError(f"fake has no node {name}")
        self._nodes[param]["value"] = int(value) if is_int else float(value)

    def _get_number(self, name: str, is_int: bool) -> float | int | None:
        param = _SFNC_TO_PARAM.get(name)
        if param is None or param not in self._nodes:
            return None
        value = self._nodes[param]["value"]
        return int(value) if is_int else float(value)

    def load_params(self, config_str: str) -> None:
        if config_str:
            apply_config(self, config_str)

    def save_params(self) -> str:
        # A saved snapshot ships with the originally-loaded trigger source (via
        # _get_enum); dump_config emits only the nodes the fake models.
        return dump_config(self, "FakeCamera")

    # ----------------------------------------------------------- triggering

    def enable_frame_trigger(self) -> None:
        pass  # the fake is always software-trigger ready

    def set_trigger_source(self, use_software: bool) -> None:
        pass

    def begin_software_trigger_preview(self) -> None:
        pass

    def trigger_once(self) -> None:
        self._bump_trigger()

    def begin_freerun(self) -> bool:
        # The fake has no exposure pipeline, so free-run is simply "produce a
        # frame per fetch with no trigger" — always supported.
        return True

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        if not self._grabbing:
            return None
        with self._cond:
            self._frame_index += 1
            index = self._frame_index
            width = int(self._nodes["width"]["value"])
            height = int(self._nodes["height"]["value"])
        array = _render(width, height, index) if wants_array() else None
        return (array, time.time_ns())

    # ------------------------------------------------------------- grabbing

    def start_grab_preview(self) -> None:
        self._begin_grab()

    def start_grab_record(self) -> bool:
        self.start_grab_preview()
        return True

    def stop_grab(self) -> None:
        self._end_grab()

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        if not self._wait_pending(timeout_ms):
            return None
        with self._cond:
            self._frame_index += 1
            index = self._frame_index
            width = int(self._nodes["width"]["value"])
            height = int(self._nodes["height"]["value"])
        array = _render(width, height, index) if wants_array() else None
        return (array, time.time_ns())

    def retrieve_external(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        """External-trigger record fetch.

        The fake has no hardware buffer, so it models the *external* source with
        the same trigger counter as software mode: a frame arrives only once
        ``trigger_once`` has fired (the "external pulse"). This is exactly
        :meth:`retrieve` minus the (absent) device software trigger, and — unlike
        ``retrieve_freerun``, which fabricates a frame on every call — it lets an
        external recording with no pulses correctly yield nothing, so the
        controller's "wait for the first external frame" / "flag a zero-frame
        capture" paths stay exercised.
        """
        return self.retrieve(timeout_ms, wants_array)


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
    # Debug, not info: the auto cascade enumerates every tier, so CameraSystem
    # logs the single attributed "Detected N" summary (see basler backend).
    log.debug("fake enumerated %d camera(s)", len(available))
    final = sorted(available) if not requested_serials else list(requested_serials)
    out = []
    for serial in final:
        if serial not in available:
            log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, serial))
    return out
