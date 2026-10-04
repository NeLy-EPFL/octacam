"""Basler backend over pypylon, the only module that imports it.

A pylon ``InstantCamera`` per camera, enumerated through :func:`tl_factory`;
parameters persist as ``.pfs`` feature-stream files.
"""

import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from typing import Any

from pypylon import genicam, pylon

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

TRIGGER_READY_TIMEOUT_MS = 1000
# Stream-grabber counters a recording reports, where the transport layer has them.
_STREAM_STATISTICS = (
    "Statistic_Total_Buffer_Count",
    "Statistic_Failed_Buffer_Count",
    "Statistic_Buffer_Underrun_Count",
    "Statistic_Missed_Frame_Count",
    "Statistic_Resynchronization_Count",
)

_TRIGGER_SELECTOR_RE = re.compile(r"\{TriggerSelector=([^}]+)\}")

# Interface type -> widget kind. pypylon hands nodes back already downcast to
# their typed interface; only INode metadata needs .GetNode().
_IFACE_KIND = {
    genicam.intfIInteger: "int",
    genicam.intfIFloat: "float",
    genicam.intfIBoolean: "bool",
    genicam.intfIEnumeration: "enum",
    genicam.intfIString: "string",
    genicam.intfICommand: "command",
    genicam.intfICategory: "category",
}
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


def _normalize_pfs_triggers(content: str, original_source: str | None) -> str:
    """Undo a live preview's FrameStart trigger overrides in a saved .pfs:
    TriggerMode back to Off (the shipped convention), TriggerSource to the one
    load_params captured. Other selectors are left alone. The TSV counterpart is
    :func:`octacam.cameras._genicam_config.normalize_trigger_source`.
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
    """Drop .pfs entries with an empty value ("ImageFilename\\t"): current
    parsers reject the whole stream over them."""
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
        self.raw: Any = pylon.InstantCamera(device)  # None once closed
        info = self.raw.GetDeviceInfo()
        self._serial = str(info.GetSerialNumber())
        # Whether grab timestamps count ns (USB3 Vision; a GigE camera's count ticks).
        self._stamps_ns = info.GetDeviceClass() == "BaslerUsb"
        self._original_trigger_source: str | None = None
        # Grabs pylon flagged failed (bandwidth gaps, packet loss).
        self._incomplete_grabs = 0
        self._init_trigger_handoff()

    @property
    def serial_number(self) -> str:
        return self._serial

    def open(self) -> None:
        try:
            self.raw.Open()
        except genicam.GenericException as e:
            raise BackendError(str(e)) from e

    def close(self) -> None:
        # Destroy the device while the pylon runtime lives: pypylon runs
        # PylonTerminate() from a Py_AtExit hook, and a device the GC destroys
        # after it segfaults. Close() alone does not detach the device.
        if self.raw is None:  # a second close() is a no-op
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
        return self.raw is not None and self._grabbing

    def grab_locked_features(self) -> frozenset[str]:
        # pylon locks the whole ROI while grabbing (TLParamsLocked), offsets too.
        return GEOMETRY_FEATURES | {"OffsetX", "OffsetY"}

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
            min=_typed_value_attr(node, "GetMin"),
            max=_typed_value_attr(node, "GetMax"),
            inc=_typed_value_attr(node, "GetInc"),
            unit=_typed_value_attr(node, "GetUnit"),
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
                if vis not in ("beginner", "expert", "guru"):
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
                typed.SetValue(coerce_bool(value))
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
        """``.pfs`` lines as {name: last field}; selector-qualified lines collapse
        to the base node, enough for the per-field reset."""
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

    # Guarded by is_open(), not raw.IsOpen(): raw is None once closed.
    def enable_frame_trigger(self) -> None:
        if not self.is_open():
            return
        self._clear_freerun_cap()
        self.raw.TriggerSelector.Value = "FrameStart"
        self.raw.TriggerMode.Value = "On"

    def set_trigger_source(self, use_software: bool) -> None:
        if not self.is_open():
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
        self._clear_freerun_cap()
        self.raw.TriggerSelector.Value = "FrameStart"
        self.raw.TriggerMode.Value = "On"
        self.raw.TriggerSource.Value = "Software"

    def trigger_once(self) -> None:
        self._bump_trigger()  # no device call: retrieve() fires it (_trigger_handoff)

    def _apply_freerun_cap(self, fps: float) -> None:
        """Best-effort: cap the free-run rate at ``fps`` (SFNC nodes on the ace)."""
        try:
            self.raw.AcquisitionFrameRateEnable.Value = True
            self.raw.AcquisitionFrameRate.Value = float(fps)
        except genicam.GenericException as e:
            log.debug(
                "Could not cap free-run rate at %s fps on camera %s: %s",
                fps, self._serial, e,
            )

    def _clear_freerun_cap(self) -> None:
        """Best-effort removal of the free-run cap, which on Basler applies even
        while triggered and would clip a recording."""
        raw = self.raw
        if raw is None:
            return
        try:
            raw.AcquisitionFrameRateEnable.Value = False
        except genicam.GenericException:
            pass

    def begin_freerun(self, fps: float | None = None) -> bool:
        """TriggerMode Off, capped at ``fps`` when given; False if refused. Arming
        a triggered mode later clears the cap."""
        raw = self.raw
        if raw is None:
            return False
        try:
            raw.TriggerMode.Value = "Off"
            try:
                raw.AcquisitionMode.Value = "Continuous"
            except genicam.GenericException:
                pass  # Continuous is the default
            if fps is not None:
                self._apply_freerun_cap(fps)
            return True
        except genicam.GenericException as e:
            log.debug("free-run unsupported on camera %s: %s", self._serial, e)
            return False

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        raw = self.raw
        if raw is None or not self._grabbing:
            return None
        try:
            result = raw.RetrieveResult(timeout_ms, pylon.TimeoutHandling_Return)
        except genicam.GenericException:
            return None
        try:
            if not result.IsValid():
                return None
            if not result.GrabSucceeded():
                self._count_incomplete(result)
                return None
            return (result.Array if wants_array() else None, result.TimeStamp)
        except genicam.GenericException:
            return None
        finally:
            result.Release()

    def _count_incomplete(self, result) -> None:
        """Count a failed grab, logging its cause rate-limited."""
        self._incomplete_grabs += 1
        if self._incomplete_grabs % 100 == 1:
            log.warning(
                "Camera %s: %d incomplete grab(s); last: %s (0x%08X)",
                self._serial,
                self._incomplete_grabs,
                result.GetErrorDescription(),
                result.GetErrorCode(),
            )

    def stream_statistics(self) -> dict[str, int]:
        """The failed grabs this backend discarded, plus pylon's stream-grabber
        statistics where the transport layer exposes them."""
        out = {"IncompleteImagesDiscarded": self._incomplete_grabs}
        raw = self.raw
        if raw is None:
            return out
        try:
            nodemap = raw.GetStreamGrabberNodeMap()
        except Exception:
            return out
        for name in _STREAM_STATISTICS:
            try:
                node = nodemap.GetNode(name)
                if node is not None and genicam.IsReadable(node):
                    out[name] = int(node.GetValue())
            except Exception:
                continue
        return out

    # ------------------------------------------------------------- grabbing

    def _start_grabbing(self, strategy) -> None:
        try:
            self.raw.StartGrabbing(strategy)
        except genicam.GenericException as e:
            # pylon's error names only a USB address.
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
        # A ready gate for the first software trigger. Camera.start_record does
        # not stop the grab on failure, so a failed gate stops it here, or the
        # next StartGrabbing finds the camera wedged.
        try:
            ready = self.raw.WaitForFrameTriggerReady(
                TRIGGER_READY_TIMEOUT_MS, pylon.TimeoutHandling_Return
            )
        except genicam.GenericException:
            self.stop_grab()
            raise
        if not ready:
            self.stop_grab()
        return ready

    def stop_grab(self) -> None:
        self._end_grab()
        if self.raw is not None:
            self.raw.StopGrabbing()

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        fire = self._claim_trigger(timeout_ms)
        if fire is None:
            return None
        raw = self.raw
        if raw is None or not self._grabbing:
            return None
        if fire:
            try:
                raw.ExecuteSoftwareTrigger()
            except genicam.GenericException:
                self._trigger_unfired()
                return None
        # RetrieveResult raises, not just times out, on an unplug or a transport error.
        try:
            result = raw.RetrieveResult(
                self._fetch_timeout_ms(timeout_ms), pylon.TimeoutHandling_Return
            )
        except genicam.GenericException:
            return None
        try:
            # A timed-out RetrieveResult is an empty result whose accessors throw.
            if not result.IsValid():
                return None
            succeeded = result.GrabSucceeded()
            # A failed grab answers its trigger too; the clock check needs ns.
            self._trigger_answered(
                int(result.TimeStamp) if succeeded and self._stamps_ns else None
            )
            if not succeeded:
                self._count_incomplete(result)
                return None
            timestamp = result.TimeStamp
            array = result.Array if wants_array() else None
            return (array, timestamp)
        except genicam.GenericException:
            return None
        finally:
            result.Release()


def _describe_open_failure(serial: str, exc: Exception) -> str:
    """An actionable reason a Basler camera cannot be opened: pylon's "USB 2.0
    port" error reads like a wrong port when a USB3 link that failed to train
    fell back to USB 2.0 (the cable or connector)."""
    text = str(exc)
    if "USB 2.0" in text or "USB 3.0 compatible port" in text:
        return (
            f"Camera {serial} came up on a USB 2.0 link and cannot be opened. A "
            "USB3 camera whose SuperSpeed link fails to train drops back to USB "
            "2.0 even in a USB 3 port, so the cause is the cable or connector, "
            "not the port choice. Reseat both ends of its cable (or swap in a "
            "known-good USB3 cable), or move it to another USB 3 port, then "
            "reload. `lsusb -t` shows each camera's link speed — a healthy one "
            "reads 5000M, this one 480M. Skipping this camera for now."
        )
    if "first register" in text or "maximum device response time" in text:
        return (
            f"Camera {serial} enumerated but never answered its first register "
            "read, so pylon could not download its XML description. The link "
            "trained (it may well report a healthy 5000M) but the camera is not "
            "answering USB control transfers — almost always a marginal cable or "
            "connector. Check `dmesg` for a matching `can't set config` line, "
            "then reseat both ends of its cable or move it to another USB 3 "
            "port. Skipping this camera for now."
        )
    return f"Camera {serial} could not be opened and will be skipped: {text}"


# pylon loads every GenTL producer on GENICAM_GENTL64_PATH when the factory first
# loads its transport layers, and only then. A system pylon puts its producers
# there (/etc/profile.d/basler-gentl-path.sh); their libuxapi shadows pypylon's
# bundled copy, and their unload segfaulted every process at exit. octacam uses
# only pylon's native transport layers, so the path is hidden for that load.
# Mutating the environment races a C getenv, so it happens once, before any other
# SDK starts: the cascade enumerates basler first, and doctor loads the factory
# before starting its workers.
_tl_factory_lock = threading.Lock()
_tl_factory_ready = False


def tl_factory():
    """pylon's transport-layer factory, loaded without GenTL producers. Every
    path into pylon goes through it; a process that used pylon first has already
    loaded them."""
    global _tl_factory_ready
    with _tl_factory_lock:
        if _tl_factory_ready:
            return pylon.TlFactory.GetInstance()
        saved = os.environ.pop("GENICAM_GENTL64_PATH", None)
        try:
            factory = pylon.TlFactory.GetInstance()
            factory.EnumerateTls()  # loads every transport layer, the GenTL one too
        finally:
            if saved is not None:
                os.environ["GENICAM_GENTL64_PATH"] = saved
        _tl_factory_ready = True
        return factory


# CreateDevice downloads the camera's XML over USB, with no pylon timeout (its
# Read/WriteTimeout are GigE-only): a camera whose link trained but whose control
# transfers time out held a rig's startup for 271 s in one call (healthy: 0.16 s).
_CREATE_DEVICE_TIMEOUT_S = 15.0
# When to name the cameras still awaited; also the deadline loop's poll slice.
_CREATE_PROGRESS_INTERVAL_S = 3.0


def _create_device_timeout() -> float:
    """The CreateDevice deadline: ``OCTACAM_BASLER_CREATE_TIMEOUT`` if it is a
    finite positive number (``float()`` accepts ``inf``, which would remove the
    guard, and ``nan``, which would spin the deadline loop), else the default."""
    raw = os.environ.get("OCTACAM_BASLER_CREATE_TIMEOUT", "").strip()
    if not raw:
        return _CREATE_DEVICE_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        log.warning(
            "Ignoring OCTACAM_BASLER_CREATE_TIMEOUT=%r (need a finite number of "
            "seconds greater than 0); using %gs",
            raw,
            _CREATE_DEVICE_TIMEOUT_S,
        )
        return _CREATE_DEVICE_TIMEOUT_S
    return value


def _release_late_device(factory, serial: str) -> Callable[["Future"], None]:
    """A done-callback destroying a handle that arrives after the deadline:
    nothing owns it, and left to the GC it would segfault at exit (see
    :meth:`BaslerBackend.close`). This is the factory's DestroyDevice, not the
    InstantCamera's."""

    def _callback(future: "Future") -> None:
        try:
            device = future.result()
        except Exception:
            return  # it failed on its own; there is no handle to release
        try:
            factory.DestroyDevice(device)
        except Exception as e:  # best effort — we are already past the deadline
            log.debug("Could not release late handle for camera %s: %s", serial, e)
        else:
            log.info(
                "Camera %s responded after it had already been skipped; "
                "released its handle.",
                serial,
            )

    return _callback


def enumerate_basler(
    requested_serials: list[str] | None = None, *, warn_missing: bool = True
):
    """``[(serial, device)]``: every camera sorted by serial, or the requested
    ones in order. A camera present but unusable (a USB 2.0 fallback, no answer
    to its first register read) gets a None handle and a loud message: the
    cascade claims it without opening it, so no lower tier retries it. The
    CreateDevice calls run concurrently under one deadline
    (:func:`_create_device_timeout`).
    """
    factory = tl_factory()
    devices = factory.EnumerateDevices()
    if not devices:
        return []
    log.debug("basler enumerated %d camera(s)", len(devices))

    detected = [str(device.GetSerialNumber()) for device in devices]
    final = sorted(detected) if not requested_serials else list(requested_serials)

    wanted: list[tuple[str, object]] = []
    seen: set[str] = set()
    for serial in final:
        if serial in seen:  # a second handle would never be destroyed
            continue
        try:
            index = detected.index(serial)
        except ValueError:
            if warn_missing:
                log.warning("Camera with serial number %s not found", serial)
            continue
        seen.add(serial)
        wanted.append((serial, devices[index]))
    if not wanted:
        return []

    timeout = _create_device_timeout()
    # Not a `with` block: its __exit__ waits out pylon's whole retry, undoing the
    # deadline. Non-daemon workers: an abandoned one is still inside pylon, and
    # finalizing the interpreter under it is the segfault close() avoids.
    pool = ThreadPoolExecutor(
        max_workers=len(wanted), thread_name_prefix="pylon-create"
    )
    fut_to_serial = {
        pool.submit(factory.CreateDevice, device): serial for serial, device in wanted
    }
    pool.shutdown(wait=False)

    results: dict[str, object | None] = {}
    pending = set(fut_to_serial)
    started = time.monotonic()
    announced = False
    while pending:
        # Past the deadline this is a last zero-timeout wait, which still takes
        # every handle that landed since the previous one.
        left = max(0.0, started + timeout - time.monotonic())
        done, pending = futures_wait(
            pending,
            timeout=min(left, _CREATE_PROGRESS_INTERVAL_S),
            return_when=FIRST_COMPLETED,
        )
        for future in done:
            serial = fut_to_serial[future]
            try:
                results[serial] = future.result()
            except Exception as e:
                # pypylon also raises RuntimeError, OSError or MemoryError here.
                log.error("%s", _describe_open_failure(serial, e))
                results[serial] = None
        if not left:
            break
        if (
            pending
            and not announced
            and time.monotonic() - started >= _CREATE_PROGRESS_INTERVAL_S
        ):
            # Time-gated: a healthy rig finishes inside the interval, quietly.
            announced = True
            log.info(
                "Waiting up to %gs for %d Basler camera(s) to respond: %s",
                timeout,
                len(pending),
                ", ".join(sorted(fut_to_serial[f] for f in pending)),
            )

    for future in pending:  # still inside pylon at the deadline
        serial = fut_to_serial[future]
        log.error(
            "Camera %s did not respond within %gs and will be skipped. It "
            "enumerated, so its link trained (it may even report a healthy "
            "5000M), but pylon got no answer from it. Most often that is a "
            "marginal USB cable or connector — check `dmesg` for a matching "
            "`can't set config` line, then reseat both ends of its cable or move "
            "it to another USB 3 port. It can also mean another process already "
            "holds the camera. Set OCTACAM_BASLER_CREATE_TIMEOUT to allow longer "
            "than %gs. Note that octacam cannot cancel the call it gave up on, so "
            "shutting down may pause until that camera finally answers.",
            serial,
            timeout,
            timeout,
        )
        future.add_done_callback(_release_late_device(factory, serial))
        results[serial] = None

    return [(serial, results[serial]) for serial, _device in wanted]
