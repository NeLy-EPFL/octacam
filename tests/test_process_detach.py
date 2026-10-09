"""CLI surface for detached processing: `process --detach`, `octacam jobs`, pause,
and the gui shutdown-and-process hand-off.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from octacam import process_jobs as pj
from octacam.cli import app, gui

runner = CliRunner()


# ------------------------------------------------------------- process --detach


def test_process_detach_spawns_with_absolute_paths(cache_dir, tmp_path, monkeypatch):
    folder = tmp_path / "rec"
    folder.mkdir()
    captured = {}

    def fake_spawn(*, argv_tail, folders, **kw):
        captured["argv_tail"] = argv_tail
        captured["folders"] = folders
        return pj.JobStatus(job_id="20260101T000000-1")

    monkeypatch.setattr(pj, "spawn_detached", fake_spawn)
    result = runner.invoke(
        app, ["process", str(folder), "--detach", "--no-grid", "--no-transfer"]
    )
    assert result.exit_code == 0
    assert "20260101T000000-1" in result.stdout  # id echoed for scripting
    # Cache selectors dropped; folder path is absolute; skip flags forwarded.
    assert str(folder.resolve()) in captured["argv_tail"]
    assert "--no-grid" in captured["argv_tail"]
    assert "--no-transfer" in captured["argv_tail"]


def test_process_options_argv_is_absolute_and_drops_raw_output(tmp_path, monkeypatch):
    from octacam.process import ProcessOptions

    folder = tmp_path / "r"
    folder.mkdir()
    monkeypatch.chdir(tmp_path)
    options = ProcessOptions(grid=False, force=True, raw_output=True)
    assert options.argv([Path("r")]) == ["--no-grid", "--force", str(folder.resolve())]
    every = ProcessOptions(
        transcode=False,
        grid=False,
        transfer=False,
        force=True,
        recursive=True,
        delete_source=True,
        dry_run=True,
        ignore_capture=True,
        config_dir=Path("r"),
    )
    assert every.argv([]) == [
        "--no-transcode",
        "--no-grid",
        "--no-transfer",
        "--force",
        "--recursive",
        "--delete-source",
        "--dry-run",
        "--ignore-capture",
        "--config",
        str(folder.resolve()),
    ]


# ------------------------------------------------------------- octacam jobs


def test_jobs_list_empty(cache_dir):
    result = runner.invoke(app, ["jobs", "list"])
    assert result.exit_code == 0
    assert "No processing jobs" in result.stdout


def test_jobs_attach_no_jobs(cache_dir):
    result = runner.invoke(app, ["jobs", "attach"])
    assert result.exit_code != 0


def test_jobs_cancel_delegates(cache_dir, monkeypatch):
    pj.write_status(
        pj.job_dir("live"), pj.JobStatus(job_id="live", state="running", pid=1)
    )
    monkeypatch.setattr(pj, "is_live", lambda jd: True)  # make it "live" for resolve
    called = {}
    monkeypatch.setattr(
        pj, "cancel", lambda job: called.setdefault("id", job.job_id) or True
    )
    result = runner.invoke(app, ["jobs", "cancel", "live"])
    assert result.exit_code == 0
    assert called["id"] == "live"


def test_jobs_pause_resume_delegate(cache_dir, monkeypatch):
    pj.write_status(
        pj.job_dir("live"), pj.JobStatus(job_id="live", state="running", pid=1)
    )
    monkeypatch.setattr(pj, "is_live", lambda jd: True)
    seen = []
    monkeypatch.setattr(
        pj, "pause", lambda job: seen.append(("pause", job.job_id)) or True
    )
    monkeypatch.setattr(
        pj, "resume", lambda job: seen.append(("resume", job.job_id)) or True
    )
    assert runner.invoke(app, ["jobs", "pause", "live"]).exit_code == 0
    assert runner.invoke(app, ["jobs", "resume", "live"]).exit_code == 0
    assert seen == [("pause", "live"), ("resume", "live")]


@pytest.mark.parametrize(
    ("verb", "message"),
    [
        ("pause", "Could not pause job live."),
        ("resume", "Could not resume job live."),
        ("cancel", "Could not cancel job live (it may have already finished)."),
    ],
)
def test_jobs_control_failures(cache_dir, monkeypatch, verb, message):
    none_live = runner.invoke(app, ["jobs", verb])
    assert none_live.exit_code == 1
    assert f"No live processing jobs to {verb}." in none_live.output
    unknown = runner.invoke(app, ["jobs", verb, "nope"])
    assert unknown.exit_code == 1
    assert "No such live job." in unknown.output

    pj.write_status(
        pj.job_dir("live"), pj.JobStatus(job_id="live", state="running", pid=1)
    )
    monkeypatch.setattr(pj, "is_live", lambda jd: True)
    monkeypatch.setattr(pj, verb, lambda job: False)
    refused = runner.invoke(app, ["jobs", verb, "live"])
    assert refused.exit_code == 1
    assert message in refused.output


# ------------------------------------------------------------- pause_gate


class _FakeReporter(pj.NullReporter):
    def __init__(self):
        self.calls = []

    def set_paused(self, flag, reason):
        self.calls.append((flag, reason))


def test_pause_gate_blocks_then_resumes(monkeypatch):
    from octacam import session_cache

    seq = iter([True, True, False])  # capture active twice, then clears
    monkeypatch.setattr(session_cache, "capture_active", lambda: next(seq, False))
    monkeypatch.setattr(pj.time, "sleep", lambda s: None)  # don't actually wait
    reporter = _FakeReporter()
    pj.pause_gate(reporter, unit="file")
    assert (True, "capture-active") in reporter.calls
    assert reporter.calls[-1] == (False, None)  # cleared on resume


def test_pause_gate_reports_both_reasons(cache_dir, monkeypatch):
    from octacam import session_cache

    jd = pj.job_dir("j")
    jd.mkdir(parents=True)
    status = pj.JobStatus(job_id="j")
    pj.pause(status)  # manual flag set
    seq = iter([True, False])
    monkeypatch.setattr(session_cache, "capture_active", lambda: next(seq, False))
    reasons = []

    def sleep(_seconds):
        reasons.append(pj.read_status(jd).paused_reason)
        pj.resume(status)

    monkeypatch.setattr(pj.time, "sleep", sleep)
    pj.pause_gate(pj.JobReporter(jd, status), unit="file")
    assert reasons == ["capture-active+manual"]
    assert pj.read_status(jd).paused is False


def test_pause_gate_ignore_capture_never_waits_on_a_capture(monkeypatch):
    from octacam import session_cache

    monkeypatch.setattr(session_cache, "capture_active", lambda: True)
    monkeypatch.setattr(pj.time, "sleep", lambda s: pytest.fail("must not wait"))
    pj.pause_gate(pj.NullReporter(), unit="file", ignore_capture=True)


def test_pause_gate_does_not_swallow_interrupt(monkeypatch):
    from octacam import session_cache

    monkeypatch.setattr(session_cache, "capture_active", lambda: True)

    def raise_ki(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(pj.time, "sleep", raise_ki)
    with pytest.raises(KeyboardInterrupt):
        pj.pause_gate(pj.NullReporter(), unit="file")


# ------------------------------------------------------------- _finish_gui_session


def test_finish_gui_session_spawns_when_process_after(cache_dir, tmp_path, monkeypatch):
    from octacam import session_cache

    folder = tmp_path / "rec"
    folder.mkdir()
    monkeypatch.setattr(session_cache, "session_folders", lambda sid: [folder])
    captured = {}
    monkeypatch.setattr(
        pj,
        "spawn_detached",
        lambda **kw: captured.update(kw) or pj.JobStatus(job_id="jid"),
    )
    gui._finish_gui_session("sess1", Path("/cfg"), process_after=True)
    assert captured["argv_tail"] == ["--session-id", "sess1", "--config", "/cfg"]
    assert captured["folders"] == [folder]


def test_finish_gui_session_prints_hints_when_not(cache_dir, monkeypatch):
    called = {"spawn": False, "hints": False}
    monkeypatch.setattr(pj, "spawn_detached", lambda **kw: called.update(spawn=True))
    monkeypatch.setattr(
        gui, "_print_transcode_hints", lambda sid: called.update(hints=True)
    )
    gui._finish_gui_session("sess1", Path("/cfg"), process_after=False)
    assert called == {"spawn": False, "hints": True}


def test_finish_gui_session_noop_without_recordings(cache_dir, monkeypatch):
    from octacam import session_cache

    monkeypatch.setattr(session_cache, "session_folders", lambda sid: [])
    monkeypatch.setattr(
        pj,
        "spawn_detached",
        lambda **kw: pytest.fail("must not spawn with no recordings"),
    )
    # Must not raise.
    gui._finish_gui_session("sess1", Path("/cfg"), process_after=True)
