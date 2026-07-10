"""Harvesters + GenTL-producer camera backend (producer-agnostic tier 2).

`Harvesters <https://github.com/genicam/harvesters>`_ is the reference GenICam
GenTL *consumer*: it drives any camera reachable through an installed GenTL
*producer* — a vendor-neutral ``.cti`` transport layer found on
``GENICAM_GENTL64_PATH``. Unlike the vendor SDKs this needs no per-vendor Python
wheel (both ``harvesters`` and ``genicam`` are core deps and install on modern
Python), so it sits above the always-available :mod:`pycameleon` floor and below
the vendor tiers: it adds full node **bounds** and **hardware timestamps** that
pycameleon lacks, for any camera a producer enumerates.

The producer is a user-installed system dependency (like PySpin/the Spinnaker
SDK). This tier is **opt-in only** (``backend = "harvesters"``); the auto cascade
never routes to it (see :mod:`octacam.cameras.registry`). Empirical producer
findings on the FLIR + Basler USB3 test rig (why the default set is what it is):

* **Basler pylon** ``ProducerU3V.cti`` (``/opt/pylon/lib/gentlproducer/gtl``) —
  the recommended producer. Enumerates Basler U3V cameras and, crucially,
  **opens *and* closes cleanly** (no deadlock, no watermark). Two quirks handled
  in code: it does not implement ``Buffer.timestamp_ns`` (we fall back to the raw
  ``timestamp`` tick — see :func:`_buffer_timestamp_ns`), and its native library
  SIGSEGVs during *Python interpreter finalization* (a multi-lib teardown
  interaction, after all work is done — the enumeration subprocess in
  ``octacam doctor`` sidesteps it with ``os._exit``). It is a Basler-focused U3V
  producer: it does **not** enumerate FLIR cameras (drive those with the
  dedicated ``spinnaker``/``flir`` backends).
* **Teledyne Spinnaker** ``Spinnaker_GenTL.cti`` — enumerates FLIR cameras but
  **must not be used**: its ``DevClose`` deadlocks *while holding the Python GIL*,
  wedging the whole process — no bounded thread, ``os._exit``, or signal handler
  can recover (all need the GIL), only an external ``SIGKILL``. Denylisted.
* **Balluff mvIMPACT / Impact Acquire** — previously the default, now removed and
  denylisted: it SIGSEGVs inside ``IFUpdateDeviceList`` during the device scan and
  watermarks frames after an ~8 s evaluation window. Do not reinstall it as the
  octacam producer.
* **Allied Vision Vimba X** — its USB transport layer only enumerates Allied
  Vision's own cameras (third-party support is GigE, not USB3), so it sees none of
  a FLIR/Basler USB3 rig.

When several producers are installed, :func:`_find_cti_files` selects which to
load: set ``OCTACAM_GENTL_PRODUCER`` (``os.pathsep``-separated, case-insensitive
substrings) to pin/prioritise producers (e.g. ``ProducerU3V``); left unset,
every discovered producer is loaded except the known-broken ones in
:data:`_DENY_PRODUCERS`. :meth:`HarvestersBackend.open` binds each camera to the
first (highest-priority) producer that enumerates its serial, so a camera seen by
two producers no longer raises the GenTL "multiple devices found" error.

Mapping notes vs. the other backends:

* Node names are the standard SFNC ones, so :data:`PARAM_NODES` is reused;
  ``remote_device.node_map.<Name>`` yields a typed genicam node whose
  ``.value/.min/.max/.inc/.unit`` and access mode fill :class:`NodeInfo`.
* Parameters persist in the camera's native GenApi feature-persistence TSV
  (``extension = "txt"``; see :mod:`octacam.cameras._genicam_config`), shared with
  the FLIR/Spinnaker/pycameleon backends.
* The GenTL ``System`` is owned by a module-level :class:`Harvester` singleton,
  reset once (after every camera is closed) by :func:`teardown`, which
  :class:`~octacam.cameras.system.CameraSystem` calls through the registry.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from octacam.cameras import _genicam_features
from octacam.cameras._genicam_config import GenICamTriggerConfig
from octacam.cameras._trigger_handoff import SoftwareTriggerHandoff
from octacam.cameras.base import (
    GEOMETRY_FEATURES,
    PARAM_NODES,
    BackendError,
    FeatureInfo,
    Frame,
    NodeInfo,
)
from octacam.cameras.registry import BackendUnavailable

try:  # both are core deps, but keep the import defensive like the FLIR backend.
    import genicam.genapi as genapi
    from harvesters.core import Harvester
except ImportError:  # pragma: no cover - harvesters/genicam ship in core
    genapi = None
    Harvester = None

log = logging.getLogger("octacam")

# Integer SFNC nodes; the rest of PARAM_NODES are floats.
_INT_PARAMS = frozenset({"width", "height", "offset_x", "offset_y"})

# Seconds to wait for a device close before giving up (defends against the
# Spinnaker producer's DevClose deadlock — see the module docstring).
_CLOSE_TIMEOUT_S = 5.0

# Grab pacing (see retrieve). A GenTL producer's buffer wait is a native call
# that holds the Python GIL for its whole duration, so one long blocking
# ``fetch(timeout=0.1)`` freezes every other Python thread — including the
# asyncio web server that pumps the preview WebSocket — for up to ~100 ms per
# frame. Empirically that starves the event loop enough to stall previews and
# drop the WebSocket over a latent link (e.g. an ``ssh -L`` tunnel). Instead we
# poll with a *short* native wait and a ``time.sleep`` between attempts:
# ``time.sleep`` releases the GIL, so the process stays responsive (event-loop
# stalls drop from ~25 ms to ~3 ms) while still capturing every frame.
_FETCH_POLL_S = 0.002  # each native try_fetch waits at most this (GIL held)
_FETCH_SLEEP_S = 0.003  # GIL-released nap between polls

# The GenTL System, held for the whole session and reset once in teardown().
_harvester = None
_harvester_lock = threading.Lock()


def _require():
    """Return the Harvester class, or raise a clean BackendUnavailable."""
    if Harvester is None or genapi is None:
        raise BackendUnavailable(
            "harvesters",
            "the 'harvesters' and 'genicam' packages must be installed",
        )
    return Harvester


def ensure_available() -> None:
    """Raise BackendUnavailable if harvesters/genicam are not importable."""
    _require()


def _discover_cti_files() -> list[str]:
    """All GenTL producer ``.cti`` files on the search path (before selection).

    Scans the standard ``GENICAM_GENTL64_PATH``/``GENICAM_GENTL32_PATH`` search
    dirs plus an optional ``OCTACAM_GENTL_CTI`` override (``os.pathsep``-separated
    ``.cti`` files and/or directories) so a rig can point at a specific producer
    without touching the GenICam env vars. Deduplicated by resolved path. The
    denylist / ``OCTACAM_GENTL_PRODUCER`` selection is applied by
    :func:`_find_cti_files`; this raw list is what ``octacam doctor`` reports.
    """
    dirs: list[str] = []
    for var in ("GENICAM_GENTL64_PATH", "GENICAM_GENTL32_PATH"):
        dirs.extend(p for p in os.environ.get(var, "").split(os.pathsep) if p)

    files: list[str] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            files.append(str(path))

    for entry in os.environ.get("OCTACAM_GENTL_CTI", "").split(os.pathsep):
        if not entry:
            continue
        path = Path(entry)
        if path.is_file() and path.suffix == ".cti":
            _add(path)
        elif path.is_dir():
            dirs.append(str(path))

    for directory in dirs:
        try:
            for cti in sorted(Path(directory).glob("*.cti")):
                _add(cti)
        except OSError:
            continue
    return files


def _find_cti_files() -> list[str]:
    """The GenTL producer ``.cti`` files the harvesters tier will actually load.

    :func:`_discover_cti_files` filtered by the ``OCTACAM_GENTL_PRODUCER``
    allowlist / :data:`_DENY_PRODUCERS` denylist (see :func:`_select_producers`).
    """
    return _select_producers(_discover_cti_files())


# GenTL producers excluded from auto-discovery, matched case-insensitively as a
# substring of the .cti basename. Force one in anyway by naming it in
# OCTACAM_GENTL_PRODUCER. Three kinds are excluded:
#  - "spinnaker_gentl": Teledyne's Spinnaker producer deadlocks in DevClose *while
#    holding the Python GIL*, so no bounded thread / os._exit / signal handler can
#    recover — only an external SIGKILL (see the module docstring).
#  - "mvgentlproducer": Balluff mvIMPACT SIGSEGVs inside IFUpdateDeviceList during
#    the device scan and watermarks frames after an ~8 s eval window. Removed as
#    octacam's producer; denylisted so a reinstall can't silently crash the scan.
#  - "vimbacamerasimulator": Allied Vision's simulator TL always presents phantom
#    virtual cameras, which the cascade would otherwise try to open and record.
_DENY_PRODUCERS = ("spinnaker_gentl", "mvgentlproducer", "vimbacamerasimulator")


def _select_producers(files: list[str]) -> list[str]:
    """Apply the producer-selection policy to discovered ``.cti`` files.

    ``OCTACAM_GENTL_PRODUCER`` (``os.pathsep``-separated, case-insensitive
    substrings) is both an allowlist and a priority order: only producers whose
    basename contains one of the substrings are kept, ordered by which substring
    matched (so ``ProducerU3V:ProducerGEV`` prefers the U3V producer, then GEV).
    With it unset, every discovered producer is kept except those matching
    :data:`_DENY_PRODUCERS`.
    """
    prefs = [
        p.strip().lower()
        for p in os.environ.get("OCTACAM_GENTL_PRODUCER", "").split(os.pathsep)
        if p.strip()
    ]
    if prefs:
        ranked: list[tuple[int, str]] = []
        for f in files:
            name = os.path.basename(f).lower()
            for rank, pref in enumerate(prefs):
                if pref in name:
                    ranked.append((rank, f))
                    break
        ranked.sort(key=lambda t: t[0])  # stable: preserves discovery order per rank
        selected = [f for _rank, f in ranked]
        if not selected and files:
            log.warning(
                "OCTACAM_GENTL_PRODUCER=%r matched none of the discovered GenTL "
                "producers: %s",
                os.environ.get("OCTACAM_GENTL_PRODUCER"),
                ", ".join(files),
            )
        return selected
    return [
        f
        for f in files
        if not any(d in os.path.basename(f).lower() for d in _DENY_PRODUCERS)
    ]


# Why each denylisted producer is not used, keyed by the same case-insensitive
# basename substrings as _DENY_PRODUCERS. Surfaced by `octacam doctor` so an
# installed-but-unusable producer is shown with its reason, not silently hidden.
_PRODUCER_DENY_REASONS = {
    "spinnaker_gentl": (
        "Teledyne Spinnaker GenTL — DevClose deadlocks while holding the Python "
        "GIL, which would wedge the whole process (recoverable only by external "
        "SIGKILL). Drive FLIR cameras with the 'spinnaker' or 'flir' backend."
    ),
    "mvgentlproducer": (
        "Balluff mvIMPACT — SIGSEGVs during the device scan and watermarks frames "
        "after an ~8 s evaluation window. Removed as octacam's producer."
    ),
    "vimbacamerasimulator": (
        "Allied Vision simulator TL — presents phantom virtual cameras; excluded "
        "so the cascade never opens them."
    ),
}

# Well-known GenTL producer install dirs to probe for *reporting* even when they
# are not on GENICAM_GENTL64_PATH — e.g. the Spinnaker SDK installs its .cti
# off-path, so without this `octacam doctor` could not show it as
# detected-but-unusable. Never loaded from here; discovery for load still goes
# through GENICAM_GENTL64_PATH / OCTACAM_GENTL_CTI only.
_KNOWN_PRODUCER_DIRS = ("/opt/spinnaker/lib/spinnaker-gentl",)


def _deny_reason(cti_path: str) -> str | None:
    """The human-readable reason a producer is denylisted, or None if usable."""
    name = os.path.basename(cti_path).lower()
    for key, reason in _PRODUCER_DENY_REASONS.items():
        if key in name:
            return reason
    return None


def report_producers() -> tuple[list[str], list[tuple[str, str]]]:
    """``(will_load, [(cti, reason), ...])`` for ``octacam doctor``.

    ``will_load`` is exactly what the harvesters tier would load (honouring
    ``OCTACAM_GENTL_PRODUCER`` and the denylist). The second list is every other
    discovered-or-known producer that is present but *not* loaded, each with why —
    so an installed producer such as Spinnaker's (denylisted for its close
    deadlock) is visible rather than silently dropped.
    """
    will_load = _find_cti_files()

    def _key(path: str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return path

    loaded_keys = {_key(p) for p in will_load}
    candidates = list(_discover_cti_files())
    for directory in _KNOWN_PRODUCER_DIRS:
        try:
            candidates.extend(str(p) for p in sorted(Path(directory).glob("*.cti")))
        except OSError:
            continue

    disabled: list[tuple[str, str]] = []
    seen: set[str] = set(loaded_keys)
    for cti in candidates:
        key = _key(cti)
        if key in seen:
            continue
        seen.add(key)
        reason = _deny_reason(cti) or (
            "not selected by OCTACAM_GENTL_PRODUCER="
            f"{os.environ.get('OCTACAM_GENTL_PRODUCER')!r}"
        )
        disabled.append((cti, reason))
    return will_load, disabled


def _get_harvester():
    """The lazily-built Harvester singleton with every producer ``.cti`` loaded."""
    global _harvester
    with _harvester_lock:
        if _harvester is None:
            harvester = _require()()
            for cti in _find_cti_files():
                try:
                    harvester.add_file(cti)
                except Exception as e:  # a bad/duplicate producer must not abort
                    log.warning("Could not load GenTL producer %s: %s", cti, e)
            _harvester = harvester
        return _harvester


def _writable_modes():
    """The genicam access modes that count as writable (RW / WO)."""
    gp = genapi
    if gp is None:  # pragma: no cover - guarded by ensure_available()
        raise BackendUnavailable("harvesters", "genicam is not installed")
    return (gp.EAccessMode.RW, gp.EAccessMode.WO)


def _node_attr(node, attr: str):
    """Best-effort read of node.min/.max/.inc/.unit; None when unsupported.

    Enum/command nodes lack these and raise rather than return, so a missing
    attribute is expected, not an error (mirrors the Basler backend)."""
    try:
        return getattr(node, attr)
    except Exception:
        return None


def _buffer_timestamp_ns(buffer) -> int:
    """Best-effort hardware timestamp in ns; 0 when the producer lacks it.

    ``Buffer.timestamp_ns`` needs the producer to implement the GenTL
    timestamp-frequency query. Some producers (e.g. Basler's pylon
    ``ProducerU3V``) do not and *raise* ``NotImplementedException`` rather than
    return — which ``getattr(..., 0)`` does not catch (it only defaults on
    ``AttributeError``), so an unguarded read would cost us the whole frame. Fall
    back to the raw ``timestamp`` device tick (nanoseconds on Basler/FLIR U3V),
    then 0, so a producer without ``timestamp_ns`` still yields images."""
    for attr in ("timestamp_ns", "timestamp"):
        try:
            value = getattr(buffer, attr)
        except Exception:
            continue
        if value:
            return int(value)
    return 0


def _device_info_for(harvester, serial: str):
    """First enumerated device_info matching ``serial``, in producer-load order.

    Load order reflects the ``OCTACAM_GENTL_PRODUCER`` priority (see
    :func:`_select_producers`), so when two producers enumerate the same camera the
    higher-priority one wins — and passing that specific device_info to ``create``
    avoids the GenTL "multiple devices found" error a bare serial dict raises.
    Returns None if the serial is not enumerated (caller falls back to a dict)."""
    for info in getattr(harvester, "device_info_list", None) or []:
        if str(getattr(info, "serial_number", "") or "") == serial:
            return info
    return None


class HarvestersBackend(GenICamTriggerConfig, SoftwareTriggerHandoff):
    """A single camera driven through Harvesters over a GenTL producer."""

    extension = "txt"

    def __init__(self, serial: str):
        self._serial = serial
        # The ImageAcquirer, created in open() and dropped in close(); None means
        # closed, which the is_open/is_grabbing guards tolerate. Typed Any so the
        # created-in-open lifecycle (and the untyped GenTL buffer payloads) do not
        # trip the checker on every access.
        self._ia: Any = None
        # DeviceVendorName, read once at open(). Harvesters serves any vendor
        # through a GenTL producer, so the ROI offsets' grab-lock policy is
        # decided per device (see grab_locked_features).
        self._vendor: str | None = None
        self._original_trigger_source: str | None = None
        # Software-trigger hand-off (shared mixin): trigger_once bumps a counter;
        # the device TriggerSoftware.execute moves into retrieve() on the grab
        # thread so the shared trigger timer never blocks on this camera.
        self._init_trigger_handoff()

    @property
    def serial_number(self) -> str:
        return self._serial

    # ------------------------------------------------------------- lifecycle

    def open(self) -> None:
        harvester = _get_harvester()
        info = _device_info_for(harvester, self._serial)
        try:
            self._ia = harvester.create(
                info if info is not None else {"serial_number": self._serial}
            )
        except Exception as e:
            # Usually the device is already in use by another process. Surface it
            # as a BackendError so the caller reports it cleanly.
            raise BackendError(str(e)) from e
        # Force Mono8 so the payload is a 2-D uint8 image matching the writer.
        try:
            self._set_enum("PixelFormat", "Mono8")
        except BackendError as e:
            log.warning("Could not set Mono8 on camera %s: %s", self._serial, e)
        self._vendor = self._read_vendor_name()

    def close(self) -> None:
        ia = self._ia
        if ia is None:  # idempotent: a second close() must not raise
            return
        self._ia = None
        self._grabbing = False

        def _teardown() -> None:
            try:
                if ia.is_acquiring():
                    ia.stop()
            except Exception:
                pass
            try:
                ia.destroy()
            except Exception:
                pass

        # Bounded close: a well-behaved producer (Basler's pylon ProducerU3V) tears
        # down immediately, so the join returns at once. This bound only protects
        # against a merely *slow* close — it does NOT rescue a truly deadlocking
        # producer: Spinnaker's DevClose holds the GIL, so the daemon can't release
        # it, the join below can never re-acquire it, and the whole process wedges
        # regardless. That is why such producers are excluded in _find_cti_files
        # rather than tolerated here.
        thread = threading.Thread(
            target=_teardown, name=f"harvesters-close-{self._serial}", daemon=True
        )
        thread.start()
        thread.join(timeout=_CLOSE_TIMEOUT_S)
        if thread.is_alive():
            log.warning(
                "Camera %s did not close within %.0fs — the GenTL producer is slow "
                "to tear down; leaving the handle to a daemon thread. Prefer the "
                "pylon ProducerU3V producer.",
                self._serial,
                _CLOSE_TIMEOUT_S,
            )

    def is_open(self) -> bool:
        return self._ia is not None

    def is_grabbing(self) -> bool:
        # The hand-off flag is authoritative (see BaslerBackend.is_grabbing):
        # stop_grab flips it and wakes a blocked retrieve before the native
        # ia.stop(), so the grab loop must agree from that instant.
        return self._ia is not None and self._grabbing

    def _read_vendor_name(self) -> str | None:
        try:
            return str(self._nodemap().DeviceVendorName.value)
        except Exception:  # optional node / read failure — treat as unknown
            return None

    def grab_locked_features(self) -> frozenset[str]:
        # Width/Height are locked mid-acquisition on every GenICam camera. Basler
        # additionally locks the ROI offsets while grabbing (unlike FLIR/Teledyne,
        # which keep them live-writable), so decide per device from the vendor
        # name; an unknown vendor keeps the live-offset default.
        if self._vendor and "basler" in self._vendor.lower():
            return GEOMETRY_FEATURES | {"OffsetX", "OffsetY"}
        return GEOMETRY_FEATURES

    def width(self) -> int:
        return int(self._nodemap().Width.value)

    def height(self) -> int:
        return int(self._nodemap().Height.value)

    # ----------------------------------------------------- node plumbing

    def _nodemap(self):
        return self._ia.remote_device.node_map

    def _node(self, name: str):
        return getattr(self._nodemap(), PARAM_NODES[name])

    def _set_enum(self, name: str, value: str) -> None:
        try:
            getattr(self._nodemap(), name).value = value
        except Exception as e:
            raise BackendError(str(e)) from e

    def _get_enum(self, name: str) -> str | None:
        try:
            return getattr(self._nodemap(), name).value
        except Exception:
            return None

    # Typed-setter seam used by the native-TSV config applier (_genicam_config).
    # genicam nodes are duck-typed: assigning/reading ``.value`` works for
    # boolean, integer and float nodes alike.
    def _set_bool(self, name: str, value: bool) -> None:
        try:
            getattr(self._nodemap(), name).value = bool(value)
        except Exception as e:
            raise BackendError(str(e)) from e

    def _get_bool(self, name: str) -> bool | None:
        try:
            return bool(getattr(self._nodemap(), name).value)
        except Exception:
            return None

    def _set_number(self, name: str, value: float, is_int: bool) -> None:
        try:
            getattr(self._nodemap(), name).value = (
                int(value) if is_int else float(value)
            )
        except Exception as e:
            raise BackendError(str(e)) from e

    def _get_number(self, name: str, is_int: bool) -> float | int | None:
        try:
            value = getattr(self._nodemap(), name).value
        except Exception:
            return None
        return int(value) if is_int else float(value)

    # ----------------------------------------------------- sensor parameters

    def read_node(self, name: str) -> NodeInfo:
        node = self._node(name)
        try:
            value = node.value
        except Exception as e:
            raise BackendError(str(e)) from e
        try:
            writable = node.get_access_mode() in _writable_modes()
        except Exception:
            writable = False
        return NodeInfo(
            value=value,
            min=_node_attr(node, "min"),
            max=_node_attr(node, "max"),
            inc=_node_attr(node, "inc"),
            unit=_node_attr(node, "unit"),
            writable=writable,
        )

    def write_node(self, name: str, value: float) -> None:
        node = self._node(name)
        try:
            node.value = int(value) if name in _INT_PARAMS else float(value)
        except Exception as e:
            raise BackendError(str(e)) from e

    # ---------------------------------------------------- full device node map

    def list_features(self) -> list[FeatureInfo]:
        if self._ia is None:
            return []
        return _genicam_features.walk_features(genapi, self._nodemap())

    def read_feature(self, name: str) -> FeatureInfo:
        return _genicam_features.read_feature(genapi, self._nodemap(), name)

    def write_feature(self, name: str, value: object) -> None:
        _genicam_features.write_feature(genapi, self._nodemap(), name, value)

    def execute_command(self, name: str) -> None:
        _genicam_features.execute_command(genapi, self._nodemap(), name)

    # config_values / load_params / save_params and the software-trigger chain
    # (enable_frame_trigger / set_trigger_source / begin_software_trigger_preview
    # / trigger_once / begin_freerun / _enable_trigger_overlap) are inherited
    # unchanged from GenICamTriggerConfig.

    def retrieve_freerun(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        # Free-run: the camera pushes frames continuously, so fetch the next one
        # without waiting on / firing a software trigger.
        ia = self._ia
        if ia is None or not self._grabbing:
            return None
        return self._fetch_frame(ia, timeout_ms, wants_array)

    # ------------------------------------------------------------- grabbing

    def _start(self) -> None:
        try:
            self._ia.start()
        except Exception as e:
            log.error("Failed to start streaming on camera %s: %s", self._serial, e)
            raise BackendError(str(e)) from e
        self._begin_grab()

    def start_grab_preview(self) -> None:
        self._start()

    def start_grab_record(self) -> bool:
        self._start()
        return True

    def stop_grab(self) -> None:
        # Flip the hand-off flag and wake any blocked retrieve BEFORE the native
        # stop, so the grab loop sees "not grabbing" immediately.
        self._end_grab()
        if self._ia is None:
            return
        try:
            if self._ia.is_acquiring():
                self._ia.stop()
        except Exception:
            pass

    def retrieve(
        self, timeout_ms: int, wants_array: Callable[[], bool]
    ) -> Frame | None:
        # Wait for a pending software trigger, then fire exactly one device
        # trigger and fetch exactly one frame on this camera's own grab thread.
        if not self._wait_pending(timeout_ms):
            return None
        ia = self._ia
        if ia is None or not self._grabbing:
            return None
        # Fire the device trigger here (not on the caught _trigger_all path). The
        # grab loop does not wrap retrieve() in try/except, so a stop-race or a
        # trigger failure must return None, never raise — a lost trigger is one
        # lost frame, the same as the try_fetch timeout below.
        try:
            self._nodemap().TriggerSoftware.execute()
        except Exception:
            return None
        return self._fetch_frame(ia, timeout_ms, wants_array)

    def _fetch_frame(self, ia, timeout_ms: int, wants_array) -> Frame | None:
        # Poll for a buffer with short native waits and GIL-releasing sleeps
        # (see _FETCH_POLL_S) rather than one long blocking fetch, so the rest
        # of the process (notably the asyncio preview server) is not frozen
        # while this camera waits for its next frame. Never raises: a transient
        # producer error or bad payload is one lost frame (as the grab loop
        # expects), the same as the fetch timeout.
        deadline = time.monotonic() + timeout_ms / 1000.0
        buffer = None
        while True:
            try:
                buffer = ia.try_fetch(timeout=_FETCH_POLL_S)
            except Exception:
                return None  # transient producer error
            if buffer is not None:
                break
            if time.monotonic() >= deadline:
                return None  # no frame within the caller's window
            time.sleep(_FETCH_SLEEP_S)
        try:
            timestamp = _buffer_timestamp_ns(buffer)
            array = None
            if wants_array():
                component = buffer.payload.components[0]
                array = component.data.reshape(
                    component.height, component.width
                ).copy()  # own it; the buffer is re-queued below
            return (array, timestamp)
        except Exception as e:
            log.warning("Camera %s: bad frame payload (%s); skipping", self._serial, e)
            return None
        finally:
            # Return the buffer to the producer's pool for reuse.
            try:
                buffer.queue()
            except Exception:
                pass


def enumerate_harvesters(requested_serials: list[str] | None = None):
    """Return ``[(serial, serial), ...]`` for the requested cameras.

    Empty when no GenTL producer is installed (no ``.cti`` on the search path) or
    none enumerate a device — that is how this tier self-disables and the cascade
    falls through to pycameleon. Mirrors the Basler/FLIR enumeration otherwise:
    all detected serials (sorted) when nothing is requested, else the listed
    serials in order with a warning for any not connected. The handle is the
    serial (the backend re-creates the ImageAcquirer by serial in ``open()``).
    """
    harvester = _get_harvester()
    try:
        harvester.update()
    except Exception as e:
        log.warning("GenTL device update failed: %s", e)
        return []
    infos = harvester.device_info_list or []
    if not infos:
        return []

    by_serial: dict[str, object] = {}
    detected: list[str] = []
    for info in infos:
        serial = str(getattr(info, "serial_number", "") or "")
        if not serial or serial in by_serial:
            continue  # a camera seen by two producers enumerates once, not twice
        detected.append(serial)
        by_serial[serial] = info
    if not detected:
        return []
    # Debug, not info: the auto cascade enumerates every tier, so CameraSystem
    # logs the single attributed "Detected N" summary (see basler backend).
    log.debug("harvesters enumerated %d camera(s)", len(detected))

    final = sorted(detected) if not requested_serials else list(requested_serials)
    out = []
    for serial in final:
        if serial not in by_serial:
            log.warning("Camera with serial number %s not found", serial)
            continue
        out.append((serial, serial))
    return out


def teardown() -> None:
    """Reset the Harvester singleton once, after every camera has been closed.

    Called by CameraSystem.close() through the registry (like the FLIR System
    release). Resetting drops the loaded producers and the GenTL system so a
    later run starts clean."""
    global _harvester
    with _harvester_lock:
        if _harvester is not None:
            try:
                _harvester.reset()
            except Exception:
                pass
            _harvester = None
