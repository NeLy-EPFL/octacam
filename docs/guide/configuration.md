# Configuration

A **config directory** describes one rig. It holds two kinds of file:

- one `octacam_config.toml` — the camera [backend](backends.md), the camera list
  and display layout, GUI/recording defaults, opt-in [plugins](plugins.md), and
  the grid/NAS post-processing settings;
- one **per-camera sensor parameter file** per camera — `<serial>.pfs` for
  Basler, `<serial>.json` for FLIR, `<serial>.fake` for the fake backend —
  holding that camera's exposure, gain, ROI, and trigger settings.

You point every command at a config directory: `octacam gui <config_dir>`,
`octacam record <config_dir>`, and `octacam transcode --config <config_dir>`.

Everything in `octacam_config.toml` is optional and has a sensible default: a
missing or empty file simply uses **all detected cameras** with default
settings. See [`configs/`](https://github.com/NeLy-EPFL/octacam/tree/main/configs)
for complete, working examples.

!!! info "Parsing is deliberately tolerant"
    The loader never raises. A malformed file, section, or field is logged as a
    warning and replaced with its default, so a typo in the config can never stop
    the rig from starting. Unknown keys are ignored.

## Editing the config

You rarely need to write the whole file by hand. The web GUI's **View** and
**Camera** tabs tune the per-camera display layout and sensor parameters against
a live preview, and its **Save…** dialog writes them back. A GUI save
round-trips through the raw TOML and patches only the per-camera display fields
(and names) it changed — your `[gui]`, `[[plugins]]`, `[grid]`, and `[nas]`
sections are preserved verbatim, and every write is atomic (temp file +
rename), so a crash can never leave a truncated config behind.

## Top level

```toml
backend = "basler"   # "basler" (default) | "flir" | "fake"
```

| Key | Default | Meaning |
| --- | --- | --- |
| `backend` | `"basler"` | Which camera SDK this rig uses — one vendor per config directory. Any other value warns and falls back to `basler`. See [Camera backends](backends.md). |

The three backends and their per-camera parameter-file extensions:

| `backend` | Driver | Parameter file |
| --- | --- | --- |
| `basler` | pypylon (bundled) | `<serial>.pfs` |
| `flir` | Spinnaker SDK + PySpin (installed separately) | `<serial>.json` |
| `fake` | in-memory synthetic frames (for testing) | `<serial>.fake` |

## `[[cameras]]`

One entry per camera, keyed by serial number. Pins each camera's **name** and
its place in the preview and grid layout. With **no** `[[cameras]]` entries,
every detected camera is used with auto-assigned names.

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
| `serial_number` | — (required) | The camera's serial number. An unquoted integer serial is coerced to a string. |
| `name` | `""` | Display name; also the name you reference in a `[grid]` layout. Becomes the video filename stem (`<name>.mkv`), so it must be a single safe path segment. |
| `scale_x`, `scale_y` | `1.0` | Preview scale on each axis. A **negative** value is a flip (horizontal / vertical); the magnitude is ignored. |
| `rotation_deg` | `0.0` | Display rotation, clockwise, in 90° steps. |
| `window_x`, `window_y` | `-1.0` | Preview-tile top-left position, as a fraction of the canvas. `-1` means auto-place. |
| `window_width`, `window_height` | `-1.0` | Preview-tile size, as a fraction of the canvas. `-1` means auto-size. |

!!! note "Flips and rotation are baked into recordings"
    `scale_x`/`scale_y` (as flips) and `rotation_deg` form the camera's *display
    transform*. When a recording is made in the default `display` record form,
    that transform is baked into the video pixels; only 90° rotation steps can be
    baked (a non-multiple of 90 is dropped with a warning). See
    [Recording](recording.md).

Duplicate serial numbers, and duplicate or unsafe names (`..`, path separators),
are skipped with a warning; an unsafe name falls back to the serial number.

## `[gui]`

Defaults that seed the web GUI's controls and the `octacam record` fallbacks.
Every field is optional.

```toml
[gui]
fps_default = 100.0
duration_default = 5.0
duration_unit_default_index = 0        # 0 = seconds, 1 = minutes, 2 = hours
save_directory_default = "/data/octacam/%y%m%d/Fly1/001-bhv"
trigger_source_default_index = 0       # 0 = software, 1 = external
record_form_default = "display"        # "display" | "sensor"
save_frame_timestamps_default = false
```

### Recording defaults

| Key | Default | Meaning |
| --- | --- | --- |
| `fps_default` | `100.0` | Initial frame rate. |
| `fps_min`, `fps_max` | `0.01`, `1000.0` | Allowed frame-rate range in the GUI. |
| `duration_default` | `5.0` | Initial recording length, in the selected unit. |
| `duration_min`, `duration_max` | `0.01`, `1000000.0` | Allowed duration range. |
| `duration_unit_default_index` | `0` | Duration unit dropdown: `0` = seconds, `1` = minutes, `2` = hours. |
| `save_directory_default` | `"./"` | Save-directory template. `strftime` codes (e.g. `%y%m%d`) are expanded **when the config is parsed**. |
| `trigger_source_default_index` | `0` | Trigger dropdown: `0` = `software` (octacam paces a software trigger at `fps`), `1` = `external` (an outside master fires the cameras). See [Recording](recording.md). |
| `record_form_default` | `"display"` | `display` bakes each camera's rotation/flips into the video; `sensor` saves the raw, untransformed image. |
| `save_frame_timestamps_default` | `false` | When true, also writes a per-frame timestamp CSV per camera (debugging only). |

### Encoder defaults (x264 codec)

| Key | Default | Meaning |
| --- | --- | --- |
| `video_writer_default` | `""` | Explicit codec key, `"x264"` or `"raw"`. Preferred over the positional index below. |
| `video_writer_default_index` | `0` | Positional codec fallback: `0` = `x264` (H.264 MKV), `1` = `raw` (Mono8 dump). |
| `crf_default` | `18` | libx264 quality (lower is better; 0 = lossless). |
| `preset_default` | `"ultrafast"` | libx264 speed preset used at capture time. |
| `pix_fmt_default` | `"gray"` | Pixel format (true monochrome 4:0:0). |
| `x264_params_default` | `""` | Extra `-x264-params` passed verbatim to ffmpeg (e.g. `"keyint=30:scenecut=0"`). |

!!! tip
    These x264 defaults are the fast, near-lossless *capture* settings. The
    slower, higher-compression pass happens offline in
    [`octacam transcode`](processing.md).

### UI cadence and layout

| Key | Default | Meaning |
| --- | --- | --- |
| `display_refresh_interval_ms` | `33` | Preview refresh cadence (~30 Hz). |
| `record_countdown_timer_interval_ms` | `1000` | Recording countdown tick. |
| `check_record_started_timer_interval_ms` | `100` | Poll interval for "recording started". |
| `dock_min_width`, `dock_max_width` | `200`, `300` | Side-panel width bounds. |
| `save_dir_edit_height_factor` | `4` | Save-directory text box height factor. |

## `[grid]`

Defines the composite **grid video** `octacam transcode`/`octacam grid` build
from a folder's per-camera videos. See [Processing](processing.md).

```toml
[grid]
default = true    # auto-build grid.mp4 when a --config is passed to `octacam transcode`
layout = [
    ["camera_LF", "",          "camera_RF"],
    ["camera_LM", "camera_F",  "camera_RM"],
    ["camera_LH", "",          "camera_RH"],
]
```

| Key | Default | Meaning |
| --- | --- | --- |
| `default` | `false` | When true and a `--config` is supplied to `octacam transcode`, a `grid.mp4` is generated automatically (no `--grid` flag needed). |
| `layout` | — (required for the section) | A 2-D array (rows × columns) of camera `name`s. `""` is a black fill cell. **All rows must have the same length.** |

A layout cell naming a camera that is not in `[[cameras]]` is warned about and
renders as a black tile. If the whole `[grid]` section is malformed (missing
`layout`, ragged rows, non-string cells), it is dropped with a warning.

## `[nas]`

Where `octacam transcode`/`octacam nas` mirror finished outputs, preserving the
directory tree. Omit the section (or leave `path` blank) to disable automatic
NAS export.

```toml
[nas]
path = "/mnt/nas/matthias"      # destination root
local_base = "/home/nely/data/MD"  # local root stripped to mirror the tree
verify = true                    # checksum each copy before promoting it
```

| Key | Default | Meaning |
| --- | --- | --- |
| `path` | `""` | Destination root. Blank disables NAS export. |
| `local_base` | `""` | Local root stripped from the recording path to compute the sub-path reproduced under `path`. Blank uses just the folder name. |
| `verify` | `true` | Content-checksum each copied file before promoting it to its final name. `false` = faster size-only check. Overridden by `--nas-verify`/`--no-nas-verify`. |

## `[[plugins]]`

Enables opt-in serial-hardware [plugins](plugins.md). The default launch loads
**none**. Two plugins ship with octacam: `flywheel` (Arduino stepper-motor
controller) and `twophoton` (Arduino hardware camera trigger for a 2-photon
rig). Their serial dependency (pyserial) ships by default, so no extra install
is needed.

```toml
[[plugins]]
name = "flywheel"

[plugins.options]
device = "/dev/ttyACM0"
baud = 115200
```

| Key | Meaning |
| --- | --- |
| `name` | Plugin name: `flywheel` or `twophoton` (the legacy alias `arduino` still resolves to `flywheel`). |
| `[plugins.options]` | Optional per-plugin settings sub-table (e.g. `device`, `baud`; `twophoton` also takes `default_fps`, `default_duration_ms`). |

A bare-name array is also accepted:

```toml
plugins = ["flywheel", "twophoton"]
```

Plugins can also be enabled per launch with `--plugin <name>` on `gui`/`record`
(repeatable, adds to the config selection), and disabled for one run with
`--no-plugins`. Run `octacam list-plugins` to see which are available.

## Per-camera sensor parameter files

Alongside `octacam_config.toml`, each camera has one **sensor parameter file**
named after its serial number, holding the device-side settings (exposure, gain,
ROI, trigger). The file format matches the active `backend`:

| Backend | File | Format |
| --- | --- | --- |
| `basler` | `<serial>.pfs` | native pylon feature-persistence stream |
| `flir` | `<serial>.json` | JSON (`params`, `trigger_mode`, `trigger_source`) |
| `fake` | `<serial>.fake` | JSON (same shape as FLIR) |

These files are written by the GUI's **Save…** dialog (from the values you tune
in the **Camera** tab) and loaded when octacam opens a camera. The editable
sensor parameters are:

| GUI field | GenICam node | Notes |
| --- | --- | --- |
| `width`, `height` | `Width`, `Height` | Geometry; only writable while the camera is **not** grabbing (the GUI cycles the preview around the change). |
| `exposure` | `ExposureTime` | Writable live. |
| `gain` | `Gain` | Writable live. |
| `offset_x`, `offset_y` | `OffsetX`, `OffsetY` | ROI origin; writable live. |

!!! note "Missing parameter file"
    If a camera's `<serial>.<ext>` is absent, octacam opens it at its current
    on-device defaults and logs a warning — recording still works.

!!! warning "Trigger source is normalized on save"
    Live preview always drives the camera with a software trigger. When the
    Save… dialog persists a parameter file it **restores the camera's original
    (config) trigger source** and sets the frame-trigger mode back off, so a save
    taken during a software-triggered preview never bakes `TriggerSource=Software`
    into the file — which would otherwise make a later external-trigger recording
    silently never start.

Auxiliary `.pfs`/`.json` files in the directory whose stem is not a live camera
serial (for example a shared `fictrac_camera_config.pfs`) are read but simply
never match a camera, and are carried along when a new preset directory is
created from the GUI.

## Example configs

The [`configs/`](https://github.com/NeLy-EPFL/octacam/tree/main/configs)
directory ships complete rigs you can copy and adapt:

| Directory | What it shows |
| --- | --- |
| `2p_1` | 7-camera Basler 2-photon behavior rig with a 3×3 `[grid]` and a `[nas]` template. |
| `emulate_8_cameras` | 8 emulated Basler cameras — run hardware-free with `PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras`. |
| `flir_example` | `backend = "flir"` with no pinned `[[cameras]]` (uses every detected FLIR); documents the `<serial>.json` parameter files. |
| `scape_fly_facing_left_wide` | 8 Basler cameras, some mirrored via `scale_x = -1`, with a grid and NAS section. |
