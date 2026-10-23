"""Z-stack MIP renders -- synthetic ThorImage Z-stack folders (tifffile)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

tifffile = pytest.importorskip("tifffile")
import numpy as np

from octacam.cli import app
from octacam.plugins.twophoton.mip import (
    MIP_FILENAMES,
    find_zstacks,
    is_zstack,
    mip_output_dir,
    render_zstack_mips,
)

runner = CliRunner()


def _experiment_xml(folder: Path, *, zstage: str, stream: str, timepoints: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Experiment.xml").write_text(
        '<?xml version="1.0"?><ThorImageExperiment><Date date="x" uTime="1" />'
        '<LSM pixelX="32" pixelY="16" widthUM="64" heightUM="64" />'
        f"{zstage}{stream}"
        f'<Timelapse timepoints="{timepoints}" /></ThorImageExperiment>'
    )


def _make_zstack(folder: Path, channels=("ChanA", "ChanB")) -> Path:
    _experiment_xml(
        folder,
        zstage='<ZStage steps="5" enable="1" />',
        stream='<Streaming enable="0" zFastEnable="1" />',
        timepoints=1,
    )
    rng = np.random.default_rng(0)
    for chan in channels:
        stack = rng.integers(0, 100, (5, 16, 32), dtype=np.uint16)
        stack[2, 4:8, 10:20] = 4000  # a bright structure on one slice only
        tifffile.imwrite(folder / f"{chan}.tif", stack)
    return folder


def test_is_zstack_rejects_streaming_and_fastz(tmp_path):
    z = _make_zstack(tmp_path / "Fly1_Zstack")
    assert is_zstack(z)
    # Real Fly1_Streaming: ZStage enabled with 201 steps, but streaming 3000 tp.
    streaming = tmp_path / "Fly1_Streaming"
    _experiment_xml(
        streaming,
        zstage='<ZStage steps="201" enable="1" />',
        stream='<Streaming enable="1" zFastEnable="0" />',
        timepoints=3000,
    )
    fastz = tmp_path / "Fly1_Streaming_FastZ"
    _experiment_xml(
        fastz,
        zstage='<ZStage steps="5" enable="1" />',
        stream='<Streaming enable="1" zFastEnable="1" />',
        timepoints=500,
    )
    assert not is_zstack(streaming)
    assert not is_zstack(fastz)


def test_mip_lands_in_reconciled_wrapper_and_keeps_the_structure(tmp_path):
    wrapper = tmp_path / "Fly1" / "Recording1_2P_Zstack"
    z = _make_zstack(wrapper / "2P" / "Fly1_Zstack")
    out = render_zstack_mips(z)
    assert out == mip_output_dir(z) == wrapper
    for name in MIP_FILENAMES.values():
        assert (out / name).stat().st_size > 0
    assert list(find_zstacks(tmp_path)) == [z]


def test_mip_keeps_an_older_takes_renderings_folder(tmp_path):
    wrapper = tmp_path / "Fly1" / "Recording1_2P_Zstack"
    z = _make_zstack(wrapper / "2P" / "Fly1_Zstack")
    (wrapper / "Renderings").mkdir()
    out = render_zstack_mips(z)
    assert out == wrapper / "Renderings"
    assert (out / MIP_FILENAMES["dual"]).exists()


def test_existing_mips_are_kept_unless_forced(tmp_path):
    z = _make_zstack(tmp_path / "2P_only" / "Fly1_Zstack")
    out = mip_output_dir(z)
    out.mkdir(parents=True)
    for name in MIP_FILENAMES.values():
        (out / name).write_bytes(b"old")
    render_zstack_mips(z)
    assert (out / MIP_FILENAMES["dual"]).read_bytes() == b"old"
    render_zstack_mips(z, force=True)
    assert (out / MIP_FILENAMES["dual"]).read_bytes() != b"old"


def test_single_channel_writes_only_its_color(tmp_path):
    z = _make_zstack(tmp_path / "Fly1_Zstack", channels=("ChanB",))
    out = render_zstack_mips(z)
    assert out is not None
    assert (out / MIP_FILENAMES["red"]).exists()
    assert not (out / MIP_FILENAMES["green"]).exists()
    assert not (out / MIP_FILENAMES["dual"]).exists()


def test_unassembled_zstack_is_skipped(tmp_path):
    folder = tmp_path / "Fly1_Zstack"
    _experiment_xml(
        folder,
        zstage='<ZStage steps="5" enable="1" />',
        stream='<Streaming enable="0" />',
        timepoints=1,
    )
    (folder / "ChanA_001_001_001_001.tif").write_bytes(b"x")
    assert render_zstack_mips(folder) is None
    assert list(find_zstacks(tmp_path)) == []


def test_render_command_renders_mips_and_dry_run_writes_nothing(tmp_path):
    day = tmp_path / "261007_Exp"
    z = _make_zstack(day / "Fly1" / "Recording1_2P_Zstack" / "2P" / "Fly1_Zstack")
    result = runner.invoke(app, ["twophoton", "render", str(day), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not (mip_output_dir(z) / MIP_FILENAMES["dual"]).exists()
    result = runner.invoke(app, ["twophoton", "render", str(day), "--no-synced"])
    assert result.exit_code == 0, result.output
    assert (mip_output_dir(z) / MIP_FILENAMES["dual"]).exists()
