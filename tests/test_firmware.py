"""octacam.firmware — fingerprint, banner parsing, classify, discovery, flash.

None of these tests shell out to a real arduino-cli or touch a real board:
``_run_streaming`` / ``core_installed`` are monkeypatched where a flash is
exercised, and discovery is driven through monkeypatched ``shutil.which``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from octacam import firmware as fw

# ---------------------------------------------------------------------------
# sketch_fingerprint
# ---------------------------------------------------------------------------


def _write_sketch(tmp_path: Path, ino: str = "void setup(){}\n", header: str = '#define X "PLACEHOLDER"\n') -> Path:
    d = tmp_path / "triggerbox"
    d.mkdir(parents=True)
    (d / "triggerbox.ino").write_text(ino)
    (d / "fw_build_info.h").write_text(header)
    (d / "README.md").write_text("# not hashed\n")
    return d


def test_fingerprint_is_stable_and_hex8(tmp_path):
    d = _write_sketch(tmp_path)
    a = fw.sketch_fingerprint(d)
    b = fw.sketch_fingerprint(d)
    assert a == b
    assert len(a) == 8
    assert all(c in "0123456789abcdef" for c in a)


def test_fingerprint_changes_when_source_changes(tmp_path):
    d = _write_sketch(tmp_path)
    before = fw.sketch_fingerprint(d)
    (d / "triggerbox.ino").write_text("void setup(){} // edited\n")
    assert fw.sketch_fingerprint(d) != before


def test_fingerprint_excludes_the_build_header(tmp_path):
    """Rewriting only the generated header must NOT change the hash (else the
    baked-in value would be circular)."""
    d = _write_sketch(tmp_path)
    before = fw.sketch_fingerprint(d)
    (d / "fw_build_info.h").write_text('#define X "a-completely-different-value"\n')
    assert fw.sketch_fingerprint(d) == before


def test_fingerprint_ignores_non_source_files(tmp_path):
    d = _write_sketch(tmp_path)
    before = fw.sketch_fingerprint(d)
    (d / "triggerbox_send.py").write_text("print('host tool')\n")
    (d / "notes.txt").write_text("scratch\n")
    assert fw.sketch_fingerprint(d) == before


def test_fingerprint_normalizes_line_endings(tmp_path):
    d1 = _write_sketch(tmp_path / "a", ino="a\nb\nc\n")
    d2 = _write_sketch(tmp_path / "b", ino="a\r\nb\r\nc\r\n")
    assert fw.sketch_fingerprint(d1) == fw.sketch_fingerprint(d2)


def test_fingerprint_tracks_file_add(tmp_path):
    d = _write_sketch(tmp_path)
    before = fw.sketch_fingerprint(d)
    (d / "extra.h").write_text("#define Y 1\n")
    assert fw.sketch_fingerprint(d) != before


# ---------------------------------------------------------------------------
# parse_banner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "banner,expected",
    [
        ("TRIGGERBOX 2 a1b2c3d4", ("TRIGGERBOX", 2, "a1b2c3d4")),
        ("TRIGGERBOX 2", ("TRIGGERBOX", 2, None)),
        ("triggerbox 2 abcd", ("TRIGGERBOX", 2, "abcd")),  # name upper-cased
        ("OMNIVIEW 1", ("OMNIVIEW", 1, None)),
        ("WEIRD", ("WEIRD", None, None)),
        ("TRIGGERBOX x deadbeef", ("TRIGGERBOX", None, "deadbeef")),  # bad version
        (None, (None, None, None)),
        ("", (None, None, None)),
        ("   ", (None, None, None)),
    ],
)
def test_parse_banner(banner, expected):
    assert fw.parse_banner(banner) == expected


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


@pytest.fixture
def spec():
    return fw.FirmwareSpec(
        name="triggerbox",
        sketch_dir=Path("/does/not/matter/triggerbox"),
        fqbn="arduino:esp32:nano_nora",
        banner_prefix="TRIGGERBOX",
        protocol_version=2,
        legacy_prefixes=("OMNIVIEW",),
    )


def test_classify_current(spec):
    c = fw.classify(spec, "TRIGGERBOX 2 abc12345", "abc12345")
    assert c.state is fw.FirmwareState.CURRENT
    assert not c.needs_flash
    assert not c.safe_to_auto_flash


def test_classify_outdated_by_build(spec):
    c = fw.classify(spec, "TRIGGERBOX 2 oldbuild", "abc12345")
    assert c.state is fw.FirmwareState.OUTDATED
    assert c.needs_flash and c.safe_to_auto_flash


def test_classify_outdated_when_board_has_no_build(spec):
    """A board flashed before fingerprinting reports just 'TRIGGERBOX 2'."""
    c = fw.classify(spec, "TRIGGERBOX 2", "abc12345")
    assert c.state is fw.FirmwareState.OUTDATED
    assert c.needs_flash and c.safe_to_auto_flash


def test_classify_wrong_version(spec):
    c = fw.classify(spec, "TRIGGERBOX 1 abc12345", "abc12345")
    assert c.state is fw.FirmwareState.WRONG_VERSION
    assert c.needs_flash and c.safe_to_auto_flash


def test_classify_legacy_is_wrong_board_but_safe(spec):
    c = fw.classify(spec, "OMNIVIEW 1", "abc12345")
    assert c.state is fw.FirmwareState.WRONG_BOARD
    assert c.is_legacy
    assert c.needs_flash and c.safe_to_auto_flash


def test_classify_foreign_board_is_not_auto_flashable(spec):
    c = fw.classify(spec, "SOMETHINGELSE 5", "abc12345")
    assert c.state is fw.FirmwareState.WRONG_BOARD
    assert not c.is_legacy
    assert c.needs_flash and not c.safe_to_auto_flash


def test_classify_unidentified_is_not_auto_flashable(spec):
    c = fw.classify(spec, None, "abc12345")
    assert c.state is fw.FirmwareState.UNIDENTIFIED
    assert c.needs_flash and not c.safe_to_auto_flash


def test_check_to_dict_roundtrip(spec):
    d = fw.classify(spec, "TRIGGERBOX 2", "abc12345").to_dict()
    assert d["state"] == "outdated"
    assert d["needs_flash"] is True
    assert d["needed_build"] == "abc12345"
    assert set(d) >= {"state", "detail", "board_build", "needs_flash", "safe_to_auto_flash"}


# ---------------------------------------------------------------------------
# arduino-cli / sketch discovery
# ---------------------------------------------------------------------------


def test_arduino_cli_env_override_absolute(tmp_path, monkeypatch):
    exe = tmp_path / "arduino-cli"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("OCTACAM_ARDUINO_CLI", str(exe))
    assert fw.arduino_cli_path() == str(exe)


def test_arduino_cli_from_path(monkeypatch):
    monkeypatch.delenv("OCTACAM_ARDUINO_CLI", raising=False)
    monkeypatch.setattr(fw.shutil, "which", lambda name: "/usr/bin/arduino-cli" if name == "arduino-cli" else None)
    assert fw.arduino_cli_path() == "/usr/bin/arduino-cli"


def test_arduino_cli_absent(monkeypatch):
    monkeypatch.delenv("OCTACAM_ARDUINO_CLI", raising=False)
    monkeypatch.setattr(fw.shutil, "which", lambda name: None)
    monkeypatch.setattr(fw.glob, "glob", lambda pat: [])
    assert fw.arduino_cli_path() is None


def test_resolve_sketch_dir_finds_triggerbox():
    d = fw.resolve_sketch_dir("triggerbox")
    assert d is not None
    assert (d / "triggerbox.ino").is_file()


def test_resolve_sketch_dir_env_override(tmp_path, monkeypatch):
    (tmp_path / "widget").mkdir()
    (tmp_path / "widget" / "widget.ino").write_text("void setup(){}\n")
    monkeypatch.setenv("OCTACAM_ARDUINO_DIR", str(tmp_path))
    assert fw.resolve_sketch_dir("widget") == tmp_path / "widget"


def test_resolve_sketch_dir_unknown_is_none():
    assert fw.resolve_sketch_dir("no-such-sketch-xyz") is None


# ---------------------------------------------------------------------------
# core_installed / preflight
# ---------------------------------------------------------------------------


def test_core_installed_true(monkeypatch):
    class R:
        returncode = 0
        stdout = "ID            Installed\narduino:esp32 2.0.18\n"
    monkeypatch.setattr(fw.subprocess, "run", lambda *a, **k: R())
    assert fw.core_installed("arduino-cli", "arduino:esp32:nano_nora") is True


def test_core_installed_false(monkeypatch):
    class R:
        returncode = 0
        stdout = "ID          Installed\narduino:avr 1.8.6\n"
    monkeypatch.setattr(fw.subprocess, "run", lambda *a, **k: R())
    assert fw.core_installed("arduino-cli", "arduino:esp32:nano_nora") is False


def test_core_installed_none_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(fw.subprocess, "run", boom)
    assert fw.core_installed("arduino-cli", "arduino:esp32:nano_nora") is None


def test_preflight_no_cli(spec):
    ok, msg = fw.preflight(spec, None)
    assert not ok and "arduino-cli" in msg


def test_preflight_no_sketch(monkeypatch):
    spec = fw.FirmwareSpec(
        name="x", sketch_dir=Path("/nope/x"), fqbn="arduino:esp32:nano_nora",
        banner_prefix="X", protocol_version=1,
    )
    ok, msg = fw.preflight(spec, "arduino-cli")
    assert not ok and "sketch" in msg.lower()


def test_preflight_core_missing(monkeypatch):
    spec_dir = fw.resolve_sketch_dir("triggerbox")
    spec = fw.FirmwareSpec(
        name="triggerbox", sketch_dir=spec_dir, fqbn="arduino:esp32:nano_nora",
        banner_prefix="TRIGGERBOX", protocol_version=2,
    )
    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: False)
    ok, msg = fw.preflight(spec, "arduino-cli")
    assert not ok and "core install" in msg


def test_preflight_ok(monkeypatch):
    spec_dir = fw.resolve_sketch_dir("triggerbox")
    spec = fw.FirmwareSpec(
        name="triggerbox", sketch_dir=spec_dir, fqbn="arduino:esp32:nano_nora",
        banner_prefix="TRIGGERBOX", protocol_version=2,
    )
    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)
    ok, msg = fw.preflight(spec, "arduino-cli")
    assert ok


# ---------------------------------------------------------------------------
# flash
# ---------------------------------------------------------------------------


@pytest.fixture
def real_spec():
    d = fw.resolve_sketch_dir("triggerbox")
    assert d is not None
    return fw.FirmwareSpec(
        name="triggerbox", sketch_dir=d, fqbn="arduino:esp32:nano_nora",
        banner_prefix="TRIGGERBOX", protocol_version=2,
        build_define="TRIGGERBOX_FW_BUILD", legacy_prefixes=("OMNIVIEW",),
    )


def test_flash_writes_header_and_reports_success(real_spec, monkeypatch):
    captured = {}

    def fake_run(cmd, timeout, on_line):
        sketch = Path(cmd[-1])
        captured["cmd"] = cmd
        captured["header"] = (sketch / "fw_build_info.h").read_text()
        captured["has_ino"] = (sketch / "triggerbox.ino").is_file()
        if on_line:
            on_line("Uploading…")
        return (0, "compiled\nuploaded")

    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)
    monkeypatch.setattr(fw, "_run_streaming", fake_run)
    lines: list[str] = []
    result = fw.flash(real_spec, "/dev/ttyACM0", "beefcafe", cli="/x/arduino-cli", on_line=lines.append)

    assert result.ok
    assert result.build == "beefcafe"
    assert "beefcafe" in captured["header"]  # the fingerprint was baked in
    assert "TRIGGERBOX_FW_BUILD" in captured["header"]
    assert captured["has_ino"]
    assert "--upload" in captured["cmd"] and "/dev/ttyACM0" in captured["cmd"]
    assert any("Uploading" in ln for ln in lines)


def test_flash_reports_failure_from_nonzero_exit(real_spec, monkeypatch):
    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)
    monkeypatch.setattr(fw, "_run_streaming", lambda cmd, timeout, on_line: (1, "error: boom"))
    result = fw.flash(real_spec, "/dev/ttyACM0", "beefcafe", cli="/x/arduino-cli")
    assert not result.ok
    assert "boom" in result.log


def test_flash_short_circuits_on_preflight(real_spec, monkeypatch):
    """No cli → preflight fails and _run_streaming is never called."""
    called = {"run": False}
    monkeypatch.setattr(fw, "_run_streaming", lambda *a, **k: called.__setitem__("run", True) or (0, ""))
    monkeypatch.setattr(fw, "arduino_cli_path", lambda: None)
    result = fw.flash(real_spec, "/dev/ttyACM0", "beefcafe")
    assert not result.ok
    assert not called["run"]


def test_flash_never_raises(real_spec, monkeypatch):
    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)

    def boom(cmd, timeout, on_line):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(fw, "_run_streaming", boom)
    result = fw.flash(real_spec, "/dev/ttyACM0", "beefcafe", cli="/x/arduino-cli")
    assert not result.ok
    assert "kaboom" in result.message


def test_flash_result_to_dict_truncates_log():
    r = fw.FlashResult(True, "ok", log="x" * 20000, build="abc")
    d = r.to_dict(log_tail=100)
    assert len(d["log"]) <= 102  # "…\n" + 100
    assert d["log"].startswith("…")


def test_flash_never_dirties_the_repo_sketch(real_spec, monkeypatch):
    """The build header is written only into the throwaway copy — the committed
    sketch's fw_build_info.h must be byte-for-byte unchanged after a flash."""
    header = real_spec.sketch_dir / real_spec.build_header
    before = header.read_bytes()
    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)
    monkeypatch.setattr(fw, "_run_streaming", lambda cmd, timeout, on_line: (0, "ok"))
    result = fw.flash(real_spec, "/dev/ttyACM0", "beefcafe", cli="/x/arduino-cli")
    assert result.ok
    assert header.read_bytes() == before  # repo tree untouched
    assert "UNBAKED" in before.decode()  # still the committed placeholder


def test_flash_removes_temp_build_dir(real_spec, monkeypatch):
    """The mkdtemp'd build copy must be cleaned up (success and failure)."""
    seen: dict[str, str] = {}

    def fake_run(cmd, timeout, on_line):
        seen["sketch"] = cmd[-1]  # the temp sketch dir passed to arduino-cli
        return (0, "ok")

    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)
    monkeypatch.setattr(fw, "_run_streaming", fake_run)
    fw.flash(real_spec, "/dev/ttyACM0", "beefcafe", cli="/x/arduino-cli")
    sketch = Path(seen["sketch"])
    assert not sketch.exists()  # the copy…
    assert not sketch.parent.exists()  # …and its mkdtemp parent are gone


def test_flash_removes_temp_build_dir_on_failure(real_spec, monkeypatch):
    seen: dict[str, str] = {}

    def boom(cmd, timeout, on_line):
        seen["sketch"] = cmd[-1]
        raise RuntimeError("kaboom")

    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)
    monkeypatch.setattr(fw, "_run_streaming", boom)
    result = fw.flash(real_spec, "/dev/ttyACM0", "beefcafe", cli="/x/arduino-cli")
    assert not result.ok
    assert not Path(seen["sketch"]).parent.exists()  # cleaned up despite the raise


# ---------------------------------------------------------------------------
# FirmwareProvisioner — reopen-failure handling + lock reentrancy
# ---------------------------------------------------------------------------


def _provisioner(spec, *, reopen, close=lambda: None, is_busy=None):
    return fw.FirmwareProvisioner(
        spec,
        resolve_device=lambda: ("/dev/ttyACM0", "ok"),
        reopen=reopen,
        close_link=close,
        wait_for_device=lambda device, timeout=3.0: True,
        is_busy=is_busy,
    )


def test_provisioner_clears_stale_check_when_reopen_fails(real_spec, monkeypatch):
    """A successful upload whose reopen fails must NOT keep reporting the board as
    out of date (the flash landed; we just can't confirm)."""
    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)
    monkeypatch.setattr(fw, "_run_streaming", lambda cmd, timeout, on_line: (0, "uploaded"))
    prov = _provisioner(real_spec, reopen=lambda: "cannot reopen /dev/ttyACM0")
    prov.classify("TRIGGERBOX 2")  # start out of date
    assert prov.check.needs_flash
    result = prov.flash()
    assert result.ok
    assert prov.check is None  # stale "outdated" dropped
    assert "could not be reopened" in result.message


def test_provisioner_refuses_when_busy(real_spec):
    prov = _provisioner(real_spec, reopen=lambda: None, is_busy=lambda: (True, "busy now"))
    result = prov.flash()
    assert not result.ok and result.message == "busy now"


def test_provisioner_port_lock_is_reentrant(real_spec, monkeypatch):
    """flash() holds port_lock across reopen(); reopen (the plugin's _open) takes
    the same lock — it must not self-deadlock."""
    monkeypatch.setattr(fw, "core_installed", lambda cli, fqbn: True)
    monkeypatch.setattr(fw, "_run_streaming", lambda cmd, timeout, on_line: (0, "ok"))
    prov = None

    def reopen():
        # Same-thread re-acquire while flash() holds it (RLock).
        with prov.port_lock:
            prov.classify(f"TRIGGERBOX 2 {prov.needed_build}")
        return None

    prov = _provisioner(real_spec, reopen=reopen)
    result = prov.flash()
    assert result.ok
    assert prov.check.state is fw.FirmwareState.CURRENT
