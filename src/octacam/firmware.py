"""Firmware fingerprinting and flashing for the serial plugins' Arduino boards.

A sketch's identify banner ends with a short hash of its source
(``TRIGGERBOX 2 a1b2c3d4``). :func:`sketch_fingerprint` hashes the sketch on disk
the same way, so :func:`classify` sees any source drift, and :func:`flash` bakes
the hash into ``fw_build_info.h`` in a throwaway copy of the sketch, never in the
repo (a manual build reports the committed placeholder). Discovery and flashing
never raise: a missing arduino-cli or core, or a compile error, is a message.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger("octacam")

# Hashed into the fingerprint and copied into a build; the build header is
# copied but not hashed, since it holds the hash.
SOURCE_EXTS = frozenset({".ino", ".h", ".hpp", ".c", ".cpp", ".cc", ".cxx", ".S"})

_ENV_CLI = "OCTACAM_ARDUINO_CLI"
_ENV_ARDUINO_DIR = "OCTACAM_ARDUINO_DIR"

_DEFAULT_FLASH_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class FirmwareSpec:
    """What a plugin's board should run, and how to flash it. ``sketch_dir``
    holds ``<sketch_dir.name>.ino``, as arduino-cli requires; None without a
    source checkout (a wheel install), where a board is never classified by its
    build or flashed."""

    name: str
    sketch_dir: Path | None
    fqbn: str
    banner_prefix: str
    protocol_version: int
    build_define: str
    build_header: str = "fw_build_info.h"

    @property
    def main_ino(self) -> Path | None:
        if self.sketch_dir is None:
            return None
        return self.sketch_dir / f"{self.sketch_dir.name}.ino"


class FirmwareState(str, Enum):
    """How the firmware on the board relates to the sketch on disk."""

    CURRENT = "current"              # name + version + build all match: up to date
    OUTDATED = "outdated"           # right name + version, different source (drift)
    WRONG_VERSION = "wrong_version"  # right name, wrong protocol version
    WRONG_BOARD = "wrong_board"      # a different firmware banner entirely
    UNIDENTIFIED = "unidentified"    # no banner (blank board? wrong board? wedged link?)


@dataclass(frozen=True)
class FirmwareCheck:
    """How the board's banner compares with the sketch source."""

    state: FirmwareState
    detail: str
    needed_build: str

    @property
    def needs_flash(self) -> bool:
        return self.state is not FirmwareState.CURRENT

    @property
    def safe_to_auto_flash(self) -> bool:
        """Whether a reflash may skip the operator's confirmation: only for this
        board on stale firmware. A blank or foreign board never may, since a
        flash overwrites whatever it runs."""
        return self.state in (FirmwareState.OUTDATED, FirmwareState.WRONG_VERSION)

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "needed_build": self.needed_build,
            "needs_flash": self.needs_flash,
            "safe_to_auto_flash": self.safe_to_auto_flash,
        }


@dataclass
class FlashResult:
    """What :func:`flash` did."""

    ok: bool
    message: str
    log: str = ""
    build: str | None = None

    def to_dict(self, log_tail: int = 8000) -> dict:
        log = self.log
        if log_tail and len(log) > log_tail:
            log = "…\n" + log[-log_tail:]
        return {"ok": self.ok, "message": self.message, "log": log, "build": self.build}


def sketch_fingerprint(sketch_dir: Path, exclude: str = "fw_build_info.h", length: int = 8) -> str:
    """A short hash of a sketch's source files: names and contents, CRLF read as
    LF so checkouts agree, without the build header *exclude*."""
    h = hashlib.sha256()
    files = sorted(
        p
        for p in sketch_dir.iterdir()
        if p.is_file() and p.suffix in SOURCE_EXTS and p.name != exclude
    )
    for p in files:
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()[:length]


def source_build(spec: FirmwareSpec) -> str | None:
    """The fingerprint of *spec*'s sketch on disk: the build a current board
    reports. None without a source checkout or when the sketch cannot be read."""
    if spec.sketch_dir is None:
        return None
    try:
        return sketch_fingerprint(spec.sketch_dir)
    except Exception:
        log.debug("firmware: could not fingerprint %s sketch", spec.name, exc_info=True)
        return None


def parse_banner(banner: str | None) -> tuple[str | None, int | None, str | None]:
    """``"TRIGGERBOX 2 a1b2c3d4"`` -> ``("TRIGGERBOX", 2, "a1b2c3d4")``; a missing
    version or build is None, and the name is upper-cased."""
    if not banner:
        return (None, None, None)
    parts = banner.split()
    if not parts:
        return (None, None, None)
    name = parts[0].upper()
    version: int | None = None
    build: str | None = None
    if len(parts) >= 2:
        try:
            version = int(parts[1])
        except ValueError:
            version = None
    if len(parts) >= 3:
        build = parts[2]
    return (name, version, build)


def classify(spec: FirmwareSpec, banner: str | None, needed_build: str) -> FirmwareCheck:
    """Compare a board's identify *banner* against *spec* and the source hash."""
    name, version, build = parse_banner(banner)
    nv = spec.protocol_version
    prefix = spec.banner_prefix.upper()

    def check(state: FirmwareState, detail: str) -> FirmwareCheck:
        return FirmwareCheck(state, detail, needed_build)

    if name is None:
        return check(
            FirmwareState.UNIDENTIFIED,
            "the board sent no firmware identity — it may be blank (never flashed), "
            "a different board, or its link may be wedged",
        )
    if name == prefix:
        if version != nv:
            return check(
                FirmwareState.WRONG_VERSION,
                f"the board runs protocol v{version} but octacam speaks v{nv}",
            )
        if build != needed_build:
            shown = build or "an older build (no fingerprint)"
            return check(
                FirmwareState.OUTDATED,
                f"the board runs {shown}; the current source is {needed_build}",
            )
        return check(FirmwareState.CURRENT, f"up to date (build {needed_build})")
    return check(
        FirmwareState.WRONG_BOARD,
        f"the board reports {banner!r}, not {spec.banner_prefix} firmware",
    )


def resolve_sketch_dir(sketch_name: str) -> Path | None:
    """The ``arduino/<sketch_name>`` folder of a source checkout
    (``OCTACAM_ARDUINO_DIR`` names the ``arduino`` dir instead), or None: the
    wheel does not ship the sketches, and without one only flashing is lost."""
    candidates: list[Path] = []
    env = os.environ.get(_ENV_ARDUINO_DIR)
    if env:
        candidates.append(Path(env) / sketch_name)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "arduino" / sketch_name)
    for c in candidates:
        if (c / f"{sketch_name}.ino").is_file():
            return c
    return None


def arduino_cli_path() -> str | None:
    """The arduino-cli executable: ``OCTACAM_ARDUINO_CLI`` (a path or a name on
    PATH), else PATH, else the usual install dirs; None if absent."""
    override = os.environ.get(_ENV_CLI)
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        return shutil.which(override)  # may be a bare name on PATH, else None
    found = shutil.which("arduino-cli")
    if found:
        return found
    patterns = ["/opt/arduino-cli*/arduino-cli", "/usr/local/bin/arduino-cli"]
    # Path.home() raises without HOME and a passwd entry (docker --user).
    try:
        home = Path.home()
        patterns += [str(home / "bin" / "arduino-cli"), str(home / ".local" / "bin" / "arduino-cli")]
    except (RuntimeError, OSError):
        pass
    for pat in patterns:
        # Newest version dir first, compared numerically (1.10 > 1.9); an
        # unversioned dir sorts last.
        for m in sorted(
            glob.glob(pat),
            key=lambda p: [int(n) for n in re.findall(r"\d+", p)],
            reverse=True,
        ):
            if os.path.isfile(m) and os.access(m, os.X_OK):
                return m
    return None


def core_installed(cli: str, fqbn: str, timeout: float = 20.0) -> bool | None:
    """Whether *fqbn*'s core is installed; None when arduino-cli cannot tell, so
    an inconclusive check never blocks a flash."""
    core_id = ":".join(fqbn.split(":")[:2])  # arduino:esp32:nano_nora -> arduino:esp32
    try:
        proc = subprocess.run(
            [cli, "core", "list"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.strip().startswith(core_id):
            return True
    return False


def preflight(spec: FirmwareSpec, cli: str | None) -> tuple[bool, str]:
    """Check the toolchain can flash *spec*; ``(ok, actionable_message)``."""
    if cli is None:
        return False, (
            "arduino-cli was not found. Install it (https://arduino.github.io/"
            "arduino-cli/latest/installation/) or set the OCTACAM_ARDUINO_CLI "
            "environment variable to its path."
        )
    ino = spec.main_ino
    if ino is None or not ino.is_file():
        expected = f" (expected {ino})" if ino else ""
        return False, (
            f"the {spec.name} sketch source was not found{expected}; auto-flash "
            "needs a source checkout. Set OCTACAM_ARDUINO_DIR, or flash manually "
            "with arduino-cli."
        )
    core = core_installed(cli, spec.fqbn)
    core_id = ":".join(spec.fqbn.split(":")[:2])
    if core is False:
        return False, (
            f"the {core_id} core is not installed. Install it with: "
            f"arduino-cli core install {core_id}"
        )
    return True, "ok"


def _render_build_header(spec: FirmwareSpec, build: str) -> str:
    return (
        "// Auto-generated by octacam firmware provisioning — do not edit.\n"
        "// Overwrites the committed placeholder so the identify banner reports the\n"
        "// exact source octacam built and uploaded.\n"
        "#pragma once\n"
        f'#define {spec.build_define} "{build}"\n'
    )


def _run_streaming(
    cmd: list[str], timeout: float, on_line: Callable[[str], None] | None
) -> tuple[int, str]:
    """Run *cmd*, streaming each line of its output (stderr merged) to
    *on_line*: ``(returncode, log)``. A timeout kills it and is noted in the log."""
    lines: list[str] = []
    # A process group of its own, so a timeout also kills the compiler and the
    # uploader arduino-cli forked; a surviving uploader holds the serial port.
    posix = os.name == "posix"
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=posix,
        )
    except OSError as e:
        return 127, f"could not launch {cmd[0]}: {e}"

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    log.debug("firmware: flash progress callback error", exc_info=True)

    def kill_tree() -> None:
        if posix:
            try:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                return
            except (OSError, ProcessLookupError):
                pass
        proc.kill()

    t = threading.Thread(target=reader, daemon=True, name="arduino-cli-reader")
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        lines.append(f"[octacam] arduino-cli timed out after {timeout:.0f}s; killed it")
    t.join(timeout=2.0)
    return (proc.returncode if proc.returncode is not None else -1), "\n".join(lines)


def flash(
    spec: FirmwareSpec,
    port: str,
    needed_build: str,
    *,
    cli: str | None = None,
    timeout: float = _DEFAULT_FLASH_TIMEOUT_S,
    on_line: Callable[[str], None] | None = None,
) -> FlashResult:
    """Build a temp copy of *spec*'s sketch with *needed_build* baked in and
    upload it to *port*. Never raises. The caller must not hold the port open:
    arduino-cli resets the board to upload."""
    cli = cli or arduino_cli_path()
    ok, msg = preflight(spec, cli)
    if not ok:
        return FlashResult(False, msg)
    assert cli is not None and spec.sketch_dir is not None  # preflight guarantees both

    try:
        tmp = tempfile.mkdtemp(prefix="octacam-fw-")
    except OSError as e:
        return FlashResult(False, f"could not create a temp build dir: {e}")
    try:
        build_sketch = Path(tmp) / spec.sketch_dir.name
        build_sketch.mkdir()
        for p in spec.sketch_dir.iterdir():
            if p.is_file() and p.suffix in SOURCE_EXTS:
                shutil.copy2(p, build_sketch / p.name)
        (build_sketch / spec.build_header).write_text(
            _render_build_header(spec, needed_build)
        )
        cmd = [
            cli, "compile",
            "--fqbn", spec.fqbn,
            "--upload", "--port", port,
            str(build_sketch),
        ]
        if on_line is not None:
            on_line(f"$ {' '.join(cmd)}")
        code, out = _run_streaming(cmd, timeout, on_line)
        if code == 0:
            return FlashResult(
                True,
                f"uploaded {spec.name} firmware (build {needed_build}) to {port}",
                out,
                build=needed_build,
            )
        return FlashResult(
            False,
            f"arduino-cli exited {code} while flashing {spec.name} to {port}; "
            "see the log for the compiler/uploader output",
            out,
        )
    except Exception as e:  # never let a flash take down the caller
        log.debug("firmware: flash raised", exc_info=True)
        return FlashResult(False, f"flashing {spec.name} failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Boards still driven: a drifted source (OUTDATED) or an unread banner
# (UNIDENTIFIED: a slow link, an old firmware) speaks the same protocol.
_ARMABLE = frozenset({FirmwareState.CURRENT, FirmwareState.OUTDATED, FirmwareState.UNIDENTIFIED})


def arm_compatible(check: FirmwareCheck | None) -> bool:
    """Whether a classified board can be driven (its protocol is understood)."""
    return check is None or check.state in _ARMABLE


class FirmwareProvisioner:
    """A serial plugin's firmware check and flash, around the plugin's own link.

    ``reopen`` must reopen the link and :meth:`classify` the new banner;
    ``is_busy`` refuses a flash while the board is in use. :meth:`flash` holds
    the re-entrant ``port_lock`` across close -> upload -> reopen, and the
    plugin's own open and reconnect take it too, so nothing seizes the port
    mid-upload.
    """

    def __init__(
        self,
        spec: FirmwareSpec,
        *,
        resolve_device: Callable[[], tuple[str | None, str]],
        reopen: Callable[[], str | None],
        close_link: Callable[[], None],
        wait_for_device: Callable[[str, float], object],
        is_busy: Callable[[], tuple[bool, str]] | None = None,
    ):
        self.spec = spec
        self.needed_build = source_build(spec)
        self.check: FirmwareCheck | None = None
        self.port_lock = threading.RLock()
        self._resolve_device = resolve_device
        self._reopen = reopen
        self._close_link = close_link
        self._wait_for_device = wait_for_device
        self._is_busy = is_busy

    def classify(self, banner: str | None) -> FirmwareCheck | None:
        """Classify *banner* against the source; None when no source is available."""
        if self.needed_build is None:
            self.check = None
        else:
            self.check = classify(self.spec, banner, self.needed_build)
        return self.check

    @property
    def can_flash(self) -> bool:
        return self.needed_build is not None and arduino_cli_path() is not None

    def provisioning(
        self, *, plugin_name: str, device: str, firmware: str | None,
        firmware_ok: bool, extra: dict | None = None,
    ) -> dict:
        """The firmware picture for ``octacam flash`` and the GUI."""
        out: dict = {
            "plugin": plugin_name,
            "device": device,
            "firmware": firmware,
            "firmware_ok": firmware_ok,
            "needed_build": self.needed_build,
            "sketch_found": self.spec.sketch_dir is not None,
            "cli_available": arduino_cli_path() is not None,
            "can_flash": self.can_flash,
        }
        if self.check is not None:
            out.update(self.check.to_dict())
        else:
            out["state"] = None
            if self.needed_build is not None:
                out["detail"] = "the board has not been probed"
            else:
                why = "was not found" if self.spec.sketch_dir is None else "could not be read"
                out["detail"] = (
                    f"the sketch source {why}, so the board's build can't be compared "
                    "or flashed"
                    if firmware_ok
                    else f"the board does not run {self.spec.banner_prefix} "
                    f"v{self.spec.protocol_version} firmware (arming is disabled); the "
                    f"sketch source {why}, so it can't be flashed"
                )
            out["needs_flash"] = False
            out["safe_to_auto_flash"] = False
        if extra:
            out.update(extra)
        return out

    def flash(self, *, on_line: Callable[[str], None] | None = None) -> FlashResult:
        """Upload the sketch, then reopen and re-verify. Never raises.

        Refused while busy or without the source. An upload whose reopen fails
        clears the stale check, so the board does not still read as outdated."""
        if self._is_busy is not None:
            busy, why = self._is_busy()
            if busy:
                return FlashResult(False, why)
        if self.needed_build is None:
            return FlashResult(
                False,
                "the sketch source was not found; auto-flash is unavailable (flash "
                "manually with arduino-cli, or set OCTACAM_ARDUINO_DIR)",
            )
        with self.port_lock:
            device, reason = self._resolve_device()
            if device is None:
                return FlashResult(False, reason)
            self._close_link()
            log.info("firmware: flashing %s (build %s) to %s …",
                     self.spec.name, self.needed_build, device)
            result = flash(self.spec, device, self.needed_build,
                           cli=arduino_cli_path(), on_line=on_line)
            log.log(logging.INFO if result.ok else logging.ERROR,
                    "firmware: %s", result.message)
            try:  # the board reboots after an upload
                self._wait_for_device(device, 8.0)
            except Exception:
                log.debug("firmware: wait_for_device raised", exc_info=True)
            reopen_err = self._reopen()
            if reopen_err is not None:
                log.warning("firmware: could not reopen %s after flashing: %s",
                            device, reopen_err)
                if result.ok:
                    self.check = None
                    result = FlashResult(
                        True,
                        f"{result.message} — but the port could not be reopened to "
                        "confirm; replug or power-cycle the board",
                        result.log, result.build,
                    )
            return result
