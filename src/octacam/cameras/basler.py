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

from octacam.cameras.base import (
    PARAM_NODES,
    BackendError,
    Frame,
    NodeInfo,
)

log = logging.getLogger("octacam")

TRIGGER_READY_TIMEOUT_MS = 1000

_TRIGGER_SELECTOR_RE = re.compile(r"\{TriggerSelector=([^}]+)\}")


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


class BaslerBackend:
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
        return self.raw is not None and self.raw.IsGrabbing()

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
        if self.raw.IsGrabbing():
            try:
                self.raw.ExecuteSoftwareTrigger()
            except genicam.GenericException as e:
                raise BackendError(str(e)) from e

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

    def start_grab_preview(self) -> None:
        self._start_grabbing(pylon.GrabStrategy_LatestImageOnly)

    def start_grab_record(self) -> bool:
        self._start_grabbing(pylon.GrabStrategy_OneByOne)
        return self.raw.WaitForFrameTriggerReady(
            TRIGGER_READY_TIMEOUT_MS, pylon.TimeoutHandling_Return
        )

    def stop_grab(self) -> None:
        self.raw.StopGrabbing()

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        result = self.raw.RetrieveResult(timeout_ms, pylon.TimeoutHandling_Return)
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
    log.info("Detected %d camera(s)", len(devices))

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
