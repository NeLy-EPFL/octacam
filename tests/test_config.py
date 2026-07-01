import logging
import time
from pathlib import Path

from octacam.config import (
    GuiConfig,
    RecordConfig,
    duration_to_seconds,
    load_config_dir,
    parse_config,
    resolve_save_dir,
)
from octacam.writer import DEFAULT_FFMPEG_PARAMS

REPO_ROOT = Path(__file__).parent.parent


class _LogCapture(logging.Handler):
    """Capture ``octacam`` logger messages directly.

    Attached to the logger itself rather than via ``caplog`` because another
    test may leave ``propagate = False``, which would empty caplog's capture.
    """

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def __enter__(self):
        logger = logging.getLogger("octacam")
        self._prev = logger.level
        logger.addHandler(self)
        logger.setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc):
        logger = logging.getLogger("octacam")
        logger.removeHandler(self)
        logger.setLevel(self._prev)


def test_missing_file_returns_defaults(tmp_path):
    config = parse_config(tmp_path / "nope.toml")
    assert config.cameras == []
    assert config.gui == GuiConfig()


def test_record_defaults():
    record = RecordConfig()
    assert record.save_transformed is True
    assert record.save_timestamps is False
    assert record.trigger_source == "software"


def test_parses_emulate_8_cameras_config():
    config = load_config_dir(REPO_ROOT / "configs" / "emulate_8_cameras")
    assert config.record.fps == 30.0
    assert config.record.duration == 1.0
    assert len(config.cameras) == 8
    assert config.cameras[0].serial_number == "0815-0000"
    assert config.cameras[0].name == "camera_LF"
    assert config.cameras[7].window_height == 0.666667
    # directory/relative_directory carry strftime codes that are only expanded
    # at record time via resolve_save_dir, not at parse time.
    assert "%y" in config.record.relative_directory
    save_dir = resolve_save_dir(config.record, when=time.localtime(0))
    assert "%y" not in save_dir
    assert time.strftime("%y%m%d", time.localtime(0)) in save_dir


def test_duplicate_serial_skipped(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\n'
        '[[cameras]]\nserial_number = "a"\n'
        '[[cameras]]\nserial_number = "b"\n'
    )
    config = load_config_dir(tmp_path)
    assert [c.serial_number for c in config.cameras] == ["a", "b"]


def test_duplicate_name_skipped(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\nname = "x"\n'
        '[[cameras]]\nserial_number = "b"\nname = "x"\n'
    )
    config = load_config_dir(tmp_path)
    assert [c.serial_number for c in config.cameras] == ["a"]


def test_unsafe_camera_name_dropped(tmp_path):
    # A name becomes a video filename stem; a traversal/separator name must not
    # survive the load (it would write outside the save dir). The camera is
    # kept but its name is cleared so it falls back to the serial.
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\nname = "../evil"\n'
        '[[cameras]]\nserial_number = "b"\nname = "ok"\n'
    )
    config = load_config_dir(tmp_path)
    assert [c.serial_number for c in config.cameras] == ["a", "b"]
    assert config.cameras[0].name == ""
    assert config.cameras[1].name == "ok"


def test_integer_serial_and_name_coerced_to_string(tmp_path):
    # TOML keeps types explicit, but an unquoted integer serial/name should
    # still be read as text rather than rejected.
    (tmp_path / "octacam_config.toml").write_text(
        "[[cameras]]\nserial_number = 40029805\nname = 7\n"
    )
    config = load_config_dir(tmp_path)
    assert config.cameras[0].serial_number == "40029805"
    assert config.cameras[0].name == "7"


def test_date_directory_coerced(tmp_path):
    # An unquoted date parses as a TOML date; it must be read as a string and
    # not silently fall back to the default. Templating happens at record time,
    # so the raw value round-trips verbatim (unexpanded) at parse time.
    (tmp_path / "octacam_config.toml").write_text("[record]\ndirectory = 2024-06-11\n")
    assert load_config_dir(tmp_path).record.directory == "2024-06-11"


def test_non_scalar_serial_skipped(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        "[[cameras]]\nserial_number = [1, 2]\n"
        "[[cameras]]\nserial_number = true\n"
        '[[cameras]]\nserial_number = "40"\n'
    )
    config = load_config_dir(tmp_path)
    assert [c.serial_number for c in config.cameras] == ["40"]


def test_bad_types_keep_defaults(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        "[record]\n"
        'fps = "not-a-number"\n'
        "[gui]\n"
        "display_refresh_interval_ms = 1.5\n"
        "[[cameras]]\n"
        'serial_number = "a"\n'
        'scale_x = "wat"\n'
    )
    config = load_config_dir(tmp_path)
    assert config.record.fps == 100.0
    assert config.gui.display_refresh_interval_ms == 33
    assert config.cameras[0].scale_x == 1.0


def test_plugins_bare_name_list(tmp_path):
    (tmp_path / "octacam_config.toml").write_text('plugins = ["flywheel", "other"]\n')
    plugins = load_config_dir(tmp_path).plugins
    assert [p.name for p in plugins] == ["flywheel", "other"]
    assert plugins[0].options == {}


def test_plugins_tables_with_options_and_duplicates(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[[plugins]]\nname = "flywheel"\n'
        '[[plugins]]\nname = "flywheel"\n'  # duplicate -> skipped
        '[[plugins]]\nname = "other"\noptions = {device = "/dev/ttyACM0"}\n'
    )
    plugins = load_config_dir(tmp_path).plugins
    assert [p.name for p in plugins] == ["flywheel", "other"]
    assert plugins[1].options == {"device": "/dev/ttyACM0"}


def test_toml_file_loaded(tmp_path):
    (tmp_path / "octacam_config.toml").write_text("[record]\nfps = 42\n")
    assert load_config_dir(tmp_path).record.fps == 42.0


def test_encoder_defaults_parsed(tmp_path):
    # The capture encoder args default to DEFAULT_FFMPEG_PARAMS and can be
    # overridden as a verbatim ffmpeg arg string in [record].
    assert RecordConfig().ffmpeg_params == DEFAULT_FFMPEG_PARAMS
    (tmp_path / "octacam_config.toml").write_text(
        "[record]\n"
        'ffmpeg_params = "-c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p"\n'
    )
    record = load_config_dir(tmp_path).record
    assert (
        record.ffmpeg_params == "-c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p"
    )
    import shlex

    assert shlex.split(record.ffmpeg_params) == [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
    ]


def test_bad_ffmpeg_params_falls_back(tmp_path):
    # An ffmpeg_params string ffmpeg could never parse (unbalanced quote) is
    # dropped (warn) and the default applies, like the other tolerant fields.
    (tmp_path / "octacam_config.toml").write_text(
        '[record]\nffmpeg_params = "-vf \\"unterminated"\n'
    )
    assert load_config_dir(tmp_path).record.ffmpeg_params == DEFAULT_FFMPEG_PARAMS


def test_duration_to_seconds():
    assert duration_to_seconds(5.0, "seconds", 100.0) == 5.0
    assert duration_to_seconds(2.0, "minutes", 100.0) == 120.0
    assert duration_to_seconds(1.0, "hours", 100.0) == 3600.0
    # "frames" is a frame count at the recording rate -> divided by fps.
    assert duration_to_seconds(200.0, "frames", 100.0) == 2.0


def test_backend_defaults_to_auto(tmp_path):
    # An absent backend key means auto-detect every installed vendor, so a rig
    # can mix Basler and FLIR and just use whatever is connected.
    (tmp_path / "octacam_config.toml").write_text("[record]\nfps = 1\n")
    assert load_config_dir(tmp_path).backend == "auto"


def test_backend_parsed_when_present(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        'backend = "flir"\n[[cameras]]\nserial_number = "a"\n'
    )
    config = load_config_dir(tmp_path)
    assert config.backend == "flir"
    assert [c.serial_number for c in config.cameras] == ["a"]


def test_unknown_backend_falls_back_to_auto(tmp_path):
    (tmp_path / "octacam_config.toml").write_text('backend = "nikon"\n')
    assert load_config_dir(tmp_path).backend == "auto"


def test_transfer_checksum_defaults_true(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[transfer]\ndirectory = "/mnt/store"\n'
    )
    cfg = load_config_dir(tmp_path)
    assert cfg.transfer is not None and cfg.transfer.checksum is True


def test_transfer_checksum_parsed(tmp_path):
    (tmp_path / "octacam_config.toml").write_text("[transfer]\nchecksum = false\n")
    assert load_config_dir(tmp_path).transfer.checksum is False


def test_visualization_layout_unknown_camera_warns(tmp_path):
    # A layout cell naming a camera that isn't declared must be reported, not
    # silently rendered black.
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\nname = "camera_LF"\n'
        '[[cameras]]\nserial_number = "b"\nname = "camera_RF"\n'
        '[[visualization]]\nlayout = [["camera_LF", "camera_TYPO"]]\n'
    )
    with _LogCapture() as cap:
        load_config_dir(tmp_path)
    assert any("camera_TYPO" in m for m in cap.messages)


def test_visualization_layout_known_cameras_no_unknown_warning(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\nname = "camera_LF"\n'
        '[[visualization]]\nlayout = [["camera_LF", ""]]\n'
    )
    with _LogCapture() as cap:
        load_config_dir(tmp_path)
    assert not any("unknown camera" in m for m in cap.messages)
