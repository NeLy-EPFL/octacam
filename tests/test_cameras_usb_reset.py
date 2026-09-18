"""Unit tests for octacam.cameras._usb_reset (camera USB bus-reset recovery).

Pure Python, no hardware: sysfs is faked with a tmp_path tree and the ioctl
primitive is monkeypatched, mirroring tests/test_serial_ports.py's approach
for the tty-side reset_usb_device."""

from octacam.cameras import _usb_reset as ur


def _write(path, name, content):
    (path / name).write_text(content)


def test_find_usb_bus_dev_by_serial_matches(tmp_path, monkeypatch):
    sysfs = tmp_path / "devices"
    sysfs.mkdir()
    dev_a = sysfs / "2-5"
    dev_a.mkdir()
    _write(dev_a, "serial", "40002336\n")
    _write(dev_a, "busnum", "2\n")
    _write(dev_a, "devnum", "5\n")
    dev_b = sysfs / "2-6"
    dev_b.mkdir()
    _write(dev_b, "serial", "40002339\n")
    _write(dev_b, "busnum", "2\n")
    _write(dev_b, "devnum", "6\n")
    monkeypatch.setattr(ur, "_SYSFS_USB_DEVICES", str(sysfs))

    assert ur.find_usb_bus_dev_by_serial("40002336") == (2, 5)
    assert ur.find_usb_bus_dev_by_serial("40002339") == (2, 6)


def test_find_usb_bus_dev_by_serial_no_match(tmp_path, monkeypatch):
    sysfs = tmp_path / "devices"
    sysfs.mkdir()
    dev_a = sysfs / "2-5"
    dev_a.mkdir()
    _write(dev_a, "serial", "40002336\n")
    _write(dev_a, "busnum", "2\n")
    _write(dev_a, "devnum", "5\n")
    monkeypatch.setattr(ur, "_SYSFS_USB_DEVICES", str(sysfs))

    assert ur.find_usb_bus_dev_by_serial("does-not-exist") is None


def test_find_usb_bus_dev_by_serial_tolerates_devices_without_serial(tmp_path, monkeypatch):
    # A hub or non-vision device may have no `serial` file at all (e.g. some
    # root hubs); it must be skipped, not raise.
    sysfs = tmp_path / "devices"
    sysfs.mkdir()
    (sysfs / "1-0:1.0").mkdir()  # no serial/busnum/devnum files
    monkeypatch.setattr(ur, "_SYSFS_USB_DEVICES", str(sysfs))

    assert ur.find_usb_bus_dev_by_serial("40002336") is None


def test_find_usb_bus_dev_by_serial_missing_sysfs_root(monkeypatch):
    monkeypatch.setattr(ur, "_SYSFS_USB_DEVICES", "/octacam-does-not-exist")
    assert ur.find_usb_bus_dev_by_serial("40002336") is None


def test_reset_camera_usb_link_non_linux_is_noop(monkeypatch):
    monkeypatch.setattr(ur.sys, "platform", "darwin")
    ok, msg = ur.reset_camera_usb_link("40002336")
    assert ok is False and "only implemented on Linux" in msg


def test_reset_camera_usb_link_not_found_returns_false(monkeypatch):
    monkeypatch.setattr(ur.sys, "platform", "linux")
    monkeypatch.setattr(ur, "find_usb_bus_dev_by_serial", lambda serial: None)
    ok, msg = ur.reset_camera_usb_link("40002336")
    assert ok is False
    assert "could not locate it in sysfs" in msg


def test_reset_camera_usb_link_delegates_to_reset_usb_node(monkeypatch):
    monkeypatch.setattr(ur.sys, "platform", "linux")
    monkeypatch.setattr(ur, "find_usb_bus_dev_by_serial", lambda serial: (8, 3))
    calls = []

    def fake_reset(bus, dev, context):
        calls.append((bus, dev, context))
        return True, "ok"

    monkeypatch.setattr(ur, "reset_usb_node", fake_reset)
    ok, msg = ur.reset_camera_usb_link("40012161")
    assert ok is True and msg == "ok"
    assert calls == [(8, 3, "camera 40012161")]


def test_wait_for_serial_true_when_already_present():
    assert ur.wait_for_serial(lambda: ["A", "B"], "B", timeout=0.2) is True


def test_wait_for_serial_false_when_absent():
    assert ur.wait_for_serial(lambda: ["A"], "B", timeout=0.1) is False
