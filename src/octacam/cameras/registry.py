"""Camera backend selection and the ``auto`` cascade.

A backend name resolves to its enumeration function, backend class and
parameter-file extension. Each backend module is imported only when selected,
and a missing SDK raises :class:`BackendUnavailable`, not a raw ``ImportError``.
"""

import importlib
from collections.abc import Callable

BACKENDS = ("basler", "flir", "spinnaker", "pycameleon", "fake")

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


def select_backend(name: str) -> tuple[Callable, Callable, str]:
    """Resolve ``name`` to ``(enumerate_fn, backend_factory, extension)``.

    ``enumerate_fn(requested_serials, *, warn_missing=True)`` returns
    ``[(serial, handle), ...]``; ``extension`` has no dot. Every backend must
    accept ``warn_missing``: the cascade offers each tier the rig's serials with
    ``warn_missing=False``, since a tier also sees serials another one owns.
    """
    key = (name or "basler").strip().lower()
    if key == "basler":
        try:
            basler = importlib.import_module("octacam.cameras.basler")
        except ImportError as e:
            raise BackendUnavailable(
                "basler", "the 'pypylon' package is not installed"
            ) from e
        return (
            basler.enumerate_basler,
            basler.BaslerBackend,
            basler.BaslerBackend.extension,
        )
    if key == "fake":
        try:
            fake = importlib.import_module("octacam.cameras.fake")
        except ImportError as e:  # pragma: no cover - fake has no hard deps
            raise BackendUnavailable("fake", str(e)) from e
        return fake.enumerate_fake, fake.FakeBackend, fake.FakeBackend.extension
    if key == "flir":
        try:
            flir = importlib.import_module("octacam.cameras.flir")
        except ImportError as e:
            raise BackendUnavailable(
                "flir",
                "the Spinnaker SDK and its PySpin wheel must be installed "
                "(they are not on PyPI; see the README)",
            ) from e
        flir.ensure_available()
        return flir.enumerate_flir, flir.FlirBackend, flir.FlirBackend.extension
    if key == "spinnaker":
        try:
            spinnaker = importlib.import_module("octacam.cameras.spinnaker_c")
        except ImportError as e:  # pragma: no cover - module has no import-time deps
            raise BackendUnavailable(
                "spinnaker",
                "the Spinnaker SDK (libSpinnaker_C.so) is not installed",
            ) from e
        spinnaker.ensure_available()
        return (
            spinnaker.enumerate_spinnaker,
            spinnaker.SpinnakerBackend,
            spinnaker.SpinnakerBackend.extension,
        )
    if key == "pycameleon":
        try:
            pycameleon = importlib.import_module("octacam.cameras.pycameleon")
        except ImportError as e:
            raise BackendUnavailable(
                "pycameleon", "the 'pycameleon' package is not installed"
            ) from e
        pycameleon.ensure_available()
        return (
            pycameleon.enumerate_pycameleon,
            pycameleon.PycameleonBackend,
            pycameleon.PycameleonBackend.extension,
        )
    raise BackendUnavailable(name, f"unknown backend (expected one of {BACKENDS})")


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


def resolve_backend_names(name: str | None) -> list[str]:
    """``auto``, ``all`` or empty: :func:`available_backends`; else ``[name]``."""
    key = (name or "auto").strip().lower()
    if key in ("auto", "all", ""):
        return available_backends()
    return [key]


# Backends holding the Spinnaker System singleton, released by each module's
# teardown() once every camera is closed.
_TEARDOWN_MODULES = {
    "flir": "octacam.cameras.flir",
    "spinnaker": "octacam.cameras.spinnaker_c",
}


def teardown_backend(name: str) -> None:
    """Release ``name``'s session-wide SDK resources (a no-op for most)."""
    key = (name or "basler").strip().lower()
    module = _TEARDOWN_MODULES.get(key)
    if module is None:
        return
    try:
        mod = importlib.import_module(module)
    except ImportError:  # pragma: no cover - nothing to release if it never loaded
        return
    mod.teardown()
