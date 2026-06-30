import logging
import time
from pathlib import Path

from octacam.config import GuiConfig, load_config_dir, parse_config

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


def test_gui_recording_output_defaults():
    gui = GuiConfig()
    assert gui.record_form_default == "display"
    assert gui.save_frame_timestamps_default is False


def test_parses_emulate_8_cameras_config():
    config = load_config_dir(REPO_ROOT / "configs" / "emulate_8_cameras")
    assert config.gui.fps_default == 100.0
    assert config.gui.duration_default == 5.0
    assert len(config.cameras) == 8
    assert config.cameras[0].serial_number == "0815-0000"
    assert config.cameras[0].name == "camera_LF"
    assert config.cameras[7].window_height == 0.6666667
    # save_directory_default contains strftime codes that must be expanded
    assert "%y" not in config.gui.save_directory_default
    assert time.strftime("%y%m%d") in config.gui.save_directory_default


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


def test_date_save_directory_coerced(tmp_path):
    # An unquoted date parses as a TOML date; it must be read as a string and
    # not silently fall back to "./".
    (tmp_path / "octacam_config.toml").write_text(
        "[gui]\nsave_directory_default = 2024-06-11\n"
    )
    assert load_config_dir(tmp_path).gui.save_directory_default == "2024-06-11"


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
        "[gui]\n"
        'fps_default = "not-a-number"\n'
        "dock_min_width = 1.5\n"
        "[[cameras]]\n"
        'serial_number = "a"\n'
        'scale_x = "wat"\n'
    )
    config = load_config_dir(tmp_path)
    assert config.gui.fps_default == 100.0
    assert config.gui.dock_min_width == 200
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
    (tmp_path / "octacam_config.toml").write_text("[gui]\nfps_default = 42\n")
    assert load_config_dir(tmp_path).gui.fps_default == 42.0


def test_encoder_defaults_parsed(tmp_path):
    # Capture defaults to CRF 18 and can be overridden in [gui]; x264_params
    # is a free-form ffmpeg -x264-params string.
    assert GuiConfig().crf_default == 18
    (tmp_path / "octacam_config.toml").write_text(
        "[gui]\n"
        "crf_default = 23\n"
        'preset_default = "veryfast"\n'
        'pix_fmt_default = "yuv420p"\n'
        'x264_params_default = "keyint=30:scenecut=0"\n'
    )
    gui = load_config_dir(tmp_path).gui
    assert gui.crf_default == 23
    assert gui.preset_default == "veryfast"
    assert gui.pix_fmt_default == "yuv420p"
    assert gui.x264_params_default == "keyint=30:scenecut=0"


def test_bad_crf_default_falls_back(tmp_path):
    # A non-integer crf_default is dropped (warn) and the default applies,
    # like the other tolerant [gui] fields.
    (tmp_path / "octacam_config.toml").write_text('[gui]\ncrf_default = "lossless"\n')
    assert load_config_dir(tmp_path).gui.crf_default == 18


def test_backend_defaults_to_basler(tmp_path):
    (tmp_path / "octacam_config.toml").write_text("[gui]\nfps_default = 1\n")
    assert load_config_dir(tmp_path).backend == "basler"


def test_backend_parsed_when_present(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        'backend = "flir"\n[[cameras]]\nserial_number = "a"\n'
    )
    config = load_config_dir(tmp_path)
    assert config.backend == "flir"
    assert [c.serial_number for c in config.cameras] == ["a"]


def test_unknown_backend_falls_back_to_basler(tmp_path):
    (tmp_path / "octacam_config.toml").write_text('backend = "nikon"\n')
    assert load_config_dir(tmp_path).backend == "basler"


def test_nas_verify_defaults_true(tmp_path):
    (tmp_path / "octacam_config.toml").write_text('[nas]\npath = "/mnt/nas"\n')
    cfg = load_config_dir(tmp_path)
    assert cfg.nas is not None and cfg.nas.verify is True


def test_nas_verify_parsed(tmp_path):
    (tmp_path / "octacam_config.toml").write_text("[nas]\nverify = false\n")
    assert load_config_dir(tmp_path).nas.verify is False


def test_grid_layout_unknown_camera_warns(tmp_path):
    # A layout cell naming a camera that isn't declared must be reported, not
    # silently rendered black.
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\nname = "camera_LF"\n'
        '[[cameras]]\nserial_number = "b"\nname = "camera_RF"\n'
        '[grid]\nlayout = [["camera_LF", "camera_TYPO"]]\n'
    )
    with _LogCapture() as cap:
        load_config_dir(tmp_path)
    assert any("camera_TYPO" in m for m in cap.messages)


def test_grid_layout_known_cameras_no_unknown_warning(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\nname = "camera_LF"\n'
        '[grid]\nlayout = [["camera_LF", ""]]\n'
    )
    with _LogCapture() as cap:
        load_config_dir(tmp_path)
    assert not any("unknown camera" in m for m in cap.messages)
