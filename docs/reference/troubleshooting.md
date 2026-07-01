# Troubleshooting

Start with `octacam doctor` (optionally `octacam doctor <config_dir>`). It checks
the toolchain, storage, cameras, and runtime conflicts and usually points
straight at the problem. The cases below cover the most common failures.

## "Insufficient system resources exist to complete the API"

Seen at the start of streaming, usually with many cameras. pylon's USB stack
needs ~150 open file descriptors and ~16 MB of usbfs memory **per camera**.

octacam raises its own soft file-descriptor limit at startup, but if the
session's **hard** limit is still too low, raise it:

```bash
ulimit -Hn        # check the hard limit — an 8-camera rig needs ~1200
```

- Raise it in `/etc/security/limits.conf`, or run Basler's `setup-usb.sh`.
- Make sure the usbfs memory is bumped too:

```bash
cat /sys/module/usbcore/parameters/usbfs_memory_mb    # want 1000
# set usbcore.usbfs_memory_mb=1000 on the kernel command line
```

## "Another octacam instance is already running for this config"

Only one octacam can own a rig's cameras at a time. Open the running instance's
GUI in your browser, or stop it first. This guard is keyed on the config
directory, not the port — relaunching the same rig on a different `--port` is
still refused. Two different configs can run at once.

## "Could not open the cameras"

The cameras couldn't be opened — most often because another octacam already
holds them (vendor SDKs open USB3 devices exclusively), or a camera is
unplugged. Stop the other instance, or check the connection, then retry.
`octacam doctor <config_dir>` cross-checks declared vs detected serials.

## "Port 8765 is already in use"

Another program (or an octacam serving a different config) holds the port. Pick a
free one:

```bash
octacam gui <config_dir> --port 8766
```

## FLIR: PySpin missing

If a `flir` config is served without PySpin installed, octacam exits with a clear
message. Install the Spinnaker SDK and its PySpin wheel — see
[FLIR / Teledyne setup](../guide/backends.md#flir-teledyne). Confirm with:

```bash
octacam doctor --backend flir
```

## Dropped frames

- `recording_summary.json` counts frames the **encoder/writer queue** could not
  accept. If you see these, the machine is CPU-bound — check whether an
  `octacam process` transcode is running at the same time (it's CPU-heavy and
  `gui`/`record` warn about it at startup).
- Frames the **camera or transport** never delivered (e.g. USB bandwidth gaps)
  are *not* counted as dropped. Enable `record.save_timestamps = true` and
  inspect the per-frame CSV's inter-frame gaps to find those.

## Transfer destination not present / not writable

`octacam process` reports when the `[transfer]` destination isn't
mounted/present or isn't writable (local recording still works either way). Mount
the share or fix permissions, then re-run — transfers **resume**, redoing at most
the one in-progress file. See [Processing → Transfer](../guide/processing.md#transfer).

## Colour looks wrong after transcoding

octacam resolves an ffmpeg binary (bundled `imageio-ffmpeg` by default), and
colour-range flags can differ between ffmpeg versions. `octacam doctor` reports
which ffmpeg is in use and whether a different system ffmpeg is on `PATH`. To
force a specific binary, set `OCTACAM_FFMPEG=/path/to/ffmpeg`.

## Still stuck?

Run `octacam doctor --json` and include the output when you
[open an issue](https://github.com/NeLy-EPFL/octacam/issues).
