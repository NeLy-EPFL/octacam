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

from octacam.cameras.base import GEOMETRY_FEATURES, BackendError, Frame
from octacam.cameras.genicam import Bounds, GenApi, NodeMapBackend
from octacam.cameras.registry import BackendSpec, select_serials

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


def _call(obj: Any, method: str) -> Any:
    """``obj.<method>()``, or None where the SDK refuses or the node lacks it
    (a float without GetInc)."""
    try:
        return getattr(obj, method)()
    except (AttributeError, genicam.GenericException):
        return None


class _PylonGenApi(GenApi):
    """pypylon's GenApi. Nodes come back downcast to their typed interface;
    their INode (``GetNode()``) carries the metadata and the access mode."""

    def node(self, nodemap: Any, name: str) -> Any:
        try:
            return nodemap.GetNode(name)
        except genicam.GenericException:
            return None

    def children(self, category: Any) -> list[Any]:
        out = []
        for typed in _call(category, "GetFeatures") or ():
            try:
                inode = typed.GetNode()
                if inode.IsFeature() and genicam.IsAvailable(inode):
                    out.append(typed)
            except genicam.GenericException as e:
                log.debug("Skipping node during feature walk: %s", e)
        return out

    def kind(self, node: Any) -> str | None:
        return _IFACE_KIND.get(_call(node.GetNode(), "GetPrincipalInterfaceType"))

    def name(self, node: Any) -> str | None:
        return _call(node.GetNode(), "GetName")

    def display_name(self, node: Any) -> str | None:
        return _call(node.GetNode(), "GetDisplayName")

    def tooltip(self, node: Any) -> str | None:
        inode = node.GetNode()
        return _call(inode, "GetToolTip") or _call(inode, "GetDescription") or None

    def visibility(self, node: Any) -> str:
        return _VIS_NAME.get(_call(node.GetNode(), "GetVisibility"), "beginner")

    def readable(self, node: Any) -> bool:
        try:
            return bool(genicam.IsReadable(node.GetNode()))
        except genicam.GenericException:
            return False

    def writable(self, node: Any) -> bool:
        try:
            return bool(genicam.IsWritable(node.GetNode()))
        except genicam.GenericException:
            return False

    def value(self, node: Any, kind: str) -> Any:
        return _call(node, "ToString" if kind == "enum" else "GetValue")

    def bounds(self, node: Any, kind: str) -> Bounds:
        unit = _call(node, "GetUnit") or None
        return (_call(node, "GetMin"), _call(node, "GetMax"), _call(node, "GetInc"), unit)

    def entries(self, node: Any) -> list[tuple[str, bool]]:
        out = []
        for entry in _call(node, "GetEntries") or ():
            symbolic = _call(entry, "GetSymbolic")
            if not symbolic:
                continue
            try:
                available = bool(genicam.IsAvailable(entry.GetNode()))
            except genicam.GenericException:
                available = True
            out.append((symbolic, available))
        return out

    def write(self, node: Any, kind: str, value: Any) -> None:
        try:
            if kind in ("enum", "string"):
                node.FromString(str(value))
            else:
                node.SetValue(value)
        except genicam.GenericException as e:
            raise BackendError(str(e)) from e

    def execute(self, node: Any) -> None:
        try:
            node.Execute()
        except genicam.GenericException as e:
            raise BackendError(str(e)) from e


_GENAPI = _PylonGenApi()


def _normalize_pfs_triggers(content: str, original_source: str | None) -> str:
    """Undo a live preview's FrameStart trigger overrides in a saved .pfs:
    TriggerMode back to Off (the shipped convention), TriggerSource to the one
    load_params captured. Other selectors are left alone. The TSV counterpart is
    :func:`octacam.cameras.genicam.normalize_trigger_source`.
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


class BaslerBackend(NodeMapBackend):
    """A single Basler camera, driven through pypylon; parameters persist as
    ``.pfs`` (the trigger chain is the shared GenICam one)."""

    extension = "pfs"

    def __init__(self, device):
        self.raw: Any = pylon.InstantCamera(device)  # None once closed
        info = self.raw.GetDeviceInfo()
        super().__init__(str(info.GetSerialNumber()), _GENAPI)
        # Whether grab timestamps count ns (USB3 Vision; a GigE camera's count ticks).
        self._stamps_ns = info.GetDeviceClass() == "BaslerUsb"
        # Grabs pylon flagged failed (bandwidth gaps, packet loss).
        self._incomplete_grabs = 0

    def open(self) -> None:
        try:
            self.raw.Open()
        except genicam.GenericException as e:
            raise BackendError(str(e)) from e
        self._nodemap = self.raw.GetNodeMap()

    def close(self) -> None:
        # Destroy the device while the pylon runtime lives: pypylon runs
        # PylonTerminate() from a Py_AtExit hook, and a device the GC destroys
        # after it segfaults. Close() alone does not detach the device.
        if self.raw is None:  # a second close() is a no-op
            return
        self.trigger.end_grab()
        self._nodemap = None
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

    def grab_locked_features(self) -> frozenset[str]:
        # pylon locks the whole ROI while grabbing (TLParamsLocked), offsets too.
        return GEOMETRY_FEATURES | {"OffsetX", "OffsetY"}

    # ------------------------------------------------- .pfs persistence

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
        self._original_trigger_source = self.get_node("TriggerSource", "enum")

    def save_params(self) -> str:
        content = pylon.FeaturePersistence.SaveToString(self.raw.GetNodeMap())
        return _normalize_pfs_triggers(content, self._original_trigger_source)

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
        self.trigger.begin_grab()

    def start_grab_preview(self) -> None:
        self._start_grabbing(pylon.GrabStrategy_LatestImageOnly)

    def start_grab_record(self) -> bool:
        self._start_grabbing(pylon.GrabStrategy_OneByOne)
        # A ready gate for the first software trigger only: retrieve fires the
        # next trigger once the previous one is answered or given up, so
        # exposures never pile up in OneByOne's bounded queue.
        # Camera.start_record does not stop the grab on failure, so a failed
        # gate stops it here, or the next StartGrabbing finds the camera wedged.
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
        self.trigger.end_grab()
        if self.raw is not None:
            self.raw.StopGrabbing()

    def _fire_trigger(self) -> bool:
        raw = self.raw
        if raw is None:
            return False
        try:
            raw.ExecuteSoftwareTrigger()
        except genicam.GenericException:
            return False
        return True

    def _fetch(
        self, timeout_ms: int, wants_array: Callable[[], bool], answers_trigger: bool
    ) -> Frame | None:
        raw = self.raw
        if raw is None:
            return None
        # RetrieveResult raises, not just times out, on an unplug or a transport error.
        try:
            result = raw.RetrieveResult(timeout_ms, pylon.TimeoutHandling_Return)
        except genicam.GenericException:
            return None
        try:
            # A timed-out RetrieveResult is an empty result whose accessors throw.
            if not result.IsValid():
                return None
            succeeded = result.GrabSucceeded()
            if answers_trigger:
                # A failed grab answers its trigger too; the clock check needs ns.
                self.trigger.answered(
                    int(result.TimeStamp) if succeeded and self._stamps_ns else None
                )
            if not succeeded:
                self._count_incomplete(result)
                return None
            timestamp = result.TimeStamp
            return (result.Array if wants_array() else None, timestamp)
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


def enumerate_basler(requested_serials: list[str] | None = None):
    """``[(serial, device)]`` in :func:`select_serials` order (each serial once:
    a second handle would never be destroyed). A camera present but unusable (a
    USB 2.0 fallback, no answer to its first register read) gets a None handle
    and a loud message: the cascade claims it without opening it, so no lower
    tier retries it. The CreateDevice calls run concurrently under one deadline
    (:func:`_create_device_timeout`).
    """
    factory = tl_factory()
    devices = factory.EnumerateDevices()
    if not devices:
        return []
    log.debug("basler enumerated %d camera(s)", len(devices))

    by_serial: dict[str, object] = {}
    for device in devices:
        by_serial.setdefault(str(device.GetSerialNumber()), device)
    wanted = [
        (serial, by_serial[serial])
        for serial in select_serials(by_serial, requested_serials)
    ]
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


SPEC = BackendSpec(enumerate_basler, BaslerBackend)
