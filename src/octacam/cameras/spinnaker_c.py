"""FLIR / Teledyne Spinnaker backend over the C API via ``ctypes``.

This is the fast, watermark-free, modern-Python path for FLIR cameras. It drives
``libSpinnaker_C.so`` (the Spinnaker SDK's flat C ABI) directly through
:mod:`ctypes`, mirroring the PySpin backend (:mod:`octacam.cameras.flir`)
node-for-node — same SFNC names, same :data:`PARAM_NODES`, same
``extension = "json"``, same session-wide ``spinSystem`` singleton released in
:func:`teardown` — but calling the C functions instead of PySpin's C++ wrappers.

Why this backend exists (see ``docs/plan-spinnaker-c-backend.md``):

* **Modern Python.** PySpin ships only as a cp310 wheel, so on the project's
  Python 3.14 the :mod:`~octacam.cameras.flir` tier drops out. The C library has
  no such limit — ``ctypes.CDLL`` loads it on any Python.
* **No GIL starvation.** The pycameleon floor's ``receive()`` holds the Python
  GIL for the whole exposure wait, so two FLIRs starve a co-recorded Basler.
  ``ctypes`` (a ``CDLL``, not a ``PyDLL``) **releases the GIL around every foreign
  call**, so :func:`_Spinnaker.get_next_image`'s blocking
  ``spinCameraGetNextImageEx`` does not hold it — other camera threads keep
  running while this one waits for its triggered frame.
* **Clean close, no watermark.** This is the SDK C API, not the Spinnaker *GenTL
  producer* (``Spinnaker_GenTL.cti``) whose ``DevClose`` deadlocks while holding
  the GIL; and unlike the Balluff mvIMPACT producer it stamps no evaluation
  watermark. We control the ``DeInit`` / ``ReleaseInstance`` ordering directly.

Structure:

* :class:`_Spinnaker` is the thin binding: it owns the loaded ``ctypes`` library,
  configures every function's ``argtypes``/``restype`` once (essential on 64-bit,
  or pointer/handle args truncate to 32 bits), and exposes typed helpers that
  raise :class:`BackendError` on any non-``SPINNAKER_ERR_SUCCESS`` return.
* :class:`SpinnakerBackend` is pure orchestration over that binding, structurally
  identical to :class:`~octacam.cameras.flir.FlirBackend`.
* The library is loaded lazily by :func:`_spin`; a missing SDK surfaces as a clean
  :class:`BackendUnavailable` (never a raw ``OSError``), so this tier self-disables
  on boxes without Spinnaker — exactly like the FLIR/PySpin tier.
"""

import atexit
import ctypes
import json
import logging
from typing import Any

import numpy as np

from octacam.cameras._trigger_handoff import SoftwareTriggerHandoff
from octacam.cameras.base import (
    PARAM_NODES,
    BackendError,
    Frame,
    NodeInfo,
    snap_value,
)
from octacam.cameras.registry import BackendUnavailable

log = logging.getLogger("octacam")

# The runtime is registered in ldconfig as libSpinnaker_C.so (SDK 4.3.x); loading
# by soname lets the dynamic linker resolve it from /opt/spinnaker/lib.
_LIB_NAME = "libSpinnaker_C.so"

# Buffer length for the C string getters (serial, unit, enum symbolic). 256 is
# the length FLIR's own C examples use (MAX_BUFF_LEN).
_MAX_BUFF_LEN = 256

# spinError codes (SpinnakerDefsC.h). SUCCESS is 0; a grab that times out returns
# TIMEOUT and is treated as "no frame this cycle", never an error.
SPINNAKER_ERR_SUCCESS = 0
SPINNAKER_ERR_TIMEOUT = -1011

_ERR_NAMES = {
    -1001: "ERR_ERROR",
    -1002: "ERR_NOT_INITIALIZED",
    -1003: "ERR_NOT_IMPLEMENTED",
    -1004: "ERR_RESOURCE_IN_USE",
    -1005: "ERR_ACCESS_DENIED",
    -1006: "ERR_INVALID_HANDLE",
    -1008: "ERR_NO_DATA",
    -1009: "ERR_INVALID_PARAMETER",
    -1010: "ERR_IO",
    -1011: "ERR_TIMEOUT",
    -1014: "ERR_NOT_AVAILABLE",
    -1022: "ERR_BUSY",
    -2006: "ERR_GENICAM_ACCESS",
    -2007: "ERR_GENICAM_TIMEOUT",
}

# Spinnaker node interface types differ per parameter; the rest are floats. Int
# nodes expose min/max/inc but no unit; float nodes expose min/max/unit but no
# inc (SpinnakerGenApiC.h has no spinIntegerGetUnit / spinFloatGetInc), so those
# NodeInfo fields are None for the respective kind. snap_value tolerates both.
_INT_PARAMS = frozenset({"width", "height", "offset_x", "offset_y"})

# The System singleton and its camera list are held for the whole session and
# released exactly once in teardown(), after every camera has been de-inited.
_system: Any = None
_cam_list: Any = None

# Camera handles enumerate_spinnaker() has handed out that are not yet released,
# keyed by id() (a spinCamera is an opaque ctypes handle, so its identity — not
# a value — is the key; works for the fake handle in tests too). A handle left
# outstanding when the System is released makes Spinnaker's USB transport abort
# the whole process with a libusb ``usbi_mutex_destroy`` pthread assertion (exit
# 134): the dangling device reference trips libusb's own teardown. In the normal
# path SpinnakerBackend.close() releases every handle, but an enumerate-only
# probe (``octacam doctor``, a scratch script) or a killed/hung record never
# closes — teardown() releases whatever is still here first, so an abnormal
# shutdown degrades to a clean release instead of SIGABRT.
_outstanding: dict[int, Any] = {}

# The loaded binding, created lazily by _spin(). Tests replace it with a fake so
# the backend orchestration is exercised without the SDK or hardware.
_facade: "_Spinnaker | None" = None


def _err_name(err: int) -> str:
    return _ERR_NAMES.get(err, f"spinError {err}")


def _chk(err: int, what: str) -> None:
    """Raise BackendError unless ``err`` is SPINNAKER_ERR_SUCCESS."""
    if err != SPINNAKER_ERR_SUCCESS:
        raise BackendError(f"{what} failed ({_err_name(err)})")


def _configure(lib) -> None:
    """Set argtypes/restype for every C function we call.

    Mandatory on 64-bit: without argtypes, ctypes passes Python ints as 32-bit
    ``c_int`` and truncates the opaque 64-bit handles/pointers, corrupting the
    call. Every Spinnaker C function returns ``spinError`` (an int).
    """
    P = ctypes.POINTER
    v = ctypes.c_void_p
    sz = ctypes.c_size_t
    i64 = ctypes.c_int64
    u64 = ctypes.c_uint64
    dbl = ctypes.c_double
    u8 = ctypes.c_uint8
    ch = ctypes.c_char_p
    specs = {
        # System / camera list
        "spinSystemGetInstance": [P(v)],
        "spinSystemReleaseInstance": [v],
        "spinSystemGetCameras": [v, v],
        "spinCameraListCreateEmpty": [P(v)],
        "spinCameraListClear": [v],
        "spinCameraListDestroy": [v],
        "spinCameraListGetSize": [v, P(sz)],
        "spinCameraListGet": [v, sz, P(v)],
        # Camera lifecycle
        "spinCameraInit": [v],
        "spinCameraDeInit": [v],
        "spinCameraRelease": [v],
        "spinCameraIsInitialized": [v, P(u8)],
        "spinCameraIsStreaming": [v, P(u8)],
        "spinCameraGetNodeMap": [v, P(v)],
        "spinCameraGetTLDeviceNodeMap": [v, P(v)],
        "spinCameraGetTLStreamNodeMap": [v, P(v)],
        "spinCameraGetDeviceID": [v, ch, P(sz)],
        "spinCameraBeginAcquisition": [v],
        "spinCameraEndAcquisition": [v],
        "spinCameraGetNextImageEx": [v, u64, P(v)],
        # Image
        "spinImageIsIncomplete": [v, P(u8)],
        "spinImageGetWidth": [v, P(sz)],
        "spinImageGetHeight": [v, P(sz)],
        "spinImageGetTimeStamp": [v, P(u64)],
        "spinImageGetData": [v, P(v)],
        "spinImageGetStride": [v, P(sz)],
        "spinImageRelease": [v],
        # GenApi nodes
        "spinNodeMapGetNode": [v, ch, P(v)],
        "spinNodeIsAvailable": [v, P(u8)],
        "spinNodeIsReadable": [v, P(u8)],
        "spinNodeIsWritable": [v, P(u8)],
        "spinIntegerGetValue": [v, P(i64)],
        "spinIntegerSetValue": [v, i64],
        "spinIntegerGetMin": [v, P(i64)],
        "spinIntegerGetMax": [v, P(i64)],
        "spinIntegerGetInc": [v, P(i64)],
        "spinFloatGetValue": [v, P(dbl)],
        "spinFloatSetValue": [v, dbl],
        "spinFloatGetMin": [v, P(dbl)],
        "spinFloatGetMax": [v, P(dbl)],
        "spinFloatGetUnit": [v, ch, P(sz)],
        "spinEnumerationGetEntryByName": [v, ch, P(v)],
        "spinEnumerationGetCurrentEntry": [v, P(v)],
        "spinEnumerationEntryGetIntValue": [v, P(i64)],
        "spinEnumerationSetIntValue": [v, i64],
        "spinEnumerationEntryGetSymbolic": [v, ch, P(sz)],
        "spinStringGetValue": [v, ch, P(sz)],
        "spinCommandExecute": [v],
    }
    for name, argtypes in specs.items():
        fn = getattr(lib, name)
        fn.restype = ctypes.c_int
        fn.argtypes = argtypes


class _Spinnaker:
    """Thin typed binding over ``libSpinnaker_C.so``.

    Every method wraps one small C call sequence and raises :class:`BackendError`
    on failure, so :class:`SpinnakerBackend` reads like the PySpin backend. Node
    operations take a nodemap handle plus the SFNC node name; camera/image
    operations take the relevant opaque handle.
    """

    def __init__(self, lib):
        self._lib = lib
        _configure(lib)

    # ------------------------------------------------------------ low-level
    def _node(self, nodemap, name: str):
        """Fetch a node handle by name; raise BackendError if it is absent."""
        h = ctypes.c_void_p()
        _chk(
            self._lib.spinNodeMapGetNode(
                nodemap, name.encode("ascii"), ctypes.byref(h)
            ),
            f"GetNode {name}",
        )
        if not h.value:
            raise BackendError(f"node {name} not found")
        return h

    def _flag(self, fn, handle) -> bool:
        b = ctypes.c_uint8(0)
        return fn(handle, ctypes.byref(b)) == SPINNAKER_ERR_SUCCESS and bool(b.value)

    def _readable(self, handle) -> bool:
        return self._flag(self._lib.spinNodeIsReadable, handle)

    def _writable(self, handle) -> bool:
        return self._flag(self._lib.spinNodeIsWritable, handle)

    def _string_from(self, fn, handle) -> str | None:
        buf = ctypes.create_string_buffer(_MAX_BUFF_LEN)
        n = ctypes.c_size_t(_MAX_BUFF_LEN)
        if fn(handle, buf, ctypes.byref(n)) != SPINNAKER_ERR_SUCCESS:
            return None
        return buf.value.decode("ascii", "replace") or None

    def _opt_i64(self, fn, handle) -> int | None:
        val = ctypes.c_int64()
        return (
            val.value
            if fn(handle, ctypes.byref(val)) == SPINNAKER_ERR_SUCCESS
            else None
        )

    def _opt_f64(self, fn, handle) -> float | None:
        val = ctypes.c_double()
        return (
            val.value
            if fn(handle, ctypes.byref(val)) == SPINNAKER_ERR_SUCCESS
            else None
        )

    # ------------------------------------------------------- system / cameras
    def get_system(self):
        h = ctypes.c_void_p()
        _chk(self._lib.spinSystemGetInstance(ctypes.byref(h)), "spinSystemGetInstance")
        return h

    def system_release_instance(self, hsystem) -> None:
        _chk(self._lib.spinSystemReleaseInstance(hsystem), "spinSystemReleaseInstance")

    def create_camera_list(self):
        h = ctypes.c_void_p()
        _chk(
            self._lib.spinCameraListCreateEmpty(ctypes.byref(h)),
            "spinCameraListCreateEmpty",
        )
        return h

    def system_get_cameras(self, hsystem, hcamlist) -> None:
        _chk(self._lib.spinSystemGetCameras(hsystem, hcamlist), "spinSystemGetCameras")

    def camera_list_size(self, hcamlist) -> int:
        n = ctypes.c_size_t()
        _chk(
            self._lib.spinCameraListGetSize(hcamlist, ctypes.byref(n)),
            "spinCameraListGetSize",
        )
        return int(n.value)

    def camera_list_get(self, hcamlist, index: int):
        h = ctypes.c_void_p()
        _chk(
            self._lib.spinCameraListGet(
                hcamlist, ctypes.c_size_t(index), ctypes.byref(h)
            ),
            "spinCameraListGet",
        )
        return h

    def camera_list_clear(self, hcamlist) -> None:
        _chk(self._lib.spinCameraListClear(hcamlist), "spinCameraListClear")

    def camera_list_destroy(self, hcamlist) -> None:
        _chk(self._lib.spinCameraListDestroy(hcamlist), "spinCameraListDestroy")

    def read_serial(self, hcam) -> str:
        """DeviceSerialNumber from the TL device nodemap (readable pre-Init).

        Falls back to the GenTL device id, then an empty string — mirrors the
        PySpin backend's ``GetUniqueID`` fallback.
        """
        try:
            hmap = self._out_handle(self._lib.spinCameraGetTLDeviceNodeMap, hcam)
            serial = self.read_string(hmap, "DeviceSerialNumber")
            if serial:
                return serial
        except BackendError:
            pass
        buf = ctypes.create_string_buffer(_MAX_BUFF_LEN)
        n = ctypes.c_size_t(_MAX_BUFF_LEN)
        if (
            self._lib.spinCameraGetDeviceID(hcam, buf, ctypes.byref(n))
            == SPINNAKER_ERR_SUCCESS
        ):
            return buf.value.decode("ascii", "replace")
        return ""

    # ------------------------------------------------------- camera lifecycle
    def _out_handle(self, fn, hcam):
        h = ctypes.c_void_p()
        _chk(fn(hcam, ctypes.byref(h)), fn.__name__)
        return h

    def camera_init(self, hcam) -> None:
        _chk(self._lib.spinCameraInit(hcam), "spinCameraInit")

    def camera_deinit(self, hcam) -> None:
        _chk(self._lib.spinCameraDeInit(hcam), "spinCameraDeInit")

    def camera_release(self, hcam) -> None:
        _chk(self._lib.spinCameraRelease(hcam), "spinCameraRelease")

    def camera_is_initialized(self, hcam) -> bool:
        return self._flag(self._lib.spinCameraIsInitialized, hcam)

    def camera_is_streaming(self, hcam) -> bool:
        return self._flag(self._lib.spinCameraIsStreaming, hcam)

    def camera_get_nodemap(self, hcam):
        return self._out_handle(self._lib.spinCameraGetNodeMap, hcam)

    def camera_get_tl_stream_nodemap(self, hcam):
        return self._out_handle(self._lib.spinCameraGetTLStreamNodeMap, hcam)

    def begin_acquisition(self, hcam) -> None:
        _chk(self._lib.spinCameraBeginAcquisition(hcam), "spinCameraBeginAcquisition")

    def end_acquisition(self, hcam) -> None:
        _chk(self._lib.spinCameraEndAcquisition(hcam), "spinCameraEndAcquisition")

    # ------------------------------------------------------------- node I/O
    def read_number(self, nodemap, name: str, is_int: bool) -> NodeInfo:
        node = self._node(nodemap, name)
        if not self._readable(node):
            raise BackendError(f"node {name} is not readable")
        writable = self._writable(node)
        if is_int:
            value = ctypes.c_int64()
            _chk(
                self._lib.spinIntegerGetValue(node, ctypes.byref(value)), f"read {name}"
            )
            return NodeInfo(
                value=int(value.value),
                min=self._opt_i64(self._lib.spinIntegerGetMin, node),
                max=self._opt_i64(self._lib.spinIntegerGetMax, node),
                inc=self._opt_i64(self._lib.spinIntegerGetInc, node),
                unit=None,
                writable=writable,
            )
        value_f = ctypes.c_double()
        _chk(self._lib.spinFloatGetValue(node, ctypes.byref(value_f)), f"read {name}")
        return NodeInfo(
            value=float(value_f.value),
            min=self._opt_f64(self._lib.spinFloatGetMin, node),
            max=self._opt_f64(self._lib.spinFloatGetMax, node),
            inc=None,
            unit=self._string_from(self._lib.spinFloatGetUnit, node),
            writable=writable,
        )

    def write_number(self, nodemap, name: str, value: float, is_int: bool) -> None:
        node = self._node(nodemap, name)
        if not self._writable(node):
            raise BackendError(f"node {name} is not writable")
        if is_int:
            _chk(
                self._lib.spinIntegerSetValue(node, ctypes.c_int64(int(value))),
                f"set {name}",
            )
        else:
            _chk(
                self._lib.spinFloatSetValue(node, ctypes.c_double(float(value))),
                f"set {name}",
            )

    def read_string(self, nodemap, name: str) -> str | None:
        try:
            node = self._node(nodemap, name)
        except BackendError:
            return None
        if not self._readable(node):
            return None
        return self._string_from(self._lib.spinStringGetValue, node)

    def set_enum(self, nodemap, name: str, symbolic: str) -> None:
        node = self._node(nodemap, name)
        if not self._writable(node):
            raise BackendError(f"enumeration {name} is not writable")
        entry = ctypes.c_void_p()
        _chk(
            self._lib.spinEnumerationGetEntryByName(
                node, symbolic.encode("ascii"), ctypes.byref(entry)
            ),
            f"GetEntryByName {name}",
        )
        if not entry.value:
            raise BackendError(f"enumeration {name} has no entry {symbolic!r}")
        int_value = ctypes.c_int64()
        _chk(
            self._lib.spinEnumerationEntryGetIntValue(entry, ctypes.byref(int_value)),
            f"EntryGetIntValue {name}",
        )
        _chk(
            self._lib.spinEnumerationSetIntValue(node, ctypes.c_int64(int_value.value)),
            f"SetIntValue {name}",
        )

    def get_enum(self, nodemap, name: str) -> str | None:
        try:
            node = self._node(nodemap, name)
        except BackendError:
            return None
        if not self._readable(node):
            return None
        entry = ctypes.c_void_p()
        if (
            self._lib.spinEnumerationGetCurrentEntry(node, ctypes.byref(entry))
            != SPINNAKER_ERR_SUCCESS
            or not entry.value
        ):
            return None
        return self._string_from(self._lib.spinEnumerationEntryGetSymbolic, entry)

    def execute_command(self, nodemap, name: str) -> None:
        node = self._node(nodemap, name)
        _chk(self._lib.spinCommandExecute(node), f"execute {name}")

    # --------------------------------------------------------------- imaging
    def get_next_image(self, hcam, timeout_ms: int):
        """Block up to ``timeout_ms`` for the next image; None if none arrives.

        This is the GIL-free grab: ctypes releases the GIL for the whole
        ``spinCameraGetNextImageEx`` wait, so a slow FLIR here never freezes the
        other camera threads (the pycameleon failure mode this backend replaces).
        A timeout — the normal "no triggered frame yet" case — returns None, not
        an error, so the caller never raises out of the grab loop.
        """
        h = ctypes.c_void_p()
        err = self._lib.spinCameraGetNextImageEx(
            hcam, ctypes.c_uint64(int(timeout_ms)), ctypes.byref(h)
        )
        if err != SPINNAKER_ERR_SUCCESS or not h.value:
            if err not in (SPINNAKER_ERR_SUCCESS, SPINNAKER_ERR_TIMEOUT):
                log.debug("spinCameraGetNextImageEx: %s", _err_name(err))
            return None
        return h

    def image_incomplete(self, himage) -> bool:
        b = ctypes.c_uint8(0)
        # Treat an unreadable status as incomplete (skip the frame) rather than
        # trusting a garbage buffer.
        if (
            self._lib.spinImageIsIncomplete(himage, ctypes.byref(b))
            != SPINNAKER_ERR_SUCCESS
        ):
            return True
        return bool(b.value)

    def image_timestamp(self, himage) -> int:
        v = ctypes.c_uint64()
        if (
            self._lib.spinImageGetTimeStamp(himage, ctypes.byref(v))
            == SPINNAKER_ERR_SUCCESS
        ):
            return int(v.value)
        return 0

    def image_array(self, himage) -> np.ndarray:
        """Owned 2-D uint8 copy of a Mono8 image (safe after ImageRelease)."""
        w = ctypes.c_size_t()
        h = ctypes.c_size_t()
        _chk(self._lib.spinImageGetWidth(himage, ctypes.byref(w)), "spinImageGetWidth")
        _chk(
            self._lib.spinImageGetHeight(himage, ctypes.byref(h)), "spinImageGetHeight"
        )
        width, height = int(w.value), int(h.value)
        data = ctypes.c_void_p()
        _chk(self._lib.spinImageGetData(himage, ctypes.byref(data)), "spinImageGetData")
        if not data.value or width == 0 or height == 0:
            raise BackendError("image has no data")
        # Honour the row stride so a padded buffer (stride > width) reshapes
        # correctly; Mono8 from a USB3 FLIR is usually unpadded (stride == width).
        stride = ctypes.c_size_t(0)
        ok = (
            self._lib.spinImageGetStride(himage, ctypes.byref(stride))
            == SPINNAKER_ERR_SUCCESS
        )
        row = stride.value if (ok and stride.value >= width) else width
        buffer = (ctypes.c_ubyte * (row * height)).from_address(data.value)
        # as_array views the SDK buffer in place; .copy() owns the pixels before
        # the SDK recycles that buffer on Release.
        flat = np.ctypeslib.as_array(buffer)
        return flat.reshape(height, row)[:, :width].copy()

    def image_release(self, himage) -> None:
        self._lib.spinImageRelease(himage)


def _spin() -> _Spinnaker:
    """Return the loaded binding, or raise a clean BackendUnavailable.

    Loads ``libSpinnaker_C.so`` lazily on first use; a box without the Spinnaker
    SDK surfaces a :class:`BackendUnavailable` instead of a raw ``OSError``, so
    the tier self-disables in the cascade exactly like FLIR/PySpin.
    """
    global _facade
    if _facade is None:
        try:
            lib = ctypes.CDLL(_LIB_NAME)
        except OSError as e:
            raise BackendUnavailable(
                "spinnaker",
                "the Spinnaker SDK (libSpinnaker_C.so) is not installed",
            ) from e
        _facade = _Spinnaker(lib)
    return _facade


def ensure_available() -> None:
    """Raise BackendUnavailable if libSpinnaker_C.so cannot be loaded."""
    _spin()


class SpinnakerBackend(SoftwareTriggerHandoff):
    """A single FLIR camera driven through the Spinnaker C API via ctypes."""

    extension = "json"

    def __init__(self, cam: Any):
        # ``cam`` is an opaque spinCamera handle from enumerate_spinnaker(); it is
        # released in close() and set to None so a second close (or a post-close
        # query) is a safe no-op. Typed Any so the close()-sets-None lifecycle
        # does not trip the optional-access checker on every method.
        self._cam: Any = cam
        self._nodemap: Any = None
        self._stream_nodemap: Any = None
        self._serial = _spin().read_serial(cam)
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
            spin.camera_init(self._cam)
        except BackendError as e:
            # Usually the camera is already in use by another process (a second
            # `octacam gui` on the rig). It is already a BackendError; re-raise so
            # the caller reports it cleanly instead of a raw traceback.
            raise BackendError(str(e)) from e
        self._nodemap = spin.camera_get_nodemap(self._cam)
        try:
            self._stream_nodemap = spin.camera_get_tl_stream_nodemap(self._cam)
        except BackendError as e:
            self._stream_nodemap = None
            log.debug("No TL stream nodemap on camera %s: %s", self._serial, e)
        # Force monochrome so image_array yields a 2-D uint8 array matching the
        # GRAY8 video writer.
        try:
            spin.set_enum(self._nodemap, "PixelFormat", "Mono8")
        except BackendError as e:
            log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)
        self._maximize_link_throughput()

    def _maximize_link_throughput(self) -> None:
        """Raise DeviceLinkThroughputLimit to the device max (best-effort).

        FLIR ships this node capped below the sensor's real ceiling — on the
        GS3-U3-41C6NIR it defaults to 350.6 MB/s while DeviceMaxThroughput is
        384.4 MB/s. At 2048² Mono8 that is the difference between an ~83.6 fps and
        an ~91.6 fps transfer ceiling, i.e. between missing and hitting the
        camera's 90 fps spec (measured: software-triggered grab 64 → 90 fps just
        from raising this limit — the exposure then fully overlaps the readout so
        the grab loop becomes transfer-bound, no trigger pipelining needed).

        Uncapping the per-camera limit lets each camera use as much USB3 bandwidth
        as it can; on a rig where several cameras share one bus the *bus* is the
        ceiling and the producer arbitrates (a saturated bus yields skipped frames,
        which retrieve() already drops as incomplete — never corrupt data). A model
        without the node keeps its default. Set once at open so both preview and
        record benefit.
        """
        if self._nodemap is None:
            return
        spin = _spin()
        try:
            info = spin.read_number(self._nodemap, "DeviceLinkThroughputLimit", True)
        except BackendError as e:
            log.debug("No DeviceLinkThroughputLimit on camera %s: %s", self._serial, e)
            return
        if info.max is None or not info.writable or info.value >= info.max:
            return
        try:
            spin.write_number(
                self._nodemap, "DeviceLinkThroughputLimit", info.max, True
            )
            log.debug(
                "Camera %s: DeviceLinkThroughputLimit %d -> %d (max)",
                self._serial,
                info.value,
                info.max,
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
        spin = _spin()
        # Flip the hand-off flag first (defensive; stop_grab normally did it).
        self._end_grab()
        try:
            if spin.camera_is_streaming(cam):
                spin.end_acquisition(cam)
        except Exception:
            pass
        try:
            if spin.camera_is_initialized(cam):
                spin.camera_deinit(cam)
        except Exception:
            pass
        # Release the handle obtained from spinCameraListGet (paired 1:1). This
        # must happen before teardown() clears/destroys the list and releases the
        # System, mirroring FLIR's C examples.
        try:
            spin.camera_release(cam)
        except Exception:
            pass
        # Drop it from the outstanding set so teardown() does not release it a
        # second time (a double spinCameraRelease is itself an error).
        _outstanding.pop(id(cam), None)
        self._cam = None
        self._nodemap = None
        self._stream_nodemap = None

    def is_open(self) -> bool:
        if self._cam is None:
            return False
        try:
            return _spin().camera_is_initialized(self._cam)
        except Exception:
            return False

    def is_grabbing(self) -> bool:
        # The hand-off flag is authoritative (see FlirBackend.is_grabbing):
        # stop_grab flips it and wakes a blocked retrieve before EndAcquisition().
        return self._cam is not None and self._grabbing

    def width(self) -> int:
        return int(_spin().read_number(self._nodemap, "Width", True).value)

    def height(self) -> int:
        return int(_spin().read_number(self._nodemap, "Height", True).value)

    # ----------------------------------------------------- node plumbing

    def _set_enum(self, name: str, value: str) -> None:
        _spin().set_enum(self._nodemap, name, value)

    def _get_enum(self, name: str) -> str | None:
        if self._nodemap is None:
            return None
        return _spin().get_enum(self._nodemap, name)

    # ----------------------------------------------------- sensor parameters

    def read_node(self, name: str) -> NodeInfo:
        if self._nodemap is None:
            raise BackendError("camera is not open")
        return _spin().read_number(
            self._nodemap, PARAM_NODES[name], name in _INT_PARAMS
        )

    def write_node(self, name: str, value: float) -> None:
        if self._nodemap is None:
            raise BackendError("camera is not open")
        _spin().write_number(
            self._nodemap, PARAM_NODES[name], value, name in _INT_PARAMS
        )

    def load_params(self, config_str: str) -> None:
        if config_str:
            try:
                data = json.loads(config_str)
            except (ValueError, TypeError) as e:
                raise BackendError(f"invalid Spinnaker parameters: {e}") from e
            if not isinstance(data, dict):
                raise BackendError("invalid Spinnaker parameters: expected an object")
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

    def _enable_trigger_overlap(self) -> None:
        """Let a trigger be accepted during the previous frame's readout.

        Without this (``TriggerOverlap=Off``, the FLIR default) a FrameStart
        software trigger fired while the sensor is still reading out the previous
        frame is **silently ignored**, so the camera accepts only ~every other
        trigger — roughly halving the software-triggered frame rate and adding a
        full grab-timeout stall on each dropped trigger (measured: 4.7 fps with
        200 ms max stalls at 4 ms exposure). ``ReadOut`` pipelines back-to-back
        triggers and restores the sensor's real rate (measured: 64 fps, 15 ms
        max, same exposure). Best-effort: a model without the node keeps its
        default and behaves as before.
        """
        try:
            self._set_enum("TriggerOverlap", "ReadOut")
        except BackendError as e:
            log.debug("Could not set TriggerOverlap on camera %s: %s", self._serial, e)

    def enable_frame_trigger(self) -> None:
        if not self.is_open():
            return
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
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")
        self._set_enum("TriggerSource", "Software")
        self._enable_trigger_overlap()

    def trigger_once(self) -> None:
        # Only bump the pending counter; retrieve() fires the device trigger on
        # the grab thread so the shared trigger timer never blocks on this camera.
        self._bump_trigger()

    def begin_freerun(self) -> bool:
        """Switch to continuous free-run (TriggerMode Off) for the benchmark.

        Best-effort: a failure returns False so the benchmark skips the free-run
        ceiling for this camera. A later ``begin_software_trigger_preview`` re-arms
        the FrameStart trigger, so no explicit restore is needed.
        """
        try:
            self._set_enum("TriggerMode", "Off")
            try:
                self._set_enum("AcquisitionMode", "Continuous")
            except BackendError:
                pass
            return True
        except BackendError as e:
            log.debug("free-run unsupported on camera %s: %s", self._serial, e)
            return False

    def retrieve_freerun(self, timeout_ms: int, wants_array) -> Frame | None:
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
            spin.set_enum(self._nodemap, "AcquisitionMode", "Continuous")
        except BackendError as e:
            # FLIR defaults to Continuous; a rejected write is not fatal.
            log.debug("Could not set AcquisitionMode on camera %s: %s", self._serial, e)
        # Buffer handling lives on the transport-layer stream nodemap: NewestOnly
        # for preview (≈ LatestImageOnly), OldestFirst for recording (≈ OneByOne).
        if self._stream_nodemap is not None:
            try:
                spin.set_enum(
                    self._stream_nodemap, "StreamBufferHandlingMode", buffer_mode
                )
            except BackendError as e:
                log.debug(
                    "Could not set buffer mode %s on camera %s: %s",
                    buffer_mode,
                    self._serial,
                    e,
                )
        try:
            spin.begin_acquisition(self._cam)
        except BackendError as e:
            # Name the camera (mirrors the Basler "insufficient resources" hint).
            log.error("Failed to start streaming on camera %s: %s", self._serial, e)
            raise
        self._begin_grab()

    def start_grab_preview(self) -> None:
        self._begin_acquisition("NewestOnly")

    def start_grab_record(self) -> bool:
        # Spinnaker has no WaitForFrameTriggerReady; the camera arms on the first
        # software trigger, so report ready once acquisition has begun.
        self._begin_acquisition("OldestFirst")
        return True

    def stop_grab(self) -> None:
        # Flip the hand-off flag and wake any blocked retrieve BEFORE the native
        # stop, so the grab loop sees "not grabbing" immediately.
        self._end_grab()
        cam = self._cam
        if cam is None:
            return
        try:
            spin = _spin()
            if spin.camera_is_streaming(cam):
                spin.end_acquisition(cam)
        except Exception:
            pass

    def retrieve(self, timeout_ms: int, wants_array) -> Frame | None:
        # Wait for a pending software trigger, then fire exactly one device
        # trigger and fetch exactly one frame on this camera's own grab thread.
        if not self._wait_pending(timeout_ms):
            return None
        cam = self._cam
        if cam is None or not self._grabbing:
            return None
        spin = _spin()
        # Fire the device trigger here (not on the caught _trigger_all path). The
        # grab loop does not wrap retrieve() in try/except, so a stop-race or a
        # trigger failure must return None, never raise — a lost trigger is one
        # lost frame, the same as the GetNextImageEx timeout below.
        try:
            spin.execute_command(self._nodemap, "TriggerSoftware")
        except BackendError:
            return None
        return self._fetch_image(cam, timeout_ms, wants_array)

    def _fetch_image(self, cam, timeout_ms: int, wants_array) -> Frame | None:
        # Fetch exactly one image; never raises (a timeout, incomplete or bad
        # frame is one lost frame, as the grab loop expects).
        spin = _spin()
        image = spin.get_next_image(cam, timeout_ms)  # None on timeout — GIL-free
        if image is None:
            return None
        try:
            if spin.image_incomplete(image):
                return None
            timestamp = spin.image_timestamp(image)
            array = None
            if wants_array():
                arr = spin.image_array(image)
                if arr.ndim != 2:
                    log.warning(
                        "Camera %s delivered a non-mono frame; skipping", self._serial
                    )
                    return None
                array = arr
            return (array, timestamp)
        except BackendError as e:
            log.warning("Camera %s: bad frame (%s); skipping", self._serial, e)
            return None
        finally:
            spin.image_release(image)  # MUST release every image


def enumerate_spinnaker(requested_serials: list[str] | None = None):
    """Return ``[(serial, spinCamera), ...]`` for the requested FLIR cameras.

    Holds the System singleton and camera list for the session (released in
    :func:`teardown`). Mirrors the FLIR/PySpin enumeration: all detected cameras
    (sorted) when nothing is requested, else the listed serials in order with a
    warning for any not connected. Camera handles that are *not* handed to a
    backend are released here (each spinCameraListGet is paired 1:1 with
    spinCameraRelease) so unselected cameras do not leak.
    """
    spin = _spin()
    global _system, _cam_list
    # Release any prior session first. Re-enumerating without this (e.g. octacam
    # doctor sweeps the backend list AND the cascade, so it enumerates twice)
    # would overwrite _system/_cam_list and orphan the previous System with its
    # camera handles still referenced; releasing that stale System late — at
    # process exit — aborts via a libusb assertion. teardown() is idempotent and
    # clears the outstanding-handle set, so a fresh enumeration starts clean.
    if _system is not None or _cam_list is not None:
        teardown()
    _system = spin.get_system()
    _cam_list = spin.create_camera_list()
    spin.system_get_cameras(_system, _cam_list)
    count = spin.camera_list_size(_cam_list)
    if count == 0:
        teardown()
        return []
    # Debug, not info: the auto cascade enumerates every tier, so CameraSystem
    # logs the single attributed "Detected N" summary (see basler backend).
    log.debug("spinnaker enumerated %d camera(s)", count)

    all_cams: list[tuple[str, Any]] = []
    for i in range(count):
        hcam = spin.camera_list_get(_cam_list, i)
        all_cams.append((spin.read_serial(hcam), hcam))
    by_serial = dict(all_cams)

    detected = [serial for serial, _hcam in all_cams]
    final = sorted(detected) if not requested_serials else list(requested_serials)
    out: list[tuple[str, Any]] = []
    used: set[str] = set()
    for serial in final:
        hcam = by_serial.get(serial)
        if hcam is None:
            log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, hcam))
        used.add(serial)
        # Track the handed-out handle so teardown() can release it if the caller
        # never close()s the camera (see _outstanding); close() removes it again.
        _outstanding[id(hcam)] = hcam
    # Release the handles we will not open, so they do not leak until teardown.
    for serial, hcam in all_cams:
        if serial not in used:
            try:
                spin.camera_release(hcam)
            except BackendError:
                pass
    return out


def teardown() -> None:
    """Release the Spinnaker System singleton, once, after cameras are closed.

    Called by CameraSystem.close() through the registry. The camera list is
    cleared and destroyed and the System instance released only after every
    SpinnakerBackend.close() has de-inited and released its camera handle, or
    Spinnaker reports cameras still in use.
    """
    global _system, _cam_list
    spin = _facade
    # Release any handles enumerate handed out that were never close()d, so the
    # System is not released with a dangling device reference (which aborts the
    # process via a libusb pthread assertion — see _outstanding). Best-effort and
    # idempotent: close() already dropped the handles it released, so in the
    # normal CameraSystem.close() path (every camera closes before teardown) this
    # loop is empty; it only fires for a leaked handle (probe / abnormal exit).
    if spin is not None and _outstanding:
        for handle in list(_outstanding.values()):
            try:
                spin.camera_release(handle)
            except Exception:
                pass
    _outstanding.clear()
    if _cam_list is not None:
        if spin is not None:
            try:
                spin.camera_list_clear(_cam_list)
            except Exception:
                pass
            try:
                spin.camera_list_destroy(_cam_list)
            except Exception:
                pass
        _cam_list = None
    if _system is not None:
        if spin is not None:
            try:
                spin.system_release_instance(_system)
            except Exception:
                pass
        _system = None


# Safety net: guarantee the System (and any handle enumerate handed out) is
# released at interpreter shutdown even for a caller that enumerates but never
# runs teardown() — notably ``octacam doctor``/``benchmark``, which read serials
# and drop the handles. Without this such a path leaves a dangling device
# reference that aborts the whole process with a libusb ``usbi_mutex_destroy``
# assertion at exit. teardown() is idempotent and a no-op when the SDK was never
# loaded, so this composes with CameraSystem.close() already calling it.
atexit.register(teardown)
