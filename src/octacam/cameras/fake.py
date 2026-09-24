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
    coerce_bool,
)

log = logging.getLogger("octacam")

FAKE_CAMERAS_ENV = "OCTACAM_FAKE_CAMERAS"
_DEFAULT_SERIALS = "FAKE-0,FAKE-1"

# Full sensor size the fake models; Width/Height ROI sits within it and the
# offset ranges (and centering) are derived from it, like real hardware.
_SENSOR_W, _SENSOR_H = 1920, 1200
# How long a software-trigger fetch that finds no image ready waits (a real one
# blocks up to its timeout): long enough not to spin, short enough that an image
# one fetch late is still well inside a test's trigger period.
_FETCH_WAIT_S = 0.002

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
        self._freerun_fps: float | None = None
        # True while a preview grab is live (set in start_grab_preview, cleared in
        # start_grab_record/stop_grab). Lets retrieve_freerun pace an uncapped
        # managed preview without pacing the benchmark's uncapped record-grab probe.
        self._preview_grab = False
        # Test knobs modelling a camera's failure modes, keyed by trigger sequence
        # number: ``miss_triggers`` — no frame for these (a missed pulse; on the
        # software path the SDK says so at once, like an incomplete image);
        # ``ignore_first_triggers`` — how many triggers after each grab start
        # produce no frame at all (a FLIR Grasshopper3 ignores its first two);
        # ``hardware_period_ns`` — stamp frames from an ideal camera clock ticking
        # at the trigger period and hide the trigger sequence, so a recording
        # places frames by timestamp exactly as it must for a real
        # hardware-triggered camera; ``zero_timestamp_triggers`` — on that clock,
        # frames that come without a timestamp (0). The software-trigger path
        # (no ``hardware_period_ns``) models the device's image queue:
        # ``late_triggers`` maps a trigger to how many fetches its image misses
        # before it arrives, and ``lost_triggers`` never deliver an image, without
        # a word (the hand-off gives up on them at its answer deadline).
        self.miss_triggers: set[int] = set()
        self.ignore_first_triggers = 0
        # On the software path, ignore those first triggers *silently*, as a real
        # Grasshopper3 does (no image, no error: the hand-off waits out each one's
        # answer deadline) instead of answering them at once.
        self.ignore_first_silently = False
        self.hardware_period_ns: int | None = None
        self.zero_timestamp_triggers: set[int] = set()
        self.late_triggers: dict[int, int] = {}
        self.lost_triggers: set[int] = set()
        # Seconds from a software trigger to its image being ready (a long
        # exposure plus readout and transfer), and per trigger fired since the
        # grab started (1 = the first) a latency of its own (a USB stall).
        self.image_latency_s = 0.0
        self.latency_by_fire: dict[int, float] = {}
        # A fetch that finds no image waits out the retrieve's whole timeout, as
        # a real SDK's does, instead of a token wait that keeps tests quick.
        self.fetch_blocks = False
        self._clock_t0 = 1_000_000_000_000
        self._triggers_since_grab = 0
        # Images exposed but not yet fetched, oldest first: [trigger sequence
        # number (-1 when unnumbered), fetches it still misses, monotonic time
        # it is ready, its timestamp: when it was exposed, as a camera stamps it].
        self._device_images: list[list[float]] = []
        self._init_trigger_handoff()

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

    def _roi_max(self, sfnc: str) -> int | None:
        """The dynamic ceiling of a ROI node, or None for any other node.

        The coupling runs both ways on a real GenICam camera: an origin maxes out
        at (sensor - size), and a size maxes out at (sensor - origin). Modelling
        the second direction is what lets the hardware-free suite catch a caller
        that programs the ROI in the wrong order — growing Width/Height while a
        stale origin is still on the device (see
        ``_genicam_config._clear_roi_offsets``)."""
        if sfnc in ("OffsetX", "OffsetY"):
            return self._offset_max(sfnc)
        if sfnc == "Width":
            return int(
                self._nodes["WidthMax"]["value"] - self._nodes["OffsetX"]["value"]
            )
        if sfnc == "Height":
            return int(
                self._nodes["HeightMax"]["value"] - self._nodes["OffsetY"]["value"]
            )
        return None

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
        # Reject an out-of-range ROI value the way a real camera does. Only the
        # four coupled ROI nodes are bounds-checked (see _roi_max); every other
        # node stays a permissive value store, so the applier's other paths keep
        # exercising the same round-trips as before.
        limit = self._roi_max(name)
        if limit is not None and value > limit:
            raise BackendError(f"fake: {name} value {int(value)} exceeds max {limit}")
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
            node["value"] = coerce_bool(value)
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

    def begin_freerun(self, fps: float | None = None) -> bool:
        # The fake has no exposure pipeline, so free-run is simply "produce a
        # frame per fetch with no trigger" — always supported. ``fps`` is the
        # free-run preview rate cap; the fake honours it only to pace
        # retrieve_freerun (see there), since it has no real sensor timing.
        self._freerun_fps = fps
        return True

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        with self._cond:
            if not self._grabbing:
                return None
            # Pace a *capped* free-run (a preview) to its target rate so it does
            # not busy-loop — real backends block on the SDK fetch here. A managed
            # preview grabs via retrieve_freerun with no fps cap (_freerun_fps is
            # None), so also bound it to the grab timeout when this is a preview
            # grab. Only the benchmark's uncapped record-grab probe (no cap, not a
            # preview) returns immediately. cond.wait releases the lock and is
            # woken by stop_grab's notify.
            if self._freerun_fps or self._preview_grab:
                self._cond.wait(
                    min(1.0 / self._freerun_fps, timeout_ms / 1000.0)
                    if self._freerun_fps
                    else timeout_ms / 1000.0
                )
                if not self._grabbing:
                    return None
            self._frame_index += 1
            index = self._frame_index
            width = int(self._nodes["Width"]["value"])
            height = int(self._nodes["Height"]["value"])
        array = _render(width, height, index) if wants_array() else None
        return (array, time.time_ns())

    # ------------------------------------------------------------- grabbing

    def start_grab_preview(self) -> None:
        self._preview_grab = True
        self._triggers_since_grab = 0
        self._device_images.clear()
        self._begin_grab()

    def start_grab_record(self) -> bool:
        self.start_grab_preview()
        self._preview_grab = False  # a record grab is uncapped (benchmark probe)
        return True

    def stop_grab(self) -> None:
        self._preview_grab = False
        self._end_grab()

    def restart_trigger_sequence(self) -> None:
        # A real camera's clock runs on through the pause between a recording's
        # priming pulses and its train; model it (1 s) so the train's frames are
        # not mistaken for priming stragglers.
        if self.hardware_period_ns:
            self._clock_t0 += self._next_seq * self.hardware_period_ns + 1_000_000_000
        super().restart_trigger_sequence()

    @property
    def last_trigger_index(self) -> int | None:
        # A hardware-clocked fake behaves like a real hardware-triggered camera:
        # its frames carry a timestamp, not the trigger's sequence number.
        if self.hardware_period_ns:
            return None
        return super().last_trigger_index

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        # A frame shows its trigger's sequence number (mod 256), so a test can
        # tell which pulse a video frame really is.
        period = self.hardware_period_ns
        if period:
            return self._retrieve_clocked(timeout_ms, wants_array, period)
        fire = self._claim_trigger(timeout_ms)
        if fire is None:
            return None
        with self._cond:
            if fire and not self._expose():
                return None
            fetched = self._fetch(self._fetch_timeout_ms(timeout_ms))
            if fetched is None:
                return None
            seq, stamp = fetched
            self._frame_index += 1
            index = self._frame_index if seq < 0 else seq
            width = int(self._nodes["Width"]["value"])
            height = int(self._nodes["Height"]["value"])
        array = _render(width, height, index) if wants_array() else None
        return (array, stamp)

    def _expose(self) -> bool:
        """The device's response to the trigger just fired (the newest outstanding
        one; caller holds the condition). False when no image will come of it."""
        if not self._outstanding:  # the grab restarted under us: nothing fired
            return False
        self._triggers_since_grab += 1
        seq = self._outstanding[-1][0]
        if self._triggers_since_grab <= self.ignore_first_triggers and (
            self.ignore_first_silently
        ):
            return True  # nothing will arrive, and nothing says so
        if self._triggers_since_grab <= self.ignore_first_triggers or (
            seq in self.miss_triggers
        ):
            # No frame, and the SDK says so at once (like an incomplete image):
            # the trigger is answered, and the next one can fire.
            self._trigger_answered()
            return False
        if seq in self.lost_triggers:
            return True  # nothing will arrive; the hand-off gives up on it
        key = -1 if seq is None else seq
        self._device_images.append(
            [
                key,
                self.late_triggers.get(key, 0),
                time.monotonic()
                + self.latency_by_fire.get(self._triggers_since_grab, self.image_latency_s),
                time.time_ns(),
            ]
        )
        return True

    def _fetch(self, timeout_ms: int = 0) -> tuple[int, int] | None:
        """One fetch from the device's image queue (caller holds the condition):
        the sequence number (-1 when unnumbered) and timestamp of the image it
        hands over, or None when none is ready — after a short wait, standing in
        for a real fetch's timeout without holding a test up for it."""
        head = self._device_images[0] if self._device_images else None
        if head is None or head[1] > 0 or time.monotonic() < head[2]:
            if head is not None and head[1] > 0:
                head[1] -= 1
            if not self.fetch_blocks:
                self._cond.wait(_FETCH_WAIT_S)
                return None
            # A real fetch returns early only for an image, never because a
            # trigger was offered meanwhile (the condition's notify).
            until = time.monotonic() + timeout_ms / 1000.0
            while (now := time.monotonic()) < until:
                if self._device_images and self._device_images[0][1] <= 0 and (
                    now >= self._device_images[0][2]
                ):
                    break
                self._cond.wait(until - now)
            else:
                return None
            head = self._device_images[0]
        seq, stamp = int(head[0]), int(head[3])
        self._device_images.pop(0)
        self._trigger_answered(stamp)
        return seq, stamp

    def _retrieve_clocked(
        self, timeout_ms: int, wants_array: Callable[[], bool], period_ns: int
    ) -> Frame | None:
        # The hardware-clock model: a pulse exposes a frame (or is missed) whether
        # or not the camera answered the previous one, so there is no answer to
        # wait for.
        if not self._wait_pending(timeout_ms):
            return None
        with self._cond:
            self._triggers_since_grab += 1
            seq = self._answer[0] if self._answer is not None else None
            if self._triggers_since_grab <= self.ignore_first_triggers:
                return None  # this trigger never exposed a frame
            if seq is not None and (seq in self.miss_triggers or seq in self.lost_triggers):
                return None  # the pulse was missed
            self._frame_index += 1
            index = self._frame_index if seq is None else seq
            width = int(self._nodes["Width"]["value"])
            height = int(self._nodes["Height"]["value"])
        array = _render(width, height, index) if wants_array() else None
        if seq is None:
            return (array, time.time_ns())
        if seq in self.zero_timestamp_triggers:
            return (array, 0)
        return (array, self._clock_t0 + seq * period_ns)

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
    """A cheap, owned mono frame of value ``index`` mod 256 (the trigger's
    sequence number where the frame answers one, else the frame count)."""
    return np.full((height, width), index % 256, dtype=np.uint8)


def _available_serials() -> list[str]:
    raw = os.environ.get(FAKE_CAMERAS_ENV, _DEFAULT_SERIALS)
    return [s.strip() for s in raw.split(",") if s.strip()]


def enumerate_fake(
    requested_serials: list[str] | None = None, *, warn_missing: bool = True
):
    """Return ``[(serial, serial), ...]`` for the configured fake cameras.

    Mirrors :func:`octacam.cameras.basler.enumerate_basler`: with no requested
    serials, every available fake camera is returned (sorted); otherwise the
    listed serials are returned in order, warning about any not available. The
    handle is just the serial string (the FakeBackend needs nothing more).

    ``warn_missing=False`` suppresses the per-serial "not found" warning: the
    auto cascade offers the whole rig's serial list to every tier, so most of
    those serials legitimately belong to another backend and must not be
    reported missing here (``CameraSystem._enumerate`` warns once for a serial
    that no tier claimed).
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
            if warn_missing:
                log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, serial))
    return out
