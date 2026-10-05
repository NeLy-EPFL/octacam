"""The GenICam layer the SFNC backends share.

:class:`GenApi` is one SDK's node access, under the one node-map walker and the
typed accessors. :class:`GenICamBackend` reads and writes SFNC nodes by name
through its typed seam (``get_node``/``set_node``), on which the trigger chain
and the GenApi persistence TSV are built; :class:`NodeMapBackend` implements
that seam and the Camera-tab browser over a :class:`GenApi`.

The TSV is SpinView's ``#``-commented ``<Feature>\\t<Value>`` format, written by
octacam from :data:`CONFIG_NODES` because the GS3's own ``StoreToBag`` keeps only
~18 streamable nodes (not Gain, Exposure or the trigger chain). ``set_node`` must
raise :class:`BackendError`, never a raw SDK exception: :func:`apply_config`'s
best-effort skip rests on it, and a leak turns one refused value into a dead rig.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from octacam.cameras.base import BackendError, CameraBackend, FeatureInfo, coerce_bool

log = logging.getLogger("octacam")

# An int or float node's (min, max, inc, unit), each None where absent.
Bounds = tuple[Any, Any, Any, "str | None"]


def _snap_int(value: object, node_min: Any, node_inc: Any) -> int:
    """``value`` rounded onto the node's increment grid from its min (the
    firmware rejects an off-grid write)."""
    snapped = int(round(float(value)))  # type: ignore[arg-type]
    if node_inc:
        base = node_min if node_min is not None else 0
        snapped = int(base + round((snapped - base) / node_inc) * node_inc)
    return snapped


class GenApi(ABC):
    """One SDK's GenApi node access.

    Node and node-map handles are opaque. The queries return None (or False)
    when the SDK fails; ``write`` and ``execute`` raise :class:`BackendError`.
    ``kind`` is a :class:`FeatureInfo` type, ``"category"`` included, or None
    for a node the browser skips.
    """

    @abstractmethod
    def node(self, nodemap: Any, name: str) -> Any:
        """The node called ``name``, or None."""

    @abstractmethod
    def children(self, category: Any) -> list[Any]:
        """The category's available feature nodes."""

    @abstractmethod
    def kind(self, node: Any) -> str | None: ...
    @abstractmethod
    def name(self, node: Any) -> str | None: ...
    @abstractmethod
    def display_name(self, node: Any) -> str | None: ...
    @abstractmethod
    def tooltip(self, node: Any) -> str | None: ...

    @abstractmethod
    def visibility(self, node: Any) -> str:
        """beginner, expert, guru or invisible."""

    @abstractmethod
    def readable(self, node: Any) -> bool: ...
    @abstractmethod
    def writable(self, node: Any) -> bool: ...

    @abstractmethod
    def value(self, node: Any, kind: str) -> Any:
        """The value read as ``kind`` (an enum's current symbolic)."""

    @abstractmethod
    def bounds(self, node: Any, kind: str) -> Bounds: ...

    @abstractmethod
    def entries(self, node: Any) -> list[tuple[str, bool]]:
        """An enum's (symbolic, available) entries."""

    @abstractmethod
    def write(self, node: Any, kind: str, value: Any) -> None:
        """Write ``value`` as ``kind`` (an enum by its symbolic)."""

    @abstractmethod
    def execute(self, node: Any) -> None: ...

    # ------------------------------------------------------ by name, typed
    def get(self, nodemap: Any, name: str, kind: str) -> Any:
        """``name``'s value as ``kind``; None if absent or unreadable."""
        node = self.node(nodemap, name)
        if node is None or not self.readable(node):
            return None
        return self.value(node, kind)

    def put(self, nodemap: Any, name: str, kind: str, value: Any) -> None:
        node = self.node(nodemap, name)
        if node is None or not self.writable(node):
            raise BackendError(f"node {name} is not writable")
        self.write(node, kind, value)

    # ------------------------------------------------- the Camera-tab browser
    def feature(self, node: Any, kind: str) -> FeatureInfo:
        name = self.name(node) or ""
        readable = self.readable(node)
        feature = FeatureInfo(
            name=name,
            display_name=self.display_name(node) or name,
            type=kind,
            readable=readable,
            writable=self.writable(node),
            visibility=self.visibility(node),
            tooltip=self.tooltip(node),
        )
        if readable and kind != "command":
            feature.value = self.value(node, kind)
        if kind in ("int", "float"):
            feature.min, feature.max, feature.inc, feature.unit = self.bounds(node, kind)
        elif kind == "enum":
            feature.entries = [
                {"value": s, "display": s, "available": a} for s, a in self.entries(node)
            ] or None
        return feature

    def walk(self, nodemap: Any) -> list[FeatureInfo]:
        """Every visible feature under Root, depth first, each once, labeled by
        its category's display name ("Other" directly under Root)."""
        out: list[FeatureInfo] = []
        root = self.node(nodemap, "Root")
        if root is not None:
            self._walk(root, "", out, set())
        return out

    def _walk(self, category: Any, label: str, out: list[FeatureInfo], seen: set) -> None:
        for node in self.children(category):
            try:  # one bad node must not abort the walk
                if self.visibility(node) == "invisible":
                    continue
                kind = self.kind(node)
                if kind == "category":
                    sub = self.display_name(node) or self.name(node) or label
                    self._walk(node, sub, out, seen)
                    continue
                name = self.name(node)
                if kind is None or not name or name in seen:
                    continue
                seen.add(name)
                feature = self.feature(node, kind)
                feature.category = label or "Other"
                out.append(feature)
            except Exception as e:
                log.debug("Skipping node during feature walk: %s", e)

    def _feature_node(self, nodemap: Any, name: str) -> tuple[Any, str | None]:
        node = self.node(nodemap, name)
        if node is None:
            raise BackendError(f"no such node: {name}")
        return node, self.kind(node)

    def read_feature(self, nodemap: Any, name: str) -> FeatureInfo:
        node, kind = self._feature_node(nodemap, name)
        if kind is None or kind == "category":
            raise BackendError(f"node {name} is not an editable feature")
        return self.feature(node, kind)

    def write_feature(self, nodemap: Any, name: str, value: object) -> None:
        """Write ``value`` coerced to the node's kind; an int snaps to its grid."""
        node, kind = self._feature_node(nodemap, name)
        if kind not in ("int", "float", "bool", "enum", "string"):
            raise BackendError(f"node {name} is not writable ({kind})")
        if not self.writable(node):
            raise BackendError(f"node {name} is not writable")
        if kind == "int":
            node_min, _max, node_inc, _unit = self.bounds(node, kind)
            typed: Any = _snap_int(value, node_min, node_inc)
        elif kind == "float":
            typed = float(value)  # type: ignore[arg-type]
        elif kind == "bool":
            typed = coerce_bool(value)
        else:
            typed = str(value)
        self.write(node, kind, typed)

    def run_command(self, nodemap: Any, name: str) -> None:
        node, kind = self._feature_node(nodemap, name)
        if kind != "command":
            raise BackendError(f"node {name} is not a command")
        self.execute(node)


# SFNC spells the rate gate AcquisitionFrameRateEnable, the GS3 ...Enabled (plus an
# Auto enum); both are written.
_FRAME_RATE_ENABLES = ("AcquisitionFrameRateEnable", "AcquisitionFrameRateEnabled")


class GenICamBackend(CameraBackend):
    """A backend over SFNC nodes by name: the shared trigger chain and the TSV
    persistence, written through ``get_node``/``set_node``."""

    extension = "txt"

    def __init__(self, serial: str):
        super().__init__(serial)
        # TriggerSource as load_params left it: what a save restores.
        self._original_trigger_source: str | None = None

    @abstractmethod
    def get_node(self, name: str, kind: str) -> Any:
        """``name``'s value as ``kind``; None if absent or unreadable."""

    @abstractmethod
    def set_node(self, name: str, kind: str, value: Any) -> None:
        """Write ``value`` as ``kind``; BackendError if refused."""

    def _try_set(self, name: str, kind: str, value: Any) -> None:
        try:
            self.set_node(name, kind, value)
        except BackendError as e:
            log.debug("Could not set %s = %s on camera %s: %s", name, value, self._serial, e)

    def _size(self, name: str) -> int:
        value = self.get_node(name, "int")
        if value is None:
            raise BackendError(f"{name} is not readable on camera {self._serial}")
        return int(value)

    def width(self) -> int:
        return self._size("Width")

    def height(self) -> int:
        return self._size("Height")

    # ----------------------------------------------------------- triggering
    def _arm_frame_trigger(self, source: str | None = None) -> None:
        self._cap_frame_rate(None)
        self.set_node("TriggerSelector", "enum", "FrameStart")
        self.set_node("TriggerMode", "enum", "On")
        if source is not None:
            self.set_node("TriggerSource", "enum", source)
        # With the FLIR default Off, a trigger fired during the previous frame's
        # readout is silently ignored (GS3 at 4 ms exposure: ~64 -> ~4.7 fps).
        self._try_set("TriggerOverlap", "enum", "ReadOut")

    def _cap_frame_rate(self, fps: float | None) -> None:
        """Best-effort free-run rate cap at ``fps``, so a free-run preview draws
        an fps-matched recording's bandwidth; None removes it, as a triggered
        mode needs (on Basler the cap applies even while triggered)."""
        for name in _FRAME_RATE_ENABLES:
            self._try_set(name, "bool", fps is not None)
        if fps is not None:
            self._try_set("AcquisitionFrameRateAuto", "enum", "Off")
            self._try_set("AcquisitionFrameRate", "float", float(fps))

    def enable_frame_trigger(self) -> None:
        if self.is_open():
            self._arm_frame_trigger()

    def set_trigger_source(self, use_software: bool) -> None:
        if not self.is_open():
            return
        try:
            if use_software:
                self.set_node("TriggerSource", "enum", "Software")
            elif self._original_trigger_source is not None:
                self.set_node("TriggerSource", "enum", self._original_trigger_source)
        except BackendError as e:
            log.warning("Failed to set trigger source on camera %s: %s", self._serial, e)

    def begin_software_trigger_preview(self) -> None:
        self._arm_frame_trigger("Software")

    def begin_freerun(self, fps: float | None = None) -> bool:
        """TriggerMode Off, capped at ``fps`` when given; False if refused.
        Arming a triggered mode later clears the cap."""
        if not self.is_open():
            return False
        try:
            self.set_node("TriggerMode", "enum", "Off")
        except BackendError as e:
            log.debug("free-run unsupported on camera %s: %s", self._serial, e)
            return False
        self._try_set("AcquisitionMode", "enum", "Continuous")
        if fps is not None:
            self._cap_frame_rate(fps)
        return True

    # ----------------------------------------------------- config persistence
    def config_values(self, config_str: str) -> dict[str, str]:
        return dict(parse_config(config_str))

    def load_params(self, config_str: str) -> None:
        if config_str:
            apply_config(self, config_str)
        self._original_trigger_source = self.get_node("TriggerSource", "enum")

    def save_params(self) -> str:
        model = self.get_node("DeviceModelName", "string")
        return normalize_trigger_source(
            dump_config(self, model), self._original_trigger_source
        )


class NodeMapBackend(GenICamBackend):
    """A GenICamBackend over a walked node map: ``_api`` is the SDK's
    :class:`GenApi`, ``_nodemap`` the device node map while open, else None."""

    def __init__(self, serial: str, api: GenApi):
        super().__init__(serial)
        self._api = api
        self._nodemap: Any = None

    def _open_nodemap(self) -> Any:
        if self._nodemap is None:
            raise BackendError(f"camera {self._serial} is not open")
        return self._nodemap

    def get_node(self, name: str, kind: str) -> Any:
        if self._nodemap is None:
            return None
        return self._api.get(self._nodemap, name, kind)

    def set_node(self, name: str, kind: str, value: Any) -> None:
        self._api.put(self._open_nodemap(), name, kind, value)

    def list_features(self) -> list[FeatureInfo]:
        return [] if self._nodemap is None else self._api.walk(self._nodemap)

    def read_feature(self, name: str) -> FeatureInfo:
        return self._api.read_feature(self._open_nodemap(), name)

    def write_feature(self, name: str, value: object) -> None:
        self._api.write_feature(self._open_nodemap(), name, value)

    def execute_command(self, name: str) -> None:
        self._api.run_command(self._open_nodemap(), name)


# --- the GenApi persistence TSV ---------------------------------------------

# Nodes octacam owns, never applied from a file: the link throughput (maxed at
# open; the file's value would undo it), PixelFormat (Mono8 for the GRAY8 writer),
# and transport and data-flash state the SDK manages.
CONFIG_SKIP_NODES = frozenset({
    "DeviceLinkThroughputLimit",
    "PixelFormat",
    "TLParamsLocked",
    "ActivePageNumber",
    "ActivePageOffset",
    "ActivePageValue",
})

# The capture features octacam persists, with their GenApi types, in apply order:
# from a GS3-U3-41C6NIR node-map walk at factory defaults, each value after the
# Auto or Enable node that unlocks it. A model lacking a node skips it; the
# category only labels the export's sections.
CONFIG_NODES: tuple[tuple[str, str, str], ...] = (
    ("AnalogControl", "GainAuto", "enum"),
    ("AnalogControl", "Gain", "float"),
    ("AnalogControl", "AutoGainLowerLimit", "float"),
    ("AnalogControl", "AutoGainUpperLimit", "float"),
    ("AnalogControl", "BlackLevel", "float"),
    ("AnalogControl", "Gamma", "float"),
    ("AnalogControl", "GammaEnabled", "bool"),
    ("DeviceControl", "DeviceUserID", "string"),
    ("DeviceControl", "AutoFunctionAOIsControl", "enum"),
    ("DeviceControl", "pgrDevicePowerSupplySelector", "enum"),
    ("DeviceControl", "DeviceLinkThroughputLimit", "int"),
    ("DeviceControl", "TestPendingAck", "int"),
    ("AcquisitionControl", "TriggerSelector", "enum"),
    ("AcquisitionControl", "TriggerMode", "enum"),
    ("AcquisitionControl", "TriggerSource", "enum"),
    ("AcquisitionControl", "TriggerActivation", "enum"),
    # The delay is read-only until enabled.
    ("AcquisitionControl", "TriggerDelayEnabled", "bool"),
    ("AcquisitionControl", "TriggerDelay", "float"),
    ("AcquisitionControl", "ExposureMode", "enum"),
    ("AcquisitionControl", "ExposureAuto", "enum"),
    ("AcquisitionControl", "ExposureTime", "float"),
    ("AcquisitionControl", "AutoExposureTimeLowerLimit", "float"),
    ("AcquisitionControl", "AutoExposureTimeUpperLimit", "float"),
    ("AcquisitionControl", "pgrExposureCompensationAuto", "enum"),
    ("AcquisitionControl", "pgrExposureCompensation", "float"),
    ("AcquisitionControl", "pgrAutoExposureCompensationLowerLimit", "float"),
    ("AcquisitionControl", "pgrAutoExposureCompensationUpperLimit", "float"),
    ("AcquisitionControl", "AcquisitionMode", "enum"),
    ("AcquisitionControl", "AcquisitionFrameRateAuto", "enum"),
    ("AcquisitionControl", "AcquisitionFrameRateEnabled", "bool"),
    ("AcquisitionControl", "AcquisitionFrameRate", "float"),
    ("AcquisitionControl", "AcquisitionStatusSelector", "enum"),
    ("AcquisitionControl", "SingleFrameAcquisitionMode", "enum"),
    ("AcquisitionControl", "pgrHDRModeEnabled", "bool"),
    ("ImageFormatControl", "PixelFormat", "enum"),
    ("ImageFormatControl", "OnBoardColorProcessEnabled", "bool"),
    ("ImageFormatControl", "Width", "int"),
    ("ImageFormatControl", "Height", "int"),
    ("ImageFormatControl", "OffsetX", "int"),
    ("ImageFormatControl", "OffsetY", "int"),
    ("ImageFormatControl", "VideoMode", "enum"),
    ("ImageFormatControl", "BinningVertical", "int"),
    ("ImageFormatControl", "ReverseX", "bool"),
    ("ImageFormatControl", "TestImageSelector", "enum"),
    ("ImageFormatControl", "TestPattern", "enum"),
    ("ImageFormatControl/PixelDefectControl", "pgrDefectPixelCorrectionEnable", "bool"),
    ("ImageFormatControl/PixelDefectControl", "pgrDefectPixelCorrectionTestMode", "enum"),
    ("ImageFormatControl/PixelDefectControl", "pgrCurrentCorrectedPixelCount", "int"),
    ("ImageFormatControl/PixelDefectControl", "pgrCurrentCorrectedPixelIndex", "int"),
    ("ImageFormatControl/PixelDefectControl", "pgrCurrentCorrectedPixelOffsetX", "int"),
    ("ImageFormatControl/PixelDefectControl", "pgrCurrentCorrectedPixelOffsetY", "int"),
    ("UserSetControl", "UserSetSelector", "enum"),
    ("UserSetControl", "UserSetDefault", "enum"),
    ("UserSetControl", "UserSetDefaultSelector", "enum"),
    ("DataFlashControl", "ActivePageNumber", "int"),
    ("DataFlashControl", "ActivePageOffset", "int"),
    ("DataFlashControl", "ActivePageValue", "int"),
    ("DigitalIOControl", "LineSelector", "enum"),
    ("DigitalIOControl", "LineMode", "enum"),
    ("DigitalIOControl", "LineDebouncerTimeRaw", "int"),
    ("DigitalIOControl", "UserOutputSelector", "enum"),
    ("LUTControl", "LUTSelector", "enum"),
    ("LUTControl", "LUTEnable", "bool"),
    ("TransportLayerControl", "U3VDeviceConfigurationHigh", "int"),
    ("TransportLayerControl", "U3VDeviceConfigurationLow", "int"),
    ("TransportLayerControl", "U3VMessageChannelID", "int"),
    ("TransportLayerControl", "U3VAccessPrivilege", "int"),
    ("TransportLayerControl", "U3VCPConfigurationHigh", "int"),
    ("TransportLayerControl", "U3VCPConfigurationLow", "int"),
    ("TransportLayerControl", "TLParamsLocked", "int"),
    ("ChunkDataControl", "ChunkModeActive", "bool"),
    ("ChunkDataControl", "ChunkSelector", "enum"),
    ("ChunkDataControl", "ChunkEnable", "bool"),
    ("EventControl", "EventSelector", "enum"),
    ("EventControl", "EventNotification", "enum"),
    ("RemoveParameterLimits", "ParameterSelector", "enum"),
    ("RemoveParameterLimits", "RemoveLimits", "bool"),
    ("UserDefinedValues", "UserDefinedValueSelector", "enum"),
    ("UserDefinedValues", "UserDefinedValue", "int"),
)

# name -> GenApi value type ("enum" | "bool" | "int" | "float" | "string").
NODE_TYPE: dict[str, str] = {name: kind for _cat, name, kind in CONFIG_NODES}

_HEADER = (
    "# {octacam GenApi persistence}",
    "# GenApi persistence file (version 3.0.0)",
)

# (size node, the ROI origin that clamps it: a size's max is sensor - origin).
_ROI_PAIRS = (("Width", "OffsetX"), ("Height", "OffsetY"))
_ROI_OFFSET_NODES = frozenset(offset for _size, offset in _ROI_PAIRS)

# A refused geometry write keeps the previous ROI, so the recording comes out the
# wrong size: a warning, not the debug line of an absent node.
_LOUD_NODES = frozenset({"Width", "Height", "OffsetX", "OffsetY"})


def _roi_offsets_last(
    pairs: "list[tuple[str, str]]",
) -> "list[tuple[str, str]]":
    """Move the ROI origins after every size node; the rest keeps file order.

    :func:`dump_config` lists sizes first, but a vendor-exported or hand-edited
    file may not, and an origin written first clamps the size after it.
    """
    head = [(n, v) for n, v in pairs if n not in _ROI_OFFSET_NODES]
    tail = [(n, v) for n, v in pairs if n in _ROI_OFFSET_NODES]
    return head + tail


def _clear_roi_offsets(backend: GenICamBackend, names: set[str]) -> None:
    """Zero each ROI origin whose size node the file sets, before applying it.

    A camera keeps its ROI until power-cycled, so the previous session's origin
    clamps this file's size: a rig cropped to OffsetY=278 put the next rig's
    Height=2048 out of range (max 1770). The file's own origin is applied last; a
    size without one lands at origin 0. Best-effort.
    """
    for size, offset in _ROI_PAIRS:
        if size in names:
            backend._try_set(offset, "int", 0)


def parse_config(text: str) -> list[tuple[str, str]]:
    """The TSV's ``(name, value)`` pairs in order, without comments and blanks."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "\t" not in line:
            continue
        name, value = line.split("\t", 1)
        out.append((name.strip(), value.strip()))
    return out


def _parse_value(kind: str, text: str) -> Any:
    if kind == "bool":
        return coerce_bool(text)
    if kind == "int":
        return int(float(text))
    if kind == "float":
        return float(text)
    return text


def apply_config(backend: GenICamBackend, text: str) -> None:
    """Apply each ``name\\tvalue`` line, best-effort, in file order (selectors
    and Auto-before-value rely on it), except the ROI origins: zeroed first and
    applied last (see :func:`_clear_roi_offsets`). A refused or absent node is
    logged and skipped; :data:`CONFIG_SKIP_NODES` are never applied, nor string
    nodes (not capture parameters, e.g. DeviceUserID). A node not in
    :data:`NODE_TYPE` is set as a symbolic enum.
    """
    serial = backend.serial_number
    pairs = parse_config(text)
    if not pairs and any(
        ln.strip() and not ln.strip().startswith("#") for ln in text.splitlines()
    ):
        # Content without one feature line is malformed (stray text, old JSON):
        # fail loudly rather than apply nothing.
        raise BackendError("no GenApi feature lines found in configuration")
    _clear_roi_offsets(backend, {name for name, _ in pairs})
    for name, value in _roi_offsets_last(pairs):
        kind = NODE_TYPE.get(name, "enum")
        if name in CONFIG_SKIP_NODES or kind == "string":
            continue
        try:
            backend.set_node(name, kind, _parse_value(kind, value))
        except BackendError as e:
            if name in _LOUD_NODES:
                log.warning(
                    "Camera %s rejected %s = %s (%s) — it keeps its current "
                    "geometry, so this recording may not be the configured size",
                    serial,
                    name,
                    value,
                    e,
                )
            else:
                log.debug("Skipping %s on camera %s: %s", name, serial, e)
        except (ValueError, TypeError) as e:
            log.debug("Bad value for %s on camera %s: %r (%s)", name, serial, value, e)


def _render(kind: str, value: Any) -> str:
    if kind == "bool":
        return "true" if value else "false"
    if kind == "int":
        return str(int(value))
    if kind == "float":
        # 6 significant figures, as GenApi writes them (5.07812; 2000.0 -> "2000").
        return format(float(value), ".6g")
    return str(value)


def dump_config(backend: GenICamBackend, model: str | None = None) -> str:
    """The camera's :data:`CONFIG_NODES` as TSV; unreadable nodes are omitted."""
    serial = backend.serial_number
    device = " ".join(filter(None, (model, f"({serial})" if serial else "")))
    lines: list[str] = list(_HEADER)
    if device:
        lines.append(f"# Device = {device}")
    last_cat: str | None = None
    for category, name, kind in CONFIG_NODES:
        value = None if kind == "string" else backend.get_node(name, kind)
        if value is None:
            continue
        if category != last_cat:
            lines.append(f"# --- {category} ---")
            last_cat = category
        lines.append(f"{name}\t{_render(kind, value)}")
    return "\n".join(lines) + "\n"


def normalize_trigger_source(text: str, original_source: str | None) -> str:
    """Rewrite a dumped ``TriggerSource\\tSoftware`` to ``original_source``.

    A software-trigger preview sets the camera's TriggerSource to Software; saved
    into the file, a later hardware-triggered recording would wait forever for a
    software trigger. A no-op without a hardware source to restore. Basler's
    counterpart is :func:`octacam.cameras.basler._normalize_pfs_triggers`.
    """
    if not original_source or original_source == "Software":
        return text
    out = []
    for line in text.splitlines():
        if not line.startswith("#") and "\t" in line:
            name, _, value = line.partition("\t")
            if name.strip() == "TriggerSource" and value.strip() == "Software":
                line = f"{name}\t{original_source}"
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")
