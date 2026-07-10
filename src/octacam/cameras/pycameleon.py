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
* Parameters persist in the camera's native GenApi feature-persistence TSV
  (``extension = "txt"``; see :mod:`octacam.cameras._genicam_config`), shared
  with the FLIR/Spinnaker backends.
* pycameleon takes an **exclusive borrow** of the camera object: calling
  ``execute()`` (the software trigger) from one thread while ``receive()`` runs on
  another raises "Already borrowed". octacam's shared trigger timer and grab loop
  are different threads, so — like the *fake* backend — :meth:`trigger_once` only
  bumps a pending counter (no device access) and the grab loop's :meth:`retrieve`
  does the ``TriggerSoftware`` execute *and* the ``receive`` back-to-back under a
  single lock. All other device access (node reads/writes) takes the same lock,
  so the camera is only ever touched by one thread at a time.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from octacam.cameras._genicam_config import GenICamTriggerConfig
from octacam.cameras._trigger_handoff import SoftwareTriggerHandoff
from octacam.cameras.base import (
    GEOMETRY_FEATURES,
    PARAM_NODES,
    BackendError,
    FeatureInfo,
    Frame,
    NodeInfo,
    curated_list_features,
    curated_read_feature,
    curated_write_feature,
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


class PycameleonBackend(GenICamTriggerConfig, SoftwareTriggerHandoff):
    """A single USB3-Vision camera driven through pycameleon/libusb."""

    extension = "txt"

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
        # Event loop that drives the timeout-bounded receive_async (see
        # _receive_bounded); created lazily on the grab thread, closed by close().
        self._recv_loop: asyncio.AbstractEventLoop | None = None
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
            if self._recv_loop is not None:
                try:
                    self._recv_loop.close()
                except Exception:
                    pass
                self._recv_loop = None

    def is_open(self) -> bool:
        return self._cam is not None and self._open

    def is_grabbing(self) -> bool:
        return self._grabbing

    def grab_locked_features(self) -> frozenset[str]:
        # libusb FLIR floor: only Width/Height are locked during acquisition;
        # the ROI offsets stay live-writable while grabbing.
        return GEOMETRY_FEATURES

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

    def _get_enum(self, node: str) -> str | None:
        return self._read_enum_opt(node)

    # Typed-setter seam used by the native-TSV config applier (_genicam_config).
    def _set_bool(self, node: str, value: bool) -> None:
        with self._lock:
            try:
                self._cam.write_bool(node, bool(value))
            except Exception as e:
                raise BackendError(str(e)) from e

    def _get_bool(self, node: str) -> bool | None:
        with self._lock:
            try:
                return bool(self._cam.read_bool(node))
            except Exception:
                return None

    def _set_number(self, node: str, value: float, is_int: bool) -> None:
        with self._lock:
            try:
                if is_int:
                    self._cam.write_integer(node, int(value))
                else:
                    self._cam.write_float(node, float(value))
            except Exception as e:
                raise BackendError(str(e)) from e

    def _get_number(self, node: str, is_int: bool) -> float | int | None:
        with self._lock:
            try:
                return (
                    int(self._cam.read_integer(node))
                    if is_int
                    else float(self._cam.read_float(node))
                )
            except Exception:
                return None

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

    # pycameleon exposes only a small typed accessor set, not a full node-map
    # walk; the Camera tab falls back to the six curated PARAM_NODES.
    def list_features(self) -> list[FeatureInfo]:
        if not self.is_open():
            return []
        return curated_list_features(self)

    def read_feature(self, name: str) -> FeatureInfo:
        return curated_read_feature(self, name)

    def write_feature(self, name: str, value: object) -> None:
        curated_write_feature(self, name, value)

    def execute_command(self, name: str) -> None:
        raise BackendError("command execution is not supported on this backend")

    # config_values / load_params / save_params and the software-trigger chain
    # (enable_frame_trigger / set_trigger_source / begin_software_trigger_preview
    # / trigger_once / begin_freerun / _enable_trigger_overlap) are inherited
    # unchanged from GenICamTriggerConfig. load_params captures the original
    # TriggerSource via the shared _get_enum, which pycameleon routes through
    # _read_enum_opt (same lock-guarded read).

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        # Free-run: the camera streams continuously, so receive the next frame
        # without waiting on / executing a software trigger. The receive is
        # timeout_ms-bounded so a free-run source that stops (or a mid-record
        # unplug) can't park the grab thread — and _lock — forever.
        with self._lock:
            if not self._grabbing or self._receiver is None or self._cam is None:
                return None
            try:
                array = self._receive_bounded(timeout_ms)
            except Exception as e:
                log.debug("receive failed on camera %s: %s", self._serial, e)
                return None
        if array is None:
            return None  # timed out; the grab loop re-checks the stop flag
        out = np.array(array, copy=True) if wants_array() else None
        return (out, 0)

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

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
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
                array = self._receive_bounded(timeout_ms)
            except Exception as e:
                log.debug("receive failed on camera %s: %s", self._serial, e)
                return None
        if array is None:
            return None  # timed out; the grab loop re-checks the stop flag
        # Own the frame; skip the copy when the display slot is full (preview).
        out = np.array(array, copy=True) if wants_array() else None
        return (out, 0)

    def _receive_bounded(self, timeout_ms: int):
        """Receive the next frame, or return None after *timeout_ms*.

        pycameleon's ``receive()`` is a blocking call with **no timeout**: a lost
        software-triggered frame, a mid-record USB unplug, or a free-run source
        that stops would park the grab thread inside ``receive()`` forever — with
        ``_lock`` held — wedging ``stop_grab``/``join``/``close`` and any
        concurrent node access. Instead await the non-blocking ``receive_async``
        under a ``timeout_ms`` deadline (:func:`asyncio.wait_for`) and return None
        on timeout, so the grab loop re-checks :meth:`is_grabbing` / the stop flag
        and can exit. Mirrors the bounded-fetch design of the harvesters backend
        (see ``harvesters._fetch_frame``). Called only under ``_lock`` from the
        grab thread, so the reused event loop is single-threaded.
        """
        loop = self._recv_loop
        if loop is None:
            loop = self._recv_loop = asyncio.new_event_loop()

        async def _await_frame():
            return await self._cam.receive_async(self._receiver)

        try:
            return loop.run_until_complete(
                asyncio.wait_for(_await_frame(), max(timeout_ms, 0) / 1000.0)
            )
        except TimeoutError:
            return None


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
