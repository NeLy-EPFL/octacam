# Troubleshooting

Common failures and how to fix them, grouped by where they happen: launching and
opening cameras, recording, plugins/serial hardware, and post-processing.

octacam has no dedicated `doctor` command. The two read-only inspection commands
are the fastest first checks:

```bash
octacam list-cameras --backend basler   # what the SDK actually sees (basler|flir|fake)
octacam list-plugins                     # which plugins are enabled-able and why
```

For anything opaque, re-run with debug logging — every subcommand accepts the
app-level flag, which must come **before** the subcommand:

```bash
octacam -l debug gui <config_dir>        # or --log-level debug
```

Logs go to stderr; machine-readable output (camera lists, recording paths,
transcode results) stays on stdout.

---

## Launching and opening cameras

### "Insufficient system resources exist to complete the API"

Seen when streaming starts, usually with several cameras. pylon's USB stack uses
roughly **150 open file descriptors per streaming camera**, so an 8-camera rig
blows past the common 1024 soft limit and `StartGrabbing` fails.

octacam raises its own *soft* file-descriptor limit to the hard limit at startup,
so if this still fails the session's **hard** limit is too low:

```bash
ulimit -Hn        # the hard cap; an 8-camera rig needs well over 1024
```

Raise it persistently in `/etc/security/limits.conf` (or run Basler's
`setup-usb.sh`), and make sure the usbfs memory pool is enlarged too:

```bash
cat /sys/module/usbcore/parameters/usbfs_memory_mb    # want ~1000
# add usbcore.usbfs_memory_mb=1000 to the kernel command line
```

### "Another octacam instance is already running for this config"

Only one octacam process may own a rig's cameras at a time. The guard is a lock
keyed on the **config directory**, not the port, so relaunching the same rig on a
different `--port` is still refused (two genuinely different configs can run side
by side). Open the running instance's GUI in a browser, or stop it first.

### "Could not open the cameras"

The vendor SDKs open USB3 devices exclusively, so this almost always means
another octacam already holds the cameras — or a camera is unplugged. Stop the
other instance (see above), or check the cable, then retry.

### "Port 8765 is already in use on ..."

Another program (or an octacam serving a different config) holds the port.
octacam probes the port before spending time opening cameras, so it fails fast.
Pick a free one:

```bash
octacam gui <config_dir> --port 8766
```

### No cameras opened / a declared serial is missing

If the config's `[[cameras]]` list names a serial that isn't connected, octacam
logs `Camera with serial number <SN> not found` and skips it. If nothing is left,
it prints `No cameras opened. Exiting.` and exits with status 1.

Confirm what the SDK actually enumerates:

```bash
octacam list-cameras --backend basler        # or --backend flir
```

!!! tip "Run without hardware"
    For the Basler backend, `PYLON_CAMEMU=N` summons N emulated cameras:

    ```bash
    PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
    ```

    For the `fake` backend there is no SDK emulator; its serials come from
    `OCTACAM_FAKE_CAMERAS` (default `FAKE-0,FAKE-1`).

### "Parameters file not found at ..."

Per-camera sensor parameters live in the config directory as `<serial>.<ext>` —
`.pfs` for Basler, `.json` for FLIR, and `.fake` for the fake backend. A missing
file is a warning, not an
error: that camera simply loads at its current defaults. Add the file (or export
it from the GUI's Camera tab) to pin the sensor settings.

---

## Camera backend problems

The backend is chosen once per rig by the top-level `backend` key in
`octacam_config.toml`. Only three values exist: `basler` (default), `flir`, and
`fake`.

### Unknown backend in the config

An unrecognized `backend` value is not fatal — the tolerant config parser warns
and falls back to `basler`:

```
Ignoring unknown "backend" '<value>' in octacam config; using "basler"
```

If a rig is silently running as Basler when you expected FLIR, check that log
line and the spelling of the `backend` key.

### FLIR: "Camera backend 'flir' is unavailable"

PySpin is **not on PyPI** — it ships as a wheel with Teledyne's Spinnaker SDK.
When a config pins `backend = "flir"` without it installed, octacam exits with a
clear message instead of a raw `ImportError`:

```
Camera backend 'flir' is unavailable: the Spinnaker SDK and its PySpin wheel
must be installed (they are not on PyPI; see the README)
```

Install the Spinnaker SDK for your platform, then the matching PySpin wheel into
octacam's environment:

```bash
pip install spinnaker_python-*.whl
```

See [Camera backends](../guide/backends.md) for the full setup.

### FLIR: "delivered a non-mono frame; skipping"

The FLIR backend forces `PixelFormat = Mono8` at open so frames arrive as 2-D
`uint8`. If a camera nonetheless delivers a non-monochrome frame, that frame is
dropped with a warning. Check that the sensor's `.json` parameter file (or a
manual pixel-format change) isn't overriding the format back to a colour mode.

---

## Recording problems

### Recording never starts / cameras wait forever

This is almost always a trigger mismatch. octacam has two recording trigger
sources:

| `trigger_source` | Who fires the trigger |
| --- | --- |
| `software` (default) | octacam paces a software trigger at the recording fps |
| `external` | a master octacam does **not** drive (e.g. ThorSync, a trigger board) |

With an **external** trigger, frames only arrive when the external master fires,
so octacam's monitor thread waits **indefinitely** for the first frame. If the
master never pulses, the recording hangs. On the CLI, `--trigger hardware` maps
to `external`; `--trigger software` is the self-paced default:

```bash
octacam record <config_dir> --trigger software     # octacam paces the trigger
octacam record <config_dir> --trigger hardware      # wait for an external master
```

If you expected octacam to pace the cameras itself, use `software`.

### A camera produced 0 frames / an "empty header-only file"

A camera that captured nothing still leaves a valid but header-only video file.
The controller flags any 0-frame camera during teardown; the most common cause is
an **external trigger that never fired** during the recording window. Later, when
you transcode, such a file is skipped rather than fed to ffmpeg (which would only
emit a cryptic Matroska/EBML error):

```
Skipping <file>: recording captured 0 frames (empty header-only file)
```

### Dropped frames

`recording_summary.json` records a `dropped` count per camera, but it means one
specific thing:

!!! warning "What `dropped` counts — and what it doesn't"
    > `dropped` counts only frames the encoder/writer queue could not accept
    > (the host could not keep up). Frames the camera or transport never
    > delivered (e.g. USB bandwidth gaps) are NOT detected here.

- **Non-zero `dropped`** means the machine is **CPU-bound** — the ffmpeg
  encode queue backed up. Check whether an `octacam transcode` is running on the
  same box (it is CPU-heavy; `gui` and `record` warn about this at startup), and
  consider a faster preset (`--preset ultrafast`).
- **Frames the camera/USB never delivered** are not in `dropped`. To find those,
  record with `--save-frame-timestamps` (off by default) and inspect the
  inter-frame gaps in each camera's `<name>.csv` (columns
  `frame_index,timestamp,dropped`).

### Basler: repeated "incomplete grab(s)" warnings

```
Camera <SN>: N incomplete grab(s); last: <description> (0x...)
```

pylon flagged frames as incomplete — a USB bandwidth gap or packet loss. pylon
already discards them, but partial frames are the prime suspect for corrupt
previews. Spread cameras across separate USB host controllers, shorten exposure,
or lower the frame rate to fit the bus.

### "Settings are locked while recording" / "... locked while recording"

Camera parameters, names, transforms, and recording settings can't be changed
while a recording is active (or while a geometry reconfiguration is in progress).
Stop the recording first. This is by design — a device write mid-recording would
corrupt the capture.

### My recording went to a different folder than I set

After a **successful** (non-aborted) recording, octacam auto-increments the
trailing 3-digit group of the save directory so the next trial doesn't overwrite
the last: `001-bhv` → `002-bhv`. An **aborted** recording leaves the directory
unchanged.

### "Directory already exists, data might be overwritten"

Headless `octacam record` warns and proceeds if the output directory exists. In
the GUI, starting a recording over an existing folder returns a confirmation
prompt (`Existing data will be overwritten.`) before it overwrites anything.

---

## Plugins and serial hardware

Plugins (`flywheel`, `twophoton`) are **opt-in** — the default launch loads none.
Enable them with a `[[plugins]]` entry in the config, or per launch:

```bash
octacam gui <config_dir> --plugin flywheel        # add a plugin (repeatable)
octacam gui <config_dir> --no-plugins             # disable all for this run
```

### A plugin isn't showing up

Check that it is loadable and spelled correctly:

```bash
octacam list-plugins
```

Output is `name<TAB>status<TAB>summary`. `available` means its dependencies are
present (the bundled plugins' dep, pyserial, ships by default); `unavailable`
lines carry the reason. An unknown `--plugin` name is logged and skipped —
core keeps running.

!!! note "Legacy plugin name"
    The old `arduino` name still resolves to `flywheel`, with a deprecation
    warning. Update your config's `[[plugins]]` name (or `--plugin` flag).

### "Serial port not available" / board not connecting

A serial plugin whose board is absent or unplugged at launch does **not** stop
the GUI — it logs a warning and starts anyway. Plug the board in and reconnect
without restarting via each plugin's REST endpoint:

- flywheel: `POST /api/serial/reconnect`
- twophoton: `POST /api/twophoton/reconnect`

Default devices are `/dev/ttyACM0` (flywheel) and `/dev/arduinoCams`
(twophoton); override with the plugin's `device`/`baud` options in
`[plugins.options]`. If you see `pyserial is not importable`, the environment is
broken (pyserial ships by default) — reinstall it.

### twophoton: recording waits but the board never triggers

The twophoton plugin arms the Arduino at recording start and waits ~1 s for its
`A` (armed) acknowledgement. Two warnings point at the problem:

- Link not open — the arm was skipped entirely:
  > link to `<device>` is not open; recording will NOT be hardware-armed
  > (cameras may wait for a trigger that never fires)
- No acknowledgement — the arm packet may have been dropped:
  > no arm acknowledgement from `<device>` within 1.0 s; the board may not have
  > armed

Reconnect the board (endpoint above), confirm the recording uses an **external**
trigger, and verify the ThorSync edge actually fires. See
[Plugins](../guide/plugins.md) for wiring and firmware.

---

## Post-processing (transcode / grid / NAS)

### "No ffmpeg executable found"

octacam resolves ffmpeg in this order: the `OCTACAM_FFMPEG` environment variable,
the bundled `imageio-ffmpeg`, then a system `ffmpeg` on `PATH`. If none is found:

```
No ffmpeg executable found: install the imageio-ffmpeg package or a system
ffmpeg, or set OCTACAM_FFMPEG.
```

Pin a specific binary with `OCTACAM_FFMPEG=/path/to/ffmpeg`.

### A transcoded video won't play (or colour looks wrong)

Captures and the default transcode use a **true monochrome** H.264 stream
(`pix_fmt gray`, 4:0:0). Many players (QuickTime, browsers, Keynote) can't play
4:0:0. For a widely playable file, transcode to `yuv420p`:

```bash
octacam transcode <folder> --pix-fmt yuv420p
```

The composite **grid** video is always `yuv420p` for exactly this reason, so
`octacam grid` output plays everywhere.

### "No recording directories found" / nothing gets processed

`grid` and `nas` (and folder arguments to `transcode`) identify a recording
directory by the presence of `recording_summary.json`. If you point them at a
parent directory, pass `-r`/`--recursive`; without it, each argument must itself
be a recording directory. When a parent contains recordings beneath it, octacam
says so and suggests `-r`.

### transcode: "Provide one or more PATHS, or one of --last/--session/..."

`transcode` needs either explicit paths or exactly one cache selector; the
selectors are mutually exclusive and can't be combined with paths:

```bash
octacam transcode --last          # the most recent recording folder
octacam transcode --session       # every folder from the last GUI session
octacam transcode --session-id ID # an exact session (printed on GUI exit)
octacam transcode --all           # every folder still in the cache
```

The cache lives under `~/.cache/octacam` (override with `OCTACAM_CACHE_DIR`) and
silently skips folders deleted since recording. See
[Processing recordings](../guide/processing.md).

### NAS: files fail to copy

`octacam nas` (and `transcode --nas-path`) log a clear error when the destination
can't be created or written and report a per-run tally:

```
Could not create NAS directory <dest>: <reason>
NAS: <n> copied, <n> skipped, <n> failed
```

Mount the share or fix permissions, then re-run — copies are **atomic and
resumable**: each file is streamed to a hidden temp and only renamed onto its
final name once whole and (by default) checksum-verified, and a re-run skips
files already present (by size). At most the one in-progress file is redone. Use
`--no-verify` for a faster size-only check on trusted links, or `--checksum` to
re-copy files whose bytes differ. Add `--dry-run` first to preview the plan.

### "octacam transcode running ... may slow capture"

Transcoding runs slow x264 presets and saturates the CPU, so `gui` and `record`
warn at startup when a transcode is already running on the same machine — it
competes with live capture and can cause dropped frames. Wait for it to finish,
or move the transcode to another host.

---

## Config gotchas

Config parsing is deliberately **tolerant**: a malformed file, section, or field
is warned about and replaced with a default — it never stops octacam from
starting. So if a rig behaves unexpectedly, the answer is usually in the warnings.
Run with debug logging to see every parse decision:

```bash
octacam -l debug gui <config_dir>
```

A few specifics:

- An unknown `backend` falls back to `basler` (see above).
- A camera `name` that isn't a safe single path segment (it becomes
  `<name>.<ext>` at record time) is rejected and falls back to the serial number;
  duplicate serials or names are skipped.
- A `[grid]` layout cell naming a camera not in `[[cameras]]` renders as a black
  tile and is reported in the log.

---

## Still stuck?

Re-run the failing command with `-l debug`, capture the stderr log, and
[open an issue](https://github.com/NeLy-EPFL/octacam/issues) with it — plus the
output of `octacam list-cameras` and `octacam list-plugins`.
