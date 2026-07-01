# Configuration

A **config directory** describes one rig. It holds:

- one `octacam_config.toml` — camera names, display layout, and the
  recording/encoding/transfer settings, plus the [camera backend](backends.md);
- one per-camera **sensor parameter file** — `<serial>.pfs` for Basler,
  `<serial>.json` for FLIR (written by the GUI's *Save…* dialog).

Everything below is optional and has a sensible default — an empty or missing
`octacam_config.toml` uses all detected cameras with defaults. octacam parses the
file **leniently**: a malformed value is warned about and falls back to its
default rather than crashing the rig.

See [configs/](https://github.com/NeLy-EPFL/octacam/tree/main/configs) for
complete, working examples.

## Scaffolding a config

You rarely have to write the file by hand. `octacam config` walks you through it:
it auto-detects the connected cameras (across every installed backend, so a
mixed Basler+FLIR rig is picked up in one go), prompts for the record and
transfer settings, writes an `octacam_config.toml`, and snapshots each camera's
current sensor parameters into a per-camera file.

```bash
octacam config <config_dir>    # or omit the dir to be prompted for a name
```

By default the wizard opens each detected camera once to save its current sensor
parameters into a `<serial>.pfs` (Basler) or `<serial>.json` (FLIR) file. A
camera that is busy — held by a live session — is skipped with a warning; you
can capture its parameters later from the GUI's *Save…* dialog. Pass
`--no-snapshot-params` to skip that step entirely (enumeration only, no camera is
opened). Pass `--backend basler|flir|fake` to pin the rig to one vendor instead
of auto-detecting, or `--force` to overwrite an existing file. The wizard
deliberately leaves the **visual**
settings — per-camera window placement, rotation, and the grid — to `octacam
gui`, which tunes them against a live preview; run it next on the new directory.
Everything the wizard writes stays hand-editable afterward.

## Top level

```toml
# backend = "auto"   # "auto" (default) | "basler" | "flir" | "fake"
```

`backend` is optional. Omit it (or set `"auto"`) and the rig auto-detects every
installed backend and uses whatever is connected — Basler and FLIR cameras can
run together in one config, each keeping its own parameter-file format. Set a
concrete value to pin the rig to a single vendor. See
[Camera backends](backends.md).

## `[record]`

Controls capture and how frames are written.

```toml
[record]
fps = 100.0
duration = 5.0
duration_unit = "seconds"      # frames | seconds | minutes | hours
trigger_source = "software"    # software | external

directory = "/data/octacam"
relative_directory = "%y%m%d-genotype/Fly1/001-bhv"   # strftime template

save_method = "ffmpeg"         # ffmpeg | raw
ffmpeg_params = "-c:v libx264 -preset ultrafast -crf 18 -pix_fmt gray"
save_transformed = true
save_timestamps = false
```

| Key | Default | Meaning |
| --- | --- | --- |
| `fps` | `100.0` | Frame rate. |
| `duration` | `5.0` | Recording length, in `duration_unit`. |
| `duration_unit` | `"seconds"` | `frames` \| `seconds` \| `minutes` \| `hours`. |
| `trigger_source` | `"software"` | `software` or an `external` hardware trigger. |
| `directory` | `"./"` | Base save directory. |
| `relative_directory` | `""` | Sub-path appended to `directory`; a `strftime` template (e.g. `%y%m%d/…`), so trials sort into a date/subject/trial tree. |
| `save_method` | `"ffmpeg"` | `ffmpeg` (encoded video) or `raw` (a `.raw` byte dump per camera). |
| `ffmpeg_params` | ultrafast x264, see above | Encoder args used at record time. |
| `save_transformed` | `true` | Bake each camera's rotation/flips into the file (see [Recording](recording.md#transformed-vs-raw-frames)). |
| `save_timestamps` | `false` | Also write a per-frame timestamp CSV. |

## `[transcode]`

Encoder args `octacam process` uses to re-encode recordings to archival mp4.
Usually a slower, higher-quality preset than the record-time params.

```toml
[transcode]
ffmpeg_params = "-c:v libx264 -preset veryslow -crf 20 -pix_fmt gray"
```

## `[transfer]`

Where `octacam process` copies finished outputs. Omit the whole section to
disable transfer.

```toml
[transfer]
directory = "/mnt/store/matthias"    # strftime %-codes expand here too
checksum = true                      # content-verify each copy (default)
```

| Key | Default | Meaning |
| --- | --- | --- |
| `directory` | `""` | Destination base folder; recordings mirror into it under their relative directory. Blank disables transfer. |
| `checksum` | `true` | blake2b content-verify each copy. `false` = faster size-only verify. |

See [Processing → Transfer](processing.md#transfer).

## `[[cameras]]`

One entry per camera, keyed by serial number. Pins each camera's **name** and its
place in the preview/grid layout. With no `[[cameras]]` entries, every detected
camera is used with auto-assigned names and a default layout.

```toml
[[cameras]]
serial_number = "40001978"
name = "camera_LF"
scale_x = 1
scale_y = 1
rotation_deg = 0
window_x = 0.5
window_y = 0.25
window_width = 0.5
window_height = 0.25
```

| Key | Default | Meaning |
| --- | --- | --- |
| `serial_number` | — (required) | The camera's serial number. |
| `name` | `""` | Display name; also the name you use in grid layouts. |
| `scale_x`, `scale_y` | `1.0` | Preview scale. |
| `rotation_deg` | `0.0` | Display rotation (baked into recordings when `save_transformed`). |
| `window_x`, `window_y`, `window_width`, `window_height` | `-1.0` | Preview tile placement as fractions of the canvas; `-1` means auto-place. |

!!! tip
    You normally set these from the GUI's **View** and **Camera** tabs and save
    them back, rather than editing the TOML by hand.

## `[[visualization]]`

Defines the composite **grid** video(s) `octacam process` builds. List several
entries to produce several grids. See
[Processing → Grid video](processing.md#grid-video).

```toml
[[visualization]]
name = "grid.mp4"            # output filename inside each recording folder
layout = [
    ["camera_LF", "",          "camera_RF"],
    ["camera_LM", "camera_F",  "camera_RM"],
    ["camera_LH", "",          "camera_RH"],
]
# ffmpeg_params = ""         # optional per-grid encoder override
```

Each cell is a camera `name`; `""` is a black fill. All rows must have the same
number of columns. With no `[[visualization]]`, a near-square layout is derived
from the rig's cameras.

## `[[plugins]]`

Enables opt-in [plugins](plugins.md). The default launch loads none.

```toml
# Bare names work too: plugins = ["flywheel"]
[[plugins]]
name = "flywheel"
options = { device = "/dev/ttyACM0", baud = 115200 }
```

## `[gui]`

```toml
[gui]
display_refresh_interval_ms = 33     # preview refresh cadence (~30 Hz)
```

## Validate a config

Check a directory before recording — this cross-checks declared vs detected
cameras and resolves the save/transfer paths:

```bash
octacam doctor <config_dir>
```

See [`doctor`](../reference/cli.md#doctor).
