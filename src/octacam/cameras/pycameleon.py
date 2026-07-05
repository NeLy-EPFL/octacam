"""pycameleon camera backend (the always-available cascade floor).

``pycameleon`` (PyPI, MIT) is a PyO3 binding over the Rust ``cameleon`` crate,
which speaks USB3 Vision directly through ``libusb`` — no GenTL ``.cti``
producer, no vendor SDK, no EULA. Its wheel is ``cp39-abi3``, so ``uv add
pycameleon`` installs on modern Python (3.14 today), which is why this backend is
a **core dependency** and the guaranteed final tier of the backend cascade: when
no vendor SDK and no GenTL producer are available, pycameleon still drives any
USB3-Vision camera.

Mapping notes vs. the other backends:

* Node names are the standard SFNC ones, so :data:`PARAM_NODES` is reused;
  Width/Height/Offset* are integer nodes, ExposureTime/Gain are float nodes
  (:func:`read_integer` / :func:`read_float`).
* pycameleon 0.2.x exposes **only node values** — no bounds/increment/unit or
  writability. :class:`NodeInfo` therefore leaves ``min/inc/unit`` ``None`` and
  reports ``writable`` from the open state (as the fake backend does for
  geometry); ``snap_value`` already tolerates ``None`` bounds. Width/Height fill
  ``max`` from the readable SFNC ``WidthMax``/``HeightMax`` when present.
* There is no per-frame hardware timestamp, so :meth:`retrieve` returns a ``0``
  timestamp and :class:`~octacam.cameras.base.Camera` falls back to host time.
* Parameters persist as JSON (``extension = "json"``), the same scheme the FLIR
  and fake backends use.
* pycameleon takes an **exclusive borrow** of the camera object: calling
  ``execute()`` (the software trigger) from one thread while ``receive()`` runs on
  another raises "Already borrowed". octacam's shared trigger timer and grab loop
  are different threads, so — like the *fake* backend — :meth:`trigger_once` only
  bumps a pending counter (no device access) and the grab loop's :meth:`retrieve`
  does the ``TriggerSoftware`` execute *and* the ``receive`` back-to-back under a
  single lock. All other device access (node reads/writes) takes the same lock,
  so the camera is only ever touched by one thread at a time.
"""

import json
import logging
import threading
from typing import Any

import numpy as np

from octacam.cameras._trigger_handoff import SoftwareTriggerHandoff
from octacam.cameras.base import (
    PARAM_NODES,
    BackendError,
    Frame,
    NodeInfo,
    snap_value,
)
from octacam.cameras.registry import BackendUnavailable

try:  # pycameleon is a core dep, but keep the import defensive like the others.
    import pycameleon
except ImportError:  # pragma: no cover - pycameleon ships in core
    pycameleon = None

log = logging.getLogger("octacam")

# Spinnaker/Basler-style integer nodes; the rest of PARAM_NODES are floats.
_INT_PARAMS = frozenset({"width", "height", "offset_x", "offset_y"})

# In-flight payload buffers handed to start_streaming. Software triggering means
# one frame is produced per trigger, so a small pool is plenty.
_STREAM_CAPACITY = 8


def _pycameleon():
    """Return the pycameleon module, or raise a clean BackendUnavailable."""
    if pycameleon is None:
        raise BackendUnavailable(
            "pycameleon", "the 'pycameleon' package is not installed"
        )
    return pycameleon


def ensure_available() -> None:
    """Raise BackendUnavailable if pycameleon is not importable."""
    _pycameleon()


class PycameleonBackend(SoftwareTriggerHandoff):
    """A single USB3-Vision camera driven through pycameleon/libusb."""

    extension = "json"

    def __init__(self, cam):
        # The PyCameleonCamera handle from enumerate_cameras(); set to None by
        # close() so a second close() (or a post-close query) is a safe no-op.
        # Typed Any (like the FLIR CameraPtr) so the close()-sets-None lifecycle
        # does not trip the optional-access checker on every method.
        self._cam: Any = cam
        self._serial = _read_serial(cam)
        self._open = False
        self._original_trigger_source: str | None = None
        # Cached GenApi XML: fetched once from the camera on the first open so a
        # later re-open can use the faster/safer load_context_from_xml.
        self._context_xml: str | None = None
        # Streaming state, owned by start_grab_* / stop_grab.
        self._receiver = None
        # Serializes every device call (pycameleon allows only one at a time).
        # Reentrant so composed methods (read_node → _read_int_opt) can nest.
        self._lock = threading.RLock()
        # Software-trigger hand-off (shared mixin): trigger_once bumps a counter;
        # retrieve consumes it and does the execute+receive under _lock. pycameleon
        # *requires* this — an exclusive borrow forbids execute() on one thread
        # while receive() runs on another.
        self._init_trigger_handoff()

    @property
    def serial_number(self) -> str:
        return self._serial

    # ------------------------------------------------------------- lifecycle

    def open(self) -> None:
        with self._lock:
            try:
                self._cam.open()
                if self._context_xml is None:
                    self._context_xml = self._cam.load_context_from_camera()
                else:
                    self._cam.load_context_from_xml(self._context_xml)
            except Exception as e:
                # Usually the device is already in use by another process (a
                # second octacam on the rig). Surface it as a BackendError so the
                # caller reports it cleanly instead of a raw pycameleon traceback.
                raise BackendError(str(e)) from e
            self._open = True
            # Force Mono8 so receive() yields a 2-D uint8 array matching the writer.
            try:
                self._set_enum("PixelFormat", "Mono8")
            except BackendError as e:
                log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)

    def close(self) -> None:
        if self._cam is None:  # idempotent: a second close() must not raise
            return
        self.stop_grab()  # sets _grabbing False and stops streaming under the lock
        with self._lock:
            try:
                self._cam.close()  # auto-stops streaming and closes cleanly
            except Exception as e:
                log.warning("Error closing camera %s: %s", self._serial, e)
            self._cam = None
            self._open = False

    def is_open(self) -> bool:
        return self._cam is not None and self._open

    def is_grabbing(self) -> bool:
        return self._grabbing

    def width(self) -> int:
        with self._lock:
            return int(self._cam.read_integer("Width"))

    def height(self) -> int:
        with self._lock:
            return int(self._cam.read_integer("Height"))

    # ----------------------------------------------------- node plumbing

    def _read_int_opt(self, node: str) -> int | None:
        with self._lock:
            try:
                return int(self._cam.read_integer(node))
            except Exception:
                return None

    def _read_enum_opt(self, node: str) -> str | None:
        with self._lock:
            try:
                return self._cam.read_enum_as_str(node)
            except Exception:
                return None

    def _set_enum(self, node: str, value: str) -> None:
        with self._lock:
            try:
                self._cam.write_enum_as_str(node, value)
            except Exception as e:
                raise BackendError(str(e)) from e

    # ----------------------------------------------------- sensor parameters

    def read_node(self, name: str) -> NodeInfo:
        sfnc = PARAM_NODES[name]
        with self._lock:
            try:
                if name in _INT_PARAMS:
                    value: float | int = int(self._cam.read_integer(sfnc))
                else:
                    value = float(self._cam.read_float(sfnc))
            except Exception as e:
                raise BackendError(str(e)) from e
        # Only the sensor bound is available (WidthMax/HeightMax); pycameleon
        # exposes no min/inc/unit or per-node writability.
        maximum: float | None = None
        if name == "width":
            maximum = self._read_int_opt("WidthMax")
        elif name == "height":
            maximum = self._read_int_opt("HeightMax")
        return NodeInfo(
            value=value,
            min=None,
            max=maximum,
            inc=None,
            unit=None,
            writable=self._open,
        )

    def write_node(self, name: str, value: float) -> None:
        sfnc = PARAM_NODES[name]
        with self._lock:
            try:
                if name in _INT_PARAMS:
                    self._cam.write_integer(sfnc, int(value))
                else:
                    self._cam.write_float(sfnc, float(value))
            except Exception as e:
                raise BackendError(str(e)) from e

    def load_params(self, config_str: str) -> None:
        if config_str:
            try:
                data = json.loads(config_str)
            except (ValueError, TypeError) as e:
                raise BackendError(f"invalid pycameleon parameters: {e}") from e
            if not isinstance(data, dict):
                raise BackendError("invalid pycameleon parameters: expected an object")
            params = data.get("params") or {}
            # Geometry first (while not streaming), then the live params.
            for name in ("width", "height", "offset_x", "offset_y", "exposure", "gain"):
                if name not in params:
                    continue
                try:
                    info = self.read_node(name)
                    self.write_node(name, snap_value(float(params[name]), info))
                except BackendError as e:
                    log.warning(
                        "Could not restore %s on camera %s: %s", name, self._serial, e
                    )
        self._original_trigger_source = self._read_enum_opt("TriggerSource")

    def save_params(self) -> str:
        params: dict[str, float] = {}
        for name in PARAM_NODES:
            try:
                params[name] = self.read_node(name).value
            except BackendError:
                continue
        data = {
            "params": params,
            "trigger_mode": "Off",
            "trigger_source": self._original_trigger_source,
        }
        return json.dumps(data, indent=2) + "\n"

    # ----------------------------------------------------------- triggering

    def _enable_trigger_overlap(self) -> None:
        """Let a trigger be accepted during the previous frame's readout.

        Without this (``TriggerOverlap=Off``, the FLIR default) a FrameStart
        software trigger fired while the sensor is still reading out the previous
        frame is **silently ignored**, so the camera accepts only ~every other
        trigger — roughly halving the software-triggered frame rate and adding a
        full grab-timeout stall on each dropped one. ``ReadOut`` pipelines
        back-to-back triggers and restores the sensor's real rate (measured on
        the Spinnaker backends: ~4.7 → ~64 fps at 4 ms exposure; see
        ``docs/plan-spinnaker-c-backend.md``). Mirrors the same fix in
        :mod:`~octacam.cameras.spinnaker_c` / :mod:`~octacam.cameras.flir`.
        Best-effort: a camera without the node keeps its default.
        """
        try:
            self._set_enum("TriggerOverlap", "ReadOut")
        except BackendError as e:
            log.debug("Could not set TriggerOverlap on camera %s: %s", self._serial, e)

    def enable_frame_trigger(self) -> None:
        if not self._open:
            return
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")
        self._enable_trigger_overlap()

    def set_trigger_source(self, use_software: bool) -> None:
        if not self._open:
            return
        try:
            if use_software:
                self._set_enum("TriggerSource", "Software")
            elif self._original_trigger_source is not None:
                self._set_enum("TriggerSource", self._original_trigger_source)
        except BackendError as e:
            log.warning(
                "Failed to set trigger source on camera %s: %s", self._serial, e
            )

    def begin_software_trigger_preview(self) -> None:
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")
        self._set_enum("TriggerSource", "Software")
        self._enable_trigger_overlap()

    def trigger_once(self) -> None:
        # Only bump the pending counter — the device call (TriggerSoftware) is
        # done by retrieve(), so the trigger timer thread never touches the camera
        # while the grab loop's receive() holds the borrow.
        self._bump_trigger()

    # ------------------------------------------------------------- grabbing

    def _start_streaming(self) -> None:
        with self._lock:
            try:
                self._receiver = self._cam.start_streaming(_STREAM_CAPACITY)
            except Exception as e:
                log.error("Failed to start streaming on camera %s: %s", self._serial, e)
                raise BackendError(str(e)) from e
        self._begin_grab()

    def start_grab_preview(self) -> None:
        self._start_streaming()

    def start_grab_record(self) -> bool:
        self._start_streaming()
        return True

    def stop_grab(self) -> None:
        if not self._end_grab():  # flips _grabbing + wakes any blocked retrieve
            return
        with self._lock:
            if self._receiver is not None:
                try:
                    self._cam.stop_streaming()
                except Exception:
                    pass
                self._receiver = None

    def retrieve(self, timeout_ms: int, wants_array) -> Frame | None:
        # Wait (like the fake backend) for a software trigger, then do the
        # execute+receive back-to-back under the device lock so nothing else
        # touches the camera in between. A 0 timestamp makes Camera fall back to
        # host time.
        if not self._wait_pending(timeout_ms):
            return None
        with self._lock:
            if not self._grabbing or self._receiver is None or self._cam is None:
                return None
            try:
                self._cam.execute("TriggerSoftware")
                array = self._cam.receive(self._receiver)
            except Exception as e:
                log.debug("receive failed on camera %s: %s", self._serial, e)
                return None
        # Own the frame; skip the copy when the display slot is full (preview).
        out = np.array(array, copy=True) if wants_array() else None
        return (out, 0)


def _read_serial(cam) -> str:
    """Best-effort serial from a PyCameleonCamera's info() descriptor."""
    try:
        info = cam.info()
        serial = info.get("serial_number") if isinstance(info, dict) else None
    except Exception:
        serial = None
    return str(serial) if serial else ""


def enumerate_pycameleon(requested_serials: list[str] | None = None):
    """Return ``[(serial, PyCameleonCamera), ...]`` for the requested cameras.

    Mirrors :func:`octacam.cameras.basler.enumerate_basler`: with no requested
    serials, every detected camera is returned (sorted by serial); otherwise the
    listed serials are returned in order, warning about any not connected.
    """
    p = _pycameleon()
    cams = p.enumerate_cameras()
    if not cams:
        return []
    # Debug, not info: the auto cascade enumerates every tier, so CameraSystem
    # logs the single attributed "Detected N" summary (see basler backend).
    log.debug("pycameleon enumerated %d camera(s)", len(cams))

    by_serial: dict[str, object] = {}
    detected: list[str] = []
    for cam in cams:
        serial = _read_serial(cam)
        if not serial:
            continue
        detected.append(serial)
        by_serial[serial] = cam

    final = sorted(detected) if not requested_serials else list(requested_serials)
    out = []
    for serial in final:
        cam = by_serial.get(serial)
        if cam is None:
            log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, cam))
    return out
