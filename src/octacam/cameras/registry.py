"""Camera backend selection and the auto-detect cascade.

Maps a backend name (from the rig config's ``backend`` field) to the trio the
:class:`~octacam.cameras.system.CameraSystem` needs: an enumeration function, a
per-device backend factory, and the config-file extension that backend persists
parameters as. Backend modules are imported lazily so a backend whose SDK is not
installed (e.g. FLIR/PySpin on a box without the Spinnaker SDK) costs nothing
until selected, and raising :class:`BackendUnavailable` keeps a missing SDK from
surfacing as a raw ``ImportError`` traceback.

``"auto"`` resolves to the :data:`CASCADE`: a per-camera preference order where
each camera is claimed by the highest-priority tier that enumerates its serial.
The tiers, best-to-floor:

1. **Vendor SDK** — ``basler`` (pypylon) / ``flir`` (Spinnaker + PySpin). Best
   features/perf; the SDK is user-installed and not always available on modern
   Python (PySpin is cp310-only), so the tier simply drops out of the cascade
   when its import fails.
2. **spinnaker** — the Spinnaker SDK's C API (``libSpinnaker_C.so``) driven via
   ``ctypes``. Same FLIR cameras as ``flir`` but with no cp310 wheel limit, so it
   claims the FLIRs on modern Python where ``flir`` (PySpin) drops out. Faster
   and watermark-free vs the pycameleon floor because ctypes releases the GIL on
   the blocking grab. Self-disables when the SDK is not installed.
3. **pycameleon** — libusb-only, a core dependency, so it is always present and
   the guaranteed final fallback.

The ``harvesters`` GenTL tier is deliberately **NOT** in the auto cascade —
every GenTL producer depends on a user-installed, vendor-EULA'd ``.cti`` with its
own quirks, so a camera lacking a vendor SDK must never be silently routed through
one. The validated producer is now Basler's pylon ``ProducerU3V`` (opens/closes
cleanly, watermark-free, but enumerates only Basler U3V cameras); the previously
used Balluff mvIMPACT producer was removed because it watermarks third-party
frames after an ~8 s eval window and SIGSEGVs during the device scan, and
Teledyne's Spinnaker GenTL producer is unusable (its close deadlocks holding the
GIL). See :mod:`octacam.cameras.harvesters` for the full producer notes.
harvesters remains fully supported but only when a rig **explicitly** names
``backend = "harvesters"`` and opts in. Everything the cascade would have sent to
harvesters lands on the always-free pycameleon floor instead.
"""

import importlib
from collections.abc import Callable

BACKENDS = ("basler", "flir", "spinnaker", "harvesters", "pycameleon", "fake")

# The per-camera preference order for "auto": a camera is claimed by the highest
# tier here that enumerates its serial (see CameraSystem._enumerate). Vendor SDKs
# (basler/flir) rank above the always-available pycameleon floor; ``spinnaker``
# (the Spinnaker C API via ctypes) sits at the FLIR-vendor position just below
# ``flir`` so it claims the FLIRs on modern Python where PySpin is unavailable,
# before they fall through to the pycameleon floor. ``harvesters`` is
# intentionally absent (see the module docstring): every GenTL producer is a
# user-installed, vendor-EULA'd .cti with its own quirks, so it must never be
# picked automatically — a rig opts into it by name.
CASCADE = ("basler", "flir", "spinnaker", "pycameleon")

# The real (non-``fake``) backends an auto-detecting rig sweeps, in cascade
# priority order. ``fake`` is synthetic (it always reports FAKE-* serials
# regardless of hardware), so it is never swept — only used when named.
REAL_BACKENDS = CASCADE


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
    if key == "harvesters":
        try:
            harvesters = importlib.import_module("octacam.cameras.harvesters")
        except ImportError as e:
            raise BackendUnavailable(
                "harvesters",
                "the 'harvesters' and 'genicam' packages must be installed",
            ) from e
        harvesters.ensure_available()
        return (
            harvesters.enumerate_harvesters,
            harvesters.HarvestersBackend,
            harvesters.HarvestersBackend.extension,
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
    """The :data:`CASCADE` tiers whose SDK/deps import here, in priority order.

    This is what ``"auto"`` sweeps: an unavailable tier (a missing vendor SDK, or
    no GenTL producer's Python deps) is skipped. ``pycameleon`` is a core
    dependency, so the result is never empty on a normal install.
    """
    out: list[str] = []
    for name in CASCADE:
        try:
            select_backend(name)
        except BackendUnavailable:
            continue
        out.append(name)
    return out


def resolve_backend_names(name: str | None) -> list[str]:
    """Expand a backend selector into the concrete backend names to try.

    ``"auto"`` (or an empty/absent selector, and its alias ``"all"``) means "the
    available cascade" — :func:`available_backends`, in priority order — so a rig
    picks the best backend per camera from whatever is installed. Any other value
    is treated as a single explicit backend (including ``"fake"`` and unknown
    names, which :func:`select_backend` then accepts or rejects).
    """
    key = (name or "auto").strip().lower()
    if key in ("auto", "all", ""):
        return available_backends()
    return [key]


# Backends that hold session-wide SDK resources needing an explicit release after
# every camera is closed, mapped to their module (each exposes ``teardown()``).
_TEARDOWN_MODULES = {
    "flir": "octacam.cameras.flir",
    "spinnaker": "octacam.cameras.spinnaker_c",
    "harvesters": "octacam.cameras.harvesters",
}


def teardown_backend(name: str) -> None:
    """Release any session-wide SDK resources held by ``name`` (once).

    FLIR/spinnaker (the Spinnaker ``System`` singleton, held by PySpin or the C
    API respectively) and harvesters (the GenTL ``Harvester`` singleton) must be
    released after every camera is closed; for every other backend this is a
    no-op. Called by :meth:`CameraSystem.close`.
    """
    key = (name or "basler").strip().lower()
    module = _TEARDOWN_MODULES.get(key)
    if module is None:
        return
    try:
        mod = importlib.import_module(module)
    except ImportError:  # pragma: no cover - nothing to release if it never loaded
        return
    mod.teardown()
