"""pycameleon backend: USB3 Vision over libusb, the cascade's always-present floor.

pycameleon (a PyO3 binding of the Rust ``cameleon`` crate) needs no vendor SDK,
so it is a core dependency. It exposes node values only (no bounds, units or
writability) and no frame timestamp, so its frames are host-clocked. It borrows
the camera exclusively: ``execute`` on one thread while ``receive`` runs on
another raises "Already borrowed", so every device call holds ``_lock``.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from octacam.cameras._genicam_config import GenICamTriggerConfig
from octacam.cameras.base import (
    INT_PARAMS,
    PARAM_NODES,
    BackendError,
    CameraBackend,
    FeatureInfo,
    Frame,
    NodeInfo,
    curated_list_features,
    curated_read_feature,
    curated_write_feature,
)
from octacam.cameras.registry import BackendSpec, BackendUnavailable, select_serials

try:  # pycameleon is a core dep, but keep the import defensive like the others.
    import pycameleon
except ImportError:  # pragma: no cover - pycameleon ships in core
    pycameleon = None

log = logging.getLogger("octacam")

# Payload buffers: one frame per software trigger, so a small pool is plenty.
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


class PycameleonBackend(GenICamTriggerConfig, CameraBackend):
    """A single USB3-Vision camera driven through pycameleon/libusb."""

    extension = "txt"

    def __init__(self, cam):
        self._cam: Any = cam  # a PyCameleonCamera; None once closed
        super().__init__(_read_serial(cam))
        self._open = False
        self._original_trigger_source: str | None = None
        # The GenApi XML, read from the camera on the first open; a re-open loads it.
        self._context_xml: str | None = None
        self._receiver = None
        # Drives the bounded receive_async (_receive_bounded); closed by close().
        self._recv_loop: asyncio.AbstractEventLoop | None = None
        # Every device call holds it. Reentrant: open() sets Mono8 through _set_enum.
        self._lock = threading.RLock()

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
                raise BackendError(str(e)) from e
            self._open = True
            # Mono8, so receive() yields the 2-D uint8 array the GRAY8 writer takes.
            try:
                self._set_enum("PixelFormat", "Mono8")
            except BackendError as e:
                log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)

    def close(self) -> None:
        if self._cam is None:  # a second close() is a no-op
            return
        self.stop_grab()
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

    def width(self) -> int:
        with self._lock:
            return int(self._cam.read_integer("Width"))

    def height(self) -> int:
        with self._lock:
            return int(self._cam.read_integer("Height"))

    # ----------------------------------------------------- node plumbing

    def _set_enum(self, node: str, value: str) -> None:
        with self._lock:
            try:
                self._cam.write_enum_as_str(node, value)
            except Exception as e:
                raise BackendError(str(e)) from e

    def _get_enum(self, node: str) -> str | None:
        with self._lock:
            try:
                return self._cam.read_enum_as_str(node)
            except Exception:
                return None

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
                if name in INT_PARAMS:
                    value: float | int = int(self._cam.read_integer(sfnc))
                else:
                    value = float(self._cam.read_float(sfnc))
            except Exception as e:
                raise BackendError(str(e)) from e
        # The only bounds pycameleon can read: WidthMax/HeightMax.
        maximum: float | None = None
        if name == "width":
            maximum = self._get_number("WidthMax", True)
        elif name == "height":
            maximum = self._get_number("HeightMax", True)
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
                if name in INT_PARAMS:
                    self._cam.write_integer(sfnc, int(value))
                else:
                    self._cam.write_float(sfnc, float(value))
            except Exception as e:
                raise BackendError(str(e)) from e

    # No node-map walk: the six curated PARAM_NODES.
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

    # ------------------------------------------------------------- grabbing

    def _start_streaming(self) -> None:
        with self._lock:
            try:
                self._receiver = self._cam.start_streaming(_STREAM_CAPACITY)
            except Exception as e:
                log.error("Failed to start streaming on camera %s: %s", self._serial, e)
                raise BackendError(str(e)) from e
        self.trigger.begin_grab()

    def start_grab_preview(self) -> None:
        self._start_streaming()

    def start_grab_record(self) -> bool:
        self._start_streaming()
        return True

    def stop_grab(self) -> None:
        if not self.trigger.end_grab():
            return
        with self._lock:
            if self._receiver is not None:
                try:
                    self._cam.stop_streaming()
                except Exception:
                    pass
                self._receiver = None

    def _streaming(self) -> bool:
        """A live grab with a receiver (caller holds ``_lock``)."""
        return self.trigger.grabbing and self._receiver is not None and self._cam is not None

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        # CameraBackend.retrieve, with the fire and the receive under one hold of
        # the device lock (exclusive borrow); the copy runs after it.
        fire = self.trigger.claim(timeout_ms)
        if fire is None:
            return None
        with self._lock:
            if not self._streaming():
                return None
            if fire and not self._fire_trigger():
                self.trigger.unfired()
                return None
            array = self._receive(
                self.trigger.fetch_timeout_ms(timeout_ms), answers_trigger=True
            )
        return self._frame(array, wants_array, answers_trigger=True)

    def _fire_trigger(self) -> bool:
        with self._lock:
            try:
                self._cam.execute("TriggerSoftware")
            except Exception as e:
                log.debug("trigger failed on camera %s: %s", self._serial, e)
                return False
            return True

    def _fetch(
        self, timeout_ms: int, wants_array: Callable[[], bool], answers_trigger: bool
    ) -> Frame | None:
        array = self._receive(timeout_ms, answers_trigger)
        return self._frame(array, wants_array, answers_trigger)

    def _receive(self, timeout_ms: int, answers_trigger: bool):
        """The next payload under ``_lock``, or None (timed out, or rejected)."""
        with self._lock:
            if not self._streaming():
                return None
            try:
                return self._receive_bounded(timeout_ms)
            except Exception as e:
                # A payload cameleon rejected (short, a trailer error) is this
                # backend's incomplete image: it answers its trigger.
                log.debug("receive failed on camera %s: %s", self._serial, e)
                if answers_trigger:
                    self.trigger.answered()
                return None

    def _frame(
        self, array, wants_array: Callable[[], bool], answers_trigger: bool
    ) -> Frame | None:
        """A received payload as an owned frame, off ``_lock``."""
        if array is None:
            return None  # timed out; the grab loop re-checks the stop flag
        if answers_trigger:
            self.trigger.answered()
        out = np.array(array, copy=True) if wants_array() else None
        return (out, 0)

    def _receive_bounded(self, timeout_ms: int):
        """The next frame, or None after ``timeout_ms``.

        pycameleon's ``receive()`` has no timeout: a lost frame or an unplug
        would park the grab thread in it forever, holding ``_lock`` and wedging
        stop and close. Grab thread only, under ``_lock``, so the reused event
        loop is single-threaded.
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
        except (TimeoutError, asyncio.TimeoutError):  # distinct before Python 3.11
            return None


def _read_serial(cam) -> str:
    """Best-effort serial from a PyCameleonCamera's info() descriptor."""
    try:
        info = cam.info()
        serial = info.get("serial_number") if isinstance(info, dict) else None
    except Exception:
        serial = None
    return str(serial) if serial else ""


def read_model(cam) -> str | None:
    """Best-effort model name from ``info()``, read without opening (for doctor)."""
    try:
        info = cam.info()
        model = info.get("model_name") if isinstance(info, dict) else None
    except Exception:
        model = None
    return str(model) if model else None


def enumerate_pycameleon(requested_serials: list[str] | None = None):
    """``[(serial, PyCameleonCamera)]`` in :func:`select_serials` order."""
    p = _pycameleon()
    cams = p.enumerate_cameras()
    if not cams:
        return []
    log.debug("pycameleon enumerated %d camera(s)", len(cams))

    by_serial: dict[str, object] = {}
    for cam in cams:
        serial = _read_serial(cam)
        if serial:
            by_serial[serial] = cam
    return [
        (serial, by_serial[serial])
        for serial in select_serials(by_serial, requested_serials)
    ]


SPEC = BackendSpec(
    enumerate_pycameleon,
    PycameleonBackend,
    ensure_available=ensure_available,
    read_model=read_model,
)
