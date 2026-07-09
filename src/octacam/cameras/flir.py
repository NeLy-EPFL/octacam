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
* There is no pylon ``FeaturePersistence``/``.pfs``; parameters persist in the
  camera's native GenApi feature-persistence TSV (``extension = "txt"``; see
  :mod:`octacam.cameras._genicam_config`), shared with the Spinnaker-C backend.
* The ``System`` singleton must be released exactly once, after every camera is
  de-initialized; that teardown is centralized in :func:`teardown`, which
  :class:`~octacam.cameras.system.CameraSystem` calls via the registry.
"""

import logging
from typing import Any

from octacam.cameras._genicam_config import (
    apply_config,
    apply_freerun_rate_cap,
    clear_freerun_rate_cap,
    dump_config,
    normalize_trigger_source,
    parse_config,
)
from octacam.cameras._trigger_handoff import SoftwareTriggerHandoff
from octacam.cameras.base import (
    GEOMETRY_FEATURES,
    PARAM_NODES,
    BackendError,
    FeatureInfo,
    Frame,
    NodeInfo,
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


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# --- Full node-map walk (Camera tab) ------------------------------------------
# The PySpin interface-type / visibility constants live on the module, which may
# be absent (cp310-only), so the maps are built lazily from the passed-in module
# rather than at import time (unlike the Basler backend, whose pypylon is always
# present). This mirrors the standalone genicam walk in _genicam_features.py, but
# over PySpin's C++ node wrappers (CIntegerPtr/CEnumerationPtr/... casts) instead.
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


class FlirBackend(SoftwareTriggerHandoff):
    """A single FLIR camera, driven through PySpin."""

    extension = "txt"

    def __init__(self, cam: Any):
        # PySpin's CameraPtr is untyped here (the SDK has no stubs); typing it
        # Any keeps the close()-sets-None lifecycle from tripping the checker.
        self._cam: Any = cam
        self._serial = _read_serial(cam)
        self._original_trigger_source: str | None = None
        # Software-trigger hand-off (shared mixin): trigger_once bumps a counter;
        # the device TriggerSoftware execute moves into retrieve() on the grab
        # thread so the shared trigger timer never blocks on this camera.
        self._init_trigger_handoff()

    @property
    def serial_number(self) -> str:
        return self._serial

    # ------------------------------------------------------------- lifecycle

    def open(self) -> None:
        spin = _spin()
        try:
            self._cam.Init()
        except spin.SpinnakerException as e:
            # Usually the camera is already in use by another process (a second
            # `octacam gui` on the rig). Surface it as a BackendError so the
            # caller reports it cleanly instead of a raw PySpin traceback.
            raise BackendError(str(e)) from e
        # Force monochrome so GetNDArray() yields a 2-D uint8 array matching the
        # GRAY8 video writer.
        try:
            self._set_enum("PixelFormat", "Mono8")
        except BackendError as e:
            log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)
        self._maximize_link_throughput()

    def _maximize_link_throughput(self) -> None:
        """Raise DeviceLinkThroughputLimit to the device max (best-effort).

        FLIR ships this node capped below the sensor's real ceiling — on the
        GS3-U3-41C6NIR it defaults to 350.6 MB/s while the max is 384.4 MB/s. At
        2048² Mono8 that is the difference between an ~83.6 fps and an ~91.6 fps
        USB3 transfer ceiling, so at a short exposure the delivered frame rate
        rises with it (measured on the C-API mirror: ~82 → ~90 fps at 100 µs). A
        long exposure stays exposure-bound regardless — a software-triggered frame
        costs transfer + exposure serially on this CCD, so ~65 fps at 4 ms — and a
        model without the node keeps its default. See spinnaker_c.py for the
        on-rig characterisation. Set once at open so preview and record benefit.
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
        # The hand-off flag is authoritative (see BaslerBackend.is_grabbing):
        # stop_grab flips it and wakes a blocked retrieve before EndAcquisition().
        return self._cam is not None and self._grabbing

    def grab_locked_features(self) -> frozenset[str]:
        # FLIR/Teledyne lock only Width/Height during acquisition; the ROI
        # offsets stay writable while grabbing (live ROI pan), so only the size
        # nodes need the grab-cycle path.
        return GEOMETRY_FEATURES

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

    # Typed-setter seam used by the native-TSV config applier (_genicam_config).
    def _set_bool(self, name: str, value: bool) -> None:
        spin = _spin()
        node = spin.CBooleanPtr(self._nodemap().GetNode(name))
        if not spin.IsAvailable(node) or not spin.IsWritable(node):
            raise BackendError(f"boolean {name} is not writable")
        node.SetValue(bool(value))

    def _get_bool(self, name: str) -> bool | None:
        spin = _spin()
        node = spin.CBooleanPtr(self._nodemap().GetNode(name))
        if not spin.IsAvailable(node) or not spin.IsReadable(node):
            return None
        return bool(node.GetValue())

    def _set_number(self, name: str, value: float, is_int: bool) -> None:
        spin = _spin()
        raw = self._nodemap().GetNode(name)
        node = spin.CIntegerPtr(raw) if is_int else spin.CFloatPtr(raw)
        if not spin.IsAvailable(node) or not spin.IsWritable(node):
            raise BackendError(f"node {name} is not writable")
        node.SetValue(int(value) if is_int else float(value))

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

    # Full device node-map browser (Camera tab): depth-first walk of the GenApi
    # category tree, exactly like the Basler backend does over pypylon.genicam
    # (and the Spinnaker C-API backend over ctypes) — same widget kinds, same
    # per-node bounds/enum-entries, grouped by category.
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
        # Category is only known from the tree walk; leave it blank on a single
        # re-read (the client keeps the grouping it already has).
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
                spin.CBooleanPtr(node).SetValue(_coerce_bool(value))
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

    def config_values(self, config_str: str) -> dict[str, str]:
        return dict(parse_config(config_str))

    def load_params(self, config_str: str) -> None:
        # Native GenApi persistence TSV, applied best-effort in file order (see
        # _genicam_config). Runs after open(), so open()'s Mono8/throughput stay
        # authoritative (both are in the applier's skip set).
        if config_str:
            apply_config(self, config_str)
        self._original_trigger_source = self._get_enum("TriggerSource")

    def save_params(self) -> str:
        return normalize_trigger_source(dump_config(self), self._original_trigger_source)

    # ----------------------------------------------------------- triggering

    def _enable_trigger_overlap(self) -> None:
        """Let a trigger be accepted during the previous frame's readout.

        Without this (``TriggerOverlap=Off``, the FLIR default) a FrameStart
        software trigger fired mid-readout is silently ignored, so the camera
        accepts only ~every other trigger — roughly halving the software-triggered
        frame rate. ``ReadOut`` pipelines back-to-back triggers and restores the
        sensor's real rate. Best-effort: a model without the node keeps its
        default. (Verified on the Spinnaker C-API mirror; see spinnaker_c.py.)
        """
        try:
            self._set_enum("TriggerOverlap", "ReadOut")
        except BackendError as e:
            log.debug("Could not set TriggerOverlap on camera %s: %s", self._serial, e)

    def enable_frame_trigger(self) -> None:
        if not self.is_open():
            return
        clear_freerun_rate_cap(self)  # drop any free-run preview cap before triggering
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")
        self._enable_trigger_overlap()

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
        clear_freerun_rate_cap(self)  # drop any free-run preview cap before triggering
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")
        self._set_enum("TriggerSource", "Software")
        self._enable_trigger_overlap()

    def trigger_once(self) -> None:
        # Only bump the pending counter; retrieve() fires the device trigger on
        # the grab thread so the shared trigger timer never blocks on this camera.
        self._bump_trigger()

    def begin_freerun(self, fps: float | None = None) -> bool:
        """Switch to continuous free-run (TriggerMode Off).

        Used by the benchmark (``fps=None``, uncapped, to measure the ceiling) and
        by free-run *preview* (``fps`` set, so the rate is capped at the target and
        the preview draws the same bandwidth as an fps-matched recording).
        Best-effort: a failure returns False so the caller skips free-run for this
        camera. A later ``begin_software_trigger_preview`` re-arms the FrameStart
        trigger (and clears the cap), so no explicit restore is needed.
        """
        try:
            self._set_enum("TriggerMode", "Off")
            try:
                self._set_enum("AcquisitionMode", "Continuous")
            except BackendError:
                pass
            if fps is not None:
                apply_freerun_rate_cap(self, fps)
            return True
        except BackendError as e:
            log.debug("free-run unsupported on camera %s: %s", self._serial, e)
            return False

    def retrieve_freerun(self, timeout_ms, wants_array) -> Frame | None:
        # Free-run: the camera acquires continuously, so fetch the next image
        # without waiting on / firing a software trigger.
        cam = self._cam
        if cam is None or not self._grabbing:
            return None
        return self._fetch_image(cam, timeout_ms, wants_array)

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
            log.error("Failed to start streaming on camera %s: %s", self._serial, e)
            raise BackendError(str(e)) from e
        self._begin_grab()

    def start_grab_preview(self) -> None:
        self._begin_acquisition("NewestOnly")

    def start_grab_record(self) -> bool:
        # Spinnaker has no WaitForFrameTriggerReady; the camera arms on the
        # first software trigger, so report ready once acquisition has begun.
        self._begin_acquisition("OldestFirst")
        return True

    def stop_grab(self) -> None:
        # Flip the hand-off flag and wake any blocked retrieve BEFORE the native
        # stop, so the grab loop sees "not grabbing" immediately.
        self._end_grab()
        if self._cam is not None and self._cam.IsStreaming():
            try:
                self._cam.EndAcquisition()
            except Exception:
                pass

    def retrieve(self, timeout_ms: int, wants_array) -> Frame | None:
        spin = _spin()
        # Wait for a pending software trigger, then fire exactly one device
        # trigger and fetch exactly one frame on this camera's own grab thread.
        if not self._wait_pending(timeout_ms):
            return None
        cam = self._cam
        if cam is None or not self._grabbing:
            return None
        # Fire the device trigger here (not on the caught _trigger_all path). The
        # grab loop does not wrap retrieve() in try/except, so a stop-race or a
        # trigger failure must return None, never raise — a lost trigger is one
        # lost frame, the same as the GetNextImage timeout below.
        try:
            spin.CCommandPtr(self._nodemap().GetNode("TriggerSoftware")).Execute()
        except spin.SpinnakerException:
            return None
        return self._fetch_image(cam, timeout_ms, wants_array)

    def _fetch_image(self, cam, timeout_ms, wants_array) -> Frame | None:
        # Fetch exactly one image; never raises (a timeout or incomplete frame is
        # one lost frame, as the grab loop expects).
        spin = _spin()
        try:
            image = cam.GetNextImage(timeout_ms)
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
    # Debug, not info: the auto cascade enumerates every tier, so CameraSystem
    # logs the single attributed "Detected N" summary (see basler backend).
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
