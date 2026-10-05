"""FLIR / Teledyne Spinnaker backend over PySpin.

PySpin ships with the Spinnaker SDK (it is not on PyPI); without it this tier
raises :class:`BackendUnavailable`. Acquisition is Continuous with the stream's
NewestOnly buffering for preview and OldestFirst for recording. The ``System``
singleton is released once, after every camera is de-initialized, by
:func:`teardown`.
"""

import atexit
import gc
import logging
import time
from collections.abc import Callable
from typing import Any

from octacam.cameras._genicam_config import (
    MIN_STREAM_BUFFERS,
    RECORD_STREAM_BUFFERS,
    GenICamTriggerConfig,
    fewer_stream_buffers,
)
from octacam.cameras.base import (
    PARAM_NODES,
    BackendError,
    CameraBackend,
    FeatureInfo,
    Frame,
    NodeInfo,
    coerce_bool,
)
from octacam.cameras.registry import BackendSpec, BackendUnavailable

try:  # PySpin ships with the Spinnaker SDK and is not pip-installable.
    import PySpin  # type: ignore
except ImportError:  # pragma: no cover - exercised only on a non-FLIR box
    PySpin = None

log = logging.getLogger("octacam")

# TL-stream counters a recording reports (lost at the host, dropped from the
# output queue, delivered incomplete, delivered at all).
STREAM_STATISTICS = (
    "StreamLostFrameCount",
    "StreamDroppedFrameCount",
    "StreamIncompleteFrameCount",
    "StreamDeliveredFrameCount",
)
# A saturated USB bus delivers incomplete images continuously: a grab logs its
# first, then its running total at most this often (a preview's at debug).
INCOMPLETE_REPORT_INTERVAL_S = 10.0


def _set_buffer_handling(spin, snodemap, mode: str, serial: str) -> None:
    """Best-effort: the stream's buffer handling (NewestOnly or OldestFirst)."""
    try:
        handling = spin.CEnumerationPtr(snodemap.GetNode("StreamBufferHandlingMode"))
        entry = handling.GetEntryByName(mode)
        if spin.IsAvailable(entry) and spin.IsReadable(entry):
            handling.SetIntValue(entry.GetValue())
    except spin.SpinnakerException as e:
        log.debug("Could not set buffer mode %s on camera %s: %s", mode, serial, e)


def _set_stream_buffers(spin, snodemap, buffers: int, serial: str) -> None:
    """Best-effort: a manual stream buffer count of ``buffers`` (≤ the max)."""
    try:
        mode = spin.CEnumerationPtr(snodemap.GetNode("StreamBufferCountMode"))
        manual = mode.GetEntryByName("Manual")
        if spin.IsAvailable(manual) and spin.IsWritable(mode):
            mode.SetIntValue(manual.GetValue())
        count = spin.CIntegerPtr(snodemap.GetNode("StreamBufferCountManual"))
        if spin.IsAvailable(count) and spin.IsWritable(count):
            count.SetValue(int(min(buffers, count.GetMax())))
    except spin.SpinnakerException as e:
        log.debug("Could not size the stream buffers of camera %s: %s", serial, e)


# Integer SFNC nodes; the rest of PARAM_NODES are floats.
_INT_PARAMS = frozenset({"width", "height", "offset_x", "offset_y"})

# The System singleton and its camera list, held until teardown().
_system = None
_cam_list = None


def _spin():
    """Return the PySpin module, or raise a clean BackendUnavailable."""
    if PySpin is None:
        raise BackendUnavailable(
            "flir",
            "PySpin (the Spinnaker SDK's Python binding) isn't importable — not on "
            "PyPI and easily pruned by `uv sync`; reinstall the PySpin wheel to "
            "restore this tier. FLIR cameras still work via the ctypes `spinnaker` "
            "tier when libSpinnaker_C.so is present.",
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


# --- Node-map walk (Camera tab) -----------------------------------------------
# The maps are built from the passed-in module: PySpin may be absent at import.
def _iface_kind(spin, itype) -> str | None:
    """Map a PySpin interface type to a FeatureInfo widget kind (None = skip)."""
    return {
        spin.intfIInteger: "int",
        spin.intfIFloat: "float",
        spin.intfIBoolean: "bool",
        spin.intfIEnumeration: "enum",
        spin.intfIString: "string",
        spin.intfICommand: "command",
        spin.intfICategory: "category",
    }.get(itype)


def _visibility_name(spin, node) -> str:
    try:
        return {
            spin.Beginner: "beginner",
            spin.Expert: "expert",
            spin.Guru: "guru",
            spin.Invisible: "invisible",
        }.get(node.GetVisibility(), "beginner")
    except Exception:
        return "beginner"


def _flir_enum_entries(spin, enum) -> list[dict] | None:
    try:
        out = []
        for entry in enum.GetEntries():
            try:
                symbolic = spin.CEnumEntryPtr(entry).GetSymbolic()
            except Exception:
                continue
            if not symbolic:
                continue
            try:
                available = spin.IsAvailable(entry)
            except Exception:
                available = True
            out.append({"value": symbolic, "display": symbolic, "available": available})
        return out or None
    except Exception:
        return None


def _flir_feature(spin, node) -> FeatureInfo | None:
    """Build a FeatureInfo from a PySpin INode (None = skip category/non-feature)."""
    kind = _iface_kind(spin, node.GetPrincipalInterfaceType())
    if kind is None or kind == "category":
        return None
    name = node.GetName()
    readable = spin.IsReadable(node)
    feature = FeatureInfo(
        name=name,
        display_name=_safe(node.GetDisplayName) or name,
        type=kind,
        readable=readable,
        writable=spin.IsWritable(node),
        visibility=_visibility_name(spin, node),
        tooltip=(_safe(node.GetToolTip) or _safe(node.GetDescription) or None),
    )
    if kind in ("int", "float"):
        typed = spin.CIntegerPtr(node) if kind == "int" else spin.CFloatPtr(node)
        if readable:
            feature.value = _safe(typed.GetValue)
        feature.min = _safe(typed.GetMin)
        feature.max = _safe(typed.GetMax)
        feature.inc = _safe(typed.GetInc)
        feature.unit = _safe(typed.GetUnit) or None
    elif kind == "bool":
        if readable:
            feature.value = _safe(spin.CBooleanPtr(node).GetValue)
    elif kind == "enum":
        enum = spin.CEnumerationPtr(node)
        if readable:
            entry = _safe(enum.GetCurrentEntry)
            feature.value = _safe(entry.GetSymbolic) if entry is not None else None
        feature.entries = _flir_enum_entries(spin, enum)
    elif kind == "string" and readable:
        feature.value = _safe(spin.CStringPtr(node).GetValue)
    # command: no value
    return feature


class FlirBackend(GenICamTriggerConfig, CameraBackend):
    """A single FLIR camera, driven through PySpin."""

    extension = "txt"

    def __init__(self, cam: Any):
        # Any: PySpin has no stubs, and close() sets it None.
        self._cam: Any = cam
        super().__init__(_read_serial(cam))
        self._original_trigger_source: str | None = None
        # Incomplete images, discarded (never written) but counted for the summary.
        self._incomplete_images = 0
        # The current grab's share, for the rate-limited log (_count_incomplete).
        self._grab_is_record = False
        self._grab_incomplete = 0
        self._grab_incomplete_logged = 0
        self._incomplete_logged_at = 0.0

    # ------------------------------------------------------------- lifecycle

    def open(self) -> None:
        spin = _spin()
        try:
            self._cam.Init()
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e
        # Mono8, so GetNDArray() yields the 2-D uint8 array the GRAY8 writer takes.
        try:
            self._set_enum("PixelFormat", "Mono8")
        except BackendError as e:
            log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)
        self._maximize_link_throughput()

    def _maximize_link_throughput(self) -> None:
        """Best-effort: raise DeviceLinkThroughputLimit to its max, once at open.

        FLIR ships it capped: on the GS3, 350.6 of 384.4 MB/s, an ~84 vs ~92 fps
        transfer ceiling at 2048² Mono8 (measured ~82 -> ~90 fps at 100 µs
        exposure). A software-triggered GS3 frame costs transfer plus exposure
        serially, so a long exposure stays exposure-bound (~65 fps at 4 ms).
        """
        spin = _spin()
        node = spin.CIntegerPtr(self._nodemap().GetNode("DeviceLinkThroughputLimit"))
        if not spin.IsAvailable(node) or not spin.IsWritable(node):
            return
        try:
            node_max = node.GetMax()
            if node.GetValue() < node_max:
                node.SetValue(node_max)
                log.debug(
                    "Camera %s: DeviceLinkThroughputLimit -> %d (max)",
                    self._serial,
                    node_max,
                )
        except spin.SpinnakerException as e:
            log.debug(
                "Could not raise DeviceLinkThroughputLimit on camera %s: %s",
                self._serial,
                e,
            )

    def close(self) -> None:
        cam = self._cam
        if cam is None:
            return
        self.trigger.end_grab()
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
        self._cam = None  # so teardown() can release the System

    def is_open(self) -> bool:
        return self._cam is not None and self._cam.IsInitialized()

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

    # Each setter wraps SpinnakerException in BackendError: a writable node can
    # still refuse a value (see _genicam_config).
    def _set_enum(self, name: str, value: str) -> None:
        spin = _spin()
        node = spin.CEnumerationPtr(self._nodemap().GetNode(name))
        if not spin.IsAvailable(node) or not spin.IsWritable(node):
            raise BackendError(f"enumeration {name} is not writable")
        try:
            entry = node.GetEntryByName(value)
            if not spin.IsAvailable(entry) or not spin.IsReadable(entry):
                raise BackendError(f"enumeration {name} has no entry {value!r}")
            node.SetIntValue(entry.GetValue())
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e

    def _get_enum(self, name: str) -> str | None:
        spin = _spin()
        node = spin.CEnumerationPtr(self._nodemap().GetNode(name))
        if not spin.IsAvailable(node) or not spin.IsReadable(node):
            return None
        try:
            entry = node.GetCurrentEntry()
        except spin.SpinnakerException:
            return None
        return entry.GetSymbolic() if entry is not None else None

    def _set_bool(self, name: str, value: bool) -> None:
        spin = _spin()
        node = spin.CBooleanPtr(self._nodemap().GetNode(name))
        if not spin.IsAvailable(node) or not spin.IsWritable(node):
            raise BackendError(f"boolean {name} is not writable")
        try:
            node.SetValue(bool(value))
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e

    def _get_bool(self, name: str) -> bool | None:
        spin = _spin()
        node = spin.CBooleanPtr(self._nodemap().GetNode(name))
        if not spin.IsAvailable(node) or not spin.IsReadable(node):
            return None
        try:
            return bool(node.GetValue())
        except spin.SpinnakerException:
            return None

    def _set_number(self, name: str, value: float, is_int: bool) -> None:
        spin = _spin()
        raw = self._nodemap().GetNode(name)
        node = spin.CIntegerPtr(raw) if is_int else spin.CFloatPtr(raw)
        if not spin.IsAvailable(node) or not spin.IsWritable(node):
            raise BackendError(f"node {name} is not writable")
        try:
            node.SetValue(int(value) if is_int else float(value))
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e

    def _get_number(self, name: str, is_int: bool) -> float | int | None:
        spin = _spin()
        raw = self._nodemap().GetNode(name)
        node = spin.CIntegerPtr(raw) if is_int else spin.CFloatPtr(raw)
        if not spin.IsAvailable(node) or not spin.IsReadable(node):
            return None
        try:
            return node.GetValue()
        except spin.SpinnakerException:
            return None

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

    def list_features(self) -> list[FeatureInfo]:
        if self._cam is None or not self._cam.IsInitialized():
            return []
        spin = _spin()
        try:
            root = spin.CCategoryPtr(self._nodemap().GetNode("Root"))
        except spin.SpinnakerException:
            return []
        out: list[FeatureInfo] = []
        self._walk(spin, root, "", out, set())
        return out

    def _walk(self, spin, category, path: str, out: list, seen: set) -> None:
        """Depth-first walk of the GenApi category tree, collecting features."""
        try:
            features = category.GetFeatures()
        except spin.SpinnakerException:
            return
        for node in features:
            try:
                if not spin.IsAvailable(node) or not node.IsFeature():
                    continue
                if _visibility_name(spin, node) not in ("beginner", "expert", "guru"):
                    continue
                if node.GetPrincipalInterfaceType() == spin.intfICategory:
                    label = _safe(node.GetDisplayName) or _safe(node.GetName) or path
                    self._walk(spin, spin.CCategoryPtr(node), label, out, seen)
                    continue
                name = node.GetName()
                if name in seen:
                    continue
                seen.add(name)
                feature = _flir_feature(spin, node)
                if feature is not None:
                    feature.category = path or "Other"
                    out.append(feature)
            except spin.SpinnakerException as e:
                log.debug("Skipping node during feature walk: %s", e)

    def read_feature(self, name: str) -> FeatureInfo:
        spin = _spin()
        try:
            node = self._nodemap().GetNode(name)
        except spin.SpinnakerException as e:
            raise BackendError(f"no such node: {name}") from e
        if node is None or not spin.IsAvailable(node):
            raise BackendError(f"no such node: {name}")
        feature = _flir_feature(spin, node)
        if feature is None:
            raise BackendError(f"node {name} is not an editable feature")
        return feature

    def write_feature(self, name: str, value: object) -> None:
        spin = _spin()
        try:
            node = self._nodemap().GetNode(name)
        except spin.SpinnakerException as e:
            raise BackendError(f"no such node: {name}") from e
        if node is None:  # GetNode returns None (not a raise) for an absent name
            raise BackendError(f"no such node: {name}")
        kind = _iface_kind(spin, node.GetPrincipalInterfaceType())
        try:
            if kind == "int":
                typed = spin.CIntegerPtr(node)
                node_min = _safe(typed.GetMin)
                node_inc = _safe(typed.GetInc)
                snapped = int(round(float(value)))  # type: ignore[arg-type]
                if node_inc:
                    base = node_min if node_min is not None else 0
                    snapped = int(base + round((snapped - base) / node_inc) * node_inc)
                typed.SetValue(snapped)
            elif kind == "float":
                spin.CFloatPtr(node).SetValue(float(value))  # type: ignore[arg-type]
            elif kind == "bool":
                spin.CBooleanPtr(node).SetValue(coerce_bool(value))
            elif kind == "enum":
                spin.CEnumerationPtr(node).FromString(str(value))
            elif kind == "string":
                spin.CStringPtr(node).SetValue(str(value))
            else:
                raise BackendError(f"node {name} is not writable ({kind})")
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e

    def execute_command(self, name: str) -> None:
        spin = _spin()
        try:
            node = self._nodemap().GetNode(name)
        except spin.SpinnakerException as e:
            raise BackendError(f"no such node: {name}") from e
        if node is None:  # GetNode returns None (not a raise) for an absent name
            raise BackendError(f"no such node: {name}")
        if node.GetPrincipalInterfaceType() != spin.intfICommand:
            raise BackendError(f"node {name} is not a command")
        try:
            spin.CCommandPtr(node).Execute()
        except spin.SpinnakerException as e:
            raise BackendError(str(e)) from e

    # ------------------------------------------------------------- grabbing

    def stream_statistics(self) -> dict[str, int]:
        """Spinnaker's transport counters (see STREAM_STATISTICS) plus the
        incomplete images this backend discarded."""
        spin = _spin()
        out = {"IncompleteImagesDiscarded": self._incomplete_images}
        if self._cam is None:
            return out
        try:
            snodemap = self._cam.GetTLStreamNodeMap()
        except spin.SpinnakerException:
            return out
        for name in STREAM_STATISTICS:
            node = spin.CIntegerPtr(snodemap.GetNode(name))
            try:
                if spin.IsAvailable(node) and spin.IsReadable(node):
                    out[name] = int(node.GetValue())
            except spin.SpinnakerException:
                continue
        return out

    def _begin_acquisition(self, buffer_mode: str, buffers: int | None = None) -> None:
        # Only BeginAcquisition is fatal: FLIR defaults to Continuous, and the
        # stream settings only tune its buffering.
        spin = _spin()
        try:
            self._set_enum("AcquisitionMode", "Continuous")
        except BackendError as e:
            log.debug("Could not set AcquisitionMode on camera %s: %s", self._serial, e)
        try:
            snodemap = self._cam.GetTLStreamNodeMap()
        except spin.SpinnakerException as e:
            log.debug("No stream node map on camera %s: %s", self._serial, e)
            snodemap = None
        if snodemap is not None:
            _set_buffer_handling(spin, snodemap, buffer_mode, self._serial)
        while True:
            if buffers and snodemap is not None:
                _set_stream_buffers(spin, snodemap, buffers, self._serial)
            try:
                self._cam.BeginAcquisition()
                break
            except spin.SpinnakerException as e:
                if buffers and snodemap is not None and buffers > MIN_STREAM_BUFFERS:
                    buffers = fewer_stream_buffers(buffers, self._serial, e)
                    continue
                log.error("Failed to start streaming on camera %s: %s", self._serial, e)
                raise BackendError(str(e)) from e
        self.trigger.begin_grab()

    def start_grab_preview(self) -> None:
        self._begin_incomplete_log(record=False)
        self._begin_acquisition("NewestOnly")

    def start_grab_record(self) -> bool:
        # Spinnaker has no ready gate: ready once acquisition began (a GS3 still
        # ignores its first triggers, so the recording primes the cameras).
        self._begin_incomplete_log(record=True)
        self._begin_acquisition("OldestFirst", RECORD_STREAM_BUFFERS)
        return True

    def _begin_incomplete_log(self, *, record: bool) -> None:
        """Start a grab's incomplete-image log afresh (see _count_incomplete)."""
        self._grab_is_record = record
        self._grab_incomplete = 0
        self._grab_incomplete_logged = 0

    def _count_incomplete(self) -> None:
        """Count a discarded incomplete image, logged rate-limited (see
        INCOMPLETE_REPORT_INTERVAL_S); a warning only in a record grab."""
        self._incomplete_images += 1
        self._grab_incomplete += 1
        now = time.monotonic()
        first = self._grab_incomplete == 1
        if not first and now - self._incomplete_logged_at < INCOMPLETE_REPORT_INTERVAL_S:
            return
        level = logging.WARNING if self._grab_is_record else logging.DEBUG
        if first:
            log.log(
                level,
                "Camera %s delivered an incomplete image; discarded (more in this "
                "grab are totaled at most every %g s)",
                self._serial, INCOMPLETE_REPORT_INTERVAL_S,
            )
        else:
            log.log(
                level,
                "Camera %s: %d incomplete images discarded in this grab (%d since "
                "the last report)",
                self._serial, self._grab_incomplete,
                self._grab_incomplete - self._grab_incomplete_logged,
            )
        self._grab_incomplete_logged = self._grab_incomplete
        self._incomplete_logged_at = now

    def stop_grab(self) -> None:
        self.trigger.end_grab()
        if self._cam is not None and self._cam.IsStreaming():
            try:
                self._cam.EndAcquisition()
            except Exception:
                pass

    def _fire_trigger(self) -> bool:
        spin = _spin()
        try:
            spin.CCommandPtr(self._nodemap().GetNode("TriggerSoftware")).Execute()
        except spin.SpinnakerException:
            return False
        return True

    def _fetch(
        self, timeout_ms: int, wants_array: Callable[[], bool], answers_trigger: bool
    ) -> Frame | None:
        cam = self._cam
        if cam is None:
            return None
        spin = _spin()
        try:
            image = cam.GetNextImage(timeout_ms)
        except spin.SpinnakerException:
            return None  # timeout: the analogue of pylon's empty result
        if answers_trigger:
            self.trigger.answered(_complete_timestamp(image))
        try:
            if image.IsIncomplete():
                self._count_incomplete()
                return None
            timestamp = image.GetTimeStamp()
            array = None
            if wants_array():
                arr = image.GetNDArray()
                # Mono8 only: a Mono16 or Bayer frame is 2-D too, but would corrupt
                # the GRAY8 recording.
                if arr.ndim != 2 or arr.dtype.itemsize != 1:
                    log.warning(
                        "Camera %s delivered a non-Mono8 frame (ndim=%d, dtype=%s);"
                        " skipping",
                        self._serial,
                        arr.ndim,
                        arr.dtype,
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


def read_model(cam) -> str | None:
    """Best-effort ``DeviceModelName`` from the TL device node map, readable
    without Init, so doctor can label a camera in a live session."""
    spin = _spin()
    try:
        nodemap = cam.GetTLDeviceNodeMap()
        node = spin.CStringPtr(nodemap.GetNode("DeviceModelName"))
        if spin.IsAvailable(node) and spin.IsReadable(node):
            return node.GetValue() or None
    except Exception:
        pass
    return None


def enumerate_flir(
    requested_serials: list[str] | None = None, *, warn_missing: bool = True
):
    """``[(serial, CameraPtr)]``: every camera sorted by serial, or the requested
    ones in order. Holds the System until :func:`teardown`."""
    spin = _spin()
    global _system, _cam_list
    # Release a previous enumeration's System (doctor enumerates twice), or it
    # would be orphaned with its references live.
    if _system is not None or _cam_list is not None:
        teardown()
    _system = spin.System.GetInstance()
    _cam_list = _system.GetCameras()
    count = _cam_list.GetSize()
    if count == 0:
        teardown()
        return []
    log.debug("flir enumerated %d camera(s)", count)

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
            if warn_missing:
                log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, cam))
    return out


def _complete_timestamp(image) -> int | None:
    """A complete image's timestamp (ns) for the hand-off; None otherwise."""
    try:
        return None if image.IsIncomplete() else int(image.GetTimeStamp())
    except Exception:
        return None


def teardown() -> None:
    """Release the System singleton, after every camera was closed (else
    Spinnaker reports cameras still in use). Idempotent."""
    global _system, _cam_list
    # A failed start leaves its traceback in a reference cycle that holds the
    # camera's node maps; uncollected, Clear() refuses, and the CameraList's
    # destructor then aborts the process (std::terminate) at exit.
    gc.collect()
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


# For paths that enumerate without a CameraSystem (doctor, an aborted run).
atexit.register(teardown)

SPEC = BackendSpec(
    enumerate_flir,
    FlirBackend,
    ensure_available=ensure_available,
    read_model=read_model,
    teardown=teardown,
)
