# Web GUI

`octacam gui <config_dir>` launches a browser-based control panel for the
cameras in a config directory. It previews every camera live — while recording —
and exposes the sensor, layout, and recording controls.

```bash
octacam gui <config_dir>
```

By default it binds to `http://127.0.0.1:8765` and opens your default browser.

| Option | Purpose |
| --- | --- |
| `--host` | Bind address (default `127.0.0.1`). Keep the loopback default and reach it remotely over SSH — see below. |
| `--port` | Port to bind (default `8765`). Change it if it clashes with other software. |
| `--no-browser` | Don't open a browser automatically. |
| `--plugin <name>` | Enable a [plugin](plugins.md) for this launch (repeatable). |
| `--no-plugins` | Disable all plugins for this launch. |

If the config directory is omitted it defaults to the current directory (`.`).

## The tabs

- **Record** — set fps, duration, and the save location, then start/stop
  recording. The **Process** section here seeds the post-recording pipeline
  (transcode params, transfer destination) that gets baked into each recording's
  config snapshot — see [Processing](processing.md).
- **Camera** — per-camera sensor controls (exposure, gain, ROI). Save them back
  to the per-camera sensor file from the *Save…* dialog.
- **View** — arrange the preview layout and set each camera's rotation/flips.
  These display transforms are baked into recordings by default (see
  [transformed vs raw](recording.md#transformed-vs-raw-frames)).
- **Flywheel** / **2-Photon** — appear only when the matching
  [plugin](plugins.md) is enabled.

## One instance per rig

Only one octacam process can own a rig's cameras at a time (vendor SDKs open
USB3 devices exclusively). Launching a second `octacam gui` for the same config
directory — even on a different `--port` — is refused with a clear message
rather than fighting over the cameras. Two genuinely different configs can run
side by side.

If the chosen port is already taken, octacam fails immediately and suggests a
free one, instead of dying later with an opaque error.

## Automatic browser open

`octacam gui` opens your default browser once the server is ready. It **skips**
this automatically when it would be pointless or land on the wrong machine:

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

This runs octacam on the rig and tunnels the GUI to your browser — no need to
expose the server on the network. octacam raises the websocket keepalive window
so the preview stream survives a congested tunnel.

!!! warning "Transcoding competes with capture"
    Transcoding (via `octacam process`) is CPU-heavy and can cause dropped
    frames if it runs on the same machine while you capture. `octacam gui` warns
    at startup when a transcode is already running locally, so you can wait for
    it to finish. See [Processing](processing.md).
