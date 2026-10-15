"""ThorSync-verified matching: reading real DAQ signals out of Episode*.h5.

Synthetic h5 fixtures built with h5py itself (an actual dev dependency, not a
fake) -- shaped like the real file inspected during design (DI/CaptureOn
gates a recording window; DI/Cameras/DI/FrameOut are the trigger-pulse
trains to count edges in), verified against the *exact* edge-count/frame-count
match found on real production data (19399 Cameras edges / 1500 FrameOut
edges) before writing this test file.
"""

from pathlib import Path

import pytest

h5py = pytest.importorskip("h5py")
import numpy as np

from octacam.plugins.twophoton.signals import (
    channel_sample_rate,
    h5py_available,
    read_capture_segments,
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
    sample index in *edges* -- produces exactly len(edges) rising edges.
    """
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


def test_read_capture_segments_counts_the_synced_stops_camera_tail(tmp_path):
    # Camera pulses before the capture don't belong to it; those right after
    # CaptureOn falls do: a take synced with ThorImage keeps triggering the
    # cameras for stop_idle_ms (real 2026-10-14 SyncData166: 19618 inside +
    # 199 in the 1.98 s after the fall = the take's 19817 frames). FrameOut
    # stays within the capture.
    folder = _make_sync_folder(
        tmp_path,
        capture_on_ranges=[(500, 1000)],
        cameras_pulse_starts=[10, 600, 700, 1500],  # 600, 700 inside; 1500 = tail
        frameout_pulse_starts=[50, 1900],  # none inside
    )
    (segment,) = read_capture_segments(folder)
    assert segment.cameras_edges == 3
    assert segment.frameout_edges == 0


def test_read_capture_segments_two_segments_spanning_multiple_takes(tmp_path):
    # A SyncData folder can span more than one take (confirmed real case) --
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
        tmp_path,
        capture_on_ranges=[],
        cameras_pulse_starts=[],
        frameout_pulse_starts=[],
    )
    assert read_capture_segments(folder) == []


def test_read_capture_segments_ignores_unclosed_trailing_rise(tmp_path):
    # A rise with no matching fall (still recording / crashed mid-capture) --
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
    # Sample rate is best-effort/diagnostic only -- its absence must not block
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
    import octacam.plugins.twophoton.signals as sig_mod

    folder = _make_sync_folder(
        tmp_path,
        capture_on_ranges=[(0, 10)],
        cameras_pulse_starts=[],
        frameout_pulse_starts=[],
    )
    monkeypatch.setattr(sig_mod, "h5py_available", lambda: False)
    assert read_capture_segments(folder) is None


def test_h5py_available_true_in_this_env():
    assert h5py_available() is True


# --- ThorSync duration limit / cut-off episodes ----------------------------


def _thorsync_folder(root, name, *, capture, duration=320, rate=1000):
    """A SyncData folder whose CaptureOn column is *capture* (0/1 per sample)."""
    folder = root / name
    folder.mkdir(parents=True)
    with h5py.File(folder / "Episode001.h5", "w") as f:
        f.create_group("DI").create_dataset(
            "CaptureOn", data=np.asarray(capture, np.uint32).reshape(-1, 1)
        )
    (folder / "ThorRealTimeDataSettings.xml").write_text(
        '<?xml version="1.0"?><ThorData>'
        '<AcquireBoard type="NI6363" active="0" duration="0">'
        '<DataChannel alias="CaptureOn" enable="1" /><SampleRate enable="1" rate="5" />'
        "</AcquireBoard>"
        f'<AcquireBoard type="NI6363" active="1" duration="{duration}">'
        '<DataChannel alias="CaptureOn" enable="1" />'
        f'<SampleRate enable="1" rate="{rate}" /></AcquireBoard></ThorData>'
    )
    return folder


def test_episode_cut_off_when_capture_still_on_at_the_end(tmp_path):
    # Real 2026-10-14 SyncData167: ThorSync's 320 s limit hit mid-capture.
    from octacam.plugins.twophoton.signals import (
        describe_cut_off,
        episode_cut_off,
        thorsync_duration_limit,
    )

    # 2 s at 1 kHz, ThorSync's limit set to 2 s: the limit cut it.
    cut = _thorsync_folder(
        tmp_path, "SyncData167", capture=[0] * 10 + [1] * 1990, duration=2
    )
    done = _thorsync_folder(
        tmp_path, "SyncData166", capture=[0] * 10 + [1] * 1980 + [0] * 10
    )
    assert thorsync_duration_limit(cut / "ThorRealTimeDataSettings.xml") == 2.0
    result = episode_cut_off(cut)
    assert result is not None
    assert (result.recorded_s, result.limit_s) == (2.0, 2.0)
    assert episode_cut_off(done) is None
    message = describe_cut_off("SyncData167", result)
    assert "2 s duration limit" in message and "AirFlow" in message
    # Stopped well before its 320 s limit: stopped by hand, not the limit.
    manual = _thorsync_folder(
        tmp_path, "SyncData168", capture=[0] * 10 + [1] * 1990, duration=320
    )
    message = describe_cut_off("SyncData168", episode_cut_off(manual))
    assert "duration limit" not in message
    assert "after 2 s" in message and "stopped before ThorImage" in message


def test_capture_already_on_at_the_first_sample_is_a_segment(tmp_path):
    # ThorSync started after ThorImage: CaptureOn is high from sample 0.
    from octacam.plugins.twophoton.signals import read_capture_segments

    folder = _thorsync_folder(tmp_path, "SyncData169", capture=[1] * 50 + [0] * 10)
    (segment,) = read_capture_segments(folder)
    assert (segment.start_sample, segment.end_sample) == (0, 50)
    assert (
        segment.sample_rate == 1000.0
    )  # the active board's, not the inactive preset's


def test_latest_thorsync_limit_reads_the_newest_sync_folder(tmp_path):
    import os

    from octacam.plugins.twophoton.signals import latest_thorsync_limit

    old = _thorsync_folder(tmp_path / "ExpA", "SyncData001", capture=[0], duration=320)
    new = _thorsync_folder(tmp_path / "ExpB", "SyncData002", capture=[0], duration=7200)
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))
    assert latest_thorsync_limit(tmp_path) == (new, 7200.0)
    assert latest_thorsync_limit(tmp_path / "missing") is None


def test_an_episode_is_read_once_across_runs(tmp_path, monkeypatch):
    # 23 s per 0.6 GB episode over the share, asked by every run: the segments
    # are cached on disk, keyed by the file's path, size and mtime.
    import octacam.plugins.twophoton.signals as sig

    folder = _make_sync_folder(
        tmp_path,
        capture_on_ranges=[(100, 900)],
        cameras_pulse_starts=[110 + i * 10 for i in range(10)],
        frameout_pulse_starts=[150],
    )
    first = read_capture_segments(folder)
    monkeypatch.setattr(sig, "_SEGMENT_CACHE", {})  # a new process

    def unread(*args):
        raise AssertionError("the episode was read again")

    monkeypatch.setattr(sig, "_read_capture_segments", unread)
    assert read_capture_segments(folder) == first

    # A changed episode (still being written) is read afresh.
    episode = folder / "Episode001.h5"
    with h5py.File(episode, "a") as f:
        f.attrs["touched"] = 1
    monkeypatch.setattr(sig, "_SEGMENT_CACHE", {})
    with pytest.raises(AssertionError, match="read again"):
        read_capture_segments(folder)
