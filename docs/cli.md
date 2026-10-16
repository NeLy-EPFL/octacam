# CLI reference

Preview, record, and save video streams from multiple cameras.

Run `octacam gui <config_dir>` for the web GUI, or see the commands below.

**Usage**:

```console
$ octacam [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--version`: Show the version and exit.
* `--help`: Show this message and exit.

**Commands**:

* `gui`: Launch the octacam web GUI for the cameras...
* `doctor`: Diagnose the octacam install and,...
* `config`: Interactively scaffold a new rig config...
* `record`: Record videos headlessly from the cameras...
* `flash`: Check a serial plugin's board firmware and...
* `benchmark`: Benchmark a rig: is the target fps...
* `check`: Check recordings for missed trigger pulses...
* `process`: Post-recording pipeline: transcode, build...
* `jobs`: Manage detached `octacam process` jobs...
* `cache`: Inspect and clear the octacam cache...
* `users`: The rig's users: who records and where...
* `twophoton`: 2-photon data on the destination: verify,...

## `octacam gui`

Launch the octacam web GUI for the cameras in `config_dir`.

**Usage**:

```console
$ octacam gui [OPTIONS] [config_dir]
```

**Arguments**:

* `config_dir`: The rig's config directory (`octacam_config.toml` and its camera files), or a recording folder, whose config snapshot it uses.  [default: .]

**Options**:

* `--host <str>`: Interface to bind. Keep the loopback default and reach the GUI remotely with `ssh -L 8765:127.0.0.1:8765 <rig-hostname>`.  [default: 127.0.0.1]
* `--port <int>`: Port to serve on. Default: 8765, or the next free port.
* `--no-browser`: Don't open the web GUI in a browser automatically. Auto-open is also skipped over SSH and on headless sessions.
* `--plugin <str>`: Enable a plugin (repeatable); adds to the config's `plugins` (e.g. --plugin flywheel). See `octacam doctor`.
* `--no-plugins`: Disable all plugins for this launch, ignoring the config.
* `--set KEY=VALUE`: Override one config key for this launch by its dotted path, `table.key=value`; repeatable. The value is read as TOML (a list may drop its brackets), and `none` restores the default.
* `-u, --user <str>`: Who records: initials from the rig config's [users] (their transfer destination and plugin folders). Default: the last user on this machine. See `octacam users list`.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam doctor`

Diagnose the octacam install and, optionally, a rig config.

Lists detected cameras and bundled plugins, and checks the encoding toolchain, storage, recording cache, and runtime conflicts. Pass a `config_dir` to also validate that rig. doctor never opens the cameras, so it is safe to run while a GUI or `record` session is live.

Exits 0 when no errors are found (nonzero on errors, or on warnings too with --check), so it is usable as a pre-flight check in scripts.

**Usage**:

```console
$ octacam doctor [OPTIONS] [config_dir]
```

**Arguments**:

* `config_dir`: Optional rig config dir. When given, doctor also validates that rig's config, resolves its save/transfer paths, cross-checks declared vs detected cameras, and reports the plugin selection.

**Options**:

* `--backend <str>`: Only enumerate this backend (basler/flir/spinnaker/pycameleon/fake). Default: the whole available cascade.
* `--json`: Emit machine-readable JSON instead of the report.
* `--check`: Exit nonzero on warnings too (for CI), not only on errors.
* `--probe-serial`: Also open each detected serial port briefly to read its firmware identity. It writes to each board, even one a running session holds (except on Windows), so skip this while a board may be armed.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam config`

Interactively scaffold a new rig config directory.

Auto-detects the connected cameras (across every installed backend, so a Basler+FLIR rig just works), then prompts for the record and transfer settings and writes an octacam_config.toml. The visual per-camera bits -- window placement, rotation, and the grid -- are left to `octacam gui`, which tunes them against a live preview; run it next on the new directory.

By default it also opens each detected camera once to snapshot its current sensor parameters into a per-camera file; a busy camera is skipped with a warning. Pass --no-snapshot-params to skip that and never open a camera.

**Usage**:

```console
$ octacam config [OPTIONS] [config_dir]
```

**Arguments**:

* `config_dir`: Directory to create the config in. Omit to be prompted for one.

**Options**:

* `--backend <str>`: Pin the rig to one camera backend (basler/flir/spinnaker/pycameleon/fake). Default: auto-detect through the cascade and use whatever is connected.
* `--force`: Overwrite an existing octacam_config.toml without asking.
* `--snapshot-params / --no-snapshot-params`: Open each detected camera once to save its current sensor parameters (.pfs/.txt). Skipped when a camera is busy. On by default.  [default: snapshot-params]
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam record`

Record videos headlessly from the cameras in `config_dir`.

Encoding, save method, transform, and the save-directory template all come from the config's [record] section. `--set` overrides any config key for this take; `--fps`, `--duration` and `--out` cover the day-to-day ones and win over `--set`.

**Usage**:

```console
$ octacam record [OPTIONS] [config_dir]
```

**Arguments**:

* `config_dir`: The rig's config directory (`octacam_config.toml` and its camera files), or a recording folder, whose config snapshot it uses.  [default: .]

**Options**:

* `-f, --fps <float>`: Frame rate (default: the config's `record.fps`).
* `-d, --duration <float>`: Recording duration in seconds (default: the config's `record.duration` and `record.duration_unit`). Given here, it is exact: a plugin that would end the take itself does not.
* `-o, --out <path>`: Save directory, overriding the config's templated `record.directory` and `record.relative_directory`.
* `-y, --yes`: Before recording, reflash without asking a serial plugin's board that runs an old build of its firmware (a blank or foreign board is only warned about).
* `-F, --force`: Overwrite an existing save directory without the interactive confirmation prompt.
* `--plugin <str>`: Enable a plugin (repeatable); adds to the config's `plugins` (e.g. --plugin flywheel). See `octacam doctor`.
* `--no-plugins`: Disable all plugins for this launch, ignoring the config.
* `--set KEY=VALUE`: Override one config key for this launch by its dotted path, `table.key=value`; repeatable. The value is read as TOML (a list may drop its brackets), and `none` restores the default.
* `-u, --user <str>`: Who records: initials from the rig config's [users] (their transfer destination and plugin folders). Default: the last user on this machine. See `octacam users list`.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam flash`

Check a serial plugin's board firmware and upload the current sketch if needed.

Reads the board's identify banner, compares its build fingerprint to the sketch source in `arduino/<name>`, and (unless `--check`) compiles + uploads the current firmware with arduino-cli. Exits 0 when every board is up to date (or was flashed), nonzero otherwise; without the sketch source a board's build is unknown, which fails a flash but not `--check`.

**Usage**:

```console
$ octacam flash [OPTIONS] [config_dir]
```

**Arguments**:

* `config_dir`: Rig config dir whose serial plugins to check. Optional if --plugin is given.

**Options**:

* `--plugin <str>`: Serial plugin whose firmware to manage (e.g. triggerbox); enables it even if not in the config.
* `--device <str>`: Serial device override (e.g. /dev/ttyACM0 or auto).
* `-y, --yes`: Flash without prompting when out of date.
* `--check`: Report only; exit nonzero if any board is out of date. Never flashes.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam benchmark`

Benchmark a rig: is the target fps achievable, what is the max, and what limits it.

Runs a short, instrumented dry-run against the cameras in `config_dir` -- an acquisition-ceiling sweep, an encoder-ceiling sweep, and an end-to-end trial at the target fps -- then reports the achievable rate, the maximum, and the per-stage throughput so you can see the bottleneck. No video is kept. It opens the cameras (like `record`), so it cannot run at the same time as a live GUI or recording on the same rig.

Exits nonzero when the target fps is not achievable, so it is usable as a pre-flight check in scripts.

**Usage**:

```console
$ octacam benchmark [OPTIONS] [config_dir]
```

**Arguments**:

* `config_dir`: The rig's config directory (`octacam_config.toml` and its camera files), or a recording folder, whose config snapshot it uses.  [default: .]

**Options**:

* `-f, --fps <float>`: Target fps to test (default: from the config).
* `-d, --duration <float>`: Seconds per measurement window (each scenario).  [default: 5.0]
* `--find-max / --no-find-max`: Search for the maximum *stable* fps (software trigger only).  [default: find-max]
* `--freerun / --no-freerun`: Also measure the free-run (external-trigger-equivalent) ceiling.  [default: freerun]
* `--sink <config|null>`: What to write through: 'config' (the rig's real save_method, so the encode cost is measured) or 'null' (discard frames — isolate acquisition, skip the encoder).  [default: config]
* `--backend <str>`: Override the config's camera backend.
* `--record-form <display|sensor>`: 'display' (bake the transform) or 'sensor' (default: from the config).
* `--json`: Emit the report as JSON instead of the table.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam check`

Check recordings for missed trigger pulses and desynchronized cameras.

Reads each recording's summary and timestamps.npz (never modifies anything) and reports, per camera, the trigger pulses it delivered no frame for -- an unfilled one shifts its later frames by one against a camera that did not miss it -- plus unequal frame counts, a start offset between cameras, the recorder's own sync verdict, late exposures and camera-clock jumps. Recordings made before octacam counted pulses are re-derived from the hardware timestamps. A recording that cannot be read is a problem too. Exits 1 if any recording has a problem.

**Usage**:

```console
$ octacam check [OPTIONS] [paths]...
```

**Arguments**:

* `paths...`: Recording folders, or directories to search for them (default: the current directory).

**Options**:

* `--fps <float>`: Trigger rate to check against (default: each summary's).
* `--json`: Print machine-readable results.
* `-q, --quiet`: Only list recordings with problems.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam process`

Post-recording pipeline: transcode, build grids, and transfer recordings.

Every setting -- encoder args, grid layouts, transfer destination -- is read from each recording's own octacam_config.toml (copied in at record time), so no --config is needed. Transcode and transfer run by default (disable with --no-transcode / --no-transfer); the composite grid is opt-in -- it is built only for a rig whose config carries a [[visualization]] entry, and --no-grid skips even those.

Re-running is safe and resumes where it left off: each step skips outputs that already exist -- a finished transcode .mp4, a built grid, or a file already at the transfer destination -- so only missing work is redone. Pass --force to rebuild existing transcodes and grids anyway (e.g. after changing the encoder params or grid layout). --dry-run lists that missing work without doing any of it, so `octacam process --all --dry-run` shows what is left to process.

Instead of `paths`, pass --last (the most recent recording; same as --last recording), --last session (the last GUI session), --session-id (an exact session), or --all (every cached folder). Deleted folders are silently skipped.

**Usage**:

```console
$ octacam process [OPTIONS] [paths]...
```

**Arguments**:

* `paths...`: Recording folders (or parent directories with -r). Omit when using --last/--session-id/--all.

**Options**:

* `--last [recording|session]`: Process the most recent recording (--last or --last recording) or every folder from the last GUI session (--last session).
* `--session-id <str>`: Process every folder from one exact session id (what the GUI prints on exit).
* `--all`: Process every recording folder still in the cache.
* `-r, --recursive`: Recurse into the given folders.
* `--no-transcode`: Skip transcoding; grid/transfer act on the existing *.mp4 files.
* `--no-grid`: Skip building the configured visualization grid(s).
* `--no-transfer`: Skip transferring to the [transfer] destination.
* `--ignore-capture`: Do not pause while an octacam gui/record holds the cameras on this machine. Processing then competes with capture for CPU/GPU/disk.
* `--force`: Re-transcode and rebuild grids even when the output already exists. By default existing transcode/grid outputs are skipped (as the transfer step skips files already at the destination).
* `-c, --config <directory>`: Fallback config dir for recordings that lack an embedded octacam_config.toml (older recordings). Normally not needed.
* `-d, --delete-source`: Delete each source .mkv/.raw once it transcodes successfully. The recording's summary, timestamps and config snapshot are kept.
* `--delete-after-transfer`: Remove each local recording once every file of it is on the [transfer] destination, content-verified (after its grids are built from it). Default: the config's [transfer].delete_after_transfer.
* `--no-delete-after-transfer`: Keep the local recordings even when the config's [transfer].delete_after_transfer is set.
* `--progress-style <octacam|ffmpeg>`: How to show transcode progress. octacam (default): an octacam-style bar. ffmpeg: stream ffmpeg's own output verbatim.  [default: octacam]
* `--dry-run`: List what each step would do (files to transcode, grids to build, files to transfer) without running ffmpeg, copying, or deleting anything. Work that is already done is only counted.
* `--detach`: Run the pipeline as a detached background job that survives an SSH disconnect, then return its id. Watch it with `octacam jobs attach`; a running gui/record auto-pauses it.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam jobs`

Manage detached `octacam process` jobs (list, attach, pause, cancel).

**Usage**:

```console
$ octacam jobs [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `list`: List detached processing jobs and their...
* `attach`: Follow a detached job's live log +...
* `pause`: Pause a running detached job (it parks at...
* `resume`: Clear a manual pause (a gui/record...
* `cancel`: Cancel a running detached job (a clean...

### `octacam jobs list`

List detached processing jobs and their progress.

**Usage**:

```console
$ octacam jobs list [OPTIONS]
```

**Options**:

* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam jobs attach`

Follow a detached job's live log + progress (Ctrl-C detaches, does not cancel).

**Usage**:

```console
$ octacam jobs attach [OPTIONS] [JOB]
```

**Arguments**:

* `[JOB]`: Job id (from `octacam jobs list`). Omit for the most recent job.

**Options**:

* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam jobs pause`

Pause a running detached job (it parks at its next file/folder boundary).

**Usage**:

```console
$ octacam jobs pause [OPTIONS] [JOB]
```

**Arguments**:

* `[JOB]`: Job id (from `octacam jobs list`). Omit for the most recent job.

**Options**:

* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam jobs resume`

Clear a manual pause (a gui/record auto-pause clears on its own).

**Usage**:

```console
$ octacam jobs resume [OPTIONS] [JOB]
```

**Arguments**:

* `[JOB]`: Job id (from `octacam jobs list`). Omit for the most recent job.

**Options**:

* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam jobs cancel`

Cancel a running detached job (a clean stop; already-done work is kept).

**Usage**:

```console
$ octacam jobs cancel [OPTIONS] [JOB]
```

**Arguments**:

* `[JOB]`: Job id (from `octacam jobs list`). Omit for the most recent job.

**Options**:

* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam cache`

Inspect and clear the octacam cache (recording list, job logs, markers).

**Usage**:

```console
$ octacam cache [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `path`: Print the octacam cache directory...
* `info`: Show the cache location, size, and a...
* `clear`: Clear cached state under the cache dir.

### `octacam cache path`

Print the octacam cache directory (respects OCTACAM_CACHE_DIR / XDG_CACHE_HOME).

**Usage**:

```console
$ octacam cache path [OPTIONS]
```

**Options**:

* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam cache info`

Show the cache location, size, and a breakdown of what is cached.

**Usage**:

```console
$ octacam cache info [OPTIONS]
```

**Options**:

* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam cache clear`

Clear cached state under the cache dir.

Removes the recording list and orphaned activity markers; with `--all` it also removes finished detached-job logs. A live capture, transcode, or job is never touched.

**Usage**:

```console
$ octacam cache clear [OPTIONS]
```

**Options**:

* `--all`: Also remove finished detached-job logs (kept by default).
* `-y, --yes`: Clear without the confirmation prompt.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam users`

The rig's users: who records and where their data goes.

**Usage**:

```console
$ octacam users [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `list`: List the rig's users and where each one's...
* `add`: Add a user to the rig config (appended;...

### `octacam users list`

List the rig's users and where each one's data goes.

**Usage**:

```console
$ octacam users list [OPTIONS] {config_dir}
```

**Arguments**:

* `config_dir`: The rig's config directory.  [required]

**Options**:

* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam users add`

Add a user to the rig config (appended; the rest is left as is).

**Usage**:

```console
$ octacam users add [OPTIONS] {config_dir} {initials}
```

**Arguments**:

* `config_dir`: The rig's config directory.  [required]
* `initials`: 2 to 4 capital letters, like MD.  [required]

**Options**:

* `--name <str>`: Full name.
* `--transfer-directory <str>`: Where `octacam process` copies their recordings. Default: guessed from the other users' (their own initials folder).
* `--option PLUGIN.OPTION=VALUE`: A plugin's per-user option, e.g. twophoton.source=/mnt/share/MA; repeatable. Default: guessed like the transfer directory.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

## `octacam twophoton`

2-photon data on the destination: verify, sweep, renumber, render.

**Usage**:

```console
$ octacam twophoton [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `verify`: Which 2P folders on the share are...
* `sweep`: Copy the share's settled 2P data no take...
* `manifest`: Rewrite every day's 2p_reconciliation.md...
* `reconcile`: Renumber flies' sessions into one...
* `reassemble`: Stack ThorImage folders still holding...
* `render`: Render the synced behavior + 2P video...

### `octacam twophoton verify`

Which 2P folders on the share are verifiably at the destination, and so safe to delete. Compares every file (a stacked channel page by page); exits 1 on any mismatch. Read-only unless --delete, which deletes only the folders this run verified.

**Usage**:

```console
$ octacam twophoton verify [OPTIONS] [paths]...
```

**Arguments**:

* `paths...`: Only these experiment (or 2P) folders on the share.

**Options**:

* `-c, --config <directory>`: The rig's config directory: its [transfer].directory and the twophoton plugin's `source` and other options.  [required]
* `--delete`: Delete the share folders found identical at the destination.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam twophoton sweep`

Copy the share's settled 2P data no take claims, under its fly (else 2p_only/<experiment>/<date>/), and pair takes still waiting for theirs. `octacam process` does this after each run.

**Usage**:

```console
$ octacam twophoton sweep [OPTIONS]
```

**Options**:

* `-c, --config <directory>`: The rig's config directory: its [transfer].directory and the twophoton plugin's `source` and other options.  [required]
* `--dry-run`: List what would be done, change nothing.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam twophoton manifest`

Rewrite every day's 2p_reconciliation.md on the destination.

**Usage**:

```console
$ octacam twophoton manifest [OPTIONS]
```

**Options**:

* `-c, --config <directory>`: The rig's config directory: its [transfer].directory and the twophoton plugin's `source` and other options.  [required]
* `--dry-run`: List what would be done, change nothing.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam twophoton reconcile`

Renumber flies' sessions into one RecordingN_<Beh|2P|Synced> sequence. `octacam process` does this for the flies it touches; this covers older data and flies spread over several runs. Every rename goes to the fly's reconciliation_log.md.

**Usage**:

```console
$ octacam twophoton reconcile [OPTIONS] [paths]...
```

**Arguments**:

* `paths...`: Day (experiment) or fly folders on the destination.

**Options**:

* `-c, --config <directory>`: The rig's config directory: its [transfer].directory and the twophoton plugin's `source` and other options.  [required]
* `--all`: Every fly on the destination (slow over a share), and turn the generic bucket's groups into new flies.
* `--dry-run`: List what would be done, change nothing.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam twophoton reassemble`

Stack ThorImage folders still holding per-frame TIFFs (copied by hand, or before stacking existed): one stack per channel, every page compared with its frame, then the frames deleted.

**Usage**:

```console
$ octacam twophoton reassemble [OPTIONS] [paths]...
```

**Arguments**:

* `paths...`: Folders to search (default: the whole destination).

**Options**:

* `-c, --config <directory>`: The rig's config directory: its [transfer].directory and the twophoton plugin's `source` and other options.  [required]
* `--dry-run`: List what would be done, change nothing.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.

### `octacam twophoton render`

Render the synced behavior + 2P video (synced_2p.mp4, beside the grid) of each paired take and the MIPs of each Z-stack under *paths*: older data, or other settings (--force). `octacam process` renders new data itself.

**Usage**:

```console
$ octacam twophoton render [OPTIONS] {paths}...
```

**Arguments**:

* `paths...`: Transferred takes, or any folder above them (searched).  [required]

**Options**:

* `--synced / --no-synced`: Behavior + 2P videos.  [default: synced]
* `--mip / --no-mip`: Z-stack MIPs.  [default: mip]
* `--force`: Re-render existing outputs.
* `--green <str>`: ThorImage channel shown green.  [default: ChanA]
* `--red <str>`: ThorImage channel shown red.  [default: ChanB]
* `--fastz-projection <str>`: A FastZ timepoint's planes: mean or max.  [default: mean]
* `--fps <float>`: Synced video frame rate.  [default: 25.0]
* `--height <int>`: Synced video height (px).  [default: 720]
* `--smooth <int>`: 2P box-blur radius (px, 0: off).  [default: 1]
* `--dry-run`: List what would be done, change nothing.
* `-v, --verbose`: Debug messages, and a traceback on errors.
* `--help`: Show this message and exit.
