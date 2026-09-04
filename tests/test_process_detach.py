"""CLI surface for detached processing: `process --detach`, `octacam jobs`, pause,
and the gui shutdown-and-process hand-off."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from octacam import cli
from octacam import process_jobs as pj
from octacam.cli import app

runner = CliRunner()


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


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


def test_rebuild_process_argv_is_absolute_and_drops_selectors(tmp_path):
    folder = tmp_path / "r"
    folder.mkdir()
    argv = cli._rebuild_process_argv(
        [folder],
        recursive=False,
        no_transcode=False,
        no_grid=True,
        no_transfer=False,
        force=True,
        config_dir=None,
        delete_source=False,
        no_delete_source=False,
        delete_after_transfer=False,
        no_delete_after_transfer=False,
        no_twophoton_sweep=False,
        dry_run=False,
    )
    assert argv == ["--no-grid", "--force", str(folder.resolve())]


def test_rebuild_process_argv_forwards_new_delete_flags(tmp_path):
    folder = tmp_path / "r"
    folder.mkdir()
    argv = cli._rebuild_process_argv(
        [folder],
        recursive=False,
        no_transcode=False,
        no_grid=False,
        no_transfer=False,
        force=False,
        config_dir=None,
        delete_source=False,
        no_delete_source=True,
        delete_after_transfer=True,
        no_delete_after_transfer=False,
        no_twophoton_sweep=False,
        dry_run=False,
    )
    assert "--no-delete-source" in argv
    assert "--delete-after-transfer" in argv
    assert "--no-delete-after-transfer" not in argv


def test_rebuild_process_argv_forwards_no_twophoton_sweep(tmp_path):
    folder = tmp_path / "r"
    folder.mkdir()
    argv = cli._rebuild_process_argv(
        [folder],
        recursive=False,
        no_transcode=False,
        no_grid=False,
        no_transfer=False,
        force=False,
        config_dir=None,
        delete_source=False,
        no_delete_source=False,
        delete_after_transfer=False,
        no_delete_after_transfer=False,
        no_twophoton_sweep=True,
        dry_run=False,
    )
    assert "--no-twophoton-sweep" in argv


# ------------------------------------------------------------- octacam jobs


def test_jobs_list_empty(cache_dir):
    result = runner.invoke(app, ["jobs", "list"])
    assert result.exit_code == 0
    assert "No processing jobs" in result.stdout


def test_jobs_attach_no_jobs(cache_dir):
    result = runner.invoke(app, ["jobs", "attach"])
    assert result.exit_code != 0


def test_jobs_cancel_delegates(cache_dir, monkeypatch):
    pj.write_status(pj.job_dir("live"), pj.JobStatus(job_id="live", state="running", pid=1))
    monkeypatch.setattr(pj, "is_live", lambda jd: True)  # make it "live" for resolve
    called = {}
    monkeypatch.setattr(pj, "cancel", lambda job: called.setdefault("id", job.job_id) or True)
    result = runner.invoke(app, ["jobs", "cancel", "live"])
    assert result.exit_code == 0
    assert called["id"] == "live"


def test_jobs_pause_resume_delegate(cache_dir, monkeypatch):
    pj.write_status(pj.job_dir("live"), pj.JobStatus(job_id="live", state="running", pid=1))
    monkeypatch.setattr(pj, "is_live", lambda jd: True)
    seen = []
    monkeypatch.setattr(pj, "pause", lambda job: seen.append(("pause", job.job_id)) or True)
    monkeypatch.setattr(pj, "resume", lambda job: seen.append(("resume", job.job_id)) or True)
    assert runner.invoke(app, ["jobs", "pause", "live"]).exit_code == 0
    assert runner.invoke(app, ["jobs", "resume", "live"]).exit_code == 0
    assert seen == [("pause", "live"), ("resume", "live")]


# ------------------------------------------------------------- _pause_gate


class _FakeReporter:
    def __init__(self):
        self.calls = []

    def set_paused(self, flag, reason):
        self.calls.append((flag, reason))


def test_pause_gate_blocks_then_resumes(monkeypatch):
    from octacam import session_cache

    seq = iter([True, True, False])  # capture active twice, then clears
    monkeypatch.setattr(session_cache, "capture_active", lambda: next(seq, False))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)  # don't actually wait
    reporter = _FakeReporter()
    cli._pause_gate(reporter, None, unit="file")
    assert (True, "capture-active") in reporter.calls
    assert reporter.calls[-1] == (False, None)  # cleared on resume


def test_pause_gate_reports_both_reasons(cache_dir, monkeypatch):
    from octacam import session_cache

    jd = pj.job_dir("j")
    jd.mkdir(parents=True)
    pj.pause(pj.JobStatus(job_id="j"))  # manual flag set
    seq = iter([True, False])
    monkeypatch.setattr(session_cache, "capture_active", lambda: next(seq, False))
    monkeypatch.setattr(pj, "is_manually_paused", lambda d: False)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    reporter = _FakeReporter()
    cli._pause_gate(reporter, jd, unit="file")
    assert reporter.calls[0] == (True, "capture-active")


def test_pause_gate_does_not_swallow_interrupt(monkeypatch):
    from octacam import session_cache

    monkeypatch.setattr(session_cache, "capture_active", lambda: True)

    def raise_ki(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", raise_ki)
    with pytest.raises(KeyboardInterrupt):
        cli._pause_gate(None, None, unit="file")


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
    cli._finish_gui_session("sess1", Path("/cfg"), process_after=True)
    assert captured["argv_tail"] == ["--session-id", "sess1", "--config", "/cfg"]
    assert captured["folders"] == [folder]


def test_finish_gui_session_prints_hints_when_not(cache_dir, monkeypatch):
    called = {"spawn": False, "hints": False}
    monkeypatch.setattr(pj, "spawn_detached", lambda **kw: called.update(spawn=True))
    monkeypatch.setattr(cli, "_print_transcode_hints", lambda sid: called.update(hints=True))
    cli._finish_gui_session("sess1", Path("/cfg"), process_after=False)
    assert called == {"spawn": False, "hints": True}


def test_finish_gui_session_noop_without_recordings(cache_dir, monkeypatch):
    from octacam import session_cache

    monkeypatch.setattr(session_cache, "session_folders", lambda sid: [])
    monkeypatch.setattr(
        pj, "spawn_detached", lambda **kw: pytest.fail("must not spawn with no recordings")
    )
    # Must not raise.
    cli._finish_gui_session("sess1", Path("/cfg"), process_after=True)
