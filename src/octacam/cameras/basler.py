"""Basler / pypylon camera backend.

The only module that imports pypylon. Implements the :class:`CameraBackend`
seam over a pylon ``InstantCamera`` and enumerates devices through the pylon
transport-layer factory. Per-camera parameters persist as Basler ``.pfs``
feature-stream files.
"""

import logging
import re
from collections.abc import Callable

from pypylon import genicam, pylon

from octacam.cameras._trigger_handoff import SoftwareTriggerHandoff
from octacam.cameras.base import (
    PARAM_NODES,
    BackendError,
    FeatureInfo,
    Frame,
    NodeInfo,
)

log = logging.getLogger("octacam")

TRIGGER_READY_TIMEOUT_MS = 1000

_TRIGGER_SELECTOR_RE = re.compile(r"\{TriggerSelector=([^}]+)\}")

# pypylon.genicam interface-type int -> FeatureInfo widget kind. GetNode() nodes
# come back already downcast to the typed interface (IInteger/IEnumeration/...),
# so no CxxxPtr cast is needed; only INode-level metadata goes through .GetNode().
_IFACE_KIND = {
    genicam.intfIInteger: "int",
    genicam.intfIFloat: "float",
    genicam.intfIBoolean: "bool",
    genicam.intfIEnumeration: "enum",
    genicam.intfIString: "string",
    genicam.intfICommand: "command",
    genicam.intfICategory: "category",
}
# GetVisibility() int -> name; only Beginner/Expert are shown in the browser.
_VIS_NAME = {
    genicam.Beginner: "beginner",
    genicam.Expert: "expert",
    genicam.Guru: "guru",
    genicam.Invisible: "invisible",
}


def _typed_value_attr(node, getter: str):
    """Best-effort ``node.<getter>()`` (e.g. GetInc on a float without one)."""
    try:
        return getattr(node, getter)()
    except (AttributeError, genicam.GenericException):
        return None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _basler_feature(typed) -> FeatureInfo | None:
    """Build a FeatureInfo from a pypylon typed node (None = skip)."""
    inode = typed.GetNode()
    kind = _IFACE_KIND.get(inode.GetPrincipalInterfaceType())
    if kind is None or kind == "category":
        return None
    name = inode.GetName()
    readable = genicam.IsReadable(inode)
    writable = genicam.IsWritable(inode)
    feature = FeatureInfo(
        name=name,
        display_name=inode.GetDisplayName() or name,
        type=kind,
        readable=readable,
        writable=writable,
        visibility=_VIS_NAME.get(inode.GetVisibility(), "beginner"),
        tooltip=(inode.GetToolTip() or inode.GetDescription() or None),
    )
    if kind in ("int", "float"):
        if readable:
            feature.value = _typed_value_attr(typed, "GetValue")
        feature.min = _typed_value_attr(typed, "GetMin")
        feature.max = _typed_value_attr(typed, "GetMax")
        feature.inc = _typed_value_attr(typed, "GetInc")
        feature.unit = _typed_value_attr(typed, "GetUnit") or None
    elif kind == "bool":
        if readable:
            feature.value = _typed_value_attr(typed, "GetValue")
    elif kind == "enum":
        if readable:
            try:
                feature.value = typed.ToString()
            except genicam.GenericException:
                feature.value = None
        feature.entries = _basler_enum_entries(typed)
    elif kind == "string":
        if readable:
            feature.value = _typed_value_attr(typed, "GetValue")
    return feature


def _basler_enum_entries(node) -> list[dict] | None:
    try:
        out = []
        for entry in node.GetEntries():
            try:
                symbolic = entry.GetSymbolic()
            except genicam.GenericException:
                continue
            if not symbolic:
                continue
            try:
                available = genicam.IsAvailable(entry.GetNode())
            except genicam.GenericException:
                available = True
            out.append({"value": symbolic, "display": symbolic, "available": available})
        return out or None
    except genicam.GenericException:
        return None


def _node_attr(node, attr: str):
    """Best-effort read of node.Min/.Max/.Inc/.Unit; None when unsupported.

    Float/enum nodes (Gain, PixelFormat) lack some of these and raise rather
    than return, so a missing attribute is expected, not an error.
    """
    try:
        return getattr(node, attr)
    except (AttributeError, genicam.GenericException):
        return None


def _normalize_pfs_triggers(content: str, original_source: str | None) -> str:
    """Undo the live preview's trigger overrides in a saved .pfs.

    start_preview forces the FrameStart selector to TriggerMode On /
    TriggerSource Software; a saved snapshot would bake those in, so a later
    headless/external recording would misread the rig's intended source. Reset
    the FrameStart context to the shipped convention (TriggerMode Off,
    TriggerSource = the value load_params captured). Other selectors (e.g.
    FrameBurstStart) and context-qualified lines are left untouched.
    """
    out = []
    for line in content.splitlines():
        fields = line.split("\t")
        key = fields[0] if fields else ""
        if (
            not line.startswith("#")
            and key in ("TriggerMode", "TriggerSource")
            and len(fields) >= 2
        ):
            match = _TRIGGER_SELECTOR_RE.search("\t".join(fields[1:-1]))
            selector = match.group(1) if match else None
            if selector in (None, "FrameStart"):
                if key == "TriggerMode":
                    fields[-1] = "Off"
                    line = "\t".join(fields)
                elif key == "TriggerSource" and original_source is not None:
                    fields[-1] = original_source
                    line = "\t".join(fields)
        out.append(line)
    return "\n".join(out) + "\n"


def _drop_empty_pfs_values(content: str) -> str:
    """Remove .pfs entries with empty values (e.g. "ImageFilename\\t").

    Older pylon versions wrote such no-op entries; current GenICam
    persistence parsers reject the whole stream over them.
    """
    lines = []
    for line in content.splitlines():
        if not line.startswith("#") and "\t" in line:
            key, _, value = line.partition("\t")
            if not value.strip():
                log.debug("Dropping empty .pfs entry: %s", key)
                continue
        lines.append(line)
    return "\n".join(lines) + "\n"


class BaslerBackend(SoftwareTriggerHandoff):
    """A single Basler camera, driven through pypylon."""

    extension = "pfs"

    def __init__(self, device):
        # Set to None by close() once the device is destroyed; the is_open/
        # is_grabbing guards below tolerate that so a second close() is a no-op.
        self.raw = pylon.InstantCamera(device)
        self._serial = str(self.raw.GetDeviceInfo().GetSerialNumber())
        self._original_trigger_source: str | None = None
        # Count grabs that pylon flagged as incomplete/failed (USB bandwidth
        # gaps, packet loss) so a rig delivering partial frames can be spotted.
        self._incomplete_grabs = 0
        # Software-trigger hand-off (shared mixin): trigger_once only bumps a
        # counter; the device ExecuteSoftwareTrigger moves into retrieve() on the
        # grab thread so the shared trigger timer never blocks on this camera and
        # cannot throttle the others.
        self._init_trigger_handoff()

    @property
    def serial_number(self) -> str:
        return self._serial

    def open(self) -> None:
        try:
            self.raw.Open()
        except genicam.GenericException as e:
            # Usually the device is already opened exclusively by another
            # process (a second `octacam gui` on the rig). Surface it as a
            # BackendError so the caller reports it cleanly instead of letting
            # a raw pylon traceback escape.
            raise BackendError(str(e)) from e

    def close(self) -> None:
        # Tear the device down while the pylon runtime is still alive. pypylon
        # runs PylonTerminate() from a Py_AtExit hook during interpreter
        # shutdown; an InstantCamera (or the TlFactory) left for the garbage
        # collector to destroy *after* that point touches freed runtime state
        # and segfaults — the crash that appears right after "octacam stopped".
        # Close() alone does not detach the device, so DestroyDevice() here (and
        # dropping the reference) makes the wrapper collectable before exit.
        if self.raw is None:  # idempotent: a second close() must not raise
            return
        try:
            if self.raw.IsGrabbing():
                self.raw.StopGrabbing()
            if self.raw.IsOpen():
                self.raw.Close()
            self.raw.DestroyDevice()
        except genicam.GenericException as e:
            log.warning("Error tearing down camera %s: %s", self._serial, e)
        finally:
            self.raw = None

    def is_open(self) -> bool:
        return self.raw is not None and self.raw.IsOpen()

    def is_grabbing(self) -> bool:
        # The hand-off flag (set under the cond by start/stop) is authoritative,
        # not the native IsGrabbing(): stop_grab flips the flag and wakes a blocked
        # retrieve *before* the native StopGrabbing(), so the grab loop must see
        # "not grabbing" from the same instant, and the shared trigger timer must
        # never make a native call on the wait path.
        return self.raw is not None and self._grabbing

    def width(self) -> int:
        return self.raw.Width.Value

    def height(self) -> int:
        return self.raw.Height.Value

    # ----------------------------------------------------- sensor parameters

    def read_node(self, name: str) -> NodeInfo:
        node = getattr(self.raw, PARAM_NODES[name])
        try:
            value = node.Value
        except genicam.GenericException as e:
            raise BackendError(str(e)) from e
        return NodeInfo(
            value=value,
            min=_node_attr(node, "Min"),
            max=_node_attr(node, "Max"),
            inc=_node_attr(node, "Inc"),
            unit=_node_attr(node, "Unit"),
            writable=genicam.IsWritable(node.Node),
        )

    def write_node(self, name: str, value: float) -> None:
        node = getattr(self.raw, PARAM_NODES[name])
        try:
            node.Value = value
        except genicam.GenericException as e:
            raise BackendError(str(e)) from e

    # ---------------------------------------------------- full device node map

    def _walk(self, category, path: str, out: list, seen: set) -> None:
        """Depth-first walk of the GenApi category tree, collecting features."""
        try:
            features = category.GetFeatures()
        except genicam.GenericException:
            return
        for typed in features:
            try:
                inode = typed.GetNode()
                if not inode.IsFeature() or not genicam.IsAvailable(inode):
                    continue
                vis = _VIS_NAME.get(inode.GetVisibility(), "beginner")
                if vis not in ("beginner", "expert"):
                    continue
                iface = inode.GetPrincipalInterfaceType()
                if iface == genicam.intfICategory:
                    self._walk(typed, inode.GetName(), out, seen)
                    continue
                name = inode.GetName()
                if name in seen:
                    continue
                seen.add(name)
                feature = _basler_feature(typed)
                if feature is not None:
                    feature.category = path or "Other"
                    out.append(feature)
            except genicam.GenericException as e:
                log.debug("Skipping node during feature walk: %s", e)

    def list_features(self) -> list[FeatureInfo]:
        if self.raw is None or not self.raw.IsOpen():
            return []
        nodemap = self.raw.GetNodeMap()
        try:
            root = nodemap.GetNode("Root")
        except genicam.GenericException:
            return []
        out: list[FeatureInfo] = []
        self._walk(root, "", out, set())
        return out

    def read_feature(self, name: str) -> FeatureInfo:
        try:
            typed = self.raw.GetNodeMap().GetNode(name)
        except genicam.GenericException as e:
            raise BackendError(f"no such node: {name}") from e
        if typed is None:
            raise BackendError(f"no such node: {name}")
        feature = _basler_feature(typed)
        if feature is None:
            raise BackendError(f"node {name} is not an editable feature")
        # Category is only known from the tree walk; leave it blank on a
        # single-node re-read (the client keeps the grouping it already has).
        return feature

    def write_feature(self, name: str, value: object) -> None:
        try:
            typed = self.raw.GetNodeMap().GetNode(name)
            kind = _IFACE_KIND.get(typed.GetNode().GetPrincipalInterfaceType())
        except genicam.GenericException as e:
            raise BackendError(f"no such node: {name}") from e
        try:
            if kind == "int":
                node_min = _typed_value_attr(typed, "GetMin")
                node_inc = _typed_value_attr(typed, "GetInc")
                snapped = int(round(float(value)))
                if node_inc:
                    base = node_min if node_min is not None else 0
                    snapped = int(base + round((snapped - base) / node_inc) * node_inc)
                typed.SetValue(snapped)
            elif kind == "float":
                typed.SetValue(float(value))
            elif kind == "bool":
                typed.SetValue(_coerce_bool(value))
            elif kind in ("enum", "string"):
                typed.FromString(str(value))
            else:
                raise BackendError(f"node {name} is not writable ({kind})")
        except genicam.GenericException as e:
            raise BackendError(str(e)) from e

    def execute_command(self, name: str) -> None:
        try:
            typed = self.raw.GetNodeMap().GetNode(name)
            if _IFACE_KIND.get(typed.GetNode().GetPrincipalInterfaceType()) != "command":
                raise BackendError(f"node {name} is not a command")
            typed.Execute()
        except genicam.GenericException as e:
            raise BackendError(str(e)) from e

    def config_values(self, config_str: str) -> dict[str, str]:
        """Parse a Basler ``.pfs`` (``Name<TAB>...<TAB>value``) into name->value.

        The value is the last tab-separated field; context-qualified selector
        lines (``TriggerMode\\t{TriggerSelector=...}\\tOff``) collapse to the base
        node, which is good enough for the per-field reset fallback."""
        out: dict[str, str] = {}
        for line in config_str.splitlines():
            if line.startswith("#") or "\t" not in line:
                continue
            fields = line.split("\t")
            name = fields[0].strip()
            value = fields[-1].strip()
            if name and value:
                out.setdefault(name, value)
        return out

    def load_params(self, config_str: str) -> None:
        if config_str:
            try:
                pylon.FeaturePersistence.LoadFromString(
                    _drop_empty_pfs_values(config_str),
                    self.raw.GetNodeMap(),
                    True,
                )
            except genicam.GenericException as e:
                raise BackendError(str(e)) from e
        try:
            self._original_trigger_source = self.raw.TriggerSource.Value
        except genicam.GenericException:
            self._original_trigger_source = None

    def save_params(self) -> str:
        content = pylon.FeaturePersistence.SaveToString(self.raw.GetNodeMap())
        return _normalize_pfs_triggers(content, self._original_trigger_source)

    # ----------------------------------------------------------- triggering

    def enable_frame_trigger(self) -> None:
        if not self.raw.IsOpen():
            return
        self.raw.TriggerSelector.Value = "FrameStart"
        self.raw.TriggerMode.Value = "On"

    def set_trigger_source(self, use_software: bool) -> None:
        if not self.raw.IsOpen():
            return
        try:
            if use_software:
                self.raw.TriggerSource.Value = "Software"
            elif self._original_trigger_source is not None:
                self.raw.TriggerSource.Value = self._original_trigger_source
        except genicam.GenericException as e:
            log.warning(
                "Failed to set trigger source on camera %s: %s", self._serial, e
            )

    def begin_software_trigger_preview(self) -> None:
        self.raw.TriggerSelector.Value = "FrameStart"
        self.raw.TriggerMode.Value = "On"
        self.raw.TriggerSource.Value = "Software"

    def trigger_once(self) -> None:
        # Only bump the pending counter; retrieve() fires the device trigger on
        # the grab thread. Keeps the shared trigger timer off this device.
        self._bump_trigger()

    def begin_freerun(self) -> bool:
        """Switch to continuous free-run (TriggerMode Off) for the benchmark.

        Best-effort: any failure returns False so the benchmark skips the free-run
        ceiling for this camera rather than aborting. A subsequent
        ``begin_software_trigger_preview`` re-arms the FrameStart trigger, so no
        explicit restore is needed.
        """
        raw = self.raw
        if raw is None:
            return False
        try:
            raw.TriggerMode.Value = "Off"
            try:
                raw.AcquisitionMode.Value = "Continuous"
            except genicam.GenericException:
                pass  # Continuous is the grabbing default; a rejected write is fine
            return True
        except genicam.GenericException as e:
            log.debug("free-run unsupported on camera %s: %s", self._serial, e)
            return False

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        # Like retrieve(), but the camera free-runs so no software trigger is
        # fired: just fetch the next frame it pushed. Never raises (mirrors
        # retrieve): a stop-race or bad grab is one lost frame.
        raw = self.raw
        if raw is None or not self._grabbing:
            return None
        result = raw.RetrieveResult(timeout_ms, pylon.TimeoutHandling_Return)
        try:
            if not result.IsValid() or not result.GrabSucceeded():
                return None
            return (result.Array if wants_array() else None, result.TimeStamp)
        finally:
            result.Release()

    # ------------------------------------------------------------- grabbing

    def _start_grabbing(self, strategy) -> None:
        try:
            self.raw.StartGrabbing(strategy)
        except genicam.GenericException as e:
            # The pylon error does not identify the camera (only an opaque
            # USB address), so name it before propagating.
            log.error(
                "Failed to start streaming on camera %s. For 'insufficient "
                "system resources' errors, raise the open file limit "
                "(ulimit -n; pylon needs ~150 file descriptors per camera) "
                "and check usbfs_memory_mb.",
                self._serial,
            )
            raise BackendError(str(e)) from e
        self._begin_grab()

    def start_grab_preview(self) -> None:
        self._start_grabbing(pylon.GrabStrategy_LatestImageOnly)

    def start_grab_record(self) -> bool:
        self._start_grabbing(pylon.GrabStrategy_OneByOne)
        # One-time ready gate: covers the first software trigger. retrieve()
        # serializes execute→RetrieveResult on the single grab thread thereafter,
        # so at most one exposure is ever in flight and OneByOne's bounded output
        # queue can never overflow.
        return self.raw.WaitForFrameTriggerReady(
            TRIGGER_READY_TIMEOUT_MS, pylon.TimeoutHandling_Return
        )

    def stop_grab(self) -> None:
        # Flip the hand-off flag and wake any blocked retrieve BEFORE the native
        # stop, so the grab loop sees "not grabbing" at once (no full-timeout stall).
        self._end_grab()
        if self.raw is not None:
            self.raw.StopGrabbing()

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        # Wait for a pending software trigger, then fire exactly one device
        # trigger and fetch exactly one frame on this camera's own grab thread.
        if not self._wait_pending(timeout_ms):
            return None
        # The device call now lives here (not on the caught _trigger_all path), and
        # the grab loop does not wrap retrieve() in a try/except — so a stop-race
        # or trigger failure must return None, never raise, or the grab thread dies
        # uncaught and the ffmpeg writer child is orphaned. A trigger lost to such
        # an error is one lost frame, the same as a grab timeout.
        raw = self.raw
        if raw is None or not self._grabbing:
            return None
        try:
            raw.ExecuteSoftwareTrigger()
        except genicam.GenericException:
            return None
        result = raw.RetrieveResult(timeout_ms, pylon.TimeoutHandling_Return)
        try:
            # IsValid is the pypylon equivalent of C++'s `if (grab_result)`:
            # a timed-out RetrieveResult returns an empty result whose other
            # accessors throw, so bail before touching them.
            if not result.IsValid():
                return None
            # A valid-but-failed grab is an incomplete frame (USB bandwidth gap,
            # packet loss): pylon already drops it for us, but partial frames are
            # a prime suspect for corrupt previews, so surface the cause
            # periodically (rate-limited to avoid flooding at the trigger rate).
            if not result.GrabSucceeded():
                self._incomplete_grabs += 1
                if self._incomplete_grabs % 100 == 1:
                    log.warning(
                        "Camera %s: %d incomplete grab(s); last: %s (0x%08X)",
                        self._serial,
                        self._incomplete_grabs,
                        result.GetErrorDescription(),
                        result.GetErrorCode(),
                    )
                return None
            timestamp = result.TimeStamp
            array = result.Array if wants_array() else None
            return (array, timestamp)
        finally:
            result.Release()


def enumerate_basler(requested_serials: list[str] | None = None):
    """Return ``[(serial, device_handle), ...]`` for the requested cameras.

    With no requested serials, every detected camera is returned (sorted by
    serial); otherwise the listed serials are returned in order, warning about
    any that are not connected. Mirrors the original CameraSystem enumeration.
    """
    tl_factory = pylon.TlFactory.GetInstance()
    devices = tl_factory.EnumerateDevices()
    if not devices:
        return []
    # Debug, not info: in the auto cascade every tier enumerates in turn, so
    # per-tier info lines would print several confusing, overlapping counts.
    # CameraSystem logs one attributed "Detected N" summary instead.
    log.debug("basler enumerated %d camera(s)", len(devices))

    detected = [str(device.GetSerialNumber()) for device in devices]
    final = sorted(detected) if not requested_serials else list(requested_serials)

    out = []
    for serial in final:
        try:
            index = detected.index(serial)
        except ValueError:
            log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, tl_factory.CreateDevice(devices[index])))
    return out
