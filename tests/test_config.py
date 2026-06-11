import time
from pathlib import Path

from octacam.config import GuiConfig, load_config_dir, parse_config

REPO_ROOT = Path(__file__).parent.parent


def test_missing_file_returns_defaults(tmp_path):
    config = parse_config(tmp_path / "nope.yaml")
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
    (tmp_path / "octacam_config.yaml").write_text(
        "cameras:\n"
        "  - serial_number: a\n"
        "  - serial_number: a\n"
        "  - serial_number: b\n"
    )
    config = load_config_dir(tmp_path)
    assert [c.serial_number for c in config.cameras] == ["a", "b"]


def test_duplicate_name_skipped(tmp_path):
    (tmp_path / "octacam_config.yaml").write_text(
        "cameras:\n"
        "  - {serial_number: a, name: x}\n"
        "  - {serial_number: b, name: x}\n"
    )
    config = load_config_dir(tmp_path)
    assert [c.serial_number for c in config.cameras] == ["a"]


def test_bad_types_keep_defaults(tmp_path):
    (tmp_path / "octacam_config.yaml").write_text(
        "gui:\n"
        "  fps_default: not-a-number\n"
        "  dock_min_width: 1.5\n"
        "cameras:\n"
        "  - {serial_number: a, scale_x: wat}\n"
    )
    config = load_config_dir(tmp_path)
    assert config.gui.fps_default == 100.0
    assert config.gui.dock_min_width == 200
    assert config.cameras[0].scale_x == 1.0


def test_yml_preferred_over_yaml(tmp_path):
    (tmp_path / "octacam_config.yml").write_text("gui: {fps_default: 42}\n")
    (tmp_path / "octacam_config.yaml").write_text("gui: {fps_default: 7}\n")
    assert load_config_dir(tmp_path).gui.fps_default == 42.0
