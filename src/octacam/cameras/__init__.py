"""Multi-vendor camera layer.

The concrete, SDK-neutral :class:`Camera` / :class:`CameraSystem` live here; each
vendor implements the thin :class:`CameraBackend` seam in its own module
(``basler``, ``flir``, ``harvesters``, ``pycameleon``, ``fake``), imported lazily
through ``registry``.
"""

from octacam.cameras.base import (
    GEOMETRY_PARAMS,
    LIVE_PARAMS,
    PARAM_NODES,
    BackendError,
    Camera,
    CameraBackend,
    LatestFrame,
    NodeInfo,
    snap_value,
)
from octacam.cameras.registry import (
    BackendUnavailable,
    available_backends,
    select_backend,
)
from octacam.cameras.system import CameraSystem

__all__ = [
    "GEOMETRY_PARAMS",
    "LIVE_PARAMS",
    "PARAM_NODES",
    "BackendError",
    "BackendUnavailable",
    "Camera",
    "CameraBackend",
    "CameraSystem",
    "LatestFrame",
    "NodeInfo",
    "available_backends",
    "select_backend",
    "snap_value",
]
