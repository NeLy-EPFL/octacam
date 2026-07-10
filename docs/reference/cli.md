# CLI reference

octacam installs a single `octacam` command with seven subcommands:

```
octacam [GLOBAL OPTIONS] COMMAND [ARGS]
```

| Command | Purpose |
| --- | --- |
| [`gui`](#gui) | Launch the live web GUI for a rig. |
| [`list-cameras`](#list-cameras) | List detected cameras. |
| [`list-plugins`](#list-plugins) | List bundled plugins and whether each can load. |
| [`record`](#record) | Record headlessly from a rig. |
| [`transcode`](#transcode) | Re-encode recordings to compressed video (with optional grid/NAS steps). |
| [`grid`](#grid) | Build a composite grid video from transcoded recordings. |
| [`nas`](#nas) | Copy recordings to a NAS or any destination. |

!!! tip "`-h` / `--help` is authoritative"
    Every command accepts `-h` or `--help`, which prints the exact option list
    for the version you have installed. This page documents octacam
    **0.2.0.dev0**.

## Global options

These options appear before the subcommand (e.g. `octacam --log-level debug gui`).
Running `octacam` with no subcommand prints the help and exits.

| Option | Default | Purpose |
| --- | --- | --- |
| `--log-level`, `-l` | `info` | Logging verbosity: `debug` \| `info` \| `warning` \| `error`. |
| `--version` | — | Print the version and exit. |
| `-h`, `--help` | — | Show help (on the root or any subcommand). |

Logs are written to stderr so stdout stays clean for the machine-readable output
of `list-cameras`, `record`, `transcode`, `grid`, and `nas`.

## `gui`

```bash
octacam gui [CONFIG_DIR]
```

Launch the octacam web GUI for the cameras in `CONFIG_DIR` (default: the current
directory). The GUI serves preview, telemetry, recording control, and any loaded
plugin tabs over one WebSocket.

| Option | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. Keep the loopback default and reach the GUI remotely with `ssh -L 8765:127.0.0.1:8765 <rig-hostname>`. |
| `--port` | `8765` | Port to bind; override if it clashes with other software. |
| `--no-browser` | off | Don't auto-open a browser. Auto-open is also skipped over SSH and on headless (no-display) sessions. |
| `--plugin <name>` | — | Enable a plugin (repeatable); adds to the config's `plugins`, e.g. `--plugin flywheel`. See [`list-plugins`](#list-plugins). |
| `--no-plugins` | off | Disable all plugins for this launch, ignoring the config. |

!!! note "One octacam per rig"
    `gui` takes an exclusive lock keyed on the config directory, so a second
    `octacam gui <config_dir>` for the same rig is refused even on a different
    `--port`. The chosen port is also probed up front, so a port clash fails
    immediately instead of after opening the cameras.

On shutdown (Ctrl+C or the in-GUI shutdown control), if anything was recorded
this session octacam prints the ready-to-run `transcode` commands for the
recorded folders.

## `list-cameras`

```bash
octacam list-cameras
```

List detected cameras, one per line. Output is tab-separated: `model<TAB>serial`
for the Basler backend, and `serial<TAB>backend` for the others.

| Option | Default | Purpose |
| --- | --- | --- |
| `--backend <name>` | `basler` | Camera backend to enumerate: `basler`, `flir`, or `fake`. |

!!! tip "Run without hardware"
    Set `PYLON_CAMEMU=N` to enumerate `N` emulated Basler cameras, or use
    `--backend fake` for the synthetic test backend (serials come from
    `OCTACAM_FAKE_CAMERAS`, default `FAKE-0,FAKE-1`).

## `list-plugins`

```bash
octacam list-plugins
```

List the bundled, opt-in plugins and whether each can load. Takes no options.
Output is tab-separated: `name<TAB>status<TAB>summary`, where `status` is
`available` (the plugin's dependencies are present — the bundled plugins ship
their deps by default) or `unavailable` (the summary carries the reason).

Enable a plugin with `--plugin <name>` on [`gui`](#gui) or [`record`](#record),
or with a `[[plugins]]` entry in the rig config.

## `record`

```bash
octacam record [CONFIG_DIR]
```

Record videos headlessly from the cameras in `CONFIG_DIR` (default: the current
directory). One output path per camera is printed on stdout when done.

Every option that defaults to *from config* falls back to the rig's
`octacam_config.toml` (`[gui]` defaults) when omitted.

| Option | Default | Purpose |
| --- | --- | --- |
| `--fps`, `-f` | from config | Frame rate. |
| `--duration`, `-d` | from config | Recording duration in seconds. |
| `--output`, `-o` | from config | Save directory. |
| `--codec` | `x264` | `x264`: ffmpeg H.264 MKV (gray 4:0:0); `raw`: Mono8 dump for later [`transcode`](#transcode). |
| `--crf` | from config | x264 quality (lower = better; `0` = lossless). |
| `--preset` | from config | x264 speed preset. `ultrafast` is the only one validated at 8 cameras × 150 fps; slower presets compress better. |
| `--x264-params` | from config | Extra libx264 options passed as ffmpeg `-x264-params`, e.g. `"keyint=30:scenecut=0"`. |
| `--trigger` | `software` | `software`: trigger from a timer thread at `--fps`; `hardware`: use the trigger source configured in the `.pfs` files. |
| `--record-form` | from config | `display`: bake each camera's rotation/flips into the video; `sensor`: save the raw, untransformed image. |
| `--save-frame-timestamps` / `--no-save-frame-timestamps` | from config | Also write a per-frame timestamp CSV per camera, for debugging. |
| `--plugin <name>` | — | Enable a plugin (repeatable); adds to the config's `plugins`. |
| `--no-plugins` | off | Disable all plugins for this run. |

!!! note "`--trigger hardware`"
    With `--trigger hardware`, octacam does not pace the cameras itself: it arms
    each camera's config-native trigger source and waits for an external master
    to fire. If the external trigger never fires within the window, cameras can
    finish with zero frames (a header-only file); octacam flags those on stdout
    and skips them during transcode.

## `transcode`

```bash
octacam transcode [PATHS...]
```

Transcode recordings to compressed video, optionally building a composite grid
video and copying results to a NAS afterwards. Recordings use a fast capture
preset, so transcode always re-encodes (it never stream-copies) with the slower
`veryslow` default preset where compression is actually gained.

`PATHS` may mix recording folders and individual video files (`.mkv`/`.raw`). A
folder with a `recording_summary.json` is driven by it (which files, and each
camera's display transform); a folder without one has its loose `.mkv`/`.raw`
files transcoded with defaults and no transform.

### Selecting what to transcode

Instead of `PATHS`, select folders from the recording cache. These selectors are
**mutually exclusive** with each other and cannot be combined with explicit
`PATHS`. They silently skip any folder that has since been deleted.

| Option | Purpose |
| --- | --- |
| `--last`, `--last-recording` | The most recent recording folder. |
| `--session`, `--last-session` | Every folder from the last GUI session. |
| `--session-id <id>` | Every folder from one exact session id (the value the GUI prints on exit; unlike `--session`, it is not hijacked by a later recording). |
| `--all` | Every recording folder still in the cache (all sessions, all days). |

Passing neither `PATHS` nor a selector is an error.

### Encoding options

| Option | Default | Purpose |
| --- | --- | --- |
| `-r`, `--recursive` | off | Recurse into the given folders. |
| `--as-displayed` / `--as-saved` | `--as-saved` | `--as-displayed` applies each video's recorded display transform (skipped when already baked in); default reproduces the video as saved. |
| `--format` | `mp4` | Output container. |
| `--crf` | `20` | x264 quality. |
| `--preset` | `veryslow` | x264 speed preset. |
| `--pix-fmt` | `gray` | Pixel format. |
| `--x264-params` | `""` | Extra libx264 `-x264-params`, e.g. `"keyint=30:scenecut=0"`. |
| `--remove-source` | off | Delete each source `.mkv`/`.raw` (and a `.raw`'s `.json` sidecar) once it transcodes successfully. The `recording_summary.json` is kept. |
| `--progress-style` | `octacam` | `octacam`: reformat ffmpeg's progress into an octacam-style progress bar. `ffmpeg`: stream ffmpeg's own output verbatim. |

### Grid and NAS post-processing

After transcoding, octacam can build a grid video and mirror results to a NAS.
CLI flags override the corresponding `[grid]`/`[nas]` sections of `--config`.

| Option | Default | Purpose |
| --- | --- | --- |
| `--grid` / `--no-grid` | from config | Generate a composite grid video after each folder. When omitted, the `[grid] default` in `--config` decides; `--no-grid` always disables it. |
| `--config`, `-C <dir>` | — | Rig config directory (`octacam_config.toml`). Supplies the grid layout, whether grid/NAS run by default (`[grid] default` and `[nas] path`), and the NAS local-base. |
| `--nas-path <path>` | from config | Copy results to this destination after each folder. Overrides `[nas] path` from `--config`. |
| `--nas-local-base <path>` | from config | Local root to strip for NAS path mirroring. Overrides `[nas] local_base` from `--config`. |
| `--nas-verify` / `--no-nas-verify` | from config | Content-verify each NAS copy (checksum) before promoting it. When omitted, the `[nas] verify` value in `--config` decides (default on). |
| `--nas-checksum` | off | Decide whether an already-present NAS file can be skipped by full checksum rather than size (repair mode). |
| `--dry-run` | off | For `--grid` and `--nas-path`: log what would be done without running ffmpeg or copying files. Transcoding still runs normally. |

!!! note "Interrupt-safe"
    A Ctrl-C stops the batch where it stands; the in-flight file's partial output
    is discarded, and files already finished keep their outputs. octacam warns if
    a `transcode` is already running on the machine when you start a `gui` or
    `record` session, since transcoding is CPU-heavy and can cause dropped frames.

## `grid`

```bash
octacam grid PATHS... [OPTIONS]
```

Generate a composite grid video from already-transcoded recording folders. At
least one `PATHS` argument is required, and each path must exist.

Camera names and positions come from the `[grid]` section of the rig's
`octacam_config.toml` (`--config`). Without a config, octacam falls back to its
built-in 7-camera default layout. Missing cameras are filled with black frames,
and the output is always `yuv420p` for QuickTime / Keynote compatibility.

| Option | Default | Purpose |
| --- | --- | --- |
| `-r`, `--recursive` | off | Search each path recursively for recording directories (identified by `recording_summary.json`) and generate a grid in each. Without this flag every argument must itself be a recording directory. |
| `--config`, `-C <dir>` | — | Config directory whose `octacam_config.toml` contains a `[grid]` layout section. When omitted the built-in 7-camera default is used. |
| `--output-name`, `-o <name>` | `grid.mp4` | Output filename inside each folder. |
| `--crf` | `20` | x264 quality. |
| `--preset` | `veryslow` | x264 speed preset. |
| `--pix-fmt` | `yuv420p` | Pixel format for the grid video. `yuv420p` is required for QuickTime / Keynote compatibility. |
| `--dry-run` | off | With `-r`: list the recording directories that would be processed. Always: log the ffmpeg command without running it. |

## `nas`

```bash
octacam nas PATHS... --nas-path PATH [OPTIONS]
```

Copy recordings to a NAS or any destination, preserving the directory tree. At
least one `PATHS` argument is required, and each path must exist. `--nas-path` is
**required**.

Copies are atomic and resumable: each file is written to a temp and only swapped
onto its final name once whole and (by default) checksum-verified, and a re-run
skips files already present. The `.mp4` files (individual cameras and `grid.mp4`
if present) and the `recording_summary.json` from each recording directory are
copied.

| Option | Default | Purpose |
| --- | --- | --- |
| `--nas-path <path>` | **required** | NAS destination root, e.g. `/mnt/nas/matthias`. |
| `--nas-local-base <path>` | — | Local root to strip when computing the NAS sub-path, so the directory tree is mirrored. With `--nas-local-base /home/nely/data/MD`, a recording at `/home/nely/data/MD/260624_/Fly1/001-bhv` lands at `<nas-path>/260624_/Fly1/001-bhv`. Omit to use only the folder name. |
| `-r`, `--recursive` | off | Search each path recursively for recording directories (identified by `recording_summary.json`) and copy each one. |
| `--verify` / `--no-verify` | `--verify` | Content-verify each copied file (checksum) before promoting it to its final name. `--no-verify` falls back to a size-only check for trusted/fast links. |
| `--checksum` | off | When a file already exists on the NAS, decide whether to skip it by full checksum rather than size (repair mode: re-copies files whose bytes differ). |
| `--dry-run` | off | Log what would be copied without touching any files. |

!!! tip "Automatic tree mirroring"
    When `--nas-local-base` is omitted and several recordings are copied at once,
    their common parent is used as the base automatically, so same-named trials
    (e.g. two `001-bhv`) do not collide on the NAS.

## Environment variables

| Variable | Effect |
| --- | --- |
| `PYLON_CAMEMU` | Number of emulated Basler cameras to summon (run without hardware). |
| `OCTACAM_FAKE_CAMERAS` | Comma-separated serials for the `fake` backend (default `FAKE-0,FAKE-1`). |
| `OCTACAM_FFMPEG` | Path to an ffmpeg binary to use instead of the bundled one (`imageio-ffmpeg`), or a system `ffmpeg` on `$PATH`. |
| `OCTACAM_CACHE_DIR` | Override the recording-cache location. |
| `XDG_CACHE_HOME` | Base for the recording cache when `OCTACAM_CACHE_DIR` is unset (falls back to `~/.cache/octacam`). |
