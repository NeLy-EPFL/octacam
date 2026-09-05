"""The `octacam cache` command group (info / path / clear) and its primitives.

Covers the safe-clear contract: the recording list and stale markers go, but a
live capture, transcode, or detached job is never touched.
"""

import fcntl

import pytest
from typer.testing import CliRunner

from octacam import process_jobs as pj
from octacam import session_cache
from octacam.cli import _human_size, app

runner = CliRunner()


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cache (and so the jobs dir) at a throwaway directory."""
    target = tmp_path / "cache"
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(target))
    return target


def _job_status(job_id, **kw):
    return pj.JobStatus(job_id=job_id, **kw)


def _finished_job(job_id="fin1"):
    """A finished (unlocked) job directory on disk."""
    jd = pj.job_dir(job_id)
    jd.mkdir(parents=True)
    pj.write_status(jd, _job_status(job_id, state="done", phase="transcode"))
    return jd


def _job_with_state(job_id, state, *, age_s=0.0):
    """A job dir in ``state`` with no lock held, optionally aged ``age_s`` seconds."""
    jd = pj.job_dir(job_id)
    jd.mkdir(parents=True)
    pj.write_status(jd, _job_status(job_id, state=state))
    if age_s:
        import os

        old = session_cache._now().timestamp() - age_s
        os.utime(pj._status_path(jd), (old, old))
        os.utime(jd, (old, old))
    return jd


class _LiveJob:
    """Context manager holding a job's flock so it reads as live."""

    def __init__(self, job_id="live1"):
        self.jd = pj.job_dir(job_id)
        self.jd.mkdir(parents=True)
        pj.write_status(self.jd, _job_status(job_id, state="running", pid=1))

    def __enter__(self):
        self.handle = open(pj._lock_path(self.jd), "a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self.jd

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def _stale_marker(directory):
    """Drop an orphaned (unlocked), clearly-old marker into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "999999-dead.lock"
    marker.write_text("999999 crashed\n")
    import os

    old = session_cache._now().timestamp() - 3600
    os.utime(marker, (old, old))
    return marker


# --------------------------------------------------------------- primitives


def test_human_size():
    assert _human_size(0) == "0 B"
    assert _human_size(512) == "512 B"
    assert _human_size(7168) == "7.0 KB"
    assert _human_size(2 * 1024 * 1024) == "2.0 MB"


def test_dir_size_file_tree_and_missing(cache_dir, tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x" * 100)
    assert session_cache.dir_size(f) == 100

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a").write_bytes(b"y" * 10)
    (tree / "sub" / "b").write_bytes(b"z" * 5)
    assert session_cache.dir_size(tree) == 15

    assert session_cache.dir_size(tmp_path / "nope") == 0


def test_recordings_count_and_clear(cache_dir, tmp_path):
    rec = tmp_path / "run1"
    rec.mkdir()
    session_cache.record_recording(rec, "sess", "gui")
    assert session_cache.recordings_count() == 1

    assert session_cache.clear_recordings() is True
    assert session_cache.recordings_count() == 0
    assert not (cache_dir / session_cache.CACHE_FILENAME).exists()
    # Clearing an already-clear cache is a no-op that reports nothing removed.
    assert session_cache.clear_recordings() is False


def test_clear_recordings_sweeps_crashed_temp(cache_dir, tmp_path):
    rec = tmp_path / "run1"
    rec.mkdir()
    session_cache.record_recording(rec, "sess", "gui")
    leftover = cache_dir / f".{session_cache.CACHE_FILENAME}.999.abcdef.tmp"
    leftover.write_text("half-written\n")

    session_cache.clear_recordings()
    assert not leftover.exists()


def test_sweep_orphan_markers_removes_stale_keeps_live_and_fresh(cache_dir):
    _stale_marker(session_cache._transcode_dir())
    # A fresh (not-yet-locked) orphan is within the mid-publish window: keep it.
    fresh = session_cache._capture_dir()
    fresh.mkdir(parents=True, exist_ok=True)
    (fresh / "123-publishing.lock").write_text("123\n")

    with session_cache.mark_capture_active("live gui"):
        removed, live = session_cache.sweep_orphan_markers()

    assert removed == 1  # only the clearly-stale transcode orphan
    assert live == 1  # the held capture marker
    assert (fresh / "123-publishing.lock").exists()  # fresh orphan untouched


def test_clear_finished_and_counts_protect_live_jobs(cache_dir):
    _finished_job("fin1")
    with _LiveJob("live1"):
        assert pj.job_dir_counts() == (1, 1)  # (live, finished)
        removed, live = pj.clear_finished()
        assert (removed, live) == (1, 1)
        assert not pj.job_dir("fin1").exists()  # finished job removed
        assert pj.job_dir("live1").exists()  # the running job's dir survives


def test_clear_finished_protects_just_spawned_job_in_grace(cache_dir):
    # A detached job writes `starting` status, then takes job.lock only ~1s later
    # (after the child boots). During that window is_live() is False yet the job is
    # very much alive: it must be treated as live, never cleared.
    jd = _job_with_state("arming1", "starting")  # fresh -> within the starting grace
    assert pj.is_live(jd) is False  # no lock yet
    assert pj.job_dir_counts() == (1, 0)  # counted as live, not finished
    removed, kept = pj.clear_finished()
    assert (removed, kept) == (0, 1)  # kept: never deleted out from under the worker
    assert jd.exists()


def test_clear_finished_removes_crashed_running_job(cache_dir):
    # status says `running` but no lock is held and it is well past the grace: the
    # worker died. Reconcile treats it as failed, so it is removable.
    jd = _job_with_state("crashed1", "running", age_s=120.0)
    assert pj.job_dir_counts() == (0, 1)
    removed, kept = pj.clear_finished()
    assert (removed, kept) == (1, 0)
    assert not jd.exists()


def test_clear_finished_removes_starting_job_past_grace(cache_dir):
    # A `starting` job that never took the lock and is now old (child failed to
    # boot) is a dead husk — removable.
    jd = _job_with_state("stuck1", "starting", age_s=120.0)
    removed, kept = pj.clear_finished()
    assert (removed, kept) == (1, 0)
    assert not jd.exists()


# ----------------------------------------------------------------- cache path


def test_cache_path_prints_dir(cache_dir):
    result = runner.invoke(app, ["cache", "path"])
    assert result.exit_code == 0
    assert result.output.strip() == str(cache_dir)


# ----------------------------------------------------------------- cache info


def test_cache_info_empty(cache_dir):
    result = runner.invoke(app, ["cache", "info"])
    assert result.exit_code == 0
    assert "nothing cached yet" in result.output


def test_cache_info_breakdown(cache_dir, tmp_path):
    rec = tmp_path / "run1"
    rec.mkdir()
    session_cache.record_recording(rec, "sess", "gui")
    _finished_job("fin1")

    result = runner.invoke(app, ["cache", "info"])
    assert result.exit_code == 0
    out = result.output
    assert str(cache_dir) in out
    assert "recordings" in out and "1 entry" in out
    assert "jobs" in out and "0 live, 1 finished" in out
    assert "markers" in out


def test_cache_info_reports_last_used(cache_dir):
    session_cache.save_last_used(fps=30.0, duration_s=60.0, user="MD")
    result = runner.invoke(app, ["cache", "info"])
    assert result.exit_code == 0
    assert "last-used" in result.output
    assert "fps=30" in result.output
    assert "duration=60s" in result.output
    assert "user=MD" in result.output


def test_cache_info_no_last_used_row_when_none_cached(cache_dir, tmp_path):
    rec = tmp_path / "run1"
    rec.mkdir()
    session_cache.record_recording(rec, "sess", "gui")  # cache dir exists, but no last-used yet
    result = runner.invoke(app, ["cache", "info"])
    assert "last-used" not in result.output


# ---------------------------------------------------------------- cache clear


def test_cache_clear_yes_removes_recording_list(cache_dir, tmp_path):
    rec = tmp_path / "run1"
    rec.mkdir()
    session_cache.record_recording(rec, "sess", "gui")

    result = runner.invoke(app, ["cache", "clear", "--yes"])
    assert result.exit_code == 0, result.output
    assert "recording list" in result.output
    assert session_cache.recordings_count() == 0


def test_cache_clear_confirm_abort_keeps_everything(cache_dir, tmp_path):
    rec = tmp_path / "run1"
    rec.mkdir()
    session_cache.record_recording(rec, "sess", "gui")

    result = runner.invoke(app, ["cache", "clear"], input="n\n")
    assert result.exit_code == 1  # typer.Abort
    assert session_cache.recordings_count() == 1  # nothing was cleared


def test_cache_clear_keeps_finished_jobs_without_all(cache_dir):
    _finished_job("fin1")
    result = runner.invoke(app, ["cache", "clear", "--yes"])
    assert result.exit_code == 0, result.output
    assert "use --all to remove" in result.output
    assert pj.job_dir("fin1").exists()


def test_cache_clear_all_removes_finished_jobs(cache_dir):
    _finished_job("fin1")
    result = runner.invoke(app, ["cache", "clear", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert "finished job log" in result.output
    assert not pj.job_dir("fin1").exists()


def test_cache_clear_protects_live_job(cache_dir):
    with _LiveJob("live1"):
        result = runner.invoke(app, ["cache", "clear", "--all", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Kept:" in result.output and "1 live job" in result.output
        assert pj.job_dir("live1").exists()


def test_cache_clear_yes_removes_last_used(cache_dir):
    session_cache.save_last_used(fps=30.0)

    result = runner.invoke(app, ["cache", "clear", "--yes"])
    assert result.exit_code == 0, result.output
    assert "last-used settings" in result.output
    assert session_cache.load_last_used() == {}


def test_cache_clear_prompt_lists_last_used(cache_dir):
    session_cache.save_last_used(fps=30.0)
    result = runner.invoke(app, ["cache", "clear"], input="n\n")
    assert result.exit_code == 1  # typer.Abort
    assert "last-used fps/duration/profile" in result.output
    assert session_cache.load_last_used() == {"fps": 30.0}  # nothing was cleared


def test_cache_clear_nothing_needed(cache_dir):
    result = runner.invoke(app, ["cache", "clear", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Nothing needed clearing" in result.output


def test_cache_info_and_clear_report_live_capture(cache_dir):
    # A live capture holds its marker: info counts it and clear protects it.
    with session_cache.mark_capture_active("gui"):
        info = runner.invoke(app, ["cache", "info"])
        assert info.exit_code == 0, info.output
        assert "markers" in info.output and "1 live" in info.output

        clr = runner.invoke(app, ["cache", "clear", "--yes"])
        assert clr.exit_code == 0, clr.output
        assert "Kept:" in clr.output and "1 live capture" in clr.output
