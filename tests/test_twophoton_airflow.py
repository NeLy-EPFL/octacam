"""airflow_control's logged air commands, copied beside a take ThorSync did not
record (synthetic logs and takes).
"""

import json
from datetime import datetime
from pathlib import Path

from typer.testing import CliRunner

from octacam.cli import app
from octacam.plugins.twophoton.airflow import (
    AIRFLOW_FILENAME,
    attach_airflow_commands,
    commands_for,
)
from octacam.plugins.twophoton.settings import TwoPhotonSettings

runner = CliRunner()

T0 = 1_792_500_000.0  # the take's first frame (host clock)


def _line(t: float, channel: str, event: str, setpoint: float, **extra) -> str:
    stamp = datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")
    return json.dumps(
        {
            "t": stamp,
            "mono": 0.0,
            "channel": channel,
            "event": event,
            "setpoint": setpoint,
            **extra,
        }
    )


def _log(folder: Path, name: str, lines: list[str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("\n".join(lines) + "\n")
    return path


def _take(root: Path, *, first: float = T0, duration: float = 60.0) -> Path:
    take = root / "Fly1" / "001"
    (take / "octacam_recording").mkdir(parents=True)
    (take / "octacam_recording" / "recording_summary.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "first_frame_time_ns": int(first * 1e9),
                "duration_s": duration,
                "cameras": [{"name": "camera_LM", "frames": 6000, "fps": 100.0}],
            }
        )
    )
    return take


def test_the_take_gets_its_commands_and_the_flow_it_started_with(tmp_path):
    logs = tmp_path / "logs"
    _log(
        logs,
        "airflow_20261020_140022.jsonl",
        [
            _line(T0 - 600, "air", "continuous", 0.0),
            _line(T0 - 30, "air", "continuous", 0.7),  # on before the take
            _line(T0 - 20, "co2", "continuous", 0.0),
            _line(T0 + 10, "air", "continuous", 0.0),
            _line(T0 + 30, "air", "loop_step", 0.8),
            _line(T0 + 400, "air", "continuous", 0.0),  # long after
        ],
    )
    take = _take(tmp_path / "day")
    lines = commands_for(take, logs)
    assert lines is not None
    events = [(json.loads(x)["channel"], json.loads(x)["setpoint"]) for x in lines]
    # Each channel's last command before the take, then the take's own.
    assert events == [("air", 0.7), ("co2", 0.0), ("air", 0.0), ("air", 0.8)]


def test_a_take_no_session_covers_gets_nothing(tmp_path):
    logs = tmp_path / "logs"
    _log(
        logs,
        "airflow_20261012_115158.jsonl",
        [
            _line(T0 - 7200, "air", "continuous", 0.5),
            _line(T0 - 3600, "air", "stop", 0, reason="shutdown"),  # closed
        ],
    )
    take = _take(tmp_path / "day")
    assert commands_for(take, logs) is None
    assert attach_airflow_commands([take], logs) == []
    assert not (take / "octacam_recording" / AIRFLOW_FILENAME).exists()


def test_attached_beside_the_take_unless_a_dry_run(tmp_path):
    logs = tmp_path / "logs"
    _log(logs, "airflow_x.jsonl", [_line(T0 + 5, "air", "continuous", 1.0)])
    take = _take(tmp_path / "day")
    assert attach_airflow_commands([take], logs, dry_run=True) == [take]
    assert not (take / "octacam_recording" / AIRFLOW_FILENAME).exists()
    assert attach_airflow_commands([take], logs) == [take]
    written = (take / "octacam_recording" / AIRFLOW_FILENAME).read_text().splitlines()
    assert [json.loads(x)["setpoint"] for x in written] == [1.0]


def test_the_log_folder_is_a_plugin_option(tmp_path):
    assert TwoPhotonSettings(source="/share").airflow_log_dir is None
    settings = TwoPhotonSettings(source="/share", airflow_logs="~/airflow_control_logs")
    assert settings.airflow_log_dir == Path.home() / "airflow_control_logs"


def test_the_board_sync_command_attaches_the_commands(tmp_path):
    # A take without ThorSync, with the board's report and a ThorImage folder.
    take = _take(tmp_path / "261020_AllPAMxCI80", duration=328.9)
    info = take / "octacam_recording"
    summary = json.loads((info / "recording_summary.json").read_text())
    summary.update(fps_target=100.0)
    summary["cameras"][0]["frames"] = 32894
    (info / "recording_summary.json").write_text(json.dumps(summary))
    stop = {"reason": "thorimage", "edges": 5000, "high_ms": 323156, "run_ms": 328940}
    (info / "twophoton.json").write_text(
        json.dumps({"armed": True, "stop_idle_ms": 2000, "stop": stop})
    )
    (info / "twophoton_match.json").write_text(
        json.dumps(
            {"matched": [{"path": "AllPAMxCI80/Fly1_Streaming", "kind": "image"}]}
        )
    )
    image = take / "2P" / "Fly1_Streaming"
    image.mkdir(parents=True)
    (image / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><LSM frameRate="15.312" />'
        '<Timelapse timepoints="5000" /></ThorImageExperiment>'
    )
    logs = tmp_path / "logs"
    _log(logs, "airflow_x.jsonl", [_line(T0 + 5, "air", "continuous", 0.6)])
    result = runner.invoke(
        app,
        ["twophoton", "board-sync", str(tmp_path), "--airflow-logs", str(logs)],
    )
    assert result.exit_code == 0, result.output
    assert (info / "twophoton_board_sync.json").is_file()
    assert (info / AIRFLOW_FILENAME).is_file()


def test_only_the_covering_session_and_its_last_setpoints_describe_the_start(tmp_path):
    # Real 261020 Fly1/Recording4: the last air line before the take was a
    # loop_disarm (no setpoint), and older sessions' shutdown lines (other
    # days, other channel names) leaked in as the starting state.
    logs = tmp_path / "logs"
    _log(
        logs,
        "airflow_20261016_143809.jsonl",
        [_line(T0 - 86400 * 4, "co2", "stop", 0.0)],
    )
    _log(
        logs,
        "airflow_20261020_140022.jsonl",
        [
            _line(T0 - 60, "air", "continuous", 0.9),
            json.dumps(
                {
                    "t": datetime.fromtimestamp(T0 - 30)
                    .astimezone()
                    .isoformat(timespec="milliseconds"),
                    "channel": "air",
                    "event": "loop_disarm",
                }
            ),
            _line(T0 + 3, "air", "continuous", 0.0),
        ],
    )
    take = _take(tmp_path / "day", duration=7.0)
    lines = [json.loads(x) for x in commands_for(take, logs) or []]
    assert [(r["channel"], r.get("setpoint")) for r in lines] == [
        ("air", 0.9),
        ("air", 0.0),
    ]


def test_a_constant_flow_through_the_take_is_still_covered(tmp_path):
    # Tommy's review: airflow_control logs only changes, so a session whose
    # last line came before the take read "air unknown". It runs until its
    # shutdown, or until the next session starts.
    logs = tmp_path / "logs"
    _log(
        logs,
        "airflow_20261022_113724.jsonl",
        [
            _line(T0 - 600, "air", "continuous", 0.4),
            _line(T0 + 3600, "air", "stop", 0.0, reason="shutdown"),
        ],
    )
    take = _take(tmp_path / "day")
    lines = [json.loads(x) for x in commands_for(take, logs) or []]
    assert [(r["channel"], r["setpoint"]) for r in lines] == [("air", 0.4)]


def test_a_take_stopped_by_hand_spans_its_frames_not_its_set_duration(tmp_path):
    # Tommy's review: duration_s is the set duration for a take stopped by
    # hand, so commands logged after it ended were attached. 6000 frames at
    # 100 fps ran 60 s of a set 300 s.
    logs = tmp_path / "logs"
    _log(
        logs,
        "airflow_x.jsonl",
        [
            _line(T0 + 10, "air", "continuous", 1.0),
            _line(T0 + 120, "air", "continuous", 0.0),  # after the take
        ],
    )
    take = _take(tmp_path / "day", duration=300.0)
    summary = take / "octacam_recording" / "recording_summary.json"
    summary.write_text(
        json.dumps({**json.loads(summary.read_text()), "fps_target": 100.0})
    )
    lines = [json.loads(x) for x in commands_for(take, logs) or []]
    assert [r["setpoint"] for r in lines] == [1.0]
