import dataclasses
import logging
import os
import time
from pathlib import Path
from typing import get_args

import pytest

from octacam.config import (
    GuiConfig,
    OctacamConfig,
    RecordConfig,
    RecordForm,
    RecordingSettings,
    SaveMethod,
    TranscodeConfig,
    TransferConfig,
    compose_save_dir,
    duration_to_seconds,
    increment_trailing_number,
    load_config_dir,
    normalize_dir,
    parse_config,
    resolve_save_path,
    safe_segment,
)
from octacam.writer import DEFAULT_FFMPEG_PARAMS, FORMATS

REPO_ROOT = Path(__file__).parent.parent


def test_missing_file_returns_defaults(tmp_path):
    config = parse_config(tmp_path / "nope.toml")
    assert config.cameras == []
    assert config.gui == GuiConfig()


def test_record_defaults():
    record = RecordConfig()
    assert record.save_transformed is True
    assert record.save_timestamps is False
    assert record.trigger_source == "software"


def test_writer_queue_size_default_and_floor():
    assert RecordConfig().writer_queue_size == 64
    assert RecordConfig(writer_queue_size=128).writer_queue_size == 128
    # queue.Queue(maxsize=0) is *unbounded*; the validator floors at 1 so the
    # bound is always meaningful (a slow encoder can never OOM the host).
    assert RecordConfig(writer_queue_size=0).writer_queue_size == 1
    assert RecordConfig(writer_queue_size=-10).writer_queue_size == 1


def test_parses_emulate_basler_config(monkeypatch):
    epoch = time.localtime(0)
    monkeypatch.setattr(time, "localtime", lambda *_a: epoch)
    config = load_config_dir(REPO_ROOT / "configs" / "emulate_basler")
    assert config.record.fps == 30.0
    assert config.record.duration == 1.0
    assert len(config.cameras) == 8
    assert config.cameras[0].serial_number == "0815-0000"
    assert config.cameras[0].name == "camera_LF"
    assert config.cameras[7].window_height == 0.666667
    # directory/relative_directory carry strftime codes that are only expanded
    # at record time via resolve_save_path, not at parse time.
    assert "%y" in config.record.relative_directory
    save_dir = resolve_save_path(config.record).save_dir
    assert "%y" not in save_dir
    assert time.strftime("%y%m%d", epoch) in save_dir


def test_resolve_save_path_expands_both_parts_at_one_moment(monkeypatch):
    # Each read of the clock a year later: a second read would split the parts.
    moments = iter(time.strptime(f"{year}-12-31", "%Y-%m-%d") for year in (2025, 2026))
    monkeypatch.setattr(time, "localtime", lambda *_a: next(moments))
    home = os.path.expanduser("~")
    record = RecordConfig(directory="~/data/%Y", relative_directory="%Y%m%d/001")
    path = resolve_save_path(record)
    assert path.directory == f"{home}/data/2025"
    # Kept relative: the transfer mirrors it under its destination.
    assert path.relative == "20251231/001"
    assert path.save_dir == f"{home}/data/2025/20251231/001"
    assert resolve_save_path(RecordConfig(directory="/d")).save_dir == "/d"


def test_compose_save_dir():
    assert compose_save_dir("/base", "run/001") == "/base/run/001"
    assert compose_save_dir("/base", "") == "/base"
    # An absolute relative part discards the base.
    assert compose_save_dir("/base", "/elsewhere/001") == "/elsewhere/001"


def test_normalize_dir():
    home = os.path.expanduser("~")
    assert normalize_dir(" ~/data ") == f"{home}/data"
    assert normalize_dir("/a/b") == "/a/b"
    assert normalize_dir("rel") == f"{os.getcwd()}/rel"
    assert normalize_dir("a\\b").endswith("/a/b")


def test_increment_trailing_number():
    assert increment_trailing_number("/data/001-bhv") == "/data/002-bhv"
    assert increment_trailing_number("/d/240101_/Fly1/009") == "/d/240101_/Fly1/010"
    assert increment_trailing_number("/data/run007/trial003") == "/data/run007/trial004"
    assert increment_trailing_number("/data/999") == "/data/1000"
    assert increment_trailing_number("/data/no-number") == "/data/no-number"


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


def test_name_clashing_with_serial_fallback_skipped(tmp_path):
    # A blank-name camera records under its serial as the filename stem, so a
    # later entry whose explicit name equals that serial would clobber the same
    # video file; the clash must be rejected at parse time.
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "X"\n'
        '[[cameras]]\nserial_number = "Y"\nname = "X"\n'
    )
    config = load_config_dir(tmp_path)
    assert [c.serial_number for c in config.cameras] == ["X"]


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


@pytest.mark.parametrize("name", ["", "   ", ".", "..", "a/b", "a\\b", "/abs", "x/../y"])
def test_safe_segment_rejects(name):
    with pytest.raises(ValueError, match="^Invalid camera name: "):
        safe_segment(name, "camera name")


def test_safe_segment_strips():
    assert safe_segment("  cam left  ", "camera name") == "cam left"
    assert safe_segment("cam_01", "camera name") == "cam_01"


def test_center_flags_parse(tmp_path):
    # The ROI auto-centering flags default off and round-trip as booleans.
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\ncenter_x = true\ncenter_y = true\n'
        '[[cameras]]\nserial_number = "b"\n'
    )
    config = load_config_dir(tmp_path)
    assert config.cameras[0].center_x is True and config.cameras[0].center_y is True
    assert config.cameras[1].center_x is False and config.cameras[1].center_y is False


def test_gui_theme_parse(tmp_path):
    # [gui].theme defaults to "dark", accepts "light", and falls back to the
    # default on an unknown value (lenient validation).
    assert GuiConfig().theme == "dark"
    (tmp_path / "octacam_config.toml").write_text('[gui]\ntheme = "light"\n')
    assert load_config_dir(tmp_path).gui.theme == "light"
    (tmp_path / "octacam_config.toml").write_text('[gui]\ntheme = "chartreuse"\n')
    assert load_config_dir(tmp_path).gui.theme == "dark"


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


def test_spinnaker_backend_is_pinnable(tmp_path):
    # The Spinnaker C-API tier must be nameable in TOML: on modern Python the
    # PySpin "flir" tier drops out, so pinning FLIRs to "spinnaker" is the only
    # way to force the vendor C API instead of leaving it to "auto".
    (tmp_path / "octacam_config.toml").write_text('backend = "spinnaker"\n')
    assert load_config_dir(tmp_path).backend == "spinnaker"


def test_unknown_backend_falls_back_to_auto(tmp_path):
    (tmp_path / "octacam_config.toml").write_text('backend = "nikon"\n')
    assert load_config_dir(tmp_path).backend == "auto"


def test_backend_names_match_the_registry():
    # config lists the names itself so parsing imports no camera layer.
    from octacam.cameras import registry
    from octacam.config import _BACKENDS

    assert _BACKENDS == ("auto", *registry.BACKENDS)


def test_transfer_checksum_defaults_true(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[transfer]\ndirectory = "/mnt/store"\n'
    )
    cfg = load_config_dir(tmp_path)
    assert cfg.transfer is not None and cfg.transfer.checksum is True


def test_transfer_checksum_parsed(tmp_path):
    (tmp_path / "octacam_config.toml").write_text("[transfer]\nchecksum = false\n")
    assert load_config_dir(tmp_path).transfer.checksum is False


@pytest.mark.parametrize("body", ["", 'transfer = "/mnt/store"\n'])
def test_transfer_absent_or_not_a_table_is_none(tmp_path, body):
    (tmp_path / "octacam_config.toml").write_text(body)
    assert load_config_dir(tmp_path).transfer is None


def test_transfer_bad_field_keeps_default(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[transfer]\ndirectory = "/mnt/store"\nchecksum = "maybe"\n'
    )
    transfer = load_config_dir(tmp_path).transfer
    assert transfer is not None
    assert (transfer.directory, transfer.checksum) == ("/mnt/store", True)


def test_visualization_skips_entries_without_a_valid_layout(tmp_path, caplog):
    (tmp_path / "octacam_config.toml").write_text(
        'visualization = [\n'
        '  "not a table",\n'
        '  { name = "missing.mp4" },\n'
        '  { name = "empty.mp4", layout = [] },\n'
        '  { name = "ragged.mp4", layout = [["a", "b"], ["c"]] },\n'
        '  { name = "number.mp4", layout = [["a", 1]] },\n'
        '  { name = "flat.mp4", layout = ["a", "b"] },\n'
        '  { name = "ok.mp4", layout = [["a", ""], ["", "b"]] },\n'
        ']\n'
    )
    with caplog.at_level(logging.WARNING, logger="octacam"):
        viz = load_config_dir(tmp_path).visualization
    assert [(v.name, v.layout) for v in viz] == [("ok.mp4", [["a", ""], ["", "b"]])]
    assert len(caplog.messages) == 6


def test_visualization_bad_fields_default_and_duplicate_names_skip(tmp_path):
    (tmp_path / "octacam_config.toml").write_text(
        '[[visualization]]\nname = ["x"]\nffmpeg_params = true\nlayout = [["a"]]\n'
        '[[visualization]]\nlayout = [["b"]]\n'
        '[[visualization]]\nname = "side.mp4"\nffmpeg_params = "-crf 20"\n'
        'layout = [["c"]]\n'
    )
    viz = load_config_dir(tmp_path).visualization
    assert [(v.name, v.layout, v.ffmpeg_params) for v in viz] == [
        ("grid.mp4", [["a"]], ""),
        ("side.mp4", [["c"]], "-crf 20"),
    ]


def test_visualization_bad_ffmpeg_params_falls_back(tmp_path, caplog):
    # The grid splits them at build time: bad quoting there would abort
    # `octacam process` before its transfer.
    (tmp_path / "octacam_config.toml").write_text(
        '[[visualization]]\nlayout = [["a"]]\nffmpeg_params = "-c:v libx264 \\"oops"\n'
    )
    with caplog.at_level(logging.WARNING, logger="octacam"):
        (viz,) = load_config_dir(tmp_path).visualization
    assert viz.ffmpeg_params == ""
    assert 'invalid "ffmpeg_params"' in caplog.text


def test_visualization_layout_unknown_camera_warns(tmp_path, caplog):
    # A layout cell naming a camera that isn't declared must be reported, not
    # silently rendered black.
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\nname = "camera_LF"\n'
        '[[cameras]]\nserial_number = "b"\nname = "camera_RF"\n'
        '[[visualization]]\nlayout = [["camera_LF", "camera_TYPO"]]\n'
    )
    with caplog.at_level(logging.WARNING, logger="octacam"):
        load_config_dir(tmp_path)
    assert any("camera_TYPO" in m for m in caplog.messages)


def test_visualization_layout_known_cameras_no_unknown_warning(tmp_path, caplog):
    (tmp_path / "octacam_config.toml").write_text(
        '[[cameras]]\nserial_number = "a"\nname = "camera_LF"\n'
        '[[visualization]]\nlayout = [["camera_LF", ""]]\n'
    )
    with caplog.at_level(logging.WARNING, logger="octacam"):
        load_config_dir(tmp_path)
    assert not any("unknown camera" in m for m in caplog.messages)


# --- a config that does not parse at all ------------------------------------ #


def test_parse_config_raises_on_malformed_toml(tmp_path):
    """A file that exists but does not parse must fail loudly, not silently
    become an all-defaults config.

    Field-level problems stay tolerant (warn-and-default), but a decode error
    yielded *nothing*: the rig then ran on stock defaults — every detected
    camera, save dir "./", no plugins, no [transfer] destination — which reads to
    the operator as "octacam ignored my config". The documented flywheel
    `options.command` example was itself invalid TOML (an inline table cannot be
    extended with a dotted key), so copy-pasting the docs hit exactly this.
    """
    from octacam.config import ConfigError, parse_config

    bad = tmp_path / "octacam_config.toml"
    bad.write_text(
        '[[plugins]]\n'
        'name = "flywheel"\n'
        'options = { device = "/dev/ttyACM0" }\n'
        'options.command = { n_steps = -2048 }\n'
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(bad)
    # The message names the file and the offending line, so it is actionable.
    assert str(bad) in str(excinfo.value)


def test_parse_config_still_tolerant_of_bad_fields(tmp_path):
    """The decode-error change must not make field-level parsing strict."""
    from octacam.config import parse_config

    good = tmp_path / "octacam_config.toml"
    good.write_text('[record]\nfps = "not-a-number"\n')
    config = parse_config(good)  # warns and defaults, does not raise
    assert config.record.fps > 0


def test_parse_config_missing_file_returns_defaults(tmp_path):
    from octacam.config import parse_config

    config = parse_config(tmp_path / "nope.toml")
    assert config.cameras == []


def test_documented_flywheel_options_example_parses():
    """The docs' [[plugins]] flywheel block must be valid TOML.

    It is copy-paste material for operators, and an invalid one used to land them
    in the all-defaults path above rather than pointing at the bad line.
    """
    import re
    from pathlib import Path

    import tomllib

    doc = Path(__file__).resolve().parents[1] / "docs" / "guide" / "plugins.md"
    block = re.search(
        r"```toml\n(\[\[plugins\]\]\nname = \"flywheel\".*?)```", doc.read_text(), re.S
    )
    assert block is not None, "flywheel options example not found in plugins.md"
    parsed = tomllib.loads(block.group(1))
    assert parsed["plugins"][0]["options"]["command"]["n_steps"] == -2048


# ------------------------------------------------- live settings


def test_record_config_values_covers_every_record_setting():
    # Each recording's config snapshot is written from record_config_values, so a
    # new [record] key must either be reproduced there or be deliberately left
    # out — otherwise a recording made with it would relaunch with the rig
    # file's value instead of its own.
    reproduced = set(RecordingSettings().record_config_values())
    # duration_s stands in for the duration/unit pair (config_writer picks a unit).
    reproduced = (reproduced - {"duration_s"}) | {"duration", "duration_unit"}
    # The save path templates are kept as written, so a relaunch resolves a fresh
    # dated folder; the path a recording used is in its summary.
    excluded = {"directory", "relative_directory"}
    assert set(RecordConfig.model_fields) == reproduced | excluded


def test_from_config_takes_the_config_as_it_loads(monkeypatch):
    moment = time.localtime(0)
    monkeypatch.setattr(time, "localtime", lambda *_a: moment)

    # Every value off its default, so a dropped mapping fails.
    config = OctacamConfig(
        record=RecordConfig(
            fps=50.0,
            duration=100.0,
            duration_unit="frames",
            trigger_source="managed",
            preview_trigger_source="free_running",
            directory="/data/%Y",
            relative_directory="Fly1/001",
            save_method="nvenc",
            ffmpeg_params="-c:v libx264 -crf 23",
            nvenc_params="-c:v hevc_nvenc -cq 20",
            max_nvenc_sessions=3,
            writer_queue_size=12,
            save_transformed=False,
            save_timestamps=True,
        ),
        transcode=TranscodeConfig(ffmpeg_params="-c:v libx265"),
        transfer=TransferConfig(directory="/store", checksum=False),
    )
    year = time.strftime("%Y", moment)
    assert dataclasses.asdict(RecordingSettings.from_config(config)) == {
        "fps": 50.0,
        "duration_s": 2.0,
        "save_dir": f"/data/{year}/Fly1/001",
        "record_directory": f"/data/{year}",
        "relative_directory": "Fly1/001",
        "trigger_source": "managed",
        "preview_trigger_source": "free_running",
        "save_method": "nvenc",
        "ffmpeg_params": "-c:v libx264 -crf 23",
        "nvenc_params": "-c:v hevc_nvenc -cq 20",
        "max_nvenc_sessions": 3,
        "writer_queue_size": 12,
        "record_form": "sensor",
        "save_frame_timestamps": True,
        "transcode_ffmpeg_params": "-c:v libx265",
        "transfer_directory": "/store",
        "transfer_checksum": False,
    }
    # The fps override applies before a frame-count duration converts.
    overridden = RecordingSettings.from_config(config, fps=100.0)
    assert (overridden.fps, overridden.duration_s) == (100.0, 1.0)
    # No [transfer]: no transfer, and checksums on for a later one.
    bare = RecordingSettings.from_config(OctacamConfig())
    assert (bare.transfer_directory, bare.transfer_checksum) == ("", True)


def test_from_config_expands_the_save_dirs_at_one_moment(monkeypatch):
    # Each read of the clock a year later: a second read would split the dirs.
    moments = iter(time.strptime(f"{year}-12-31", "%Y-%m-%d") for year in (2025, 2026))
    monkeypatch.setattr(time, "localtime", lambda *_a: next(moments))
    record = RecordConfig(directory="/d/%Y", relative_directory="%Y/001")
    settings = RecordingSettings.from_config(OctacamConfig(record=record))
    assert (settings.record_directory, settings.relative_directory) == (
        "/d/2025",
        "2025/001",
    )
    assert settings.save_dir == "/d/2025/2025/001"


def test_next_take_bumps_the_relative_part_else_save_dir():
    split = RecordingSettings(
        record_directory="/base", relative_directory="day/009", save_dir="/base/day/009"
    ).next_take()
    assert split.relative_directory == "day/010"
    assert split.save_dir == "/base/day/010"
    lone = RecordingSettings(save_dir="/data/001-bhv").next_take()
    assert lone.save_dir == "/data/002-bhv"
    # The relative part is joined stripped, as a live edit joins it.
    padded = RecordingSettings(
        record_directory="/base", relative_directory=" day/009", save_dir="/base/ day/009"
    ).next_take()
    assert (padded.relative_directory, padded.save_dir) == (" day/010", "/base/day/010")


def test_updated_composes_save_dir_from_the_split():
    settings = RecordingSettings(record_directory="/base", save_dir="/base")
    assert settings.updated(relative_directory="day/002").save_dir == "/base/day/002"
    # Stored as sent, joined stripped.
    padded = settings.updated(relative_directory=" day/002")
    assert (padded.relative_directory, padded.save_dir) == (" day/002", "/base/day/002")
    # An absolute relative part discards the base.
    for relative in ("/elsewhere/001", " /elsewhere/001"):
        assert settings.updated(relative_directory=relative).save_dir == "/elsewhere/001"
    # The base is stored normalized: the GUI shows it and relative_save_dir
    # measures against it.
    based = settings.updated(record_directory=" ~/b ")
    home = os.path.expanduser("~")
    assert (based.record_directory, based.save_dir) == (f"{home}/b", f"{home}/b")


def test_with_save_dir_clears_the_split():
    settings = RecordingSettings(
        record_directory="/base", relative_directory="day/001", save_dir="/base/day/001"
    ).with_save_dir(" ~/other ")
    assert settings.save_dir == f"{os.path.expanduser('~')}/other"
    assert (settings.record_directory, settings.relative_directory) == ("", "")


def test_relative_save_dir():
    # The explicit relative part, else save_dir under the base, else its name.
    assert RecordingSettings(
        record_directory="/b", relative_directory="day/001", save_dir="/b/day/001"
    ).relative_save_dir() == "day/001"
    under = RecordingSettings(record_directory="/b", save_dir="/b/x/002")
    assert under.relative_save_dir() == "x/002"
    outside = RecordingSettings(record_directory="/b", save_dir="/out/003")
    assert outside.relative_save_dir() == "003"
    assert RecordingSettings(save_dir="/out/004").relative_save_dir() == "004"


def test_settings_updated_names_each_bad_field():
    settings = RecordingSettings()
    with pytest.raises(ValueError, match=r"^Unknown settings: \['codec'\]$"):
        settings.updated(codec="vp9")
    with pytest.raises(ValueError, match="^fps: ") as error:
        settings.updated(fps=0, duration_s=5.0)
    assert "duration_s" not in str(error.value)
    with pytest.raises(ValueError, match="^duration_s: "):
        settings.updated(duration_s=0)
    with pytest.raises(ValueError, match="^save_method: "):
        settings.updated(save_method="vp9")
    with pytest.raises(ValueError, match="^transcode_ffmpeg_params: bad quoting"):
        settings.updated(transcode_ffmpeg_params='a "b')
    # Every field is type-checked, as JSON would be: no None in a str or bool.
    for field in ("save_dir", "transfer_directory", "save_frame_timestamps"):
        with pytest.raises(ValueError, match=f"^{field}: "):
            settings.updated(**{field: None})
    # Coerced like the HTTP boundary's JSON: a numeric string is a number.
    assert settings.updated(fps="100").fps == 100.0
    # The changed fields only: a config value the GUI would refuse (fps 0, as
    # the tolerant config loads it) must not block editing another field.
    assert RecordingSettings(fps=0.0).updated(duration_s=5.0).duration_s == 5.0


def test_save_methods_are_the_writer_formats():
    assert set(get_args(SaveMethod)) == set(FORMATS)


def test_record_forms_are_the_benchmark_choices():
    from octacam import cli

    assert {form.value for form in cli.RecordForm} == set(get_args(RecordForm))


def test_video_format_carries_ffmpeg_params():
    settings = RecordingSettings(
        save_method="ffmpeg",
        ffmpeg_params="-c:v libx264 -preset superfast -crf 20 -pix_fmt yuv420p",
    )
    video_format = settings.video_format()
    assert video_format.save_method == "ffmpeg"
    assert (
        video_format.ffmpeg_params
        == "-c:v libx264 -preset superfast -crf 20 -pix_fmt yuv420p"
    )
    assert RecordingSettings(save_method="raw").video_format().extension == "raw"


def test_recording_settings_default_ffmpeg_params():
    # The capture default tracks writer.DEFAULT_FFMPEG_PARAMS (CRF 18 ultrafast,
    # near visually lossless); config's record.ffmpeg_params overrides it.

    assert RecordingSettings().ffmpeg_params == DEFAULT_FFMPEG_PARAMS
