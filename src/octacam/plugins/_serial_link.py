"""The serial link with a reader thread that triggerbox and twophoton share."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import serial

from octacam.serial_ports import IDENTIFY_MAGIC

log = logging.getLogger("octacam")

_CANCEL_MAGIC = 0xCA  # both trigger firmwares


class SerialReaderLink:
    """A serial port plus a reader thread that parses the board's tokens.

    A subclass implements ``_read_loop`` (its token grammar) and ``send_arm``
    (its arm packet). The callbacks run on the reader thread.
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
        # Serializes open/close, so concurrent reconnects cannot leak a port.
        self._lifecycle_lock = threading.Lock()
        self._on_status = on_status
        self._on_broken = on_broken
        self._on_reject = on_reject
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._identity: str | None = None
        self._identity_event = threading.Event()

    def open(self, device: str, baud: int) -> None:
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
        """Drop the handle after the port died under the reader.

        pyserial's ``is_open`` stays True on a dead port, so the link would read
        as usable and the GUI would never offer a reconnect. Takes only the write
        lock and notifies outside it, so a close() joining the reader cannot
        deadlock against it."""
        with self._write_lock:
            s, self._serial = self._serial, None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
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
                log.warning("%s: serial write failed: %s", self.log_prefix, e)
                return False

    def send_cancel(self) -> None:
        self._write(bytes([_CANCEL_MAGIC]))

    def send_identify(self) -> None:
        self._write(IDENTIFY_MAGIC)

    @property
    def identity(self) -> str | None:
        return self._identity

    def identify(self, timeout: float = 0.5) -> str | None:
        """The firmware banner, or None when the board does not answer in time."""
        self._identity = None
        self._identity_event.clear()
        self.send_identify()
        self._identity_event.wait(timeout)
        return self._identity

    @staticmethod
    def _read_chunk(s) -> bytes:
        """Block for one byte, then drain what is waiting.

        ``read(n)`` waits for n bytes or the 0.2 s port timeout, so a fixed-size
        read held each short token back (the host saw 'R' 140 ms late). This
        delivers a token within USB latency and still wakes every timeout to
        check for shutdown."""
        chunk = s.read(1)
        waiting = getattr(s, "in_waiting", 0) if chunk else 0
        if waiting:
            chunk += s.read(waiting)
        return chunk

    def _dispatch(self, cb, arg) -> None:
        """Call a status/reject callback, logging any error it raises."""
        if cb is None:
            return
        try:
            cb(arg)
        except Exception:
            log.exception("%s: status/reject callback error", self.log_prefix)

    def _read_loop(self) -> None:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError
