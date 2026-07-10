"""Arduino firmware provisioning for octacam's serial plugins.

Some octacam plugins need a specific sketch running on their Arduino board — the
``triggerbox`` plugin, for instance, speaks a wire protocol that only its own
firmware understands. Historically the operator had to flash that sketch by hand
(``arduino-cli compile --upload …``). This module lets octacam do it: it can tell
whether the *exact* current sketch is already on the board and, if not, build and
upload it.

The mechanism is a **build fingerprint**. Each sketch's identify banner carries a
short hash of its source (``TRIGGERBOX 2 a1b2c3d4``). octacam recomputes the same
hash from the sketch on disk (:func:`sketch_fingerprint`) and compares
(:func:`classify`) — so it detects any source drift, not just a bumped protocol
version. The hash is baked into a generated ``fw_build_info.h`` *in a throwaway
copy* of the sketch at flash time, so the repo tree is never dirtied and a manual
``arduino-cli compile`` of the committed sketch still works (it reports the
committed placeholder, which octacam sees as "not the managed build").

Design notes mirror :mod:`octacam.serial_ports`: everything degrades gracefully.
Discovery and :func:`flash` never raise — they return structured results, so a
missing ``arduino-cli``, an absent core, or a compile error surfaces as an
actionable message rather than a traceback. This module is generic: it knows
nothing about any specific plugin. A plugin supplies a :class:`FirmwareSpec` built
from its own constants.
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

# Source file extensions that go into a sketch's fingerprint and its temp build
# copy. The generated build-info header is excluded from the hash (it holds the
# hash — including it would be circular) but IS written into the build copy.
SOURCE_EXTS = frozenset({".ino", ".h", ".hpp", ".c", ".cpp", ".cc", ".cxx", ".S"})

# Environment overrides (documented in the plugin READMEs):
#   OCTACAM_ARDUINO_CLI  path to (or name of) the arduino-cli binary
#   OCTACAM_ARDUINO_DIR  dir holding the sketch folders (repo's arduino/)
_ENV_CLI = "OCTACAM_ARDUINO_CLI"
_ENV_ARDUINO_DIR = "OCTACAM_ARDUINO_DIR"

_DEFAULT_FLASH_TIMEOUT_S = 300.0


# ---------------------------------------------------------------------------
#  Spec + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FirmwareSpec:
    """What a plugin's board should be running, and how to (re)flash it.

    ``sketch_dir`` must contain ``<sketch_dir.name>.ino`` (arduino-cli requires
    the main sketch file to match the folder name). ``banner_prefix`` /
    ``protocol_version`` are matched against the board's identify banner;
    ``legacy_prefixes`` are known predecessor names (e.g. ``omniview`` → the same
    board, just old firmware) that are safe to auto-upgrade.
    """

    name: str
    sketch_dir: Path
    fqbn: str
    banner_prefix: str
    protocol_version: int
    build_header: str = "fw_build_info.h"
    build_define: str = "TRIGGERBOX_FW_BUILD"
    legacy_prefixes: tuple[str, ...] = ()

    @property
    def main_ino(self) -> Path:
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
    """Verdict from :func:`classify` — the board's firmware vs. the source."""

    state: FirmwareState
    detail: str
    board_name: str | None
    board_version: int | None
    board_build: str | None
    needed_build: str
    needed_version: int
    is_legacy: bool = False

    @property
    def needs_flash(self) -> bool:
        return self.state is not FirmwareState.CURRENT

    @property
    def safe_to_auto_flash(self) -> bool:
        """Whether a headless/opt-in path may reflash WITHOUT a human confirming.

        True only when the board is unambiguously *this* board running stale
        firmware (right name, or a known predecessor). A blank/unidentified board
        or an unknown foreign banner is never auto-flashed — the operator must
        confirm it really is the plugin's board first, since flashing overwrites
        whatever is there."""
        if self.state in (FirmwareState.OUTDATED, FirmwareState.WRONG_VERSION):
            return True
        return self.state is FirmwareState.WRONG_BOARD and self.is_legacy

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "board_name": self.board_name,
            "board_version": self.board_version,
            "board_build": self.board_build,
            "needed_build": self.needed_build,
            "needed_version": self.needed_version,
            "is_legacy": self.is_legacy,
            "needs_flash": self.needs_flash,
            "safe_to_auto_flash": self.safe_to_auto_flash,
        }


@dataclass
class FlashResult:
    """Outcome of a :func:`flash` attempt (never raised — always returned)."""

    ok: bool
    message: str
    log: str = ""
    build: str | None = None

    def to_dict(self, log_tail: int = 8000) -> dict:
        log = self.log
        if log_tail and len(log) > log_tail:
            log = "…\n" + log[-log_tail:]
        return {"ok": self.ok, "message": self.message, "log": log, "build": self.build}


# ---------------------------------------------------------------------------
#  Fingerprint + banner parsing + classification
# ---------------------------------------------------------------------------


def sketch_fingerprint(sketch_dir: Path, exclude: str = "fw_build_info.h", length: int = 8) -> str:
    """A short, stable hash of a sketch's compiled source.

    Hashes every :data:`SOURCE_EXTS` file in *sketch_dir* (sorted by name, line
    endings normalised so CRLF/LF checkouts agree), tagging each with its name so
    a rename/add/remove changes the hash. The generated build header (*exclude*)
    is skipped — it carries this very value, so hashing it would be circular. The
    result is the value baked into that header and reported in the identify
    banner."""
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


def parse_banner(banner: str | None) -> tuple[str | None, int | None, str | None]:
    """Split an identify banner into ``(name, version, build)``.

    ``"TRIGGERBOX 2 a1b2c3d4"`` → ``("TRIGGERBOX", 2, "a1b2c3d4")``. Tolerates the
    older two-field ``"TRIGGERBOX 2"`` (build → None) and a bare name. The name is
    upper-cased for case-insensitive comparison; the build is kept verbatim."""
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

    def check(state: FirmwareState, detail: str, is_legacy: bool = False) -> FirmwareCheck:
        return FirmwareCheck(state, detail, name, version, build, needed_build, nv, is_legacy)

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
    is_legacy = name in {p.upper() for p in spec.legacy_prefixes}
    tail = " (a known predecessor)" if is_legacy else ""
    return check(
        FirmwareState.WRONG_BOARD,
        f"the board reports {banner!r}, not {spec.banner_prefix} firmware{tail}",
        is_legacy=is_legacy,
    )


# ---------------------------------------------------------------------------
#  Sketch + toolchain discovery
# ---------------------------------------------------------------------------


def resolve_sketch_dir(sketch_name: str) -> Path | None:
    """Locate the ``arduino/<sketch_name>`` folder for a source/editable install.

    octacam's Arduino sketches live at the repo root (``arduino/<name>/``) and are
    not packaged into the wheel, so this walks up from this module to find them.
    ``OCTACAM_ARDUINO_DIR`` overrides the search (point it at the ``arduino`` dir).
    Returns ``None`` when the sketch can't be found — flashing is then
    unavailable, but firmware *detection* still works from the banner alone."""
    candidates: list[Path] = []
    env = os.environ.get(_ENV_ARDUINO_DIR)
    if env:
        candidates.append(Path(env) / sketch_name)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "arduino" / sketch_name)
    # Packaged fallback, if a future build ever bundles the sketch next to the pkg.
    candidates.append(here.parent / "arduino" / sketch_name)
    for c in candidates:
        if (c / f"{sketch_name}.ino").is_file():
            return c
    return None


def arduino_cli_path() -> str | None:
    """Find the ``arduino-cli`` executable, or ``None``.

    Honours ``OCTACAM_ARDUINO_CLI`` (a full path or a name on PATH), then PATH,
    then a few common install locations (the official installer drops it under
    ``/opt/arduino-cli*/`` or ``~/bin``)."""
    override = os.environ.get(_ENV_CLI)
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        return shutil.which(override)  # may be a bare name on PATH, else None
    found = shutil.which("arduino-cli")
    if found:
        return found
    patterns = ["/opt/arduino-cli*/arduino-cli", "/usr/local/bin/arduino-cli"]
    # Path.home() raises (not returns) when HOME is unset and the UID has no passwd
    # entry — e.g. `docker run --user <unmapped>`. This must never break the
    # never-raise contract, so the home-based candidates are best-effort.
    try:
        home = Path.home()
        patterns += [str(home / "bin" / "arduino-cli"), str(home / ".local" / "bin" / "arduino-cli")]
    except (RuntimeError, OSError):
        pass
    for pat in patterns:
        # Prefer the highest version dir. A numeric key orders 1.10.0 ahead of
        # 1.9.0 (a plain string sort would pick the older 1.9.0); an unversioned
        # /opt/arduino-cli has an empty key and so sorts last under reverse=True.
        for m in sorted(
            glob.glob(pat),
            key=lambda p: [int(n) for n in re.findall(r"\d+", p)],
            reverse=True,
        ):
            if os.path.isfile(m) and os.access(m, os.X_OK):
                return m
    return None


def core_installed(cli: str, fqbn: str, timeout: float = 20.0) -> bool | None:
    """Whether the core for *fqbn* (e.g. ``arduino:esp32``) is installed.

    Best-effort: ``None`` when it can't be determined (arduino-cli missing/erroring)
    so callers don't block a flash on an inconclusive check."""
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
    if spec.sketch_dir is None or not spec.main_ino.is_file():
        return False, (
            f"the {spec.name} sketch source was not found "
            f"(expected {spec.main_ino}); auto-flash needs a source checkout. Set "
            "OCTACAM_ARDUINO_DIR, or flash manually with arduino-cli."
        )
    core = core_installed(cli, spec.fqbn)
    core_id = ":".join(spec.fqbn.split(":")[:2])
    if core is False:
        return False, (
            f"the {core_id} core is not installed. Install it with: "
            f"arduino-cli core install {core_id}"
        )
    return True, "ok"


# ---------------------------------------------------------------------------
#  Flash
# ---------------------------------------------------------------------------


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
    """Run *cmd*, streaming each output line to *on_line*; ``(returncode, log)``.

    stderr is merged into stdout so the log reads in order. On timeout the process
    is killed and the timeout is noted in the log (returncode is then nonzero)."""
    lines: list[str] = []
    # Run arduino-cli in its own process group so a timeout can kill the whole
    # tree — arduino-cli forks the compiler and the uploader (dfu-util/esptool),
    # and just killing the parent would leave a child holding the serial port.
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
    """Compile *spec*'s sketch with *needed_build* baked in and upload to *port*.

    Copies the sketch's source files to a throwaway dir, writes the build header
    with the fingerprint, then runs ``arduino-cli compile --upload``. The repo
    tree is never touched. **The caller must not hold the serial port open** —
    arduino-cli resets the board (1200-baud touch / DFU) to upload. Never raises;
    returns a :class:`FlashResult`."""
    cli = cli or arduino_cli_path()
    ok, msg = preflight(spec, cli)
    if not ok:
        return FlashResult(False, msg)
    assert cli is not None  # preflight guarantees it

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
        # Overwrite the copied placeholder header with the real fingerprint.
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


# A wire protocol version / firmware name is compatible enough to *use* (arm/command)
# in these states — the exact source may have drifted (OUTDATED) or be unreadable
# (UNIDENTIFIED, e.g. a slow link or a board with no identify command), but the host
# can still talk to it. WRONG_VERSION / WRONG_BOARD are refused.
_ARMABLE = frozenset({FirmwareState.CURRENT, FirmwareState.OUTDATED, FirmwareState.UNIDENTIFIED})


def arm_compatible(check: FirmwareCheck | None) -> bool:
    """Whether a classified board can be driven (its protocol is understood)."""
    return check is None or check.state in _ARMABLE


# ---------------------------------------------------------------------------
#  FirmwareProvisioner — shared detect + flash lifecycle for serial plugins
# ---------------------------------------------------------------------------


class FirmwareProvisioner:
    """Owns a serial plugin's firmware detection + flash lifecycle.

    A plugin composes one of these and hands it the plugin-specific bits (each
    plugin owns its own serial link and (re)open logic) as callbacks:

    * ``resolve_device() -> (device|None, reason)`` — the concrete port to flash.
    * ``reopen() -> str|None`` — reopen the link and re-read the identity (usually
      the plugin's ``_open``). It MUST call :meth:`classify` with the new banner.
    * ``close_link()`` — release the serial port (arduino-cli resets the board).
    * ``wait_for_device(device, timeout)`` — wait for the port after the reset.
    * ``is_busy() -> (bool, why)`` — refuse to flash while the board is in use.

    The provisioner owns a single re-entrant ``port_lock``. :meth:`flash` holds it
    across the whole close→upload→reopen sequence, and the plugin's ``reopen`` /
    reconnect paths must take the SAME lock, so nothing can seize the port mid-
    upload. ``classify`` records the latest :class:`FirmwareCheck`.
    """

    def __init__(
        self,
        spec: FirmwareSpec | None,
        *,
        resolve_device: Callable[[], tuple[str | None, str]],
        reopen: Callable[[], str | None],
        close_link: Callable[[], None],
        wait_for_device: Callable[[str, float], object],
        is_busy: Callable[[], tuple[bool, str]] | None = None,
    ):
        self.spec = spec
        self.needed_build: str | None = None
        if spec is not None:
            try:
                self.needed_build = sketch_fingerprint(spec.sketch_dir)
            except Exception:
                log.debug("firmware: could not fingerprint %s sketch",
                          getattr(spec, "name", "?"), exc_info=True)
        self.check: FirmwareCheck | None = None
        self.port_lock = threading.RLock()
        self._resolve_device = resolve_device
        self._reopen = reopen
        self._close_link = close_link
        self._wait_for_device = wait_for_device
        self._is_busy = is_busy

    def classify(self, banner: str | None) -> FirmwareCheck | None:
        """Classify *banner* against the source; None when no source is available."""
        if self.spec is None or self.needed_build is None:
            self.check = None
        else:
            self.check = classify(self.spec, banner, self.needed_build)
        return self.check

    @property
    def can_flash(self) -> bool:
        return (
            self.spec is not None
            and self.needed_build is not None
            and arduino_cli_path() is not None
        )

    def provisioning(
        self, *, plugin_name: str, device: str, firmware: str | None,
        firmware_ok: bool, extra: dict | None = None,
    ) -> dict:
        """The firmware picture for the CLI and GUI (see FirmwareCheck.to_dict)."""
        out: dict = {
            "plugin": plugin_name,
            "device": device,
            "firmware": firmware,
            "firmware_ok": firmware_ok,
            "needed_build": self.needed_build,
            "sketch_found": self.spec is not None,
            "cli_available": arduino_cli_path() is not None,
            "can_flash": self.can_flash,
        }
        if self.check is not None:
            out.update(self.check.to_dict())
        else:
            out["state"] = None
            out["detail"] = "firmware not classified (no source checkout or not probed)"
            out["needs_flash"] = False
            out["safe_to_auto_flash"] = False
        if extra:
            out.update(extra)
        return out

    def flash(self, *, on_line: Callable[[str], None] | None = None) -> FlashResult:
        """Compile + upload the sketch, then reopen + re-verify. Never raises.

        Refuses when the board is busy or the source is missing. Holds ``port_lock``
        across the whole sequence so a concurrent reopen/reconnect can't grab the
        port mid-upload. On a successful upload whose reopen fails, the stale
        classification is cleared (so the board isn't wrongly shown as still out of
        date) and the message says the upload landed but couldn't be confirmed."""
        if self._is_busy is not None:
            busy, why = self._is_busy()
            if busy:
                return FlashResult(False, why)
        if self.spec is None or self.needed_build is None:
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
            # The board reboots after an upload; wait for the port, then reopen and
            # re-read the banner (reopen() calls classify()).
            try:
                self._wait_for_device(device, 8.0)
            except Exception:
                log.debug("firmware: wait_for_device raised", exc_info=True)
            reopen_err = self._reopen()
            if reopen_err is not None:
                log.warning("firmware: could not reopen %s after flashing: %s",
                            device, reopen_err)
                if result.ok:
                    # Upload landed but we can't confirm the new banner. Drop the
                    # stale check so needs_flash doesn't falsely stay True.
                    self.check = None
                    result = FlashResult(
                        True,
                        f"{result.message} — but the port could not be reopened to "
                        "confirm; replug or power-cycle the board",
                        result.log, result.build,
                    )
            return result
