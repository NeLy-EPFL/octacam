"""The camera layer: SDK-neutral ``Camera`` and ``CameraSystem``, and one
backend module per SDK, imported on selection through ``registry``."""

from octacam.cameras.base import BackendError
from octacam.cameras.registry import BackendUnavailable, select_backend
from octacam.cameras.system import CameraSystem

__all__ = [
    "BackendError",
    "BackendUnavailable",
    "CameraSystem",
    "select_backend",
]
