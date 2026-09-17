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

A "FastZ" acquisition (ThorImage's own ``zFastEnable`` — recording several Z
planes fast enough to sample a transient, e.g. seeing both a cell body and
its axon at once) varies *two* of the 4 filename numeric groups
simultaneously — one per Z-plane, one per timepoint — confirmed on real data
(``ZStage steps="4"`` + ``Timelapse timepoints="50"``, 200 real files, every
Z-plane's own timepoint sequence contiguous). This still produces one file
per (channel, Z-plane) — every timepoint for that plane in one stack, not a
single combined 4-D file — rather than guess at an interleaving order for a
true 4-D file; see :func:`_plan_fastz_channel`.

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
    """``<ZStage steps="...">`` — the Z-axis frame count for a Z-stack *or* a
    simultaneous multi-plane ("FastZ") acquisition, or None if
    missing/unparseable/neither is actually active.

    Two distinct ways real ThorImage data enables Z motion, both confirmed
    on real data: a traditional step-and-shoot Z-stack sets ``ZStage
    enable="1"`` directly; a FastZ acquisition (the piezo moves *during* one
    continuous streaming acquisition, not between discrete stops) instead
    leaves ``ZStage enable="0"`` and signals Z motion via
    ``<Streaming enable="1" zFastEnable="1">`` — confirmed on a real
    ``Fly1_FastZ_Test``, where checking only ``ZStage``'s own ``enable``
    would have missed real, active Z motion entirely."""
    try:
        root = ET.parse(experiment_xml).getroot()
    except (ET.ParseError, OSError):
        return None
    zstage_el = root.find("ZStage")
    if zstage_el is None:
        return None
    zstage_enabled = zstage_el.get("enable") == "1"
    if not zstage_enabled:
        streaming_el = root.find("Streaming")
        fastz = (
            streaming_el is not None
            and streaming_el.get("enable") == "1"
            and streaming_el.get("zFastEnable") == "1"
        )
        if not fastz:
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


def _varying_positions(groups: list[tuple[int, int, int, int]]) -> list[int]:
    """Every one of the 4 numeric positions that varies across *groups*."""
    return [i for i in range(4) if len({g[i] for g in groups}) > 1]


def _varying_position(groups: list[tuple[int, int, int, int]]) -> int | None:
    """Which single one of the 4 numeric positions actually varies across
    *groups* — generic over streaming (position 3 varies, confirmed real
    data) vs. Z-stack (position 2 varies, confirmed real data) file layouts,
    never a hardcoded position. None when zero or more than one position
    varies (ambiguous file set — never guessed)."""
    varying = _varying_positions(groups)
    return varying[0] if len(varying) == 1 else None


def _plan_fastz_channel(
    channel: str,
    found: list[tuple[Path, tuple[int, int, int, int]]],
    varying: list[int],
    timepoints: int | None,
    zsteps: int | None,
) -> list[ChannelPlan] | None:
    """Plan a simultaneous multi-plane ("FastZ") channel — exactly two of the
    4 filename numeric groups vary at once, one per Z-plane and one per
    timepoint, rather than the single-axis streaming/Z-stack case
    :func:`plan_assembly` otherwise handles. Confirmed on real data
    (ThorImage's own ``zFastEnable`` acquisitions, e.g. a real
    ``Fly1_FastZ_Test``: ``ZStage steps="4"`` + ``Timelapse timepoints="50"``
    + ``Streaming enable="1"``, real files with Z values 1-4 crossed with T
    values 1-50, 200 files total, every Z-plane's own T-sequence contiguous
    1..50).

    Output shape (Matthias's own preference, 2026-09-17): one TIFF per
    (channel, Z-plane) — every timepoint for that plane, not one combined
    4-D file — which is exactly the existing single-axis assembly case,
    just partitioned by Z first: split ``found`` into one group per distinct
    Z value, then apply the same ordering/contiguity check
    :func:`plan_assembly` already does for a single varying axis to each
    group independently.

    Returns one ``"ready"`` :class:`ChannelPlan` per Z-plane when both
    varying positions unambiguously match ``zsteps``/``timepoints`` (by
    distinct-value count — the same count-based axis discrimination the
    single-axis case uses) and every Z-plane's own timepoint sequence is
    contiguous. Returns ``None`` — never a partial/guessed result — when
    either axis can't be configured, both counts could plausibly be either
    axis (e.g. ``zsteps == timepoints``, genuinely unresolvable from counts
    alone), or any single Z-plane's own file set doesn't cleanly resolve;
    the caller falls back to one "problem" entry for the whole channel."""
    if timepoints is None or zsteps is None or timepoints == zsteps:
        return None
    counts = {i: len({g[i] for _, g in found}) for i in varying}
    if counts[varying[0]] == zsteps and counts[varying[1]] == timepoints:
        z_pos, t_pos = varying[0], varying[1]
    elif counts[varying[1]] == zsteps and counts[varying[0]] == timepoints:
        z_pos, t_pos = varying[1], varying[0]
    else:
        return None

    by_z: dict[int, list[tuple[Path, tuple[int, int, int, int]]]] = {}
    for item in found:
        by_z.setdefault(item[1][z_pos], []).append(item)

    plans: list[ChannelPlan] = []
    for z in sorted(by_z):
        z_items = sorted(by_z[z], key=lambda item: item[1][t_pos])
        t_values = [item[1][t_pos] for item in z_items]
        if len(z_items) != timepoints or t_values != list(
            range(t_values[0], t_values[0] + len(t_values))
        ):
            return None  # one bad Z-plane invalidates the whole channel
        plans.append(
            ChannelPlan(
                channel,
                "ready",
                [p for p, _ in z_items],
                f"{channel}_Z{z:02d}.tif",
                axis="T",
                frame_count=timepoints,
            )
        )
    return plans


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
        varying = _varying_positions([g for _, g in found])
        if len(varying) == 2:
            fastz_plans = _plan_fastz_channel(channel, found, varying, timepoints, zsteps)
            if fastz_plans is not None:
                plans.extend(fastz_plans)
            else:
                plans.append(ChannelPlan(
                    channel, "problem", [p for p, _ in found], dest_name,
                    reason=f"ambiguous multi-axis frame naming: two numeric "
                           f"groups vary (positions {varying}) but couldn't be "
                           f"matched to Experiment.xml's ZStage steps={zsteps} "
                           f"/ Timelapse timepoints={timepoints}",
                ))
            continue
        pos = varying[0] if len(varying) == 1 else None
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
