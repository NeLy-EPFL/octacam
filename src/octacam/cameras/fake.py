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

from octacam.cameras._genicam_config import apply_config, dump_config, parse_config
from octacam.cameras._trigger_handoff import SoftwareTriggerHandoff
from octacam.cameras.base import (
    GEOMETRY_FEATURES,
    PARAM_NODES,
    BackendError,
    FeatureInfo,
    Frame,
    NodeInfo,
)

log = logging.getLogger("octacam")

FAKE_CAMERAS_ENV = "OCTACAM_FAKE_CAMERAS"
_DEFAULT_SERIALS = "FAKE-0,FAKE-1"

# Full sensor size the fake models; Width/Height ROI sits within it and the
# offset ranges (and centering) are derived from it, like real hardware.
_SENSOR_W, _SENSOR_H = 1920, 1200

# The fake's node model is keyed by SFNC name (matching real GenICam backends);
# the six legacy snake_case params (read_node/write_node/set_live_param) map onto
# it via PARAM_NODES. Nodes the fake does not model are skipped by the config
# applier/serialiser and never appear in the feature browser.
_PARAM_TO_SFNC = dict(PARAM_NODES)


def _default_nodes() -> dict[str, dict]:
    """A fresh SFNC-keyed node table spanning every widget kind.

    Each entry carries the descriptor fields the feature browser needs
    (display_name/type/category/bounds/entries/visibility). Int/float exercise
    the numeric-snap path; enum/bool/string/command exercise the other widgets.
    ``writable`` is False for read-only device info and the octacam-managed nodes
    (PixelFormat/TriggerMode/...), which the Camera core also locks.
    """

    def n(name, display, kind, category, value, **kw):
        e = {"name": name, "display_name": display, "type": kind,
             "category": category, "value": value, "writable": kw.pop("writable", True),
             "visibility": kw.pop("visibility", "beginner")}
        e.update(kw)
        return name, e

    nodes = dict([
        # --- ImageFormatControl ---
        n("Width", "Width", "int", "ImageFormatControl", _SENSOR_W,
          min=16, max=_SENSOR_W, inc=16, unit="px"),
        n("Height", "Height", "int", "ImageFormatControl", _SENSOR_H,
          min=16, max=_SENSOR_H, inc=16, unit="px"),
        n("OffsetX", "Offset X", "int", "ImageFormatControl", 0,
          min=0, max=_SENSOR_W - 16, inc=4, unit="px"),
        n("OffsetY", "Offset Y", "int", "ImageFormatControl", 0,
          min=0, max=_SENSOR_H - 16, inc=2, unit="px"),
        n("WidthMax", "Width Max", "int", "ImageFormatControl", _SENSOR_W,
          min=16, max=_SENSOR_W, inc=1, unit="px", writable=False, visibility="expert"),
        n("HeightMax", "Height Max", "int", "ImageFormatControl", _SENSOR_H,
          min=16, max=_SENSOR_H, inc=1, unit="px", writable=False, visibility="expert"),
        n("PixelFormat", "Pixel Format", "enum", "ImageFormatControl", "Mono8",
          entries=[{"value": v, "display": v, "available": True}
                   for v in ("Mono8", "Mono16")]),
        n("ReverseX", "Reverse X", "bool", "ImageFormatControl", False,
          visibility="expert"),
        n("TestPattern", "Test Pattern", "enum", "ImageFormatControl", "Off",
          entries=[{"value": v, "display": v, "available": True}
                   for v in ("Off", "GreyRamp", "ColorBars")]),
        # --- AcquisitionControl ---
        n("ExposureTime", "Exposure Time", "float", "AcquisitionControl", 5000.0,
          min=20.0, max=1_000_000.0, inc=1.0, unit="us"),
        n("ExposureAuto", "Exposure Auto", "enum", "AcquisitionControl", "Off",
          entries=[{"value": v, "display": v, "available": True}
                   for v in ("Off", "Once", "Continuous")]),
        n("AcquisitionFrameRate", "Acquisition Frame Rate", "float",
          "AcquisitionControl", 100.0, min=1.0, max=1000.0, inc=0.1, unit="Hz",
          visibility="expert"),
        n("TriggerMode", "Trigger Mode", "enum", "AcquisitionControl", "Off",
          entries=[{"value": v, "display": v, "available": True}
                   for v in ("Off", "On")]),
        n("TriggerSource", "Trigger Source", "enum", "AcquisitionControl", "Line1",
          entries=[{"value": v, "display": v, "available": True}
                   for v in ("Software", "Line0", "Line1")]),
        # --- AnalogControl ---
        n("Gain", "Gain", "float", "AnalogControl", 0.0,
          min=0.0, max=24.0, inc=0.1, unit="dB"),
        n("GainAuto", "Gain Auto", "enum", "AnalogControl", "Off",
          entries=[{"value": v, "display": v, "available": True}
                   for v in ("Off", "Once", "Continuous")]),
        n("BlackLevel", "Black Level", "float", "AnalogControl", 0.0,
          min=0.0, max=63.0, inc=0.1, unit="%", visibility="expert"),
        n("Gamma", "Gamma", "float", "AnalogControl", 1.0,
          min=0.25, max=4.0, inc=0.01, visibility="expert"),
        n("GammaEnable", "Gamma Enable", "bool", "AnalogControl", True,
          visibility="expert"),
        # --- DeviceControl ---
        n("DeviceModelName", "Device Model Name", "string", "DeviceControl",
          "FakeCamera", writable=False),
        n("DeviceUserID", "Device User ID", "string", "DeviceControl", "",
          visibility="expert"),
        n("DeviceLinkThroughputLimit", "Device Link Throughput Limit", "int",
          "DeviceControl", 380_000_000, min=1_000_000, max=380_000_000, inc=1,
          unit="Bps", visibility="expert"),
    ])
    return nodes


# Command nodes the fake exposes (name -> (display, category, visibility)). The
# execute is a no-op that bumps a counter so tests can assert it ran.
_COMMANDS = {
    "TimestampLatch": ("Timestamp Latch", "DeviceControl", "expert"),
    "DeviceReset": ("Device Reset", "DeviceControl", "guru"),
}


class FakeBackend(SoftwareTriggerHandoff):
    """A single in-memory camera driven by software triggers."""

    extension = "fake"

    def __init__(self, serial: str):
        self._serial = serial
        self._open = False
        self._nodes = _default_nodes()
        self._commands_run: dict[str, int] = {}
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

    def grab_locked_features(self) -> frozenset[str]:
        # The fake models a live-offset camera (FLIR-like): only Width/Height are
        # grab-locked. A test can monkeypatch this to exercise the Basler-style
        # offset-grab-lock path.
        return GEOMETRY_FEATURES

    def width(self) -> int:
        return int(self._nodes["Width"]["value"])

    def height(self) -> int:
        return int(self._nodes["Height"]["value"])

    # ----------------------------------------------------- sensor parameters

    def _offset_max(self, sfnc: str) -> int:
        """Dynamic offset ceiling: sensor size minus the current ROI size, like
        real hardware (so centering exercises the (full - size) path)."""
        if sfnc == "OffsetX":
            return int(self._nodes["WidthMax"]["value"] - self._nodes["Width"]["value"])
        return int(self._nodes["HeightMax"]["value"] - self._nodes["Height"]["value"])

    def read_node(self, name: str) -> NodeInfo:
        """One of the six legacy snake_case params (maps to its SFNC node)."""
        sfnc = _PARAM_TO_SFNC.get(name, name)
        try:
            node = self._nodes[sfnc]
        except KeyError as e:
            raise BackendError(f"unknown node: {name}") from e
        max_val = node.get("max")
        if sfnc in ("OffsetX", "OffsetY"):
            max_val = self._offset_max(sfnc)
        return NodeInfo(
            value=node["value"],
            min=node.get("min"),
            max=max_val,
            inc=node.get("inc"),
            unit=node.get("unit"),
            writable=self._open and node.get("writable", True),
        )

    def write_node(self, name: str, value: float) -> None:
        sfnc = _PARAM_TO_SFNC.get(name, name)
        if sfnc not in self._nodes:
            raise BackendError(f"unknown node: {name}")
        self._nodes[sfnc]["value"] = value

    # Typed-setter seam used by the native-TSV config applier (_genicam_config),
    # keyed by SFNC name straight into the node table. A node the fake does not
    # model raises/returns-None so the applier and serialiser skip it, exactly as
    # a real camera skips a node it lacks.
    def _set_enum(self, name: str, value: str) -> None:
        node = self._nodes.get(name)
        if node is None or node["type"] != "enum":
            raise BackendError(f"fake has no enumeration {name}")
        node["value"] = value

    def _get_enum(self, name: str) -> str | None:
        node = self._nodes.get(name)
        return node["value"] if node and node["type"] == "enum" else None

    def _set_bool(self, name: str, value: bool) -> None:
        node = self._nodes.get(name)
        if node is None or node["type"] != "bool":
            raise BackendError(f"fake has no boolean {name}")
        node["value"] = bool(value)

    def _get_bool(self, name: str) -> bool | None:
        node = self._nodes.get(name)
        return bool(node["value"]) if node and node["type"] == "bool" else None

    def _set_number(self, name: str, value: float, is_int: bool) -> None:
        node = self._nodes.get(name)
        if node is None or node["type"] not in ("int", "float"):
            raise BackendError(f"fake has no node {name}")
        node["value"] = int(value) if is_int else float(value)

    def _get_number(self, name: str, is_int: bool) -> float | int | None:
        node = self._nodes.get(name)
        if node is None or node["type"] not in ("int", "float"):
            return None
        return int(node["value"]) if is_int else float(node["value"])

    # ---------------------------------------------------- full device node map

    def _feature(self, sfnc: str) -> FeatureInfo:
        node = self._nodes[sfnc]
        kind = node["type"]
        max_val = node.get("max")
        if sfnc in ("OffsetX", "OffsetY"):
            max_val = self._offset_max(sfnc)
        return FeatureInfo(
            name=sfnc,
            display_name=node["display_name"],
            type=kind,
            category=node["category"],
            value=node["value"],
            min=node.get("min") if kind in ("int", "float") else None,
            max=max_val if kind in ("int", "float") else None,
            inc=node.get("inc") if kind in ("int", "float") else None,
            unit=node.get("unit") if kind in ("int", "float") else None,
            entries=node.get("entries") if kind == "enum" else None,
            readable=True,
            writable=self._open and node.get("writable", True),
            visibility=node.get("visibility", "beginner"),
        )

    def _command_feature(self, name: str) -> FeatureInfo:
        display, category, visibility = _COMMANDS[name]
        return FeatureInfo(
            name=name,
            display_name=display,
            type="command",
            category=category,
            readable=False,
            writable=self._open,
            visibility=visibility,
        )

    def list_features(self) -> list[FeatureInfo]:
        if not self._open:
            return []
        features = [self._feature(sfnc) for sfnc in self._nodes]
        # The genicam walker surfaces Beginner/Expert/Guru (only Invisible is
        # dropped); the browser's level selector filters client-side. All fake
        # command nodes are at or below Guru, so none are skipped here.
        features += [self._command_feature(name) for name in _COMMANDS]
        return features

    def read_feature(self, name: str) -> FeatureInfo:
        if name in self._nodes:
            return self._feature(name)
        if name in _COMMANDS:
            return self._command_feature(name)
        raise BackendError(f"no such node: {name}")

    def write_feature(self, name: str, value: object) -> None:
        node = self._nodes.get(name)
        if node is None:
            raise BackendError(f"no such node: {name}")
        kind = node["type"]
        if not node.get("writable", True):
            raise BackendError(f"node {name} is not writable")
        if kind == "int":
            node["value"] = int(round(float(value)))
        elif kind == "float":
            node["value"] = float(value)
        elif kind == "bool":
            node["value"] = str(value).strip().lower() in ("1", "true", "yes", "on") \
                if isinstance(value, str) else bool(value)
        elif kind == "enum":
            valid = {e["value"] for e in node.get("entries", [])}
            if valid and str(value) not in valid:
                raise BackendError(f"{value!r} is not a valid {name} entry")
            node["value"] = str(value)
        elif kind == "string":
            node["value"] = str(value)
        else:
            raise BackendError(f"node {name} is not writable ({kind})")

    def execute_command(self, name: str) -> None:
        if name not in _COMMANDS:
            raise BackendError(f"no such command: {name}")
        self._commands_run[name] = self._commands_run.get(name, 0) + 1

    def config_values(self, config_str: str) -> dict[str, str]:
        return dict(parse_config(config_str))

    def load_params(self, config_str: str) -> None:
        if config_str:
            apply_config(self, config_str)

    def save_params(self) -> str:
        # dump_config walks CONFIG_NODES and emits only the nodes the fake models
        # (via the typed getters above), in the native GenApi persistence TSV.
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
            width = int(self._nodes["Width"]["value"])
            height = int(self._nodes["Height"]["value"])
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
            width = int(self._nodes["Width"]["value"])
            height = int(self._nodes["Height"]["value"])
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
