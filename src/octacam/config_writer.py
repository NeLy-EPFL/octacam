"""Write octacam_config.toml (and camera .pfs files) back to disk.

The stdlib ships ``tomllib`` for *reading* TOML but no writer, and octacam
keeps its dependency set deliberately lean, so this module hand-serializes the
small, fixed config schema (``[gui]`` / ``[[cameras]]`` / ``[[plugins]]`` /
``[grid]`` / ``[nas]``).

Two design choices keep saves faithful:

* The writer starts from the **raw parsed TOML** (``tomllib.loads`` of the
  existing file) and patches only the per-camera display fields the GUI
  changed. ``[gui]`` and ``[[plugins]]`` are preserved verbatim. This matters
  because :func:`octacam.config._finalize` ``strftime``-expands
  ``gui.save_directory_default`` at parse time, so re-emitting it from the
  parsed model would bake today's date into the saved template.
* Every file is written through a temp file + ``os.replace`` so a crash can
  never leave a truncated config behind.
"""

import contextlib
import copy
import os
import tempfile
import tomllib
from pathlib import Path

from octacam.config import find_config_file

# Per-camera display fields the GUI may change (sensor params live in .pfs).
DISPLAY_FIELDS = (
    "scale_x",
    "scale_y",
    "rotation_deg",
    "window_x",
    "window_y",
    "window_width",
    "window_height",
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
        elif ord(ch) < 0x20:
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
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
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

    gui = data.get("gui")
    if isinstance(gui, dict) and gui:
        block("gui", gui)
    for camera in data.get("cameras", []) or []:
        if isinstance(camera, dict):
            block("cameras", camera, array=True)
    for plugin in data.get("plugins", []) or []:
        if isinstance(plugin, dict):
            block("plugins", plugin, array=True)
    # Preserve the [grid]/[nas] post-processing sections so a GUI save (which
    # round-trips through here for camera-display tweaks) never wipes them.
    for header in ("grid", "nas"):
        table = data.get(header)
        if isinstance(table, dict) and table:
            block(header, table)

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


def write_pfs_files(
    target_dir: str | Path, pfs_by_serial: dict[str, str], extension: str = "pfs"
) -> None:
    target_dir = Path(target_dir)
    for serial, text in pfs_by_serial.items():
        atomic_write_text(target_dir / f"{serial}.{extension}", text)


def read_pfs_files(config_dir: str | Path, extension: str = "pfs") -> dict[str, str]:
    """Map ``<serial> -> param text`` for every per-camera file in ``config_dir``.

    The inverse of :func:`write_pfs_files`, used to reset live cameras back to
    the parameters the active config shipped. ``extension`` is the active
    backend's parameter-file suffix (``pfs`` for Basler, ``json`` for FLIR, ...).
    Keyed by file stem (the serial for a ``<serial>.<extension>``), so auxiliary
    files like ``fictrac_camera_config.pfs`` are read too but simply never match
    a live serial.
    """
    config_dir = Path(config_dir)
    out: dict[str, str] = {}
    if not config_dir.is_dir():
        return out
    for path in sorted(config_dir.glob(f"*.{extension}")):
        try:
            out[path.stem] = path.read_text()
        except OSError:
            continue
    return out


def copy_auxiliary_pfs(
    src_dir: str | Path,
    target_dir: str | Path,
    live_serials: set[str],
    extension: str = "pfs",
) -> None:
    """Copy per-camera files that are not ``<live-serial>.<extension>`` to a new dir.

    Preserves helper configs (e.g. ``fictrac_camera_config.pfs``) and the
    parameter files of cameras not currently opened, so the new dir is a
    complete preset.
    """
    src_dir, target_dir = Path(src_dir), Path(target_dir)
    if src_dir.resolve() == target_dir.resolve():
        return
    for src in sorted(src_dir.glob(f"*.{extension}")):
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
