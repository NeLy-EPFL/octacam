"""Shared serial reader-thread link for the trigger-controller plugins.

:class:`~octacam.plugins.triggerbox.TriggerboxLink` and
:class:`~octacam.plugins.twophoton.TwoPhotonLink` are near-identical serial links:
each opens a pyserial port, runs a background reader thread that parses
newline-terminated status/banner tokens, and writes arm/cancel/identify packets.
This base owns the common open/close/write/identify lifecycle; a subclass supplies
only its ``_read_loop`` (the token grammar) and ``send_arm`` (the wire packet),
plus three class attributes: ``log_prefix``, ``reader_name`` and ``expected_banner``.

The pyserial module is resolved from the *concrete subclass's* module
(:meth:`SerialReaderLink._serial_module`) rather than imported here, so a test that
stubs ``<plugin>.serial`` is honoured by the shared ``open``/``_write`` too, exactly
as it was when those methods lived in each plugin module.

The lighter flywheel :class:`~octacam.plugins.flywheel.SerialLink` has no reader
thread, so it stays separate.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable

log = logging.getLogger("octacam")

_NO_PYSERIAL_MSG = (
    "pyserial is not importable (it ships with octacam by default, so the "
    "environment may be broken); reinstall with: pip install pyserial"
)

# Wire magics shared by both trigger firmwares (host -> Arduino).
_CANCEL_MAGIC = 0xCA
_IDENTIFY_MAGIC = 0x3F


class SerialReaderLink:
    """Serial link with a background reader thread (shared by the trigger plugins).

    Subclasses set :attr:`log_prefix` / :attr:`reader_name` / :attr:`expected_banner`
    and implement :meth:`_read_loop` (the firmware's token grammar) and ``send_arm``
    (the wire packet). Everything else — open/close, the failed-write guard, the
    identify round-trip, callback dispatch — is shared here. The status/reject
    callbacks run on the reader thread; callers must be thread-safe.
    """

    log_prefix = "serial"
    reader_name = "serial-reader"
    expected_banner = ""

    def __init__(
        self,
        on_status: Callable[[str], None],
        on_broken: Callable[[], None] | None = None,
        on_reject: Callable[[str], None] | None = None,
    ):
        self._serial = None
        self._write_lock = threading.Lock()
        # Serializes the open/close/reconnect lifecycle so two concurrent
        # reconnects (double-click, two browser tabs, or a reconnect racing
        # teardown) cannot each create a port and leak the loser's FD + reader.
        self._lifecycle_lock = threading.Lock()
        self._on_status = on_status
        # Called from the reader thread when the port dies mid-session (not on a
        # clean close), so the owner can surface the lost link to the GUI.
        self._on_broken = on_broken
        self._on_reject = on_reject
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._identity: str | None = None
        self._identity_event = threading.Event()

    def _serial_module(self):
        """The monkeypatchable ``serial`` binding from the concrete subclass's
        module, so a test that stubs ``<plugin>.serial`` reaches the shared code."""
        return getattr(sys.modules.get(type(self).__module__), "serial", None)

    def open(self, device: str, baud: int) -> None:
        serial = self._serial_module()
        if serial is None:
            raise RuntimeError(_NO_PYSERIAL_MSG)
        with self._lifecycle_lock:
            self._close_locked()
            s = serial.Serial(device, baud, timeout=0.2, write_timeout=1)
            self._serial = s
            self._reader_stop.clear()
            self._reader = threading.Thread(
                target=self._read_loop, daemon=True, name=self.reader_name
            )
            self._reader.start()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        """Tear down the port and reader. Caller must hold ``_lifecycle_lock``."""
        self._reader_stop.set()
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                s.close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None

    def _mark_broken(self) -> None:
        """Drop the handle after the port dies under the reader thread.

        Without this the reader exits but pyserial's ``is_open`` stays True, so
        ``is_open``/``is_ready`` would report a dead link as usable forever and the
        GUI would never offer reconnect. Touches only ``_write_lock`` (never the
        lifecycle lock) so it can't deadlock a concurrent close() joining us."""
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        # Notify outside the write lock (and never the lifecycle lock) so a
        # broadcast hook can't deadlock a concurrent close() that is joining us.
        if self._on_broken is not None:
            try:
                self._on_broken()
            except Exception:
                log.exception("%s: on_broken callback error", self.log_prefix)

    @property
    def is_open(self) -> bool:
        s = self._serial
        return s is not None and s.is_open

    def _write(self, data: bytes) -> bool:
        """Write bytes; return whether they were handed to the OS successfully.

        A wedged USB-CDC board can fail here with EPIPE (a plain OSError, which
        pyserial does not always wrap in SerialException), so both are caught and
        reported as a failed write rather than raised."""
        serial = self._serial_module()
        with self._write_lock:
            s = self._serial
            if s is None or not s.is_open:
                return False
            try:
                s.write(data)
                return True
            except (OSError, serial.SerialException) as e:  # pyright: ignore[reportOptionalMemberAccess]
                log.warning("%s: serial write failed: %s", self.log_prefix, e)
                return False

    def send_cancel(self) -> None:
        self._write(bytes([_CANCEL_MAGIC]))

    def send_identify(self) -> None:
        self._write(bytes([_IDENTIFY_MAGIC]))

    @property
    def identity(self) -> str | None:
        return self._identity

    def identify(self, timeout: float = 0.5) -> str | None:
        """Query the firmware banner and wait briefly for the reply (None if the
        board has no identify command — e.g. older firmware)."""
        self._identity = None
        self._identity_event.clear()
        self.send_identify()
        self._identity_event.wait(timeout)
        return self._identity

    def _dispatch(self, cb, arg) -> None:
        """Invoke a status/reject callback, swallowing (and logging) any error."""
        if cb is None:
            return
        try:
            cb(arg)
        except Exception:
            log.exception("%s: status/reject callback error", self.log_prefix)

    def _read_loop(self) -> None:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError
