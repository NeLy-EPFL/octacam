"""Display transforms (rotation + flips) baked into recorded video.

The GUI shows each camera through the CSS transform ``scale(sx, sy)
rotate(deg)`` (``web/static/js/grid.js``). CSS composes right-to-left, so a
video reproduces it by rotating first (clockwise for a positive angle), then
flipping along the screen axes. Only what the View tab produces is supported:
90° steps, and flips (a negative scale; its magnitude is ignored).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from octacam.config import CameraConfig

log = logging.getLogger("octacam")


@dataclass(frozen=True)
class DisplayTransform:
    """A bakeable display orientation: a clockwise 0/90/180/270° rotation, then
    flips along the screen axes."""

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
    """The bakeable transform of a camera's configured display."""
    return DisplayTransform.from_scale_rotation(
        cfg.scale_x, cfg.scale_y, cfg.rotation_deg
    )


def apply_display_transform(array: np.ndarray, t: DisplayTransform) -> np.ndarray:
    """``array`` rotated then flipped per ``t``, C-contiguous (the writer casts
    it to raw bytes)."""
    if t.is_identity:
        return array
    # CSS rotate(+deg) is clockwise; np.rot90's positive k is counter-clockwise.
    out = np.rot90(array, k=-(t.rotation_deg // 90))
    if t.flip_h:
        out = np.fliplr(out)
    if t.flip_v:
        out = np.flipud(out)
    return np.ascontiguousarray(out)
