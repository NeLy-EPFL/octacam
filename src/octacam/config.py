"""octacam_config.toml parsing.

The loader is deliberately *tolerant*: a malformed file, section, or field is
warned about and falls back to the default rather than raising, so a rig's
config can never stop the app from starting. pydantic validates the types;
``_lenient_validate`` turns validation errors into warn-and-default.
"""

import datetime
import logging
import time
import tomllib
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from octacam.writer import (
    DEFAULT_CRF,
    DEFAULT_PIX_FMT,
    DEFAULT_PRESET,
    DEFAULT_X264_PARAMS,
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

    @field_validator("serial_number", "name", mode="before")
    @classmethod
    def _as_scalar_str(cls, value: object) -> str:
        return _scalar_str(value)


class GuiConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fps_default: float = 100.0
    fps_min: float = 0.01
    fps_max: float = 1000.0
    duration_default: float = 5.0
    duration_min: float = 0.01
    duration_max: float = 1_000_000.0
    duration_unit_default_index: int = 0
    save_directory_default: str = "./"
    trigger_source_default_index: int = 0
    video_writer_default_index: int = 0
    # Explicit codec key (x264/raw); when set it overrides the
    # positional video_writer_default_index, which is fragile across changes
    # to the writer list.
    video_writer_default: str = ""

    # libx264 capture defaults (used when the codec is x264). They seed the
    # initial RecordingSettings and the `octacam record` fallbacks; the web UI
    # and CLI can still override them per recording. x264_params is passed
    # verbatim to ffmpeg's -x264-params (e.g. "keyint=30:scenecut=0").
    crf_default: int = DEFAULT_CRF
    preset_default: str = DEFAULT_PRESET
    pix_fmt_default: str = DEFAULT_PIX_FMT
    x264_params_default: str = DEFAULT_X264_PARAMS

    # Recording outputs. By default each recording writes a compact
    # recording_summary.json and bakes the per-camera display transform
    # (rotation/flips) into the video ("display" form). save_frame_timestamps
    # turns the legacy per-frame timestamp CSV back on (debugging only);
    # record_form="sensor" keeps the raw, untransformed sensor image.
    save_frame_timestamps_default: bool = False
    record_form_default: str = "display"  # "display" | "sensor"

    display_refresh_interval_ms: int = 33
    record_countdown_timer_interval_ms: int = 1000
    check_record_started_timer_interval_ms: int = 100

    dock_min_width: int = 200
    dock_max_width: int = 300
    save_dir_edit_height_factor: int = 4

    @field_validator(
        "save_directory_default",
        "video_writer_default",
        "preset_default",
        "pix_fmt_default",
        "x264_params_default",
        "record_form_default",
        mode="before",
    )
    @classmethod
    def _as_scalar_str(cls, value: object) -> str:
        return _scalar_str(value)


class PluginConfig(BaseModel):
    name: str
    options: dict = Field(default_factory=dict)


_BACKENDS = ("basler", "flir", "fake")


class OctacamConfig(BaseModel):
    # Which camera SDK this rig uses. One vendor per config directory; absent
    # means Basler so every existing config keeps working untouched.
    backend: str = "basler"
    gui: GuiConfig = Field(default_factory=GuiConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)
    plugins: list[PluginConfig] = Field(default_factory=list)


def _parse_backend(value: object) -> str:
    """Parse the optional top-level ``backend`` key, tolerantly (defaults basler)."""
    if value is None:
        return "basler"
    try:
        name = _scalar_str(value).strip().lower()
    except ValueError:
        log.warning('Ignoring "backend" in octacam config as it is not a string')
        return "basler"
    if name not in _BACKENDS:
        log.warning(
            'Ignoring unknown "backend" %r in octacam config; using "basler"', name
        )
        return "basler"
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

    Each entry is either a bare name (``plugins = ["arduino"]``) or a table with
    ``name`` and optional ``options`` (``[[plugins]]`` / ``name = "arduino"`` +
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
        if camera.name:
            if camera.name in used_names:
                log.warning(
                    'Ignoring the %dth entry of "cameras" as its "name" is not unique',
                    index,
                )
                continue
            used_names.add(camera.name)
        cameras.append(camera)
    return cameras


def _finalize(config: OctacamConfig) -> OctacamConfig:
    # The save directory template may contain strftime codes (e.g. %y%m%d).
    try:
        config.gui.save_directory_default = time.strftime(
            config.gui.save_directory_default
        )
    except ValueError:
        pass
    return config


def parse_config(file_path: str | Path) -> OctacamConfig:
    config = OctacamConfig()
    file_path = Path(file_path)

    if not file_path.exists():
        log.info("octacam config file not found at %s.", file_path)
        log.info("All detected cameras will be used.")
        return _finalize(config)

    try:
        data = tomllib.loads(file_path.read_text())
    except tomllib.TOMLDecodeError as e:
        log.error("Failed to parse octacam config file: %s", e)
        return _finalize(config)

    config.backend = _parse_backend(data.get("backend"))

    gui_src = data.get("gui")
    if gui_src is not None:
        if not isinstance(gui_src, dict):
            log.warning('Ignoring "gui" in octacam config as it is not a table')
        else:
            config.gui = _lenient_validate(GuiConfig, gui_src, "gui", GuiConfig())

    # Parsed before the cameras block, which has several early returns.
    config.plugins = _parse_plugins(data.get("plugins"))

    cameras_src = data.get("cameras")
    if cameras_src is None:
        return _finalize(config)
    if not isinstance(cameras_src, list):
        log.warning('Ignoring "cameras" in octacam config as it is not an array')
        return _finalize(config)

    config.cameras = _parse_cameras(cameras_src)
    if not config.cameras:
        log.info(
            "No cameras found in octacam config file. All detected cameras "
            "will be used."
        )
        return _finalize(config)

    log.info("Found %d camera(s) in octacam config file", len(config.cameras))
    return _finalize(config)


def find_config_file(config_dir: str | Path) -> Path:
    return Path(config_dir) / "octacam_config.toml"


def load_config_dir(config_dir: str | Path) -> OctacamConfig:
    return parse_config(find_config_file(config_dir))
