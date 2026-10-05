"""Camera backend selection and the ``auto`` cascade.

Each backend module declares a module-level :data:`SPEC` (a :class:`BackendSpec`)
and is imported only when selected; a missing SDK raises
:class:`BackendUnavailable`, not a raw ``ImportError``.
"""

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from octacam.cameras.base import CameraBackend

# Backend name -> (module, why it cannot be imported).
_MODULES = {
    "basler": ("octacam.cameras.basler", "the 'pypylon' package is not installed"),
    "flir": (
        "octacam.cameras.flir",
        "the Spinnaker SDK and its PySpin wheel must be installed "
        "(they are not on PyPI; see the README)",
    ),
    "spinnaker": (
        "octacam.cameras.spinnaker_c",
        "the Spinnaker SDK (libSpinnaker_C.so) is not installed",
    ),
    "pycameleon": (
        "octacam.cameras.pycameleon",
        "the 'pycameleon' package is not installed",
    ),
    "fake": ("octacam.cameras.fake", "the fake backend module failed to import"),
}
BACKENDS = tuple(_MODULES)

# "auto": each camera is claimed by the first tier here that enumerates its
# serial (CameraSystem._enumerate). Vendor SDKs first; ``spinnaker`` (the
# Spinnaker C API over ctypes) claims the FLIRs when PySpin is missing;
# ``pycameleon`` (libusb, a core dependency) is the always-present floor.
CASCADE = ("basler", "flir", "spinnaker", "pycameleon")


class BackendUnavailable(RuntimeError):
    """A requested camera backend is unknown or its SDK is not installed."""

    def __init__(self, backend: str, detail: str = ""):
        self.backend = backend
        message = f"Camera backend {backend!r} is unavailable"
        if detail:
            message += f": {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class BackendSpec:
    """What the registry reaches in one backend module.

    ``enumerate(requested_serials)`` returns ``[(serial, handle), ...]`` in
    :func:`select_serials` order, silently: a tier also sees serials another one
    owns, so only CameraSystem reports one nobody found. A None handle is a camera
    present but unusable, claimed without being opened. ``factory`` builds the
    backend from a handle.
    """

    enumerate: Callable[..., list[tuple[str, Any]]]
    factory: Callable[[Any], "CameraBackend"]
    # Raises BackendUnavailable when the module imports but its SDK is missing.
    ensure_available: Callable[[], None] | None = None
    # The model name from a handle without opening the camera (for doctor).
    read_model: Callable[[Any], str | None] | None = None
    # Releases session-wide SDK state once every camera is closed.
    teardown: Callable[[], None] | None = None


def select_serials(detected, requested: list[str] | None) -> list[str]:
    """The serials an enumeration hands out, each once: the requested ones it
    detected, in requested order, else every detected one, sorted."""
    found = set(detected)
    if not requested:
        return sorted(found)
    return [serial for serial in dict.fromkeys(requested) if serial in found]


def select_backend(name: str) -> BackendSpec:
    """The :class:`BackendSpec` of ``name`` (case-insensitive; empty is basler),
    its SDK imported."""
    key = (name or "basler").strip().lower()
    if key not in _MODULES:
        raise BackendUnavailable(name, f"unknown backend (expected one of {BACKENDS})")
    module_name, missing = _MODULES[key]
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise BackendUnavailable(key, missing) from e
    spec: BackendSpec = module.SPEC
    if spec.ensure_available is not None:
        spec.ensure_available()
    return spec


def available_backends() -> list[str]:
    """The :data:`CASCADE` tiers whose SDK imports here, in priority order."""
    out: list[str] = []
    for name in CASCADE:
        try:
            select_backend(name)
        except BackendUnavailable:
            continue
        out.append(name)
    return out


def is_auto(name: str | None) -> bool:
    """Whether a backend selector means the cascade: ``auto``, ``all`` or empty."""
    return (name or "").strip().lower() in ("auto", "all", "")


def resolve_backend_names(name: str | None) -> list[str]:
    """The cascade's :func:`available_backends` for :func:`is_auto`, else
    ``[name]``."""
    if is_auto(name):
        return available_backends()
    return [(name or "").strip().lower()]
