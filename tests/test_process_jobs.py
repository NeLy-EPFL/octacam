"""The detached-processing job store (octacam.process_jobs) + worker path."""

import json
import os
import signal
import subprocess
import sys
import time

import numpy as np
import pytest
from typer.testing import CliRunner

from octacam import process_jobs as pj
from octacam.cli import app

runner = CliRunner()


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cache (and so the jobs dir) at a throwaway directory."""
    target = tmp_path / "cache"
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(target))
    return target


def _job(job_id="20260101T000000-1", **kw):
    return pj.JobStatus(job_id=job_id, **kw)


def _recording_folder(tmp_path, name="run1", frames=1):
    """A minimal .raw recording folder the transcoder can process."""
    folder = tmp_path / name
    folder.mkdir()
    frame = np.arange(12 * 16, dtype=np.uint8).reshape(12, 16)
    (folder / "cam.raw").write_bytes(frame.tobytes() * frames)
    (folder / "recording_summary.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "fps_target": 10.0,
                "cameras": [
                    {
                        "file": "cam.raw",
                        "width": 16,
                        "height": 12,
                        "pixel_format": "Mono8",
                        "fps": 10.0,
                        "frames": frames,
                        "transform": {"scale_x": 1, "scale_y": 1, "rotation_deg": 0},
                        "transform_applied": False,
                    }
                ],
            }
        )
    )
    return folder


# --------------------------------------------------------------- store I/O


def test_jobs_dir_honors_cache_override(cache_dir):
    assert pj.jobs_dir() == cache_dir / "jobs"


def test_status_round_trip(cache_dir):
    jd = pj.job_dir("j1")
    s = _job("j1", state="running", phase="transcode", percent=42.0, folders=["/a"])
    pj.write_status(jd, s)
    r = pj.read_status(jd)
    assert r is not None
    assert (r.state, r.phase, r.percent, r.folders) == ("running", "transcode", 42.0, ["/a"])


def test_read_status_tolerant(cache_dir):
    jd = pj.job_dir("j2")
    jd.mkdir(parents=True)
    assert pj.read_status(jd) is None  # missing
    pj._status_path(jd).write_text("{ not json")
    assert pj.read_status(jd) is None  # corrupt
    pj._status_path(jd).write_text(json.dumps({"no": "job_id"}))
    assert pj.read_status(jd) is None  # missing required field


def test_is_live_via_flock(cache_dir):
    jd = pj.job_dir("j3")
    jd.mkdir(parents=True)
    assert pj.is_live(jd) is False  # no lock file
    import fcntl

    handle = open(pj._lock_path(jd), "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        assert pj.is_live(jd) is True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    assert pj.is_live(jd) is False


# --------------------------------------------------------------- reconcile / admin


def test_list_jobs_reconciles_dead_running_to_failed(cache_dir):
    jd = pj.job_dir("j4")
    pj.write_status(jd, _job("j4", state="running", pid=999999))
    # No live lock and not terminal -> reported failed on read.
    (job,) = pj.list_jobs()
    assert job.state == "failed"
    assert job.error


def test_starting_grace_not_yet_failed(cache_dir, monkeypatch):
    jd = pj.job_dir("j5")
    pj.write_status(jd, _job("j5", state="starting"))
    # Fresh "starting" (within grace) is not yet declared dead.
    (job,) = pj.list_jobs()
    assert job.state == "starting"


def test_resolve_and_latest(cache_dir):
    pj.write_status(pj.job_dir("a"), _job("a", state="done", started="2026-01-01T00:00:00+00:00"))
    pj.write_status(pj.job_dir("b"), _job("b", state="running", started="2026-01-02T00:00:00+00:00"))
    assert pj.resolve_job("a").job_id == "a"
    assert pj.resolve_job("missing") is None
    # latest overall is b; latest *live* is b (a is terminal), require_live filters a.
    assert pj.latest_job().job_id == "b"
    assert pj.resolve_job("a", require_live=True) is None  # terminal


def test_prune_removes_old_finished(cache_dir):
    jd = pj.job_dir("old")
    pj.write_status(jd, _job("old", state="done"))
    old = time.time() - 40 * 86400
    os.utime(pj._status_path(jd), (old, old))
    pj.prune()
    assert not jd.exists()


def test_cancel_signals_only_when_live(cache_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(pj.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    monkeypatch.setattr(pj.os, "getpgid", lambda pid: pid)

    dead = _job("dead", pid=4242)
    assert pj.cancel(dead) is False  # not live -> no signal
    assert calls == []

    # A live job (hold its flock) is signalled.
    jd = pj.job_dir("live")
    jd.mkdir(parents=True)
    import fcntl

    handle = open(pj._lock_path(jd), "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        assert pj.cancel(_job("live", pid=4242)) is True
        assert calls == [(4242, signal.SIGINT)]
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_pause_resume_flag(cache_dir):
    jd = pj.job_dir("p")
    jd.mkdir(parents=True)
    s = _job("p")
    assert pj.pause(s) is True and pj.is_manually_paused(jd) is True
    assert pj.resume(s) is True and pj.is_manually_paused(jd) is False


# --------------------------------------------------------------- spawn (mocked)


def test_spawn_detached_writes_starting_and_builds_cmd(cache_dir, tmp_path, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 5555

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return FakeProc()

    monkeypatch.setattr(pj.subprocess, "Popen", fake_popen)
    folder = tmp_path / "rec"
    folder.mkdir()
    status = pj.spawn_detached(argv_tail=["--no-grid", str(folder)], folders=[folder])

    # A "starting" status.json exists with pid recorded.
    on_disk = pj.read_status(pj.job_dir(status.job_id))
    assert on_disk.state == "starting" and on_disk.pid == 5555
    # The child is invoked via `python -m octacam process ... --_job-dir <dir>`.
    cmd = captured["cmd"]
    assert cmd[0] == sys.executable and cmd[1:3] == ["-m", "octacam"]
    assert "process" in cmd and "--_job-dir" in cmd
    assert captured["kw"]["start_new_session"] is True
    # The child's log is a pipe, so color is forced on: log.txt keeps ANSI that
    # `jobs attach` replays in color.
    assert captured["kw"]["env"].get("FORCE_COLOR") == "1"


# --------------------------------------------------------------- attach helpers


def test_log_follower_line_buffering(cache_dir, tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("full line\npartial")  # no trailing newline
    f = pj._LogFollower(log)
    assert f.lines() == ["full line"]  # the partial tail is held back
    with log.open("a") as fh:
        fh.write(" now complete\nsecond\n")
    assert f.lines() == ["partial now complete", "second"]
    assert f.lines() == []  # nothing new
    # drain() surfaces a dangling partial once the job has ended.
    with log.open("a") as fh:
        fh.write("dangling")
    assert f.drain() == ["dangling"]


def test_bar_description_and_counts():
    running = _job(state="running", phase="transcode", current_file="cam3.mkv")
    assert pj._bar_description(running) == "transcode: cam3.mkv"
    assert pj._bar_counts(_job(files_done=2, files_total=6)) == "2/6"
    assert pj._bar_counts(_job(files_total=0)) == ""
    paused = _job(state="running", paused=True, paused_reason="capture-active")
    assert pj._bar_description(paused) == "paused (capture-active)"
    # A phase with no current file falls back to the phase name; none -> the state.
    assert pj._bar_description(_job(state="running", phase="grid")) == "grid"
    assert pj._bar_description(_job(state="starting")) == "starting"


def test_attach_follows_running_job_to_completion(cache_dir, monkeypatch, capsys):
    import fcntl

    from rich.console import Console

    jd = pj.job_dir("follow")
    jd.mkdir(parents=True)
    pj.write_status(jd, _job("follow", state="running", phase="transcode", pid=1))
    pj._log_path(jd).write_text("[transcode 1/1] cam0.mkv\n")
    # Hold the lock so the job reads as live until we flip it to done.
    handle = open(pj._lock_path(jd), "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def flip_to_done(_):
        pj.write_status(jd, _job("follow", state="done", phase="transcode"))

    monkeypatch.setattr(pj.time, "sleep", flip_to_done)  # end the follow loop
    try:
        rc = pj.attach(_job("follow", state="running", pid=1), Console())
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    out = capsys.readouterr().out
    assert rc == 0
    assert "[transcode 1/1] cam0.mkv" in out  # tailed log line
    assert "finished" in out  # final outcome after the bar tears down


# --------------------------------------------------------------- worker e2e


def _run_worker(job_id, folder, *extra):
    jd = pj.job_dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    pj.write_status(jd, _job(job_id, folders=[str(folder)]))
    result = runner.invoke(
        app, ["process", str(folder), "--_job-dir", str(jd), *extra]
    )
    return jd, result


def test_worker_transcodes_and_marks_done(cache_dir, tmp_path):
    folder = _recording_folder(tmp_path)
    jd, result = _run_worker("w1", folder, "--no-grid", "--no-transfer")
    assert result.exit_code == 0, result.output
    s = pj.read_status(jd)
    assert s.state == "done" and s.files_done == s.files_total == 1
    assert s.percent == 100.0
    assert (folder / "cam.mp4").exists()


def test_worker_failure_marks_failed(cache_dir, tmp_path, monkeypatch):
    folder = _recording_folder(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("ffmpeg blew up")

    monkeypatch.setattr("octacam.writer.transcode_file", boom)
    jd, result = _run_worker("w2", folder, "--no-grid", "--no-transfer")
    assert result.exit_code != 0
    s = pj.read_status(jd)
    assert s.state == "failed" and s.error


def test_worker_interrupt_marks_cancelled(cache_dir, tmp_path, monkeypatch):
    folder = _recording_folder(tmp_path)

    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("octacam.writer.transcode_file", interrupt)
    jd, result = _run_worker("w3", folder, "--no-grid", "--no-transfer")
    assert result.exit_code == 130
    s = pj.read_status(jd)
    assert s.state == "cancelled"


# --------------------------------------------------------------- attach


def test_attach_replays_finished_job(cache_dir, capsys):
    from rich.console import Console

    jd = pj.job_dir("done1")
    jd.mkdir(parents=True)
    pj.write_status(jd, _job("done1", state="done"))
    pj._log_path(jd).write_text("transcoding cam0.mkv\nfinished\n")
    rc = pj.attach(_job("done1", state="done"), Console())
    out = capsys.readouterr().out
    assert rc == 0
    assert "transcoding cam0.mkv" in out  # replayed log
    assert "finished" in out


def test_attach_ctrl_c_detaches_without_cancel(cache_dir, monkeypatch, capsys):
    from rich.console import Console

    jd = pj.job_dir("run")
    jd.mkdir(parents=True)
    pj.write_status(jd, _job("run", state="running", pid=1))
    pj._log_path(jd).write_text("working…\n")
    monkeypatch.setattr(pj, "is_live", lambda d: True)  # keep the follow loop going

    def raise_ki(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(pj.time, "sleep", raise_ki)  # Ctrl-C on the first poll
    rc = pj.attach(_job("run", state="running", pid=1), Console())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Detached from job" in out  # detached, not cancelled
    assert pj.read_status(jd).state == "running"  # job untouched


def test_attach_dead_running_job_reports_failed_no_hang(cache_dir, capsys):
    from rich.console import Console

    jd = pj.job_dir("dead")
    jd.mkdir(parents=True)
    pj.write_status(jd, _job("dead", state="running", pid=999999))
    # No live lock -> reconciled to failed -> attach prints outcome and returns.
    rc = pj.attach(_job("dead", state="running", pid=999999), Console())
    assert rc == 0
    assert "failed" in capsys.readouterr().out.lower()


# --------------------------------------------------------------- entrypoint


def test_python_dash_m_octacam_runs():
    r = subprocess.run(
        [sys.executable, "-m", "octacam", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0
    assert r.stdout.strip()  # prints a version
