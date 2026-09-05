"""octacam_config.toml parsing.

The loader is deliberately *tolerant*: a malformed file, section, or field is
warned about and falls back to the default rather than raising, so a rig's
config can never stop the app from starting. pydantic validates the types;
``_lenient_validate`` turns validation errors into warn-and-default.
"""

import datetime
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from octacam._compat import tomllib
from octacam.writer import (
    DEFAULT_FFMPEG_PARAMS,
    DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    NVENC_H264_PARAMS,
)

log = logging.getLogger("octacam")

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _scalar_str(value: object) -> str:
    """Coerce a TOML scalar to a string, rejecting bool/array/table.

    TOML strings are normally quoted, but an unquoted integer serial number or
    a date-like save directory parses as an int/date; accept those as text
    (mirroring how the values were always meant to be read)."""
    if isinstance(value, bool):
        raise ValueError("expected a string, got a boolean")
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, datetime.date, datetime.datetime)):
        return str(value)
    raise ValueError("expected a string")


class CameraConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    serial_number: str
    name: str = ""
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0
    window_x: float = -1.0
    window_y: float = -1.0
    window_width: float = -1.0
    window_height: float = -1.0
    # Auto-center the sensor ROI on each axis: when true, OffsetX/OffsetY are
    # derived from the full sensor size and the ROI size and recomputed whenever
    # the ROI changes, instead of being set by hand.
    center_x: bool = False
    center_y: bool = False

    @field_validator("serial_number", "name", mode="before")
    @classmethod
    def _as_scalar_str(cls, value: object) -> str:
        return _scalar_str(value)


def _valid_ffmpeg_params(value: str) -> str:
    """Reject an ffmpeg_params string ffmpeg could never parse (bad quoting).

    Raising here lets ``_lenient_validate`` fall the field back to its default
    rather than letting the malformed string reach ffmpeg at record/transcode."""
    try:
        shlex.split(value)
    except ValueError as e:
        raise ValueError(f"invalid ffmpeg_params: {e}") from e
    return value


class RecordConfig(BaseModel):
    """The ``[record]`` section: how and where recordings are captured.

    ``directory``/``relative_directory`` are path templates resolved at record
    start: they accept strftime ``%``-codes (see :func:`resolve_save_dir`).
    ``ffmpeg_params`` is the verbatim encoder arg string used when
    ``save_method == "ffmpeg"``.
    """

    model_config = ConfigDict(extra="ignore")

    fps: float = 100.0
    duration: float = 5.0
    duration_unit: Literal["frames", "seconds", "minutes", "hours"] = "seconds"
    trigger_source: Literal["software", "managed", "external"] = "software"
    # How preview is triggered so it approximates the recording. "auto" mirrors
    # trigger_source; "software"/"free_running" force that preview mode. See
    # RecordingController._effective_preview_mode.
    preview_trigger_source: Literal["auto", "software", "free_running"] = "auto"
    directory: str = "./"
    relative_directory: str = ""
    # "ffmpeg" = CPU (libx264); "nvenc" = NVIDIA GPU (H.264 NVENC, falling back to
    # libx264 for cameras beyond max_nvenc_sessions); "raw" = Mono8 dump for
    # offline transcoding.
    save_method: Literal["ffmpeg", "raw", "nvenc"] = "ffmpeg"
    # CPU encoder args (save_method="ffmpeg").
    ffmpeg_params: str = DEFAULT_FFMPEG_PARAMS
    # GPU encoder args (save_method="nvenc"). A separate field so the CPU and GPU
    # presets persist independently; defaults to the curated NVENC H.264 preset.
    nvenc_params: str = NVENC_H264_PARAMS
    # Max concurrent NVENC (GPU) encode sessions to use when save_method="nvenc";
    # cameras beyond this encode on CPU instead of failing. Omit (None) to
    # auto-detect the GPU/driver cap (one consumer GeForce allows only a handful —
    # 8 on driver 570; a Quadro/patched driver allows more); set an int to cap it
    # lower (e.g. to reserve GPU headroom). `octacam doctor` reports the detected
    # limit for this GPU.
    max_nvenc_sessions: int | None = None
    # Frames buffered per camera between the grab loop and the encoder. The grab
    # loop never blocks, so a frame arriving while this queue is full is dropped;
    # a deeper queue absorbs a transient encoder stall (bursty ffmpeg/GPU) at the
    # cost of peak RAM (queue x frame bytes x cameras). Raise it if a fast rig
    # shows brief drop bursts; lower it on memory-tight / high-resolution rigs.
    writer_queue_size: int = 64
    # true bakes each camera's display transform (rotation/flips) into the video
    # (old record_form "display"); false saves the raw sensor image ("sensor").
    save_transformed: bool = True
    save_timestamps: bool = False

    @field_validator(
        "directory",
        "relative_directory",
        "ffmpeg_params",
        "nvenc_params",
        mode="before",
    )
    @classmethod
    def _as_scalar_str(cls, value: object) -> str:
        return _scalar_str(value)

    @field_validator("ffmpeg_params", "nvenc_params")
    @classmethod
    def _check_ffmpeg_params(cls, value: str) -> str:
        return _valid_ffmpeg_params(value)

    @field_validator("max_nvenc_sessions")
    @classmethod
    def _floor_nvenc_sessions(cls, value: int | None) -> int | None:
        # None = auto-detect the GPU session cap; an int is floored at 0.
        return None if value is None else max(0, value)

    @field_validator("writer_queue_size")
    @classmethod
    def _floor_writer_queue_size(cls, value: int) -> int:
        # A queue.Queue(maxsize=0) is *unbounded* (would grow to OOM on a slow
        # encoder); floor at 1 so the bound is always meaningful.
        return max(1, value)


class TranscodeConfig(BaseModel):
    """The ``[transcode]`` section: encoder args for `octacam process`."""

    model_config = ConfigDict(extra="ignore")

    ffmpeg_params: str = DEFAULT_TRANSCODE_FFMPEG_PARAMS
    # Delete each source .mkv/.raw once it transcodes successfully (local, no
    # network involved — independent of transfer.delete_after_transfer, which
    # gates on a NAS-verified copy instead). The CLI --delete-source/
    # --no-delete-source flag overrides this either way.
    delete_source: bool = False

    @field_validator("ffmpeg_params", mode="before")
    @classmethod
    def _as_scalar_str(cls, value: object) -> str:
        return _scalar_str(value)

    @field_validator("ffmpeg_params")
    @classmethod
    def _check_ffmpeg_params(cls, value: str) -> str:
        return _valid_ffmpeg_params(value)


class VisualizationConfig(BaseModel):
    """One ``[[visualization]]`` entry: a named composite grid to generate.

    ``octacam process`` builds ``<name>`` in each recording folder from
    ``layout`` (a 2D list of camera names; ``""`` is a black fill cell, all rows
    equal length). Optional ``ffmpeg_params`` overrides the encoder args for this
    grid; empty falls back to ``[transcode].ffmpeg_params`` (with pix_fmt forced
    to a widely-playable yuv420p).

    Example for a 3×3 grid on an 8-camera rig::

        [[visualization]]
        name = "grid.mp4"
        layout = [
            ["camera_LF", "",          "camera_RF"],
            ["camera_LM", "camera_F",  "camera_RM"],
            ["camera_LH", "camera_H",  "camera_RH"],
        ]
    """

    model_config = ConfigDict(extra="ignore")

    name: str = "grid.mp4"
    layout: list[list[str]] = Field(default_factory=list)
    ffmpeg_params: str = ""

    @field_validator("name", "ffmpeg_params", mode="before")
    @classmethod
    def _as_scalar_str(cls, value: object) -> str:
        return _scalar_str(value)


class TwoPhotonTransferConfig(BaseModel):
    """The ``[transfer.twophoton]`` sub-table: pairing behavior takes with 2P data.

    Presence of this table (even empty) turns the feature on for a rig;
    ``transfer.twophoton = None`` (the default — no ``[transfer.twophoton]``
    section at all) means "no 2P pairing for this rig." ``source`` is the root
    to scan for ThorSync (``SyncData*``)/ThorImage folders — same strftime
    ``%``-codes as ``record.directory``. ``match_window_s`` is the max
    deviation allowed on *each* of a candidate's start/end from the take's
    corresponding endpoint (not just "the two windows overlap somewhere" —
    real data found a short, unrelated 2P snapshot nested entirely inside a
    much longer take otherwise satisfies a plain overlap check) accepted as a
    timestamp-only pairing. 60s comfortably covers the documented ~20-40s
    ThorSync startup lag plus a ThorImage folder's own ~25-30s disk
    write-out lag (it buffers frames during acquisition and flushes them to
    `.tif` files afterward, confirmed by comparing a verified pairing's
    `FrameOut` edge timing against its files' raw mtimes) while still
    rejecting the ~90s+ deviations a real spurious match showed. ``settle_s``
    is how long a 2P folder's mtime must be quiescent before it's considered
    finished writing.

    ``verify_with_signals`` attempts to confirm a ``SyncData*`` pairing by
    reading its actual recorded DAQ signals (``octacam.twophoton_signals`` —
    the ``Cameras``/``FrameOut`` digital-input edge counts against the take's
    own recorded frame count / ThorImage's own timepoints) instead of trusting
    the timestamp guess; it degrades to timestamp-only automatically when
    ``h5py`` isn't installed or a file can't be read. ``verify_window_s`` is
    a separate, wider time window bounding which ``SyncData*`` folders are
    even opened for verification (looser than ``match_window_s`` is fine —
    an exact edge-count match is decisive regardless of how loose the coarse
    timestamp gap was).

    ``assemble_tiff_stacks`` consolidates ThorImage's per-frame streaming-mode
    TIFFs (one ``.tif`` per frame per channel — thousands of files on a real
    recording) into one OME-TIFF stack per channel at transfer time, instead
    of dumping every per-frame file onto the NAS. Degrades to a plain
    per-file copy automatically, like ``verify_with_signals`` does without
    ``h5py``, when the ``twophoton`` extra's ``tifffile`` dependency is
    absent or when a channel's file set can't be safely assembled
    (non-contiguous/duplicate frame indices, or its count matches neither
    ``Experiment.xml``'s own ``Timelapse`` timepoints nor ``ZStage`` steps).
    """

    model_config = ConfigDict(extra="ignore")

    source: str = ""
    match_window_s: float = 60.0
    settle_s: float = 300.0
    verify_with_signals: bool = True
    verify_window_s: float = 3600.0
    assemble_tiff_stacks: bool = True

    @field_validator("source", mode="before")
    @classmethod
    def _as_scalar_str(cls, value: object) -> str:
        return _scalar_str(value)


class TwoPhotonUserOverride(BaseModel):
    """``[transfer.users.<initials>.twophoton]``: the one 2P field that
    genuinely differs per person — their own ThorSync/ThorImage working
    folder on the share. Everything else (``match_window_s``, ``settle_s``,
    ``verify_with_signals``, ...) is rig-timing-tuned and stays shared,
    inherited from the rig's own ``[transfer.twophoton]`` block."""

    model_config = ConfigDict(extra="ignore")

    source: str = ""


class TransferUserOverride(BaseModel):
    """``[transfer.users.<initials>]``: overrides only the save-destination
    fields of the shared ``[transfer]`` block for one named lab member —
    everything else (``checksum``, ``delete_after_transfer``, ...) stays
    rig-wide policy, since those are hardware/timing-tuned and have no
    reason to differ per person. See ``resolve_transfer_for_user``."""

    model_config = ConfigDict(extra="ignore")

    directory: str = ""
    twophoton: TwoPhotonUserOverride | None = None


class TransferConfig(BaseModel):
    """The ``[transfer]`` section: where `octacam process` mirrors recordings.

    Each recording folder is copied to ``directory``/``relative_directory`` (the
    ``relative_directory`` resolved at record time and stored in the summary), so
    the local tree is mirrored on the destination. ``directory`` supports the
    same strftime ``%``-codes as ``record.directory``. ``checksum``
    content-verifies each copy before promoting it (false = size-only).

    ``users`` holds one ``[transfer.users.<initials>]`` entry per lab member
    sharing this rig — nobody, including the rig's usual owner, is an
    implicit default; a save destination is only ever picked by an explicit
    ``--user <initials>`` (see ``resolve_transfer_for_user``). Absent
    entirely when nobody has configured any per-user profiles yet.

    ``users_root`` is the NAS root new profiles get created under (e.g. by
    the GUI's self-service "add yourself") — ``<users_root>/<initials>/
    octacam_2P``. Set automatically, once, the first time ``octacam config
    --bootstrap-users <nas-root>`` runs (never overwritten after — a
    deliberate custom value always wins); blank means self-service creation
    must be given an explicit directory instead of auto-suggesting one.
    """

    model_config = ConfigDict(extra="ignore")

    directory: str = ""
    checksum: bool = True
    # Delete the local recording folder (and any matched 2P source folder, see
    # twophoton below) once every file is checksum-verified present on the
    # NAS. Off by default; the CLI --delete-after-transfer/
    # --no-delete-after-transfer flag overrides this either way. Always forces
    # checksum verification internally when active, regardless of `checksum`.
    delete_after_transfer: bool = False
    twophoton: TwoPhotonTransferConfig | None = None
    users: dict[str, TransferUserOverride] = {}
    users_root: str = ""

    @field_validator("directory", mode="before")
    @classmethod
    def _as_scalar_str(cls, value: object) -> str:
        return _scalar_str(value)


class GuiConfig(BaseModel):
    """The ``[gui]`` section: pure web-UI render settings (rig-tunable)."""

    model_config = ConfigDict(extra="ignore")

    display_refresh_interval_ms: int = 33
    # Default colour theme for the web GUI ("dark" or "light"). Applied on load
    # as the rig default; a per-browser choice made with the header toggle (kept
    # in localStorage) always overrides it.
    theme: Literal["dark", "light"] = "dark"


class PluginConfig(BaseModel):
    name: str
    options: dict = Field(default_factory=dict)


# Backend names accepted in the TOML ``backend`` key: "auto" plus every concrete
# backend the registry knows (kept in sync with ``registry.BACKENDS``). Listing
# them as a literal here avoids importing the cameras package (and its heavy SDK
# deps) just to validate a config. ``spinnaker`` (the Spinnaker C-API tier) is
# included so a rig without PySpin installed can still pin its FLIRs to it by
# name instead of only reaching it via "auto".
_BACKENDS = (
    "auto",
    "basler",
    "flir",
    "spinnaker",
    "pycameleon",
    "fake",
)


class OctacamConfig(BaseModel):
    # Which camera backend(s) this rig uses. "auto" (the default, and what an
    # absent key means) resolves to the preference cascade: each camera is claimed
    # by the best available tier that sees it — vendor SDK (basler/flir), then the
    # Spinnaker C-API tier (spinnaker) that claims the FLIRs when PySpin is not
    # installed, then the always-present pycameleon floor — so a rig just
    # uses whatever is plugged in with whatever is installed. A concrete name pins
    # the rig to one backend (including "spinnaker" for the FLIR C-API tier). Every
    # existing config keeps working.
    backend: str = "auto"
    record: RecordConfig = Field(default_factory=RecordConfig)
    transcode: TranscodeConfig = Field(default_factory=TranscodeConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)
    plugins: list[PluginConfig] = Field(default_factory=list)
    visualization: list[VisualizationConfig] = Field(default_factory=list)
    transfer: TransferConfig | None = None
    gui: GuiConfig = Field(default_factory=GuiConfig)


# ---------------------------------------------------------------------------
# Save-directory templating (resolved at record start)
# ---------------------------------------------------------------------------

_DURATION_UNIT_SECONDS = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0}


def duration_to_seconds(duration: float, unit: str, fps: float) -> float:
    """Convert a record ``duration`` in its ``unit`` to seconds.

    ``"frames"`` divides by ``fps`` (a frame count at the recording rate); the
    other units multiply by their seconds factor."""
    if unit == "frames":
        return duration / fps if fps > 0 else 0.0
    return duration * _DURATION_UNIT_SECONDS.get(unit, 1.0)


def _apply_template(text: str, when: time.struct_time) -> str:
    """Expand strftime ``%``-codes in a path template.

    Tolerant: a bad strftime code is warned about and the text is left as-is
    rather than raising, matching the module's parsing philosophy."""
    try:
        return time.strftime(text, when)
    except ValueError as e:
        log.warning("Could not expand strftime codes in path template %r: %s", text, e)
        return text


def _normalize_dir(text: str) -> str:
    """Strip, expand ``~``, make absolute, forward-slash — mirrors the GUI."""
    return str(Path(text.strip()).expanduser().absolute()).replace("\\", "/")


def resolve_dir_template(template: str, when: time.struct_time | None = None) -> str:
    """Resolve a directory template to an absolute path.

    Used for ``record.directory`` and ``transfer.directory`` (both accept
    strftime ``%``-codes, expanded at record time)."""
    when = when or time.localtime()
    return _normalize_dir(_apply_template(template, when))


def resolve_record_directory(
    record: RecordConfig, when: time.struct_time | None = None
) -> str:
    """Resolve just ``record.directory`` (the base the save dir sits under)."""
    return resolve_dir_template(record.directory, when)


def resolve_transfer_for_user(transfer: TransferConfig | None, user: str) -> TransferConfig:
    """Apply a ``--user`` override on top of the shared ``[transfer]`` block.

    Unlike the rest of this module's tolerant, warn-and-default parsing, this
    raises ``ValueError`` (never silently falls back) when *user* has no
    ``[transfer.users.<user>]`` entry — a ``--user`` typo is a CLI
    input-validation error, categorically different from "a config file has
    a malformed value", and must never quietly transfer to the wrong
    destination. Callers (cli.py) turn this into a clear ``sys.exit``.
    """
    if transfer is None or user not in transfer.users:
        known = sorted(transfer.users) if transfer else []
        raise ValueError(
            f'--user "{user}" has no [transfer.users.{user}] entry in this config'
            + (f" (known: {', '.join(known)})" if known else " (no [transfer.users.*] configured)")
        )
    override = transfer.users[user]
    result = transfer.model_copy(deep=True)
    if override.directory:
        result.directory = override.directory
    if override.twophoton and override.twophoton.source and result.twophoton:
        result.twophoton = result.twophoton.model_copy(update={"source": override.twophoton.source})
    return result


def resolve_relative_directory(
    record: RecordConfig, when: time.struct_time | None = None
) -> str:
    """Resolve ``record.relative_directory`` (strftime codes expanded, kept relative).

    Unlike ``resolve_record_directory`` this is *not* absolutized: it is the
    sub-path that sits under the base directory (and that the transfer step
    mirrors onto the destination)."""
    when = when or time.localtime()
    return _apply_template(record.relative_directory, when)


def resolve_save_dir(record: RecordConfig, when: time.struct_time | None = None) -> str:
    """Resolve the absolute save directory from directory + relative_directory.

    Templating happens at record start (pass a single ``when`` snapshot so both
    parts share one date). Returns an absolute, ``~``-expanded path."""
    when = when or time.localtime()
    base = _apply_template(record.directory, when)
    rel = _apply_template(record.relative_directory, when)
    combined = os.path.join(base, rel) if rel else base
    return _normalize_dir(combined)


def _parse_transfer(transfer_src: object) -> TransferConfig | None:
    """Parse the optional ``[transfer]`` section, returning None on any problem."""
    if transfer_src is None:
        return None
    if not isinstance(transfer_src, dict):
        log.warning('Ignoring "transfer" in octacam config as it is not a table')
        return None
    return _lenient_validate(TransferConfig, transfer_src, "transfer", TransferConfig())


def _parse_layout(layout_src: object, context: str) -> list[list[str]] | None:
    """Validate a 2D camera-name grid layout, returning None on any problem."""
    if layout_src is None:
        log.warning('%s is missing a "layout" key; ignoring it', context)
        return None
    if not isinstance(layout_src, list):
        log.warning('%s "layout" must be an array of arrays; ignoring it', context)
        return None
    layout: list[list[str]] = []
    expected_cols: int | None = None
    for row_i, row in enumerate(layout_src):
        if not isinstance(row, list):
            log.warning(
                '%s "layout" row %d is not an array; ignoring it', context, row_i
            )
            return None
        if not all(isinstance(cell, str) for cell in row):
            log.warning(
                '%s "layout" row %d has non-string cells; ignoring it', context, row_i
            )
            return None
        if expected_cols is None:
            expected_cols = len(row)
        elif len(row) != expected_cols:
            log.warning(
                '%s "layout" rows have inconsistent lengths (%d vs %d); ignoring it',
                context,
                expected_cols,
                len(row),
            )
            return None
        layout.append([str(cell) for cell in row])
    if not layout:
        log.warning('%s "layout" is empty; ignoring it', context)
        return None
    return layout


def _parse_visualization(src: object) -> list[VisualizationConfig]:
    """Parse the optional ``[[visualization]]`` array; each entry is generated.

    Malformed entries (missing/invalid layout, duplicate output name) are warned
    about and skipped, never raised."""
    if src is None:
        return []
    if not isinstance(src, list):
        log.warning('Ignoring "visualization" in octacam config as it is not an array')
        return []
    result: list[VisualizationConfig] = []
    seen_names: set[str] = set()
    for index, entry in enumerate(src):
        context = f'the {index}th "visualization" entry'
        if not isinstance(entry, dict):
            log.warning("Ignoring %s as it is not a table", context)
            continue
        layout = _parse_layout(entry.get("layout"), context)
        if layout is None:
            continue
        try:
            name = _scalar_str(entry.get("name", "grid.mp4"))
        except ValueError:
            log.warning('Ignoring invalid "name" in %s; using "grid.mp4"', context)
            name = "grid.mp4"
        try:
            ffmpeg_params = _scalar_str(entry.get("ffmpeg_params", ""))
        except ValueError:
            log.warning('Ignoring invalid "ffmpeg_params" in %s', context)
            ffmpeg_params = ""
        if name in seen_names:
            log.warning(
                "Ignoring %s as its output name %r is already used", context, name
            )
            continue
        seen_names.add(name)
        result.append(
            VisualizationConfig(name=name, layout=layout, ffmpeg_params=ffmpeg_params)
        )
    return result


def _validate_visualization_cameras(config: OctacamConfig) -> None:
    """Warn about visualization-layout cells naming a camera not in ``[[cameras]]``.

    Runs only when both a visualization and an explicit camera list exist (an
    empty camera list means "use all detected", whose names aren't known here).
    A typo'd cell would otherwise silently render as a black tile with no hint.
    """
    if not config.visualization or not config.cameras:
        return
    known = {c.name for c in config.cameras if c.name}
    unknown = sorted(
        {
            cell
            for viz in config.visualization
            for row in viz.layout
            for cell in row
            if cell and cell not in known
        }
    )
    if unknown:
        log.warning(
            "A [[visualization]] layout references unknown camera(s) %s — those "
            "cells will render black. Known cameras: %s",
            ", ".join(repr(u) for u in unknown),
            ", ".join(sorted(known)) or "(none)",
        )


def _parse_backend(value: object) -> str:
    """Parse the optional top-level ``backend`` key, tolerantly (defaults auto)."""
    if value is None:
        return "auto"
    try:
        name = _scalar_str(value).strip().lower()
    except ValueError:
        log.warning('Ignoring "backend" in octacam config as it is not a string')
        return "auto"
    if name not in _BACKENDS:
        log.warning(
            'Ignoring unknown "backend" %r in octacam config; using "auto"', name
        )
        return "auto"
    return name


def _lenient_validate(
    model_cls: type[_ModelT], data: dict, context: str, fallback: _ModelT
) -> _ModelT:
    """Validate ``data`` against ``model_cls``, dropping invalid fields.

    Each field that fails validation is warned about and removed (so its model
    default applies), then validation is retried. This reproduces the original
    "a bad field keeps its default" behavior on top of pydantic."""
    data = dict(data)
    while True:
        try:
            return model_cls.model_validate(data)
        except ValidationError as exc:
            removable = {
                err["loc"][0]
                for err in exc.errors()
                if err["loc"]
                and isinstance(err["loc"][0], str)
                and err["loc"][0] in data
            }
            if not removable:
                log.warning(
                    'Could not parse the "%s" config section; using defaults', context
                )
                return fallback
            for key in removable:
                log.warning(
                    'Ignoring invalid "%s" in %s; using the default', key, context
                )
                data.pop(key, None)


def _parse_plugins(plugins_src: object) -> list[PluginConfig]:
    """Parse the optional ``plugins`` array (opt-in plugin selection).

    Each entry is either a bare name (``plugins = ["flywheel"]``) or a table with
    ``name`` and optional ``options`` (``[[plugins]]`` / ``name = "flywheel"`` +
    ``[plugins.options]``). Malformed or duplicate entries are warned about and
    skipped — never raised — matching the rest of this module's tolerant
    parsing."""
    if plugins_src is None:
        return []
    if not isinstance(plugins_src, list):
        log.warning('Ignoring "plugins" in octacam config as it is not an array')
        return []
    result: list[PluginConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(plugins_src):
        name: str | None = None
        options: dict = {}
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            raw_name = entry.get("name")
            if isinstance(raw_name, str) and raw_name:
                name = raw_name
                raw_options = entry.get("options", {})
                if isinstance(raw_options, dict):
                    options = raw_options
                elif raw_options is not None:
                    log.warning(
                        'Ignoring options for plugin "%s" as they are not a table',
                        name,
                    )
        if not name:
            log.warning(
                'Ignoring the %dth entry of "plugins" as it is malformed', index
            )
            continue
        if name in seen:
            log.warning('Ignoring duplicate plugin "%s" in the config file', name)
            continue
        seen.add(name)
        result.append(PluginConfig(name=name, options=options))
    return result


def _is_safe_camera_name(name: str) -> bool:
    """Whether ``name`` is usable as a per-camera video filename stem.

    A camera's name becomes ``<name>.<ext>`` at record time, so it must be a
    single path segment with no traversal. Mirrors
    ``controller.sanitize_camera_name``, duplicated here so this parse module
    stays free of the heavier camera/controller imports.
    """
    return (
        name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def _parse_cameras(cameras_src: list) -> list[CameraConfig]:
    cameras: list[CameraConfig] = []
    used_serial_numbers: set[str] = set()
    used_names: set[str] = set()

    for index, src in enumerate(cameras_src):
        if not isinstance(src, dict) or "serial_number" not in src:
            log.warning(
                'Ignoring the %dth entry of "cameras" as its "serial_number" is absent',
                index,
            )
            continue
        try:
            serial_number = _scalar_str(src["serial_number"])
        except ValueError:
            log.warning(
                'Ignoring the %dth entry of "cameras" as its "serial_number" is not a scalar',
                index,
            )
            continue
        if serial_number in used_serial_numbers:
            log.warning(
                'Ignoring the %dth entry of "cameras" as its "serial_number" is not unique',
                index,
            )
            continue
        used_serial_numbers.add(serial_number)

        fields = dict(src)
        fields["serial_number"] = serial_number
        camera = _lenient_validate(
            CameraConfig,
            fields,
            f'the {index}th entry of "cameras"',
            CameraConfig(serial_number=serial_number),
        )
        if camera.name and not _is_safe_camera_name(camera.name):
            log.warning(
                'Ignoring unsafe "name" %r in the %dth entry of "cameras"; '
                "falling back to the serial number",
                camera.name,
                index,
            )
            camera.name = ""
        # A blank name records under the serial number as the filename stem, so
        # validate the *effective* name (name or serial) to catch a later entry
        # whose explicit name collides with this camera's serial fallback.
        effective_name = camera.name or serial_number
        if effective_name in used_names:
            log.warning(
                'Ignoring the %dth entry of "cameras" as its "name" is not unique',
                index,
            )
            continue
        used_names.add(effective_name)
        cameras.append(camera)
    return cameras


def _parse_section(data: dict, key: str, model_cls: type[_ModelT], default: _ModelT):
    """Parse a single-table section (``[key]``) via lenient validation."""
    src = data.get(key)
    if src is None:
        return default
    if not isinstance(src, dict):
        log.warning('Ignoring "%s" in octacam config as it is not a table', key)
        return default
    return _lenient_validate(model_cls, src, key, default)


def parse_config(file_path: str | Path) -> OctacamConfig:
    config = OctacamConfig()
    file_path = Path(file_path)

    if not file_path.exists():
        log.info("octacam config file not found at %s.", file_path)
        log.info("All detected cameras will be used.")
        return config

    try:
        data = tomllib.loads(file_path.read_text())
    except tomllib.TOMLDecodeError as e:
        log.error("Failed to parse octacam config file: %s", e)
        return config

    config.backend = _parse_backend(data.get("backend"))
    config.record = _parse_section(data, "record", RecordConfig, RecordConfig())
    config.transcode = _parse_section(
        data, "transcode", TranscodeConfig, TranscodeConfig()
    )
    config.gui = _parse_section(data, "gui", GuiConfig, GuiConfig())
    config.visualization = _parse_visualization(data.get("visualization"))
    config.transfer = _parse_transfer(data.get("transfer"))

    # Parsed before the cameras block, which has several early returns.
    config.plugins = _parse_plugins(data.get("plugins"))

    cameras_src = data.get("cameras")
    if cameras_src is None:
        return config
    if not isinstance(cameras_src, list):
        log.warning('Ignoring "cameras" in octacam config as it is not an array')
        return config

    config.cameras = _parse_cameras(cameras_src)
    if not config.cameras:
        log.info(
            "No cameras found in octacam config file. All detected cameras "
            "will be used."
        )
        return config

    log.info("Found %d camera(s) in octacam config file", len(config.cameras))
    _validate_visualization_cameras(config)
    return config


def find_config_file(config_dir: str | Path) -> Path:
    return Path(config_dir) / "octacam_config.toml"


def load_config_dir(config_dir: str | Path) -> OctacamConfig:
    return parse_config(find_config_file(config_dir))
