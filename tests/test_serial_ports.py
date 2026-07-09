"""Unit tests for octacam.serial_ports (enumeration, classification, helpers).

Enumeration and probing are monkeypatched so these run identically with or
without a real board plugged in."""

from types import SimpleNamespace

import pytest

from octacam import serial_ports as sp


def _port(device, vid=None, pid=None, sn=None, desc="", manuf=None, product=None):
    return SimpleNamespace(
        device=device,
        description=desc,
        manufacturer=manuf,
        product=product,
        vid=vid,
        pid=pid,
        serial_number=sn,
        hwid=f"{device}-hwid",
    )


# --------------------------------------------------------------- classify_port


@pytest.mark.parametrize(
    "vid,pid,expected_name,mcu,arduino",
    [
        (0x2341, 0x0042, "Arduino Mega 2560", True, True),
        (0x2341, 0x0070, "Arduino Nano ESP32", True, True),
        (0x2341, 0xABCD, "Arduino (unknown model)", True, True),
        (0x2A03, 0x0001, "Arduino.org / Genuino", True, True),
        (0x303A, 0x1001, "Espressif ESP32-S3 (e.g. Arduino Nano ESP32)", True, True),
        (0x0403, 0x6001, "FTDI serial (clone/adapter)", True, False),
        (0x1A86, 0x7523, "CH340/CH341 (clone)", True, False),
        (0x10C4, 0xEA60, "CP210x (SiLabs)", True, False),
        (0x1234, 0x5678, "generic serial", False, False),
        (None, None, "generic serial", False, False),
    ],
)
def test_classify_port_table(vid, pid, expected_name, mcu, arduino):
    name, is_mcu, is_arduino = sp.classify_port(vid, pid)
    assert name == expected_name
    assert is_mcu is mcu
    assert is_arduino is arduino


def test_classify_port_description_fallback():
    # An unknown VID whose description mentions Arduino is flagged best-effort.
    name, is_mcu, is_arduino = sp.classify_port(0x9999, 0x0001, "My Arduino clone")
    assert "Arduino" in name
    assert is_mcu and is_arduino


# ------------------------------------------------------------ list_serial_ports


def test_list_serial_ports_maps_and_sorts(monkeypatch):
    ports = [
        _port("/dev/ttyACM0", vid=0x2341, pid=0x0070, sn="ABC", manuf="Arduino"),
        _port("/dev/ttyS0"),
    ]
    monkeypatch.setattr(sp, "comports", lambda: list(reversed(ports)))
    result = sp.list_serial_ports()
    assert [p.device for p in result] == ["/dev/ttyACM0", "/dev/ttyS0"]  # sorted
    arduino = result[0]
    assert arduino.board_name == "Arduino Nano ESP32"
    assert arduino.likely_arduino is True
    assert arduino.vid_pid == "2341:0070"
    assert result[1].likely_microcontroller is False


def test_list_serial_ports_survives_comports_raising(monkeypatch):
    def boom():
        raise OSError("enumeration exploded")

    monkeypatch.setattr(sp, "comports", boom)
    assert sp.list_serial_ports() == []


def test_list_serial_ports_without_pyserial(monkeypatch):
    monkeypatch.setattr(sp, "comports", None)
    assert sp.list_serial_ports() == []


# ---------------------------------------------------------------- resolve_device


def test_resolve_device_explicit_path_passthrough(monkeypatch):
    # A concrete path is returned unchanged and never enumerates.
    monkeypatch.setattr(sp, "list_serial_ports", lambda: pytest.fail("enumerated"))
    device, _reason = sp.resolve_device("/dev/ttyUSB7")
    assert device == "/dev/ttyUSB7"


def _sp(device, arduino=True):
    return sp.SerialPort(
        device=device,
        description="",
        manufacturer=None,
        product=None,
        vid=0x2341,
        pid=0x0070,
        serial_number="SN",
        hwid="",
        board_name="Arduino Nano ESP32",
        likely_microcontroller=True,
        likely_arduino=arduino,
    )


def test_resolve_device_auto_single(monkeypatch):
    monkeypatch.setattr(sp, "list_serial_ports", lambda: [_sp("/dev/ttyACM0")])
    device, reason = sp.resolve_device("auto")
    assert device == "/dev/ttyACM0"
    assert "auto-selected" in reason


def test_resolve_device_auto_none(monkeypatch):
    monkeypatch.setattr(sp, "list_serial_ports", lambda: [])
    device, reason = sp.resolve_device("auto")
    assert device is None
    assert "no microcontroller-class" in reason


def test_resolve_device_auto_ambiguous(monkeypatch):
    monkeypatch.setattr(
        sp, "list_serial_ports", lambda: [_sp("/dev/ttyACM0"), _sp("/dev/ttyACM1")]
    )
    device, reason = sp.resolve_device("auto")
    assert device is None
    assert "set [plugins.options].device" in reason


# ---------------------------------------------------------------- probe_identity


class _FakeSerial:
    def __init__(self, *a, reply=b"TRIGGERBOX 1\n", **kw):
        self._reply = reply

    def reset_input_buffer(self):
        pass

    def write(self, data):
        pass

    def flush(self):
        pass

    def readline(self):
        return self._reply

    def close(self):
        pass


def test_probe_identity_reads_banner(monkeypatch):
    monkeypatch.setattr(sp.serial, "Serial", lambda *a, **k: _FakeSerial())
    ident = sp.probe_identity("/dev/ttyACM0")
    assert ident.banner == "TRIGGERBOX 1"
    assert ident.busy is False
    assert ident.error is None


def test_probe_identity_busy_on_open_error(monkeypatch):
    def raise_busy(*a, **k):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(sp.serial, "Serial", raise_busy)
    ident = sp.probe_identity("/dev/ttyACM0")
    assert ident.busy is True
    assert ident.banner is None


def test_probe_identity_no_reply(monkeypatch):
    monkeypatch.setattr(
        sp.serial, "Serial", lambda *a, **k: _FakeSerial(reply=b"")
    )
    ident = sp.probe_identity("/dev/ttyACM0")
    assert ident.banner is None
    assert ident.busy is False


# --------------------------------------------------- format / udev / explain


def test_format_candidates_prefers_microcontrollers():
    ports = [_sp("/dev/ttyACM0"), sp.SerialPort(
        "/dev/ttyS0", "", None, None, None, None, None, "", "generic serial", False, False
    )]
    text = sp.format_candidates(ports)
    assert "/dev/ttyACM0" in text
    assert "/dev/ttyS0" not in text  # generic port not offered as a candidate


def test_format_candidates_no_microcontroller_ports():
    generic = sp.SerialPort(
        "/dev/ttyS0", "", None, None, None, None, None, "", "generic serial", False, False
    )
    assert "generic port(s) present" in sp.format_candidates([generic])
    assert sp.format_candidates([]) == "no serial ports detected"


def test_udev_rule_for():
    rule = sp.udev_rule_for(_sp("/dev/ttyACM0"), symlink="triggerbox")
    assert 'ATTRS{idVendor}=="2341"' in rule
    assert 'ATTRS{idProduct}=="0070"' in rule
    assert 'ATTRS{serial}=="SN"' in rule
    assert 'SYMLINK+="triggerbox"' in rule


def test_explain_open_failure_device_absent(monkeypatch):
    monkeypatch.setattr(sp, "list_serial_ports", lambda: [_sp("/dev/ttyACM0")])
    msg = sp.explain_open_failure("/dev/ttyACM9", OSError("nope"))
    assert "failed to open /dev/ttyACM9: nope" in msg
    assert "not among the connected serial ports" in msg
    assert "/dev/ttyACM0" in msg


def test_explain_open_failure_device_present(monkeypatch):
    monkeypatch.setattr(sp, "list_serial_ports", lambda: [_sp("/dev/ttyACM0")])
    # The device exists, so the failure is permissions/busy — no candidate list.
    msg = sp.explain_open_failure("/dev/ttyACM0", OSError("permission denied"))
    assert msg == "failed to open /dev/ttyACM0: permission denied"


# --- USB bus reset recovery (reset_usb_device / wait_for_device) ------------
# These never touch real hardware: reset_usb_device on a non-existent tty finds
# no sysfs entry and returns (False, ...) before opening any usbfs node.


def test_reset_usb_device_non_linux_is_noop(monkeypatch):
    monkeypatch.setattr(sp.sys, "platform", "darwin")
    ok, msg = sp.reset_usb_device("/dev/ttyACM0")
    assert ok is False and "only implemented on Linux" in msg


def test_reset_usb_device_unknown_tty_returns_false(monkeypatch):
    monkeypatch.setattr(sp.sys, "platform", "linux")
    ok, msg = sp.reset_usb_device("/dev/octacam-does-not-exist")
    assert ok is False
    assert "could not locate the backing USB device" in msg


def test_wait_for_device_true_when_present():
    assert sp.wait_for_device("/dev/null", timeout=0.2) is True


def test_wait_for_device_false_when_absent():
    assert sp.wait_for_device("/dev/octacam-does-not-exist", timeout=0.1) is False
