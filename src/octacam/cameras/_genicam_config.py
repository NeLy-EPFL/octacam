"""The GenApi persistence TSV and the trigger chain shared by the GenICam backends.

The file is SpinView's ``#``-commented ``<Feature>\\t<Value>`` format, written
by octacam from :data:`CONFIG_NODES` because the GS3's own ``StoreToBag`` keeps
only ~18 streamable nodes (not Gain, Exposure or the trigger chain).
:func:`apply_config` writes each line best-effort through the backend's typed
setters (``_set_enum``/``_set_bool``/``_set_number``), which must raise
:class:`BackendError`, never a raw SDK exception: a leak turns one refused value
into a dead rig.
"""

import logging
from typing import TYPE_CHECKING

from octacam.cameras.base import BackendError, coerce_bool

log = logging.getLogger("octacam")

# FLIR record-grab stream buffers (capped by StreamBufferCountMax): ~1 s at 125 fps
# instead of the SDK's 9, so a grab thread stalled by a GC pause or a disk hiccup
# delays frames rather than losing them. They come out of the kernel's shared USB
# memory (usbcore.usbfs_memory_mb: two full-sensor GS3s need ~1.1 GB at 128), so
# a pool that cannot start is halved, down to MIN_STREAM_BUFFERS.
RECORD_STREAM_BUFFERS = 128
MIN_STREAM_BUFFERS = 16


def fewer_stream_buffers(buffers: int, serial: str, error: object) -> int:
    """Half of a stream buffer pool the camera could not start acquisition with."""
    log.warning(
        "Camera %s could not start acquisition with %d stream buffers (%s); "
        "retrying with %d. The pool shares the kernel's USB memory with the other "
        "cameras: raise usbcore.usbfs_memory_mb to keep the full pool, which "
        "absorbs longer host stalls.",
        serial,
        buffers,
        error,
        buffers // 2,
    )
    return buffers // 2


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


def _fmt_float(value: float) -> str:
    # 6 significant figures, as GenApi writes them (5.07812; 2000.0 -> "2000").
    return format(float(value), ".6g")


# SFNC spells the rate gate AcquisitionFrameRateEnable, the GS3 ...Enabled (plus an
# Auto enum); both are written.
_FRAMERATE_ENABLE_NODES = ("AcquisitionFrameRateEnable", "AcquisitionFrameRateEnabled")

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


def _clear_roi_offsets(backend, names: set[str], serial: str) -> None:
    """Zero each ROI origin whose size node the file sets, before applying it.

    A camera keeps its ROI until power-cycled, so the previous session's origin
    clamps this file's size: a rig cropped to OffsetY=278 put the next rig's
    Height=2048 out of range (max 1770). The file's own origin is applied last; a
    size without one lands at origin 0. Best-effort.
    """
    for size, offset in _ROI_PAIRS:
        if size not in names:
            continue
        try:
            backend._set_number(offset, 0, True)
        except BackendError as e:
            log.debug("Could not zero %s on camera %s: %s", offset, serial, e)


def apply_freerun_rate_cap(backend, fps: float) -> None:
    """Best-effort cap of a free-running camera at ``fps`` (TriggerMode must be
    Off), so a free-run preview draws an fps-matched recording's bandwidth."""
    for name in _FRAMERATE_ENABLE_NODES:
        try:
            backend._set_bool(name, True)
        except BackendError:
            pass
    try:
        backend._set_enum("AcquisitionFrameRateAuto", "Off")
    except BackendError:
        pass
    try:
        backend._set_number("AcquisitionFrameRate", float(fps), False)
    except BackendError as e:
        log.debug("Could not cap free-run rate at %s fps: %s", fps, e)


def clear_freerun_rate_cap(backend) -> None:
    """Best-effort removal of the free-run cap before a triggered mode, which it
    would clip (on Basler the enable applies even while triggered)."""
    for name in _FRAMERATE_ENABLE_NODES:
        try:
            backend._set_bool(name, False)
        except BackendError:
            pass


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


def apply_config(backend, text: str) -> None:
    """Apply each ``name\\tvalue`` line, best-effort, in file order (selectors
    and Auto-before-value rely on it), except the ROI origins: zeroed first and
    applied last (see :func:`_clear_roi_offsets`). A refused or absent node is
    logged and skipped; :data:`CONFIG_SKIP_NODES` are never applied.
    """
    serial = getattr(backend, "serial_number", "?")
    pairs = parse_config(text)
    if not pairs and any(
        ln.strip() and not ln.strip().startswith("#") for ln in text.splitlines()
    ):
        # Content without one feature line is malformed (stray text, old JSON):
        # fail loudly rather than apply nothing.
        raise BackendError("no GenApi feature lines found in configuration")
    _clear_roi_offsets(backend, {name for name, _ in pairs}, serial)
    for name, value in _roi_offsets_last(pairs):
        if name in CONFIG_SKIP_NODES:
            continue
        kind = NODE_TYPE.get(name)
        try:
            if kind == "string":
                continue  # not a capture parameter (e.g. DeviceUserID)
            elif kind == "bool":
                backend._set_bool(name, coerce_bool(value))
            elif kind == "int":
                backend._set_number(name, int(float(value)), True)
            elif kind == "float":
                backend._set_number(name, float(value), False)
            else:
                # enum, or a node not in the registry: set as a symbolic enum.
                backend._set_enum(name, value)
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


def dump_config(backend, model: str | None = None) -> str:
    """The camera's :data:`CONFIG_NODES` as TSV; unreadable nodes are omitted."""
    serial = getattr(backend, "serial_number", None)
    device = " ".join(filter(None, (model, f"({serial})" if serial else "")))
    lines: list[str] = list(_HEADER)
    if device:
        lines.append(f"# Device = {device}")
    last_cat: str | None = None
    for category, name, kind in CONFIG_NODES:
        try:
            if kind == "string":
                continue
            elif kind == "bool":
                got = backend._get_bool(name)
                rendered = None if got is None else ("true" if got else "false")
            elif kind in ("int", "float"):
                got = backend._get_number(name, kind == "int")
                if got is None:
                    rendered = None
                elif kind == "int":
                    rendered = str(int(got))
                else:
                    rendered = _fmt_float(got)
            else:
                rendered = backend._get_enum(name)
        except BackendError:
            rendered = None
        if rendered is None:
            continue
        if category != last_cat:
            lines.append(f"# --- {category} ---")
            last_cat = category
        lines.append(f"{name}\t{rendered}")
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


class GenICamTriggerConfig:
    """The SFNC trigger chain and TSV persistence of the flir, spinnaker and
    pycameleon backends, mixed in beside
    :class:`~octacam.cameras._trigger_handoff.SoftwareTriggerHandoff`. The backend
    supplies ``_serial``, ``is_open`` and the typed getters and setters.
    """

    # For the type checker only: nothing is created at runtime, so the MRO finds
    # the backend's and the hand-off's implementations.
    _serial: str
    _original_trigger_source: str | None

    if TYPE_CHECKING:
        # Positional-only: pycameleon names the parameter ``node``.
        def _set_enum(self, name: str, value: str, /) -> None: ...
        def _get_enum(self, name: str, /) -> str | None: ...
        def is_open(self) -> bool: ...
        def _bump_trigger(self) -> None: ...

    # ----------------------------------------------------------- triggering

    def _enable_trigger_overlap(self) -> None:
        """Best-effort TriggerOverlap=ReadOut. With the FLIR default Off, a trigger
        fired during the previous frame's readout is silently ignored, halving the
        rate with a stall per miss (GS3 at 4 ms exposure: ~4.7 -> ~64 fps)."""
        try:
            self._set_enum("TriggerOverlap", "ReadOut")
        except BackendError as e:
            log.debug("Could not set TriggerOverlap on camera %s: %s", self._serial, e)

    def enable_frame_trigger(self) -> None:
        if not self.is_open():
            return
        clear_freerun_rate_cap(self)
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
        clear_freerun_rate_cap(self)
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")
        self._set_enum("TriggerSource", "Software")
        self._enable_trigger_overlap()

    def trigger_once(self) -> None:
        self._bump_trigger()  # no device call: retrieve() fires it (_trigger_handoff)

    def begin_freerun(self, fps: float | None = None) -> bool:
        """TriggerMode Off, capped at ``fps`` when given; False if refused. Arming
        a triggered mode later clears the cap."""
        if not self.is_open():
            return False
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

    # ----------------------------------------------------- config persistence

    def config_values(self, config_str: str) -> dict[str, str]:
        return dict(parse_config(config_str))

    def load_params(self, config_str: str) -> None:
        if config_str:
            apply_config(self, config_str)
        self._original_trigger_source = self._get_enum("TriggerSource")

    def save_params(self) -> str:
        return normalize_trigger_source(
            dump_config(self), self._original_trigger_source
        )
