# Recording

You can record from the [web GUI](gui.md)'s **Record** tab or headlessly with
`octacam record`. Both write the same outputs and both remember where they
recorded so [`octacam process`](processing.md) can find the results later.

## Headless recording

```bash
octacam record <config_dir>
```

Encoding, save method, transform, and the save-directory template all come from
the config's `[record]` section (see [Configuration](configuration.md)). The
options override only the day-to-day values:

| Option | Purpose |
| --- | --- |
| `--fps`, `-f` | Frame rate (default: from config). |
| `--duration`, `-d` | Duration in seconds (default: from config's `duration`/`duration_unit`). |
| `--output`, `-o` | Save directory, overriding the templated location. |
| `--yes`, `-y` | If a serial plugin's board firmware is out of date, reflash it before recording (also lets a headless run flash instead of only warning). |
| `--plugin <name>` | Enable a [plugin](plugins.md) (repeatable). |
| `--no-plugins` | Disable all plugins for this run. |

```bash
octacam record configs/my_rig --fps 100 --duration 10
```

## Recording outputs

Each recording writes, into its own save directory:

- one video file per camera,
- one `recording_summary.json`,
- the recording's config: a snapshot of `octacam_config.toml` plus each camera's
  sensor parameter file (`<serial>.pfs` / `<serial>.txt`),
- one `timestamps.npz` (only when `record.save_timestamps` is on).

### The recording summary

`recording_summary.json` holds what matters for checking a trial: per camera the
recording fps, the start timestamp, and which frame indices were **dropped**,
plus the session start wall-clock time and the recording settings.

!!! note "What *dropped* does and doesn't count"
    Only frames the encoder/writer queue could not accept are counted as
    dropped — **not** frames the camera or transport never delivered (e.g. USB
    bandwidth gaps). To catch the latter, turn on the per-frame timestamps
    (`record.save_timestamps = true`, off by default) and inspect the
    inter-frame gaps.

### Per-frame timestamps

With `record.save_timestamps = true`, each recording also writes a single
compressed `timestamps.npz` holding every camera's per-frame series (replacing
the old per-camera CSVs). Per camera it stores two arrays keyed by camera name —
`"<name>/timestamp_ns"` (int64, `frame_index` is the array position) and
`"<name>/dropped"` (bool):

```python
import numpy as np
d = np.load("timestamps.npz")
ts = d["cam0/timestamp_ns"]          # nanoseconds
gaps_ms = np.diff(ts) / 1e6          # inter-frame gaps
```

Where each camera's timestamps came from — hardware (the camera/SDK clock) or
host wall-clock fallback — is recorded per camera as `timestamp_source` in
`recording_summary.json` (see its `timestamp_note`). Hardware timestamps are
free-running per-camera counters: precise for one camera's relative timing, but
not wall-clock and not aligned across cameras.

### The embedded config snapshot

Each recording also saves its config into its own folder: a copy of the rig's
`octacam_config.toml` updated with everything changed live in the GUI, plus each
camera's sensor parameter file. That makes the folder a complete
[config directory](configuration.md) for the setup the recording actually used:

- **`[record]`** holds the settings the recording ran with (fps, duration,
  trigger source, save method and encoder args, …), even when they were changed
  in the **Record** tab and never saved to the rig config.
- **`[[plugins]]`** holds each plugin's live settings, e.g. the triggerbox camera
  lines and light channels as set in its tab.
- **`<serial>.pfs` / `<serial>.txt`** are read from each camera just before it
  starts recording, so they include **Camera**-tab edits (exposure, gain, ROI, …)
  that were never saved. The rig's other parameter files are copied too.

If nothing was changed live, the snapshot is an exact copy of the rig's file,
comments included. The `directory` / `relative_directory` templates are always
kept as written; the path this recording used is stored in its summary.

To record again with the same setup, copy those files into a new config
directory and launch from it. The copy on your storage works too, since
`octacam process` transfers the config along with the videos:

```bash
mkdir -p configs/wt-rerun
rsync -a --include='octacam_config.toml' --include='*.pfs' --include='*.txt' \
  --exclude='*' /mnt/store/matthias/260620-wt/Fly1/001-bhv/ configs/wt-rerun/
octacam gui configs/wt-rerun
```

New recordings go to the templated save directory, with today's date and a fresh
trial number. You can also run `octacam gui <recording folder>` directly, but a
GUI *Save…* would then write into that folder.

The snapshot is also what lets `octacam process` transcode, build grids, and
transfer with no `--config` flag: it reads the encoder args
(`[transcode].ffmpeg_params`), grid layouts (`[[visualization]]`), and transfer
destination (`[transfer]`) straight from the embedded copy. See
[Processing](processing.md).

!!! note "Recording into a folder twice"
    Confirming the "directory already exists" prompt overwrites the files the
    new take writes, but the previous take's transcoded `.mp4`/`grid.mp4` stay
    behind. `octacam process` notices they are older than the recording they sit
    with and redoes them, rather than transferring a video from the earlier take.

## Transformed vs raw frames

By default frames are saved **transformed**: each camera's rotation/flips (as set
in the GUI's **View** tab) are baked into the video, so the file matches what you
saw on screen.

- Set `record.save_transformed = false` to save the raw, untransformed sensor
  image instead. This is also toggleable live in the GUI's **Record** tab.
- A raw recording (`record.save_method = "raw"`) writes only a `.raw` byte dump
  per camera. Its width/height/pixel-format/fps live in `recording_summary.json`,
  so `octacam process` can transcode it later without a per-camera sidecar.

## Where recordings go

The save directory is templated from the config's `[record]` section
(`directory` joined with a `strftime`-expanded `relative_directory`), so trials
sort themselves into a `.../date/subject/trial` tree automatically. See
[Configuration → `[record]`](configuration.md#record).

octacam notes every finished recording in a small cache (`~/.cache/octacam`), so
you never have to retype paths when processing — `octacam process --last`,
`--last session`, and `--all` read it. See
[Processing → Selecting recordings](processing.md#selecting-recordings-from-the-cache).
