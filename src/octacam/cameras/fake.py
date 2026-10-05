"""In-memory fake camera backend, the CI vehicle.

An SFNC-keyed node table spanning every widget kind, and synthetic Mono8 frames
that come only once ``trigger_once`` has fired, so the rig's PreciseTimer drives
it. Its test knobs model a real camera's failure modes. ``enumerate_fake`` reads
serials from ``OCTACAM_FAKE_CAMERAS`` (default ``FAKE-0,FAKE-1``).
"""

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from octacam.cameras.base import BackendError, FeatureInfo, Frame, coerce_bool
from octacam.cameras.genicam import GenICamBackend
from octacam.cameras.registry import BackendSpec, select_serials

log = logging.getLogger("octacam")

FAKE_CAMERAS_ENV = "OCTACAM_FAKE_CAMERAS"
_DEFAULT_SERIALS = "FAKE-0,FAKE-1"

# The modeled sensor: the ROI sits within it and bounds the offsets.
_SENSOR_W, _SENSOR_H = 1920, 1200
# A fetch finding no image waits this long (unless fetch_blocks): long enough not
# to spin, short enough that an image one fetch late is well inside a period.
_FETCH_WAIT_S = 0.002


def _default_nodes() -> dict[str, dict]:
    """A fresh SFNC-keyed node table spanning every widget kind."""

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


# The Python type of each node kind's value.
_CAST: dict[str, Callable[[Any], Any]] = {
    "int": int, "float": float, "bool": bool, "enum": str, "string": str,
}


@dataclass
class _Image:
    """An image the fake device exposed and holds until a fetch takes it."""

    seq: int  # the trigger's sequence number
    misses: int  # fetches it still misses
    ready_at: float  # monotonic time it is ready
    stamp: int  # its timestamp: when it was exposed, as a camera stamps it

    def ready(self, now: float) -> bool:
        return self.misses <= 0 and now >= self.ready_at


# Command nodes: name -> (display, category, visibility). Executing one counts it.
_COMMANDS = {
    "TimestampLatch": ("Timestamp Latch", "DeviceControl", "expert"),
    "DeviceReset": ("Device Reset", "DeviceControl", "guru"),
}


class FakeBackend(GenICamBackend):
    """A single in-memory camera driven by software triggers."""

    extension = "fake"

    def __init__(self, serial: str):
        super().__init__(serial)
        self._open = False
        self._nodes = _default_nodes()
        self._commands_run: dict[str, int] = {}
        self._frame_index = 0
        self._freerun_fps: float | None = None
        # A preview grab is live: retrieve_freerun paces it even uncapped (a
        # managed preview), but not the benchmark's uncapped record grab.
        self._preview_grab = False
        # Test knobs modeling a camera's failure modes, by trigger sequence
        # number. Missed pulses: no frame (on the software path the SDK says so at
        # once, like an incomplete image).
        self.miss_triggers: set[int] = set()
        # Triggers after each grab start that expose nothing, as a GS3 ignores its
        # first; on the software path silently (no image, no error) if set.
        self.ignore_first_triggers = 0
        self.ignore_first_silently = False
        # Stamp frames from an ideal camera clock at this period and hide the
        # sequence, as a hardware-triggered camera; frames for
        # zero_timestamp_triggers carry a 0 timestamp on that clock.
        self.hardware_period_ns: int | None = None
        self.zero_timestamp_triggers: set[int] = set()
        # Software path: how many fetches a trigger's image misses before it
        # arrives, and triggers whose image never comes.
        self.late_triggers: dict[int, int] = {}
        self.lost_triggers: set[int] = set()
        # Trigger-to-image latency, overall and per fire since the grab start
        # (1 = the first: a USB stall).
        self.image_latency_s = 0.0
        self.latency_by_fire: dict[int, float] = {}
        # A fetch finding no image waits out its whole timeout (or until the grab
        # ends), not woken by a trigger offer, as a real SDK fetch does.
        self.fetch_blocks = False
        self._clock_t0 = 1_000_000_000_000
        self._triggers_since_grab = 0
        # Images exposed but not yet fetched, oldest first, and whether the next
        # fetch reports an incomplete image (a missed trigger).
        self._device_images: list[_Image] = []
        self._incomplete = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self.stop_grab()
        self._open = False

    def is_open(self) -> bool:
        return self._open

    # ----------------------------------------------------- sensor parameters

    def _offset_max(self, sfnc: str) -> int:
        """An offset's ceiling: sensor size minus ROI size, as on a camera."""
        if sfnc == "OffsetX":
            return int(self._nodes["WidthMax"]["value"] - self._nodes["Width"]["value"])
        return int(self._nodes["HeightMax"]["value"] - self._nodes["Height"]["value"])

    def _roi_max(self, sfnc: str) -> int | None:
        """A ROI node's ceiling, or None. The coupling runs both ways, as on a
        camera (an origin's max is sensor - size, a size's sensor - origin), so
        the suite catches ROI writes in the wrong order (see
        ``genicam._clear_roi_offsets``)."""
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

    # The typed seam, straight into the node table; a node the fake lacks raises
    # or reads None, so the TSV applier skips it as a camera's would.
    def _typed(self, name: str, kind: str) -> dict | None:
        """``name``'s table entry if it holds a ``kind`` value (int and float
        interchange), else None."""
        node = self._nodes.get(name)
        numeric = ("int", "float")
        if node is None or not (
            node["type"] == kind or (kind in numeric and node["type"] in numeric)
        ):
            return None
        return node

    def get_node(self, name: str, kind: str) -> Any:
        node = self._typed(name, kind)
        return None if node is None else _CAST[kind](node["value"])

    def set_node(self, name: str, kind: str, value: Any) -> None:
        node = self._typed(name, kind)
        if node is None:
            raise BackendError(f"fake has no {kind} node {name}")
        # Only the coupled ROI nodes are bounds-checked (see _roi_max).
        limit = self._roi_max(name)
        if limit is not None and value > limit:
            raise BackendError(f"fake: {name} value {int(value)} exceeds max {limit}")
        node["value"] = _CAST[kind](value)

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

    # ----------------------------------------------------------- triggering

    def enable_frame_trigger(self) -> None:
        pass  # the fake is always software-trigger ready

    def set_trigger_source(self, use_software: bool) -> None:
        pass

    def begin_software_trigger_preview(self) -> None:
        pass

    def begin_freerun(self, fps: float | None = None) -> bool:
        self._freerun_fps = fps  # only paces retrieve_freerun
        return True

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        if not self.trigger.grabbing:
            return None
        # Pace a capped free run and any preview, as a real fetch blocks; the
        # benchmark's uncapped record grab returns at once. stop_grab wakes the
        # wait.
        if self._freerun_fps or self._preview_grab:
            self.trigger.wait(
                min(1.0 / self._freerun_fps, timeout_ms / 1000.0)
                if self._freerun_fps
                else timeout_ms / 1000.0
            )
            if not self.trigger.grabbing:
                return None
        self._frame_index += 1
        array = _render(self.width(), self.height(), self._frame_index) if wants_array() else None
        return (array, time.time_ns())

    # ------------------------------------------------------------- grabbing

    def start_grab_preview(self) -> None:
        self._preview_grab = True
        self._triggers_since_grab = 0
        self._device_images.clear()
        self._incomplete = False
        self.trigger.begin_grab()

    def start_grab_record(self) -> bool:
        self.start_grab_preview()
        self._preview_grab = False  # a record grab is uncapped (benchmark probe)
        return True

    def stop_grab(self) -> None:
        self._preview_grab = False
        self.trigger.end_grab()

    def restart_trigger_sequence(self) -> None:
        # A camera clock runs on through the post-priming settle (1 s here), so
        # the train's frames are not taken for priming stragglers.
        if self.hardware_period_ns:
            self._clock_t0 += self.trigger.next_index * self.hardware_period_ns + 1_000_000_000
        super().restart_trigger_sequence()

    @property
    def last_trigger_index(self) -> int | None:
        # Hardware-clocked: frames carry a timestamp, not a sequence number.
        if self.hardware_period_ns:
            return None
        return super().last_trigger_index

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        if self.hardware_period_ns:
            return self._retrieve_clocked(timeout_ms, wants_array, self.hardware_period_ns)
        return super().retrieve(timeout_ms, wants_array)

    def _fire_trigger(self) -> bool:
        """The device's response to the trigger just fired (the newest
        outstanding one); False only when the grab restarted under it."""
        seq = self.trigger.fired_index
        if seq is None:
            return False
        self._triggers_since_grab += 1
        ignored = self._triggers_since_grab <= self.ignore_first_triggers
        if ignored and self.ignore_first_silently:
            return True  # nothing will arrive, and nothing says so
        if ignored or seq in self.miss_triggers:
            # No frame, and the SDK says so at once, like an incomplete image:
            # the trigger is answered, and the next one can fire.
            self._incomplete = True
            return True
        if seq in self.lost_triggers:
            return True  # nothing will arrive; the hand-off gives up on it
        latency = self.latency_by_fire.get(self._triggers_since_grab, self.image_latency_s)
        self._device_images.append(
            _Image(
                seq=seq,
                misses=self.late_triggers.get(seq, 0),
                ready_at=time.monotonic() + latency,
                stamp=time.time_ns(),
            )
        )
        return True

    def _fetch(
        self, timeout_ms: int, wants_array: Callable[[], bool], answers_trigger: bool
    ) -> Frame | None:
        # A frame shows its trigger's sequence number (mod 256), so a test can
        # tell which pulse a video frame really is.
        if self._incomplete:
            self._incomplete = False
            if answers_trigger:
                self.trigger.answered()
            return None
        image = self._next_image(timeout_ms)
        if image is None:
            return None
        if answers_trigger:
            self.trigger.answered(image.stamp)
        array = _render(self.width(), self.height(), image.seq) if wants_array() else None
        return (array, image.stamp)

    def _next_image(self, timeout_ms: int) -> _Image | None:
        """One fetch from the device's image queue: the image it hands over, or
        None. A wait ends early on a trigger offer or the grab's end (see
        SoftwareTrigger.wait)."""
        head = self._device_images[0] if self._device_images else None
        if head is None or not head.ready(time.monotonic()):
            if head is not None and head.misses > 0:
                head.misses -= 1
            if not self.fetch_blocks:
                self.trigger.wait(_FETCH_WAIT_S)
                return None
            # A real fetch returns early only for an image, never because a
            # trigger was offered meanwhile; this one also ends with the grab.
            until = time.monotonic() + timeout_ms / 1000.0
            while (now := time.monotonic()) < until and self.trigger.grabbing:
                if self._device_images and self._device_images[0].ready(now):
                    break
                self.trigger.wait(until - now)
            else:
                return None
        return self._device_images.pop(0)

    def _retrieve_clocked(
        self, timeout_ms: int, wants_array: Callable[[], bool], period_ns: int
    ) -> Frame | None:
        # A pulse exposes a frame (or is missed) whether or not the previous one
        # was answered: nothing to wait for.
        seq = self.trigger.take(timeout_ms)
        if seq is None:
            return None
        self._triggers_since_grab += 1
        if self._triggers_since_grab <= self.ignore_first_triggers:
            return None  # this trigger never exposed a frame
        if seq in self.miss_triggers or seq in self.lost_triggers:
            return None  # the pulse was missed
        array = _render(self.width(), self.height(), seq) if wants_array() else None
        if seq in self.zero_timestamp_triggers:
            return (array, 0)
        return (array, self._clock_t0 + seq * period_ns)

    def retrieve_external(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        """External-trigger record fetch: the trigger counter models the external
        source, so a recording with no pulses yields nothing (retrieve_freerun
        would fabricate a frame per call)."""
        return self.retrieve(timeout_ms, wants_array)


def _render(width: int, height: int, index: int) -> np.ndarray:
    """A cheap, owned mono frame of value ``index`` mod 256 (the trigger's
    sequence number where the frame answers one, else the frame count)."""
    return np.full((height, width), index % 256, dtype=np.uint8)


def _available_serials() -> list[str]:
    raw = os.environ.get(FAKE_CAMERAS_ENV, _DEFAULT_SERIALS)
    return [s.strip() for s in raw.split(",") if s.strip()]


def enumerate_fake(requested_serials: list[str] | None = None):
    """``[(serial, serial)]`` in :func:`select_serials` order."""
    available = _available_serials()
    if not available:
        return []
    log.debug("fake enumerated %d camera(s)", len(available))
    return [(serial, serial) for serial in select_serials(available, requested_serials)]


SPEC = BackendSpec(enumerate_fake, FakeBackend)
