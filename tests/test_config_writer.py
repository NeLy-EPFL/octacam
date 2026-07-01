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
            {"name": "flywheel", "options": {"port": "/dev/ttyUSB0", "baud": 9600}}
        ]
    }
    assert tomllib.loads(cw._dumps(doc)) == doc


def test_visualization_and_transfer_sections_roundtrip():
    # A GUI save round-trips through _dumps; it must not wipe the
    # [[visualization]] (array-of-tables, incl. the nested layout list-of-lists)
    # or [transfer] post-processing sections.
    doc = {
        "visualization": [
            {
                "name": "grid.mp4",
                "layout": [
                    ["camera_LF", "", "camera_RF"],
                    ["camera_LM", "camera_F", ""],
                ],
            }
        ],
        "transfer": {"directory": "/mnt/store", "checksum": False},
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


def test_pfs_helpers_honor_a_non_pfs_extension(tmp_path):
    # The persistence generalization: a FLIR/fake backend persists per-camera
    # files under its own extension; the helpers must round-trip those too.
    cw.write_pfs_files(tmp_path, {"FAKE-0": "{}\n"}, extension="json")
    assert (tmp_path / "FAKE-0.json").exists()
    assert not (tmp_path / "FAKE-0.pfs").exists()
    assert cw.read_pfs_files(tmp_path, extension="json") == {"FAKE-0": "{}\n"}
    # the default extension stays "pfs" and ignores the json file
    assert cw.read_pfs_files(tmp_path) == {}


def test_pfs_helpers_handle_a_mixed_vendor_rig(tmp_path):
    # A Basler+FLIR rig writes each camera's params in its own format and reads
    # them all back: write_pfs_files takes a per-serial extension map, and
    # read_pfs_files / copy_auxiliary_pfs take the set of suffixes in play.
    ext_by_serial = {"BAS-1": "pfs", "FLIR-1": "json"}
    cw.write_pfs_files(tmp_path, {"BAS-1": "<pfs/>\n", "FLIR-1": "{}\n"}, ext_by_serial)
    assert (tmp_path / "BAS-1.pfs").exists()
    assert (tmp_path / "FLIR-1.json").exists()

    both = cw.read_pfs_files(tmp_path, ("pfs", "json"))
    assert both == {"BAS-1": "<pfs/>\n", "FLIR-1": "{}\n"}
    # A single suffix still reads only its own files.
    assert cw.read_pfs_files(tmp_path, "json") == {"FLIR-1": "{}\n"}

    # copy_auxiliary_pfs preserves non-live per-camera files across both formats.
    (tmp_path / "aux.pfs").write_text("<aux/>\n")
    dst = tmp_path / "dst"
    cw.copy_auxiliary_pfs(tmp_path, dst, {"BAS-1", "FLIR-1"}, ("pfs", "json"))
    assert (dst / "aux.pfs").exists()
    assert not (dst / "BAS-1.pfs").exists()  # a live serial is not copied
    assert not (dst / "FLIR-1.json").exists()


def test_backend_key_preserved_through_save(tmp_path):
    raw = {"backend": "flir", "gui": {"fps_default": 30.0}, "cameras": []}
    doc = cw.merge_camera_display(raw, [{"serial": "A", "rotation_deg": 90.0}])
    cw.write_config(tmp_path, doc)
    written = (tmp_path / "octacam_config.toml").read_text()
    assert tomllib.loads(written)["backend"] == "flir"
    assert parse_config(tmp_path / "octacam_config.toml").backend == "flir"


def test_with_process_params_overlays_and_preserves_other_sections():
    raw = {
        "transcode": {"ffmpeg_params": "-c:v libx264 -crf 20"},
        "transfer": {"directory": "~/store", "checksum": True},
        "visualization": [{"name": "grid.mp4", "layout": [["a", "b"]]}],
    }
    edited = cw.with_process_params(
        raw,
        transcode_ffmpeg_params="-c:v ffv1",
        transfer_directory="~/other",
        transfer_checksum=False,
    )
    assert edited["transcode"]["ffmpeg_params"] == "-c:v ffv1"
    assert edited["transfer"] == {"directory": "~/other", "checksum": False}
    # Untouched sections (incl. the 2D visualization layout) are preserved and
    # the input dict is not mutated.
    assert edited["visualization"] == raw["visualization"]
    assert raw["transcode"]["ffmpeg_params"] == "-c:v libx264 -crf 20"


def test_with_process_params_noop_when_values_match():
    # Unchanged values -> the copy compares equal, so the snapshot stays a
    # byte-verbatim copy rather than a re-emit.
    raw = {
        "record": {"fps": 100.0},
        "transcode": {"ffmpeg_params": "-c:v libx264 -crf 20"},
        "transfer": {"directory": "~/store", "checksum": True},
    }
    assert (
        cw.with_process_params(
            raw,
            transcode_ffmpeg_params="-c:v libx264 -crf 20",
            transfer_directory="~/store",
            transfer_checksum=True,
        )
        == raw
    )


def test_with_process_params_adds_sections_only_when_diverging():
    from octacam.writer import DEFAULT_TRANSCODE_FFMPEG_PARAMS

    # No [transcode]/[transfer] and default/blank values -> no sections added,
    # so a rig without a transfer destination never grows an empty one.
    base = {"record": {"fps": 100.0}}
    assert (
        cw.with_process_params(
            base,
            transcode_ffmpeg_params=DEFAULT_TRANSCODE_FFMPEG_PARAMS,
            transfer_directory="",
            transfer_checksum=True,
        )
        == base
    )
    # A non-default transcode arg / non-blank transfer dir creates the sections.
    added = cw.with_process_params(
        {},
        transcode_ffmpeg_params="-c:v ffv1",
        transfer_directory="~/store",
        transfer_checksum=False,
    )
    assert added == {
        "transcode": {"ffmpeg_params": "-c:v ffv1"},
        "transfer": {"directory": "~/store", "checksum": False},
    }
