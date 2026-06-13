"""Backwards-compatible shim.

The camera layer moved to the :mod:`octacam.cameras` package (one module per
vendor behind a shared core). This module re-exports the public surface so
existing ``from octacam.camera import ...`` imports keep working.
"""

from octacam.cameras import (
    GEOMETRY_PARAMS,
    LIVE_PARAMS,
    PARAM_NODES,
    Camera,
    CameraSystem,
)
from octacam.cameras.basler import _normalize_pfs_triggers

__all__ = [
    "GEOMETRY_PARAMS",
    "LIVE_PARAMS",
    "PARAM_NODES",
    "Camera",
    "CameraSystem",
    "_normalize_pfs_triggers",
]
