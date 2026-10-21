"""Maximum-intensity projections of anatomical 2P Z-stacks.

For every transferred ThorImage Z-stack (one timepoint, `ZStage` stepped,
not streaming -- see `is_zstack`), writes `zstack_mip_green.png`,
`zstack_mip_red.png` and `zstack_mip_dual.png` (the same names the lab's
earlier hand-made renders used) beside the recording: the
`RecordingN_2P_<detail>/` wrapper once reconciled (its `Renderings/` in the
older layout), else the 2P folder's own `Renderings/`.

Each slice is lightly box-blurred before the max (a raw MIP keeps the
brightest *noise* pixel of every column, so an unsmoothed one is speckled),
then each channel is stretched between percentiles of its own MIP and the
image is corrected to the physical um aspect. Channel -> color mapping is
shared with the synced render (ChanA green, ChanB red by default).

Reads the assembled `ChanX.tif` stacks one page at a time (constant
memory); a Z-stack whose per-frame TIFFs were never assembled is skipped.
Best-effort: `render_zstack_mips` logs and returns `None` instead of
raising.
"""

from __future__ import annotations

import logging
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("octacam")

MIP_FILENAMES = {
    "green": "zstack_mip_green.png",
    "red": "zstack_mip_red.png",
    "dual": "zstack_mip_dual.png",
}


@dataclass(frozen=True)
class MipOptions:
    green_channel: str = "ChanA"
    red_channel: str = "ChanB"
    low_percentile: float = 0.5
    high_percentile: float = 99.8
    smooth_px: int = 1


def options_from_config(twophoton_cfg) -> MipOptions | None:
    """MIP options from the 2P settings (`settings`), or None when
    `render_zstack_mip` is off.
    """
    if twophoton_cfg is None or not getattr(twophoton_cfg, "render_zstack_mip", True):
        return None
    return MipOptions(
        green_channel=twophoton_cfg.synced_green_channel,
        red_channel=twophoton_cfg.synced_red_channel,
    )


def is_zstack(image_dir: Path) -> bool:
    """A step-and-shoot anatomical Z-stack: `ZStage enable="1"` with more
    than one step, a single timepoint, and streaming off. Streaming and FastZ
    captures also carry an enabled `ZStage` (real data: `Fly1_Streaming`
    has `steps="201"`) -- the timepoint/streaming checks rule those out.
    """
    from octacam.plugins.twophoton.matching import EXPERIMENT_XML_FILENAME

    try:
        root = ET.parse(image_dir / EXPERIMENT_XML_FILENAME).getroot()
    except ET.ParseError, OSError:
        return False
    z = root.find("ZStage")
    stream = root.find("Streaming")
    timelapse = root.find("Timelapse")
    try:
        steps = int(z.get("steps", "0")) if z is not None else 0
        timepoints = (
            int(timelapse.get("timepoints", "1")) if timelapse is not None else 1
        )
    except ValueError:
        return False
    return (
        z is not None
        and z.get("enable") == "1"
        and steps > 1
        and timepoints <= 1
        and (stream is None or stream.get("enable") != "1")
    )


def mip_output_dir(image_dir: Path) -> Path:
    """`<wrapper>` (or its legacy `Renderings/`) for `<wrapper>/2P/<name>`,
    else `<image_dir>/Renderings` -- never among the raw ThorImage files.
    """
    from octacam.plugins.twophoton.layout import (
        LEGACY_RENDERINGS_DIRNAME,
        TWOPHOTON_DIRNAME,
        renders_dir,
    )

    if image_dir.parent.name == TWOPHOTON_DIRNAME:
        return renders_dir(image_dir.parent.parent)
    return image_dir / LEGACY_RENDERINGS_DIRNAME


def find_zstacks(root: Path) -> Iterator[Path]:
    """Every Z-stack image folder under *root* (any depth), sorted."""
    from octacam.plugins.twophoton.matching import EXPERIMENT_XML_FILENAME
    from octacam.plugins.twophoton.render import channel_stacks

    if (root / EXPERIMENT_XML_FILENAME).is_file():
        candidates = [root]
    else:
        candidates = sorted(p.parent for p in root.rglob(EXPERIMENT_XML_FILENAME))
    for folder in candidates:
        # A reconciled take keeps a copy of Experiment.xml at its own root
        # too; only the folder that actually holds the stacks counts.
        if is_zstack(folder) and channel_stacks(folder):
            yield folder


def _write_png(path: Path, rgb) -> None:
    from octacam.ffmpeg import find_ffmpeg

    h, w, _ = rgb.shape
    # Temp then rename: a killed run must not leave a truncated PNG that the
    # next run's "all outputs exist" check would accept as done.
    tmp = path.with_name(f".{path.stem}.partial.png")
    try:
        subprocess.run(
            [
                find_ffmpeg(),
                "-v",
                "error",
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                f"{w}x{h}",
                "-i",
                "-",
                "-frames:v",
                "1",
                "-f",
                "image2",
                str(tmp),
            ],
            input=rgb.tobytes(),
            check=True,
        )
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def render_zstack_mips(
    image_dir: Path, options: MipOptions | None = None, *, force: bool = False
) -> Path | None:
    """Write the MIP PNGs for one Z-stack folder; returns the output dir, or
    None when skipped/failed (reason logged). Never raises.
    """
    try:
        return _render(image_dir, options or MipOptions(), force)
    except Exception:
        log.exception("zstack MIP: failed for %s", image_dir)
        return None


def _render(image_dir: Path, opts: MipOptions, force: bool) -> Path | None:
    import numpy as np

    from octacam.plugins.twophoton.matching import EXPERIMENT_XML_FILENAME
    from octacam.plugins.twophoton.render import (
        _box_blur,
        _pixel_aspect,
        channel_stacks,
    )

    out_dir = mip_output_dir(image_dir)
    if not force and all((out_dir / n).exists() for n in MIP_FILENAMES.values()):
        log.info("zstack MIP: %s already rendered -- skipping", image_dir)
        return out_dir
    try:
        import tifffile
    except ImportError:
        log.warning("zstack MIP: needs the 'twophoton' extra (tifffile) -- skipping")
        return None
    stacks = {c: ps[0] for c, ps in channel_stacks(image_dir).items() if len(ps) == 1}
    if not stacks:
        log.info("zstack MIP: skipping %s (no assembled ChanX.tif stack)", image_dir)
        return None

    mips = {}
    for chan, path in stacks.items():
        with tifffile.TiffFile(path) as tif:
            mip = None
            for k in range(len(tif.pages)):
                # One page at a time, never imread(): see `tiff`.
                img = tif.asarray(key=k).astype(np.float32)
                if opts.smooth_px > 0:
                    img = _box_blur(img, opts.smooth_px)
                mip = img if mip is None else np.maximum(mip, img)
        if mip is not None:
            mips[chan] = mip
    if not mips:
        return None

    aspect = _pixel_aspect(image_dir / EXPERIMENT_XML_FILENAME)

    def to_u8(img):
        lo, hi = np.percentile(img, [opts.low_percentile, opts.high_percentile])
        u8 = (np.clip((img - lo) / max(hi - lo, 1e-6), 0, 1) * 255).astype(np.uint8)
        if abs(aspect - 1.0) > 0.02:
            rows = np.minimum(
                (np.arange(max(2, round(u8.shape[0] * aspect))) / aspect).astype(int),
                u8.shape[0] - 1,
            )
            u8 = u8[rows]
        return u8

    norm = {c: to_u8(m) for c, m in mips.items()}
    shape = next(iter(norm.values())).shape
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    green = norm.get(opts.green_channel)
    red = norm.get(opts.red_channel)
    if green is None and red is None:  # unmapped channel names: one gray MIP
        gray = next(iter(norm.values()))
        rgb = np.repeat(gray[..., None], 3, axis=2)
        _write_png(out_dir / MIP_FILENAMES["dual"], np.ascontiguousarray(rgb))
        written.append(MIP_FILENAMES["dual"])
    else:
        dual = np.zeros((*shape, 3), np.uint8)
        if green is not None:
            g = np.zeros((*shape, 3), np.uint8)
            g[..., 1] = dual[..., 1] = green
            _write_png(out_dir / MIP_FILENAMES["green"], g)
            written.append(MIP_FILENAMES["green"])
        if red is not None:
            r = np.zeros((*shape, 3), np.uint8)
            r[..., 0] = dual[..., 0] = red
            _write_png(out_dir / MIP_FILENAMES["red"], r)
            written.append(MIP_FILENAMES["red"])
        if green is not None and red is not None:
            _write_png(out_dir / MIP_FILENAMES["dual"], dual)
            written.append(MIP_FILENAMES["dual"])
    log.info("zstack MIP: wrote %s -> %s", ", ".join(written), out_dir)
    return out_dir
