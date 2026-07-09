"""Serial-port enumeration, classification, and helpers for octacam.

Arduino boards (Nano ESP32, Mega 2560, stepper controllers) are commonly used
with octacam as hardware triggers and LED-strobe controllers, driven through the
opt-in plugins (``triggerbox``, ``twophoton``, ``flywheel``). This module is the
serial analogue of :mod:`octacam.cameras.registry`: it enumerates the connected
serial ports, gives each a best-effort friendly board name from its USB VID/PID,
and provides the small helpers the CLI (``octacam doctor``/``octacam config``),
the plugins, and the web app share.

Design notes:

* ``comports`` is imported at module top so tests can monkeypatch
  ``octacam.serial_ports.comports`` without touching pyserial internals.
* Everything degrades gracefully: a broken/absent pyserial or a raising
  ``comports()`` yields an empty list plus a warning, never an exception — so
  ``octacam doctor`` can never be taken down by serial enumeration.
* Enumeration is **passive**: it never opens a port. Only :func:`probe_identity`
  opens a port (to read a firmware banner), and it is strictly opt-in.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

try:
    import serial
    from serial.tools.list_ports import comports
except ImportError:  # pyserial ships by default; guard against a broken env
    serial = None  # type: ignore[assignment]
    comports = None  # type: ignore[assignment]

log = logging.getLogger("octacam")

DEFAULT_BAUD = 115200
# The triggerbox firmware's identify query byte ('?'); the board replies
# "TRIGGERBOX <version>\n". Shared so probe_identity and the plugin agree.
IDENTIFY_MAGIC = b"?"

_NO_PYSERIAL_MSG = (
    "pyserial is not importable (it ships with octacam by default, so the "
    "environment may be broken); reinstall with: pip install pyserial"
)

# The bundled plugins that talk to a serial/Arduino device, and the firmware
# banner each expects from an identify probe (used to flag a wrong board). Only
# triggerbox firmware answers an identify today; twophoton/flywheel have no such
# command, so they are absent from EXPECTED_BANNER.
SERIAL_PLUGINS = frozenset({"triggerbox", "twophoton", "flywheel"})
EXPECTED_BANNER = {"triggerbox": "TRIGGERBOX"}

# --- USB VID/PID classification --------------------------------------------
# Names are driven primarily by VID, refined by PID. PIDs for the same board
# vary by revision and by bootloader/DFU mode, so a name is best-effort — the
# raw VID:PID is always shown alongside it so an operator can verify.

_ARDUINO_VID = 0x2341  # Arduino LLC
_ARDUINO_ORG_VID = 0x2A03  # Arduino.org / Genuino
_ESPRESSIF_VID = 0x303A  # Espressif (ESP32-S3 native USB / JTAG-serial)
_FTDI_VID = 0x0403
_CH34X_VID = 0x1A86  # WCH CH340/CH341
_CP210X_VID = 0x10C4  # Silicon Labs CP210x

# Known official-Arduino boards by (vid, pid).
_ARDUINO_BOARDS: dict[tuple[int, int], str] = {
    (0x2341, 0x0042): "Arduino Mega 2560",
    (0x2341, 0x0010): "Arduino Mega 2560",
    (0x2341, 0x003F): "Arduino Mega ADK",
    (0x2341, 0x0043): "Arduino Uno",
    (0x2341, 0x0001): "Arduino Uno",
    (0x2341, 0x0070): "Arduino Nano ESP32",  # Arduino-mode enumeration
}

# USB-serial bridge chips: in this domain almost always an Arduino clone or an
# ESP dev board. likely_microcontroller is set, but not the stricter
# likely_arduino (we can't be sure it's an Arduino vs. any other serial gadget).
_BRIDGE_CHIPS: dict[int, str] = {
    _FTDI_VID: "FTDI serial (clone/adapter)",
    _CH34X_VID: "CH340/CH341 (clone)",
    _CP210X_VID: "CP210x (SiLabs)",
}


@dataclass(frozen=True)
class SerialPort:
    """A connected serial port. Every USB field is optional — macOS and Windows
    frequently return ``None`` for manufacturer/product/serial_number."""

    device: str
    description: str
    manufacturer: str | None
    product: str | None
    vid: int | None
    pid: int | None
    serial_number: str | None
    hwid: str
    board_name: str
    likely_microcontroller: bool
    likely_arduino: bool

    @property
    def vid_pid(self) -> str:
        """``"2341:0070"`` style id, or ``"?:?"`` when the VID/PID is unknown."""
        v = f"{self.vid:04X}" if self.vid is not None else "?"
        p = f"{self.pid:04X}" if self.pid is not None else "?"
        return f"{v}:{p}"


@dataclass(frozen=True)
class SerialIdentity:
    """Result of the opt-in firmware identity probe (:func:`probe_identity`).

    ``busy`` is True when the port could not be opened because something already
    holds it (a running plugin); the probe returns rather than raising."""

    device: str
    banner: str | None
    busy: bool
    error: str | None


def classify_port(
    vid: int | None,
    pid: int | None,
    description: str | None = None,
    manufacturer: str | None = None,
) -> tuple[str, bool, bool]:
    """Map a USB VID/PID to ``(board_name, likely_microcontroller, likely_arduino)``.

    Pure function (no I/O) so it is trivially unit-testable. Classification is
    VID/PID-driven; the description/manufacturer strings are a last-resort
    refinement only, because their text is unreliable across platforms."""
    if vid is not None:
        if (vid, pid) in _ARDUINO_BOARDS:
            return _ARDUINO_BOARDS[(vid, pid)], True, True
        if vid == _ARDUINO_VID:
            return "Arduino (unknown model)", True, True
        if vid == _ARDUINO_ORG_VID:
            return "Arduino.org / Genuino", True, True
        if vid == _ESPRESSIF_VID:
            # The Arduino Nano ESP32 enumerates here in native-USB/JTAG mode; a
            # bare ESP32-S3 dev board does too. Treat as likely-Arduino in this
            # domain but keep the name honest.
            return "Espressif ESP32-S3 (e.g. Arduino Nano ESP32)", True, True
        if vid in _BRIDGE_CHIPS:
            return _BRIDGE_CHIPS[vid], True, False

    # Last-resort text refinement for otherwise-unknown ports.
    text = f"{description or ''} {manufacturer or ''}".lower()
    if "arduino" in text:
        return "Arduino (unrecognized VID)", True, True

    return "generic serial", False, False


def list_serial_ports() -> list[SerialPort]:
    """Enumerate connected serial ports, sorted by device path.

    Never raises: a missing pyserial or a ``comports()`` that itself raises
    (seen on some platforms) yields ``[]`` plus a warning."""
    if comports is None:
        log.warning("pyserial not available; cannot enumerate serial ports")
        return []
    try:
        infos = list(comports())
    except Exception as e:
        log.warning("serial port enumeration failed: %s", e)
        return []
    ports: list[SerialPort] = []
    for info in infos:
        vid = getattr(info, "vid", None)
        pid = getattr(info, "pid", None)
        description = getattr(info, "description", None) or ""
        manufacturer = getattr(info, "manufacturer", None)
        board_name, likely_mc, likely_ard = classify_port(
            vid, pid, description, manufacturer
        )
        ports.append(
            SerialPort(
                device=info.device,
                description=description,
                manufacturer=manufacturer,
                product=getattr(info, "product", None),
                vid=vid,
                pid=pid,
                serial_number=getattr(info, "serial_number", None),
                hwid=getattr(info, "hwid", "") or "",
                board_name=board_name,
                likely_microcontroller=likely_mc,
                likely_arduino=likely_ard,
            )
        )
    ports.sort(key=lambda p: p.device)
    return ports


def _is_busy_error(exc: Exception) -> bool:
    """Whether an open failure means the port is held by something else."""
    errno = getattr(exc, "errno", None)
    if errno in (16, 11):  # EBUSY, EAGAIN
        return True
    text = str(exc).lower()
    return any(
        s in text
        for s in ("busy", "in use", "access is denied", "permission denied")
    )


def probe_identity(
    device: str,
    baud: int = DEFAULT_BAUD,
    magic: bytes = IDENTIFY_MAGIC,
    timeout: float = 1.0,
) -> SerialIdentity:
    """Open *device* briefly, send the identify byte, read one line back.

    Opt-in and mildly invasive (it writes one query byte), so callers gate it
    behind an explicit flag. A port already held by a running plugin fails to
    open and is reported as ``busy=True`` rather than raising.

    Caveat: on Linux a plugin that opened the port without ``O_EXCL`` does not
    block a second open, so a concurrently-held port may not be detected as busy
    there; the ``--probe-serial`` caveat documents this."""
    if serial is None:
        return SerialIdentity(device, banner=None, busy=False, error=_NO_PYSERIAL_MSG)
    # Ask for an exclusive open on POSIX so two probes (or an exclusive holder)
    # are correctly seen as busy; Windows ports are exclusive already and the
    # kwarg is unsupported there.
    kwargs = {"exclusive": True} if os.name == "posix" else {}
    try:
        port = serial.Serial(device, baud, timeout=timeout, write_timeout=timeout, **kwargs)
    except Exception as e:
        return SerialIdentity(device, banner=None, busy=_is_busy_error(e), error=str(e))
    try:
        try:
            port.reset_input_buffer()
        except Exception:
            pass
        port.write(magic)
        port.flush()
        line = port.readline()
        banner = line.decode("ascii", "replace").strip() or None
        return SerialIdentity(device, banner=banner, busy=False, error=None)
    except Exception as e:
        return SerialIdentity(device, banner=None, busy=False, error=str(e))
    finally:
        try:
            port.close()
        except Exception:
            pass


# USBDEVFS_RESET ioctl = _IO('U', 20): re-initialise a USB device's link from
# the host without a physical unplug (the tty path is preserved).
_USBDEVFS_RESET = (ord("U") << 8) | 20


def _usb_device_dir(tty_device: str) -> str | None:
    """The sysfs USB *device* dir backing a tty (has busnum/devnum), or None.

    Walks up from ``/sys/class/tty/<name>/device`` (the USB *interface*) to the
    parent USB device node. Linux-only; returns None for a non-USB tty."""
    name = os.path.basename(os.path.realpath(tty_device))
    start = f"/sys/class/tty/{name}/device"
    if not os.path.exists(start):
        return None
    path = os.path.realpath(start)
    for _ in range(8):  # interface -> device is one hop; bound the walk anyway
        if os.path.exists(os.path.join(path, "busnum")) and os.path.exists(
            os.path.join(path, "devnum")
        ):
            return path
        parent = os.path.dirname(path)
        if parent == path or not parent.startswith("/sys"):
            break
        path = parent
    return None


def reset_usb_device(device: str) -> tuple[bool, str]:
    """Issue a host-side USB bus reset to the USB device backing *device*.

    Some USB-CDC microcontrollers — notably the ESP32-S3 on the Arduino Nano
    ESP32 — can *wedge*: the port stays enumerated but every write/control
    transfer stalls with ``EPIPE``, so the board is unreachable (arm packets
    never land; cameras then hang waiting for a trigger that never fires). A
    ``USBDEVFS_RESET`` ioctl re-initialises the link and clears the stall without
    a physical unplug; the tty path is preserved.

    Returns ``(ok, message)``. It is a best-effort recovery: returns
    ``(False, reason)`` — never raises — on non-Linux, when the backing USB node
    can't be located, or when the usbfs node can't be opened (needs write access,
    e.g. root or the ``plugdev`` group). **The caller must not hold the tty open.**
    """
    if not sys.platform.startswith("linux"):
        return False, "USB bus reset is only implemented on Linux"
    try:
        import fcntl
    except ImportError:  # pragma: no cover - fcntl ships on posix
        return False, "fcntl is unavailable; cannot issue a USB reset"
    usbdir = _usb_device_dir(device)
    if usbdir is None:
        return False, (
            f"{device}: could not locate the backing USB device in sysfs "
            "(not a USB serial port?)"
        )
    try:
        with open(os.path.join(usbdir, "busnum")) as f:
            bus = int(f.read().strip())
        with open(os.path.join(usbdir, "devnum")) as f:
            dev = int(f.read().strip())
    except (OSError, ValueError) as e:
        return False, f"{device}: could not read USB bus/dev numbers: {e}"
    node = f"/dev/bus/usb/{bus:03d}/{dev:03d}"
    try:
        fd = os.open(node, os.O_WRONLY)
    except OSError as e:
        return False, (
            f"cannot open {node} to reset it ({e}); a USB reset needs write "
            "access to the usbfs node (root, or the plugdev group)"
        )
    try:
        fcntl.ioctl(fd, _USBDEVFS_RESET, 0)
    except OSError as e:
        return False, f"USB reset ioctl on {node} failed: {e}"
    finally:
        os.close(fd)
    return True, f"issued a USB bus reset to {node} (backing {device})"


def wait_for_device(device: str, timeout: float = 3.0) -> bool:
    """Poll until *device* exists again (a bus reset briefly drops the node)."""
    real = os.path.realpath(device)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if os.path.exists(real) or os.path.exists(device):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def resolve_device(
    configured: str | None, baud: int = DEFAULT_BAUD
) -> tuple[str | None, str]:
    """Resolve a plugin's configured ``device`` to a concrete port.

    Returns ``(device|None, reason)``. Precedence: an explicit concrete path is
    returned unchanged (config always wins); ``"auto"`` / empty auto-selects the
    single microcontroller-class port. On zero or multiple candidates it returns
    ``(None, reason)`` — deliberately *not* guessing — so the caller surfaces a
    clear error (auto-select only fires when unambiguous)."""
    text = (configured or "").strip()
    if text and text.lower() != "auto":
        return text, f"using configured device {text}"
    ports = list_serial_ports()
    candidates = [p for p in ports if p.likely_microcontroller]
    if len(candidates) == 1:
        chosen = candidates[0]
        return chosen.device, f"auto-selected {chosen.device} ({chosen.board_name})"
    if not candidates:
        return None, (
            "device set to 'auto' but no microcontroller-class serial port was "
            f"detected ({format_candidates(ports)})"
        )
    return None, (
        f"device set to 'auto' but {len(candidates)} candidate ports were found; "
        f"set [plugins.options].device explicitly to one of: "
        f"{format_candidates(candidates)}"
    )


def format_candidates(ports: list[SerialPort], limit: int = 8) -> str:
    """One-line human summary of detected ports, for error messages.

    Shows only microcontroller-class ports (the plausible Arduino candidates); a
    host's dozens of legacy ``/dev/ttyS*`` are summarized as a count so the
    message stays useful."""
    mcus = [p for p in ports if p.likely_microcontroller]
    if mcus:
        items = [f"{p.device} ({p.board_name})" for p in mcus[:limit]]
        suffix = f", … (+{len(mcus) - limit} more)" if len(mcus) > limit else ""
        return ", ".join(items) + suffix
    if ports:
        return (
            f"no microcontroller-class serial ports detected "
            f"({len(ports)} generic port(s) present)"
        )
    return "no serial ports detected"


def udev_rule_for(port: SerialPort, symlink: str = "arduino0") -> str:
    """A udev rule line that pins *port* to a stable ``/dev/<symlink>`` path.

    Keyed on the USB VID/PID and (when present) the board's serial number, so it
    survives re-enumeration order changes — the stable-device-path approach the
    plugin READMEs recommend."""
    parts = ['SUBSYSTEM=="tty"']
    if port.vid is not None:
        parts.append(f'ATTRS{{idVendor}}=="{port.vid:04x}"')
    if port.pid is not None:
        parts.append(f'ATTRS{{idProduct}}=="{port.pid:04x}"')
    if port.serial_number:
        parts.append(f'ATTRS{{serial}}=="{port.serial_number}"')
    parts.append(f'SYMLINK+="{symlink}"')
    return ", ".join(parts)


def explain_open_failure(device: str, exc: Exception) -> str:
    """Turn a bare serial open error into an actionable message.

    When *device* is not among the connected ports (the common "unplugged /
    wrong path" case), append the detected candidates and how to fix it. When
    the device *is* present (so the failure is permissions/busy/etc.), return the
    plain error unembellished."""
    base = f"failed to open {device}: {exc}"
    try:
        ports = list_serial_ports()
    except Exception:
        return base
    real = os.path.realpath(device)
    present = any(os.path.realpath(p.device) == real for p in ports)
    if present:
        return base
    return (
        f"{base}\n  {device} is not among the connected serial ports "
        f"(detected: {format_candidates(ports)}).\n"
        "  Fix: set [plugins.options].device to one of these (or \"auto\" for a "
        "single board), or add a stable udev symlink (`octacam doctor` prints one)."
    )
