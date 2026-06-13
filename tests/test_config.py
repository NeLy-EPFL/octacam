import time
from pathlib import Path

from octacam.config import GuiConfig, load_config_dir, parse_config

REPO_ROOT = Path(__file__).parent.parent


def test_missing_file_returns_defaults(tmp_path):
    config = parse_config(tmp_path / "nope.toml")
    assert config.cameras == []
    assert config.gui == GuiConfig()


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
    (tmp_path / "octacam_config.toml").write_text('plugins = ["arduino", "other"]\n')
    plugins = load_config_dir(tmp_path).plugins
    assert [p.name for p in plugins] == ["arduino", "other"]
    assert plugins[0].options == {}


def test_plugins_tables_with_options_and_duplicates(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[[plugins]]\nname = "arduino"\n'
        '[[plugins]]\nname = "arduino"\n'  # duplicate -> skipped
        '[[plugins]]\nname = "other"\noptions = {device = "/dev/ttyACM0"}\n'
    )
    plugins = load_config_dir(tmp_path).plugins
    assert [p.name for p in plugins] == ["arduino", "other"]
    assert plugins[1].options == {"device": "/dev/ttyACM0"}


def test_toml_file_loaded(tmp_path):
    (tmp_path / "octacam_config.toml").write_text("[gui]\nfps_default = 42\n")
    assert load_config_dir(tmp_path).gui.fps_default == 42.0
