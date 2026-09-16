"""TOML config writer: round-trip fidelity, strftime safety, atomic writes."""

import glob
from pathlib import Path

import pytest

from octacam import config_writer as cw
from octacam._compat import tomllib
from octacam.config import parse_config, parse_record_section

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


def test_merge_persists_center_flags(tmp_path):
    # The ROI auto-centering flags travel with the display fields and survive a
    # write -> reparse round-trip (including an explicit False).
    raw = {"cameras": [{"serial_number": "A"}]}
    doc = cw.merge_camera_display(
        raw,
        [
            {"serial": "A", "center_x": True, "center_y": False},
            {"serial": "B", "center_x": True, "center_y": True},
        ],
    )
    cw.write_config(tmp_path, doc)
    config = parse_config(tmp_path / "octacam_config.toml")
    by_serial = {c.serial_number: c for c in config.cameras}
    assert by_serial["A"].center_x is True and by_serial["A"].center_y is False
    assert by_serial["B"].center_x is True and by_serial["B"].center_y is True


def test_dumps_escapes_strings():
    doc = {"gui": {"save_directory_default": 'a"b\\c'}}
    assert tomllib.loads(cw._dumps(doc))["gui"]["save_directory_default"] == 'a"b\\c'


def test_dumps_escapes_del_control_char():
    # U+007F (DEL) must be escaped; emitting it raw produces unparseable TOML
    # that would silently reset the whole config to defaults on the next load.
    doc = {"record": {"directory": "ab\x7fcd"}}
    assert tomllib.loads(cw._dumps(doc))["record"]["directory"] == "ab\x7fcd"


def test_dumps_serializes_date_and_datetime():
    # The reader accepts an unquoted, date-like scalar (parsed to datetime.date);
    # the writer must round-trip it instead of raising TypeError.
    import datetime

    doc = {
        "record": {
            "directory": datetime.date(2026, 7, 9),
            "stamp": datetime.datetime(2026, 7, 9, 13, 30, 5),
        }
    }
    reparsed = tomllib.loads(cw._dumps(doc))["record"]
    assert reparsed["directory"] == datetime.date(2026, 7, 9)
    assert reparsed["stamp"] == datetime.datetime(2026, 7, 9, 13, 30, 5)


def test_plugin_options_roundtrip():
    doc = {
        "plugins": [
            {"name": "flywheel", "options": {"port": "/dev/ttyUSB0", "baud": 9600}}
        ]
    }
    assert tomllib.loads(cw._dumps(doc)) == doc


def test_plugin_options_inline_table_arrays_roundtrip():
    # The triggerbox plugin nests arrays-of-inline-tables (cameras/lights) under
    # [plugins.options]; a GUI camera-display save re-dumps the whole config, so
    # these must survive _dumps (previously _toml_value raised on a dict).
    doc = {
        "plugins": [
            {
                "name": "triggerbox",
                "options": {
                    "device": "auto",
                    "strobe_guard_us": 100,
                    "cameras": [{"pin": "D13", "pulse_us": 500}],
                    "lights": [
                        {"channel": 1, "mode": "strobe", "duty_mode": "auto"},
                        {"channel": 3, "mode": "pulse_train", "freq_hz": 10.0},
                    ],
                },
            }
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


def test_bare_name_plugins_survive_a_rewrite(tmp_path):
    # The loader accepts `plugins = ["flywheel"]`; a rewrite used to drop the list
    # entirely because only [[plugins]] tables were emitted.
    raw = tomllib.loads('plugins = ["flywheel"]\n[record]\nfps = 10.0\n')
    cw.write_config(tmp_path, raw)
    plugins = parse_config(tmp_path / "octacam_config.toml").plugins
    assert [(p.name, p.options) for p in plugins] == [("flywheel", {})]


# --- with_record_settings ----------------------------------------------------

# The live values of a recording whose settings all equal the RecordConfig defaults.
_DEFAULTS = parse_record_section({})
_DEFAULT_LIVE = {
    "fps": _DEFAULTS.fps,
    "duration_s": _DEFAULTS.duration,  # the default unit is seconds
    "trigger_source": _DEFAULTS.trigger_source,
    "preview_trigger_source": _DEFAULTS.preview_trigger_source,
    "save_method": _DEFAULTS.save_method,
    "ffmpeg_params": _DEFAULTS.ffmpeg_params,
    "nvenc_params": _DEFAULTS.nvenc_params,
    "max_nvenc_sessions": _DEFAULTS.max_nvenc_sessions,
    "writer_queue_size": _DEFAULTS.writer_queue_size,
    "save_transformed": _DEFAULTS.save_transformed,
    "save_timestamps": _DEFAULTS.save_timestamps,
}


def test_with_record_settings_noop_when_nothing_changed():
    # A config that omits every [record] key loads as the defaults, so a
    # recording made with them leaves the snapshot verbatim (no section added)...
    assert cw.with_record_settings({}, _DEFAULT_LIVE) == {}
    # ...and so does one that states the same length in another unit.
    raw = {"record": {"duration": 2.0, "duration_unit": "minutes"}}
    assert cw.with_record_settings(raw, {**_DEFAULT_LIVE, "duration_s": 120.0}) == raw


def test_with_record_settings_patches_only_what_changed():
    raw = {
        "record": {
            "fps": 80.0,
            "directory": "~/data/%y%m%d",
            "relative_directory": "Fly1/001",
            "max_nvenc_sessions": 2,
        },
        "transfer": {"directory": "~/store"},
    }
    live = {**_DEFAULT_LIVE, "fps": 125.0, "save_timestamps": True}
    edited = cw.with_record_settings(raw, live)
    # The changed values are written; max_nvenc_sessions went back to auto
    # (None), which TOML can only express by omitting the key; the unchanged
    # duration and the path templates stay as they were.
    assert edited["record"] == {
        "fps": 125.0,
        "directory": "~/data/%y%m%d",
        "relative_directory": "Fly1/001",
        "save_timestamps": True,
    }
    assert edited["transfer"] == raw["transfer"]
    assert raw["record"]["fps"] == 80.0  # the input is not mutated


@pytest.mark.parametrize(
    ("unit", "fps", "duration_s", "expected"),
    [
        # Exact and readable in the config's unit: kept.
        ("minutes", 100.0, 3600.0, (60.0, "minutes")),
        ("hours", 100.0, 1800.0, (0.5, "hours")),
        # A frame count follows the fps: 10 s at 125 fps is 1250 frames.
        ("frames", 125.0, 10.0, (1250.0, "frames")),
        # Unreadable in the unit (1.6666666666666667 min, 0.0019444 h): seconds.
        ("minutes", 100.0, 100.0, (100.0, "seconds")),
        ("hours", 100.0, 7.0, (7.0, "seconds")),
        # Not exact in the unit (0.1 s * 3 fps / 3 fps != 0.1 s): seconds.
        ("frames", 3.0, 0.1, (0.1, "seconds")),
    ],
)
def test_with_record_settings_duration_units(unit, fps, duration_s, expected):
    from octacam.config import duration_to_seconds

    raw = {"record": {"fps": fps, "duration": 1.0, "duration_unit": unit}}
    edited = cw.with_record_settings(
        raw, {**_DEFAULT_LIVE, "fps": fps, "duration_s": duration_s}
    )
    record = edited["record"]
    assert (record["duration"], record["duration_unit"]) == expected
    # Whatever the unit, the snapshot loads back as the recorded length.
    reloaded = parse_record_section(edited)
    assert duration_to_seconds(reloaded.duration, reloaded.duration_unit, fps) == duration_s


def test_with_record_settings_rescales_frames_when_only_fps_changed():
    # 800 frames at 80 fps is 10 s. A 10 s recording at 125 fps must not keep
    # "800 frames", which would now load as 6.4 s.
    raw = {"record": {"fps": 80.0, "duration": 800.0, "duration_unit": "frames"}}
    edited = cw.with_record_settings(
        raw, {**_DEFAULT_LIVE, "fps": 125.0, "duration_s": 10.0}
    )
    assert edited["record"] == {
        "fps": 125.0,
        "duration": 1250.0,
        "duration_unit": "frames",
    }


# --- with_plugin_options -----------------------------------------------------


def test_with_plugin_options_merges_into_the_configured_entry():
    raw = {
        "plugins": [
            {"name": "triggerbox", "options": {"device": "/dev/ttyACM0", "lights": []}},
        ]
    }
    edited = cw.with_plugin_options(
        raw, {"triggerbox": {"lights": [{"channel": 1, "mode": "continuous"}]}}
    )
    assert edited["plugins"] == [
        {
            "name": "triggerbox",
            "options": {
                "device": "/dev/ttyACM0",
                "lights": [{"channel": 1, "mode": "continuous"}],
            },
        }
    ]
    assert raw["plugins"][0]["options"]["lights"] == []  # not mutated


def test_with_plugin_options_matches_aliases_and_bare_names():
    raw = {"plugins": ["arduino", {"name": "twophoton"}]}
    edited = cw.with_plugin_options(raw, {"flywheel": {"x": 1}, "twophoton": {}})
    # The legacy name is matched (not appended again) and the bare entry becomes
    # a table to hold its options; a plugin with nothing to add is untouched.
    assert edited["plugins"] == [
        {"name": "arduino", "options": {"x": 1}},
        {"name": "twophoton"},
    ]


def test_with_plugin_options_appends_plugins_the_config_does_not_list():
    # A plugin enabled only with --plugin must be listed for a relaunch to load it.
    edited = cw.with_plugin_options({"record": {}}, {"triggerbox": {}})
    assert edited["plugins"] == [{"name": "triggerbox", "options": {}}]


def test_with_plugin_options_noop_when_nothing_to_add():
    raw = {"plugins": [{"name": "triggerbox", "options": {"device": "x"}}]}
    assert cw.with_plugin_options(raw, {"triggerbox": {}}) == raw
    assert cw.with_plugin_options({}, {}) == {}
