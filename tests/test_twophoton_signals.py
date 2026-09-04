"""ThorSync-verified matching: reading real DAQ signals out of Episode*.h5.

Synthetic h5 fixtures built with h5py itself (an actual dev dependency, not a
fake) — shaped like the real file inspected during design (DI/CaptureOn
gates a recording window; DI/Cameras/DI/FrameOut are the trigger-pulse
trains to count edges in), verified against the *exact* edge-count/frame-count
match found on real production data (19399 Cameras edges / 1500 FrameOut
edges) before writing this test file.
"""

from pathlib import Path

import pytest

h5py = pytest.importorskip("h5py")
import numpy as np  # noqa: E402

from octacam.twophoton_signals import (  # noqa: E402
    CaptureSegment,
    channel_sample_rate,
    h5py_available,
    read_capture_segments,
    verify_camera_match,
    verify_frameout_match,
)

SETTINGS_XML = """<?xml version="1.0" encoding="utf-8"?>
<RealTimeDataSettings>
  <DaqDevices>
    <AcquireBoard devID="Dev3">
      <SampleRate name="High 1MHz" enable="0" rate="1000001" />
      <SampleRate name="Medium 100kHz" enable="1" rate="100000" />
      <DataChannel alias="Cameras" enable="1" />
      <DataChannel alias="FrameOut" enable="1" />
      <DataChannel alias="CaptureOn" enable="1" />
    </AcquireBoard>
    <AcquireBoard devID="Dev3">
      <SampleRate name="Low 20kHz" enable="1" rate="20000" />
      <DataChannel alias="FrameOut" enable="1" />
    </AcquireBoard>
  </DaqDevices>
</RealTimeDataSettings>
"""


def _square_wave(total: int, on_ranges: list[tuple[int, int]]) -> np.ndarray:
    """A (total, 1) uint32 array, high (1) within each [start, end) range."""
    arr = np.zeros((total, 1), dtype=np.uint32)
    for start, end in on_ranges:
        arr[start:end] = 1
    return arr


def _pulse_train(total: int, edges: list[int], width: int = 2) -> np.ndarray:
    """A (total, 1) uint32 array with a short high pulse starting at each
    sample index in *edges* — produces exactly len(edges) rising edges."""
    arr = np.zeros((total, 1), dtype=np.uint32)
    for e in edges:
        arr[e : e + width] = 1
    return arr


def _make_sync_folder(
    tmp_path: Path,
    *,
    capture_on_ranges: list[tuple[int, int]],
    cameras_pulse_starts: list[int],
    frameout_pulse_starts: list[int],
    total: int = 2000,
    write_settings: bool = True,
) -> Path:
    folder = tmp_path / "SyncData001"
    folder.mkdir()
    if write_settings:
        (folder / "ThorRealTimeDataSettings.xml").write_text(SETTINGS_XML)
    with h5py.File(folder / "Episode001.h5", "w") as f:
        di = f.create_group("DI")
        di.create_dataset("CaptureOn", data=_square_wave(total, capture_on_ranges))
        di.create_dataset("Cameras", data=_pulse_train(total, cameras_pulse_starts))
        di.create_dataset("FrameOut", data=_pulse_train(total, frameout_pulse_starts))
    return folder


# --- channel_sample_rate -----------------------------------------------------


def test_channel_sample_rate_resolves_enabled_board(tmp_path):
    xml = tmp_path / "ThorRealTimeDataSettings.xml"
    xml.write_text(SETTINGS_XML)
    assert channel_sample_rate(xml, "CaptureOn") == 100000.0


def test_channel_sample_rate_missing_channel_returns_none(tmp_path):
    xml = tmp_path / "ThorRealTimeDataSettings.xml"
    xml.write_text(SETTINGS_XML)
    assert channel_sample_rate(xml, "DoesNotExist") is None


def test_channel_sample_rate_missing_file_returns_none(tmp_path):
    assert channel_sample_rate(tmp_path / "nope.xml", "Cameras") is None


# --- read_capture_segments ----------------------------------------------------


def test_read_capture_segments_single_segment_exact_counts(tmp_path):
    # 10 camera pulses and 3 frameout pulses, all inside the one CaptureOn window.
    cameras = [110 + i * 10 for i in range(10)]
    frameout = [150, 400, 650]
    folder = _make_sync_folder(
        tmp_path,
        capture_on_ranges=[(100, 900)],
        cameras_pulse_starts=cameras,
        frameout_pulse_starts=frameout,
    )
    (segment,) = read_capture_segments(folder)
    assert segment.cameras_edges == 10
    assert segment.frameout_edges == 3
    assert segment.sample_rate == 100000.0
    assert segment.duration_s == pytest.approx(800 / 100000.0)


def test_read_capture_segments_excludes_edges_outside_window(tmp_path):
    # Pulses before/after the CaptureOn window must not be counted.
    folder = _make_sync_folder(
        tmp_path,
        capture_on_ranges=[(500, 1000)],
        cameras_pulse_starts=[10, 600, 700, 1500],  # only 600, 700 are inside
        frameout_pulse_starts=[50, 1900],  # none inside
    )
    (segment,) = read_capture_segments(folder)
    assert segment.cameras_edges == 2
    assert segment.frameout_edges == 0


def test_read_capture_segments_two_segments_spanning_multiple_takes(tmp_path):
    # A SyncData folder can span more than one take (confirmed real case) —
    # each CaptureOn on/off pair is its own independent segment.
    folder = _make_sync_folder(
        tmp_path,
        capture_on_ranges=[(100, 400), (600, 900)],
        cameras_pulse_starts=[150, 200, 250, 700, 750],  # 3 in seg 1, 2 in seg 2
        frameout_pulse_starts=[350, 850],
    )
    seg1, seg2 = read_capture_segments(folder)
    assert (seg1.start_sample, seg1.end_sample) == (100, 400)
    assert seg1.cameras_edges == 3
    assert seg1.frameout_edges == 1
    assert (seg2.start_sample, seg2.end_sample) == (600, 900)
    assert seg2.cameras_edges == 2
    assert seg2.frameout_edges == 1


def test_read_capture_segments_zero_segments_when_never_toggled(tmp_path):
    folder = _make_sync_folder(
        tmp_path, capture_on_ranges=[], cameras_pulse_starts=[], frameout_pulse_starts=[]
    )
    assert read_capture_segments(folder) == []


def test_read_capture_segments_ignores_unclosed_trailing_rise(tmp_path):
    # A rise with no matching fall (still recording / crashed mid-capture) —
    # skip it rather than guess an end sample.
    total = 1000
    arr = np.zeros((total, 1), dtype=np.uint32)
    arr[100:400] = 1  # closed segment
    arr[600:] = 1  # unclosed trailing rise
    folder = tmp_path / "SyncData001"
    folder.mkdir()
    (folder / "ThorRealTimeDataSettings.xml").write_text(SETTINGS_XML)
    with h5py.File(folder / "Episode001.h5", "w") as f:
        di = f.create_group("DI")
        di.create_dataset("CaptureOn", data=arr)
        di.create_dataset("Cameras", data=_pulse_train(total, [150, 250]))
        di.create_dataset("FrameOut", data=_pulse_train(total, [350]))
    (segment,) = read_capture_segments(folder)
    assert (segment.start_sample, segment.end_sample) == (100, 400)


def test_read_capture_segments_missing_episode_file_returns_none(tmp_path):
    folder = tmp_path / "SyncData001"
    folder.mkdir()
    (folder / "ThorRealTimeDataSettings.xml").write_text(SETTINGS_XML)
    assert read_capture_segments(folder) is None


def test_read_capture_segments_missing_dataset_returns_none(tmp_path):
    folder = tmp_path / "SyncData001"
    folder.mkdir()
    with h5py.File(folder / "Episode001.h5", "w") as f:
        f.create_group("DI")  # no CaptureOn dataset at all
    assert read_capture_segments(folder) is None


def test_read_capture_segments_corrupt_file_returns_none(tmp_path):
    folder = tmp_path / "SyncData001"
    folder.mkdir()
    (folder / "Episode001.h5").write_bytes(b"not an hdf5 file")
    assert read_capture_segments(folder) is None


def test_read_capture_segments_missing_settings_xml_still_reads_edges(tmp_path):
    # Sample rate is best-effort/diagnostic only — its absence must not block
    # the actual edge-count verification.
    folder = _make_sync_folder(
        tmp_path,
        capture_on_ranges=[(100, 900)],
        cameras_pulse_starts=[150, 250],
        frameout_pulse_starts=[400],
        write_settings=False,
    )
    (segment,) = read_capture_segments(folder)
    assert segment.cameras_edges == 2
    assert segment.sample_rate is None
    assert segment.duration_s is None


def test_read_capture_segments_returns_none_without_h5py(tmp_path, monkeypatch):
    import octacam.twophoton_signals as sig_mod

    folder = _make_sync_folder(
        tmp_path, capture_on_ranges=[(0, 10)], cameras_pulse_starts=[], frameout_pulse_starts=[]
    )
    monkeypatch.setattr(sig_mod, "h5py_available", lambda: False)
    assert read_capture_segments(folder) is None


def test_h5py_available_true_in_this_env():
    assert h5py_available() is True


# --- verify_camera_match / verify_frameout_match -----------------------------


def test_verify_camera_match_exact():
    seg = CaptureSegment(0, 100, cameras_edges=19399, frameout_edges=1500)
    assert verify_camera_match(seg, [19399, 19399, 19399]) is True


def test_verify_camera_match_within_tolerance():
    seg = CaptureSegment(0, 100, cameras_edges=19398, frameout_edges=0)
    assert verify_camera_match(seg, [19399], tolerance=1) is True
    assert verify_camera_match(seg, [19399], tolerance=0) is False


def test_verify_camera_match_no_match():
    seg = CaptureSegment(0, 100, cameras_edges=500, frameout_edges=0)
    assert verify_camera_match(seg, [19399, 12000]) is False


def test_verify_frameout_match_exact_and_tolerance():
    seg = CaptureSegment(0, 100, cameras_edges=0, frameout_edges=1500)
    assert verify_frameout_match(seg, 1500) is True
    assert verify_frameout_match(seg, 1501, tolerance=1) is True
    assert verify_frameout_match(seg, 1600) is False
