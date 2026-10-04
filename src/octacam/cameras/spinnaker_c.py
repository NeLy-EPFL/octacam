"""FLIR / Teledyne Spinnaker backend over the SDK's C API via ``ctypes``.

The FLIR tier when PySpin is missing: it needs only ``libSpinnaker_C.so``, and
mirrors :mod:`octacam.cameras.flir` node for node. A ``CDLL`` releases the GIL
around every foreign call, so a blocking ``spinCameraGetNextImageEx`` never
stalls the other cameras' threads. It drives the C API, not the Spinnaker GenTL
producer, whose ``DevClose`` deadlocks holding the GIL.

:class:`_Spinnaker` is the binding: typed helpers that raise
:class:`BackendError` on any failed ``spinError``. :class:`SpinnakerBackend`
orchestrates it as :class:`~octacam.cameras.flir.FlirBackend` does PySpin.
"""

import atexit
import ctypes
import logging
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from octacam.cameras._genicam_config import (
    MIN_STREAM_BUFFERS,
    RECORD_STREAM_BUFFERS,
    GenICamTriggerConfig,
    dump_config,
    fewer_stream_buffers,
    normalize_trigger_source,
)
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
from octacam.cameras.registry import BackendUnavailable

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

# Integer SFNC nodes; the rest of PARAM_NODES are floats. The C ABI has no
# spinIntegerGetUnit or spinFloatGetInc (SDK 4.4), so an int's unit and a float's
# increment stay None: unitless and continuous on these nodes anyway.
_INT_PARAMS = frozenset({"width", "height", "offset_x", "offset_y"})

# As in flir.py.
STREAM_STATISTICS = (
    "StreamLostFrameCount",
    "StreamDroppedFrameCount",
    "StreamIncompleteFrameCount",
    "StreamDeliveredFrameCount",
)
INCOMPLETE_REPORT_INTERVAL_S = 10.0

# spinNodeType (SpinnakerGenApiDefsC.h) -> widget kind; unmapped types are skipped.
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

# The System singleton and its camera list, held until teardown().
_system: Any = None
_cam_list: Any = None

# Camera handles handed out and not yet released, by id(). The System released
# with one outstanding aborts the process (a libusb usbi_mutex_destroy
# assertion, exit 134), so teardown() releases any handle no close() released,
# as after doctor's enumerate-only probe.
_outstanding: dict[int, Any] = {}

# The loaded binding (_spin()); tests replace it with a fake.
_facade: "_Spinnaker | None" = None


def _snap_int(value: float, node_min: int | None, node_inc: int | None) -> int:
    """Round ``value`` to the node's increment grid from its min (the firmware
    rejects an off-grid write)."""
    snapped = int(round(float(value)))
    if node_inc:
        base = node_min if node_min is not None else 0
        snapped = int(base + round((snapped - base) / node_inc) * node_inc)
    return snapped


def _err_name(err: int) -> str:
    return _ERR_NAMES.get(err, f"spinError {err}")


def _chk(err: int, what: str) -> None:
    """Raise BackendError unless ``err`` is SPINNAKER_ERR_SUCCESS."""
    if err != SPINNAKER_ERR_SUCCESS:
        raise BackendError(f"{what} failed ({_err_name(err)})")


def _configure(lib) -> None:
    """Set argtypes and restype (spinError) for every C function called.

    Mandatory on 64-bit: without argtypes, ctypes passes a handle as a 32-bit
    ``c_int`` and truncates it.
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


class _Spinnaker:
    """Typed binding over ``libSpinnaker_C.so``; failures raise BackendError."""

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
        """DeviceSerialNumber from the TL device node map (readable without
        Init), else the device id, else the unique id."""
        try:
            hmap = self._out_handle(self._lib.spinCameraGetTLDeviceNodeMap, hcam)
            serial = self.read_string(hmap, "DeviceSerialNumber")
            if serial:
                return serial
        except BackendError:
            pass
        for fn in (self._lib.spinCameraGetDeviceID, self._lib.spinCameraGetUniqueID):
            got = self._string_from(fn, hcam)
            if got:
                return got
        return ""

    def read_model(self, hcam) -> str | None:
        """DeviceModelName from the TL device node map, or None; readable
        without Init, so doctor can label a camera in a live session."""
        try:
            hmap = self._out_handle(self._lib.spinCameraGetTLDeviceNodeMap, hcam)
            return self.read_string(hmap, "DeviceModelName") or None
        except BackendError:
            return None

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

    def get_bool(self, nodemap, name: str) -> bool | None:
        try:
            node = self._node(nodemap, name)
        except BackendError:
            return None
        if not self._readable(node):
            return None
        b = ctypes.c_uint8(0)
        if (
            self._lib.spinBooleanGetValue(node, ctypes.byref(b))
            != SPINNAKER_ERR_SUCCESS
        ):
            return None
        return bool(b.value)

    def set_bool(self, nodemap, name: str, value: bool) -> None:
        node = self._node(nodemap, name)
        if not self._writable(node):
            raise BackendError(f"boolean {name} is not writable")
        _chk(
            self._lib.spinBooleanSetValue(node, ctypes.c_uint8(1 if value else 0)),
            f"set {name}",
        )

    def execute_command(self, nodemap, name: str) -> None:
        node = self._node(nodemap, name)
        _chk(self._lib.spinCommandExecute(node), f"execute {name}")

    # ------------------------------------------------------- full node-map walk
    # Node handles belong to the node map and are freed with the camera handle
    # (SpinnakerGenApiC.h), so neither the walk nor _node releases them.
    def _node_type(self, handle) -> int:
        t = ctypes.c_int(-1)
        if self._lib.spinNodeGetType(handle, ctypes.byref(t)) != SPINNAKER_ERR_SUCCESS:
            return -1
        return int(t.value)

    def _visibility(self, handle) -> str:
        vis = ctypes.c_int(0)
        if (
            self._lib.spinNodeGetVisibility(handle, ctypes.byref(vis))
            != SPINNAKER_ERR_SUCCESS
        ):
            return "beginner"
        return _VIS_NAME.get(int(vis.value), "beginner")

    def _available(self, handle) -> bool:
        return self._flag(self._lib.spinNodeIsAvailable, handle)

    def _tooltip(self, handle) -> str | None:
        return self._string_from(
            self._lib.spinNodeGetToolTip, handle
        ) or self._string_from(self._lib.spinNodeGetDescription, handle)

    def _current_enum_symbolic(self, handle) -> str | None:
        entry = ctypes.c_void_p()
        if (
            self._lib.spinEnumerationGetCurrentEntry(handle, ctypes.byref(entry))
            != SPINNAKER_ERR_SUCCESS
            or not entry.value
        ):
            return None
        return self._string_from(self._lib.spinEnumerationEntryGetSymbolic, entry)

    def _enum_entries(self, handle) -> list[dict] | None:
        n = ctypes.c_size_t()
        if (
            self._lib.spinEnumerationGetNumEntries(handle, ctypes.byref(n))
            != SPINNAKER_ERR_SUCCESS
        ):
            return None
        out: list[dict] = []
        for i in range(int(n.value)):
            entry = ctypes.c_void_p()
            if (
                self._lib.spinEnumerationGetEntryByIndex(
                    handle, ctypes.c_size_t(i), ctypes.byref(entry)
                )
                != SPINNAKER_ERR_SUCCESS
                or not entry.value
            ):
                continue
            symbolic = self._string_from(
                self._lib.spinEnumerationEntryGetSymbolic, entry
            )
            if not symbolic:
                continue
            out.append(
                {
                    "value": symbolic,
                    "display": symbolic,
                    "available": self._available(entry),
                }
            )
        return out or None

    def _build_feature(self, handle, kind: str, name: str) -> FeatureInfo:
        """Assemble one FeatureInfo from a node handle already typed as ``kind``."""
        readable = self._readable(handle)
        feature = FeatureInfo(
            name=name,
            display_name=self._string_from(self._lib.spinNodeGetDisplayName, handle)
            or name,
            type=kind,
            readable=readable,
            writable=self._writable(handle),
            visibility=self._visibility(handle),
            tooltip=self._tooltip(handle),
        )
        if kind == "int":
            if readable:
                feature.value = self._opt_i64(self._lib.spinIntegerGetValue, handle)
            feature.min = self._opt_i64(self._lib.spinIntegerGetMin, handle)
            feature.max = self._opt_i64(self._lib.spinIntegerGetMax, handle)
            feature.inc = self._opt_i64(self._lib.spinIntegerGetInc, handle)
        elif kind == "float":
            if readable:
                feature.value = self._opt_f64(self._lib.spinFloatGetValue, handle)
            feature.min = self._opt_f64(self._lib.spinFloatGetMin, handle)
            feature.max = self._opt_f64(self._lib.spinFloatGetMax, handle)
            feature.unit = self._string_from(self._lib.spinFloatGetUnit, handle)
        elif kind == "bool":
            if readable:
                b = ctypes.c_uint8(0)
                if (
                    self._lib.spinBooleanGetValue(handle, ctypes.byref(b))
                    == SPINNAKER_ERR_SUCCESS
                ):
                    feature.value = bool(b.value)
        elif kind == "enum":
            if readable:
                feature.value = self._current_enum_symbolic(handle)
            feature.entries = self._enum_entries(handle)
        elif kind == "string" and readable:
            feature.value = self._string_from(self._lib.spinStringGetValue, handle)
        # command: no value
        return feature

    def _walk(self, category, path: str, out: list[FeatureInfo], seen: set) -> None:
        """Depth-first walk of the GenApi category tree, collecting features."""
        n = ctypes.c_size_t()
        if (
            self._lib.spinCategoryGetNumFeatures(category, ctypes.byref(n))
            != SPINNAKER_ERR_SUCCESS
        ):
            return
        for i in range(int(n.value)):
            child = ctypes.c_void_p()
            if (
                self._lib.spinCategoryGetFeatureByIndex(
                    category, ctypes.c_size_t(i), ctypes.byref(child)
                )
                != SPINNAKER_ERR_SUCCESS
                or not child.value
            ):
                continue
            try:
                if not self._available(child):
                    continue
                if self._visibility(child) not in ("beginner", "expert", "guru"):
                    continue
                kind = _KIND_BY_NODE_TYPE.get(self._node_type(child))
                if kind == "category":
                    label = (
                        self._string_from(self._lib.spinNodeGetDisplayName, child)
                        or self._string_from(self._lib.spinNodeGetName, child)
                        or path
                    )
                    self._walk(child, label, out, seen)
                    continue
                if kind is None:
                    continue
                name = self._string_from(self._lib.spinNodeGetName, child)
                if not name or name in seen:
                    continue
                seen.add(name)
                feature = self._build_feature(child, kind, name)
                feature.category = path or "Other"
                out.append(feature)
            except Exception as e:  # one bad node must not abort the walk
                log.debug("Skipping node during feature walk: %s", e)

    def list_features(self, nodemap) -> list[FeatureInfo]:
        """Every browsable feature in ``nodemap``, grouped by GenApi category."""
        out: list[FeatureInfo] = []
        try:
            root = self._node(nodemap, "Root")
        except BackendError:
            return out
        self._walk(root, "", out, set())
        return out

    def read_feature(self, nodemap, name: str) -> FeatureInfo:
        """Re-read one node into a FeatureInfo (raises BackendError if absent)."""
        node = self._node(nodemap, name)
        kind = _KIND_BY_NODE_TYPE.get(self._node_type(node))
        if kind is None or kind == "category":
            raise BackendError(f"node {name} is not an editable feature")
        return self._build_feature(node, kind, name)

    def write_feature(self, nodemap, name: str, value: object) -> None:
        """Write one node, coercing ``value`` to the node's GenApi type."""
        node = self._node(nodemap, name)
        kind = _KIND_BY_NODE_TYPE.get(self._node_type(node))
        if not self._writable(node):
            raise BackendError(f"node {name} is not writable")
        if kind == "int":
            snapped = _snap_int(
                float(value),  # type: ignore[arg-type]
                self._opt_i64(self._lib.spinIntegerGetMin, node),
                self._opt_i64(self._lib.spinIntegerGetInc, node),
            )
            _chk(
                self._lib.spinIntegerSetValue(node, ctypes.c_int64(snapped)),
                f"set {name}",
            )
        elif kind == "float":
            _chk(
                self._lib.spinFloatSetValue(node, ctypes.c_double(float(value))),  # type: ignore[arg-type]
                f"set {name}",
            )
        elif kind == "bool":
            _chk(
                self._lib.spinBooleanSetValue(
                    node, ctypes.c_uint8(1 if coerce_bool(value) else 0)
                ),
                f"set {name}",
            )
        elif kind == "enum":
            self.set_enum(nodemap, name, str(value))
        elif kind == "string":
            # GenICam strings are ASCII: a BackendError, not a UnicodeEncodeError.
            try:
                encoded = str(value).encode("ascii")
            except UnicodeEncodeError as e:
                raise BackendError(f"{name}: value must be ASCII") from e
            _chk(self._lib.spinStringSetValue(node, encoded), f"set {name}")
        else:
            raise BackendError(f"node {name} is not writable ({kind})")

    # --------------------------------------------------------------- imaging
    def get_next_image(self, hcam, timeout_ms: int):
        """The next image within ``timeout_ms`` (the wait releases the GIL), or
        None on a timeout or error."""
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
        # An unreadable status counts as incomplete: never trust the buffer.
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

    def image_bits_per_pixel(self, himage) -> int:
        """Bits per pixel of the image (8 for Mono8), or 0 if unknown."""
        n = ctypes.c_size_t()
        if (
            self._lib.spinImageGetBitsPerPixel(himage, ctypes.byref(n))
            == SPINNAKER_ERR_SUCCESS
        ):
            return int(n.value)
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
        # Honor the row stride: a padded buffer has stride > width.
        stride = ctypes.c_size_t(0)
        ok = (
            self._lib.spinImageGetStride(himage, ctypes.byref(stride))
            == SPINNAKER_ERR_SUCCESS
        )
        row = stride.value if (ok and stride.value >= width) else width
        buffer = (ctypes.c_ubyte * (row * height)).from_address(data.value)
        # A view of the SDK buffer: copied before Release recycles it.
        flat = np.ctypeslib.as_array(buffer)
        return flat.reshape(height, row)[:, :width].copy()

    def image_release(self, himage) -> None:
        self._lib.spinImageRelease(himage)


def _spin() -> _Spinnaker:
    """The binding, loading the library on first use; BackendUnavailable
    without the SDK."""
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


class SpinnakerBackend(GenICamTriggerConfig, SoftwareTriggerHandoff):
    """A single FLIR camera driven through the Spinnaker C API via ctypes."""

    extension = "txt"

    def __init__(self, cam: Any):
        self._cam: Any = cam  # an opaque spinCamera handle; None once closed
        self._nodemap: Any = None
        self._stream_nodemap: Any = None
        self._serial = _spin().read_serial(cam)
        self._original_trigger_source: str | None = None
        # Incomplete images, as in FlirBackend.
        self._incomplete_images = 0
        self._grab_is_record = False
        self._grab_incomplete = 0
        self._grab_incomplete_logged = 0
        self._incomplete_logged_at = 0.0
        self._init_trigger_handoff()

    @property
    def serial_number(self) -> str:
        return self._serial

    # ------------------------------------------------------------- lifecycle

    def open(self) -> None:
        spin = _spin()
        spin.camera_init(self._cam)
        self._nodemap = spin.camera_get_nodemap(self._cam)
        try:
            self._stream_nodemap = spin.camera_get_tl_stream_nodemap(self._cam)
        except BackendError as e:
            self._stream_nodemap = None
            log.debug("No TL stream nodemap on camera %s: %s", self._serial, e)
        # Mono8, so image_array yields the 2-D uint8 array the GRAY8 writer takes.
        try:
            spin.set_enum(self._nodemap, "PixelFormat", "Mono8")
        except BackendError as e:
            log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)
        self._maximize_link_throughput()

    def _maximize_link_throughput(self) -> None:
        """See :meth:`octacam.cameras.flir.FlirBackend._maximize_link_throughput`."""
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
        # Pairs spinCameraListGet, before teardown() releases the System.
        try:
            spin.camera_release(cam)
        except Exception:
            pass
        # teardown() must not release it again: a double spinCameraRelease errors.
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
        return self._cam is not None and self._grabbing

    def grab_locked_features(self) -> frozenset[str]:
        return GEOMETRY_FEATURES  # the ROI offsets stay writable mid-grab

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

    def _set_bool(self, name: str, value: bool) -> None:
        _spin().set_bool(self._nodemap, name, value)

    def _get_bool(self, name: str) -> bool | None:
        if self._nodemap is None:
            return None
        return _spin().get_bool(self._nodemap, name)

    def _set_number(self, name: str, value: float, is_int: bool) -> None:
        _spin().write_number(self._nodemap, name, value, is_int)

    def _get_number(self, name: str, is_int: bool) -> float | int | None:
        if self._nodemap is None:
            return None
        try:
            return _spin().read_number(self._nodemap, name, is_int).value
        except BackendError:
            return None

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

    def list_features(self) -> list[FeatureInfo]:
        if self._nodemap is None:
            return []
        return _spin().list_features(self._nodemap)

    def read_feature(self, name: str) -> FeatureInfo:
        if self._nodemap is None:
            raise BackendError("camera is not open")
        return _spin().read_feature(self._nodemap, name)

    def write_feature(self, name: str, value: object) -> None:
        if self._nodemap is None:
            raise BackendError("camera is not open")
        _spin().write_feature(self._nodemap, name, value)

    def execute_command(self, name: str) -> None:
        if self._nodemap is None:
            raise BackendError("camera is not open")
        _spin().execute_command(self._nodemap, name)

    def save_params(self) -> str:
        """GenICamTriggerConfig's dump, stamped with the device model."""
        model = (
            _spin().read_string(self._nodemap, "DeviceModelName")
            if self._nodemap is not None
            else None
        )
        return normalize_trigger_source(
            dump_config(self, model), self._original_trigger_source
        )

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        cam = self._cam
        if cam is None or not self._grabbing:
            return None
        return self._fetch_image(cam, timeout_ms, wants_array)

    # ------------------------------------------------------------- grabbing

    def stream_statistics(self) -> dict[str, int]:
        """As :meth:`FlirBackend.stream_statistics`."""
        out = {"IncompleteImagesDiscarded": self._incomplete_images}
        if self._stream_nodemap is None:
            return out
        spin = _spin()
        for name in STREAM_STATISTICS:
            try:
                out[name] = int(spin.read_number(self._stream_nodemap, name, True).value)
            except BackendError:
                continue
        return out

    def _set_stream_buffers(self, buffers: int) -> None:
        """Best-effort: a manual stream buffer count of ``buffers`` (≤ the max)."""
        spin = _spin()
        try:
            spin.set_enum(self._stream_nodemap, "StreamBufferCountMode", "Manual")
            info = spin.read_number(self._stream_nodemap, "StreamBufferCountManual", True)
            target = min(buffers, int(info.max)) if info.max is not None else buffers
            spin.write_number(self._stream_nodemap, "StreamBufferCountManual", target, True)
        except BackendError as e:
            log.debug("Could not size the stream buffers of camera %s: %s", self._serial, e)

    def _begin_acquisition(self, buffer_mode: str, buffers: int | None = None) -> None:
        spin = _spin()
        try:
            spin.set_enum(self._nodemap, "AcquisitionMode", "Continuous")
        except BackendError as e:
            # Only BeginAcquisition is fatal, as in FlirBackend.
            log.debug("Could not set AcquisitionMode on camera %s: %s", self._serial, e)
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
        while True:
            if buffers and self._stream_nodemap is not None:
                self._set_stream_buffers(buffers)
            try:
                spin.begin_acquisition(self._cam)
                break
            except BackendError as e:
                if (
                    buffers
                    and self._stream_nodemap is not None
                    and buffers > MIN_STREAM_BUFFERS
                ):
                    buffers = fewer_stream_buffers(buffers, self._serial, e)
                    continue
                log.error("Failed to start streaming on camera %s: %s", self._serial, e)
                raise
        self._begin_grab()

    def start_grab_preview(self) -> None:
        self._begin_incomplete_log(record=False)
        self._begin_acquisition("NewestOnly")

    def start_grab_record(self) -> bool:
        self._begin_incomplete_log(record=True)
        self._begin_acquisition("OldestFirst", RECORD_STREAM_BUFFERS)
        return True

    def _begin_incomplete_log(self, *, record: bool) -> None:
        """Start a grab's incomplete-image log afresh (see _count_incomplete)."""
        self._grab_is_record = record
        self._grab_incomplete = 0
        self._grab_incomplete_logged = 0

    def _count_incomplete(self) -> None:
        """As :meth:`FlirBackend._count_incomplete`."""
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

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        fire = self._claim_trigger(timeout_ms)
        if fire is None:
            return None
        cam = self._cam
        if cam is None or not self._grabbing:
            return None
        spin = _spin()
        if fire:
            try:
                spin.execute_command(self._nodemap, "TriggerSoftware")
            except BackendError:
                self._trigger_unfired()
                return None
        return self._fetch_image(
            cam, self._fetch_timeout_ms(timeout_ms), wants_array, answers_trigger=True
        )

    def _fetch_image(
        self, cam, timeout_ms: int, wants_array, answers_trigger: bool = False
    ) -> Frame | None:
        # One image, or None; ``answers_trigger`` as in FlirBackend._fetch_image.
        spin = _spin()
        image = spin.get_next_image(cam, timeout_ms)
        if image is None:
            return None
        if answers_trigger:
            try:
                stamp = None if spin.image_incomplete(image) else int(spin.image_timestamp(image))
            except Exception:
                stamp = None
            self._trigger_answered(stamp)
        try:
            if spin.image_incomplete(image):
                self._count_incomplete()
                return None
            timestamp = spin.image_timestamp(image)
            array = None
            if wants_array():
                # Mono8 only: image_array reads one byte per pixel, and a Mono16
                # frame has the same shape (Mono8 is set at open, best-effort).
                bits = spin.image_bits_per_pixel(image)
                if bits != 8:
                    log.warning(
                        "Camera %s delivered a %d-bpp (non-Mono8) frame; skipping",
                        self._serial,
                        bits,
                    )
                    return None
                array = spin.image_array(image)
            return (array, timestamp)
        except BackendError as e:
            log.warning("Camera %s: bad frame (%s); skipping", self._serial, e)
            return None
        finally:
            spin.image_release(image)  # MUST release every image


def read_model(hcam) -> str | None:
    """See :meth:`_Spinnaker.read_model`."""
    return _spin().read_model(hcam)


def enumerate_spinnaker(
    requested_serials: list[str] | None = None, *, warn_missing: bool = True
):
    """``[(serial, spinCamera)]``: every camera sorted by serial, or the requested
    ones in order. Holds the System until :func:`teardown`; the handles not
    handed out are released here (each spinCameraListGet pairs a release)."""
    spin = _spin()
    global _system, _cam_list
    # Release a previous enumeration's System (doctor enumerates twice): released
    # late, at exit, it aborts the process (see _outstanding).
    if _system is not None or _cam_list is not None:
        teardown()
    _system = spin.get_system()
    _cam_list = spin.create_camera_list()
    spin.system_get_cameras(_system, _cam_list)
    count = spin.camera_list_size(_cam_list)
    if count == 0:
        teardown()
        return []
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
            if warn_missing:
                log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, hcam))
        used.add(serial)
        _outstanding[id(hcam)] = hcam
    for serial, hcam in all_cams:
        if serial not in used:
            try:
                spin.camera_release(hcam)
            except BackendError:
                pass
    return out


def teardown() -> None:
    """Release the System singleton, after every camera was closed, and any
    handle no camera closed (see _outstanding). Idempotent."""
    global _system, _cam_list
    spin = _facade
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


# For paths that enumerate without a CameraSystem (doctor, an aborted run).
atexit.register(teardown)
