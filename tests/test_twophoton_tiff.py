"""TIFF stack assembly: consolidating ThorImage's per-frame streaming-mode
TIFFs into one OME-TIFF stack per channel.

Synthetic per-frame tif fixtures built with tifffile itself (an actual dev
dependency, not a fake) — shaped like real data inspected during design: a
4-numeric-group filename (``Chan<name>_NNN_NNN_NNN_NNN.tif``), a varying
position that differs between a streaming acquisition (position 3, T) and a
Z-stack (position 2, Z) — both confirmed on real NAS data before writing
this test file — and a real stray ``Chan<name>_Preview.tif`` file with no
numeric groups at all, also confirmed present alongside genuine per-frame
files on real data.
"""

from pathlib import Path

import pytest

tifffile = pytest.importorskip("tifffile")
import numpy as np  # noqa: E402

from octacam import twophoton_tiff as ttiff  # noqa: E402
from octacam.twophoton_tiff import (  # noqa: E402
    assemble_channel_stack,
    plan_assembly,
    tifffile_available,
)

EXPERIMENT_XML_TEMPLATE = """<?xml version="1.0"?>
<ThorImageExperiment>
  <Wavelengths>
    <Wavelength name="ChanA" exposureTimeMS="0" />
    <Wavelength name="ChanB" exposureTimeMS="0" />
  </Wavelengths>
  <ZStage name="ThorZPiezo" steps="{zsteps}" enable="{zenable}" />
  <Timelapse timepoints="{timepoints}" intervalSec="0" triggerMode="0" />
  <LSM pixelSizeUM="0.089" />
</ThorImageExperiment>
"""


def _write_experiment_xml(folder: Path, *, timepoints: int, zsteps: int = 1, zenable: str = "0") -> Path:
    xml = folder / "Experiment.xml"
    xml.write_text(
        EXPERIMENT_XML_TEMPLATE.format(timepoints=timepoints, zsteps=zsteps, zenable=zenable)
    )
    return xml


def _write_frame(folder: Path, channel: str, groups: tuple[int, int, int, int], value: int, shape=(16, 16)) -> Path:
    name = f"{channel}_{groups[0]:03d}_{groups[1]:03d}_{groups[2]:03d}_{groups[3]:03d}.tif"
    path = folder / name
    tifffile.imwrite(path, np.full(shape, value, dtype=np.uint16))
    return path


def _write_streaming_channel(folder: Path, channel: str, n: int) -> None:
    for t in range(1, n + 1):
        _write_frame(folder, channel, (1, 1, 1, t), value=t)


def _write_zstack_channel(folder: Path, channel: str, n: int) -> None:
    for z in range(1, n + 1):
        _write_frame(folder, channel, (1, 1, z, 1), value=z)


# --------------------------------------------------------------- plan_assembly


def test_plan_assembly_noop_for_single_file(tmp_path):
    _write_experiment_xml(tmp_path, timepoints=1)
    _write_frame(tmp_path, "ChanA", (1, 1, 1, 1), value=0)
    plan = plan_assembly(tmp_path)
    assert plan.channels == []


def test_plan_assembly_streaming_orders_by_last_group(tmp_path):
    _write_experiment_xml(tmp_path, timepoints=12)
    # Write out of order, and cross a zero-padding-width boundary (9 -> 10)
    # to pin the int-sort behavior over a naive lexicographic sort.
    for t in [3, 1, 12, 2, 9, 10, 11, 4, 5, 6, 7, 8]:
        _write_frame(tmp_path, "ChanA", (1, 1, 1, t), value=t)
    plan = plan_assembly(tmp_path)
    (ch,) = plan.channels
    assert ch.status == "ready"
    assert ch.axis == "T"
    assert ch.frame_count == 12
    ordered_values = [int(p.name.rsplit("_", 1)[1].split(".")[0]) for p in ch.source_files]
    assert ordered_values == list(range(1, 13))


def test_plan_assembly_zstack_orders_by_third_group(tmp_path):
    _write_experiment_xml(tmp_path, timepoints=1, zsteps=5, zenable="1")
    _write_zstack_channel(tmp_path, "ChanA", 5)
    plan = plan_assembly(tmp_path)
    (ch,) = plan.channels
    assert ch.status == "ready"
    assert ch.axis == "Z"
    assert ch.frame_count == 5


def test_plan_assembly_ambiguous_varying_position_is_a_problem(tmp_path):
    _write_experiment_xml(tmp_path, timepoints=3)
    # Both position 2 and position 3 vary at once.
    _write_frame(tmp_path, "ChanA", (1, 1, 1, 1), value=0)
    _write_frame(tmp_path, "ChanA", (1, 1, 2, 2), value=0)
    _write_frame(tmp_path, "ChanA", (1, 1, 3, 3), value=0)
    plan = plan_assembly(tmp_path)
    (ch,) = plan.channels
    assert ch.status == "problem"
    assert "ambiguous" in ch.reason


def test_plan_assembly_gap_in_frame_indices_is_a_problem(tmp_path):
    _write_experiment_xml(tmp_path, timepoints=3)
    for t in (1, 2, 4):  # missing 3
        _write_frame(tmp_path, "ChanA", (1, 1, 1, t), value=t)
    plan = plan_assembly(tmp_path)
    (ch,) = plan.channels
    assert ch.status == "problem"
    assert "non-contiguous" in ch.reason


def test_plan_assembly_duplicate_frame_index_is_a_problem(tmp_path):
    # The only way two files can share identical numeric groups (the varying
    # position can't have a real duplicate value otherwise — that would mean
    # two files with the same filename) is a differing extension.
    _write_experiment_xml(tmp_path, timepoints=2)
    _write_frame(tmp_path, "ChanA", (1, 1, 1, 1), value=1)
    (tmp_path / "ChanA_001_001_001_001.tiff").write_bytes(
        (tmp_path / "ChanA_001_001_001_001.tif").read_bytes()
    )
    plan = plan_assembly(tmp_path)
    (ch,) = plan.channels
    assert ch.status == "problem"


def test_plan_assembly_timepoints_mismatch_is_a_problem(tmp_path):
    _write_experiment_xml(tmp_path, timepoints=10, zsteps=1, zenable="0")
    _write_streaming_channel(tmp_path, "ChanA", 8)  # only 8 of the declared 10
    plan = plan_assembly(tmp_path)
    (ch,) = plan.channels
    assert ch.status == "problem"
    assert "timepoints=10" in ch.reason


def test_plan_assembly_channel_declared_but_no_files_is_silently_skipped(tmp_path):
    _write_experiment_xml(tmp_path, timepoints=5)
    _write_streaming_channel(tmp_path, "ChanA", 5)
    # ChanB is declared in <Wavelengths> but never wrote anything (e.g. an
    # aborted rawData=1 test capture) — must not appear as a "problem".
    plan = plan_assembly(tmp_path)
    assert [c.channel for c in plan.channels] == ["ChanA"]


def test_plan_assembly_ignores_preview_and_unrelated_files(tmp_path):
    _write_experiment_xml(tmp_path, timepoints=3)
    _write_streaming_channel(tmp_path, "ChanB", 3)
    tifffile.imwrite(tmp_path / "ChanB_Preview.tif", np.zeros((16, 16), dtype=np.uint16))
    plan = plan_assembly(tmp_path)
    (ch,) = plan.channels
    assert ch.status == "ready"
    assert ch.frame_count == 3
    assert all("Preview" not in p.name for p in ch.source_files)


def test_plan_assembly_empty_when_tifffile_unavailable(tmp_path, monkeypatch):
    _write_experiment_xml(tmp_path, timepoints=3)
    _write_streaming_channel(tmp_path, "ChanA", 3)
    monkeypatch.setattr(ttiff, "tifffile_available", lambda: False)
    plan = plan_assembly(tmp_path)
    assert plan.channels == []


def test_tifffile_available_true_in_this_env():
    assert tifffile_available() is True


# ----------------------------------------------------------- assemble_channel_stack


def test_assemble_channel_stack_writes_pages_in_given_order(tmp_path):
    folder = tmp_path / "rec"
    folder.mkdir()
    xml = _write_experiment_xml(folder, timepoints=5)
    _write_streaming_channel(folder, "ChanA", 5)
    plan = plan_assembly(folder)
    (ch,) = plan.channels
    dest = folder / ch.dest_name
    result = assemble_channel_stack(
        ch.source_files, dest, axis=ch.axis, experiment_xml=xml, full_verify=True
    )
    assert result.ok
    assert result.frame_count == 5
    with tifffile.TiffFile(dest) as tf:
        assert len(tf.pages) == 5
        assert tf.series[0].axes == "TYX"
        for i in range(5):
            page = tf.pages[i].asarray()
            assert (page == i + 1).all()  # frame value == its 1-based index


def test_assemble_channel_stack_embeds_ome_metadata(tmp_path):
    folder = tmp_path / "rec"
    folder.mkdir()
    xml = _write_experiment_xml(folder, timepoints=3)
    _write_streaming_channel(folder, "ChanA", 3)
    plan = plan_assembly(folder)
    (ch,) = plan.channels
    dest = folder / ch.dest_name
    result = assemble_channel_stack(ch.source_files, dest, axis=ch.axis, experiment_xml=xml)
    assert result.ok
    with tifffile.TiffFile(dest) as tf:
        desc = tf.pages[0].description
        assert "ChanA" in desc
        assert "PhysicalSizeX" in desc


def test_assemble_channel_stack_full_verify_catches_mismatch(tmp_path, monkeypatch):
    folder = tmp_path / "rec"
    folder.mkdir()
    _write_experiment_xml(folder, timepoints=3)
    _write_streaming_channel(folder, "ChanA", 3)
    plan = plan_assembly(folder)
    (ch,) = plan.channels
    dest = folder / ch.dest_name

    real_read = ttiff._read_single_page
    call_count = {"n": 0}

    def _tampering_read(path):
        call_count["n"] += 1
        frame = real_read(path)
        if call_count["n"] > len(ch.source_files):  # only tamper during verify pass
            return frame + 100
        return frame

    monkeypatch.setattr(ttiff, "_read_single_page", _tampering_read)
    result = assemble_channel_stack(ch.source_files, dest, full_verify=True)
    assert not result.ok
    assert "does not match" in result.error
    assert not list(folder.glob("*.octacam-assemble-part*"))
    assert not dest.exists()


def test_assemble_channel_stack_missing_source_file_fails_cleanly(tmp_path):
    folder = tmp_path / "rec"
    folder.mkdir()
    _write_experiment_xml(folder, timepoints=3)
    _write_streaming_channel(folder, "ChanA", 3)
    plan = plan_assembly(folder)
    (ch,) = plan.channels
    ch.source_files[1].unlink()
    dest = folder / ch.dest_name
    result = assemble_channel_stack(ch.source_files, dest)
    assert not result.ok
    assert not dest.exists()
    assert not list(folder.glob("*.octacam-assemble-part*"))


def test_assemble_channel_stack_corrupt_source_file_fails_cleanly(tmp_path):
    folder = tmp_path / "rec"
    folder.mkdir()
    _write_experiment_xml(folder, timepoints=3)
    _write_streaming_channel(folder, "ChanA", 3)
    plan = plan_assembly(folder)
    (ch,) = plan.channels
    ch.source_files[1].write_bytes(b"not a tiff file")
    dest = folder / ch.dest_name
    result = assemble_channel_stack(ch.source_files, dest)
    assert not result.ok
    assert not dest.exists()
    assert not list(folder.glob("*.octacam-assemble-part*"))
