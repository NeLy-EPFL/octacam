"""Synced behavior + 2-photon preview video (``Renderings/synced_2p.mp4``).

A quick-look render, not an analysis product: the behavior grid on the left,
the 2P imaging as a green/red merge on the right, frame-aligned on
ThorSync's own DAQ clock. Behavior frame *k* is the take's *k*-th
``DI/Cameras`` edge and 2P frame *j* is its *j*-th ``DI/FrameOut`` edge,
both inside the ``CaptureOn`` segment whose camera-edge count verified the
pairing (see ``twophoton_signals``) — so the two halves share one clock and
never drift, unlike wall-clock file timestamps.

Works on an already-transferred take folder (NAS layout: ``Renderings/
grid.mp4``, ``2P/<ThorImage>/ChanX[_ZNN].tif`` assembled stacks,
``2P/SyncData*/Episode001.h5``, ``twophoton_match.json``), so the same code
serves the automatic post-transfer hook in ``octacam process`` and the
standalone ``octacam render`` backfill.

**FastZ** (``ChanX_Z01..ZNN.tif``, one stack per plane): each timepoint is
projected across its Z planes (``mean`` by default, which averages the
planes into a steadier image; ``max`` is also available) and timestamped at
the first ``FrameOut`` edge of its volume — a volume spans ``planes +
flybackFrames`` edges (confirmed on real data: 500 timepoints x (5 + 1) =
3000 edges).

**Look**: each channel is linearly stretched between fixed percentiles
sampled across the whole recording (fixed, so the video doesn't flicker
frame to frame), then mapped green/red. ThorImage records no per-channel
color, so the mapping is configurable; the default (ChanA green, ChanB red)
matches the 2P rig (confirmed by the operator). Non-square pixels are
corrected to the physical µm aspect from ``Experiment.xml``.

Best-effort like the rest of the 2P pipeline: :func:`render_synced_video`
logs and returns ``None`` on any missing input instead of raising, so a
render can never fail a transfer.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("octacam")

SYNCED_VIDEO_FILENAME = "synced_2p.mp4"
RENDERINGS_DIRNAME = "Renderings"
_GRID_FILENAME = "grid.mp4"
_CHANNEL_FILE_RE = re.compile(r"^(?P<chan>Chan[A-Z])(?:_Z(?P<z>\d+))?\.tif$")
_N_CONTRAST_SAMPLES = 60
_ASPECT_TOLERANCE = 0.02


@dataclass(frozen=True)
class SyncedRenderOptions:
    green_channel: str = "ChanA"
    red_channel: str = "ChanB"
    fastz_projection: str = "mean"  # "mean" | "max"
    fps: float = 25.0
    height: int = 720
    # Background-anchored stretch: sparse photon-count channels have no
    # meaningful dark floor, so the median (mostly background) maps to black.
    low_percentile: float = 50.0
    high_percentile: float = 99.9
    # Box-blur radius in pixels (0 = off): single-plane streaming frames are
    # dominated by per-pixel shot noise at quick-look scale.
    smooth_px: int = 1
    crf: int = 28


def options_from_config(twophoton_cfg) -> SyncedRenderOptions | None:
    """Render options from a ``[transfer.twophoton]`` config, or None when
    ``render_synced_video`` is off."""
    if twophoton_cfg is None or not getattr(twophoton_cfg, "render_synced_video", True):
        return None
    return SyncedRenderOptions(
        green_channel=twophoton_cfg.synced_green_channel,
        red_channel=twophoton_cfg.synced_red_channel,
        fastz_projection=twophoton_cfg.synced_fastz_projection,
    )


@dataclass(frozen=True)
class SyncedInputs:
    behavior_video: Path
    image_dir: Path
    sync_dir: Path


def find_synced_inputs(take_dir: Path) -> SyncedInputs | str:
    """Locate a transferred take's render inputs, or a reason string why it
    can't be rendered (not a synced take, a missing piece, ...)."""
    from octacam.transform import TWOPHOTON_MATCH_FILENAME

    try:
        record = json.loads((take_dir / TWOPHOTON_MATCH_FILENAME).read_text())
    except (OSError, ValueError):
        return f"no readable {TWOPHOTON_MATCH_FILENAME}"
    names = {m.get("kind"): Path(m.get("path", "")).name for m in record.get("matched", [])}
    if "image" not in names or "sync" not in names:
        return "needs both a matched ThorImage and a matched SyncData folder"
    image_dir = take_dir / "2P" / names["image"]
    sync_dir = take_dir / "2P" / names["sync"]
    for d in (image_dir, sync_dir):
        if not d.is_dir():
            return f"missing {d}"
    grid = take_dir / RENDERINGS_DIRNAME / _GRID_FILENAME
    if grid.is_file():
        behavior = grid
    else:
        mp4s = sorted((take_dir / "Behavior").glob("*.mp4"))
        if not mp4s:
            return "no grid.mp4 and no Behavior/*.mp4"
        behavior = mp4s[0]
    return SyncedInputs(behavior, image_dir, sync_dir)


def channel_stacks(image_dir: Path) -> dict[str, list[Path]]:
    """``{channel: [stack, ...]}`` — one stack for a plain stream, one per Z
    plane (sorted) for FastZ. Ignores ThorImage's ``*_Preview.tif``."""
    found: dict[str, list[tuple[int, Path]]] = {}
    for p in image_dir.iterdir():
        m = _CHANNEL_FILE_RE.match(p.name)
        if m:
            found.setdefault(m["chan"], []).append((int(m["z"] or 0), p))
    return {c: [p for _, p in sorted(v)] for c, v in sorted(found.items())}


def _xml_attr(root, tag: str, attr: str) -> str | None:
    el = root.find(tag)
    return None if el is None else el.get(attr)


def _volume_frames(experiment_xml: Path, n_planes: int) -> int:
    """FrameOut edges per timepoint: 1 for a plain stream, planes + flyback
    for FastZ."""
    if n_planes <= 1:
        return 1
    try:
        root = ET.parse(experiment_xml).getroot()
        flyback = int(_xml_attr(root, "Streaming", "flybackFrames") or 0)
    except (ET.ParseError, OSError, ValueError):
        flyback = 0
    return n_planes + flyback


def _pixel_aspect(experiment_xml: Path) -> float:
    """Physical height/width of one pixel (1.0 = square)."""
    try:
        root = ET.parse(experiment_xml).getroot()
        px = float(_xml_attr(root, "LSM", "pixelX") or 0)
        py = float(_xml_attr(root, "LSM", "pixelY") or 0)
        wu = float(_xml_attr(root, "LSM", "widthUM") or 0)
        hu = float(_xml_attr(root, "LSM", "heightUM") or 0)
    except (ET.ParseError, OSError, ValueError):
        return 1.0
    if min(px, py, wu, hu) <= 0:
        return 1.0
    return (hu / py) / (wu / px)


class _TwoPhotonReader:
    """Projected per-timepoint frames, one float32 array per channel."""

    def __init__(self, stacks: dict[str, list[Path]], projection: str):
        import tifffile

        self._files = {c: [tifffile.TiffFile(p) for p in ps] for c, ps in stacks.items()}
        self.n_planes = max(len(v) for v in self._files.values())
        self.n_frames = min(len(f.pages) for v in self._files.values() for f in v)
        self._projection = projection

    def frame(self, index: int, smooth_px: int = 0) -> dict:
        import numpy as np

        out = {}
        for chan, files in self._files.items():
            # pages[i].asarray(), never imread(): ThorImage OME-XML links
            # sibling files and imread would follow it (see twophoton_tiff).
            planes = [f.pages[index].asarray().astype(np.float32) for f in files]
            if len(planes) == 1:
                out[chan] = planes[0]
            elif self._projection == "max":
                out[chan] = np.max(planes, axis=0)
            else:
                out[chan] = np.mean(planes, axis=0)
            if smooth_px > 0:
                out[chan] = _box_blur(out[chan], smooth_px)
        return out

    def close(self) -> None:
        for files in self._files.values():
            for f in files:
                f.close()


def _box_blur(img, radius: int):
    """Separable (2r+1)^2 box mean via cumulative sums, edge-padded."""
    import numpy as np

    k = 2 * radius + 1
    padded = np.pad(img, radius, mode="edge")
    c = np.cumsum(padded, axis=0, dtype=np.float32)
    c = np.vstack([np.zeros((1, c.shape[1]), np.float32), c])
    rows = (c[k:] - c[:-k]) / k
    c = np.cumsum(rows, axis=1, dtype=np.float32)
    c = np.hstack([np.zeros((c.shape[0], 1), np.float32), c])
    return (c[:, k:] - c[:, :-k]) / k


def _probe_size(ffmpeg: str, video: Path) -> tuple[int, int] | None:
    proc = subprocess.run([ffmpeg, "-i", str(video)], capture_output=True, text=True)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", proc.stderr)
    return (int(m[1]), int(m[2])) if m else None


def _pick_segment(sync_dir: Path, frame_counts: list[int]):
    from octacam.twophoton_signals import camera_edge_diff, read_capture_segments

    segments = read_capture_segments(sync_dir) or []
    ranked = [
        (d, s) for s in segments if (d := camera_edge_diff(s, frame_counts)) is not None
    ]
    if not ranked:
        return None
    return min(ranked, key=lambda r: r[0])[1]


def render_synced_video(
    take_dir: Path,
    options: SyncedRenderOptions | None = None,
    *,
    force: bool = False,
) -> Path | None:
    """Render ``<take_dir>/Renderings/synced_2p.mp4``. Returns its path, or
    None when skipped/failed (reason logged). Skips an existing output unless
    *force*. Never raises."""
    try:
        return _render(take_dir, options or SyncedRenderOptions(), force)
    except Exception:
        log.exception("synced render: failed for %s", take_dir)
        return None


def _render(take_dir: Path, opts: SyncedRenderOptions, force: bool) -> Path | None:
    import numpy as np

    from octacam.transform import RECORDING_SUMMARY_FILENAME
    from octacam.twophoton_signals import read_segment_edge_times
    from octacam.twophoton_transfer import (
        EXPERIMENT_XML_FILENAME,
        thorimage_frame_average,
    )
    from octacam.writer import find_ffmpeg

    out = take_dir / RENDERINGS_DIRNAME / SYNCED_VIDEO_FILENAME
    if out.exists() and not force:
        log.info("synced render: %s already exists — skipping", out)
        return out
    inputs = find_synced_inputs(take_dir)
    if isinstance(inputs, str):
        log.info("synced render: skipping %s (%s)", take_dir, inputs)
        return None
    try:
        import tifffile  # noqa: F401
    except ImportError:
        log.warning("synced render: needs the 'twophoton' extra (tifffile) — skipping")
        return None

    try:
        summary = json.loads((take_dir / RECORDING_SUMMARY_FILENAME).read_text())
    except (OSError, ValueError):
        log.info("synced render: skipping %s (no recording_summary.json)", take_dir)
        return None
    frame_counts = [
        c["frames"] for c in summary.get("cameras", []) if isinstance(c.get("frames"), int)
    ]
    segment = _pick_segment(inputs.sync_dir, frame_counts)
    if segment is None:
        log.info("synced render: skipping %s (no verified CaptureOn segment)", take_dir)
        return None
    edges = read_segment_edge_times(inputs.sync_dir, segment)
    if edges is None or not edges[0] or not edges[1]:
        log.info("synced render: skipping %s (no camera/FrameOut edges)", take_dir)
        return None
    cam_times = np.asarray(edges[0])
    frameout_times = np.asarray(edges[1])

    stacks = channel_stacks(inputs.image_dir)
    if not stacks:
        log.info("synced render: skipping %s (no assembled ChanX.tif stacks)", take_dir)
        return None
    reader = _TwoPhotonReader(stacks, opts.fastz_projection)
    try:
        xml = inputs.image_dir / EXPERIMENT_XML_FILENAME
        # FrameOut pulses once per scan: a frame-averaged capture scans
        # `average` times per saved frame (real 2026-10-14 Fly4/003: x4).
        average = thorimage_frame_average(xml)
        per_volume = _volume_frames(xml, reader.n_planes) * average
        n_vol = min(reader.n_frames, len(frameout_times) // per_volume)
        if n_vol == 0:
            log.info("synced render: skipping %s (no complete 2P volume)", take_dir)
            return None
        if len(frameout_times) != reader.n_frames * per_volume:
            log.warning(
                "synced render: %s has %d FrameOut edges but %d frames x %d per volume "
                "— aligning the first %d",
                take_dir, len(frameout_times), reader.n_frames, per_volume, n_vol,
            )
        if average > 1:
            # An averaged frame spans its scans: time it at their middle.
            vol_times = frameout_times[: n_vol * per_volume].reshape(n_vol, per_volume).mean(axis=1)
        else:
            vol_times = frameout_times[: n_vol * per_volume : per_volume]

        # Fixed contrast limits per channel from frames sampled across the run.
        sample_idx = np.unique(np.linspace(0, n_vol - 1, min(n_vol, _N_CONTRAST_SAMPLES)).astype(int))
        samples: dict[str, list] = {}
        for i in sample_idx:
            for chan, img in reader.frame(int(i), opts.smooth_px).items():
                samples.setdefault(chan, []).append(img[::4, ::4])
        limits = {
            chan: (
                float(np.percentile(v, opts.low_percentile)),
                float(np.percentile(v, opts.high_percentile)),
            )
            for chan, v in samples.items()
        }

        img_h, img_w = next(iter(reader.frame(0).values())).shape
        aspect = _pixel_aspect(xml)
        row_map = None
        if abs(aspect - 1.0) > _ASPECT_TOLERANCE:
            new_h = max(2, round(img_h * aspect))
            row_map = np.minimum((np.arange(new_h) / aspect).astype(int), img_h - 1)
            img_h = new_h
        panel_h = img_h + (img_h % 2)

        def colorize(frame: dict) -> np.ndarray:
            rgb = np.zeros((img_h, img_w, 3), dtype=np.uint8)

            def norm(chan: str) -> np.ndarray:
                lo, hi = limits[chan]
                x = frame[chan] if row_map is None else frame[chan][row_map]
                return (np.clip((x - lo) / max(hi - lo, 1e-6), 0, 1) * 255).astype(np.uint8)

            g, r = opts.green_channel, opts.red_channel
            if g in frame or r in frame:
                if g in frame:
                    rgb[..., 1] = norm(g)
                if r in frame:
                    rgb[..., 0] = norm(r)
            else:  # neither configured channel present: grayscale the first one
                gray = norm(next(iter(frame)))
                rgb[...] = gray[..., None]
            return rgb

        ffmpeg = find_ffmpeg()
        size = _probe_size(ffmpeg, inputs.behavior_video)
        if size is None:
            log.info("synced render: skipping %s (cannot probe %s)", take_dir, inputs.behavior_video)
            return None
        beh_w = round(size[0] * panel_h / size[1] / 2) * 2
        beh_frame_bytes = beh_w * panel_h * 3
        total_w = beh_w + img_w + (img_w % 2)

        # Output timeline: real time from the first to the last camera edge.
        n_out = int((cam_times[-1] - cam_times[0]) * opts.fps) + 1
        t_out = cam_times[0] + np.arange(n_out) / opts.fps
        beh_idx = np.searchsorted(cam_times, t_out, side="right") - 1
        img_idx = np.searchsorted(vol_times, t_out, side="right") - 1

        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".partial.mp4")
        decode = subprocess.Popen(
            [ffmpeg, "-v", "error", "-i", str(inputs.behavior_video),
             "-vf", f"scale={beh_w}:{panel_h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE,
        )
        encode = subprocess.Popen(
            [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{total_w}x{panel_h}", "-r", f"{opts.fps}", "-i", "-",
             "-vf", f"scale=-2:{opts.height}:flags=lanczos",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(opts.crf),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)],
            stdin=subprocess.PIPE,
        )
        assert decode.stdout is not None and encode.stdin is not None
        log.info(
            "synced render: %s — %d output frames, %d 2P timepoints (%d plane(s), %s)",
            take_dir, n_out, n_vol, reader.n_planes,
            opts.fastz_projection if reader.n_planes > 1 else "single plane",
        )
        canvas = np.zeros((panel_h, total_w, 3), dtype=np.uint8)
        decoded = -1
        beh = np.zeros((panel_h, beh_w, 3), dtype=np.uint8)
        cur_img = -2
        ok = True
        try:
            for k in range(n_out):
                want = int(beh_idx[k])
                while decoded < want:
                    buf = decode.stdout.read(beh_frame_bytes)
                    if len(buf) < beh_frame_bytes:
                        break  # video shorter than its edge count: hold the last frame
                    beh = np.frombuffer(buf, np.uint8).reshape(panel_h, beh_w, 3)
                    decoded += 1
                canvas[:, :beh_w] = beh
                j = int(img_idx[k])
                if j != cur_img:
                    canvas[:, beh_w:] = 0
                    if 0 <= j < n_vol:
                        canvas[:img_h, beh_w : beh_w + img_w] = colorize(reader.frame(j, opts.smooth_px))
                    cur_img = j
                encode.stdin.write(canvas.tobytes())
                if n_out >= 10 and k % (n_out // 10) == 0 and k:
                    log.info("synced render: %s %d%%", out.name, round(100 * k / n_out))
        except BrokenPipeError:
            ok = False
        finally:
            encode.stdin.close()
            decode.stdout.close()
            decode.kill()
            decode.wait()
            ok = encode.wait() == 0 and ok
        if not ok:
            log.error("synced render: ffmpeg failed for %s", take_dir)
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(out)
        log.info("synced render: wrote %s", out)
        return out
    finally:
        reader.close()
