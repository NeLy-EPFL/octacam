"""Serial-port discovery, naming and USB recovery for the Arduino plugins.

Enumeration never opens a port and never raises, so `octacam doctor` is safe
beside a live session; only `probe_identity` opens one, on request.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import serial
from serial.tools.list_ports import comports

log = logging.getLogger("octacam")

DEFAULT_BAUD = 115200
# The identify query byte the trigger firmwares (triggerbox, twophoton) answer
# with their banner line; shared by probe_identity and the plugins' links.
# flywheel answers an 8-byte sentinel command instead (see plugins.flywheel).
IDENTIFY_MAGIC = b"?"

# Board names are best-effort (a board's PID varies by revision and bootloader
# mode), so the raw VID:PID is always shown beside them.

_ARDUINO_VID = 0x2341  # Arduino LLC
_ARDUINO_ORG_VID = 0x2A03  # Arduino.org / Genuino
_ESPRESSIF_VID = 0x303A  # Espressif (ESP32-S3 native USB / JTAG-serial)
_FTDI_VID = 0x0403
_CH34X_VID = 0x1A86  # WCH CH340/CH341
_CP210X_VID = 0x10C4  # Silicon Labs CP210x

_ARDUINO_BOARDS: dict[tuple[int, int | None], str] = {
    (0x2341, 0x0042): "Arduino Mega 2560",
    (0x2341, 0x0010): "Arduino Mega 2560",
    (0x2341, 0x003F): "Arduino Mega ADK",
    (0x2341, 0x0043): "Arduino Uno",
    (0x2341, 0x0001): "Arduino Uno",
    (0x2341, 0x0070): "Arduino Nano ESP32",  # Arduino-mode enumeration
}

# USB-serial bridges: likely a microcontroller (a clone or an ESP board), but
# not surely an Arduino.
_BRIDGE_CHIPS: dict[int, str] = {
    _FTDI_VID: "FTDI serial (clone/adapter)",
    _CH34X_VID: "CH340/CH341 (clone)",
    _CP210X_VID: "CP210x (SiLabs)",
}


@dataclass(frozen=True)
class SerialPort:
    """A connected serial port (macOS and Windows often leave the USB strings None)."""

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
        """`"2341:0070"`, with `?` for an unknown half."""
        v = f"{self.vid:04X}" if self.vid is not None else "?"
        p = f"{self.pid:04X}" if self.pid is not None else "?"
        return f"{v}:{p}"


@dataclass(frozen=True)
class SerialIdentity:
    """What `probe_identity` read; `busy` when another process holds the port."""

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
    """`(board_name, likely_microcontroller, likely_arduino)` for a USB VID/PID.

    The description and manufacturer are a last resort: their text varies by
    platform.
    """
    if vid is not None:
        if (vid, pid) in _ARDUINO_BOARDS:
            return _ARDUINO_BOARDS[(vid, pid)], True, True
        if vid == _ARDUINO_VID:
            return "Arduino (unknown model)", True, True
        if vid == _ARDUINO_ORG_VID:
            return "Arduino.org / Genuino", True, True
        if vid == _ESPRESSIF_VID:
            # The Nano ESP32 in native-USB mode, or a bare ESP32-S3 board.
            return "Espressif ESP32-S3 (e.g. Arduino Nano ESP32)", True, True
        if vid in _BRIDGE_CHIPS:
            return _BRIDGE_CHIPS[vid], True, False

    text = f"{description or ''} {manufacturer or ''}".lower()
    if "arduino" in text:
        return "Arduino (unrecognized VID)", True, True

    return "generic serial", False, False


def list_serial_ports() -> list[SerialPort]:
    """The connected serial ports by device path; `[]` and a warning if
    `comports()` raises (it does on some platforms).
    """
    try:
        infos = list(comports())
    except Exception as e:
        log.warning("serial port enumeration failed: %s", e)
        return []
    ports: list[SerialPort] = []
    for info in infos:
        vid, pid, manufacturer = info.vid, info.pid, info.manufacturer
        description = info.description or ""
        board_name, likely_mc, likely_ard = classify_port(
            vid, pid, description, manufacturer
        )
        ports.append(
            SerialPort(
                device=info.device,
                description=description,
                manufacturer=manufacturer,
                product=info.product,
                vid=vid,
                pid=pid,
                serial_number=info.serial_number,
                hwid=info.hwid or "",
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
        s in text for s in ("busy", "in use", "access is denied", "permission denied")
    )


def probe_identity(
    device: str,
    baud: int = DEFAULT_BAUD,
    magic: bytes = IDENTIFY_MAGIC,
    timeout: float = 1.0,
) -> SerialIdentity:
    """Send the identify byte to *device* and read one line back. Never raises.

    It writes to the board, so callers make it opt-in. A port another process
    holds reads as `busy`, except on Linux when the holder opened it without
    `O_EXCL`: the plugins do, so a port a live session holds is probed.
    """
    # Windows ports are always exclusive and do not take the kwarg.
    kwargs: dict[str, Any] = {"exclusive": True} if os.name == "posix" else {}
    try:
        port = serial.Serial(
            device, baud, timeout=timeout, write_timeout=timeout, **kwargs
        )
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


# USBDEVFS_RESET = _IO('U', 20)
_USBDEVFS_RESET = (ord("U") << 8) | 20


def _usb_device_dir(tty_device: str) -> str | None:
    """The sysfs dir of the USB device behind a tty (it holds busnum/devnum), or
    None for a non-USB tty. Linux only.
    """
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
    """Reset the USB device behind *device* from the host: `(ok, message)`.

    Clears a wedged USB-CDC board (the Nano ESP32 can stall every transfer with
    EPIPE while staying enumerated) without an unplug; the tty path survives.
    Never raises: `(False, reason)` off Linux, for a non-USB tty, or without
    write access to the usbfs node. The caller must not hold the tty open.
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


def resolve_device(configured: str | None) -> tuple[str | None, str]:
    """The port a plugin's configured `device` names: `(device | None, reason)`.

    A concrete path is returned as is. `"auto"` (or empty) takes the one
    microcontroller-class port, and with none or several returns None: it never
    guesses.
    """
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
    """The detected ports in one line for an error message: the
    microcontroller-class ones, else a count of the generic ones.
    """
    mcus = [p for p in ports if p.likely_microcontroller]
    if mcus:
        items = [f"{p.device} ({p.board_name})" for p in mcus[:limit]]
        suffix = (
            f", \N{HORIZONTAL ELLIPSIS} (+{len(mcus) - limit} more)"
            if len(mcus) > limit
            else ""
        )
        return ", ".join(items) + suffix
    if ports:
        return (
            f"no microcontroller-class serial ports detected "
            f"({len(ports)} generic port(s) present)"
        )
    return "no serial ports detected"


def udev_rule_for(port: SerialPort, symlink: str = "arduino0") -> str:
    """A udev rule pinning *port* to `/dev/<symlink>`, keyed on VID/PID and
    serial number so it survives re-enumeration.
    """
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
    """A serial open error, plus the detected ports and the fix when *device* is
    not among them (unplugged, or the wrong path).
    """
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
        '  Fix: set [plugins.options].device to one of these (or "auto" for a '
        "single board), or add a stable udev symlink (`octacam doctor` prints one)."
    )
