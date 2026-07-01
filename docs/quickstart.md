# Quickstart

This walks through octacam end to end: preview, record, and archive — first with
emulated cameras (no hardware needed), then on a real rig.

## 1. Try it with no hardware

Basler's runtime includes a camera emulator. Set `PYLON_CAMEMU` to the number of
fake cameras and launch the GUI with the bundled emulator config:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
```

Your browser opens to `http://127.0.0.1:8765` with eight synthetic cameras. You
can preview, adjust the layout, and record exactly as you would with real
hardware — recordings land in the directory the config's `[record]` section
points at.

!!! note
    The emulator is a great way to learn the GUI and validate a config before
    touching a rig. The [`fake` backend](guide/backends.md) is a similar
    synthetic option used for tests and CI.

## 2. Check your rig

On a real rig, first confirm octacam sees your install and cameras:

```bash
octacam doctor              # toolchain + all detected cameras
octacam doctor <config_dir> # …and validate a specific rig config
```

`doctor` lists detected cameras and bundled plugins, and checks the encoder,
storage, and runtime for problems — cross-checking the cameras a config declares
against the ones actually attached. It never opens a camera, so it is safe to
run while a session is live. See [`doctor` in the CLI reference](reference/cli.md#doctor).

## 3. What is a config directory?

Most commands take a **config directory**: a folder holding one
`octacam_config.toml` (camera names, display layout, recording/encoder/transfer
settings, and the [camera backend](guide/backends.md)) plus one per-camera sensor
file — `<serial>.pfs` for Basler, `<serial>.json` for FLIR.

```
configs/my_rig/
├── octacam_config.toml     # names, layout, [record]/[transcode]/[transfer]/…
├── 40001978.pfs            # per-camera sensor parameters (Basler)
├── 40002335.pfs
└── …
```

See [configs/](https://github.com/NeLy-EPFL/octacam/tree/main/configs) for
real examples, and the [Configuration reference](guide/configuration.md) for
every key.

## 4. Preview and record

```bash
octacam gui <config_dir>
```

The GUI opens in your browser. Use the tabs to frame each camera (**View**),
tune exposure/gain (**Camera**), and start/stop recording (**Record**). Frames
are written to video as you record. See the [Web GUI guide](guide/gui.md).

Prefer no browser (a script, or a remote box)? Record headlessly:

```bash
octacam record <config_dir> --duration 10 --fps 100
```

Either way, each recording writes its videos, a `recording_summary.json`, and a
snapshot of the rig config into its own folder. See [Recording](guide/recording.md).

## 5. Archive everything

Turn the day's recordings into transcoded videos, composite grid videos, and
copies on shared storage — one command, no paths to type:

```bash
octacam process --all
```

octacam remembers where it recorded, so `--all` processes every recent recording.
You can also pass explicit folders, or use `--last` / `--session`. See
[Processing recordings](guide/processing.md).

## Working remotely

Running octacam on a rig you reach over SSH? Tunnel the GUI to your laptop:

```bash
ssh -L 8765:127.0.0.1:8765 <rig-hostname> octacam gui <config_dir>
# then open http://localhost:8765 in your browser
```

More on this in the [Web GUI guide](guide/gui.md#remote-operation-over-ssh).
