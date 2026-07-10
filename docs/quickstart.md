# Quickstart

Get octacam running and capture your first recording — first with emulated
cameras (no hardware needed), then the same steps on a real rig.

If octacam isn't installed yet, see [Installation](installation.md). In short,
with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/NeLy-EPFL/octacam.git
```

The Basler pylon runtime, OpenCV, and ffmpeg are bundled, so a Basler rig — and
the emulator below — work out of the box. (FLIR / Teledyne cameras need the
Spinnaker SDK installed separately; see [Camera backends](guide/backends.md).)

## 1. Try it with no hardware

Basler's runtime ships a camera emulator. Set `PYLON_CAMEMU` to the number of
fake cameras and launch the GUI with the bundled emulator config (from a clone of
the repository):

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
```

Your default browser opens to `http://127.0.0.1:8765/` with eight synthetic
cameras. You can preview, adjust the layout, and record exactly as you would with
real hardware.

!!! note
    The emulator is the easiest way to learn the GUI and validate a config
    before touching a rig. There is also a separate `fake` backend (synthetic
    in-memory frames) used for tests — see [Camera backends](guide/backends.md).

## 2. What a config directory is

Every command takes a **config directory**: a folder holding one
`octacam_config.toml` (camera names, display layout, GUI/encoder defaults, opt-in
plugins, and the camera [`backend`](guide/backends.md)) plus one per-camera sensor
file named by serial number.

```
configs/my_rig/
├── octacam_config.toml     # names, layout, [gui] defaults, [grid]/[nas], plugins
├── 40001978.pfs            # per-camera sensor parameters (Basler)
├── 40002335.pfs
└── …
```

The sensor-file extension depends on the backend:

| `backend` | SDK | per-camera parameter file |
| --------- | --- | ------------------------- |
| `basler` (default) | pypylon (bundled) | `<serial>.pfs` |
| `flir` | Spinnaker / PySpin | `<serial>.json` |
| `fake` | none (in-memory) | `<serial>.fake` |

The config is parsed tolerantly — a missing file, section, or field just falls
back to a default rather than failing. With no `[[cameras]]` entries, every
detected camera is used. See [Configuration](guide/configuration.md) for every
key, and [`configs/`](https://github.com/NeLy-EPFL/octacam/tree/main/configs) for
real examples.

!!! tip
    To confirm a rig's cameras are detected before launching, list them:

    ```bash
    octacam list-cameras                 # Basler (default backend)
    octacam list-cameras --backend flir  # FLIR / Teledyne
    ```

## 3. Preview and record in the GUI

On a real rig, point `gui` at your config directory:

```bash
octacam gui <config_dir>
```

The GUI opens in your browser. Use the tabs to:

- **View** — frame and arrange each camera (rotate / flip / position).
- **Camera** — tune exposure, gain, and the ROI (width/height/offset).
- **Record** — set fps, duration, and the save directory, then start/stop a
  recording. Live preview keeps running while you record.

An enabled plugin (e.g. flywheel, twophoton) adds its own tab; see
[Plugins](guide/plugins.md). Enable one per launch with `--plugin <name>`.

!!! note "One octacam per rig"
    A rig's cameras can only be opened by one process at a time, so octacam takes
    an instance lock on the config directory: a second `octacam gui` for the same
    rig is refused (on any `--port`). Bind address and port are configurable with
    `--host` / `--port` (default `127.0.0.1:8765`); pass `--no-browser` to skip
    the automatic browser launch.

## 4. Record headlessly

For a script or a remote box with no browser, record straight from the command
line. Options left unset fall back to the config's `[gui]` defaults:

```bash
octacam record <config_dir> --duration 10 --fps 100
```

octacam paces a **software** trigger at `--fps` by default; pass
`--trigger hardware` to use the trigger source configured in each camera's sensor
file instead. When it finishes, `record` prints one output file path per camera
on stdout. See [Recording](guide/recording.md) for every option.

## 5. What a recording produces

A recording writes one file per camera into the save directory, alongside a
summary:

- **`<camera_name>.mkv`** per camera — H.264 (libx264), true monochrome 4:0:0.
  This is the default `x264` codec; `--codec raw` dumps Mono8 for later
  transcoding instead.
- **`recording_summary.json`** — per-camera fps, start timestamp, and which frame
  indices were dropped, plus the session start time and recording settings.
- A per-frame timestamp CSV per camera **only** when you opt in with
  `--save-frame-timestamps` (off by default).

!!! note
    After a successful (non-aborted) recording, the trailing 3-digit group of the
    save directory auto-increments — e.g. `001-bhv` → `002-bhv` — so the next
    recording lands in a fresh folder.

The `dropped` count in the summary reflects only frames the encoder queue could
not accept (the host couldn't keep up), not frames the camera or USB transport
never delivered — enable `--save-frame-timestamps` and inspect the inter-frame
gaps to find those.

## Working remotely over SSH

Running octacam on a rig you reach over SSH? The single WebSocket that carries
preview and control tunnels cleanly, so forward the port to your laptop:

```bash
ssh -L 8765:127.0.0.1:8765 <rig-hostname> octacam gui <config_dir>
# then open http://localhost:8765 in your browser
```

octacam detects the SSH session and skips the automatic browser launch (the
browser would otherwise open on the rig).

## Next steps

Once you have recordings, turn them into compressed videos, composite grids, and
copies on shared storage:

```bash
octacam transcode --last   # transcode the most recent recording — no paths to type
```

See [Processing recordings](guide/processing.md) for the full transcode / grid /
NAS workflow, or the [CLI reference](reference/cli.md) for every command and flag.
</content>
</invoke>
