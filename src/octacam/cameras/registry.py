"""Camera backend selection.

Maps a backend name (from the rig config's ``backend`` field) to the trio the
:class:`~octacam.cameras.system.CameraSystem` needs: an enumeration function, a
per-device backend factory, and the config-file extension that backend persists
parameters as. Backend modules are imported lazily so a backend whose SDK is not
installed (e.g. FLIR/PySpin on a Basler-only box) costs nothing until selected,
and raising :class:`BackendUnavailable` keeps a missing SDK from surfacing as a
raw ``ImportError`` traceback.
"""

import importlib
from collections.abc import Callable

BACKENDS = ("basler", "flir", "fake")


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

    ``enumerate_fn(requested_serials)`` returns ``[(serial, handle), ...]``;
    ``backend_factory(handle)`` builds the per-camera backend; ``extension`` is
    the parameter-file suffix (without the dot). Raises
    :class:`BackendUnavailable` for an unknown backend or a missing SDK.
    """
    key = (name or "basler").strip().lower()
    if key == "basler":
        basler = importlib.import_module("octacam.cameras.basler")
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
    raise BackendUnavailable(name, f"unknown backend (expected one of {BACKENDS})")


def teardown_backend(name: str) -> None:
    """Release any session-wide SDK resources held by ``name`` (once).

    Only the FLIR backend needs this (the Spinnaker ``System`` singleton must be
    released after every camera is closed); for every other backend it is a
    no-op. Called by :meth:`CameraSystem.close`.
    """
    key = (name or "basler").strip().lower()
    if key != "flir":
        return
    try:
        flir = importlib.import_module("octacam.cameras.flir")
    except ImportError:  # pragma: no cover - nothing to release if it never loaded
        return
    flir.teardown()
