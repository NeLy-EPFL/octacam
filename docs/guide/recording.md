# Recording

octacam records synchronized video from every camera on a rig at once. You can
record from the [web GUI](gui.md)'s **Record** tab or headlessly with `octacam
record`. Both drive the same recording state machine, write the same outputs,
and note where they recorded so [`octacam transcode`](processing.md) can find
the results later without you re-typing paths.

## The recording pipeline

A recording is a framework-free state machine shared by the GUI and the CLI:

```
preview / idle  ->  waiting  ->  recording  ->  finishing  ->  preview / idle
```

- **preview / idle** — the cameras free-run under the software trigger for live
  preview (the GUI always previews; `octacam record` starts from idle).
- **waiting** — recording has been armed and the cameras have started grabbing,
  but no frame has arrived yet.
- **recording** — the first frame landed; the countdown to the duration deadline
  is running.
- **finishing** — teardown: stop the trigger, let the grab loops exit, drain the
  writers, then write the summary.

A **monitor thread** drives the transitions. It polls each camera for its first
frame; if not every camera has delivered one within **3 s** it emits a warning,
and for a software-triggered recording it gives up after **10 s** and records
with whatever started (a single stalled camera can never hang the rig). Once
frames arrive it moves to `recording`, sets the deadline to `duration + 0.5 s`
grace, and counts down. `stop`/`abort` from the GUI or a duration timeout both
converge on the same ordered teardown.

Each camera runs its own grab thread. A grabbed frame is handed to that camera's
**asynchronous writer** — a background thread behind a bounded queue (20 frames)
that pipes the frame to its sink. The default sink is an **ffmpeg child process**
encoding H.264; the raw sink dumps bytes to disk. Encoding runs entirely in the
ffmpeg child, off the Python GIL, so many cameras encode in parallel.

!!! warning "Camera controls are locked while recording"
    Any device-touching operation — changing exposure/gain/ROI, renaming a
    camera, editing its transform, or changing recording settings — is refused
    while a recording is active (it raises rather than silently racing the grab
    loops). Set everything up in preview first.

Serial-hardware [plugins](plugins.md) are woven into this lifecycle: a plugin's
`on_recording_start` hook arms its board when recording starts,
`on_first_frame` fires at the countdown's t0 (so e.g. a stepper motor stays
synced to real capture), and `on_recording_stop` runs during teardown.

## Trigger model

The `trigger_source` setting has two values.

| `trigger_source` | Who fires the frames | Behaviour |
| --- | --- | --- |
| `software` (default) | octacam | A timer thread paces a software trigger at the recording `fps`. |
| `external` | an outside master octacam does not drive | Each camera is restored to the trigger source baked into its parameter file, and frames arrive only when the external source pulses. |

Under the hood, starting a recording arms `TriggerMode = On` / `TriggerSelector
= FrameStart` on every camera. For **software** it points the trigger source at
`Software` and starts a timer that calls each camera's device software-trigger
once per tick; for **external** it restores the source that was in the camera's
parameter file when the config was loaded, and the software timer never runs.

!!! note "Live preview is always software-triggered"
    Preview runs the cameras from octacam's own software trigger regardless of
    `trigger_source`, so you always get a picture while setting up — even on a
    rig that will record under an external master.

!!! tip "An external trigger that never fires"
    With `trigger_source = external`, the monitor waits **indefinitely** for the
    first frame (the master may fire far in the future). If the recording window
    ends with no pulses, those cameras write a header-only file with **0 frames**;
    octacam flags this loudly at teardown so it never surfaces later as a cryptic
    error on an empty video.

### Frame timing

The trigger timer keeps the *average* rate on target: there is no catch-up
suppression, so if a tick is late the next ones fire back-to-back until the
schedule is caught up, preserving the total frame count over the window. The
recording stops `0.5 s` after the nominal duration to catch in-flight frames.

!!! tip "Sensor and USB limits, not octacam, set the ceiling"
    octacam paces the trigger, but delivered fps is bounded by each camera's
    exposure + USB transfer time and by the shared USB3 bus bandwidth. If
    `StartGrabbing` fails with *"Insufficient system resources"* on a
    many-camera Basler rig, raise the open-file limit and `usbfs_memory_mb` — see
    [Troubleshooting](../reference/troubleshooting.md).

## Encoding and codecs

Two codecs are available.

=== "x264 (default)"

    H.264 via libx264, written to a per-camera **MKV**. The pixel format is true
    monochrome 4:0:0 (`gray`), so there is no chroma to waste bits on.

    | Setting | Default | Notes |
    | --- | --- | --- |
    | `crf` | `18` | Quality; lower is better, `0` is lossless. |
    | `preset` | `ultrafast` | Speed preset. `ultrafast` is the one validated at high camera counts; slower presets compress better but must still keep up with the cameras during capture. |
    | `pix_fmt` | `gray` | Kept as monochrome 4:0:0. |
    | `x264_params` | *(empty)* | Passed verbatim to ffmpeg's `-x264-params`, e.g. `keyint=30:scenecut=0`. |

=== "raw"

    A raw `Mono8` byte dump (`<name>.raw`) plus a small `<name>.json` sidecar
    holding `width`/`height`/`pixel_format`/`fps`. This is the maximum-throughput
    option: no encoding happens during capture, and you compress later with
    [`octacam transcode`](processing.md).

!!! note "MKV during capture; browser playback needs a transcode"
    MKV stays playable up to the point of any mid-recording crash, which is why
    it is the capture container. The monochrome 4:0:0 stream, however, is not
    directly playable in web browsers (they need `yuv420p`), and `ffprobe`
    misreports it as `yuvj420p` — the x264 encoder log (`4:0:0, 8-bit`) is the
    source of truth. Run [`octacam transcode`](processing.md) to produce an MP4
    for sharing.

ffmpeg is located from `$OCTACAM_FFMPEG`, then the bundled `imageio-ffmpeg`,
then a system `ffmpeg` on `PATH`.

## Display form vs. sensor form

`record_form` decides whether each camera's display orientation (the
rotate/flip set in the GUI's **View** tab) is written into the pixels.

- **`display`** (default) — the rotation/flips are baked into the recorded video
  at capture time, so the file matches what you saw on screen. A 90°/270°
  rotation swaps width and height in the output.
- **`sensor`** — the raw, untransformed sensor image is saved.

Either way the transform is recorded in the summary, so a `sensor` recording can
still be reproduced "as displayed" later at transcode time. Live preview always
shows the raw frame with the orientation applied in the browser, so switching
form never changes what you see.

## Recording outputs

Each recording writes into its own save directory:

- **One video file per camera**, named `<camera-name>.<ext>` — `.mkv` for x264,
  `.raw` (plus a `.json` sidecar) for raw. The camera name must be a safe,
  unique single-segment filename.
- **`recording_summary.json`** — one metadata file per recording (see below).
- **`<camera-name>.csv`** — a per-frame timestamp file, **only** when
  `save_frame_timestamps` is enabled (off by default).

After a successful (non-aborted) recording the save directory's trailing 3-digit
group auto-increments (`001-bhv` → `002-bhv`) so the next trial lands in a fresh
folder; an aborted recording leaves the directory unchanged. The folder is also
noted in a small session cache (see [Selecting recordings from the
cache](processing.md)).

### The recording summary

`recording_summary.json` (schema version 1) captures what you need to check a
trial. Top level:

| Field | Meaning |
| --- | --- |
| `start_time`, `start_time_ns` | Wall-clock start (ISO-8601 UTC, and nanoseconds). |
| `aborted` | Whether the recording was stopped early. |
| `fps_target`, `duration_s` | The requested rate and length. |
| `trigger_source`, `codec`, `record_form` | The settings used. |
| `dropped_frames_note` | Documents exactly what `dropped` counts. |
| `cameras[]` | Per-camera stats (below). |

Per camera: `name`, `serial`, `file`, `width`, `height`, `fps` (measured mean),
`frames` (frames recorded), `dropped` (count) and `dropped_indices`,
`start_timestamp_ns`, `writer_failed`, the `transform`, and `transform_applied`
(true only when a non-identity transform was baked into a `display`-form file).

!!! warning "What *dropped* does and does not count"
    `dropped` counts **only** frames the encoder/writer queue could not accept —
    i.e. the host could not keep up. Frames the camera or USB transport never
    delivered (e.g. bandwidth gaps) are **not** detected here. To find those,
    turn on `save_frame_timestamps` and inspect the inter-frame gaps in the
    per-camera CSV.

### Per-frame timestamps (opt-in)

With `save_frame_timestamps` on, each camera also writes `<camera-name>.csv`
with one row per frame:

```csv
frame_index,timestamp,dropped
0,1719223456789012345,0
1,1719223456799112233,0
```

`timestamp` is in nanoseconds (the camera/SDK timestamp, falling back to host
time), and `dropped` is `1` for a frame the writer queue could not accept. This
is a debugging aid; leave it off for normal recording.

## Headless recording (`octacam record`)

```bash
octacam record <config_dir>
```

Records from every camera in `<config_dir>` and prints one output path per
camera on stdout when finished. Every option defaults to the value in the
config's `[gui]` section (see [Configuration](configuration.md)); the flags
override for a single run.

| Option | Default | Purpose |
| --- | --- | --- |
| `--fps`, `-f` | from config | Frame rate. |
| `--duration`, `-d` | from config | Duration in **seconds**. |
| `--output`, `-o` | from config | Save directory. |
| `--codec` | `x264` | `x264` (H.264 MKV) or `raw` (Mono8 dump). |
| `--crf` | from config | x264 quality (lower = better; `0` = lossless). |
| `--preset` | from config | x264 speed preset. |
| `--x264-params` | from config | Extra libx264 options, verbatim to `-x264-params`. |
| `--trigger` | `software` | `software` (timer at `--fps`) or `hardware` (use the trigger configured in the parameter files). |
| `--record-form` | from config | `display` (bake rotation/flips) or `sensor` (raw image). |
| `--save-frame-timestamps` / `--no-save-frame-timestamps` | from config | Also write the per-frame timestamp CSV. |
| `--plugin <name>` | — | Enable a [plugin](plugins.md) (repeatable; adds to the config's selection). |
| `--no-plugins` | — | Disable all plugins for this run. |

!!! note "`--trigger hardware` maps to the external source"
    `--trigger hardware` records under `trigger_source = external`: octacam does
    not pace a trigger and instead uses whatever trigger source each camera's
    parameter file (`<serial>.pfs` / `<serial>.json`) was saved with, so an
    external master drives the frames.

```bash
# 100 fps for 10 s to a chosen directory
octacam record configs/my_rig --fps 100 --duration 10 --output ~/data/trial-001

# Raw capture (transcode later), external trigger, keep the sensor image
octacam record configs/my_rig --codec raw --trigger hardware --record-form sensor
```

If the save directory already exists, `octacam record` warns that data may be
overwritten and proceeds (the interactive confirmation only exists in the GUI).

## Recording from the GUI

In the web GUI's **Record** tab you set the fps, duration, save directory,
codec, trigger source, and record form, then start/stop the recording; a live
countdown and per-camera frame/drop counters update over the preview WebSocket.
If the save directory already exists the GUI asks you to confirm before
overwriting. See the [Web GUI guide](gui.md) for the full tab tour.

## After recording

Recordings are captured with a fast preset for throughput; the slow, high-ratio
compression happens offline. Turn a recording folder into shareable MP4s (and
optionally a composite grid video or a NAS copy) with
[`octacam transcode`](processing.md), which can find recent recordings straight
from the session cache — no paths to retype.
