"""octacam_config.toml parsing, the save-path and safe-name rules, and the live
`RecordingSettings` a config seeds.

The config is tolerant per field: a malformed section or field is warned about
and falls back to its default (`_lenient_validate`). A file that does not
parse at all raises `ConfigError`, since stock defaults would silently
run the wrong rig. The live settings are strict instead:
`RecordingSettings.updated` rejects a bad value.
"""

import dataclasses
import datetime
import difflib
import logging
import os
import re
import shlex
import time
import tomllib
import types
import typing
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    PositiveFloat,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticCustomError

from octacam.recording_format import (
    RECORDING_INFO_DIRNAME,
    RECORDING_SUMMARY_FILENAME,
    recording_info_dir,
)
from octacam.transcode import DEFAULT_TRANSCODE_FFMPEG_PARAMS
from octacam.writer import (
    DEFAULT_FFMPEG_PARAMS,
    FORMATS,
    NVENC_H264_PARAMS,
    VideoFormat,
)

log = logging.getLogger("octacam")


def _scalar_str(value: object) -> str:
    """Coerce a TOML scalar to a string, rejecting bool/array/table: an unquoted
    serial number or date-like save directory parses as an int or a date.
    """
    if isinstance(value, bool):
        # pydantic reports a ValueError, not a TypeError, as a validation error.
        raise ValueError("expected a string, got a boolean")  # noqa: TRY004
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, datetime.date, datetime.datetime)):
        return str(value)
    raise ValueError("expected a string")


def _split_ffmpeg_args(value: str) -> str:
    try:
        shlex.split(value)
    except ValueError as e:
        raise PydanticCustomError(
            "ffmpeg_args", "bad quoting ({error})", {"error": str(e)}
        ) from e
    return value


def _at_least(floor: int) -> AfterValidator:
    return AfterValidator(lambda value: max(floor, value))


# Field types shared by the [record] config and RecordingSettings. A config
# string field is a ScalarStr; encoder args must split like a shell line, as the
# writer splits them at open.
ScalarStr = Annotated[str, BeforeValidator(_scalar_str)]
FfmpegArgs = Annotated[str, AfterValidator(_split_ffmpeg_args)]
DurationUnit = Literal["frames", "seconds", "minutes", "hours"]
TriggerSource = Literal["software", "managed", "external"]
# "auto" mirrors trigger_source (RecordingController._effective_preview_mode).
PreviewTriggerSource = Literal["auto", "software", "free_running"]
# "ffmpeg" = CPU (libx264); "nvenc" = NVIDIA GPU, cameras beyond
# max_nvenc_sessions on CPU; "raw" = Mono8 dump, transcoded later.
SaveMethod = Literal["ffmpeg", "nvenc", "raw"]
# "display" bakes the display transform into the video; "sensor" does not
# (the config's save_transformed).
RecordForm = Literal["display", "sensor"]
_ScalarFfmpegArgs = Annotated[FfmpegArgs, BeforeValidator(_scalar_str)]


class CameraConfig(BaseModel):
    serial_number: ScalarStr
    name: ScalarStr = ""
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0
    window_x: float = -1.0
    window_y: float = -1.0
    window_width: float = -1.0
    window_height: float = -1.0
    # Center the ROI on that axis (its offset follows every ROI change).
    center_x: bool = False
    center_y: bool = False


class RecordConfig(BaseModel):
    """The `[record]` section: how and where recordings are captured.
    `directory`/`relative_directory` take strftime `%`-codes, expanded at
    record start (`resolve_save_path`).
    """

    fps: float = 100.0
    duration: float = 5.0
    duration_unit: DurationUnit = "seconds"
    trigger_source: TriggerSource = "software"
    preview_trigger_source: PreviewTriggerSource = "auto"
    directory: ScalarStr = "./"
    relative_directory: ScalarStr = ""
    save_method: SaveMethod = "ffmpeg"
    # Encoder args per method, kept apart so each preset persists.
    ffmpeg_params: _ScalarFfmpegArgs = DEFAULT_FFMPEG_PARAMS
    nvenc_params: _ScalarFfmpegArgs = NVENC_H264_PARAMS
    # None = the GPU's detected session cap (`octacam doctor` reports it); an
    # int caps it lower.
    max_nvenc_sessions: Annotated[int, _at_least(0)] | None = None
    # Frames buffered per camera before the encoder: a deeper queue absorbs a
    # transient encoder stall, at the cost of peak RAM (queue x frame x cameras).
    # Floored at 1: a queue.Queue(maxsize=0) is unbounded.
    writer_queue_size: Annotated[int, _at_least(1)] = 64
    # Bake each camera's display transform (rotation, flips) into the video.
    save_transformed: bool = True
    save_timestamps: bool = False


class TranscodeConfig(BaseModel):
    """The `[transcode]` section: encoder args for `octacam process`."""

    ffmpeg_params: _ScalarFfmpegArgs = DEFAULT_TRANSCODE_FFMPEG_PARAMS


class VisualizationConfig(BaseModel):
    """One `[[visualization]]` entry: a composite grid video `octacam
    process` builds in each recording folder.

    `layout` is a 2D list of camera names, rows of equal length, `""` a black
    cell. An empty `ffmpeg_params` uses `[transcode].ffmpeg_params`.
    For example, a 3x3 grid on an 8-camera rig::

        [[visualization]]
        name = "grid.mp4"
        layout = [
            ["camera_LF", "",          "camera_RF"],
            ["camera_LM", "camera_F",  "camera_RM"],
            ["camera_LH", "camera_H",  "camera_RH"],
        ]
    """

    name: ScalarStr = "grid.mp4"
    layout: list[list[str]]
    ffmpeg_params: _ScalarFfmpegArgs = ""

    @field_validator("layout")
    @classmethod
    def _rectangular(cls, layout: list[list[str]]) -> list[list[str]]:
        if not layout:
            raise ValueError("the layout is empty")
        if len({len(row) for row in layout}) > 1:
            raise ValueError("the layout's rows differ in length")
        return layout


class TransferConfig(BaseModel):
    """The `[transfer]` section: where `octacam process` mirrors recordings.

    A recording goes to `directory`/<its summary's `relative_directory`>;
    `directory` takes strftime `%`-codes like `record.directory`.
    `checksum` verifies each copy's content (false: its size only).
    """

    directory: ScalarStr = ""
    checksum: bool = True


class GuiConfig(BaseModel):
    """The `[gui]` section: pure web-UI render settings (rig-tunable)."""

    display_refresh_interval_ms: int = 33
    # The rig's default theme; a browser's own choice overrides it.
    theme: Literal["dark", "light"] = "dark"


class PluginConfig(BaseModel):
    name: str
    options: dict = Field(default_factory=dict)


# "auto" plus registry.BACKENDS (test_config keeps them in step), listed here so
# parsing a config imports no camera layer.
_BACKENDS = ("auto", "basler", "flir", "spinnaker", "pycameleon", "fake")


class OctacamConfig(BaseModel):
    # "auto": each camera is claimed by the best installed tier that sees it
    # (registry.CASCADE); a backend name pins the rig to that one.
    backend: str = "auto"
    record: RecordConfig = Field(default_factory=RecordConfig)
    transcode: TranscodeConfig = Field(default_factory=TranscodeConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)
    plugins: list[PluginConfig] = Field(default_factory=list)
    visualization: list[VisualizationConfig] = Field(default_factory=list)
    transfer: TransferConfig | None = None
    gui: GuiConfig = Field(default_factory=GuiConfig)


# ---------------------------------------------------------------------------
# Save paths (resolved at record start)
# ---------------------------------------------------------------------------

_DURATION_UNIT_SECONDS = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0}
_TRAILING_NUMBER_RE = re.compile(r"\d{3}")


def duration_to_seconds(duration: float, unit: str, fps: float) -> float:
    """A record `duration` in its `unit` (`"frames"` at `fps`) as seconds."""
    if unit == "frames":
        return duration / fps if fps > 0 else 0.0
    return duration * _DURATION_UNIT_SECONDS.get(unit, 1.0)


def _apply_template(text: str, when: time.struct_time) -> str:
    """Expand strftime `%`-codes in a path template; a bad code is warned
    about and the text kept.
    """
    try:
        return time.strftime(text, when)
    except ValueError as e:
        log.warning("Could not expand strftime codes in path template %r: %s", text, e)
        return text


def normalize_dir(text: str) -> str:
    """Strip, expand `~`, make absolute, use forward slashes."""
    return str(Path(text.strip()).expanduser().absolute()).replace("\\", "/")


def compose_save_dir(base: str, relative: str) -> str:
    """`base`/`relative`, normalized; an absolute `relative` discards the
    base.
    """
    return normalize_dir(os.path.join(base, relative) if relative else base)


def increment_trailing_number(text: str) -> str:
    """Increment the last 3-digit group: 001-bhv -> 002-bhv (else unchanged)."""
    matches = list(_TRAILING_NUMBER_RE.finditer(text))
    if not matches:
        return text
    last = matches[-1]
    return f"{text[: last.start()]}{int(last.group()) + 1:03d}{text[last.end() :]}"


def resolve_dir_template(template: str) -> str:
    """Resolve a directory template (strftime `%`-codes) to an absolute path."""
    return normalize_dir(_apply_template(template, time.localtime()))


class SavePath(NamedTuple):
    """Where a recording goes: `save_dir` is `directory`/`relative`."""

    save_dir: str
    directory: str
    # Kept relative: the transfer mirrors it under its destination.
    relative: str


def resolve_save_path(record: RecordConfig) -> SavePath:
    """`record.directory`/`relative_directory`, both expanded now, at one
    moment.
    """
    when = time.localtime()
    base = _apply_template(record.directory, when)
    relative = _apply_template(record.relative_directory, when)
    return SavePath(compose_save_dir(base, relative), normalize_dir(base), relative)


# ---------------------------------------------------------------------------
# Live settings (seeded from the config, edited in the GUI)
# ---------------------------------------------------------------------------


@dataclass
class RecordingSettings:
    """The live Record and Process settings (the `[record]` fields as
    `RecordConfig` documents them); `updated` checks the
    constraints declared here.
    """

    fps: PositiveFloat = 100.0
    duration_s: PositiveFloat = 20.0
    save_dir: str = "./"
    # save_dir is record_directory/relative_directory once either is set; the
    # transfer mirrors relative_directory (else save_dir's basename).
    record_directory: str = ""
    relative_directory: str = ""
    trigger_source: TriggerSource = "software"
    preview_trigger_source: PreviewTriggerSource = "auto"
    save_method: SaveMethod = "ffmpeg"
    ffmpeg_params: FfmpegArgs = DEFAULT_FFMPEG_PARAMS
    nvenc_params: FfmpegArgs = NVENC_H264_PARAMS
    max_nvenc_sessions: Annotated[StrictInt, Field(ge=0)] | None = None
    writer_queue_size: Annotated[StrictInt, Field(ge=1)] = 64
    record_form: RecordForm = "display"
    save_frame_timestamps: bool = False
    # `octacam process` params: unused during capture, patched into each
    # recording's config snapshot. An empty transfer_directory skips the transfer.
    transcode_ffmpeg_params: FfmpegArgs = DEFAULT_TRANSCODE_FFMPEG_PARAMS
    transfer_directory: str = ""
    transfer_checksum: bool = True

    @classmethod
    def from_config(
        cls, config: OctacamConfig, *, fps: float | None = None
    ) -> RecordingSettings:
        """The settings `config` loads as (its tolerance stands: nothing is
        re-checked), with the save dirs resolved now. `fps` overrides the
        config's, before a frame-count duration converts at it.
        """
        record, transfer = config.record, config.transfer
        fps = record.fps if fps is None else fps
        path = resolve_save_path(record)
        return cls(
            fps=fps,
            duration_s=duration_to_seconds(record.duration, record.duration_unit, fps),
            save_dir=path.save_dir,
            record_directory=path.directory,
            relative_directory=path.relative,
            trigger_source=record.trigger_source,
            preview_trigger_source=record.preview_trigger_source,
            save_method=record.save_method,
            ffmpeg_params=record.ffmpeg_params,
            nvenc_params=record.nvenc_params,
            max_nvenc_sessions=record.max_nvenc_sessions,
            writer_queue_size=record.writer_queue_size,
            record_form="display" if record.save_transformed else "sensor",
            save_frame_timestamps=record.save_timestamps,
            transcode_ffmpeg_params=config.transcode.ffmpeg_params,
            transfer_directory=transfer.directory if transfer else "",
            transfer_checksum=transfer.checksum if transfer else True,
        )

    def record_config_values(self) -> dict:
        """The settings as `[record]` keys, the inverse of `from_config`
        with `duration_s` for `duration`/`duration_unit`
        (config_writer.with_record_settings). The save path is left out: a
        snapshot keeps the config's templates so a relaunch resolves a fresh
        folder, and the path a recording used is in its summary.
        """
        return {
            "fps": self.fps,
            "duration_s": self.duration_s,
            "trigger_source": self.trigger_source,
            "preview_trigger_source": self.preview_trigger_source,
            "save_method": self.save_method,
            "ffmpeg_params": self.ffmpeg_params,
            "nvenc_params": self.nvenc_params,
            "max_nvenc_sessions": self.max_nvenc_sessions,
            "writer_queue_size": self.writer_queue_size,
            "save_transformed": self.record_form == "display",
            "save_timestamps": self.save_frame_timestamps,
        }

    def updated(self, /, **changes) -> RecordingSettings:
        """A copy with `changes` validated and applied, else ValueError naming
        each bad field. A `record_directory` or `relative_directory` edit
        recomposes save_dir from the split; a lone `save_dir` clears it.
        """
        unknown = changes.keys() - _SETTINGS_FIELDS
        if unknown:
            raise ValueError(f"Unknown settings: {sorted(unknown)}")
        try:
            # Only the changed fields: a config value the GUI would refuse
            # (fps 0) must not block editing another one.
            valid = _SETTINGS.validate_python(changes)
        except ValidationError as e:
            raise ValueError(
                "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
            ) from None
        new = dataclasses.replace(self, **{key: getattr(valid, key) for key in changes})
        if "record_directory" in changes:
            new.record_directory = normalize_dir(new.record_directory)
        if "record_directory" in changes or "relative_directory" in changes:
            new.save_dir = new._composed_save_dir()
        elif "save_dir" in changes:
            new = new.with_save_dir(new.save_dir)
        return new

    def video_format(self) -> VideoFormat:
        video_format = FORMATS[self.save_method]
        if self.save_method == "ffmpeg":
            params = self.ffmpeg_params
        elif self.save_method == "nvenc":
            params = self.nvenc_params
        else:
            return video_format
        return dataclasses.replace(video_format, ffmpeg_params=params)

    def with_save_dir(self, path: str) -> RecordingSettings:
        """An explicit save dir (`--output`, a lone GUI edit). It clears the
        split, or the transfer and next_take would recompose the old path.
        """
        return dataclasses.replace(
            self,
            save_dir=normalize_dir(path),
            record_directory="",
            relative_directory="",
        )

    def next_take(self) -> RecordingSettings:
        """The next recording's folder: the relative part's trailing number
        bumped, else save_dir's.
        """
        if not self.relative_directory.strip():
            return dataclasses.replace(
                self, save_dir=increment_trailing_number(self.save_dir)
            )
        new = dataclasses.replace(
            self, relative_directory=increment_trailing_number(self.relative_directory)
        )
        new.save_dir = new._composed_save_dir()
        return new

    def _composed_save_dir(self) -> str:
        # A live edit and the next take join the relative part stripped; the
        # first take (resolve_save_path) joins it as the config wrote it.
        return compose_save_dir(self.record_directory, self.relative_directory.strip())

    def relative_save_dir(self) -> str:
        """The recording folder relative to the base directory: the explicit
        `relative_directory`, else save_dir relative to the base, else (no
        base, or a folder outside it such as an --output override) its name.
        """
        if self.relative_directory.strip():
            return self.relative_directory
        if self.record_directory:
            try:
                rel = os.path.relpath(self.save_dir, self.record_directory)
                if not rel.startswith(".."):
                    return rel
            except ValueError:  # e.g. different drives on Windows
                pass
        return Path(self.save_dir).name


_SETTINGS = TypeAdapter(RecordingSettings)
_SETTINGS_FIELDS = frozenset(f.name for f in dataclasses.fields(RecordingSettings))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_visualization(src: object) -> list[VisualizationConfig]:
    """Parse the optional `[[visualization]]` array.

    An entry without a valid layout, or reusing an earlier entry's output name,
    is warned about and skipped; another invalid field keeps its default.
    """
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
        viz = _lenient_validate(VisualizationConfig, entry, context, None)
        if viz is None:
            continue
        if viz.name in seen_names:
            log.warning(
                "Ignoring %s as its output name %r is already used", context, viz.name
            )
            continue
        seen_names.add(viz.name)
        result.append(viz)
    return result


def _validate_visualization_cameras(config: OctacamConfig) -> None:
    """Warn about layout cells naming no configured camera (they render black).
    Without a camera list, the names are not known here.
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
            "A [[visualization]] layout references unknown camera(s) %s \N{EM DASH} "
            "those "
            "cells will render black. Known cameras: %s",
            ", ".join(repr(u) for u in unknown),
            ", ".join(sorted(known)) or "(none)",
        )


def _parse_backend(value: object) -> str:
    """Parse the optional top-level `backend` key (else `"auto"`)."""
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


def _lenient_validate[ModelT: BaseModel, DefaultT](
    model_cls: type[ModelT], data: dict, context: str, fallback: DefaultT
) -> ModelT | DefaultT:
    """Validate `data` against `model_cls`, warning about and dropping each
    invalid field so its default applies.

    `fallback` when that cannot help: a required field is missing or invalid,
    or an error names no field.
    """
    data = dict(data)
    fields = model_cls.model_fields
    while True:
        try:
            return model_cls.model_validate(data)
        except ValidationError as exc:
            for err in exc.errors():
                key = err["loc"][0] if err["loc"] else None
                if isinstance(key, str) and key in fields and fields[key].is_required():
                    log.warning(
                        'Ignoring %s: invalid "%s" (%s)', context, key, err["msg"]
                    )
                    return fallback
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
    """Parse the optional `plugins` array: bare names or `[[plugins]]`
    tables with `name` and `options`. Malformed or duplicate entries are
    warned about and skipped.
    """
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


def is_safe_segment(name: str) -> bool:
    """Whether `name` is one path segment, usable as a filename stem or folder
    name: non-empty, no separator, no `.`/`..`.
    """
    return (
        name not in ("", ".", "..")
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def safe_segment(name: str, what: str) -> str:
    """`name` stripped, or `ValueError("Invalid <what>: ...")` unless that is
    a safe path segment (`is_safe_segment`).
    """
    clean = (name or "").strip()
    if not is_safe_segment(clean):
        raise ValueError(f"Invalid {what}: {name!r}")
    return clean


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
                'Ignoring the %dth entry of "cameras" as its "serial_number" is not a '
                "scalar",
                index,
            )
            continue
        if serial_number in used_serial_numbers:
            log.warning(
                'Ignoring the %dth entry of "cameras" as its "serial_number" is not '
                "unique",
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
        if camera.name and not is_safe_segment(camera.name):
            log.warning(
                'Ignoring unsafe "name" %r in the %dth entry of "cameras"; '
                "falling back to the serial number",
                camera.name,
                index,
            )
            camera.name = ""
        # A blank name records under the serial, so names and serials must not
        # collide either.
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


def _parse_section[ModelT: BaseModel, DefaultT](
    data: dict, key: str, model_cls: type[ModelT], default: DefaultT
) -> ModelT | DefaultT:
    """Parse the optional table `[key]` leniently; `default` when it is absent
    or not a table.
    """
    src = data.get(key)
    if src is None:
        return default
    if not isinstance(src, dict):
        log.warning('Ignoring "%s" in octacam config as it is not a table', key)
        return default
    return _lenient_validate(model_cls, src, key, model_cls())


class ConfigError(Exception):
    """An octacam_config.toml that exists but cannot be parsed at all."""


# ---------------------------------------------------------------------------
# Command-line overrides (`--set KEY=VALUE`)
# ---------------------------------------------------------------------------

_OVERRIDE = re.compile(r"([A-Za-z_][\w.]*)=(.*)", re.DOTALL)


def apply_overrides(config: OctacamConfig, overrides: Sequence[str]) -> OctacamConfig:
    """A copy of `config` with `KEY=VALUE` overrides applied, validated as a whole.

    A key is a dotted path into the config: `record.fps`, `backend`. A value is
    read as TOML (`80`, `true`, `[0, 180, 0]`); a list may drop its brackets
    (`0,180,0`), `none` restores a key's default, and a key that takes only text
    takes the value verbatim. Unlike the file, the command line is strict: an
    unknown key or a bad value raises `ConfigError`, naming it.
    """
    data = config.model_dump()
    for item in overrides:
        match = _OVERRIDE.fullmatch(item)
        if match is None:
            raise ConfigError(f"{item!r} is not KEY=VALUE")
        key, raw = match.groups()
        field = _override_field(key)
        *tables, name = key.split(".")
        owner = data
        for table in tables:
            if owner.get(table) is None:  # an optional table the file leaves out
                owner[table] = {}
            owner = owner[table]
        owner[name] = _override_value(field, raw)
    try:
        updated = OctacamConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(problems) from None
    if updated.backend.lower() not in _BACKENDS:
        raise ConfigError(f"backend: {updated.backend!r} is not one of {_BACKENDS}")
    updated.backend = updated.backend.lower()
    return updated


def _bare_type(annotation: object) -> object:
    """`annotation` without `Annotated` metadata or `| None`."""
    if typing.get_origin(annotation) is Annotated:
        return _bare_type(typing.get_args(annotation)[0])
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is (
        typing.Union
    ):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _bare_type(args[0])
    return annotation


def _override_keys(
    model: type[BaseModel] = OctacamConfig, prefix: str = ""
) -> list[str]:
    """Every dotted key `--set` can address, for naming the one a typo meant."""
    keys = []
    for name, field in model.model_fields.items():
        bare = _bare_type(field.annotation)
        if isinstance(bare, type) and issubclass(bare, BaseModel):
            keys += _override_keys(bare, f"{prefix}{name}.")
        else:
            keys.append(f"{prefix}{name}")
    return keys


def _is_table(annotation: object) -> typing.TypeGuard[type[BaseModel]]:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _override_field(key: str) -> FieldInfo:
    """The field a dotted key names; `ConfigError` naming the closest key if none."""
    parts = key.split(".")
    fields: dict[str, FieldInfo] = OctacamConfig.model_fields
    for depth, part in enumerate(parts, start=1):
        field = fields.get(part)
        if field is None:
            close = difflib.get_close_matches(key, _override_keys(), n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise ConfigError(f"unknown key {key!r}{hint}")
        model = _bare_type(field.annotation)
        if depth == len(parts):
            if _is_table(model):
                raise ConfigError(f"{key!r} is a table: set one of its keys")
            return field
        fields = model.model_fields if _is_table(model) else {}
    raise AssertionError("a key has at least one part")


def _override_value(field: FieldInfo, raw: str) -> object:
    """An override's value, typed by its field: see `apply_overrides`."""
    if raw.strip().lower() in ("none", "null"):
        return field.get_default(call_default_factory=True)
    bare = _bare_type(field.annotation)
    if bare is str or typing.get_origin(bare) is Literal:
        return raw
    for text in (raw, f"[{raw}]"):
        try:
            return tomllib.loads(f"v = {text}")["v"]
        except tomllib.TOMLDecodeError:
            pass
    return raw


def parse_record_section(data: dict) -> RecordConfig:
    """The `[record]` section of a raw parsed config, as `parse_config`
    reads it (invalid fields fall back to their defaults).
    """
    return _parse_section(data, "record", RecordConfig, RecordConfig())


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
        raise ConfigError(f"{file_path}: {e}") from e

    config.backend = _parse_backend(data.get("backend"))
    config.record = parse_record_section(data)
    config.transcode = _parse_section(
        data, "transcode", TranscodeConfig, TranscodeConfig()
    )
    config.gui = _parse_section(data, "gui", GuiConfig, GuiConfig())
    config.visualization = _parse_visualization(data.get("visualization"))
    config.transfer = _parse_section(data, "transfer", TransferConfig, None)

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


def resolve_config_dir(config_dir: str | Path) -> Path:
    """The config directory *config_dir* names, allowing a recording folder.

    A recording's `octacam_recording` subfolder is a config directory, used
    when the folder has no config of its own, or only an older flat take's
    snapshot (a flat summary beside it) the subfolder superseded
    (`octacam.recording_format.recording_info_dir`). A rig config directory that
    was recorded into has no flat summary and keeps its own config.
    """
    config_dir = Path(config_dir)
    nested = config_dir / RECORDING_INFO_DIRNAME
    if not find_config_file(nested).exists():
        return config_dir
    if not find_config_file(config_dir).exists():
        return nested
    flat_take = (config_dir / RECORDING_SUMMARY_FILENAME).is_file()
    if flat_take and recording_info_dir(config_dir) == nested:
        return nested
    return config_dir


def load_config_dir(config_dir: str | Path) -> OctacamConfig:
    return parse_config(find_config_file(config_dir))
