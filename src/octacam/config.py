"""octacam_config.toml parsing.

Tolerant per field: a malformed section or field is warned about and falls back
to its default (``_lenient_validate``). A file that does not parse at all raises
:class:`ConfigError`, since stock defaults would silently run the wrong rig.
"""

import datetime
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Annotated, Literal, NamedTuple, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    ValidationError,
    field_validator,
)
from pydantic_core import PydanticCustomError

from octacam._compat import tomllib
from octacam.transform import (
    RECORDING_INFO_DIRNAME,
    RECORDING_SUMMARY_FILENAME,
    recording_info_dir,
)
from octacam.writer import (
    DEFAULT_FFMPEG_PARAMS,
    DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    NVENC_H264_PARAMS,
)

log = logging.getLogger("octacam")

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_DefaultT = TypeVar("_DefaultT")


def _scalar_str(value: object) -> str:
    """Coerce a TOML scalar to a string, rejecting bool/array/table: an unquoted
    serial number or date-like save directory parses as an int or a date."""
    if isinstance(value, bool):
        raise ValueError("expected a string, got a boolean")
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
TriggerSource = Literal["software", "managed", "external"]
# "auto" mirrors trigger_source (RecordingController._effective_preview_mode).
PreviewTriggerSource = Literal["auto", "software", "free_running"]
# "ffmpeg" = CPU (libx264); "nvenc" = NVIDIA GPU, cameras beyond
# max_nvenc_sessions on CPU; "raw" = Mono8 dump, transcoded later.
SaveMethod = Literal["ffmpeg", "raw", "nvenc"]
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
    """The ``[record]`` section: how and where recordings are captured.

    ``directory``/``relative_directory`` are path templates resolved at record
    start: they accept strftime ``%``-codes (see :func:`resolve_save_path`).
    ``ffmpeg_params`` is the verbatim encoder arg string used when
    ``save_method == "ffmpeg"``.
    """

    fps: float = 100.0
    duration: float = 5.0
    duration_unit: Literal["frames", "seconds", "minutes", "hours"] = "seconds"
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
    """The ``[transcode]`` section: encoder args for `octacam process`."""

    ffmpeg_params: _ScalarFfmpegArgs = DEFAULT_TRANSCODE_FFMPEG_PARAMS


class VisualizationConfig(BaseModel):
    """One ``[[visualization]]`` entry: a composite grid video ``octacam
    process`` builds in each recording folder.

    ``layout`` is a 2D list of camera names, rows of equal length, ``""`` a black
    cell. An empty ``ffmpeg_params`` uses ``[transcode].ffmpeg_params``.
    For example, a 3×3 grid on an 8-camera rig::

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
    ffmpeg_params: ScalarStr = ""

    @field_validator("layout")
    @classmethod
    def _rectangular(cls, layout: list[list[str]]) -> list[list[str]]:
        if not layout:
            raise ValueError("the layout is empty")
        if len({len(row) for row in layout}) > 1:
            raise ValueError("the layout's rows differ in length")
        return layout


class TransferConfig(BaseModel):
    """The ``[transfer]`` section: where `octacam process` mirrors recordings.

    A recording goes to ``directory``/<its summary's ``relative_directory``>;
    ``directory`` takes strftime ``%``-codes like ``record.directory``.
    ``checksum`` verifies each copy's content (false: its size only).
    """

    directory: ScalarStr = ""
    checksum: bool = True


class GuiConfig(BaseModel):
    """The ``[gui]`` section: pure web-UI render settings (rig-tunable)."""

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
    """A record ``duration`` in its ``unit`` (``"frames"`` at ``fps``) as seconds."""
    if unit == "frames":
        return duration / fps if fps > 0 else 0.0
    return duration * _DURATION_UNIT_SECONDS.get(unit, 1.0)


def _apply_template(text: str, when: time.struct_time) -> str:
    """Expand strftime ``%``-codes in a path template; a bad code is warned
    about and the text kept."""
    try:
        return time.strftime(text, when)
    except ValueError as e:
        log.warning("Could not expand strftime codes in path template %r: %s", text, e)
        return text


def normalize_dir(text: str) -> str:
    """Strip, expand ``~``, make absolute, use forward slashes."""
    return str(Path(text.strip()).expanduser().absolute()).replace("\\", "/")


def compose_save_dir(base: str, relative: str) -> str:
    """``base``/``relative``, normalized; an absolute ``relative`` discards the
    base."""
    return normalize_dir(os.path.join(base, relative) if relative else base)


def increment_trailing_number(text: str) -> str:
    """Increment the last 3-digit group: 001-bhv -> 002-bhv (else unchanged)."""
    matches = list(_TRAILING_NUMBER_RE.finditer(text))
    if not matches:
        return text
    last = matches[-1]
    return f"{text[: last.start()]}{int(last.group()) + 1:03d}{text[last.end() :]}"


def resolve_dir_template(template: str, when: time.struct_time | None = None) -> str:
    """Resolve a directory template (strftime ``%``-codes) to an absolute path."""
    return normalize_dir(_apply_template(template, when or time.localtime()))


class SavePath(NamedTuple):
    """Where a recording goes: ``save_dir`` is ``directory``/``relative``."""

    save_dir: str
    directory: str
    # Kept relative: the transfer mirrors it under its destination.
    relative: str


def resolve_save_path(
    record: RecordConfig, when: time.struct_time | None = None
) -> SavePath:
    """``record.directory``/``relative_directory``, both expanded at the one
    moment ``when``."""
    when = when or time.localtime()
    base = _apply_template(record.directory, when)
    relative = _apply_template(record.relative_directory, when)
    return SavePath(compose_save_dir(base, relative), normalize_dir(base), relative)


def _parse_visualization(src: object) -> list[VisualizationConfig]:
    """Parse the optional ``[[visualization]]`` array.

    An entry without a valid layout, or reusing an earlier entry's output name,
    is warned about and skipped; another invalid field keeps its default."""
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
    Without a camera list, the names are not known here."""
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
    """Parse the optional top-level ``backend`` key (else ``"auto"``)."""
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
    model_cls: type[_ModelT], data: dict, context: str, fallback: _DefaultT
) -> _ModelT | _DefaultT:
    """Validate ``data`` against ``model_cls``, warning about and dropping each
    invalid field so its default applies.

    ``fallback`` when that cannot help: a required field is missing or invalid,
    or an error names no field."""
    data = dict(data)
    fields = model_cls.model_fields
    while True:
        try:
            return model_cls.model_validate(data)
        except ValidationError as exc:
            for err in exc.errors():
                key = err["loc"][0] if err["loc"] else None
                if isinstance(key, str) and key in fields and fields[key].is_required():
                    log.warning('Ignoring %s: invalid "%s" (%s)', context, key, err["msg"])
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
    """Parse the optional ``plugins`` array: bare names or ``[[plugins]]``
    tables with ``name`` and ``options``. Malformed or duplicate entries are
    warned about and skipped."""
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
    """Whether ``name`` is one path segment, usable as a filename stem or folder
    name: non-blank, no separator, no ``.``/``..``."""
    return (
        name not in ("", ".", "..")
        and "/" not in name
        and "\\" not in name
        and Path(name).name == name
    )


def safe_segment(name: str, what: str) -> str:
    """``name`` stripped, or ``ValueError("Invalid <what>: ...")`` unless that is
    a safe path segment (:func:`is_safe_segment`)."""
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


def _parse_section(
    data: dict, key: str, model_cls: type[_ModelT], default: _DefaultT
) -> _ModelT | _DefaultT:
    """Parse the optional table ``[key]`` leniently; ``default`` when it is absent
    or not a table."""
    src = data.get(key)
    if src is None:
        return default
    if not isinstance(src, dict):
        log.warning('Ignoring "%s" in octacam config as it is not a table', key)
        return default
    return _lenient_validate(model_cls, src, key, model_cls())


class ConfigError(Exception):
    """An octacam_config.toml that exists but cannot be parsed at all."""


def parse_record_section(data: dict) -> RecordConfig:
    """The ``[record]`` section of a raw parsed config, as :func:`parse_config`
    reads it (invalid fields fall back to their defaults)."""
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

    A recording's ``octacam_recording`` subfolder is a config directory, used
    when the folder has no config of its own, or only an older flat take's
    snapshot (a flat summary beside it) the subfolder superseded
    (:func:`octacam.transform.recording_info_dir`). A rig config directory that
    was recorded into has no flat summary and keeps its own config."""
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
