"""The Spinnaker SDK's C API over `ctypes`: the `spinnaker` tier's
`FlirBinding`.

The FLIR tier when PySpin is missing: it needs only `libSpinnaker_C.so`. A
`CDLL` releases the GIL around every foreign call, so a blocking
`spinCameraGetNextImageEx` never stalls the other cameras' threads. It drives
the C API, not the Spinnaker GenTL producer, whose `DevClose` deadlocks
holding the GIL.
"""

import atexit
import ctypes
import logging
from typing import Any

import numpy as np

from octacam.cameras.base import BackendError
from octacam.cameras.flir import FlirBackend, FlirBinding
from octacam.cameras.genicam import Bounds
from octacam.cameras.registry import BackendSpec, BackendUnavailable

log = logging.getLogger("octacam")

# By soname: the SDK registers /opt/spinnaker/lib with ldconfig.
_LIB_NAME = "libSpinnaker_C.so"

# Buffer length for the C string getters, as FLIR's C examples use.
_MAX_BUFF_LEN = 256

# spinError codes (SpinnakerDefsC.h); a grab TIMEOUT is "no frame yet", not an error.
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

# spinNodeType (SpinnakerGenApiDefsC.h) -> FeatureInfo kind; other types are skipped.
_KIND_BY_NODE_TYPE = {
    2: "int",  # IntegerNode
    3: "bool",  # BooleanNode
    4: "float",  # FloatNode
    5: "command",  # CommandNode
    6: "string",  # StringNode
    8: "enum",  # EnumerationNode
    10: "category",  # CategoryNode
}
# spinVisibility (SpinnakerGenApiDefsC.h).
_VIS_NAME = {0: "beginner", 1: "expert", 2: "guru", 3: "invisible"}

_C_INT = ctypes.c_int
_SIZE = ctypes.c_size_t
_I64 = ctypes.c_int64
_F64 = ctypes.c_double
_U8 = ctypes.c_uint8


def _err_name(err: int) -> str:
    return _ERR_NAMES.get(err, f"spinError {err}")


def _ascii(value: object) -> bytes:
    """GenICam names and strings are ASCII: a BackendError, not a
    UnicodeEncodeError.
    """
    try:
        return str(value).encode("ascii")
    except UnicodeEncodeError as e:
        raise BackendError(f"{value!r} is not ASCII") from e


def _configure(lib) -> None:
    """Set argtypes and restype (spinError) for every C function called.

    Mandatory on 64-bit: without argtypes, ctypes passes a handle as a 32-bit
    `c_int` and truncates it.
    """
    P = ctypes.POINTER
    v = ctypes.c_void_p
    sz = ctypes.c_size_t
    i64 = ctypes.c_int64
    u64 = ctypes.c_uint64
    dbl = ctypes.c_double
    u8 = ctypes.c_uint8
    ch = ctypes.c_char_p
    i32 = ctypes.c_int  # spinNodeType / spinVisibility enums are plain C ints
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
        "spinCameraGetUniqueID": [v, ch, P(sz)],
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
        "spinImageGetBitsPerPixel": [v, P(sz)],
        "spinImageRelease": [v],
        # GenApi nodes
        "spinNodeMapGetNode": [v, ch, P(v)],
        "spinNodeIsAvailable": [v, P(u8)],
        "spinNodeIsReadable": [v, P(u8)],
        "spinNodeIsWritable": [v, P(u8)],
        # Full node-map walk (Camera tab): node metadata + category traversal.
        "spinNodeGetType": [v, P(i32)],
        "spinNodeGetVisibility": [v, P(i32)],
        "spinNodeGetName": [v, ch, P(sz)],
        "spinNodeGetDisplayName": [v, ch, P(sz)],
        "spinNodeGetToolTip": [v, ch, P(sz)],
        "spinNodeGetDescription": [v, ch, P(sz)],
        "spinCategoryGetNumFeatures": [v, P(sz)],
        "spinCategoryGetFeatureByIndex": [v, sz, P(v)],
        "spinEnumerationGetNumEntries": [v, P(sz)],
        "spinEnumerationGetEntryByIndex": [v, sz, P(v)],
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
        "spinStringSetValue": [v, ch],
        "spinBooleanGetValue": [v, P(u8)],
        "spinBooleanSetValue": [v, u8],
        "spinCommandExecute": [v],
    }
    for name, argtypes in specs.items():
        fn = getattr(lib, name)
        fn.restype = ctypes.c_int
        fn.argtypes = argtypes


class _Spinnaker(FlirBinding):
    """The C API binding. Handles are `void*` values (int, or None for NULL).
    Node handles belong to the node map and are freed with the camera handle
    (SpinnakerGenApiC.h), so nothing here releases them.
    """

    tier = "spinnaker"

    def __init__(self, lib: Any):
        super().__init__()
        self._lib = lib
        _configure(lib)
        # kind -> (getter, setter, ctype, Python type) of a plain value node.
        # (getter, setter, ctypes type, Python type) per kind: the SDK's ctypes
        # calls are untyped, as every backend's SDK handles are.
        self._value_calls: dict[str, tuple[Any, Any, Any, Any]] = {
            "int": (lib.spinIntegerGetValue, lib.spinIntegerSetValue, _I64, int),
            "float": (lib.spinFloatGetValue, lib.spinFloatSetValue, _F64, float),
            "bool": (lib.spinBooleanGetValue, lib.spinBooleanSetValue, _U8, int),
        }

    # ------------------------------------------------------------- helpers
    def _call(self, fn: Any, *args: Any) -> None:
        """`fn(*args)`; BackendError unless it returns SUCCESS."""
        err = fn(*args)
        if err != SPINNAKER_ERR_SUCCESS:
            raise BackendError(f"{fn.__name__} failed ({_err_name(err)})")

    def _out(self, fn: Any, *args: Any, ctype: Any = ctypes.c_void_p) -> Any:
        """The value `fn(*args, &out)` writes; BackendError on a failure."""
        out = ctype()
        self._call(fn, *args, ctypes.byref(out))
        return out.value

    def _try(self, fn: Any, *args: Any, ctype: Any = ctypes.c_void_p) -> Any:
        """As `_out`, None on a failure."""
        out = ctype()
        if fn(*args, ctypes.byref(out)) != SPINNAKER_ERR_SUCCESS:
            return None
        return out.value

    def _flag(self, fn: Any, handle: Any) -> bool:
        return bool(self._try(fn, handle, ctype=_U8))

    def _string(self, fn: Any, handle: Any) -> str | None:
        buf = ctypes.create_string_buffer(_MAX_BUFF_LEN)
        n = _SIZE(_MAX_BUFF_LEN)
        if not handle or fn(handle, buf, ctypes.byref(n)) != SPINNAKER_ERR_SUCCESS:
            return None
        return buf.value.decode("ascii", "replace") or None

    # ---------------------------------------------------------------- GenApi
    def node(self, nodemap: Any, name: str) -> Any:
        if not nodemap:
            return None
        return self._try(
            self._lib.spinNodeMapGetNode, nodemap, name.encode("ascii", "replace")
        )

    def children(self, category: Any) -> list[Any]:
        lib = self._lib
        count = self._try(lib.spinCategoryGetNumFeatures, category, ctype=_SIZE) or 0
        out = []
        for i in range(count):
            child = self._try(lib.spinCategoryGetFeatureByIndex, category, _SIZE(i))
            if child and self._flag(lib.spinNodeIsAvailable, child):
                out.append(child)
        return out

    def kind(self, node: Any) -> str | None:
        return _KIND_BY_NODE_TYPE.get(
            self._try(self._lib.spinNodeGetType, node, ctype=_C_INT)
        )

    def name(self, node: Any) -> str | None:
        return self._string(self._lib.spinNodeGetName, node)

    def display_name(self, node: Any) -> str | None:
        return self._string(self._lib.spinNodeGetDisplayName, node)

    def tooltip(self, node: Any) -> str | None:
        lib = self._lib
        return self._string(lib.spinNodeGetToolTip, node) or self._string(
            lib.spinNodeGetDescription, node
        )

    def visibility(self, node: Any) -> str:
        vis = self._try(self._lib.spinNodeGetVisibility, node, ctype=_C_INT)
        return _VIS_NAME.get(vis, "beginner")

    def readable(self, node: Any) -> bool:
        return self._flag(self._lib.spinNodeIsReadable, node)

    def writable(self, node: Any) -> bool:
        return self._flag(self._lib.spinNodeIsWritable, node)

    def value(self, node: Any, kind: str) -> Any:
        lib = self._lib
        if kind in self._value_calls:
            getter, _setter, ctype, _py = self._value_calls[kind]
            got = self._try(getter, node, ctype=ctype)
            return bool(got) if got is not None and kind == "bool" else got
        if kind == "enum":
            entry = self._try(lib.spinEnumerationGetCurrentEntry, node)
            return self._string(lib.spinEnumerationEntryGetSymbolic, entry)
        if kind == "string":
            return self._string(lib.spinStringGetValue, node)
        return None

    def bounds(self, node: Any, kind: str) -> Bounds:
        # The C ABI has no spinIntegerGetUnit or spinFloatGetInc (SDK 4.4).
        lib = self._lib
        if kind == "int":
            return (
                self._try(lib.spinIntegerGetMin, node, ctype=_I64),
                self._try(lib.spinIntegerGetMax, node, ctype=_I64),
                self._try(lib.spinIntegerGetInc, node, ctype=_I64),
                None,
            )
        return (
            self._try(lib.spinFloatGetMin, node, ctype=_F64),
            self._try(lib.spinFloatGetMax, node, ctype=_F64),
            None,
            self._string(lib.spinFloatGetUnit, node),
        )

    def entries(self, node: Any) -> list[tuple[str, bool]]:
        lib = self._lib
        count = self._try(lib.spinEnumerationGetNumEntries, node, ctype=_SIZE) or 0
        out = []
        for i in range(count):
            entry = self._try(lib.spinEnumerationGetEntryByIndex, node, _SIZE(i))
            symbolic = self._string(lib.spinEnumerationEntryGetSymbolic, entry)
            if symbolic:
                out.append((symbolic, self._flag(lib.spinNodeIsAvailable, entry)))
        return out

    def write(self, node: Any, kind: str, value: Any) -> None:
        lib = self._lib
        if kind == "enum":
            entry = self._out(lib.spinEnumerationGetEntryByName, node, _ascii(value))
            if not entry:
                raise BackendError(
                    f"enumeration {self.name(node)} has no entry {value!r}"
                )
            int_value = self._out(
                lib.spinEnumerationEntryGetIntValue, entry, ctype=_I64
            )
            self._call(lib.spinEnumerationSetIntValue, node, _I64(int_value))
        elif kind == "string":
            self._call(lib.spinStringSetValue, node, _ascii(value))
        else:
            _getter, setter, ctype, py = self._value_calls[kind]
            self._call(setter, node, ctype(py(value)))

    def execute(self, node: Any) -> None:
        self._call(self._lib.spinCommandExecute, node)

    # ------------------------------------------------------- System, cameras
    def _get_system(self) -> Any:
        return self._out(self._lib.spinSystemGetInstance)

    def _camera_list(self, system: Any) -> Any:
        lib = self._lib
        cam_list = self._out(lib.spinCameraListCreateEmpty)
        try:
            self._call(lib.spinSystemGetCameras, system, cam_list)
        except BackendError:
            lib.spinCameraListDestroy(cam_list)
            raise
        return cam_list

    def _cameras(self, cam_list: Any) -> list[Any]:
        lib = self._lib
        count = self._out(lib.spinCameraListGetSize, cam_list, ctype=_SIZE)
        return [
            self._out(lib.spinCameraListGet, cam_list, _SIZE(i)) for i in range(count)
        ]

    def _clear_camera_list(self, cam_list: Any) -> None:
        self._lib.spinCameraListClear(cam_list)
        self._lib.spinCameraListDestroy(cam_list)

    def _release_system(self, system: Any) -> None:
        self._lib.spinSystemReleaseInstance(system)

    def _release(self, cam: Any) -> None:
        self._lib.spinCameraRelease(cam)  # pairs spinCameraListGet

    def init(self, cam: Any) -> None:
        self._call(self._lib.spinCameraInit, cam)

    def deinit(self, cam: Any) -> None:
        self._call(self._lib.spinCameraDeInit, cam)

    def is_initialized(self, cam: Any) -> bool:
        return self._flag(self._lib.spinCameraIsInitialized, cam)

    def is_streaming(self, cam: Any) -> bool:
        return self._flag(self._lib.spinCameraIsStreaming, cam)

    def nodemap(self, cam: Any) -> Any:
        return self._out(self._lib.spinCameraGetNodeMap, cam)

    def stream_nodemap(self, cam: Any) -> Any:
        return self._out(self._lib.spinCameraGetTLStreamNodeMap, cam)

    def tl_device_nodemap(self, cam: Any) -> Any:
        return self._out(self._lib.spinCameraGetTLDeviceNodeMap, cam)

    def device_id(self, cam: Any) -> str:
        lib = self._lib
        return (
            self._string(lib.spinCameraGetDeviceID, cam)
            or self._string(lib.spinCameraGetUniqueID, cam)
            or ""
        )

    def begin_acquisition(self, cam: Any) -> None:
        self._call(self._lib.spinCameraBeginAcquisition, cam)

    def end_acquisition(self, cam: Any) -> None:
        self._call(self._lib.spinCameraEndAcquisition, cam)

    # ---------------------------------------------------------------- images
    def next_image(self, cam: Any, timeout_ms: int) -> Any:
        image = ctypes.c_void_p()
        err = self._lib.spinCameraGetNextImageEx(
            cam, ctypes.c_uint64(int(timeout_ms)), ctypes.byref(image)
        )
        if err != SPINNAKER_ERR_SUCCESS or not image.value:
            if err not in (SPINNAKER_ERR_SUCCESS, SPINNAKER_ERR_TIMEOUT):
                log.debug("spinCameraGetNextImageEx: %s", _err_name(err))
            return None
        return image.value

    def image_incomplete(self, image: Any) -> bool:
        incomplete = self._try(self._lib.spinImageIsIncomplete, image, ctype=_U8)
        return incomplete is None or bool(incomplete)

    def image_timestamp(self, image: Any) -> int:
        return int(
            self._try(self._lib.spinImageGetTimeStamp, image, ctype=ctypes.c_uint64)
            or 0
        )

    def image_array(self, image: Any) -> np.ndarray:
        lib = self._lib
        bits = self._try(lib.spinImageGetBitsPerPixel, image, ctype=_SIZE) or 0
        if bits != 8:
            raise BackendError(f"{bits}-bpp (non-Mono8) frame")
        width = self._out(lib.spinImageGetWidth, image, ctype=_SIZE)
        height = self._out(lib.spinImageGetHeight, image, ctype=_SIZE)
        data = self._out(lib.spinImageGetData, image)
        if not data or not width or not height:
            raise BackendError("image has no data")
        # Honor the row stride: a padded buffer has stride > width.
        stride = self._try(lib.spinImageGetStride, image, ctype=_SIZE) or 0
        row = max(stride, width)
        # A view of the SDK buffer, copied before image_release recycles it.
        flat = np.ctypeslib.as_array(
            (ctypes.c_ubyte * (row * height)).from_address(data)
        )
        return flat.reshape(height, row)[:, :width].copy()

    def image_release(self, image: Any) -> None:
        self._lib.spinImageRelease(image)


_binding: FlirBinding | None = None  # loaded by _spin(); tests swap in a fake


def _spin() -> FlirBinding:
    """The binding, loading the library on first use; BackendUnavailable
    without the SDK.
    """
    global _binding
    if _binding is None:
        try:
            lib = ctypes.CDLL(_LIB_NAME)
        except OSError as e:
            raise BackendUnavailable(
                "spinnaker",
                "the Spinnaker SDK (libSpinnaker_C.so) is not installed",
            ) from e
        _binding = _Spinnaker(lib)
    return _binding


def ensure_available() -> None:
    """Raise BackendUnavailable if libSpinnaker_C.so cannot be loaded."""
    _spin()


def teardown() -> None:
    """Release the C API session (`FlirBinding.teardown`), if loaded."""
    if _binding is not None:
        _binding.teardown()


# For paths that enumerate without a CameraSystem (doctor, an aborted run).
atexit.register(teardown)

SPEC = BackendSpec(
    lambda requested_serials=None: _spin().enumerate(requested_serials),
    lambda cam: FlirBackend(_spin(), cam),
    ensure_available=ensure_available,
    read_model=lambda cam: _spin().model(cam),
    teardown=teardown,
)
