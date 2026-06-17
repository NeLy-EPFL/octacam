"""The recording-session cache that backs `octacam transcode --last/--session/--all`."""

import datetime
import os

import pytest

from octacam import session_cache


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cache at a throwaway directory for the duration of a test."""
    target = tmp_path / "cache"
    monkeypatch.setenv("OCTACAM_CACHE_DIR", str(target))
    return target


def _make(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_cache_dir_honors_override(cache_dir):
    assert session_cache.cache_dir() == cache_dir


def test_xdg_cache_home_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("OCTACAM_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert session_cache.cache_dir() == tmp_path / "xdg" / "octacam"


def test_record_and_last_folder(cache_dir, tmp_path):
    a, b = _make(tmp_path, "a"), _make(tmp_path, "b")
    session_cache.record_recording(a, "s1")
    session_cache.record_recording(b, "s1")
    assert session_cache.last_folder() == b.resolve()


def test_last_folder_none_when_empty(cache_dir):
    assert session_cache.last_folder() is None


def test_last_folder_skips_deleted(cache_dir, tmp_path):
    a, b = _make(tmp_path, "a"), _make(tmp_path, "b")
    session_cache.record_recording(a, "s1")
    session_cache.record_recording(b, "s1")
    b.rmdir()  # the most recent folder is gone -> fall back to the previous one
    assert session_cache.last_folder() == a.resolve()


def test_session_folders_groups_and_orders(cache_dir, tmp_path):
    a, b, c = (_make(tmp_path, n) for n in "abc")
    session_cache.record_recording(a, "s1")
    session_cache.record_recording(b, "s2")
    session_cache.record_recording(c, "s2")
    # No id -> the most recent session (s2), in record order.
    assert session_cache.session_folders() == [b.resolve(), c.resolve()]
    # Explicit id selects that session.
    assert session_cache.session_folders("s1") == [a.resolve()]


def test_session_folders_skips_deleted(cache_dir, tmp_path):
    a, b = _make(tmp_path, "a"), _make(tmp_path, "b")
    session_cache.record_recording(a, "s1")
    session_cache.record_recording(b, "s1")
    a.rmdir()
    assert session_cache.session_folders("s1") == [b.resolve()]


def test_session_folders_empty_for_unknown(cache_dir, tmp_path):
    session_cache.record_recording(_make(tmp_path, "a"), "s1")
    assert session_cache.session_folders("nope") == []


def test_all_folders_spans_sessions_in_record_order(cache_dir, tmp_path):
    a, b, c = (_make(tmp_path, n) for n in "abc")
    session_cache.record_recording(a, "s1")
    session_cache.record_recording(b, "s2")
    session_cache.record_recording(c, "s2")
    assert session_cache.all_folders() == [a.resolve(), b.resolve(), c.resolve()]


def test_all_folders_empty_when_no_cache(cache_dir):
    assert session_cache.all_folders() == []


def test_all_folders_skips_deleted_and_dedups(cache_dir, tmp_path):
    a, b = _make(tmp_path, "a"), _make(tmp_path, "b")
    session_cache.record_recording(a, "s1")
    session_cache.record_recording(a, "s1")  # same folder twice -> one entry
    session_cache.record_recording(b, "s2")
    b.rmdir()  # gone since recording -> dropped
    assert session_cache.all_folders() == [a.resolve()]


def test_dedup_same_folder_recorded_twice(cache_dir, tmp_path):
    # Aborting reuses the same save dir (no increment), so the folder can appear
    # twice; queries must collapse it to one.
    a = _make(tmp_path, "a")
    session_cache.record_recording(a, "s1")
    session_cache.record_recording(a, "s1")
    assert session_cache.session_folders("s1") == [a.resolve()]


def test_retention_prunes_old_entries(cache_dir, tmp_path, monkeypatch):
    old = _make(tmp_path, "old")
    fresh = _make(tmp_path, "fresh")
    # Stamp the first write 60 days in the past, then a normal write today.
    real_now = session_cache._now()
    monkeypatch.setattr(
        session_cache, "_now", lambda: real_now - datetime.timedelta(days=60)
    )
    session_cache.record_recording(old, "s_old", retention_days=30)
    monkeypatch.setattr(session_cache, "_now", lambda: real_now)
    session_cache.record_recording(fresh, "s_new", retention_days=30)

    entries = session_cache._read_entries()
    assert [e["session"] for e in entries] == ["s_new"]  # the 60-day-old one pruned
    assert session_cache.last_folder() == fresh.resolve()


def test_corrupt_lines_are_skipped(cache_dir, tmp_path):
    a = _make(tmp_path, "a")
    session_cache.record_recording(a, "s1")
    # A half-written/garbage trailing line (e.g. from a crash) must not break reads.
    with open(session_cache._cache_file(), "a") as handle:
        handle.write("{not json\n")
    assert session_cache.last_folder() == a.resolve()


def test_new_session_id_is_unique_ish(cache_dir):
    assert session_cache.new_session_id() != ""


# --------------------------------------------------- transcode-activity markers


def test_transcode_running_zero_when_idle(cache_dir):
    assert session_cache.transcode_running() == 0


def test_transcode_running_detects_active_marker(cache_dir):
    assert session_cache.transcode_running() == 0
    with session_cache.mark_transcode_active("2 file(s)"):
        # A second descriptor on the same flock'd marker cannot acquire it, so
        # the run is detected as live.
        assert session_cache.transcode_running() == 1
    # The marker is released and removed on exit.
    assert session_cache.transcode_running() == 0


def test_transcode_running_ignores_and_cleans_stale_marker(cache_dir):
    directory = session_cache._transcode_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stale = directory / "999999-dead.lock"  # nobody holds its flock
    stale.write_text("999999 crashed\n")
    old = session_cache._now().timestamp() - 3600
    os.utime(stale, (old, old))
    assert session_cache.transcode_running() == 0  # not counted as live
    assert not stale.exists()  # and swept away once clearly stale


def test_transcode_running_keeps_fresh_unlocked_marker(cache_dir):
    # A just-created, not-yet-locked marker (mid-publish) must not be deleted.
    directory = session_cache._transcode_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fresh = directory / "12345-publishing.lock"
    fresh.write_text("12345\n")
    assert session_cache.transcode_running() == 0
    assert fresh.exists()
