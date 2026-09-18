"""Best-effort USB bus reset for a camera identified by its USB serial number.

Reuses :func:`octacam.serial_ports.reset_usb_node` (the ``USBDEVFS_RESET``
plumbing originally built for the triggerbox's wedged USB-CDC link) but
resolves the target ``(bus, dev)`` by walking sysfs for a matching USB
``serial`` file instead of by tty path — a GenICam-USB3 camera has no tty
node, so ``serial_ports._usb_device_dir``'s tty-side walk doesn't apply here.

Exists because a USB3 camera's SuperSpeed link can fail to train and fall
back to USB 2.0 (see ``cameras/basler.py``'s ``_describe_open_failure``), and
real-hardware testing (2026-09-18) found this is sometimes a one-off
link-training fluke rather than a hard fault: the same camera on the same
physical port failed once and trained cleanly on a later attempt. A host bus
reset + retry recovers those without any physical reseating.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable

from octacam.serial_ports import reset_usb_node

_SYSFS_USB_DEVICES = "/sys/bus/usb/devices"


def find_usb_bus_dev_by_serial(serial: str) -> tuple[int, int] | None:
    """Scan sysfs USB device nodes for one whose ``serial`` attribute matches.

    Returns ``(busnum, devnum)``, or None if no match is found (including
    when ``/sys/bus/usb/devices`` itself doesn't exist, e.g. non-Linux)."""
    try:
        entries = os.listdir(_SYSFS_USB_DEVICES)
    except OSError:
        return None
    for name in entries:
        base = os.path.join(_SYSFS_USB_DEVICES, name)
        try:
            with open(os.path.join(base, "serial")) as f:
                if f.read().strip() != serial:
                    continue
            with open(os.path.join(base, "busnum")) as f:
                bus = int(f.read().strip())
            with open(os.path.join(base, "devnum")) as f:
                dev = int(f.read().strip())
        except (OSError, ValueError):
            continue
        return bus, dev
    return None


def reset_camera_usb_link(serial: str) -> tuple[bool, str]:
    """Issue a host-side USB bus reset to the camera whose USB serial is *serial*.

    Never raises. Returns ``(False, reason)`` on non-Linux, when the camera
    can't be located in sysfs by serial, or when the reset ioctl fails (e.g.
    missing permission — root or the ``plugdev`` group, same requirement as
    ``serial_ports.reset_usb_device``). **The caller must have released any
    handle on the camera first** (mirrors ``reset_usb_device``'s contract).
    """
    if not sys.platform.startswith("linux"):
        return False, "USB bus reset is only implemented on Linux"
    located = find_usb_bus_dev_by_serial(serial)
    if located is None:
        return False, f"camera {serial}: could not locate it in sysfs by serial"
    bus, dev = located
    return reset_usb_node(bus, dev, f"camera {serial}")


def wait_for_serial(
    detect_serials: Callable[[], Iterable[str]], serial: str, timeout: float = 3.0
) -> bool:
    """Poll *detect_serials* until it reports *serial* again, or *timeout* elapses.

    A bus reset briefly drops and re-adds the USB device; mirrors
    ``serial_ports.wait_for_device``'s tty-node poll, generalized to any
    backend's own "list currently detected serials" call."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if serial in detect_serials():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
