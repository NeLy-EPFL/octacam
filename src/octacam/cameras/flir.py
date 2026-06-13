"""FLIR / Teledyne Spinnaker (PySpin) camera backend.

Implements the :class:`CameraBackend` seam over a PySpin ``CameraPtr``. PySpin is
NOT on PyPI — it ships as a wheel with the Spinnaker SDK installer — so it is an
optional dependency: this module imports it defensively and surfaces a clean
:class:`BackendUnavailable` (never a raw ``ImportError``) when it is missing.

Mapping notes vs. the Basler backend:

* Node names are the standard SFNC ones (ExposureTime, Gain, Width, Height,
  OffsetX, OffsetY), so :data:`PARAM_NODES` is reused; Width/Height/Offset* are
  integer nodes and ExposureTime/Gain are float nodes.
* Spinnaker has no pylon ``GrabStrategy`` enum; the equivalent is acquisition
  mode Continuous plus a stream buffer-handling mode — ``NewestOnly`` for
  preview (≈ LatestImageOnly) and ``OldestFirst`` for recording (≈ OneByOne).
* There is no ``FeaturePersistence``/``.pfs``; parameters persist as JSON
  (``extension = "json"``), the same scheme the fake backend uses.
* The ``System`` singleton must be released exactly once, after every camera is
  de-initialized; that teardown is centralized in :func:`teardown`, which
  :class:`~octacam.cameras.system.CameraSystem` calls via the registry.
"""

import json
import logging
from typing import Any

from octacam.cameras.base import (
    PARAM_NODES,
    BackendError,
    Frame,
    NodeInfo,
    snap_value,
)
from octacam.cameras.registry import BackendUnavailable

try:  # PySpin ships with the Spinnaker SDK and is not pip-installable.
    import PySpin  # type: ignore
except ImportError:  # pragma: no cover - exercised only on a non-FLIR box
    PySpin = None

log = logging.getLogger("octacam")

TRIGGER_READY_TIMEOUT_MS = 1000

# Spinnaker node interface types differ per parameter; the rest are floats.
_INT_PARAMS = frozenset({"width", "height", "offset_x", "offset_y"})

# The System singleton and its camera list are held for the whole session and
# released exactly once in teardown(), after every camera has been de-inited.
_system = None
_cam_list = None


def _spin():
    """Return the PySpin module, or raise a clean BackendUnavailable."""
    if PySpin is None:
        raise BackendUnavailable(
            "flir",
            "the Spinnaker SDK and its PySpin wheel must be installed "
            "(they are not on PyPI; see the README)",
        )
    return PySpin


def ensure_available() -> None:
    """Raise BackendUnavailable if PySpin/Spinnaker is not installed."""
    _spin()


def _safe(getter):
    """Best-effort node attribute read (Min/Max/Inc/Unit); None on failure."""
    try:
        return getter()
    except Exception:
        return None


class FlirBackend:
    """A single FLIR camera, driven through PySpin."""

    extension = "json"

    def __init__(self, cam: Any):
        # PySpin's CameraPtr is untyped here (the SDK has no stubs); typing it
        # Any keeps the close()-sets-None lifecycle from tripping the checker.
        self._cam: Any = cam
        self._serial = _read_serial(cam)
        self._original_trigger_source: str | None = None

    @property
    def serial_number(self) -> str:
        return self._serial

    # ------------------------------------------------------------- lifecycle

    def open(self) -> None:
        self._cam.Init()
        # Force monochrome so GetNDArray() yields a 2-D uint8 array matching the
        # GRAY8 video writer.
        try:
            self._set_enum("PixelFormat", "Mono8")
        except BackendError as e:
            log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)

    def close(self) -> None:
        cam = self._cam
        if cam is None:
            return
        try:
            if cam.IsStreaming():
                cam.EndAcquisition()
        except Exception:
            pass
        try:
            if cam.IsInitialized():
                cam.DeInit()
        except Exception:
            pass
        # Drop the CameraPtr so the System can be released in teardown().
        self._cam = None

    def is_open(self) -> bool:
        return self._cam is not None and self._cam.IsInitialized()

    def is_grabbing(self) -> bool:
        return self._cam is not None and self._cam.IsStreaming()

    def width(self) -> int:
        return int(self._int_node("Width").GetValue())

    def height(self) -> int:
        return int(self._int_node("Height").GetValue())

    # ----------------------------------------------------- node plumbing

    def _nodemap(self):
        return self._cam.GetNodeMap()

    def _int_node(self, sfnc: str):
        return _spin().CIntegerPtr(self._nodemap().GetNode(sfnc))

    def _typed_node(self, name: str):
        spin = _spin()
        raw = self._nodemap().GetNode(PARAM_NODES[name])
        return spin.CIntegerPtr(raw) if name in _INT_PARAMS else spin.CFloatPtr(raw)

    def _set_enum(self, name: str, value: str) -> None:
        spin = _spin()
        node = spin.CEnumerationPtr(self._nodemap().GetNode(name))
        if not spin.IsAvailable(node) or not spin.IsWritable(node):
            raise BackendError(f"enumeration {name} is not writable")
        entry = node.GetEntryByName(value)
        if not spin.IsAvailable(entry) or not spin.IsReadable(entry):
            raise BackendError(f"enumeration {name} has no entry {value!r}")
        node.SetIntValue(entry.GetValue())

    def _get_enum(self, name: str) -> str | None:
        spin = _spin()
        node = spin.CEnumerationPtr(self._nodemap().GetNode(name))
        if not spin.IsAvailable(node) or not spin.IsReadable(node):
            return None
        entry = node.GetCurrentEntry()
        return entry.GetSymbolic() if entry is not None else None

    # ----------------------------------------------------- sensor parameters

    def read_node(self, name: str) -> NodeInfo:
        spin = _spin()
        node = self._typed_node(name)
        if not spin.IsAvailable(node) or not spin.IsReadable(node):
            raise BackendError(f"node {PARAM_NODES[name]} is not readable")
        try:
            value = node.GetValue()
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e
        return NodeInfo(
            value=value,
            min=_safe(node.GetMin),
            max=_safe(node.GetMax),
            inc=_safe(node.GetInc),
            unit=_safe(node.GetUnit),
            writable=spin.IsWritable(node),
        )

    def write_node(self, name: str, value: float) -> None:
        spin = _spin()
        node = self._typed_node(name)
        if not spin.IsAvailable(node) or not spin.IsWritable(node):
            raise BackendError(f"node {PARAM_NODES[name]} is not writable")
        try:
            node.SetValue(int(value) if name in _INT_PARAMS else float(value))
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e

    def load_params(self, config_str: str) -> None:
        if config_str:
            try:
                data = json.loads(config_str)
            except (ValueError, TypeError) as e:
                raise BackendError(f"invalid FLIR parameters: {e}") from e
            if not isinstance(data, dict):
                raise BackendError("invalid FLIR parameters: expected an object")
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
        self._original_trigger_source = self._get_enum("TriggerSource")

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

    def enable_frame_trigger(self) -> None:
        if not self.is_open():
            return
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")

    def set_trigger_source(self, use_software: bool) -> None:
        if not self.is_open():
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

    def trigger_once(self) -> None:
        if not self.is_grabbing():
            return
        spin = _spin()
        node = spin.CCommandPtr(self._nodemap().GetNode("TriggerSoftware"))
        try:
            node.Execute()
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e

    # ------------------------------------------------------------- grabbing

    def _begin_acquisition(self, buffer_mode: str) -> None:
        spin = _spin()
        try:
            self._set_enum("AcquisitionMode", "Continuous")
            # Buffer handling lives on the transport-layer stream nodemap.
            snodemap = self._cam.GetTLStreamNodeMap()
            handling = spin.CEnumerationPtr(
                snodemap.GetNode("StreamBufferHandlingMode")
            )
            entry = handling.GetEntryByName(buffer_mode)
            if spin.IsAvailable(entry) and spin.IsReadable(entry):
                handling.SetIntValue(entry.GetValue())
            self._cam.BeginAcquisition()
        except spin.SpinnakerException as e:
            # Name the camera (mirrors the Basler "insufficient resources" hint).
            log.error(
                "Failed to start streaming on camera %s: %s", self._serial, e
            )
            raise BackendError(str(e)) from e

    def start_grab_preview(self) -> None:
        self._begin_acquisition("NewestOnly")

    def start_grab_record(self) -> bool:
        # Spinnaker has no WaitForFrameTriggerReady; the camera arms on the
        # first software trigger, so report ready once acquisition has begun.
        self._begin_acquisition("OldestFirst")
        return True

    def stop_grab(self) -> None:
        if self._cam is not None and self._cam.IsStreaming():
            try:
                self._cam.EndAcquisition()
            except Exception:
                pass

    def retrieve(self, timeout_ms: int, wants_array) -> Frame | None:
        spin = _spin()
        try:
            image = self._cam.GetNextImage(timeout_ms)
        except spin.SpinnakerException:
            return None  # timeout: the analogue of pylon's empty result
        try:
            if image.IsIncomplete():
                return None
            timestamp = image.GetTimeStamp()
            array = None
            if wants_array():
                arr = image.GetNDArray()
                if arr.ndim != 2:
                    log.warning(
                        "Camera %s delivered a non-mono frame; skipping",
                        self._serial,
                    )
                    return None
                array = arr.copy()  # own it; the SDK buffer is recycled on Release
            return (array, timestamp)
        finally:
            image.Release()


def _read_serial(cam) -> str:
    spin = _spin()
    nodemap = cam.GetTLDeviceNodeMap()
    node = spin.CStringPtr(nodemap.GetNode("DeviceSerialNumber"))
    if spin.IsAvailable(node) and spin.IsReadable(node):
        return node.GetValue()
    return cam.GetUniqueID()


def enumerate_flir(requested_serials: list[str] | None = None):
    """Return ``[(serial, CameraPtr), ...]`` for the requested FLIR cameras.

    Holds the System singleton and camera list for the session (released in
    :func:`teardown`). Mirrors the Basler enumeration: all detected cameras
    (sorted) when nothing is requested, else the listed serials in order with a
    warning for any not connected.
    """
    spin = _spin()
    global _system, _cam_list
    _system = spin.System.GetInstance()
    _cam_list = _system.GetCameras()
    count = _cam_list.GetSize()
    if count == 0:
        teardown()
        return []
    log.info("Detected %d camera(s)", count)

    by_serial: dict[str, object] = {}
    detected: list[str] = []
    for i in range(count):
        cam = _cam_list.GetByIndex(i)
        serial = _read_serial(cam)
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


def teardown() -> None:
    """Release the Spinnaker System singleton, once, after cameras are closed.

    Called by CameraSystem.close() through the registry. Clearing the camera
    list and releasing the instance must happen after every CameraPtr has been
    de-initialized and dropped (FlirBackend.close drops its reference), or
    Spinnaker reports cameras still in use.
    """
    global _system, _cam_list
    if _cam_list is not None:
        try:
            _cam_list.Clear()
        except Exception:
            pass
        _cam_list = None
    if _system is not None:
        try:
            _system.ReleaseInstance()
        except Exception:
            pass
        _system = None
