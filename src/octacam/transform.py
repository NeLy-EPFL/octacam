"""Display transforms (rotation + flips) shared by recording and transcoding.

The web GUI shows each camera through a CSS transform ``scale(sx, sy)
rotate(deg)`` applied to the raw frame (see ``web/static/js/grid.js``). To bake
that same orientation into a recorded/transcoded video we must reproduce it
exactly, in pixels.

CSS composes the transform list right-to-left, so the matrix is ``S · R``: a
point is first rotated, then scaled/flipped along the (unrotated) screen axes.
In pixel terms that means **rotate first, then flip** — and CSS ``rotate(+deg)``
turns clockwise. Both the numpy path (record-time baking) and the ffmpeg ``-vf``
path (transcode) implement that ordering so they produce identical pixels.

Only the transforms the View tab can actually produce are supported: rotation in
90° steps plus horizontal/vertical flips (a flip is a negative ``scale_x`` /
``scale_y`` in the config; the scale magnitude is always 1 and is ignored). A
``rotation_deg`` that is not a multiple of 90 is dropped with a warning rather
than approximated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from octacam.config import CameraConfig

log = logging.getLogger("octacam")

# Per-recording metadata file written into each recording's save directory and
# consumed by `octacam transcode`. The shared on-disk vocabulary lives here,
# next to DisplayTransform, so the recording and transcode sides never drift.
RECORDING_SUMMARY_FILENAME = "recording_summary.json"


@dataclass(frozen=True)
class DisplayTransform:
    """A bakeable display orientation: a 90° rotation step plus flips.

    ``rotation_deg`` is one of 0/90/180/270 and is interpreted clockwise (to
    match CSS). Flips are applied *after* the rotation, along the screen axes.
    """

    rotation_deg: int = 0
    flip_h: bool = False
    flip_v: bool = False

    @property
    def is_identity(self) -> bool:
        return self.rotation_deg == 0 and not self.flip_h and not self.flip_v

    def output_size(self, width: int, height: int) -> tuple[int, int]:
        """The (width, height) after this transform (90°/270° swap the axes)."""
        if self.rotation_deg in (90, 270):
            return (height, width)
        return (width, height)

    def to_dict(self) -> dict:
        return {
            "rotation_deg": self.rotation_deg,
            "flip_h": self.flip_h,
            "flip_v": self.flip_v,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DisplayTransform:
        return cls(
            rotation_deg=_normalize_rotation(data.get("rotation_deg", 0)),
            flip_h=bool(data.get("flip_h", False)),
            flip_v=bool(data.get("flip_v", False)),
        )

    @classmethod
    def from_scale_rotation(
        cls, scale_x: float, scale_y: float, rotation_deg: float
    ) -> DisplayTransform:
        """Build from the GUI's display vocabulary (negative scale = flip)."""
        return cls(
            rotation_deg=_normalize_rotation(rotation_deg),
            flip_h=scale_x < 0,
            flip_v=scale_y < 0,
        )


def _normalize_rotation(rotation_deg: float) -> int:
    """Snap a rotation to 0/90/180/270; non-multiples of 90 fall back to 0."""
    deg = round(float(rotation_deg)) % 360
    if deg % 90 != 0:
        log.warning(
            "Display rotation %s° is not a multiple of 90; ignoring it "
            "(only 90° steps can be baked into a video)",
            rotation_deg,
        )
        return 0
    return deg


def from_camera_config(cfg: CameraConfig) -> DisplayTransform:
    """Derive the bakeable transform from a camera's persisted display config.

    A negative ``scale_x`` / ``scale_y`` is a horizontal / vertical flip; the
    magnitude is ignored (the View tab only ever flips, never scales).
    """
    return DisplayTransform.from_scale_rotation(
        cfg.scale_x, cfg.scale_y, cfg.rotation_deg
    )


def apply_display_transform(array: np.ndarray, t: DisplayTransform) -> np.ndarray:
    """Return ``array`` rotated then flipped per ``t`` (a fresh C-contiguous copy).

    The result is passed to the video writer, whose ``_write_all`` casts it to
    raw bytes and therefore needs C-contiguous memory.
    """
    if t.is_identity:
        return array
    # CSS rotate(+deg) is clockwise; np.rot90's positive k is counter-clockwise.
    out = np.rot90(array, k=-(t.rotation_deg // 90))
    if t.flip_h:
        out = np.fliplr(out)
    if t.flip_v:
        out = np.flipud(out)
    return np.ascontiguousarray(out)


def display_vf_filter(t: DisplayTransform) -> str:
    """The ffmpeg ``-vf`` chain equivalent to :func:`apply_display_transform`.

    Empty string when ``t`` is the identity. Rotation filters come first, then
    flips, matching the numpy ordering (rotate, then flip).
    """
    filters: list[str] = []
    # transpose=1 is 90° clockwise, transpose=2 is 90° counter-clockwise.
    if t.rotation_deg == 90:
        filters.append("transpose=1")
    elif t.rotation_deg == 180:
        filters.append("transpose=1")
        filters.append("transpose=1")
    elif t.rotation_deg == 270:
        filters.append("transpose=2")
    if t.flip_h:
        filters.append("hflip")
    if t.flip_v:
        filters.append("vflip")
    return ",".join(filters)
