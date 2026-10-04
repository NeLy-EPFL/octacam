"""`octacam record` end to end on fake cameras, through the CLI.

Pins what an operator or a script sees: the exit code, the videos and the
recording summary on disk, and the warnings for an incomplete rig or an existing
save directory. A take is 20 software-triggered pulses of small raw frames.
"""

import json
import re

import pytest
from typer.testing import CliRunner

from octacam.cli import app

SERIALS = ["FAKE-0", "FAKE-1"]
FPS = 50.0
DURATION_S = 0.4
PULSES = round(FPS * DURATION_S)  # the train: one video frame per pulse
WIDTH, HEIGHT = 320, 240
FRAME_BYTES = WIDTH * HEIGHT  # a raw video is its Mono8 frames back to back

runner = CliRunner()


@pytest.fixture(autouse=True)
def fake_cameras(monkeypatch):
    monkeypatch.setenv("OCTACAM_FAKE_CAMERAS", ",".join(SERIALS))


def _rig(tmp_path, serials=SERIALS):
    """A rig config dir whose takes land in ``tmp_path/data/001``."""
    rig = tmp_path / "rig"
    rig.mkdir()
    record = {
        "fps": FPS,
        "duration": DURATION_S,
        "trigger_source": "software",
        "save_method": "raw",
        "directory": str(tmp_path / "data"),
        "relative_directory": "001",
    }
    lines = ['backend = "fake"', "[record]"]
    lines += [f"{key} = {json.dumps(value)}" for key, value in record.items()]
    for serial in serials:
        lines += ["[[cameras]]", f"serial_number = {json.dumps(serial)}"]
        (rig / f"{serial}.fake").write_text(f"Width\t{WIDTH}\nHeight\t{HEIGHT}\n")
    (rig / "octacam_config.toml").write_text("\n".join(lines) + "\n")
    return rig


def _record(rig, *args):
    return runner.invoke(app, ["record", str(rig), *args])


def _summary(save_dir) -> dict:
    return json.loads(
        (save_dir / "octacam_recording" / "recording_summary.json").read_text()
    )


def _text(result) -> str:
    """Everything the run printed, on one line: the log is wrapped to the
    console's width and may be colored."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).split())


def _videos(save_dir, serials=SERIALS) -> list[str]:
    return [str(save_dir / f"{serial}.raw") for serial in serials]


def test_record_writes_one_frame_per_pulse_from_every_camera(tmp_path):
    save_dir = tmp_path / "data" / "001"

    result = _record(_rig(tmp_path))

    assert result.exit_code == 0, result.output
    # stdout is only the videos, one per line, for scripts.
    assert result.stdout.splitlines() == _videos(save_dir)
    summary = _summary(save_dir)
    assert summary["completed"] is True
    assert summary["aborted"] is False
    assert summary["sync"]["ok"] is True
    assert summary["pulse_train"]["count"] == PULSES
    assert [c["serial"] for c in summary["cameras"]] == SERIALS
    for camera in summary["cameras"]:
        assert camera["frames"] == PULSES
        assert (camera["width"], camera["height"]) == (WIDTH, HEIGHT)
        video = save_dir / camera["file"]
        assert video.stat().st_size == PULSES * FRAME_BYTES


@pytest.mark.parametrize("args", [["--force"], []], ids=["force", "non-interactive"])
def test_record_on_an_incomplete_rig_records_the_cameras_it_has(tmp_path, args):
    save_dir = tmp_path / "data" / "001"

    result = _record(_rig(tmp_path, [*SERIALS, "FAKE-9"]), *args)

    assert result.exit_code == 0, result.output
    text = _text(result)
    assert "INCOMPLETE RIG" in text
    assert "FAKE-9 (not found)" in text
    assert "Recording with an incomplete rig" in text
    assert result.stdout.splitlines() == _videos(save_dir)
    summary = _summary(save_dir)
    assert [c["serial"] for c in summary["cameras"]] == SERIALS
    assert all(c["frames"] == PULSES for c in summary["cameras"])


@pytest.mark.parametrize(
    ("args", "warning"),
    [
        ([], "Save directory exists, data may be overwritten"),
        (["--force"], "Save directory exists; overwriting"),
    ],
    ids=["non-interactive", "force"],
)
def test_record_into_an_existing_save_directory_replaces_the_takes_files(
    tmp_path, args, warning
):
    save_dir = tmp_path / "data" / "001"
    save_dir.mkdir(parents=True)
    earlier_video = save_dir / "FAKE-0.raw"
    earlier_video.write_bytes(b"an earlier take")
    earlier_grid = save_dir / "grid.mp4"
    earlier_grid.write_bytes(b"an earlier take's grid")

    result = _record(_rig(tmp_path), *args)

    assert result.exit_code == 0, result.output
    assert warning in _text(result)
    assert _summary(save_dir)["completed"] is True
    # The take's own videos are rewritten, not appended to; nothing else goes.
    assert earlier_video.stat().st_size == PULSES * FRAME_BYTES
    assert earlier_grid.read_bytes() == b"an earlier take's grid"


def test_record_fails_when_a_camera_records_nothing(tmp_path, monkeypatch):
    from octacam.cameras import BackendError
    from octacam.cameras.fake import FakeBackend

    start_grab_record = FakeBackend.start_grab_record

    def refuse_one(self):
        if self.serial_number == "FAKE-1":
            raise BackendError("simulated: acquisition refused")
        return start_grab_record(self)

    monkeypatch.setattr(FakeBackend, "start_grab_record", refuse_one)
    save_dir = tmp_path / "data" / "001"

    result = _record(_rig(tmp_path))

    assert result.exit_code != 0
    assert "1 camera(s) recorded 0 frames (FAKE-1)" in _text(result)
    summary = _summary(save_dir)
    frames = {c["serial"]: c["frames"] for c in summary["cameras"]}
    assert frames == {"FAKE-0": PULSES, "FAKE-1": 0}
    # A camera that never started is a take short a camera, not a synced one.
    assert summary["sync"]["ok"] is False
    assert any("FAKE-1 did not start recording" in w for w in summary["sync"]["warnings"])
