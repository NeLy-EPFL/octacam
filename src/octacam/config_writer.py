"""Write octacam_config.toml and camera parameter files back to disk.

The stdlib has no TOML writer, so the config's small fixed schema is
serialized here. Every edit patches the raw parsed TOML, so the sections it
does not touch, and templated paths such as ``record.directory``, survive as
they were; every file is written atomically.
"""

import contextlib
import copy
import dataclasses
import datetime
import logging
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octacam._compat import tomllib
from octacam.config import (
    ConfigError,
    OctacamConfig,
    duration_to_seconds,
    find_config_file,
    parse_config,
    parse_record_section,
    safe_segment,
)
from octacam.plugins import canonical_name
from octacam.transcode import DEFAULT_TRANSCODE_FFMPEG_PARAMS
from octacam.transform import RECORDING_INFO_DIRNAME, DisplayTransform

if TYPE_CHECKING:
    from octacam.controller import RecordingController

log = logging.getLogger("octacam")

# Per-camera display fields the GUI may change (sensor params live in .pfs).
DISPLAY_FIELDS = (
    "scale_x",
    "scale_y",
    "rotation_deg",
    "window_x",
    "window_y",
    "window_width",
    "window_height",
    "center_x",
    "center_y",
)


# ----------------------------------------------------------------- serialization


def _toml_escape(value: str) -> str:
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _toml_escape(value)
    # The loader reads an unquoted date-like value as a date (_scalar_str).
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        # An inline table: arrays of tables inside a subtable (triggerbox's
        # ``cameras``/``lights`` under ``[plugins.options]``).
        inner = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{" + inner + "}"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def _emit_table(
    lines: list[str], header: str, table: dict, *, array: bool = False
) -> None:
    """Emit a ``[header]`` (or ``[[header]]``) table: its keys first, then each
    nested dict as a ``[header.key]`` subtable."""
    lines.append(f"[[{header}]]" if array else f"[{header}]")
    subtables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    for key, value in table.items():
        # TOML has no null: omit the key, and the loader restores its default.
        if value is None:
            continue
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in subtables:
        if value:
            _emit_table(lines, f"{header}.{key}", value)


def _dumps(data: dict) -> str:
    blocks: list[str] = []

    def block(header: str, table: dict, *, array: bool = False) -> None:
        lines: list[str] = []
        _emit_table(lines, header, table, array=array)
        blocks.append("\n".join(lines))

    # Top-level keys (``backend``) must precede every table header.
    scalars = {
        key: value
        for key, value in data.items()
        if isinstance(value, (str, int, float, bool))
    }
    if scalars:
        blocks.append("\n".join(f"{k} = {_toml_value(v)}" for k, v in scalars.items()))

    for header in ("record", "transcode", "gui"):
        table = data.get(header)
        if isinstance(table, dict) and table:
            block(header, table)
    for camera in data.get("cameras", []) or []:
        if isinstance(camera, dict):
            block("cameras", camera, array=True)
    for plugin in data.get("plugins", []) or []:
        if isinstance(plugin, str):  # a bare name (plugins = ["flywheel"])
            plugin = {"name": plugin}
        if isinstance(plugin, dict):
            block("plugins", plugin, array=True)
    for viz in data.get("visualization", []) or []:
        if isinstance(viz, dict):
            block("visualization", viz, array=True)
    transfer = data.get("transfer")
    if isinstance(transfer, dict) and transfer:
        block("transfer", transfer)

    return "\n\n".join(blocks) + "\n" if blocks else ""


# --------------------------------------------------------------------- merging


def merge_camera_display(raw_base: dict, patches: list[dict]) -> dict:
    """Return a copy of ``raw_base`` with per-camera display fields patched.

    ``patches`` entries carry a ``serial`` (or ``serial_number``) plus any of
    ``DISPLAY_FIELDS`` and an optional ``name``. A camera not already present
    in ``raw_base`` is appended.
    """
    doc = copy.deepcopy(raw_base) if raw_base else {}
    cameras = doc.get("cameras")
    if not isinstance(cameras, list):
        cameras = []
        doc["cameras"] = cameras

    by_serial: dict[str, dict] = {
        str(c["serial_number"]): c
        for c in cameras
        if isinstance(c, dict) and "serial_number" in c
    }
    for patch in patches:
        serial = str(patch.get("serial") or patch.get("serial_number") or "").strip()
        if not serial:
            continue
        target = by_serial.get(serial)
        if target is None:
            target = {"serial_number": serial}
            cameras.append(target)
            by_serial[serial] = target
        if patch.get("name"):
            target["name"] = patch["name"]
        for field in DISPLAY_FIELDS:
            if patch.get(field) is not None:
                target[field] = patch[field]
    return doc


def with_process_params(
    raw: dict,
    *,
    transcode_ffmpeg_params: str,
    transfer_directory: str,
    transfer_checksum: bool,
) -> dict:
    """Return a copy of ``raw`` with the live Process-section values as
    ``[transcode].ffmpeg_params`` and ``[transfer]``.

    Equal to ``raw`` when they match what the config loads as, and a missing
    section is added only for a value that differs from its default.
    """
    doc = copy.deepcopy(raw) if raw else {}

    transcode = doc.get("transcode")
    has_transcode = isinstance(transcode, dict)
    current_ff = (
        transcode.get("ffmpeg_params", DEFAULT_TRANSCODE_FFMPEG_PARAMS)
        if has_transcode
        else DEFAULT_TRANSCODE_FFMPEG_PARAMS
    )
    if transcode_ffmpeg_params != current_ff:
        if not has_transcode:
            transcode = {}
            doc["transcode"] = transcode
        transcode["ffmpeg_params"] = transcode_ffmpeg_params

    transfer = doc.get("transfer")
    has_transfer = isinstance(transfer, dict)
    current_dir = transfer.get("directory", "") if has_transfer else ""
    current_checksum = transfer.get("checksum", True) if has_transfer else True
    if transfer_directory != current_dir or transfer_checksum != current_checksum:
        if not has_transfer:
            transfer = {}
            doc["transfer"] = transfer
        transfer["directory"] = transfer_directory
        transfer["checksum"] = transfer_checksum

    return doc


def with_record_settings(raw: dict, live: Mapping[str, Any]) -> dict:
    """Return a copy of ``raw`` whose ``[record]`` reproduces a recording's settings.

    ``live`` maps ``[record]`` keys to the values the recording ran with, with
    ``duration_s`` (seconds) in place of ``duration``/``duration_unit``. A key is
    written only when its value differs from what the config already loads as,
    so a recording made with the config's own settings keeps a byte-verbatim
    snapshot. A changed duration keeps the config's unit when the value there is
    exact and readable (5 minutes -> 60 minutes), else it is written in seconds
    (100 s, not 1.6666666666666667 minutes). A ``None``
    (``max_nvenc_sessions`` left at auto) removes the key; TOML has no null.
    """
    doc = copy.deepcopy(raw) if raw else {}
    current = parse_record_section(doc)
    values = dict(live)
    duration_s = values.pop("duration_s")
    changes = {k: v for k, v in values.items() if getattr(current, k) != v}
    # A frame-count duration depends on fps, so compare at the recording's fps.
    fps = values.get("fps", current.fps)
    if duration_to_seconds(current.duration, current.duration_unit, fps) != duration_s:
        duration, unit = _duration_in(duration_s, current.duration_unit, fps)
        changes["duration"] = duration
        if unit != current.duration_unit:
            changes["duration_unit"] = unit
    if not changes:
        return doc
    section = doc.get("record")
    if not isinstance(section, dict):
        section = {}
        doc["record"] = section
    for key, value in changes.items():
        if value is None:
            section.pop(key, None)
        else:
            section[key] = value
    return doc


def with_camera_transforms(raw: dict, transforms: Mapping[str, Mapping[str, Any]]) -> dict:
    """Return a copy of ``raw`` whose ``[[cameras]]`` carry the live transforms
    (serial -> :meth:`DisplayTransform.to_dict`).

    A camera is patched only when its transform differs from what the config
    loads as, and only when the config lists it: adding one would change which
    cameras the rig opens. A flip is the sign of ``scale_x``/``scale_y``.
    """
    doc = copy.deepcopy(raw) if raw else {}
    cameras = doc.get("cameras")
    if not isinstance(cameras, list):
        return doc
    for entry in cameras:
        if not isinstance(entry, dict) or "serial_number" not in entry:
            continue
        live = transforms.get(str(entry["serial_number"]))
        if live is None:
            continue
        want = DisplayTransform.from_dict(dict(live))
        scale_x = float(entry.get("scale_x", 1.0)) or 1.0
        scale_y = float(entry.get("scale_y", 1.0)) or 1.0
        have = DisplayTransform.from_scale_rotation(
            scale_x, scale_y, float(entry.get("rotation_deg", 0.0))
        )
        if want == have:
            continue
        entry["rotation_deg"] = float(want.rotation_deg)
        entry["scale_x"] = -abs(scale_x) if want.flip_h else abs(scale_x)
        entry["scale_y"] = -abs(scale_y) if want.flip_v else abs(scale_y)
    return doc


def _duration_in(duration_s: float, unit: str, fps: float) -> tuple[float, str]:
    """``(duration, unit)`` that loads back as exactly ``duration_s``: in ``unit``
    when the value there has at most 3 decimals and converts back exactly, else
    in seconds, which always does."""
    if unit == "frames":
        value = duration_s * fps
    else:
        value = duration_s / duration_to_seconds(1.0, unit, fps)
    if round(value, 3) == value and duration_to_seconds(value, unit, fps) == duration_s:
        return value, unit
    return duration_s, "seconds"


def with_plugin_options(raw: dict, options_by_name: Mapping[str, dict]) -> dict:
    """Return a copy of ``raw`` whose ``[[plugins]]`` reproduce a session's plugins.

    ``options_by_name`` has an entry for every plugin the session loaded: its
    current name -> the options its live state differs in (often empty). Those
    options are merged into the plugin's entry, matched through legacy aliases;
    a loaded plugin the config does not list (enabled with ``--plugin``) is
    appended so a relaunch loads it too. With nothing to merge or append the copy
    compares equal to ``raw``.
    """
    doc = copy.deepcopy(raw) if raw else {}
    found = doc.get("plugins")
    # Absent, or malformed (which the loader ignores too): start a new list.
    entries: list[str | dict] = found if isinstance(found, list) else []
    listed: dict[str, int] = {}
    for index, entry in enumerate(entries):
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str):
            listed.setdefault(canonical_name(name), index)
    for name, options in options_by_name.items():
        index = listed.get(name)
        if index is None:
            entries.append({"name": name, "options": dict(options)})
            doc["plugins"] = entries
            listed[name] = len(entries) - 1
            continue
        if not options:
            continue
        entry = entries[index]
        # A bare-name entry becomes a table to hold its options.
        table: dict[str, Any] = entry if isinstance(entry, dict) else {"name": entry}
        entries[index] = table
        current = table.get("options")
        table["options"] = (
            {**current, **options} if isinstance(current, dict) else dict(options)
        )
    return doc


# ----------------------------------------------------------------- file writing


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file on the same dir + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".octacam-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_config(config_dir: str | Path, doc: dict) -> Path:
    """Serialize ``doc`` to ``config_dir/octacam_config.toml`` atomically."""
    path = find_config_file(config_dir)
    atomic_write_text(path, _dumps(doc))
    return path


def _extensions(extension: str | Iterable[str]) -> tuple[str, ...]:
    """One suffix, or a mixed rig's suffixes, as a tuple."""
    if isinstance(extension, str):
        return (extension,)
    return tuple(extension)


def write_pfs_files(
    target_dir: str | Path,
    pfs_by_serial: dict[str, str],
    extension: str | Mapping[str, str] = "pfs",
) -> None:
    """Write each ``<serial> -> param text`` to ``<serial>.<extension>``;
    ``extension`` is one suffix or a mixed rig's ``serial -> suffix`` map."""
    target_dir = Path(target_dir)
    for serial, text in pfs_by_serial.items():
        ext = extension if isinstance(extension, str) else extension.get(serial, "pfs")
        atomic_write_text(target_dir / f"{serial}.{ext}", text)


def read_pfs_files(
    config_dir: str | Path, extension: str | Iterable[str] = "pfs"
) -> dict[str, str]:
    """Map file stem -> text for every parameter file in ``config_dir`` (the
    inverse of :func:`write_pfs_files`). Auxiliary files such as
    ``fictrac_camera_config.pfs`` are read too and never match a serial."""
    config_dir = Path(config_dir)
    out: dict[str, str] = {}
    if not config_dir.is_dir():
        return out
    for ext in _extensions(extension):
        for path in sorted(config_dir.glob(f"*.{ext}")):
            try:
                out[path.stem] = path.read_text()
            except OSError:
                continue
    return out


def copy_auxiliary_pfs(
    src_dir: str | Path,
    target_dir: str | Path,
    live_serials: set[str],
    extension: str | Iterable[str] = "pfs",
) -> None:
    """Copy the parameter files no live camera wrote (auxiliary configs,
    cameras not opened), so ``target_dir`` is a complete config directory."""
    src_dir, target_dir = Path(src_dir), Path(target_dir)
    if src_dir.resolve() == target_dir.resolve():
        return
    for ext in _extensions(extension):
        for src in sorted(src_dir.glob(f"*.{ext}")):
            if src.stem not in live_serials:
                atomic_write_text(target_dir / src.name, src.read_text())


# ----------------------------------------------------------------- new config dir


def resolve_new_config_dir(
    active_dir: str | Path, name: str, *, overwrite: bool = False
) -> Path:
    """Resolve a new config dir beside the active one, where presets live; for a
    session relaunched from a recording (its ``octacam_recording`` subfolder),
    beside the recording folder."""
    active = Path(active_dir)
    anchor = active.parent if active.name == RECORDING_INFO_DIRNAME else active
    target = anchor.parent / safe_segment(name, "config name")
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    return target


def load_raw_config(config_dir: str | Path) -> dict:
    """Best-effort raw ``tomllib`` dict of the existing config (``{}`` if absent/bad)."""
    path = find_config_file(config_dir)
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        return {}


# ----------------------------------------------------------------- GUI save


@dataclasses.dataclass(frozen=True)
class SavedConfig:
    """What :func:`save_rig_config` wrote. ``raw`` is set when the active
    config's TOML was rewritten (it is now the document a save patches) and
    ``config`` when that file also parsed back (it is now live)."""

    directory: Path
    cameras_written: list[str]
    raw: dict | None = None
    config: OctacamConfig | None = None


def save_rig_config(
    controller: "RecordingController",
    active_dir: Path,
    raw: dict,
    cameras: list[dict],
    *,
    new_name: str | None = None,
    overwrite: bool = False,
    sensor: bool = True,
    display: bool = True,
) -> SavedConfig:
    """Save the cameras' parameter files (``sensor``) and their display settings
    patched into ``raw`` (``display``) to ``active_dir``, or to a new config dir
    ``new_name`` (with the auxiliary parameter files, so it is complete).

    A saved active config goes live: its cameras get its display transforms,
    which recordings bake in, and its ROI centering. Raises RuntimeError while
    recording, ValueError for a bad name, FileExistsError for an existing new
    dir without ``overwrite``, and OSError when a write fails."""
    system = controller.camera_system
    params = controller.export_camera_params() if sensor else {}
    doc = merge_camera_display(raw, cameras) if display else None
    if new_name is None:
        target = active_dir
    else:
        target = resolve_new_config_dir(active_dir, new_name, overwrite=overwrite)
        target.mkdir(parents=True, exist_ok=True)
        copy_auxiliary_pfs(active_dir, target, set(params), system.extensions)
    if sensor:
        write_pfs_files(target, params, system.extension_by_serial())
    written = sorted(params)
    if doc is None:
        return SavedConfig(target, written)
    write_config(target, doc)
    if new_name is not None:  # a new config is written, never adopted
        return SavedConfig(target, written)
    try:
        config = parse_config(find_config_file(target))
    except ConfigError as e:
        # Unexpected (written from a validated document); the save succeeded.
        log.error("Saved config did not parse back; keeping the live one: %s", e)
        return SavedConfig(target, written, raw=doc)
    system.apply_display_config(config.cameras)
    return SavedConfig(target, written, raw=doc, config=config)
