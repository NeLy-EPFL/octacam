#!/usr/bin/env python3
"""Convert an octacam_config.yaml/.yml to octacam_config.toml.

octacam switched its config format from YAML to TOML. Run this once per rig
(it needs PyYAML + tomli-w, which are not runtime dependencies):

    uv run --with pyyaml --with tomli-w \
        python scripts/yaml2toml_config.py <config_dir-or-yaml>...

For each argument (a config directory or a YAML file) it writes a sibling
octacam_config.toml. The old YAML file is left in place — delete it once you've
checked the result.
"""

import sys
from pathlib import Path

import tomli_w
import yaml


def _stringify(value: object) -> object:
    # YAML read unquoted numeric serials as ints and date-like paths as dates;
    # TOML keeps those as explicit strings.
    import datetime

    if isinstance(value, (int, float, datetime.date)) and not isinstance(value, bool):
        return str(value)
    return value


def _normalize_cameras(cameras: object) -> list:
    result = []
    for cam in cameras or []:
        if not isinstance(cam, dict):
            continue
        cam = dict(cam)
        for key in ("serial_number", "name"):
            if key in cam:
                cam[key] = _stringify(cam[key])
        result.append(cam)
    return result


def _normalize_plugins(plugins: object) -> list:
    # YAML allowed a bare name or a single-key {name: options} map per entry;
    # TOML uses an array of tables with an explicit name (+ optional options).
    result = []
    for entry in plugins or []:
        if isinstance(entry, str):
            result.append({"name": entry})
        elif isinstance(entry, dict) and len(entry) == 1:
            ((name, options),) = entry.items()
            table: dict = {"name": name}
            if isinstance(options, dict):
                table["options"] = options
            result.append(table)
    return result


def convert(yaml_path: Path) -> Path:
    data = yaml.safe_load(yaml_path.read_text()) or {}
    out: dict = {}
    if isinstance(data.get("gui"), dict):
        gui = dict(data["gui"])
        for key in ("save_directory_default", "video_writer_default"):
            if key in gui:
                gui[key] = _stringify(gui[key])
        out["gui"] = gui
    cameras = _normalize_cameras(data.get("cameras"))
    if cameras:
        out["cameras"] = cameras
    plugins = _normalize_plugins(data.get("plugins"))
    if plugins:
        out["plugins"] = plugins
    toml_path = yaml_path.with_name("octacam_config.toml")
    toml_path.write_text(tomli_w.dumps(out))
    return toml_path


def _yaml_for(arg: Path) -> Path | None:
    if arg.is_file():
        return arg
    for name in ("octacam_config.yaml", "octacam_config.yml"):
        if (arg / name).exists():
            return arg / name
    return None


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit("usage: yaml2toml_config.py <config_dir-or-yaml>...")
    for raw in argv:
        src = _yaml_for(Path(raw))
        if src is None:
            print(f"skip: no octacam_config.y(a)ml under {raw}")
            continue
        print(f"wrote {convert(src)}")


if __name__ == "__main__":
    main(sys.argv[1:])
