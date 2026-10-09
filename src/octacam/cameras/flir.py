"""FLIR / Teledyne Spinnaker backend: one `FlirBackend` over a
`FlirBinding` of the SDK, either PySpin (here, the `flir` tier) or the
C API over ctypes (`octacam.cameras.spinnaker_c`, the `spinnaker` tier).

Both share the `.txt` parameter files. Acquisition is Continuous, with the
stream's NewestOnly buffering for preview and OldestFirst for recording.
"""

import atexit
import gc
import logging
import time
from abc import abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar

import numpy as np

from octacam.cameras.base import BackendError, Frame
from octacam.cameras.genicam import Bounds, GenApi, NodeMapBackend
from octacam.cameras.registry import BackendSpec, BackendUnavailable, select_serials

log = logging.getLogger("octacam")

# Record-grab stream buffers (capped by StreamBufferCountMax): ~1 s at 125 fps
# instead of the SDK's 9, so a grab thread stalled by a GC pause or a disk hiccup
# delays frames rather than losing them. They come out of the kernel's shared USB
# memory (usbcore.usbfs_memory_mb: two full-sensor GS3s need ~1.1 GB at 128), so
# a pool that cannot start is halved, down to MIN_STREAM_BUFFERS.
RECORD_STREAM_BUFFERS = 128
MIN_STREAM_BUFFERS = 16

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


class IncompleteLog:
    """A camera's discarded incomplete images: each is counted (`total`, for
    the stream statistics) and a grab's are logged rate-limited, as warnings
    only in a record grab.
    """

    def __init__(self, serial: str):
        self._serial = serial
        self.total = 0
        self._record = False
        self._grab = 0  # this grab's
        self._logged = 0  # this grab's at the last report
        self._logged_at = 0.0

    def begin_grab(self, *, record: bool) -> None:
        self._record = record
        self._grab = 0
        self._logged = 0

    def count(self) -> None:
        self.total += 1
        self._grab += 1
        now = time.monotonic()
        first = self._grab == 1
        if not first and now - self._logged_at < INCOMPLETE_REPORT_INTERVAL_S:
            return
        level = logging.WARNING if self._record else logging.DEBUG
        if first:
            log.log(
                level,
                "Camera %s delivered an incomplete image; discarded (more in this "
                "grab are totaled at most every %g s)",
                self._serial,
                INCOMPLETE_REPORT_INTERVAL_S,
            )
        else:
            log.log(
                level,
                "Camera %s: %d incomplete images discarded in this grab (%d since "
                "the last report)",
                self._serial,
                self._grab,
                self._grab - self._logged,
            )
        self._logged = self._grab
        self._logged_at = now


def _quietly(fn: Callable[..., Any], *args: Any) -> None:
    """`fn(*args)`, any failure ignored: a release on the way out."""
    try:
        fn(*args)
    except Exception:
        pass


class FlirBinding(GenApi):
    """One binding of the Spinnaker SDK: GenApi node access plus the System,
    camera and image calls `FlirBackend` makes. A failed call raises
    `BackendError`; the `is_*` and image queries never raise.

    It holds the System from `enumerate` until `teardown`, and the
    camera handles it handed out until each is released: the System released
    with one outstanding aborts the process (a libusb `usbi_mutex_destroy`
    assertion, exit 134).
    """

    tier: ClassVar[str]

    def __init__(self) -> None:
        self._system: Any = None
        self._cam_list: Any = None
        self._outstanding: dict[int, Any] = {}  # by id()

    # ---------------------------------------------------------- the System
    @abstractmethod
    def _get_system(self) -> Any: ...
    @abstractmethod
    def _camera_list(self, system: Any) -> Any: ...
    @abstractmethod
    def _cameras(self, cam_list: Any) -> list[Any]: ...
    @abstractmethod
    def _clear_camera_list(self, cam_list: Any) -> None: ...
    @abstractmethod
    def _release_system(self, system: Any) -> None: ...

    @abstractmethod
    def _release(self, cam: Any) -> None:
        """Release one handle the camera list gave out."""

    # ------------------------------------------------------------ a camera
    @abstractmethod
    def init(self, cam: Any) -> None: ...
    @abstractmethod
    def deinit(self, cam: Any) -> None: ...
    @abstractmethod
    def is_initialized(self, cam: Any) -> bool: ...
    @abstractmethod
    def is_streaming(self, cam: Any) -> bool: ...
    @abstractmethod
    def nodemap(self, cam: Any) -> Any: ...
    @abstractmethod
    def stream_nodemap(self, cam: Any) -> Any: ...

    @abstractmethod
    def tl_device_nodemap(self, cam: Any) -> Any:
        """The transport layer's device node map, readable without Init."""

    @abstractmethod
    def device_id(self, cam: Any) -> str:
        """The SDK's id for a camera without a readable serial number."""

    @abstractmethod
    def begin_acquisition(self, cam: Any) -> None: ...
    @abstractmethod
    def end_acquisition(self, cam: Any) -> None: ...

    # -------------------------------------------------------------- images
    @abstractmethod
    def next_image(self, cam: Any, timeout_ms: int) -> Any:
        """The next image within `timeout_ms`, or None; it must be released."""

    @abstractmethod
    def image_incomplete(self, image: Any) -> bool:
        """True also when the status is unreadable: never trust the buffer."""

    @abstractmethod
    def image_timestamp(self, image: Any) -> int:
        """The camera timestamp in ns, 0 if unreadable."""

    @abstractmethod
    def image_array(self, image: Any) -> np.ndarray:
        """An owned 2-D uint8 copy, safe after release; BackendError for a
        frame that is not Mono8 (a Mono16 one has the same shape).
        """

    @abstractmethod
    def image_release(self, image: Any) -> None: ...

    # ------------------------------------------------------------- session
    def serial(self, cam: Any) -> str:
        """DeviceSerialNumber from the TL device node map, else the device id."""
        try:
            serial = self.get(
                self.tl_device_nodemap(cam), "DeviceSerialNumber", "string"
            )
        except BackendError:
            serial = None
        return serial or self.device_id(cam)

    def model(self, cam: Any) -> str | None:
        """DeviceModelName, readable without Init, so doctor can label a camera
        in a live session.
        """
        try:
            return (
                self.get(self.tl_device_nodemap(cam), "DeviceModelName", "string")
                or None
            )
        except BackendError:
            return None

    def enumerate(
        self, requested_serials: list[str] | None = None
    ) -> list[tuple[str, Any]]:
        """`[(serial, camera)]` in `select_serials` order, holding the
        System until `teardown`; handles not handed out are released.
        """
        # Release a previous enumeration's System (doctor enumerates twice):
        # released late, at exit, it aborts the process.
        if self._system is not None or self._cam_list is not None:
            self.teardown()
        self._system = self._get_system()
        self._cam_list = self._camera_list(self._system)
        cams = self._cameras(self._cam_list)
        if not cams:
            self.teardown()
            return []
        log.debug("%s enumerated %d camera(s)", self.tier, len(cams))
        found = [(self.serial(cam), cam) for cam in cams]
        by_serial = dict(found)
        used = select_serials(by_serial, requested_serials)
        for serial, cam in found:
            if serial not in used:
                _quietly(self._release, cam)
        out = [(serial, by_serial[serial]) for serial in used]
        for _serial, cam in out:
            self._outstanding[id(cam)] = cam
        return out

    def release(self, cam: Any) -> None:
        """Release a camera's handle once it is closed, before the System."""
        self._outstanding.pop(id(cam), None)
        _quietly(self._release, cam)

    def teardown(self) -> None:
        """Release every handle no close released, then the camera list and the
        System, last (released earlier, Spinnaker reports cameras in use).
        Idempotent.
        """
        for cam in list(self._outstanding.values()):
            _quietly(self._release, cam)
        self._outstanding.clear()
        if self._cam_list is not None:
            # A failed start's traceback holds the camera's node maps in a
            # reference cycle; uncollected, Clear() refuses, and the CameraList's
            # destructor then aborts the process (std::terminate) at exit.
            gc.collect()
            _quietly(self._clear_camera_list, self._cam_list)
            self._cam_list = None
        if self._system is not None:
            _quietly(self._release_system, self._system)
            self._system = None


class FlirBackend(NodeMapBackend[FlirBinding]):
    """One FLIR camera, driven through either Spinnaker binding."""

    def __init__(self, binding: FlirBinding, cam: Any):
        super().__init__(binding.serial(cam), binding)
        self._cam: Any = cam  # the binding's camera handle; None once closed
        self._stream_nodemap: Any = None
        # Discarded (never written), but counted for the summary.
        self._incomplete = IncompleteLog(self._serial)

    # ------------------------------------------------------------- lifecycle

    def open(self) -> None:
        api = self._api
        api.init(self._cam)
        self._nodemap = api.nodemap(self._cam)
        try:
            self._stream_nodemap = api.stream_nodemap(self._cam)
        except BackendError as e:
            log.debug("No TL stream node map on camera %s: %s", self._serial, e)
        # Mono8, so a frame is the 2-D uint8 array the GRAY8 writer takes.
        try:
            self.set_node("PixelFormat", "enum", "Mono8")
        except BackendError as e:
            log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)
        self._maximize_link_throughput()

    def _maximize_link_throughput(self) -> None:
        """Best-effort: raise DeviceLinkThroughputLimit to its max, once at open.

        FLIR ships it capped: on the GS3, 350.6 of 384.4 MB/s, an ~84 vs ~92 fps
        transfer ceiling at 2048^2 Mono8 (measured ~82 -> ~90 fps at 100 us).
        """
        api = self._api
        node = api.node(self._nodemap, "DeviceLinkThroughputLimit")
        if node is None or not api.writable(node):
            return
        value, top = api.value(node, "int"), api.bounds(node, "int")[1]
        if value is None or top is None or value >= top:
            return
        try:
            api.write(node, "int", int(top))
            log.debug(
                "Camera %s: DeviceLinkThroughputLimit %d -> %d (max)",
                self._serial,
                value,
                top,
            )
        except BackendError as e:
            log.debug(
                "Could not raise DeviceLinkThroughputLimit on camera %s: %s",
                self._serial,
                e,
            )

    def close(self) -> None:
        cam = self._cam
        if cam is None:
            return
        api = self._api
        self.trigger.end_grab()
        try:
            if api.is_streaming(cam):
                api.end_acquisition(cam)
        except Exception:
            pass
        try:
            if api.is_initialized(cam):
                api.deinit(cam)
        except Exception:
            pass
        api.release(cam)
        self._cam = self._nodemap = self._stream_nodemap = None

    def is_open(self) -> bool:
        return self._cam is not None and self._api.is_initialized(self._cam)

    # ------------------------------------------------------------- grabbing

    def stream_statistics(self) -> dict[str, int]:
        """Spinnaker's transport counters (see STREAM_STATISTICS) plus the
        incomplete images this backend discarded.
        """
        out = {"IncompleteImagesDiscarded": self._incomplete.total}
        if self._stream_nodemap is not None:
            for name in STREAM_STATISTICS:
                value = self._api.get(self._stream_nodemap, name, "int")
                if value is not None:
                    out[name] = int(value)
        return out

    def _stream_set(self, name: str, kind: str, value: Any) -> None:
        """Best-effort write to the TL stream node map."""
        try:
            self._api.put(self._stream_nodemap, name, kind, value)
        except BackendError as e:
            log.debug("Could not set %s on camera %s: %s", name, self._serial, e)

    def _set_stream_buffers(self, buffers: int) -> None:
        """Best-effort: a manual stream buffer count of `buffers` (<= the max)."""
        self._stream_set("StreamBufferCountMode", "enum", "Manual")
        node = self._api.node(self._stream_nodemap, "StreamBufferCountManual")
        top = None if node is None else self._api.bounds(node, "int")[1]
        self._stream_set(
            "StreamBufferCountManual",
            "int",
            buffers if top is None else min(buffers, int(top)),
        )

    def _begin_acquisition(self, buffer_mode: str, buffers: int | None = None) -> None:
        # Only BeginAcquisition is fatal: FLIR defaults to Continuous, and the
        # stream settings only tune its buffering.
        self._try_set("AcquisitionMode", "enum", "Continuous")
        stream = self._stream_nodemap
        if stream is not None:
            self._stream_set("StreamBufferHandlingMode", "enum", buffer_mode)
        while True:
            if buffers and stream is not None:
                self._set_stream_buffers(buffers)
            try:
                self._api.begin_acquisition(self._cam)
                break
            except BackendError as e:
                if buffers and stream is not None and buffers > MIN_STREAM_BUFFERS:
                    log.warning(
                        "Camera %s could not start acquisition with %d stream buffers "
                        "(%s); retrying with %d. The pool shares the kernel's USB "
                        "memory with the other cameras: raise usbcore.usbfs_memory_mb "
                        "to keep the full pool, which absorbs longer host stalls.",
                        self._serial,
                        buffers,
                        e,
                        buffers // 2,
                    )
                    buffers //= 2
                    continue
                log.error("Failed to start streaming on camera %s: %s", self._serial, e)
                raise
        self.trigger.begin_grab()

    def start_grab_preview(self) -> None:
        self._incomplete.begin_grab(record=False)
        self._begin_acquisition("NewestOnly")

    def start_grab_record(self) -> bool:
        # Spinnaker has no ready gate: ready once acquisition began (a GS3 still
        # ignores its first triggers, so the recording primes the cameras).
        self._incomplete.begin_grab(record=True)
        self._begin_acquisition("OldestFirst", RECORD_STREAM_BUFFERS)
        return True

    def stop_grab(self) -> None:
        self.trigger.end_grab()
        cam = self._cam
        if cam is None:
            return
        try:
            if self._api.is_streaming(cam):
                self._api.end_acquisition(cam)
        except Exception:
            pass

    def _fire_trigger(self) -> bool:
        if self._nodemap is None:
            return False
        node = self._api.node(self._nodemap, "TriggerSoftware")
        if node is None:
            return False
        try:
            self._api.execute(node)
        except BackendError:
            return False
        return True

    def _fetch(
        self, timeout_ms: int, wants_array: Callable[[], bool], answers_trigger: bool
    ) -> Frame | None:
        cam = self._cam
        if cam is None:
            return None
        api = self._api
        image = api.next_image(cam, timeout_ms)
        if image is None:
            return None  # a timeout: no image yet
        try:
            incomplete = api.image_incomplete(image)
            timestamp = 0 if incomplete else api.image_timestamp(image)
            if answers_trigger:
                self.trigger.answered(None if incomplete else timestamp)
            if incomplete:
                self._incomplete.count()
                return None
            return (api.image_array(image) if wants_array() else None, timestamp)
        except BackendError as e:
            log.warning("Camera %s: bad frame (%s); skipping", self._serial, e)
            return None
        finally:
            api.image_release(image)  # every image, or the SDK runs out of buffers


# --- the PySpin binding (the `flir` tier) --------------------------------

PySpin: Any = None  # imported by _spin() on first use


def _spin() -> Any:
    """The PySpin module, imported on first use; BackendUnavailable without it."""
    global PySpin
    if PySpin is None:
        try:  # PySpin ships with the Spinnaker SDK and is not pip-installable.
            import PySpin as module  # type: ignore
        except ImportError as e:
            raise BackendUnavailable(
                "flir",
                "PySpin (the Spinnaker SDK's Python binding) isn't importable "
                "\N{EM DASH} not "
                "on PyPI and easily pruned by `uv sync`; reinstall the PySpin wheel "
                "to restore this tier. FLIR cameras still work via the ctypes "
                "`spinnaker` tier when libSpinnaker_C.so is present.",
            ) from e
        PySpin = module
    return PySpin


def _safe(fn: Callable[[], Any]) -> Any:
    """`fn()`, or None on any failure (a best-effort read)."""
    try:
        return fn()
    except Exception:
        return None


def _sdk(fn: Callable[..., Any], *args: Any) -> Any:
    """`fn(*args)`, a SpinnakerException raised as BackendError."""
    spin = _spin()
    try:
        return fn(*args)
    except spin.SpinnakerException as e:
        raise BackendError(str(e)) from e


class PySpinBinding(FlirBinding):
    """The Spinnaker SDK through PySpin. Nodes are untyped INodes, cast to their
    interface (`CIntegerPtr`...) per call. A CameraPtr is released when its
    last reference drops, so `_release` has nothing to call.
    """

    tier = "flir"

    # ---------------------------------------------------------------- GenApi
    def node(self, nodemap: Any, name: str) -> Any:
        return _safe(lambda: nodemap.GetNode(name))  # None for an absent name

    def children(self, category: Any) -> list[Any]:
        spin = _spin()
        nodes = _safe(lambda: spin.CCategoryPtr(category).GetFeatures()) or []
        return [
            n for n in nodes if _safe(lambda n=n: spin.IsAvailable(n) and n.IsFeature())
        ]

    def kind(self, node: Any) -> str | None:
        spin = _spin()
        kinds = {
            spin.intfIInteger: "int",
            spin.intfIFloat: "float",
            spin.intfIBoolean: "bool",
            spin.intfIEnumeration: "enum",
            spin.intfIString: "string",
            spin.intfICommand: "command",
            spin.intfICategory: "category",
        }
        return _safe(lambda: kinds.get(node.GetPrincipalInterfaceType()))

    def name(self, node: Any) -> str | None:
        return _safe(lambda: node.GetName())

    def display_name(self, node: Any) -> str | None:
        return _safe(lambda: node.GetDisplayName())

    def tooltip(self, node: Any) -> str | None:
        return (
            _safe(lambda: node.GetToolTip())
            or _safe(lambda: node.GetDescription())
            or None
        )

    def visibility(self, node: Any) -> str:
        spin = _spin()
        names = {
            spin.Beginner: "beginner",
            spin.Expert: "expert",
            spin.Guru: "guru",
            spin.Invisible: "invisible",
        }
        return _safe(lambda: names.get(node.GetVisibility())) or "beginner"

    def readable(self, node: Any) -> bool:
        return bool(_safe(lambda: _spin().IsReadable(node)))

    def writable(self, node: Any) -> bool:
        return bool(_safe(lambda: _spin().IsWritable(node)))

    def _typed(self, node: Any, kind: str) -> Any:
        spin = _spin()
        return {
            "int": spin.CIntegerPtr,
            "float": spin.CFloatPtr,
            "bool": spin.CBooleanPtr,
            "string": spin.CStringPtr,
            "enum": spin.CEnumerationPtr,
        }[kind](node)

    def value(self, node: Any, kind: str) -> Any:
        typed = self._typed(node, kind)
        if kind == "enum":
            entry = _safe(lambda: typed.GetCurrentEntry())
            return None if entry is None else _safe(lambda: entry.GetSymbolic())
        return _safe(lambda: typed.GetValue())

    def bounds(self, node: Any, kind: str) -> Bounds:
        typed = self._typed(node, kind)
        return (
            _safe(lambda: typed.GetMin()),
            _safe(lambda: typed.GetMax()),
            _safe(lambda: typed.GetInc()),
            _safe(lambda: typed.GetUnit()) or None,
        )

    def entries(self, node: Any) -> list[tuple[str, bool]]:
        spin = _spin()
        out = []
        for entry in _safe(lambda: spin.CEnumerationPtr(node).GetEntries()) or ():
            symbolic = _safe(lambda e=entry: spin.CEnumEntryPtr(e).GetSymbolic())
            if symbolic:
                available = _safe(lambda e=entry: spin.IsAvailable(e))
                out.append((symbolic, True if available is None else bool(available)))
        return out

    def write(self, node: Any, kind: str, value: Any) -> None:
        spin = _spin()
        typed = self._typed(node, kind)
        if kind == "enum":
            entry = _sdk(typed.GetEntryByName, value)
            if (
                entry is None
                or not spin.IsAvailable(entry)
                or not spin.IsReadable(entry)
            ):
                raise BackendError(
                    f"enumeration {self.name(node)} has no entry {value!r}"
                )
            _sdk(typed.SetIntValue, entry.GetValue())
        else:
            cast = {"int": int, "float": float, "bool": bool, "string": str}[kind]
            _sdk(typed.SetValue, cast(value))

    def execute(self, node: Any) -> None:
        _sdk(_spin().CCommandPtr(node).Execute)

    # ------------------------------------------------------- System, cameras
    def _get_system(self) -> Any:
        return _sdk(_spin().System.GetInstance)

    def _camera_list(self, system: Any) -> Any:
        return _sdk(system.GetCameras)

    def _cameras(self, cam_list: Any) -> list[Any]:
        return [cam_list.GetByIndex(i) for i in range(cam_list.GetSize())]

    def _clear_camera_list(self, cam_list: Any) -> None:
        cam_list.Clear()

    def _release_system(self, system: Any) -> None:
        system.ReleaseInstance()

    def _release(self, cam: Any) -> None:
        pass

    def init(self, cam: Any) -> None:
        _sdk(cam.Init)

    def deinit(self, cam: Any) -> None:
        _sdk(cam.DeInit)

    def is_initialized(self, cam: Any) -> bool:
        return bool(_safe(lambda: cam.IsInitialized()))

    def is_streaming(self, cam: Any) -> bool:
        return bool(_safe(lambda: cam.IsStreaming()))

    def nodemap(self, cam: Any) -> Any:
        return _sdk(cam.GetNodeMap)

    def stream_nodemap(self, cam: Any) -> Any:
        return _sdk(cam.GetTLStreamNodeMap)

    def tl_device_nodemap(self, cam: Any) -> Any:
        return _sdk(cam.GetTLDeviceNodeMap)

    def device_id(self, cam: Any) -> str:
        return _sdk(cam.GetUniqueID)

    def begin_acquisition(self, cam: Any) -> None:
        _sdk(cam.BeginAcquisition)

    def end_acquisition(self, cam: Any) -> None:
        _sdk(cam.EndAcquisition)

    # ---------------------------------------------------------------- images
    def next_image(self, cam: Any, timeout_ms: int) -> Any:
        try:
            return _sdk(cam.GetNextImage, timeout_ms)
        except BackendError:
            return None

    def image_incomplete(self, image: Any) -> bool:
        incomplete = _safe(lambda: image.IsIncomplete())
        return incomplete is None or bool(incomplete)

    def image_timestamp(self, image: Any) -> int:
        return int(_safe(lambda: image.GetTimeStamp()) or 0)

    def image_array(self, image: Any) -> np.ndarray:
        arr = _sdk(image.GetNDArray)
        if arr.ndim != 2 or arr.dtype.itemsize != 1:
            raise BackendError(f"non-Mono8 frame (ndim={arr.ndim}, dtype={arr.dtype})")
        return arr.copy()

    def image_release(self, image: Any) -> None:
        _quietly(image.Release)


_binding = PySpinBinding()


def ensure_available() -> None:
    """Raise BackendUnavailable if PySpin/Spinnaker is not installed."""
    _spin()


def teardown() -> None:
    """Release the PySpin session (`FlirBinding.teardown`)."""
    _binding.teardown()


# For paths that enumerate without a CameraSystem (doctor, an aborted run).
atexit.register(teardown)

SPEC = BackendSpec(
    lambda requested_serials=None: _binding.enumerate(requested_serials),
    lambda cam: FlirBackend(_binding, cam),
    ensure_available=ensure_available,
    read_model=lambda cam: _binding.model(cam),
    teardown=teardown,
)
