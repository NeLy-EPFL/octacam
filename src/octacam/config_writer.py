"""Write octacam_config.toml (and camera .pfs files) back to disk.

The stdlib ships ``tomllib`` for *reading* TOML but no writer, and octacam
keeps its dependency set deliberately lean, so this module hand-serializes the
small, fixed config schema (``[record]`` / ``[transcode]`` / ``[gui]`` /
``[[cameras]]`` / ``[[plugins]]`` / ``[[visualization]]`` / ``[transfer]``).

Two design choices keep saves faithful:

* The writer starts from the **raw parsed TOML** (``tomllib.loads`` of the
  existing file) and patches only the per-camera display fields the GUI
  changed. Every other section is preserved verbatim, so a GUI camera-display
  save never touches (or drops) the record/transcode/visualization/transfer
  sections, and templated paths like ``record.directory`` stay unexpanded.
* Every file is written through a temp file + ``os.replace`` so a crash can
  never leave a truncated config behind.
"""

import contextlib
import copy
import datetime
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from octacam._compat import tomllib
from octacam.config import duration_to_seconds, find_config_file, parse_record_section
from octacam.plugins import canonical_name
from octacam.writer import DEFAULT_TRANSCODE_FFMPEG_PARAMS

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
    # The reader accepts date/datetime scalars (config.py:_scalar_str coerces an
    # unquoted, date-like value to text); mirror that here. Check datetime before
    # date (datetime subclasses date) so full timestamps aren't truncated.
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        # Inline table: ``{ key = value, ... }``. Used for arrays of tables that
        # live inside a subtable (e.g. the triggerbox plugin's ``cameras`` /
        # ``lights`` under ``[plugins.options]``), which _emit_table writes as a
        # single ``key = [...]`` line rather than as [[header]] arrays.
        inner = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{" + inner + "}"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def _emit_table(
    lines: list[str], header: str, table: dict, *, array: bool = False
) -> None:
    """Emit a ``[header]`` (or ``[[header]]``) table.

    Scalar keys are written before any nested dict so they belong to ``header``
    and not the subtable; a nested dict becomes a single-bracket ``[header.key]``
    subtable (the only nesting in this schema is plugin options).
    """
    lines.append(f"[[{header}]]" if array else f"[{header}]")
    subtables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    for key, value in table.items():
        # TOML has no null; a None-valued field (e.g. record.max_nvenc_sessions
        # left at auto) is written by *omitting* the key — the loader restores
        # its default on read.
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

    # Top-level scalar keys (e.g. ``backend``) must precede any table header in
    # TOML; preserve them verbatim so a round-trip save never drops them.
    scalars = {
        key: value
        for key, value in data.items()
        if isinstance(value, (str, int, float, bool))
    }
    if scalars:
        blocks.append("\n".join(f"{k} = {_toml_value(v)}" for k, v in scalars.items()))

    # Single-table sections, in schema order; each preserved verbatim so a GUI
    # camera-display save never wipes a section it doesn't touch.
    for header in ("record", "transcode", "gui"):
        table = data.get(header)
        if isinstance(table, dict) and table:
            block(header, table)
    for camera in data.get("cameras", []) or []:
        if isinstance(camera, dict):
            block("cameras", camera, array=True)
    for plugin in data.get("plugins", []) or []:
        # The loader also accepts a bare name (plugins = ["flywheel"]); write it
        # as a table, since this emits [[plugins]] headers.
        if isinstance(plugin, str):
            plugin = {"name": plugin}
        if isinstance(plugin, dict):
            block("plugins", plugin, array=True)
    # [[visualization]] is an array of tables (multiple named grids).
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
    """Return a copy of ``raw`` with the GUI's post-recording params overlaid.

    Sets ``[transcode].ffmpeg_params`` and ``[transfer].directory``/``checksum``
    to the live (Process-section) values so the recording folder's config
    snapshot drives ``octacam process``. Faithfully a *no-op* — the returned dict
    compares equal to ``raw`` — when the values already match the config, so an
    untouched snapshot stays a byte-verbatim copy. A missing
    ``[transcode]``/``[transfer]`` section is created only when a value actually
    diverges from its config default, so a rig with no ``[transfer]`` and a blank
    transfer directory never gains an empty section.
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
    """Normalize an extension argument to a tuple of suffixes.

    A plain string is a single suffix; an iterable (a mixed rig's set of
    per-vendor suffixes) is used as given. Empty tuples are tolerated (a system
    with no cameras) — the caller just globs nothing.
    """
    if isinstance(extension, str):
        return (extension,)
    return tuple(extension)


def write_pfs_files(
    target_dir: str | Path,
    pfs_by_serial: dict[str, str],
    extension: str | Mapping[str, str] = "pfs",
) -> None:
    """Write each ``<serial> -> param text`` to ``<serial>.<extension>``.

    ``extension`` is the single suffix for a one-vendor rig, or a
    ``serial -> suffix`` map for a mixed rig so each camera's params land in its
    own backend's format (``pfs`` for Basler, ``txt`` for FLIR/GenICam, ...).
    """
    target_dir = Path(target_dir)
    for serial, text in pfs_by_serial.items():
        ext = extension if isinstance(extension, str) else extension.get(serial, "pfs")
        atomic_write_text(target_dir / f"{serial}.{ext}", text)


def read_pfs_files(
    config_dir: str | Path, extension: str | Iterable[str] = "pfs"
) -> dict[str, str]:
    """Map ``<serial> -> param text`` for every per-camera file in ``config_dir``.

    The inverse of :func:`write_pfs_files`, used to reset live cameras back to
    the parameters the active config shipped. ``extension`` is the backend's
    parameter-file suffix (``pfs`` for Basler, ``txt`` for FLIR/GenICam, ...), or
    the set of suffixes for a mixed-vendor rig. Keyed by file stem (the serial for a
    ``<serial>.<extension>``), so auxiliary files like
    ``fictrac_camera_config.pfs`` are read too but simply never match a live
    serial.
    """
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
    """Copy per-camera files that are not ``<live-serial>.<extension>`` to a new dir.

    Preserves helper configs (e.g. ``fictrac_camera_config.pfs``) and the
    parameter files of cameras not currently opened, so the new dir is a
    complete preset. ``extension`` may be a single suffix or the set of suffixes
    a mixed-vendor rig uses.
    """
    src_dir, target_dir = Path(src_dir), Path(target_dir)
    if src_dir.resolve() == target_dir.resolve():
        return
    for ext in _extensions(extension):
        for src in sorted(src_dir.glob(f"*.{ext}")):
            if src.stem not in live_serials:
                atomic_write_text(target_dir / src.name, src.read_text())


# ----------------------------------------------------------------- new config dir


def safe_config_name(name: str) -> str:
    """Validate a new-config folder name as a single, safe path segment."""
    candidate = (name or "").strip()
    if (
        not candidate
        or candidate in (".", "..")
        or "/" in candidate
        or "\\" in candidate
        or os.sep in candidate
        or (os.altsep and os.altsep in candidate)
        or Path(candidate).name != candidate
    ):
        raise ValueError(f"Invalid config name: {name!r}")
    return candidate


def resolve_new_config_dir(
    active_dir: str | Path, name: str, *, overwrite: bool = False
) -> Path:
    """Resolve a new config dir as a sibling of the active one (where presets live)."""
    target = Path(active_dir).parent / safe_config_name(name)
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
