"""Display transforms (rotation + flips) baked into recorded video, and the
recording folder's on-disk vocabulary.

The GUI shows each camera through the CSS transform ``scale(sx, sy)
rotate(deg)`` (``web/static/js/grid.js``). CSS composes right-to-left, so a
video reproduces it by rotating first (clockwise for a positive angle), then
flipping along the screen axes. Only what the View tab produces is supported:
90° steps, and flips (a negative scale; its magnitude is ignored).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from octacam.config import CameraConfig

log = logging.getLogger("octacam")

RECORDING_SUMMARY_FILENAME = "recording_summary.json"
# Every camera's per-frame series (opt-in: record.save_timestamps).
TIMESTAMPS_FILENAME = "timestamps.npz"
# The rig config with the live settings, beside each camera's parameter file
# (``<serial>.<ext>``): a config directory a session can relaunch from.
CONFIG_SNAPSHOT_FILENAME = "octacam_config.toml"

# Every camera backend's parameter-file suffix (``CameraBackend.extension``):
# Basler .pfs, the GenApi-TSV backends .txt, and the synthetic ``fake``. Listed
# here so the transfer step can carry a snapshot's camera files without importing
# a vendor SDK; tests/test_backends.py keeps it in step with the backends.
PARAM_FILE_EXTENSIONS = ("pfs", "txt", "fake")

# The subfolder holding all of the above, so a recording folder shows just its
# videos. Older recordings keep them flat beside the videos; readers go through
# recording_info_dir(), which answers for either layout.
RECORDING_INFO_DIRNAME = "octacam_recording"


def recording_info_dir(folder: str | Path) -> Path:
    """The directory holding *folder*'s summary, timestamps, config snapshot and
    camera parameter files: the ``octacam_recording`` subfolder, or *folder* for
    an older flat recording.

    The summary decides, and the subfolder's wins over a flat one (an older take
    in the same folder). Without a summary, the subfolder if it exists.
    """
    folder = Path(folder)
    nested = folder / RECORDING_INFO_DIRNAME
    if (nested / RECORDING_SUMMARY_FILENAME).is_file():
        return nested
    if (folder / RECORDING_SUMMARY_FILENAME).is_file():
        return folder
    return nested if nested.is_dir() else folder


def recording_summary_path(folder: str | Path) -> Path:
    """Where the recording in *folder* keeps its summary (either layout)."""
    return recording_info_dir(folder) / RECORDING_SUMMARY_FILENAME


def is_recording_dir(folder: str | Path) -> bool:
    """Whether *folder* has a summary (either layout); an ``octacam_recording``
    subfolder is never a recording folder itself."""
    folder = Path(folder)
    if folder.name == RECORDING_INFO_DIRNAME:
        return False
    return recording_summary_path(folder).is_file()


def recording_folder_of(summary_path: str | Path) -> Path:
    """The recording folder a summary file belongs to: its directory, or that
    directory's parent when the summary sits in the ``octacam_recording``
    subfolder."""
    parent = Path(summary_path).parent
    return parent.parent if parent.name == RECORDING_INFO_DIRNAME else parent


def find_recording_dirs(root: str | Path) -> list[Path]:
    """Every recording folder at or under *root*, in either layout, sorted."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        {recording_folder_of(p) for p in root.rglob(RECORDING_SUMMARY_FILENAME)}
    )


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
