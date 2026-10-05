"""The serial plugins' base: one link with a reader thread, and a plugin that
opens the board, identifies it, recovers a wedged USB link and checks and
flashes its firmware (triggerbox, twophoton and flywheel subclass both)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, ClassVar

import serial

from octacam import firmware as fw
from octacam import serial_ports
from octacam.plugins.base import Plugin

if TYPE_CHECKING:
    from fastapi import APIRouter

log = logging.getLogger("octacam")


class SerialLink:
    """A serial port plus a reader thread that hands every chunk it reads to
    :meth:`_feed`, the subclass's token grammar. Callbacks run on the reader
    thread; :meth:`identify` sends ``identify_query`` and waits for the line
    starting with ``banner_prefix``."""

    name = "serial"  # log prefix and reader-thread name
    banner_prefix = ""
    identify_query = serial_ports.IDENTIFY_MAGIC

    def __init__(self, on_broken: Callable[[], None]):
        self._serial = None
        self._write_lock = threading.Lock()
        # Serializes open/close, so concurrent reconnects cannot leak a port.
        self._lifecycle_lock = threading.Lock()
        self._on_broken = on_broken
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._buf = bytearray()
        self.identity: str | None = None
        self._identity_event = threading.Event()

    def open(self, device: str, baud: int) -> None:
        with self._lifecycle_lock:
            self._close_locked()
            self._serial = serial.Serial(device, baud, timeout=0.2, write_timeout=1)
            self._buf = bytearray()
            self._reader_stop.clear()
            self._reader = threading.Thread(
                target=self._read_loop, daemon=True, name=f"{self.name}-reader"
            )
            self._reader.start()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        self._reader_stop.set()
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                s.close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None

    def _mark_broken(self) -> None:
        """Drop the handle of a port that died under the reader: pyserial's
        ``is_open`` stays True on a dead port, so the GUI would never offer a
        reconnect. Takes only the write lock and notifies outside it, so a
        close() joining the reader cannot deadlock against it."""
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        self._dispatch(self._on_broken)

    @property
    def is_open(self) -> bool:
        s = self._serial
        return s is not None and s.is_open

    def _write(self, data: bytes) -> bool:
        """Write *data*; False when it did not reach the OS. A wedged USB-CDC
        board fails with a bare OSError (EPIPE) that pyserial does not always wrap."""
        with self._write_lock:
            s = self._serial
            if s is None or not s.is_open:
                return False
            try:
                s.write(data)
                return True
            except (OSError, serial.SerialException) as e:
                log.warning("%s: serial write failed: %s", self.name, e)
                return False

    def identify(self, timeout: float = 0.5) -> str | None:
        """The board's banner, or None when it does not answer in time."""
        self.identity = None
        self._identity_event.clear()
        self._write(self.identify_query)
        self._identity_event.wait(timeout)
        return self.identity

    def _identified(self, line: str) -> bool:
        """Take *line* as the identify reply when it is the board's banner."""
        if not line.upper().startswith(self.banner_prefix):
            return False
        self.identity = line
        self._identity_event.set()
        return True

    def _lines(self, chunk: bytes) -> Iterator[str]:
        """The non-empty lines *chunk* completes, stripped; a partial line waits
        for the next chunk."""
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            token = line.decode("ascii", "replace").strip()
            if token:
                yield token

    def _dispatch(self, callback: Callable[..., None], *args) -> None:
        try:
            callback(*args)
        except Exception:
            log.exception("%s: callback error", self.name)

    @staticmethod
    def _read_chunk(s) -> bytes:
        """Block for one byte, then drain what is waiting: a token arrives within
        USB latency (a fixed ``read(n)`` waits for n bytes or the 0.2 s port
        timeout), and the reader still wakes every timeout to check for shutdown."""
        chunk = s.read(1)
        waiting = s.in_waiting if chunk else 0
        if waiting:
            chunk += s.read(waiting)
        return chunk

    def _read_loop(self) -> None:
        while not self._reader_stop.is_set():
            s = self._serial
            if s is None or not s.is_open:
                break
            try:
                chunk = self._read_chunk(s)
            except Exception:  # SerialException, or os.read on a port closed under us
                if not self._reader_stop.is_set():
                    log.debug("%s: read error in reader thread", self.name, exc_info=True)
                    self._mark_broken()
                break
            if chunk:
                self._feed(chunk)

    def _feed(self, chunk: bytes) -> None:
        raise NotImplementedError


class SerialPlugin(Plugin):
    """A plugin driving one Arduino over a :class:`SerialLink`: open the
    configured port, identify and classify the board, recover a wedged USB link,
    flash its firmware, and the reconnect / firmware / flash routes.

    ``port_lock`` (re-entrant) is held across an open, a USB recovery and a
    flash's close -> upload -> reopen, so nothing seizes the port mid-upload."""

    # The board's sketch (also read from the class by `octacam flash` and
    # doctor) and its default port.
    firmware: ClassVar[fw.FirmwareSpec]
    default_device: ClassVar[str]
    reconnect_path: ClassVar[str]
    # The WS topic the board's run state is pushed on (and reported as
    # ``arduino_state``); None for a board that reports none.
    state_topic: ClassVar[str | None] = None
    # A healthy board always answers identify, so a silent one may sit on a
    # wedged USB-CDC link: an open (never a flash's reopen) tries one bus reset.
    recover_silent: ClassVar[bool] = False

    def __init__(
        self,
        link: SerialLink,
        *,
        device: str,
        baud: int,
        auto_flash: bool,
        firmware: fw.FirmwareSpec | None = None,
    ):
        # What the config names (a path or "auto"; `octacam flash --device`
        # replaces it before setup), and the port it resolved to.
        self.configured_device = device
        self.device = device
        self.baud = baud
        self.auto_flash = auto_flash
        self.firmware_spec = firmware or self.firmware
        self.needed_build = fw.source_build(self.firmware_spec)
        self.firmware_check: fw.FirmwareCheck | None = None
        self.banner: str | None = None
        self.firmware_ok = True
        self.last_error: str | None = None
        self.board_state = "idle"
        self.port_lock = threading.RLock()
        self._link = link

    # ------------------------------------------------------------ link

    def setup(self) -> None:
        self._open()

    def is_ready(self) -> bool:
        return self._link.is_open

    def _open(self, *, recover: bool = True) -> str | None:
        """(Re)open the link and identify the board; the error message, or None.
        Never raises, so a missing board does not stop the GUI (reconnect
        retries it)."""
        with self.port_lock:
            self.banner = None
            self.firmware_ok = True
            self.last_error = None
            device, reason = serial_ports.resolve_device(self.configured_device)
            if device is None:
                log.warning("%s: %s", self.name, reason)
                return reason
            if device != self.device:
                log.info("%s: %s", self.name, reason)
                self.device = device
            try:
                self._link.open(device, self.baud)
            except Exception as e:
                msg = serial_ports.explain_open_failure(device, e)
                log.warning("%s: %s", self.name, msg)
                return msg
            log.info("%s: opened %s @ %d", self.name, device, self.baud)
            self._identify()
            if (
                self.banner is None
                and recover
                and self.recover_silent
                and self._recover_usb("the board did not answer an identity query")
            ):
                self._identify()
            return None

    def _recover_usb(self, why: str) -> bool:
        """Close, reset the USB device and reopen; whether the link is open
        again. Holds the port lock throughout, so a flash cannot take the tty
        in between."""
        with self.port_lock:
            device = self.device
            log.warning(
                "%s: %s; attempting a USB bus reset on %s to recover", self.name, why, device
            )
            self._link.close()
            ok, msg = serial_ports.reset_usb_device(device)
            log.warning("%s: %s", self.name, msg)
            if ok:
                serial_ports.wait_for_device(device, timeout=3.0)
            try:
                self._link.open(device, self.baud)
            except Exception as e:
                log.warning(
                    "%s: reopen after USB reset failed: %s",
                    self.name,
                    serial_ports.explain_open_failure(device, e),
                )
                return False
            if self._link.is_open:
                log.info("%s: reopened %s after USB reset", self.name, device)
            return self._link.is_open

    def _identify(self) -> None:
        """Read and classify the board's banner. A board on an OUTDATED or
        UNIDENTIFIED build is still driven (a reflash is only offered), a foreign
        or wrong-version one is not."""
        banner = self.banner = self._link.identify()
        spec = self.firmware_spec
        reflash = f"reflash to {spec.banner_prefix} {spec.protocol_version}"
        if self.needed_build is None:
            # No source to compare builds with: compatible unless the banner
            # names another firmware or protocol version.
            self.firmware_check = None
            name, version, _ = fw.parse_banner(banner)
            self.firmware_ok = not banner or (
                name == spec.banner_prefix.upper()
                and version in (None, spec.protocol_version)
            )
            if banner is None:
                log.info("%s: no firmware identity from %s; proceeding", self.name, self.device)
            elif not self.firmware_ok:
                log.warning(
                    "%s: %s reports firmware %r; %s (it is not driven until then)",
                    self.name, self.device, banner, reflash,
                )
            return
        check = self.firmware_check = fw.classify(spec, banner, self.needed_build)
        self.firmware_ok = fw.arm_compatible(check)
        S = fw.FirmwareState
        if check.state is S.CURRENT:
            log.info("%s: %s firmware %s", self.name, self.device, check.detail)
        elif check.state is S.OUTDATED:
            log.warning(
                "%s: %s is out of date — %s; run `octacam flash` (or the Flash "
                "firmware button) to upload the current build. It still works.",
                self.name, self.device, check.detail,
            )
        elif check.state is S.UNIDENTIFIED:
            log.info(
                "%s: %s sent no firmware identity (%s); proceeding",
                self.name, self.device, check.detail,
            )
        else:  # WRONG_VERSION / WRONG_BOARD: not driven
            log.warning(
                "%s: %s — %s; %s (it is not driven until then). Run `octacam "
                "flash` or use the Flash firmware button.",
                self.name, self.device, check.detail, reflash,
            )

    # ------------------------------------------------------------ status

    def link_status(self) -> dict:
        """The port and firmware fields of every status, push and reply."""
        check = self.firmware_check
        return {
            "device": self.device,
            "firmware": self.banner,
            "firmware_ok": self.firmware_ok,
            "firmware_state": check.state.value if check else None,
            "needs_flash": bool(check and check.needs_flash),
            "error": self.last_error,
        }

    def _state_field(self) -> dict:
        return {} if self.state_topic is None else {"arduino_state": self.board_state}

    def status(self) -> dict:
        return {**self.link_status(), **self._state_field()}

    def _set_state(self, state: str) -> None:
        self.board_state = state
        self._push_state()

    def _push_state(self) -> None:
        """Push the run state, readiness and firmware to the board's tab: every
        push carries them all, so no client polls."""
        if self.state_topic is not None:
            self.broadcast(
                self.state_topic,
                {**self.link_status(), "state": self.board_state, "ready": self.is_ready()},
            )

    def report_error(self, msg: str) -> None:
        """Log a link or arm failure and show it in the GUI: otherwise the
        cameras just wait, with no visible cause."""
        log.error("%s: %s", self.name, msg)
        self.last_error = msg
        self._push_state()

    # ------------------------------------------------------------ firmware

    def busy_reason(self) -> str | None:
        """Why the board cannot be flashed now (it is in use), or None."""
        return None

    def firmware_provisioning(self) -> dict:
        """The firmware picture for ``octacam flash`` and the GUI."""
        spec = self.firmware_spec
        out: dict = {
            "plugin": self.name,
            "device": self.device,
            "firmware": self.banner,
            "firmware_ok": self.firmware_ok,
            "needed_build": self.needed_build,
            "can_flash": self.needed_build is not None and fw.arduino_cli_path() is not None,
        }
        if self.firmware_check is not None:
            out.update(self.firmware_check.to_dict())
        else:
            out["state"] = None
            if self.needed_build is not None:
                out["detail"] = "the board has not been probed"
            else:
                why = "was not found" if spec.sketch_dir is None else "could not be read"
                out["detail"] = (
                    f"the sketch source {why}, so the board's build can't be compared "
                    "or flashed"
                    if self.firmware_ok
                    else f"the board does not run {spec.banner_prefix} "
                    f"v{spec.protocol_version} firmware (arming is disabled); the "
                    f"sketch source {why}, so it can't be flashed"
                )
            out["needs_flash"] = False
            out["safe_to_auto_flash"] = False
        out["auto_flash"] = self.auto_flash
        return out

    def flash_firmware(self, on_line: Callable[[str], None] | None = None) -> fw.FlashResult:
        """Upload the current firmware, then reopen and re-identify; never raises."""
        result = self._flash(on_line)
        if result.ok:
            self.board_state = "idle"  # the board rebooted after the upload
        else:
            self.last_error = result.message
        self._push_state()
        return result

    def _flash(self, on_line: Callable[[str], None] | None) -> fw.FlashResult:
        """Refused while busy or without the source. An upload whose reopen
        fails drops the stale check, so the board does not still read as outdated."""
        busy = self.busy_reason()
        if busy is not None:
            return fw.FlashResult(False, busy)
        if self.needed_build is None:
            return fw.FlashResult(
                False,
                "the sketch source was not found; auto-flash is unavailable (flash "
                "manually with arduino-cli, or set OCTACAM_ARDUINO_DIR)",
            )
        with self.port_lock:
            device, reason = serial_ports.resolve_device(self.configured_device)
            if device is None:
                return fw.FlashResult(False, reason)
            self._link.close()
            log.info("firmware: flashing %s (build %s) to %s …",
                     self.firmware_spec.name, self.needed_build, device)
            result = fw.flash(self.firmware_spec, device, self.needed_build,
                              cli=fw.arduino_cli_path(), on_line=on_line)
            log.log(logging.INFO if result.ok else logging.ERROR, "firmware: %s", result.message)
            try:  # the board reboots after an upload
                serial_ports.wait_for_device(device, 8.0)
            except Exception:
                log.debug("firmware: wait_for_device raised", exc_info=True)
            reopen_err = self._open(recover=False)
            if reopen_err is not None:
                log.warning("firmware: could not reopen %s after flashing: %s",
                            device, reopen_err)
                if result.ok:
                    self.firmware_check = None
                    result = fw.FlashResult(
                        True,
                        f"{result.message} — but the port could not be reopened to "
                        "confirm; replug or power-cycle the board",
                        result.log, result.build,
                    )
            return result

    # ------------------------------------------------------------ routes

    def api_router(self) -> APIRouter:
        """The reconnect, firmware and flash routes; a subclass adds its own.
        Sync handlers, so a tens-of-seconds flash runs on a worker thread."""
        from fastapi import APIRouter, Body

        router = APIRouter()

        @router.post(self.reconnect_path)
        def reconnect(payload: dict = Body(default={})):
            """Reopen the port, switching to ``{"device": ...}`` when given."""
            device = payload.get("device") if isinstance(payload, dict) else None
            if isinstance(device, str) and device.strip():
                self.configured_device = device.strip()
            error = self._open()
            return {
                **self.link_status(),
                **self._state_field(),
                "ready": self.is_ready(),
                "error": error,
            }

        @router.get(f"/api/{self.name}/firmware")
        def get_firmware():
            """Firmware state vs. the sketch source + whether octacam can flash it."""
            return self.firmware_provisioning()

        @router.post(f"/api/{self.name}/flash")
        def flash(payload: dict = Body(default={})):
            """Compile and upload the current firmware, then report the new state."""
            result = self.flash_firmware()
            return {
                **result.to_dict(),
                "firmware": self.banner,
                "firmware_ok": self.firmware_ok,
                "ready": self.is_ready(),
                "provisioning": self.firmware_provisioning(),
            }

        return router
