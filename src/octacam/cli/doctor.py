"""`octacam doctor`: it only enumerates and reads locks, never opens a camera
(vendor SDKs open USB3 devices exclusively), so it is safe beside a live session."""

import json
import logging
import os
import resource
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

import octacam
from octacam.cli._common import (
    browser_skip_reason,
    enumerate_backend,
    in_ssh_session,
    port_available,
    resolve_config_arg,
    serial_plugin,
    stderr_console,
)

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("octacam")


# status -> (marker, rich style). "list" is a plain indented enumeration line.
_MARKERS = {
    "ok": ("✓", "green"),
    "warn": ("⚠", "yellow"),
    "error": ("✗", "red"),
    "info": ("•", "cyan"),
    "list": ("", ""),
}


class _Report:
    """Accumulates doctor findings as ordered sections of (status, text) lines."""

    def __init__(self) -> None:
        self.sections: list[tuple[str, list[tuple[str, str]]]] = []

    def section(self, title: str) -> None:
        self.sections.append((title, []))

    def add(self, status: str, text: str) -> None:
        self.sections[-1][1].append((status, text))

    def counts(self) -> tuple[int, int]:
        """(errors, warnings) across every section, for the exit code."""
        errors = warns = 0
        for _title, items in self.sections:
            for status, _text in items:
                errors += status == "error"
                warns += status == "warn"
        return errors, warns


class _CameraScan:
    """Enumerate each backend doctor reports on once, concurrently, and serve
    every section from that scan (re-entering a vendor SDK is slow: Spinnaker
    re-inits its System per call, ~2.4 s).

    Every SDK is imported on the calling thread first, so no two cold imports
    race the import lock; the workers only scan."""

    def __init__(self, only_backend: str | None) -> None:
        from octacam.cameras.registry import BACKENDS, CASCADE, is_auto, select_backend

        self.only = None if is_auto(only_backend) else (only_backend or "").strip().lower()
        # The backends _doctor_backends reports on (fake, being synthetic, only
        # when named): it calls get() for each, and a miss reads as a failed scan.
        display = [self.only] if self.only else [b for b in BACKENDS if b != "fake"]
        # select_backend imports the SDK here, on the calling thread. An
        # unavailable tier is dropped (_doctor_backends reports it).
        self.targets: list[str] = []
        for name in display:
            try:
                select_backend(name)
            except Exception:
                continue
            self.targets.append(name)
        # tl_factory hides GENICAM_GENTL64_PATH while pylon loads; do it before the
        # workers start, so the environment never changes under the other SDKs.
        if "basler" in self.targets:
            try:
                from octacam.cameras.basler import tl_factory

                tl_factory()
            except Exception:
                pass  # the basler worker retries it and reports the failure
        self._cascade_order = [b for b in CASCADE if b in self.targets]
        self._cams: dict[str, list[tuple[str, str | None]]] = {}
        self._errs: dict[str, Exception] = {}

    def run(
        self, on_done: "Callable[[str, Exception | None], None] | None" = None
    ) -> None:
        """Enumerate every target once, concurrently, caching results and errors.

        ``on_done(name, err)`` runs on the calling thread as each one finishes."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not self.targets:
            return
        with ThreadPoolExecutor(max_workers=max(1, len(self.targets))) as pool:
            futures = {
                pool.submit(enumerate_backend, name): name for name in self.targets
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    self._cams[name] = future.result()
                    err: Exception | None = None
                except Exception as exc:  # native SDK / subprocess enumerate failure
                    self._errs[name] = exc
                    err = exc
                if on_done is not None:
                    on_done(name, err)

    def get(self, name: str) -> list[tuple[str, str | None]]:
        """Cached ``[(serial, model), ...]`` for a scanned backend (any case);
        re-raises its enumeration failure."""
        key = name.strip().lower()
        if key in self._errs:
            raise self._errs[key]
        return self._cams[key]

    def cascade(self) -> list[tuple[str, str, str | None]]:
        """:func:`cascade_assignment` from the scan; a failed tier is skipped."""
        claimed: dict[str, tuple[str, str | None]] = {}
        order: list[str] = []
        for backend in self._cascade_order:
            if backend in self._errs:
                continue
            for serial, model in self._cams.get(backend, []):
                if serial in claimed:
                    continue
                claimed[serial] = (backend, model)
                order.append(serial)
        return [(s, claimed[s][0], claimed[s][1]) for s in order]

    def detected_serials(self, backend: str | None) -> set[str]:
        """Serials to cross-check the config against: the cascade under ``auto``;
        a backend the scan skipped is enumerated live (and may raise)."""
        from octacam.cameras.registry import is_auto

        if is_auto(backend):
            return {serial for serial, _backend, _model in self.cascade()}
        key = (backend or "").strip().lower()
        if key in self._cams or key in self._errs:
            return {serial for serial, _model in self.get(key)}
        return {serial for serial, _model in enumerate_backend(key)}


def _run_scan_with_progress(scan: _CameraScan, quiet: bool) -> None:
    """Run the scan behind a per-backend spinner on stderr, shown only on a
    terminal and without ``--json``; the scan runs either way."""
    from rich.progress import Progress, SpinnerColumn, TextColumn

    console = stderr_console()
    disable = quiet or not console.is_terminal
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
        disable=disable,
    ) as progress:
        tasks = {
            name: progress.add_task(f"enumerating {name}…", total=1)
            for name in scan.targets
        }

        def on_done(name: str, err: "Exception | None") -> None:
            if err is not None:
                desc = f"{name}: enumeration failed"
            else:
                desc = f"{name}: {len(scan.get(name))} camera(s)"
            progress.update(tasks[name], completed=1, description=desc)

        scan.run(on_done=on_done)


def _nvidia_gpus() -> list[str]:
    """Detected NVIDIA GPUs as "<name> (driver <ver>)", via nvidia-smi; empty
    when there is none (so NVENC is unavailable)."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    gpus: list[str] = []
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        if len(parts) >= 2:
            gpus.append(f"{parts[0]} (driver {parts[1]})")
        else:
            gpus.append(parts[0])
    return gpus


def _report_free_space(report: _Report, path: Path, label: str) -> None:
    """Report free space on the filesystem holding ``path`` (or its nearest parent)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as e:
        report.add("warn", f"{label}: could not check free space on {probe} ({e})")
        return
    free_gb = usage.free / 1e9
    report.add(
        "warn" if free_gb < 5 else "ok",
        f"{label}: {free_gb:.1f} GB free on {probe}",
    )


def _doctor_system(report: _Report) -> None:
    import platform

    report.section("System")
    report.add("info", f"octacam {octacam.__version__}")
    report.add(
        "info", f"Python {platform.python_version()} on {platform.platform(terse=True)}"
    )
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = 1200  # ~150 fds/camera (pylon USB stack) for an 8-camera rig
    if hard != resource.RLIM_INFINITY and hard < need:
        report.add(
            "warn",
            f"open-file hard limit is low ({hard}); ~150 fds/camera means an "
            f"8-camera rig needs ~{need}",
        )
    elif soft < hard:
        report.add("ok", f"open-file limit {soft}→{hard} (raised to hard at launch)")
    else:
        report.add("ok", f"open-file limit {soft}")
    _doctor_updates(report)


def _doctor_updates(report: _Report) -> None:
    """Report whether a newer octacam release is available (advice only; a
    failed check never fails the command)."""
    from octacam import updates

    notice = updates.check()
    if notice.update_available and notice.latest:
        msg = f"octacam {notice.latest} is available (you have {notice.current})"
        if notice.command:
            msg += f" — update with: {notice.command}"
        report.add("warn", msg)
    elif notice.latest:
        report.add("ok", f"octacam {notice.current} is the latest release")
    else:
        report.add("info", f"update check skipped ({notice.note})")


def _camera_lines(cams: "list[tuple[str, str | None]]") -> list[str]:
    """One ``model: s1, s2, …`` line per model (first-seen order); a camera of
    unknown model gets a bare serial line."""
    groups: dict[str | None, list[str]] = {}
    for serial, model in cams:
        groups.setdefault(model, []).append(serial)
    lines: list[str] = []
    for model, serials in groups.items():
        if model:
            lines.append(f"{model}: {', '.join(serials)}")
        else:
            lines.extend(serials)  # unknown model → bare serial per line
    return lines


# USB vendor IDs of camera makers, for the link-speed check (which also matches
# any detected serial).
_CAMERA_USB_VENDORS = {"2676": "Basler", "1e10": "FLIR"}


_SUPERSPEED_MBPS = 5000


def _usb_camera_links(
    detected_serials: "set[str]", root: Path = Path("/sys/bus/usb/devices")
) -> "list[tuple[str, str, int]]":
    """``[(serial, product, speed_mbps), ...]`` for connected camera USB devices.

    Read from sysfs, so no device is opened; ``[]`` without that sysfs layout."""

    def _read(dev: Path, field: str) -> str:
        try:
            return (dev / field).read_text().strip()
        except OSError:
            return ""

    out: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    try:
        devices = sorted(root.iterdir())
    except OSError:
        return out
    for dev in devices:
        serial = _read(dev, "serial")
        if not serial or serial in seen:
            continue
        if _read(dev, "idVendor") not in _CAMERA_USB_VENDORS and serial not in detected_serials:
            continue
        try:
            speed = int(float(_read(dev, "speed")))
        except ValueError:
            continue
        seen.add(serial)
        out.append((serial, _read(dev, "product"), speed))
    return out


def _doctor_backends(
    report: _Report, only_backend: str | None, scan: _CameraScan
) -> None:
    from octacam.cameras import BackendUnavailable
    from octacam.cameras.registry import BACKENDS, is_auto, select_backend

    report.section("Camera backends")
    only = None if is_auto(only_backend) else only_backend
    backends = (only,) if only else tuple(b for b in BACKENDS if b != "fake")
    detected_serials: set[str] = set()
    for name in backends:
        try:
            select_backend(name)
        except BackendUnavailable as e:  # an SDK not installed is expected
            report.add("info", str(e))
            continue
        except Exception as e:
            report.add("warn", f"{name}: could not select backend ({e})")
            continue
        try:
            cams = scan.get(name)
        except Exception as e:
            report.add("warn", f"{name}: available, but enumeration failed ({e})")
            continue
        report.add("ok", f"{name}: available — {len(cams)} camera(s) detected")
        detected_serials.update(serial for serial, _model in cams)
        for line in _camera_lines(cams):
            report.add("list", line)
    # A camera whose SuperSpeed link fails to train falls back to USB 2.0 and
    # then fails to open; the negotiated speed shows it without opening.
    for serial, product, speed in _usb_camera_links(detected_serials):
        if speed >= _SUPERSPEED_MBPS:
            continue
        label = f"{product} {serial}" if product else serial
        usb2 = " (USB 2.0)" if speed == 480 else ""
        report.add(
            "warn",
            f"{label} is linked at only {speed} Mb/s{usb2}, not USB 3 SuperSpeed "
            f"({_SUPERSPEED_MBPS} Mb/s) — it will fail to open. A USB3 camera whose "
            "SuperSpeed link fails to train drops back to USB 2.0 even in a USB 3 "
            "port; check/replace its cable, reseat it, or try another USB 3 port.",
        )
    if not only and os.environ.get("PYLON_CAMEMU"):
        report.add(
            "info",
            f"PYLON_CAMEMU={os.environ['PYLON_CAMEMU']} (emulated Basler cameras)",
        )
    # Under "auto" a camera may be seen by several tiers; show which one wins.
    if not only:
        assignment = scan.cascade()
        if assignment:
            report.add("info", "cascade selection (backend each camera opens through):")
            grouped: dict[tuple[str, str | None], list[str]] = {}
            for serial, backend, model in assignment:
                grouped.setdefault((backend, model), []).append(serial)
            for (backend, model), serials in grouped.items():
                joined = ", ".join(serials)
                label = f"{model}: {joined}" if model else joined
                report.add("list", f"{label} → {backend}")


def _doctor_encoding(report: _Report) -> None:
    from octacam.ffmpeg import ffmpeg_query, ffmpeg_source, ffmpeg_version, find_ffmpeg
    from octacam.transcode import DEFAULT_TRANSCODE_FFMPEG_PARAMS
    from octacam.writer import DEFAULT_FFMPEG_PARAMS

    report.section("Encoding toolchain")
    try:
        exe = find_ffmpeg()
    except RuntimeError as e:
        report.add("error", str(e))
        return
    version = ffmpeg_version(exe)
    report.add(
        "ok" if version else "warn",
        f"ffmpeg {version or 'version unknown'} ({ffmpeg_source(exe)})",
    )
    report.add("list", exe)
    has_x264 = "libx264" in ffmpeg_query(exe, "-hide_banner", "-encoders")
    report.add(
        "ok" if has_x264 else "error",
        "libx264 encoder present"
        if has_x264
        else "libx264 encoder MISSING — the default record/transcode params need it",
    )
    system = shutil.which("ffmpeg")
    if system and os.path.realpath(system) != os.path.realpath(exe):
        sysver = ffmpeg_version(system)
        report.add(
            "info",
            f"system ffmpeg on PATH: {sysver or system} (unused; the resolved "
            "binary takes precedence — colour-range flags can differ by version)",
        )
    report.add("info", f"default record params:    {DEFAULT_FFMPEG_PARAMS}")
    report.add("info", f"default transcode params: {DEFAULT_TRANSCODE_FFMPEG_PARAMS}")
    _doctor_gpu_encoding(report)


def _doctor_gpu_encoding(report: _Report) -> None:
    """Report GPU (NVIDIA NVENC) encode availability — the opt-in save_method="nvenc"."""
    from octacam.ffmpeg import ffmpeg_version, find_ffmpeg, probe_nvenc_max_sessions
    from octacam.writer import NVENC_H264_PARAMS

    gpus = _nvidia_gpus()
    if not gpus:
        report.add(
            "info",
            'no NVIDIA GPU detected (nvidia-smi) — GPU encoding unavailable; '
            'save_method="nvenc" would fall back to CPU (libx264)',
        )
        return
    for gpu in gpus:
        report.add("ok", f"NVIDIA GPU: {gpu}")
    try:
        nvexe = find_ffmpeg(require_encoder="h264_nvenc")
    except RuntimeError:
        report.add(
            "warn",
            'no ffmpeg with a working h264_nvenc encoder found — GPU encoding '
            "unavailable (the bundled imageio-ffmpeg has no NVENC, and a system "
            "ffmpeg's NVENC needs an API version the driver supports). Install a "
            'system ffmpeg built with NVENC; until then save_method="nvenc" '
            "falls back to CPU (libx264).",
        )
        return
    report.add(
        "ok",
        f"h264_nvenc works via {ffmpeg_version(nvexe) or 'ffmpeg'} at {nvexe}",
    )
    sessions = probe_nvenc_max_sessions()
    if sessions is not None:
        suffix = "+ (probe ceiling)" if sessions >= 12 else ""
        report.add(
            "info",
            f"concurrent NVENC sessions detected: {sessions}{suffix} "
            "(cameras beyond record.max_nvenc_sessions encode on CPU)",
        )
    report.add("info", f"NVENC record params: {NVENC_H264_PARAMS}")


def _doctor_config(report: _Report, config_dir: Path):
    from octacam._compat import tomllib
    from octacam.config import (
        find_config_file,
        load_config_dir,
        resolve_dir_template,
        resolve_save_path,
    )

    report.section(f"Config ({config_dir})")
    cfg_file = find_config_file(config_dir)
    if not cfg_file.exists():
        report.add(
            "warn",
            f"no {cfg_file.name} here — all detected cameras would be used, with defaults",
        )
        return None
    try:
        tomllib.loads(cfg_file.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        report.add("error", f"{cfg_file.name} could not be parsed: {e}")
        return None
    cfg = load_config_dir(config_dir)
    report.add(
        "ok",
        f"{cfg_file.name} loaded (backend={cfg.backend}, "
        f"{len(cfg.cameras)} camera(s) declared)",
    )
    report.add("info", f"next recording → {resolve_save_path(cfg.record).save_dir}")
    if cfg.transfer and cfg.transfer.directory:
        report.add("info", f"transfer → {resolve_dir_template(cfg.transfer.directory)}")
    else:
        report.add("info", "no [transfer] destination configured")
    return cfg


def _doctor_cameras_vs_config(
    report: _Report, cfg, only_backend: str | None, scan: _CameraScan
) -> None:
    report.section("Cameras vs config")
    declared = [c.serial_number for c in cfg.cameras]
    if not declared:
        report.add("info", "config declares no serials; all detected cameras are used")
        return
    from octacam.cameras.registry import is_auto

    backend = only_backend or cfg.backend
    where = "across all backends" if is_auto(backend) else f"on {backend}"
    try:
        detected = scan.detected_serials(backend)
    except Exception as e:
        report.add("warn", f"could not enumerate {where} to cross-check ({e})")
        return
    missing = [s for s in declared if s not in detected]
    extra = sorted(detected - set(declared))
    if not missing:
        report.add("ok", f"all {len(declared)} declared camera(s) detected {where}")
    for serial in missing:
        report.add(
            "error",
            f"serial {serial} declared but NOT detected (unplugged? wrong serial?)",
        )
    for serial in extra:
        report.add("info", f"serial {serial} detected but not in config (won't record)")


def _doctor_storage(report: _Report, cfg) -> None:
    from octacam.config import resolve_dir_template, resolve_save_path

    report.section("Storage & transfer")
    save_dir = Path(resolve_save_path(cfg.record).save_dir)
    _report_free_space(report, save_dir, "record dir")
    transfer = cfg.transfer
    if transfer is None or not transfer.directory:
        report.add("info", "no [transfer] destination configured")
        return
    dest = Path(resolve_dir_template(transfer.directory))
    if not dest.exists():
        report.add(
            "warn",
            f"transfer dest not present/mounted: {dest} (local recording still works)",
        )
        return
    if not os.access(dest, os.W_OK):
        report.add("error", f"transfer dest not writable: {dest}")
        return
    _report_free_space(report, dest, "transfer dest")
    report.add(
        "info", f"checksum verify: {'on' if transfer.checksum else 'off (size-only)'}"
    )


def _doctor_plugins(report: _Report, cfg) -> None:
    from octacam import plugins as plugins_mod

    report.section("Plugins")
    infos = plugins_mod.available_plugins()
    by_name = {info.name: info for info in infos}
    for info in infos:
        if info.available:
            suffix = f" ({info.summary})" if info.summary else ""
            report.add("ok", f"{info.name} — available{suffix}")
        else:
            suffix = f" ({info.detail})" if info.detail else ""
            report.add("info", f"{info.name} — unavailable{suffix}")
    if cfg is None:
        return
    for pc in cfg.plugins:
        name = plugins_mod.canonical_name(pc.name)
        info = by_name.get(name)
        if info is None:
            report.add("error", f"config enables unknown plugin {pc.name!r}")
        elif not info.available:
            report.add(
                "error",
                f"config enables {name!r} but it is unavailable ({info.detail})",
            )
        else:
            report.add("ok", f"config enables {name!r} (available)")


def _configured_device(pc) -> tuple[str | None, bool]:
    """``(device, is_auto)`` a serial plugin's config resolves to, as the plugin
    would: the ``device`` option, else the plugin's default port."""
    from octacam.plugins import canonical_name

    raw = pc.options.get("device")
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        return None, True
    if raw:
        return str(raw), False
    cls = serial_plugin(canonical_name(pc.name))
    return (cls.default_device if cls is not None else None), False


def _doctor_serial(report: _Report, cfg, probe: bool = False) -> None:
    """List serial devices and cross-check plugin ports. Passive unless
    ``probe``, which reads each board's identity, skipping ports a session holds."""
    from octacam import serial_ports as sp

    report.section("Serial devices")
    ports = sp.list_serial_ports()
    if not ports:
        report.add("info", "no serial ports detected")

    # A host can have dozens of legacy /dev/ttyS*: they collapse to one line.
    mcus = [p for p in ports if p.likely_microcontroller]
    generic = [p for p in ports if not p.likely_microcontroller]
    for p in mcus:
        sn = f"  sn={p.serial_number}" if p.serial_number else ""
        line = f"{p.board_name}  {p.device}  [{p.vid_pid}]{sn}"
        report.add("info" if p.likely_arduino else "list", line)
    if generic:
        shown = ", ".join(p.device for p in generic[:4])
        more = f", … (+{len(generic) - 4} more)" if len(generic) > 4 else ""
        report.add("list", f"{len(generic)} other/generic serial port(s): {shown}{more}")

    _doctor_serial_vs_config(report, cfg, ports)
    if probe:
        _doctor_serial_probe(report, cfg, mcus)


def _doctor_serial_vs_config(report: _Report, cfg, ports) -> None:
    """Cross-check each serial plugin's device against the detected ports: an
    error when it is absent, info for a board no plugin uses."""
    from octacam import serial_ports as sp
    from octacam.plugins import canonical_name

    if cfg is None:
        return
    detected_real = {os.path.realpath(p.device) for p in ports}
    used_real: set[str] = set()
    any_serial_plugin = False
    for pc in cfg.plugins:
        name = canonical_name(pc.name)
        if serial_plugin(name) is None:
            continue
        any_serial_plugin = True
        device, is_auto = _configured_device(pc)
        if is_auto:
            resolved, reason = sp.resolve_device("auto")
            if resolved is None:
                report.add("error", f"plugin {name!r}: {reason}")
            else:
                report.add("ok", f"plugin {name!r} device=auto → {reason}")
                used_real.add(os.path.realpath(resolved))
            continue
        if not device:
            continue
        real = os.path.realpath(device)
        if real in detected_real:
            report.add("ok", f"plugin {name!r} device {device} is connected")
            used_real.add(real)
        else:
            report.add(
                "error",
                f"plugin {name!r} device {device} not found among connected "
                f"serial ports ({sp.format_candidates(ports)})",
            )
            arduino = next((p for p in ports if p.likely_arduino), None)
            if arduino is not None:
                report.add(
                    "info",
                    f"a stable udev rule for {arduino.device}: "
                    f"{sp.udev_rule_for(arduino)}",
                )
    if any_serial_plugin:
        for p in ports:
            if p.likely_microcontroller and os.path.realpath(p.device) not in used_real:
                report.add(
                    "info",
                    f"{p.board_name} {p.device} detected but not used by any plugin",
                )


def _doctor_serial_probe(report: _Report, cfg, mcus) -> None:
    """Read each microcontroller port's firmware identity (opt-in, invasive)."""
    from octacam import firmware as fw
    from octacam import serial_ports as sp
    from octacam.plugins import canonical_name

    expected: dict[str, tuple[str, fw.FirmwareSpec]] = {}
    if cfg is not None:
        for pc in cfg.plugins:
            name = canonical_name(pc.name)
            cls = serial_plugin(name)
            spec = cls.firmware if cls is not None else None
            device, is_auto = _configured_device(pc)
            if spec is not None and device and not is_auto:
                expected[os.path.realpath(device)] = (name, spec)
    for p in mcus:
        ident = sp.probe_identity(p.device)
        if ident.busy:
            report.add(
                "info",
                f"{p.device}: port in use (held exclusively by another process); "
                "skipped identity probe",
            )
            continue
        if ident.banner:
            report.add("info", f"{p.device}: firmware identity {ident.banner!r}")
        else:
            report.add(
                "info",
                f"{p.device}: no identity reply (not an octacam-firmware board, "
                "or its firmware has no identify command)",
            )
        exp = expected.get(os.path.realpath(p.device))
        if not exp:
            continue
        name, spec = exp
        needed = fw.source_build(spec)
        if needed is not None:
            check = fw.classify(spec, ident.banner, needed)
            if check.state is fw.FirmwareState.CURRENT:
                report.add("ok", f"{p.device}: {name} firmware up to date (build {needed})")
            elif check.needs_flash and check.state is not fw.FirmwareState.UNIDENTIFIED:
                # (UNIDENTIFIED already got the "no identity reply" line.)
                report.add(
                    "warn",
                    f"{p.device}: {name} firmware needs flashing — {check.detail}; "
                    "run `octacam flash`",
                )
        elif ident.banner and not ident.banner.upper().startswith(spec.banner_prefix):
            report.add(
                "warn",
                f"{p.device}: expected {name} firmware (banner {spec.banner_prefix!r}) "
                f"but got {ident.banner!r} — wrong board?",
            )


def _doctor_runtime(report: _Report, config_dir: Path | None) -> None:
    from octacam import locks, session_cache

    report.section("Recording cache & runtime")
    cdir = session_cache.cache_dir()
    try:
        tracked = {
            e["folder"] for e in session_cache._read_entries() if e.get("folder")
        }
    except Exception:
        tracked = set()
    existing = session_cache.all_folders()
    stale = len(tracked - {str(p) for p in existing})
    writable = os.access(cdir if cdir.exists() else cdir.parent, os.W_OK)
    report.add(
        "ok" if writable else "warn",
        f"cache {cdir} — {len(existing)} recording(s)"
        + (f", {stale} stale (deleted)" if stale else ""),
    )
    try:
        running = session_cache.transcode_running()
    except Exception:
        running = 0
    if running:
        report.add(
            "warn",
            f"{running} transcode(s) running here — CPU-heavy, may cause dropped "
            "frames if you start recording now",
        )
    else:
        report.add("ok", "no transcode running on this machine")
    if config_dir is not None:
        holder = locks.holder(config_dir)
        if holder:
            report.add(
                "warn",
                f"another octacam holds this rig's lock (pid {holder}) — its "
                "cameras are in use",
            )
        else:
            report.add("ok", "no other octacam instance holds this rig")
    if not port_available("127.0.0.1", 8765):
        report.add("warn", "GUI port 8765 is in use (launch gui with --port to change)")
    if in_ssh_session():
        report.add(
            "info",
            "SSH session — the GUI won't auto-open a browser; use an ssh -L tunnel",
        )
    elif browser_skip_reason(no_browser=False):  # past SSH, only no display is left
        report.add("info", "no local display — the GUI won't auto-open a browser")


def _render_doctor(report: _Report) -> None:
    from rich.console import Console
    from rich.text import Text

    console = Console()
    console.print()
    console.print(Text(f"octacam doctor — octacam {octacam.__version__}", style="bold"))
    for title, items in report.sections:
        console.print()
        console.print(Text(title, style="bold"))
        for status, text in items:
            marker, style = _MARKERS[status]
            if not marker:  # a plain listing line
                console.print(Text("      " + text))
                continue
            line = Text("  ")
            line.append(marker + " ", style=style or None)
            line.append(text)
            console.print(line)
    errors, warns = report.counts()
    console.print()
    if errors or warns:
        console.print(
            Text(
                f"{errors} error(s), {warns} warning(s)",
                style="bold red" if errors else "bold yellow",
            )
        )
    else:
        console.print(Text("All checks passed.", style="bold green"))


def _emit_doctor_json(report: _Report) -> None:
    errors, warns = report.counts()
    payload = {
        "octacam_version": octacam.__version__,
        "sections": [
            {
                "title": title,
                "findings": [{"status": s, "text": t} for s, t in items],
            }
            for title, items in report.sections
        ],
        "errors": errors,
        "warnings": warns,
    }
    typer.echo(json.dumps(payload, indent=2))


def doctor(
    config_dir: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Optional rig config dir. When given, doctor also validates that "
            "rig's config, resolves its save/transfer paths, cross-checks declared "
            "vs detected cameras, and reports the plugin selection.",
        ),
    ] = None,
    backend: Annotated[
        str | None,
        typer.Option(
            "--backend",
            help="Only enumerate this backend (basler/flir/spinnaker/"
            "pycameleon/fake). Default: the whole available cascade.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json", help="Emit machine-readable JSON instead of the report."
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Exit nonzero on warnings too (for CI), not only on errors.",
        ),
    ] = False,
    probe_serial: Annotated[
        bool,
        typer.Option(
            "--probe-serial",
            help="Also open each detected serial port briefly to read its "
            "firmware identity. It writes to each board, even one a running "
            "session holds (except on Windows), so skip this while a board may "
            "be armed.",
        ),
    ] = False,
) -> None:
    """Diagnose the octacam install and, optionally, a rig config.

    Lists detected cameras and bundled plugins, and checks the encoding toolchain,
    storage, recording cache, and runtime conflicts. Pass a CONFIG_DIR to also
    validate that rig. doctor never opens the cameras, so it is safe to run while
    a GUI or `record` session is live.

    Exits 0 when no errors are found (nonzero on errors, or on warnings too with
    --check), so it is usable as a pre-flight check in scripts.
    """
    if config_dir is not None:
        config_dir = resolve_config_arg(config_dir)
    report = _Report()
    # Enumeration is the slow part: one parallel scan serves every section.
    scan = _CameraScan(backend)
    _run_scan_with_progress(scan, quiet=json_output)
    _doctor_system(report)
    _doctor_backends(report, backend, scan)
    _doctor_encoding(report)
    cfg = _doctor_config(report, config_dir) if config_dir is not None else None
    if cfg is not None:
        _doctor_cameras_vs_config(report, cfg, backend, scan)
        _doctor_storage(report, cfg)
    _doctor_plugins(report, cfg)
    _doctor_serial(report, cfg, probe=probe_serial)
    _doctor_runtime(report, config_dir)

    if json_output:
        _emit_doctor_json(report)
    else:
        _render_doctor(report)

    errors, warns = report.counts()
    if errors or (strict and warns):
        raise typer.Exit(1)
