"""octacam_config.yaml parsing. Port of cpp/src/parser.{hpp,cpp}."""

import datetime
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("octacam")


@dataclass
class CameraConfig:
    serial_number: str
    name: str = ""
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0
    window_x: float = -1.0
    window_y: float = -1.0
    window_width: float = -1.0
    window_height: float = -1.0


@dataclass
class GuiConfig:
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

    display_refresh_interval_ms: int = 33
    record_countdown_timer_interval_ms: int = 1000
    check_record_started_timer_interval_ms: int = 100

    dock_min_width: int = 200
    dock_max_width: int = 300
    save_dir_edit_height_factor: int = 4


@dataclass
class OctacamConfig:
    gui: GuiConfig = field(default_factory=GuiConfig)
    cameras: list[CameraConfig] = field(default_factory=list)


def _as_float(value):
    if isinstance(value, bool):
        raise ValueError
    return float(value)


def _as_int(value):
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, float) and not value.is_integer():
        raise ValueError
    return int(value)


def _as_scalar_str(value):
    # yaml-cpp's as<std::string>() returns any scalar's text, so the C++
    # version read an unquoted serial number (parsed by PyYAML as an int)
    # or a date-like save directory (parsed as a date) as a string.
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, (int, float, datetime.date)):
        return str(value)
    raise ValueError


def _set_if_valid(src, key, cast, target, type_name):
    if not isinstance(src, dict) or key not in src:
        return
    try:
        value = cast(src[key])
    except (TypeError, ValueError):
        log.warning('"%s" is not of type %s in the config file', key, type_name)
        return
    setattr(target, key, value)


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
        file = yaml.safe_load(file_path.read_text())
    except yaml.YAMLError as e:
        log.error("Failed to parse octacam config file: %s", e)
        return _finalize(config)
    if not isinstance(file, dict):
        return _finalize(config)

    gui_src = file.get("gui")
    if gui_src is not None:
        if not isinstance(gui_src, dict):
            log.warning('Ignoring "gui" in octacam config as it is not a map')
        else:
            gui = config.gui
            for key in (
                "fps_default",
                "fps_min",
                "fps_max",
                "duration_default",
                "duration_min",
                "duration_max",
            ):
                _set_if_valid(gui_src, key, _as_float, gui, "double")
            _set_if_valid(
                gui_src, "save_directory_default", _as_scalar_str, gui, "string"
            )
            for key in (
                "duration_unit_default_index",
                "trigger_source_default_index",
                "video_writer_default_index",
                "display_refresh_interval_ms",
                "record_countdown_timer_interval_ms",
                "check_record_started_timer_interval_ms",
                "dock_min_width",
                "dock_max_width",
                "save_dir_edit_height_factor",
            ):
                _set_if_valid(gui_src, key, _as_int, gui, "int")

    cameras_src = file.get("cameras")
    if cameras_src is None:
        return _finalize(config)
    if not isinstance(cameras_src, list):
        log.warning('Ignoring "cameras" in octacam config as it is not a sequence')
        return _finalize(config)

    used_serial_numbers = set()
    used_names = set()

    for index, src in enumerate(cameras_src):
        if not isinstance(src, dict) or "serial_number" not in src:
            log.warning(
                'Ignoring the %dth entry of "cameras" as its "serial_number" '
                "is absent",
                index,
            )
            continue
        try:
            serial_number = _as_scalar_str(src["serial_number"])
        except ValueError:
            log.warning(
                'Ignoring the %dth entry of "cameras" as its "serial_number" '
                "is not a string",
                index,
            )
            continue
        if serial_number in used_serial_numbers:
            log.warning(
                'Ignoring the %dth entry of "cameras" as its "serial_number" '
                "is not unique",
                index,
            )
            continue
        used_serial_numbers.add(serial_number)

        camera = CameraConfig(serial_number=serial_number)
        if "name" in src:
            try:
                camera.name = _as_scalar_str(src["name"])
            except ValueError:
                log.warning(
                    'Ignoring "name" for camera %s as it is not a string',
                    serial_number,
                )
        if camera.name:
            if camera.name in used_names:
                log.warning(
                    'Ignoring the %dth entry of "cameras" as its "name" is '
                    "not unique",
                    index,
                )
                continue
            used_names.add(camera.name)

        for key in (
            "scale_x",
            "scale_y",
            "rotation_deg",
            "window_x",
            "window_y",
            "window_width",
            "window_height",
        ):
            _set_if_valid(src, key, _as_float, camera, "double")

        config.cameras.append(camera)

    if not config.cameras:
        log.info(
            "No cameras found in octacam config file. All detected cameras "
            "will be used."
        )
        return _finalize(config)

    log.info("Found %d camera(s) in octacam config file", len(config.cameras))
    return _finalize(config)


def find_config_file(config_dir: str | Path) -> Path:
    """Mirrors main.cpp: prefer octacam_config.yml, fall back to .yaml."""
    config_dir = Path(config_dir)
    config_path = config_dir / "octacam_config.yml"
    if not config_path.exists():
        config_path = config_dir / "octacam_config.yaml"
    return config_path


def load_config_dir(config_dir: str | Path) -> OctacamConfig:
    return parse_config(find_config_file(config_dir))
