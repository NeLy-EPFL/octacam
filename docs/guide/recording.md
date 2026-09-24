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
recording fps, the start timestamp and the frame accounting described below,
plus the session start wall-clock time, the recording settings, the trigger
train the recording was counted against (`pulse_train`) and a cross-camera
`sync` verdict with its warnings.

### Frame alignment and missed trigger pulses

A camera that misses a trigger pulse delivers no frame for it, and nothing in
its frame stream says so — the next frame just arrives one period late. Counting
frames cannot tell such a camera from a healthy one, so octacam assigns every
frame to the **trigger pulse that exposed it**: from the camera's hardware
timestamps on a hardware trigger (an interval of k periods is k-1 missed
pulses), from the trigger's sequence number on the software trigger.

On a trigger train octacam drives itself (`trigger_source = "software"` or
`"managed"`):

- **Frame k of every camera's video is pulse k.** A missed pulse is filled with
  a repeat of the previous frame, and so is a frame the writer queue could not
  accept (the host could not keep up), so a camera that misses pulses stays
  aligned with the others instead of drifting a frame behind for the rest of the
  take.
- The recording **stops on the train's pulse count**: every camera ends on the
  train's last pulse (a camera that missed the last pulses is padded to it), not
  on its own frame count.
- Before the train, the cameras are **primed** with a few sacrificial pulses,
  camera lines only, lights dark, whose frames are discarded. A FLIR
  Grasshopper3 ignores the first two triggers after its acquisition starts; without
  this every recording lost its first pulses, and two cameras starting either side
  of a pulse ended up a frame apart for the whole take. Priming repeats until every
  camera has answered a priming pulse, because a Grasshopper3 just powered up
  ignores more than two. A camera that answers none is reported: it may start the
  recording late, or not be receiving the trigger.

Per camera the summary reports:

| Field | Meaning |
| --- | --- |
| `frames` | Video frames written (fills included) — the train's pulse count on a completed octacam-driven train. |
| `missed_pulses`, `missed_pulse_indices` | Pulses the camera delivered no frame for. |
| `writer_dropped` | Frames the camera delivered but the writer queue refused. |
| `dropped`, `dropped_indices` | Every video frame that is a fill (missed + writer-refused). |
| `late_frames`, `late_pulse_indices` | Frames exposed markedly after their pulse (e.g. a camera that fired on the trigger pulse's falling edge). |
| `extra_frames` | Frames discarded because they belong to no pulse of the train. |
| `stream` | The camera SDK's own transport counters over the recording (lost, incomplete, … frames). A missed pulse with none of these is a trigger the camera never exposed, not a transport loss. |
| `start_offset_pulses` | 0 when this camera's first frame is the same pulse as the others' (checked from when each camera's first frames arrived). |

On an **external** trigger (a source octacam does not drive) the train's length
is unknown and it may be irregular by design, so missed pulses are **reported,
not filled** — use `pulse_index` in the timestamps to line the cameras up.

Missed pulses and the other anomalies also appear as warnings in the GUI's
event log while recording, and each tile's *dropped* counter includes them.

### Per-frame timestamps

With `record.save_timestamps = true`, each recording also writes a single
compressed `timestamps.npz` holding every camera's per-frame series, one entry
per **video** frame, keyed by camera name:

| Key | Type | Meaning |
| --- | --- | --- |
| `"<name>/timestamp_ns"` | int64 | Hardware timestamp of the frame (for a fill: when its pulse was due). |
| `"<name>/dropped"` | bool | The frame is a fill, not an image of its own pulse. |
| `"<name>/missed"` | bool | Of those, the camera never delivered that pulse. |
| `"<name>/pulse_index"` | int64 | The trigger pulse the frame stands for. |
| `"<name>/arrival_ns"` | int64 | Host wall-clock time the frame was delivered (0 for a fill). |

```python
import numpy as np
d = np.load("timestamps.npz")
ts = d["cam0/timestamp_ns"]          # nanoseconds
real = ~d["cam0/dropped"]            # frames that are images of their own pulse
gaps_ms = np.diff(ts[real]) / 1e6    # inter-frame gaps between real frames
```

Where each camera's timestamps came from — hardware (the camera/SDK clock) or
host wall-clock fallback — is recorded per camera as `timestamp_source` in
`recording_summary.json` (see its `timestamp_note`). Hardware timestamps are
free-running per-camera counters: precise for one camera's relative timing, but
not wall-clock and not aligned across cameras.

### Checking recordings

`octacam check` screens recording folders (or whole directory trees) for missed
pulses, unequal frame counts, a start offset between cameras, late exposures and
camera-clock jumps, and exits 1 if any recording has a problem:

```bash
octacam check ~/octacam/data/hexaview/260917          # every recording under it
octacam check -q /mnt/lab/data/experiment             # only the ones with problems
octacam check --json rec/001 > check.json
```

Recordings made before octacam counted pulses (summary `schema_version` < 4)
are checked by re-deriving the pulse of every frame from `timestamps.npz` —
without it there is nothing to check. Each missed pulse there shifts every later
frame of that camera one pulse behind a camera that did not miss it. A start
offset is found by lining up the trigger source's own timing events (the start of
a train, a late pulse), which reach every camera at the same pulse; when a
recording has too few of them it is reported as undetermined.

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
