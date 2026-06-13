"""TOML config writer: round-trip fidelity, strftime safety, atomic writes."""

import glob
import tomllib
from pathlib import Path

import pytest

from octacam import config_writer as cw
from octacam.config import parse_config

PRESETS = sorted(glob.glob("configs/*/octacam_config.toml"))


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: Path(p).parent.name)
def test_roundtrip_preset(preset, tmp_path):
    raw = tomllib.loads(Path(preset).read_text())
    doc = cw.merge_camera_display(raw, [])  # no-op patch
    cw.write_config(tmp_path, doc)
    written = (tmp_path / "octacam_config.toml").read_text()

    # raw [gui]/[[cameras]]/[[plugins]] survive verbatim...
    reparsed_raw = tomllib.loads(written)
    assert reparsed_raw.get("gui") == raw.get("gui")
    assert reparsed_raw.get("cameras") == raw.get("cameras")
    assert reparsed_raw.get("plugins") == raw.get("plugins")
    # ...and the model parses identically.
    assert (
        parse_config(tmp_path / "octacam_config.toml").model_dump()
        == parse_config(Path(preset)).model_dump()
    )


def test_save_directory_template_not_expanded(tmp_path):
    raw = {"gui": {"save_directory_default": "/data/%y%m%d_/Fly1/001"}, "cameras": []}
    doc = cw.merge_camera_display(raw, [{"serial": "X", "rotation_deg": 90.0}])
    cw.write_config(tmp_path, doc)
    text = (tmp_path / "octacam_config.toml").read_text()
    # the strftime template must be preserved literally, not date-expanded
    assert "%y%m%d_" in text


def test_merge_updates_existing_and_appends_new():
    raw = {"cameras": [{"serial_number": "A", "name": "camA", "rotation_deg": 0.0}]}
    doc = cw.merge_camera_display(
        raw,
        [
            {"serial": "A", "rotation_deg": 90.0, "scale_x": -1.0},
            {"serial": "B", "name": "camB", "window_x": 0.5},
        ],
    )
    by_serial = {str(c["serial_number"]): c for c in doc["cameras"]}
    assert by_serial["A"]["rotation_deg"] == 90.0
    assert by_serial["A"]["scale_x"] == -1.0
    assert by_serial["A"]["name"] == "camA"  # untouched fields preserved
    assert by_serial["B"]["name"] == "camB" and by_serial["B"]["window_x"] == 0.5


def test_dumps_escapes_strings():
    doc = {"gui": {"save_directory_default": 'a"b\\c'}}
    assert tomllib.loads(cw._dumps(doc))["gui"]["save_directory_default"] == 'a"b\\c'


def test_plugin_options_roundtrip():
    doc = {
        "plugins": [
            {"name": "arduino", "options": {"port": "/dev/ttyUSB0", "baud": 9600}}
        ]
    }
    assert tomllib.loads(cw._dumps(doc)) == doc


@pytest.mark.parametrize("name", ["", "  ", ".", "..", "a/b", "a\\b", "/abs", "x/../y"])
def test_safe_config_name_rejects(name):
    with pytest.raises(ValueError):
        cw.safe_config_name(name)


def test_safe_config_name_accepts():
    assert cw.safe_config_name(" my_rig ") == "my_rig"


def test_resolve_new_config_dir_collision(tmp_path):
    (tmp_path / "active").mkdir()
    (tmp_path / "exists").mkdir()
    with pytest.raises(FileExistsError):
        cw.resolve_new_config_dir(tmp_path / "active", "exists")
    assert cw.resolve_new_config_dir(tmp_path / "active", "exists", overwrite=True)
    assert cw.resolve_new_config_dir(tmp_path / "active", "fresh") == tmp_path / "fresh"


def test_atomic_write_leaves_no_temp(tmp_path):
    cw.atomic_write_text(tmp_path / "octacam_config.toml", "[gui]\nfps_default = 1.0\n")
    assert (tmp_path / "octacam_config.toml").exists()
    assert not list(tmp_path.glob(".octacam-*"))


def test_read_pfs_files(tmp_path):
    (tmp_path / "0815-0000.pfs").write_text("live\n")
    (tmp_path / "fictrac_camera_config.pfs").write_text("aux\n")
    (tmp_path / "octacam_config.toml").write_text("[gui]\n")  # non-.pfs ignored
    out = cw.read_pfs_files(tmp_path)
    assert out == {"0815-0000": "live\n", "fictrac_camera_config": "aux\n"}
    # a missing directory yields an empty map rather than raising
    assert cw.read_pfs_files(tmp_path / "nope") == {}


def test_copy_auxiliary_pfs_skips_live_serials(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "0815-0000.pfs").write_text("live")
    (src / "fictrac_camera_config.pfs").write_text("aux")
    dst = tmp_path / "dst"
    dst.mkdir()
    cw.copy_auxiliary_pfs(src, dst, {"0815-0000"})
    assert (dst / "fictrac_camera_config.pfs").exists()
    assert not (dst / "0815-0000.pfs").exists()  # live serial written separately
