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
| `--plugin <name>` | Enable a [plugin](plugins.md) (repeatable). |
| `--no-plugins` | Disable all plugins for this run. |

```bash
octacam record configs/my_rig --fps 100 --duration 10
```

## Recording outputs

Each recording writes, into its own save directory:

- one video file per camera,
- one `recording_summary.json`,
- a copy of the rig's `octacam_config.toml`.

### The recording summary

`recording_summary.json` holds what matters for checking a trial: per camera the
recording fps, the start timestamp, and which frame indices were **dropped**,
plus the session start wall-clock time and the recording settings.

!!! note "What *dropped* does and doesn't count"
    Only frames the encoder/writer queue could not accept are counted as
    dropped — **not** frames the camera or transport never delivered (e.g. USB
    bandwidth gaps). To catch the latter, turn on the per-frame timestamp CSV
    (`record.save_timestamps = true`, off by default) and inspect the
    inter-frame gaps.

### The embedded config snapshot

Each recording also saves a copy of the rig's `octacam_config.toml` into its own
folder. That snapshot is what lets `octacam process` transcode, build grids, and
transfer with no `--config` flag: it reads the encoder args
(`[transcode].ffmpeg_params`), grid layouts (`[[visualization]]`), and transfer
destination (`[transfer]`) straight from the embedded copy. See
[Processing](processing.md).

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
`--session`, and `--all` read it. See
[Processing → Selecting recordings](processing.md#selecting-recordings-from-the-cache).
