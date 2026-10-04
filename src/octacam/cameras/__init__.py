"""Multi-vendor camera layer.

:class:`~octacam.cameras.base.Camera` and :class:`CameraSystem` are SDK-neutral;
each vendor module (``basler``, ``flir``, ``spinnaker_c``, ``pycameleon``,
``fake``) implements the backend seam and is imported lazily through ``registry``.
"""

from octacam.cameras.base import BackendError
from octacam.cameras.registry import BackendUnavailable, select_backend
from octacam.cameras.system import CameraSystem

__all__ = [
    "BackendError",
    "BackendUnavailable",
    "CameraSystem",
    "select_backend",
]
