# Web GUI

`octacam gui <config_dir>` launches a browser-based control panel for the
cameras in a config directory. One process serves a single-page app, a small
REST control plane, and a WebSocket that streams a **live preview of every
camera at once — including while recording** — and carries all telemetry and
controls. It is the interactive counterpart to the headless
[`octacam record`](recording.md).

```bash
octacam gui <config_dir>
```

By default it binds to `http://127.0.0.1:8765` and opens your default browser
once the server is ready. If the config directory is omitted it defaults to the
current directory (`.`).

!!! tip "Try it without hardware"
    Set `PYLON_CAMEMU=N` to spin up `N` emulated Basler cameras, so the whole
    GUI runs with no rig attached:

    ```bash
    PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
    ```

## Launch options

| Option | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. Keep the loopback default and reach the GUI remotely over SSH (see below). |
| `--port` | `8765` | Port to bind; change it if it clashes with other software. |
| `--no-browser` | off | Don't open a browser automatically (also skipped over SSH and on headless hosts). |
| `--plugin <name>` | — | Enable a [plugin](plugins.md) for this launch (repeatable; adds to the config's selection). |
| `--no-plugins` | off | Disable all plugins for this launch, ignoring the config. |

The root command also accepts `--log-level`/`-l` (`debug`/`info`/`warning`/`error`)
and `--version`.

## The live preview

The main area is a grid of camera tiles, each showing a downscaled JPEG preview.
Frames are throttled to the config's display refresh interval, sent newest-only
per browser, and nothing is encoded when no browser is connected — so a slow
client (or a congested `ssh -L` tunnel) simply sees fewer frames without making
the rig burn CPU. Each tile shows the camera's live frame rate and dropped-frame
count, and flags a camera whose writer has failed.

Each tile behaves like a little window:

- **Move** — drag the title bar.
- **Resize** — drag an edge or corner.
- **Maximize / restore** — double-click the title bar, or use the tile's button.
- **Raise** — click a tile to bring it above its neighbours.
- **Rename** — double-click the camera name to edit it in place. The name
  becomes the video file's stem, so it must be a safe, unique filename.

The **Display cross** checkbox (View tab) overlays centering crosshairs on every
tile.

!!! note
    The tile layout and per-camera rotation/flip are visual only until you save
    them — see [Saving a configuration](#saving-a-configuration).

## The tabs

The sidebar holds three built-in tabs plus a tab for each enabled plugin. Plugin
tabs (**Flywheel**, **2-Photon**) appear only when the matching
[plugin](plugins.md) is loaded.

### Record

Configure and start/stop a recording on all cameras at once:

- **Duration** — a value plus a unit (`s` / `min` / `h`).
- **FPS** — target capture rate applied to every camera.
- **Save directory** — the folder on the rig where video files are written.
  **Browse…** opens a server-side directory picker (navigate, or create a new
  subfolder); free disk space for the chosen drive is shown alongside.
- **Trigger source** — `software` (the server paces each frame from a timer at
  the target FPS) or `external` (every frame is driven by a hardware trigger
  configured in the per-camera sensor files). See
  [Recording](recording.md) for the trigger model.
- **Format** — the container/codec (`x264` H.264 MKV, or `raw` Mono8 dump for a
  later `octacam transcode`).
- **Saved image** — `display` bakes the on-screen rotation/flip into the file,
  or `sensor` saves the raw, un-rotated sensor image.
- **Per-frame timestamp CSV** — also write a per-camera timestamp CSV (off by
  default; for debugging).

The **Start recording** button arms all cameras; while a recording is in
progress the fields lock and a status line and progress bar track it. Overwriting
an existing save directory prompts for confirmation.

### Camera

Per-camera sensor parameters, applied to the selected camera or to **All** at
once:

- **Width** / **Height** — the sensor region of interest (changing geometry
  briefly cycles the preview grab, since the SDK refuses it while grabbing).
- **Exposure** — exposure time per frame (µs).
- **Advanced** — **Gain**, **Offset X**, and **Offset Y**.

Each field shows the node's allowed range. **Reset to config** restores the
values saved in the active config's per-camera sensor file.

### View

Per-camera display transforms, applied to the selected camera or to **All**:

- **Rotate** 90° clockwise / counter-clockwise.
- **Flip** horizontal / vertical.
- **Reset** clears rotation and flips for the target.
- **Display cross** toggles the crosshair overlay described above.

These transforms affect only what you see — and, when a recording is saved in
`display` form, what gets baked into the video. They never change the sensor.

### Plugin tabs

- **Flywheel** — drive a turntable stepper motor: configure and run a
  back-and-forth loop program (optionally started with each recording) and jog
  the stage by hand.
- **2-Photon** — arm the Arduino hardware trigger for a 2-photon rig; it waits
  for a ThorSync rising edge, then emits camera trigger pulses at the Record
  tab's FPS for its duration, showing the board's live state.

See [Plugins](plugins.md) for enabling and configuring these.

!!! info "Sensor parameters and camera control lock during recording"
    Changing sensor parameters, names, transforms, recording settings, or
    saving the config is refused while a recording is in progress.

## Footer controls

The footer shows the octacam version, the live connection status, and — when
more than one browser is connected — a peer count. Its buttons:

- **Theme toggle** — switch between light and dark (dark is the default);
  remembered per-browser.
- **Save config…** — open the save dialog (below).
- **Disconnect** — disconnect this browser only; any recording keeps running on
  the rig.
- **Shut down** — stop the octacam server on the rig (refused while recording).

## Saving a configuration

**Save config…** writes the current state back to disk. You can:

- **Sensor parameters** — save the current per-camera sensor parameters (a
  `.pfs` file per Basler camera; the equivalent `.json` for other backends).
- **Display layout** — save the tile layout and each camera's rotation/flip to
  `octacam_config.toml`.

Save either to the **Active** config directory (overwriting it) or as a **New**
named config. The GUI's `[gui]`/`[[plugins]]` sections and the strftime save-dir
template are preserved verbatim. See [Configuration](configuration.md) for the
file format.

## One instance per rig

Only one octacam process can own a rig's cameras at a time — vendor SDKs open
USB3 devices exclusively. Launching a second `octacam gui` for the **same config
directory**, even on a different `--port`, is refused with a clear message
rather than fighting over the cameras. Two genuinely different configs can run
side by side.

octacam also checks the chosen port up front, so a taken port fails instantly
with a suggested alternative instead of an opaque bind error later.

## Automatic browser open

`octacam gui` opens your default browser once the server is reachable. It
**skips** this automatically when opening a browser would be pointless or land
on the wrong machine:

- over an SSH session (the browser would open on the rig, not your laptop),
- on a headless host with no display,
- when you pass `--no-browser`.

In those cases it prints the URL so you can open it yourself.

## Remote operation over SSH

Keep the default loopback bind and forward the port from your machine:

```bash
ssh -L 8765:127.0.0.1:8765 <rig-hostname> octacam gui <config_dir>
# then open http://localhost:8765
```

This runs octacam on the rig and tunnels the GUI to your browser, with no need
to expose the server on the network. The single WebSocket carries the whole UI
(preview + telemetry + controls), so it survives a plain port forward; octacam
also widens the WebSocket keepalive window so the preview stream is not killed
on a congested tunnel.

!!! note "Shared control"
    Any connected browser can drive the rig, and the footer shows how many
    browsers are connected — so an operator can see they are not alone before
    changing settings or shutting the server down.

!!! warning "Transcoding competes with capture"
    Transcoding (via [`octacam transcode`](processing.md)) is CPU-heavy and can
    cause dropped frames if it runs on the same machine while you capture.
    `octacam gui` warns at startup when a transcode is already running locally,
    so you can wait for it to finish.
