"""Discovery/settle/match logic for pairing ThorSync/ThorImage folders with
octacam takes — all synthetic folder trees in tmp_path, no real 2P hardware
or hardware-derived fixtures needed."""

import os
import time
from pathlib import Path

import pytest

from octacam.twophoton_transfer import (
    TakeInfo,
    TwoPhotonFolder,
    build_match_record,
    discover_twophoton_folders,
    is_settled,
    match_take_to_twophoton,
    match_takes_to_twophoton_batch,
)


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


def _make_sync_folder(root: Path, experiment: str, name: str, mtime: float) -> Path:
    folder = root / experiment / name
    _touch(folder / "ThorRealTimeDataSettings.xml", mtime)
    _touch(folder / "Episode001.h5", mtime)
    return folder


def _make_image_folder(
    root: Path, experiment: str, name: str, u_time: float, last_frame_mtime: float
) -> Path:
    folder = root / experiment / name
    _touch(
        folder / "Experiment.xml",
        last_frame_mtime,  # Experiment.xml itself lands near the end, not the start
    )
    (folder / "Experiment.xml").write_text(
        f'<?xml version="1.0"?><ThorImageExperiment>'
        f'<Date date="x" uTime="{u_time}" /></ThorImageExperiment>'
    )
    os.utime(folder / "Experiment.xml", (last_frame_mtime, last_frame_mtime))
    _touch(folder / "ChanA_001_001_001_001.tif", last_frame_mtime)
    return folder


# --- discover_twophoton_folders ----------------------------------------------


def test_discover_finds_sync_and_image_folders(tmp_path):
    _make_sync_folder(tmp_path, "MB247_CI63", "SyncData102", mtime=1000.0)
    _make_image_folder(
        tmp_path, "MB247_CI63", "Fly1_004", u_time=1000.0, last_frame_mtime=1200.0
    )
    found = discover_twophoton_folders(tmp_path)
    kinds = {f.kind: f for f in found}
    assert set(kinds) == {"sync", "image"}
    assert kinds["sync"].path.name == "SyncData102"
    assert kinds["image"].path.name == "Fly1_004"


def test_discover_image_start_time_from_experiment_xml_not_directory_mtime(tmp_path):
    # ThorImage's directory mtime tracks the LAST frame written (thousands of
    # tif files keep bumping it) — the real start must come from Experiment.xml.
    _make_image_folder(
        tmp_path, "MB247_CI63", "Fly1_004", u_time=1000.0, last_frame_mtime=1200.0
    )
    (folder,) = discover_twophoton_folders(tmp_path)
    assert folder.start_time == 1000.0
    assert folder.last_mtime == 1200.0


def test_discover_image_falls_back_to_directory_mtime_without_utime(tmp_path):
    folder = tmp_path / "exp" / "Fly1"
    _touch(folder / "Experiment.xml", 500.0)
    (folder / "Experiment.xml").write_text("<ThorImageExperiment/>")  # no <Date>
    os.utime(folder / "Experiment.xml", (500.0, 500.0))
    (found,) = discover_twophoton_folders(tmp_path)
    assert found.kind == "image"
    assert found.start_time == found.path.stat().st_mtime


def test_discover_ignores_unrecognized_folders(tmp_path):
    (tmp_path / "exp" / "not_2p_data").mkdir(parents=True)
    (tmp_path / "exp" / "not_2p_data" / "readme.txt").write_text("hi")
    assert discover_twophoton_folders(tmp_path) == []


def test_discover_missing_source_root_returns_empty(tmp_path):
    assert discover_twophoton_folders(tmp_path / "does-not-exist") == []


def test_discover_last_mtime_is_max_across_nested_files(tmp_path):
    folder = _make_sync_folder(tmp_path, "exp", "SyncData001", mtime=1000.0)
    _touch(folder / "later.txt", 1500.0)
    (found,) = discover_twophoton_folders(tmp_path)
    assert found.last_mtime == 1500.0


# --- is_settled ---------------------------------------------------------------


def test_is_settled_true_after_quiescence():
    folder = TwoPhotonFolder(Path("x"), "sync", start_time=0.0, last_mtime=1000.0)
    assert is_settled(folder, settle_s=300.0, now=1301.0) is True


def test_is_settled_false_while_recently_touched():
    folder = TwoPhotonFolder(Path("x"), "sync", start_time=0.0, last_mtime=1000.0)
    assert is_settled(folder, settle_s=300.0, now=1200.0) is False


def test_is_settled_defaults_now_to_current_time():
    folder = TwoPhotonFolder(Path("x"), "sync", start_time=0.0, last_mtime=time.time())
    assert is_settled(folder, settle_s=300.0) is False


# --- match_take_to_twophoton ---------------------------------------------------


def test_match_finds_sync_and_image_within_window():
    # take = [1000, 1180]. sync's worse endpoint deviation: max(|1036-1000|=36,
    # |1250-1180|=70) = 70. image's: max(|1000-1000|=0, |1240-1180|=60) = 60.
    # Both within match_window_s=120, so both match — gap_s reports the
    # (nonzero) endpoint drift, not just "0 because it overlaps somewhere".
    sync = TwoPhotonFolder(Path("s"), "sync", start_time=1036.0, last_mtime=1250.0)
    image = TwoPhotonFolder(Path("i"), "image", start_time=1000.0, last_mtime=1240.0)
    matches = match_take_to_twophoton(
        take_start=1000.0, take_duration_s=180.0, candidates=[sync, image],
        match_window_s=120.0,
    )
    by_kind = {m.folder.kind: m for m in matches}
    assert set(by_kind) == {"sync", "image"}
    assert all(m.ambiguous is False for m in matches)
    assert by_kind["sync"].gap_s == 70.0
    assert by_kind["image"].gap_s == 60.0


def test_match_respects_window_and_excludes_far_folders():
    far = TwoPhotonFolder(Path("far"), "sync", start_time=10_000.0, last_mtime=10_100.0)
    matches = match_take_to_twophoton(
        take_start=0.0, take_duration_s=10.0, candidates=[far], match_window_s=60.0,
    )
    assert matches == []


def test_match_returns_nothing_when_no_candidates_overlap():
    assert match_take_to_twophoton(0.0, 10.0, [], match_window_s=60.0) == []


def test_match_picks_closest_and_flags_ambiguous():
    # Both candidates have the take's own 10s duration (isolating start/end
    # proximity as the only thing to disambiguate) and both land within
    # match_window_s=120 — a genuine tie; "far" in the old, loose-overlap
    # sense (its own window barely touches the take's padded window) is
    # excluded entirely by the stricter checks, so it isn't a useful
    # ambiguity case any more.
    take_start, take_duration = 1000.0, 10.0
    closer = TwoPhotonFolder(Path("closer"), "sync", start_time=995.0, last_mtime=1005.0)
    farther = TwoPhotonFolder(Path("farther"), "sync", start_time=1010.0, last_mtime=1020.0)
    matches = match_take_to_twophoton(
        take_start, take_duration, [farther, closer], match_window_s=120.0
    )
    assert len(matches) == 1
    assert matches[0].folder is closer


def test_match_excludes_much_longer_candidate_straddling_a_short_take():
    # A pathological case start/end-closeness alone can miss: a candidate
    # much LONGER than the take, positioned so both its start and its end are
    # each individually "close enough", while its own duration is nothing
    # like the take's — e.g. a candidate padded 40s before and 40s after a
    # 10s take (both endpoint deviations = 40, comfortably under a 60s
    # window) but running 90s total, 9x the take's length.
    take_start, take_duration = 1000.0, 10.0  # take = [1000, 1010]
    much_longer = TwoPhotonFolder(
        Path("long"), "sync", start_time=960.0, last_mtime=1050.0  # 90s duration
    )
    matches = match_take_to_twophoton(
        take_start, take_duration, [much_longer], match_window_s=60.0
    )
    assert matches == []


def test_match_excludes_short_unrelated_acquisition_inside_a_long_take():
    # Real bug found via production data: a short, unrelated 2P snapshot
    # nested entirely inside a much longer behavior take (ThorImage-only
    # sessions running 17-33s of a 129s take, no ThorSync to verify against)
    # satisfied a plain overlap check. Both endpoints must be close, not just
    # "somewhere inside".
    take_start, take_duration = 1000.0, 129.0  # take = [1000, 1129]
    short_snapshot = TwoPhotonFolder(
        Path("snap"), "image", start_time=1010.0, last_mtime=1030.0  # 20s, well inside
    )
    matches = match_take_to_twophoton(
        take_start, take_duration, [short_snapshot], match_window_s=60.0
    )
    assert matches == []


def test_match_never_forces_a_pairing_that_isnt_there():
    # A SyncData folder can legitimately span, or miss, an unrelated ThorImage
    # take — a take with only a sync match must not fabricate an image match.
    sync = TwoPhotonFolder(Path("s"), "sync", start_time=1000.0, last_mtime=1010.0)
    matches = match_take_to_twophoton(1000.0, 10.0, [sync], match_window_s=60.0)
    assert [m.folder.kind for m in matches] == ["sync"]


# --- build_match_record --------------------------------------------------------


def test_build_match_record_uses_relative_paths():
    source_root = Path("/mnt/windows_share/MD")
    folder = TwoPhotonFolder(
        source_root / "MB247_CI63" / "SyncData102",
        "sync",
        start_time=1000.0,
        last_mtime=1010.0,
    )
    from octacam.twophoton_transfer import TwoPhotonMatch

    record = build_match_record(
        [TwoPhotonMatch(folder, gap_s=3.456, ambiguous=False)], source_root
    )
    assert record == {
        "matched": [
            {
                "path": "MB247_CI63/SyncData102",
                "kind": "sync",
                "gap_s": 3.5,
                "ambiguous": False,
                "confidence": "timestamp",
            }
        ]
    }


# --- match_takes_to_twophoton_batch (no double-booking, verified tier) ------

h5py = pytest.importorskip("h5py")
import numpy as np  # noqa: E402


def _pulse_train(total: int, edges: list[int], width: int = 1) -> np.ndarray:
    # width=1 so tightly-packed pulses (hundreds in a short segment) stay
    # distinct rising edges instead of merging into one continuous high run.
    arr = np.zeros((total, 1), dtype=np.uint32)
    for e in edges:
        arr[e : e + width] = 1
    return arr


def _make_verifiable_sync_folder(
    root: Path,
    experiment: str,
    name: str,
    *,
    mtime: float,
    capture_ranges: list[tuple[int, int]],
    cameras_per_segment: list[int],
    frameout_per_segment: list[int],
    total: int = 5000,
) -> TwoPhotonFolder:
    """A SyncData folder with a real (synthetic) Episode001.h5 whose segments
    produce known Cameras/FrameOut edge counts, one range per segment."""
    folder = root / experiment / name
    folder.mkdir(parents=True)
    capture_on = np.zeros((total, 1), dtype=np.uint32)
    cameras_edges: list[int] = []
    frameout_edges: list[int] = []
    for (start, end), n_cam, n_frame in zip(
        capture_ranges, cameras_per_segment, frameout_per_segment, strict=True
    ):
        capture_on[start:end] = 1
        step_cam = max(1, (end - start) // (n_cam + 1))
        cameras_edges += [start + step_cam * (i + 1) for i in range(n_cam)]
        step_frame = max(1, (end - start) // (n_frame + 1))
        frameout_edges += [start + step_frame * (i + 1) for i in range(n_frame)]
    with h5py.File(folder / "Episode001.h5", "w") as f:
        di = f.create_group("DI")
        di.create_dataset("CaptureOn", data=capture_on)
        di.create_dataset("Cameras", data=_pulse_train(total, cameras_edges))
        di.create_dataset("FrameOut", data=_pulse_train(total, frameout_edges))
    _touch(folder / "ThorRealTimeDataSettings.xml", mtime)
    os.utime(folder, (mtime, mtime))
    return TwoPhotonFolder(folder, "sync", start_time=mtime, last_mtime=mtime)


def _make_image_folder_with_timepoints(
    root: Path, experiment: str, name: str, *, u_time: float, timepoints: int
) -> TwoPhotonFolder:
    folder = root / experiment / name
    folder.mkdir(parents=True)
    (folder / "Experiment.xml").write_text(
        f'<?xml version="1.0"?><ThorImageExperiment>'
        f'<Date date="x" uTime="{u_time}" />'
        f'<Timelapse timepoints="{timepoints}" /></ThorImageExperiment>'
    )
    return TwoPhotonFolder(folder, "image", start_time=u_time, last_mtime=u_time)


def test_batch_verified_match_wins_beyond_match_window(tmp_path):
    # A verified edge-count match must win even when its coarse timestamp gap
    # exceeds match_window_s — exact edge counts are stronger evidence than a
    # timestamp guess.
    take_start = 1000.0
    take = TakeInfo(
        folder=tmp_path / "rec",
        start_time=take_start,
        duration_s=10.0,
        camera_frame_counts=[500, 500, 500],
    )
    far_but_correct = _make_verifiable_sync_folder(
        tmp_path,
        "exp",
        "SyncData001",
        mtime=take_start + 500,  # far outside match_window_s=60
        capture_ranges=[(100, 4900)],
        cameras_per_segment=[500],
        frameout_per_segment=[0],
    )
    results = match_takes_to_twophoton_batch(
        [take],
        [far_but_correct],
        match_window_s=60.0,
        verify_window_s=1000.0,
        verify_with_signals=True,
    )
    (match,) = results[take.folder]
    assert match.folder is far_but_correct
    assert match.confidence == "verified"


def test_batch_prefers_exact_edge_match_over_approximate(tmp_path):
    # Real data found more than one SyncData folder land within tolerance
    # (±1) of a take's frame count when nearby takes ran similar durations —
    # the exact match is the one actually confirmed correct, and it must win
    # regardless of which candidate happens to come first in iteration order.
    take = TakeInfo(
        tmp_path / "rec", start_time=1000.0, duration_s=10.0, camera_frame_counts=[500]
    )
    approx_first = _make_verifiable_sync_folder(
        tmp_path,
        "exp",
        "SyncData001",  # sorts/iterates before SyncData002
        mtime=990.0,
        capture_ranges=[(100, 4900)],
        cameras_per_segment=[501],  # off by 1 — within tolerance, but not exact
        frameout_per_segment=[0],
    )
    exact_second = _make_verifiable_sync_folder(
        tmp_path,
        "exp",
        "SyncData002",
        mtime=995.0,
        capture_ranges=[(100, 4900)],
        cameras_per_segment=[500],  # exact
        frameout_per_segment=[0],
    )
    results = match_takes_to_twophoton_batch(
        [take],
        [approx_first, exact_second],
        match_window_s=60.0,
        verify_window_s=1000.0,
        verify_with_signals=True,
    )
    (match,) = results[take.folder]
    assert match.folder is exact_second
    assert match.confidence == "verified"
    assert match.ambiguous is False


def test_batch_no_double_booking_for_timestamp_tier(tmp_path):
    # Two takes whose padded windows both overlap the SAME unverifiable
    # candidate must not both claim it.
    candidate = TwoPhotonFolder(
        tmp_path / "exp" / "SyncData001", "sync", start_time=1000.0, last_mtime=1010.0
    )
    take_a = TakeInfo(tmp_path / "a", start_time=990.0, duration_s=5.0, camera_frame_counts=[])
    take_b = TakeInfo(tmp_path / "b", start_time=1050.0, duration_s=5.0, camera_frame_counts=[])
    results = match_takes_to_twophoton_batch(
        [take_a, take_b],
        [candidate],
        match_window_s=120.0,
        verify_window_s=120.0,
        verify_with_signals=False,
    )
    claimed_by = [folder for folder, matches in results.items() if matches]
    assert len(claimed_by) == 1  # only one take got it, not both
    # The chronologically-first take gets it (processed in start_time order).
    assert claimed_by == [take_a.folder]
    assert results[take_b.folder] == []


def test_batch_same_sync_folder_verifies_two_different_segments(tmp_path):
    # A SyncData folder spanning two takes must be able to verify-match both,
    # via two different segments — not claimed whole-folder.
    sync = _make_verifiable_sync_folder(
        tmp_path,
        "exp",
        "SyncData001",
        mtime=1000.0,
        capture_ranges=[(100, 1900), (2100, 3900)],
        cameras_per_segment=[300, 700],
        frameout_per_segment=[0, 0],
    )
    take_a = TakeInfo(tmp_path / "a", start_time=1000.0, duration_s=10.0, camera_frame_counts=[300])
    take_b = TakeInfo(tmp_path / "b", start_time=1050.0, duration_s=10.0, camera_frame_counts=[700])
    results = match_takes_to_twophoton_batch(
        [take_a, take_b],
        [sync],
        match_window_s=60.0,
        verify_window_s=200.0,
        verify_with_signals=True,
    )
    (match_a,) = results[take_a.folder]
    (match_b,) = results[take_b.folder]
    assert match_a.confidence == match_b.confidence == "verified"
    assert match_a.folder is sync and match_b.folder is sync


def test_batch_verify_with_signals_false_uses_timestamp_tier_only(tmp_path):
    sync = _make_verifiable_sync_folder(
        tmp_path,
        "exp",
        "SyncData001",
        mtime=1000.0,
        capture_ranges=[(100, 1900)],
        cameras_per_segment=[500],
        frameout_per_segment=[0],
    )
    take = TakeInfo(
        tmp_path / "a", start_time=1000.0, duration_s=10.0, camera_frame_counts=[500]
    )
    results = match_takes_to_twophoton_batch(
        [take], [sync], match_window_s=60.0, verify_window_s=200.0,
        verify_with_signals=False,
    )
    (match,) = results[take.folder]
    assert match.confidence == "timestamp"


def test_batch_transitive_image_match_via_frameout(tmp_path):
    sync = _make_verifiable_sync_folder(
        tmp_path,
        "exp",
        "SyncData001",
        mtime=1000.0,
        capture_ranges=[(100, 1900)],
        cameras_per_segment=[500],
        frameout_per_segment=[50],
    )
    image = _make_image_folder_with_timepoints(
        tmp_path, "exp", "Fly1", u_time=1000.0, timepoints=50
    )
    other_image = _make_image_folder_with_timepoints(
        tmp_path, "exp", "Fly1_001", u_time=1000.0, timepoints=999
    )
    take = TakeInfo(
        tmp_path / "a", start_time=1000.0, duration_s=10.0, camera_frame_counts=[500]
    )
    results = match_takes_to_twophoton_batch(
        [take], [sync, image, other_image], match_window_s=60.0,
        verify_window_s=200.0, verify_with_signals=True,
    )
    kinds = {m.folder.kind: m for m in results[take.folder]}
    assert set(kinds) == {"sync", "image"}
    assert kinds["image"].folder is image
    assert kinds["image"].confidence == "verified"
