"""Assemble ThorImage's per-frame streaming-mode TIFFs into one OME-TIFF
stack per channel.

ThorImage writes one ``.tif`` file **per frame per channel** during
streaming-mode acquisition — a 1500-timepoint, 2-channel recording produces
3000 loose files (confirmed on real data:
``ChanA_001_001_001_NNNN.tif``, NNNN = frame index). A real Z-stack
acquisition uses the same 4-numeric-group filename shape but varies a
*different* group position (index 2, not 3) — confirmed on real data too, so
detection here finds which position actually varies per file set rather than
assuming either layout. Neither ``<Streaming enable>`` nor
``<CaptureMode mode>`` in ``Experiment.xml`` reliably distinguishes "already
one file" from "needs assembly" — every real recording inspected (streaming,
Z-stack, and ``rawData="1"`` aborted test captures) writes per-frame files or
none at all, never a native single stack — so detection is disk-truth-driven:
count each channel's actual files, cross-checked against ``Experiment.xml``'s
own ``Timelapse/@timepoints`` (a T-axis stack) or ``ZStage/@steps`` (a
Z-axis stack) as a sanity check, never as the trigger.

One assembled file per channel, not one combined multi-channel file — a
functional (e.g. GCaMP) and a purely structural/reference channel used only
for motion-correction are analyzed by different pipeline stages that never
need both loaded together, so combining them buys nothing and would require
assuming per-timepoint correspondence between channels that can't be
verified from ``Experiment.xml`` alone.

Everything here is best-effort and never raises (mirrors
``twophoton_signals.py``'s shape): ``tifffile`` is an optional dependency
(the ``twophoton`` extra), so a rig without it — or a file set that can't be
safely assembled — simply falls back to a plain per-file copy, never a
crashed transfer or lost frames.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from octacam.twophoton_transfer import EXPERIMENT_XML_FILENAME, thorimage_timepoints

log = logging.getLogger("octacam")

_FRAME_RE_TEMPLATE = r"^{chan}_(\d+)_(\d+)_(\d+)_(\d+)\.tiff?$"
_TEMP_SUFFIX = ".octacam-assemble-part"


def tifffile_available() -> bool:
    """Cheap capability check — used for logging and so tests can force the
    "assembly unavailable" path without actually uninstalling tifffile."""
    try:
        import tifffile  # noqa: F401
    except ImportError:
        return False
    return True


def _thorimage_channel_names(experiment_xml: Path) -> list[str]:
    """``<Wavelengths><Wavelength name="..."/></Wavelengths>`` names, in
    document order, or ``[]`` if missing/unparseable."""
    try:
        root = ET.parse(experiment_xml).getroot()
    except (ET.ParseError, OSError):
        return []
    wavelengths_el = root.find("Wavelengths")
    if wavelengths_el is None:
        return []
    names = []
    for wl in wavelengths_el.findall("Wavelength"):
        name = wl.get("name")
        if name:
            names.append(name)
    return names


def _thorimage_zstage_steps(experiment_xml: Path) -> int | None:
    """``<ZStage steps="..." enable="1">`` — the Z-axis frame count for a
    Z-stack acquisition, or None if missing/unparseable/disabled."""
    try:
        root = ET.parse(experiment_xml).getroot()
    except (ET.ParseError, OSError):
        return None
    zstage_el = root.find("ZStage")
    if zstage_el is None or zstage_el.get("enable") != "1":
        return None
    steps = zstage_el.get("steps")
    if steps is None:
        return None
    try:
        return int(steps)
    except ValueError:
        return None


def _thorimage_pixel_size_um(experiment_xml: Path) -> float | None:
    """``<LSM pixelSizeUM="...">`` — the scan system's actual per-pixel size,
    not the wide-field ``<Camera pixelSizeUM>`` — or None if unavailable."""
    try:
        root = ET.parse(experiment_xml).getroot()
    except (ET.ParseError, OSError):
        return None
    lsm_el = root.find("LSM")
    if lsm_el is None:
        return None
    size = lsm_el.get("pixelSizeUM")
    if size is None:
        return None
    try:
        return float(size)
    except ValueError:
        return None


def _matching_frame_files(folder: Path, channel: str) -> list[tuple[Path, tuple[int, int, int, int]]]:
    """Every file in *folder* matching ``<channel>_NNNN_NNNN_NNNN_NNNN.tif(f)``
    exactly — four numeric groups, nothing else — paired with its parsed
    numeric groups. A real stray file like ``ChanB_Preview.tif`` (no numeric
    groups at all) never matches, confirmed against real data."""
    pattern = re.compile(_FRAME_RE_TEMPLATE.format(chan=re.escape(channel)))
    found = []
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        m = pattern.match(entry.name)
        if m:
            found.append((entry, tuple(int(g) for g in m.groups())))
    return found


def _varying_position(groups: list[tuple[int, int, int, int]]) -> int | None:
    """Which single one of the 4 numeric positions actually varies across
    *groups* — generic over streaming (position 3 varies, confirmed real
    data) vs. Z-stack (position 2 varies, confirmed real data) file layouts,
    never a hardcoded position. None when zero or more than one position
    varies (ambiguous file set — never guessed)."""
    varying = [i for i in range(4) if len({g[i] for g in groups}) > 1]
    return varying[0] if len(varying) == 1 else None


@dataclass(frozen=True)
class ChannelPlan:
    channel: str
    status: Literal["ready", "problem"]
    source_files: list[Path]
    dest_name: str
    axis: Literal["T", "Z"] | None = None
    frame_count: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AssemblyPlan:
    folder: Path
    channels: list[ChannelPlan] = field(default_factory=list)

    @property
    def ready(self) -> list[ChannelPlan]:
        return [c for c in self.channels if c.status == "ready"]

    @property
    def problems(self) -> list[ChannelPlan]:
        return [c for c in self.channels if c.status == "problem"]


def plan_assembly(folder: Path) -> AssemblyPlan:
    """Never raises. Empty ``.channels`` when tifffile is unavailable, when
    ``Experiment.xml`` is missing/unparseable, or no ``<Wavelength>`` entries
    are found — caller treats an empty plan as "nothing to assemble, use a
    plain transfer_tree copy", exactly like ``h5py_available() is False``
    degrades ``twophoton_signals`` to "no verification" today. A channel
    with exactly one matching file, or a channel declared but with zero
    matching files on disk (e.g. an aborted ``rawData="1"`` test capture),
    produces no entry at all — there's nothing to assemble and nothing wrong
    either. A channel with more than one file is either ``"ready"`` (a
    single, contiguous, count-verified varying position was found) or
    ``"problem"`` (falls back to a plain per-file copy for that channel —
    assembly must never mean silently dropping or corrupting frames)."""
    empty = AssemblyPlan(folder)
    if not tifffile_available():
        return empty
    experiment_xml = folder / EXPERIMENT_XML_FILENAME
    channel_names = _thorimage_channel_names(experiment_xml)
    if not channel_names:
        return empty
    timepoints = thorimage_timepoints(experiment_xml)
    zsteps = _thorimage_zstage_steps(experiment_xml)

    plans: list[ChannelPlan] = []
    for channel in channel_names:
        found = _matching_frame_files(folder, channel)
        if len(found) <= 1:
            continue  # nothing to assemble either way
        dest_name = f"{channel}.tif"
        pos = _varying_position([g for _, g in found])
        if pos is None:
            plans.append(ChannelPlan(
                channel, "problem", [p for p, _ in found], dest_name,
                reason="ambiguous frame naming: zero or more than one "
                       "numeric group varies across the file set",
            ))
            continue
        varying_pos: int = pos  # pyright can't narrow `pos` inside the lambda closure below
        ordered = sorted(found, key=lambda item, p=varying_pos: item[1][p])
        indices = [g[varying_pos] for _, g in ordered]
        if indices != list(range(indices[0], indices[0] + len(indices))):
            plans.append(ChannelPlan(
                channel, "problem", [p for p, _ in ordered], dest_name,
                reason=f"non-contiguous or duplicate frame indices "
                       f"({indices[0]}..{indices[-1]}, {len(indices)} file(s) present)",
            ))
            continue
        n = len(indices)
        if timepoints is not None and n == timepoints:
            axis: Literal["T", "Z"] | None = "T"
        elif zsteps is not None and n == zsteps:
            axis = "Z"
        else:
            plans.append(ChannelPlan(
                channel, "problem", [p for p, _ in ordered], dest_name,
                reason=f"{n} frame file(s) present but Experiment.xml declares "
                       f"Timelapse timepoints={timepoints}, ZStage steps={zsteps} "
                       f"— neither matches",
            ))
            continue
        plans.append(ChannelPlan(
            channel, "ready", [p for p, _ in ordered], dest_name,
            axis=axis, frame_count=n,
        ))
    return AssemblyPlan(folder, plans)


@dataclass(frozen=True)
class AssembleResult:
    ok: bool
    frame_count: int = 0
    dest_path: Path | None = None
    error: str | None = None


def _ome_metadata(plan: ChannelPlan, experiment_xml: Path) -> dict:
    metadata: dict = {
        "axes": f"{plan.axis}YX",
        "Channel": {"Name": [plan.channel]},
    }
    pixel_size = _thorimage_pixel_size_um(experiment_xml)
    if pixel_size is not None:
        metadata["PhysicalSizeX"] = pixel_size
        metadata["PhysicalSizeXUnit"] = "µm"
        metadata["PhysicalSizeY"] = pixel_size
        metadata["PhysicalSizeYUnit"] = "µm"
    metadata["Description"] = (
        f"Assembled by octacam from {plan.frame_count} per-frame TIFFs "
        f"(channel {plan.channel})"
    )
    return metadata


def _read_single_page(path: Path):
    """The raw pixel array of *path*'s own first (and only) IFD — never
    ``tifffile.imread(path)``, which is unsafe here: real ThorImage per-frame
    TIFFs embed OME-XML in their first file that cross-references every
    sibling file in the whole channel/Z/T set by filename (confirmed on real
    data — a Z-stack's first file's OME-XML describes the *entire*
    SizeC=2/SizeZ=9 dataset via ``<TiffData FirstZ FirstC><UUID
    FileName=.../>`` entries per physical file), and ``imread`` follows that
    linkage, silently returning a multi-file-stitched array instead of just
    this one file's own frame."""
    import tifffile

    with tifffile.TiffFile(path) as tf:
        return tf.pages[0].asarray()


def assemble_channel_stack(
    source_files: list[Path],
    dest_path: Path,
    *,
    axis: Literal["T", "Z"] = "T",
    experiment_xml: Path | None = None,
    full_verify: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> AssembleResult:
    """Stream *source_files* (already frame-ordered by the caller) into one
    OME-TIFF at *dest_path* — one frame at a time, so this is safe on a
    multi-GB channel (never holds more than one frame's pixel data in
    memory). Writes to a unique sibling temp file, fsyncs, verifies the
    written page count always and (when *full_verify*) every page's pixel
    content against its source file, then ``os.replace()``s onto
    *dest_path* only after verification passes. Never raises: any failure
    (missing/corrupt source file, a tifffile write/read error, a verify
    mismatch) aborts, removes the temp file, and returns ``ok=False`` with a
    reason — the caller decides the fallback. Leaves no partial file at
    *dest_path* on failure."""
    import tifffile

    n = len(source_files)
    try:
        first_frame = _read_single_page(source_files[0])
    except Exception as e:  # noqa: BLE001 - any unreadable source file
        return AssembleResult(ok=False, error=f"could not read {source_files[0]}: {e}")
    shape = (n, *first_frame.shape)
    dtype = first_frame.dtype

    metadata = None
    if experiment_xml is not None:
        plan_stub = ChannelPlan(
            channel=dest_path.stem, status="ready", source_files=source_files,
            dest_name=dest_path.name, axis=axis, frame_count=n,
        )
        metadata = _ome_metadata(plan_stub, experiment_xml)

    temp_path = dest_path.with_name(f"{dest_path.name}{_TEMP_SUFFIX}-{uuid.uuid4().hex[:8]}")

    def _frames():
        for i, f in enumerate(source_files, 1):
            frame = first_frame if f is source_files[0] else _read_single_page(f)
            if frame.shape != first_frame.shape or frame.dtype != first_frame.dtype:
                raise ValueError(
                    f"{f} has shape/dtype {frame.shape}/{frame.dtype}, "
                    f"expected {first_frame.shape}/{first_frame.dtype}"
                )
            if on_progress is not None:
                on_progress(i, n)
            yield frame

    # Streams one frame at a time through tifffile's own iterator support —
    # only ever holds one frame's pixel data in memory, safe on a multi-GB
    # channel. A single .write() call (not one call per frame) is required
    # for tifffile to record correct OME-XML dimension metadata for the
    # whole series up front.
    try:
        with tifffile.TiffWriter(temp_path, bigtiff=True, ome=True) as writer:
            writer.write(_frames(), shape=shape, dtype=dtype, metadata=metadata)
    except Exception as e:  # noqa: BLE001 - any tifffile write / source-read failure
        temp_path.unlink(missing_ok=True)
        return AssembleResult(ok=False, error=f"failed writing {temp_path}: {e}")

    try:
        with tifffile.TiffFile(temp_path) as tf:
            written = len(tf.pages)
            if written != n:
                temp_path.unlink(missing_ok=True)
                return AssembleResult(
                    ok=False,
                    error=f"wrote {written} page(s), expected {n}",
                )
            if full_verify:
                for i, (page, src) in enumerate(zip(tf.pages, source_files, strict=True)):
                    written_frame = page.asarray()
                    source_frame = _read_single_page(src)
                    if written_frame.shape != source_frame.shape or (
                        written_frame != source_frame
                    ).any():
                        temp_path.unlink(missing_ok=True)
                        return AssembleResult(
                            ok=False,
                            error=f"page {i} does not match source {src}",
                        )
    except Exception as e:  # noqa: BLE001 - any tifffile verify failure
        temp_path.unlink(missing_ok=True)
        return AssembleResult(ok=False, error=f"failed verifying {temp_path}: {e}")

    try:
        fd = os.open(temp_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_path, dest_path)
    except OSError as e:
        temp_path.unlink(missing_ok=True)
        return AssembleResult(ok=False, error=f"failed promoting {temp_path}: {e}")

    return AssembleResult(ok=True, frame_count=n, dest_path=dest_path)


def make_progress_logger(folder: Path, channel: str, interval_s: float = 8.0) -> Callable[[int, int], None]:
    """A time-gated progress callback for :func:`assemble_channel_stack` —
    logs every *interval_s* seconds and always on the final frame, never once
    per frame (a 1500-frame channel would otherwise reproduce exactly the
    log-spam problem this feature exists to fix)."""
    state = {"last": 0.0}

    def _on_progress(i: int, n: int) -> None:
        now = time.monotonic()
        if i == n or now - state["last"] >= interval_s:
            state["last"] = now
            log.info("2P tiff-assemble: %s %s: %d/%d frame(s)", folder, channel, i, n)

    return _on_progress
