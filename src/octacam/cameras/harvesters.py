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
SDK). For third-party USB3 Vision cameras (e.g. FLIR, Basler) the
empirically-verified producer is Balluff **mvIMPACT / Impact Acquire**, which
enumerates and cleanly closes any U3V camera (~0.3 s). Allied Vision **Vimba X**
is *not* suitable for such rigs: its bundled USB transport layer only enumerates
Allied Vision's own USB cameras (its third-party support is GigE Vision, not
USB3), so it sees none of a FLIR/Basler USB3 rig. **Teledyne's Spinnaker producer
must not be used**: its ``DevClose`` deadlocks *while holding the Python GIL*,
which wedges the whole process — no bounded thread, ``os._exit``, or signal
handler can recover (all need the GIL), only an external ``SIGKILL``. It is
therefore excluded from auto-discovery (see :data:`_DENY_PRODUCERS`).

When several producers are installed, :func:`_find_cti_files` selects which to
load: set ``OCTACAM_GENTL_PRODUCER`` (``os.pathsep``-separated, case-insensitive
substrings) to pin/prioritise producers (e.g. ``mvGenTLProducer``); left unset,
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
from pathlib import Path
from typing import Any

from octacam.cameras import _genicam_features
from octacam.cameras._genicam_config import (
    apply_config,
    apply_freerun_rate_cap,
    clear_freerun_rate_cap,
    dump_config,
    normalize_trigger_source,
    parse_config,
)
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


def _find_cti_files() -> list[str]:
    """Locate GenTL producer ``.cti`` files to load.

    Scans the standard ``GENICAM_GENTL64_PATH``/``GENICAM_GENTL32_PATH`` search
    dirs plus an optional ``OCTACAM_GENTL_CTI`` override (``os.pathsep``-separated
    ``.cti`` files and/or directories) so a rig can point at a specific producer
    without touching the GenICam env vars. Deduplicated by resolved path.
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
    return _select_producers(files)


# GenTL producers excluded from auto-discovery, matched case-insensitively as a
# substring of the .cti basename. Force one in anyway by naming it in
# OCTACAM_GENTL_PRODUCER. Two kinds are excluded:
#  - "spinnaker_gentl": Teledyne's Spinnaker producer deadlocks in DevClose *while
#    holding the Python GIL*, so no bounded thread / os._exit / signal handler can
#    recover — only an external SIGKILL (see the module docstring).
#  - "vimbacamerasimulator": Allied Vision's simulator TL always presents phantom
#    virtual cameras, which the cascade would otherwise try to open and record.
_DENY_PRODUCERS = ("spinnaker_gentl", "vimbacamerasimulator")


def _select_producers(files: list[str]) -> list[str]:
    """Apply the producer-selection policy to discovered ``.cti`` files.

    ``OCTACAM_GENTL_PRODUCER`` (``os.pathsep``-separated, case-insensitive
    substrings) is both an allowlist and a priority order: only producers whose
    basename contains one of the substrings are kept, ordered by which substring
    matched (so ``Vimba:mvGenTL`` prefers Vimba, then mvIMPACT). With it unset,
    every discovered producer is kept except those matching :data:`_DENY_PRODUCERS`.
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


class HarvestersBackend(SoftwareTriggerHandoff):
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

        # Bounded close: a well-behaved producer (mvIMPACT) tears down in ~0.3 s, so
        # the join returns immediately. This bound only protects against a merely
        # *slow* close — it does NOT rescue a truly deadlocking producer: Spinnaker's
        # DevClose holds the GIL, so the daemon can't release it, the join below can
        # never re-acquire it, and the whole process wedges regardless. That is why
        # such producers are excluded in _find_cti_files rather than tolerated here.
        thread = threading.Thread(
            target=_teardown, name=f"harvesters-close-{self._serial}", daemon=True
        )
        thread.start()
        thread.join(timeout=_CLOSE_TIMEOUT_S)
        if thread.is_alive():
            log.warning(
                "Camera %s did not close within %.0fs — the GenTL producer is slow "
                "to tear down; leaving the handle to a daemon thread. Prefer the "
                "mvIMPACT producer.",
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

    def config_values(self, config_str: str) -> dict[str, str]:
        return dict(parse_config(config_str))

    def load_params(self, config_str: str) -> None:
        # Native GenApi persistence TSV, applied best-effort in file order (see
        # _genicam_config). Runs after open(), so open()'s Mono8 stays authoritative.
        if config_str:
            apply_config(self, config_str)
        self._original_trigger_source = self._get_enum("TriggerSource")

    def save_params(self) -> str:
        return normalize_trigger_source(dump_config(self), self._original_trigger_source)

    # ----------------------------------------------------------- triggering

    def _enable_trigger_overlap(self) -> None:
        """Let a trigger be accepted during the previous frame's readout.

        Without this (``TriggerOverlap=Off``, the FLIR default) a FrameStart
        software trigger fired while the sensor is still reading out the previous
        frame is **silently ignored**, so the camera accepts only ~every other
        trigger — roughly halving the software-triggered frame rate and adding a
        full grab-timeout stall on each dropped one. ``ReadOut`` pipelines
        back-to-back triggers and restores the sensor's real rate (measured on
        the Spinnaker backends: ~4.7 → ~64 fps at 4 ms exposure; see
        ``docs/plan-spinnaker-c-backend.md``). Mirrors the same fix in
        :mod:`~octacam.cameras.spinnaker_c` / :mod:`~octacam.cameras.flir`.
        Best-effort: a producer/model without the node keeps its default.
        """
        try:
            self._set_enum("TriggerOverlap", "ReadOut")
        except BackendError as e:
            log.debug("Could not set TriggerOverlap on camera %s: %s", self._serial, e)

    def enable_frame_trigger(self) -> None:
        if not self.is_open():
            return
        clear_freerun_rate_cap(self)  # drop any free-run preview cap before triggering
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")
        self._enable_trigger_overlap()

    def set_trigger_source(self, use_software: bool) -> None:
        if not self.is_open():
            return
        try:
            if use_software:
                self._set_enum("TriggerSource", "Software")
            elif self._original_trigger_source is not None:
                self._set_enum("TriggerSource", self._original_trigger_source)
        except BackendError as e:
            log.warning(
                "Failed to set trigger source on camera %s: %s", self._serial, e
            )

    def begin_software_trigger_preview(self) -> None:
        clear_freerun_rate_cap(self)  # drop any free-run preview cap before triggering
        self._set_enum("TriggerSelector", "FrameStart")
        self._set_enum("TriggerMode", "On")
        self._set_enum("TriggerSource", "Software")
        self._enable_trigger_overlap()

    def trigger_once(self) -> None:
        # Only bump the pending counter; retrieve() fires the device trigger on
        # the grab thread so the shared trigger timer never blocks on this camera.
        self._bump_trigger()

    def begin_freerun(self, fps: float | None = None) -> bool:
        """Switch to continuous free-run (TriggerMode Off).

        Used by the benchmark (``fps=None``, uncapped, to measure the ceiling) and
        by free-run *preview* (``fps`` set, so the rate is capped at the target and
        the preview draws the same bandwidth as an fps-matched recording).
        Best-effort: a failure returns False so the caller skips free-run for this
        camera. A later ``begin_software_trigger_preview`` re-arms the FrameStart
        trigger (and clears the cap), so no explicit restore is needed.
        """
        if not self.is_open():
            return False
        try:
            self._set_enum("TriggerMode", "Off")
            try:
                self._set_enum("AcquisitionMode", "Continuous")
            except BackendError:
                pass
            if fps is not None:
                apply_freerun_rate_cap(self, fps)
            return True
        except BackendError as e:
            log.debug("free-run unsupported on camera %s: %s", self._serial, e)
            return False

    def retrieve_freerun(self, timeout_ms: int, wants_array) -> Frame | None:
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

    def retrieve(self, timeout_ms: int, wants_array) -> Frame | None:
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
            timestamp = getattr(buffer, "timestamp_ns", 0) or 0
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
