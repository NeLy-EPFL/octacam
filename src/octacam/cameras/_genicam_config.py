"""Native GenApi feature-persistence (TSV) config for GenICam camera backends.

Replaces the old hand-rolled ``{"params": ..., "trigger_source": ...}`` JSON with
the camera's *native* export format — the GenApi ``CFeatureBag`` persistence file
that SpinView / Spinnaker write and read: a ``#``-commented, tab-separated
``<FeatureName>\\t<Value>`` list (see the ``# GenApi persistence file`` header).

Why not the SDK's own ``CFeatureBag`` round-trip? On the Grasshopper3
GS3-U3-41C6NIR firmware here, ``StoreToBag`` persists only ~18 "streamable"
nodes (Gain/Exposure/Trigger/... are *not* among them) and the
``DeviceFeaturePersistenceStart/End`` scope-expansion commands SpinView relies on
are absent, so the SDK export cannot carry the settings we care about. Instead we
persist/apply the full writable capture feature set ourselves — the node list in
:data:`CONFIG_NODES`, derived from a live node-map walk — while keeping the exact
same on-disk text format, so the files stay recognisable and portable.

Design:

* :func:`apply_config` reads every ``name\\tvalue`` line and writes it to the
  camera **best-effort, in file order** (so selector features like
  ``LineSelector`` and auto-off-before-dependent-value ordering both work), via
  the small typed-setter seam each backend implements
  (``_set_enum``/``_set_bool``/``_set_number``). Nodes in
  :data:`CONFIG_SKIP_NODES` are never applied — octacam owns them at runtime
  (``DeviceLinkThroughputLimit`` is maximised at ``open()``; ``PixelFormat`` is
  forced to Mono8 for the GRAY8 writer) — and transport/data-flash lock state is
  left to the SDK.
* :func:`dump_config` walks :data:`CONFIG_NODES` and reads each back through the
  matching typed getter, emitting the native TSV so a GUI "Save…" round-trips.

The value *type* per node (enum/bool/int/float) comes from :data:`CONFIG_NODES`
(a live-hardware-derived registry), so the untyped TSV strings are coerced
correctly without every backend needing GenApi type introspection. A node absent
on a given model is simply skipped.
"""

import logging

from octacam.cameras.base import BackendError

log = logging.getLogger("octacam")

# Nodes octacam controls itself; never written from a config file even if the
# file lists them. DeviceLinkThroughputLimit is raised to the device max at
# open() (writing the shipped default would undo that throughput win); PixelFormat
# is forced to Mono8 (the video writer requires GRAY8); TLParamsLocked and the
# DataFlash page registers are transport/flash state the SDK manages.
CONFIG_SKIP_NODES = frozenset({
    "DeviceLinkThroughputLimit",
    "PixelFormat",
    "TLParamsLocked",
    "ActivePageNumber",
    "ActivePageOffset",
    "ActivePageValue",
})

# The writable capture feature set octacam persists, in native-export order, with
# each node's GenApi value type. Derived from a live Grasshopper3 GS3-U3-41C6NIR
# node-map walk at factory defaults; the four params that are read-only at default
# (Gain/ExposureTime/pgrExposureCompensation/AcquisitionFrameRate) are listed right
# after the Auto that unlocks them, so apply order satisfies the dependency. A
# model that lacks a node just skips it. (name, type) pairs grouped by category
# purely for the export's ``# --- category ---`` section comments.
CONFIG_NODES: tuple[tuple[str, str, str], ...] = (
    # AnalogControl
    ("AnalogControl", "GainAuto", "enum"),
    ("AnalogControl", "Gain", "float"),
    ("AnalogControl", "AutoGainLowerLimit", "float"),
    ("AnalogControl", "AutoGainUpperLimit", "float"),
    ("AnalogControl", "BlackLevel", "float"),
    ("AnalogControl", "Gamma", "float"),
    ("AnalogControl", "GammaEnabled", "bool"),
    # DeviceControl
    ("DeviceControl", "DeviceUserID", "string"),
    ("DeviceControl", "AutoFunctionAOIsControl", "enum"),
    ("DeviceControl", "pgrDevicePowerSupplySelector", "enum"),
    ("DeviceControl", "DeviceLinkThroughputLimit", "int"),
    ("DeviceControl", "TestPendingAck", "int"),
    # AcquisitionControl
    ("AcquisitionControl", "TriggerSelector", "enum"),
    ("AcquisitionControl", "TriggerMode", "enum"),
    ("AcquisitionControl", "TriggerSource", "enum"),
    ("AcquisitionControl", "TriggerActivation", "enum"),
    # TriggerDelayEnabled before TriggerDelay: the delay node is read-only until
    # it is enabled (spin_utils.cpp enables first, then sets the value).
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
    # ImageFormatControl
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
    # ImageFormatControl/PixelDefectControl
    ("ImageFormatControl/PixelDefectControl", "pgrDefectPixelCorrectionEnable", "bool"),
    ("ImageFormatControl/PixelDefectControl", "pgrDefectPixelCorrectionTestMode", "enum"),
    ("ImageFormatControl/PixelDefectControl", "pgrCurrentCorrectedPixelCount", "int"),
    ("ImageFormatControl/PixelDefectControl", "pgrCurrentCorrectedPixelIndex", "int"),
    ("ImageFormatControl/PixelDefectControl", "pgrCurrentCorrectedPixelOffsetX", "int"),
    ("ImageFormatControl/PixelDefectControl", "pgrCurrentCorrectedPixelOffsetY", "int"),
    # UserSetControl
    ("UserSetControl", "UserSetSelector", "enum"),
    ("UserSetControl", "UserSetDefault", "enum"),
    ("UserSetControl", "UserSetDefaultSelector", "enum"),
    # DataFlashControl
    ("DataFlashControl", "ActivePageNumber", "int"),
    ("DataFlashControl", "ActivePageOffset", "int"),
    ("DataFlashControl", "ActivePageValue", "int"),
    # DigitalIOControl
    ("DigitalIOControl", "LineSelector", "enum"),
    ("DigitalIOControl", "LineMode", "enum"),
    ("DigitalIOControl", "LineDebouncerTimeRaw", "int"),
    ("DigitalIOControl", "UserOutputSelector", "enum"),
    # LUTControl
    ("LUTControl", "LUTSelector", "enum"),
    ("LUTControl", "LUTEnable", "bool"),
    # TransportLayerControl
    ("TransportLayerControl", "U3VDeviceConfigurationHigh", "int"),
    ("TransportLayerControl", "U3VDeviceConfigurationLow", "int"),
    ("TransportLayerControl", "U3VMessageChannelID", "int"),
    ("TransportLayerControl", "U3VAccessPrivilege", "int"),
    ("TransportLayerControl", "U3VCPConfigurationHigh", "int"),
    ("TransportLayerControl", "U3VCPConfigurationLow", "int"),
    ("TransportLayerControl", "TLParamsLocked", "int"),
    # ChunkDataControl
    ("ChunkDataControl", "ChunkModeActive", "bool"),
    ("ChunkDataControl", "ChunkSelector", "enum"),
    ("ChunkDataControl", "ChunkEnable", "bool"),
    # EventControl
    ("EventControl", "EventSelector", "enum"),
    ("EventControl", "EventNotification", "enum"),
    # RemoveParameterLimits
    ("RemoveParameterLimits", "ParameterSelector", "enum"),
    ("RemoveParameterLimits", "RemoveLimits", "bool"),
    # UserDefinedValues
    ("UserDefinedValues", "UserDefinedValueSelector", "enum"),
    ("UserDefinedValues", "UserDefinedValue", "int"),
)

# name -> GenApi value type ("enum" | "bool" | "int" | "float" | "string").
NODE_TYPE: dict[str, str] = {name: kind for _cat, name, kind in CONFIG_NODES}

_HEADER = (
    "# {octacam GenApi persistence}",
    "# GenApi persistence file (version 3.0.0)",
)


def _to_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _fmt_float(value: float) -> str:
    # 6 significant figures, matching the GenApi persistence text (e.g. 5.07812);
    # integral floats render without a trailing ".0" (2000.0 -> "2000").
    return format(float(value), ".6g")


# The AcquisitionFrameRate control differs across firmware: SFNC spells the gate
# ``AcquisitionFrameRateEnable``, while the Point Grey / Grasshopper3 feature set
# uses ``AcquisitionFrameRateEnabled`` plus an ``AcquisitionFrameRateAuto`` enum.
# Both spellings are written best-effort so one call covers every GenICam vendor.
_FRAMERATE_ENABLE_NODES = ("AcquisitionFrameRateEnable", "AcquisitionFrameRateEnabled")


def apply_freerun_rate_cap(backend, fps: float) -> None:
    """Best-effort: cap a free-running camera's rate at ``fps``.

    Used by ``begin_freerun(fps)`` so a free-run *preview* draws the same bus
    bandwidth as an fps-matched recording (and reports the true target rate)
    instead of the uncapped sensor ceiling. Every write is independent and
    swallowed: a model missing a node (or a node not writable in free-run) simply
    keeps running uncapped. Requires the caller to have already set
    ``TriggerMode=Off`` (the manual-rate nodes are inert/hidden while triggered).
    """
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
    """Best-effort: disable the manual frame-rate cap set by :func:`apply_freerun_rate_cap`.

    Called when arming a triggered mode (software or hardware) so a cap left over
    from a free-run preview cannot clip a subsequent externally-triggered
    recording (notably on Basler, where the enable node applies even while
    triggered). No-op on a model that never had the node.
    """
    for name in _FRAMERATE_ENABLE_NODES:
        try:
            backend._set_bool(name, False)
        except BackendError:
            pass


def parse_config(text: str) -> list[tuple[str, str]]:
    """Parse a native persistence TSV into ordered ``(name, value)`` pairs.

    ``#`` comment lines (the header and the ``# --- category ---`` markers) and
    blank lines are skipped, as GenApi's own reader does.
    """
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "\t" not in line:
            continue
        name, value = line.split("\t", 1)
        out.append((name.strip(), value.strip()))
    return out


def apply_config(backend, text: str) -> None:
    """Apply every ``name\\tvalue`` line to *backend*, best-effort, in file order.

    Nodes in :data:`CONFIG_SKIP_NODES` are left to octacam's runtime management.
    Unknown/absent nodes and failed writes are logged at debug and skipped, so a
    partial or cross-model file still applies what it can (mirrors how the FLIR C
    config tool guards each node with an availability check).
    """
    serial = getattr(backend, "serial_number", "?")
    pairs = parse_config(text)
    if not pairs and any(
        ln.strip() and not ln.strip().startswith("#") for ln in text.splitlines()
    ):
        # Non-empty content but not a single `name<TAB>value` line: this is a
        # malformed config (stray text, or leftover JSON), not a legitimately
        # empty one — reject it so callers like the web reset endpoint still fail
        # loudly instead of silently applying nothing.
        raise BackendError("no GenApi feature lines found in configuration")
    for name, value in pairs:
        if name in CONFIG_SKIP_NODES:
            continue
        kind = NODE_TYPE.get(name)
        try:
            if kind == "string":
                continue  # not a capture parameter (e.g. DeviceUserID)
            elif kind == "bool":
                backend._set_bool(name, _to_bool(value))
            elif kind == "int":
                backend._set_number(name, int(float(value)), True)
            elif kind == "float":
                backend._set_number(name, float(value), False)
            else:
                # enum, or a node not in the registry: set as a symbolic enum.
                backend._set_enum(name, value)
        except BackendError as e:
            log.debug("Skipping %s on camera %s: %s", name, serial, e)
        except (ValueError, TypeError) as e:
            log.debug("Bad value for %s on camera %s: %r (%s)", name, serial, value, e)


def dump_config(backend, model: str | None = None) -> str:
    """Serialise the current camera state to native persistence TSV.

    Walks :data:`CONFIG_NODES` and reads each back through the backend's typed
    getters; nodes that are absent/unreadable are omitted. Grouped by category
    with ``# --- category ---`` comment markers for readability.
    """
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
    """Undo a live software-trigger preview's ``TriggerSource=Software`` override.

    ``begin_software_trigger_preview`` forces ``TriggerSource=Software`` on the
    live camera; a config saved (GUI "Save") while previewing would bake that in,
    so a later external-trigger recording would wait for a software trigger that
    never fires — the cameras just never start, silently (this is exactly how the
    triggerbox FLIR ``.txt`` files ended up unrecordable). Rewrite a dumped
    ``TriggerSource\\tSoftware`` line back to *original_source* — the hardware line
    :meth:`load_params` captured when the config was loaded. Mirrors
    :func:`octacam.cameras.basler._normalize_pfs_triggers`.

    No-op when there is nothing to undo (the value is not ``Software``) or no
    hardware source to restore to (``original_source`` is unknown or itself
    ``Software``), so a genuinely software-triggered rig is left untouched.
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
