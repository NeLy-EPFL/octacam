# CLI reference

Every command supports `-h` / `--help`, which prints the authoritative,
up-to-date option list (with Rich-styled panels). This page summarizes them.

```
octacam [GLOBAL OPTIONS] COMMAND [ARGS]
```

## Global options

| Option | Purpose |
| --- | --- |
| `--log-level`, `-l` | Logging verbosity: `debug` \| `info` \| `warning` \| `error` (default `info`). |
| `--version` | Print the version and exit. |
| `-h`, `--help` | Show help (on the root or any command). |

## `gui`

```bash
octacam gui [CONFIG_DIR]
```

Launch the live web GUI for the cameras in `CONFIG_DIR` (default: current
directory). See the [Web GUI guide](../guide/gui.md).

| Option | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. Keep loopback and tunnel over SSH for remote use. |
| `--port` | `8765` | Port to bind. |
| `--no-browser` | off | Don't auto-open a browser (also auto-skipped over SSH / headless). |
| `--plugin <name>` | — | Enable a [plugin](../guide/plugins.md) (repeatable). |
| `--no-plugins` | off | Disable all plugins for this launch. |
| `--user`, `-u` | — | Apply this person's `[transfer.users.<user>]` save-destination override for the whole session — see [Processing → per-user transfer profiles](../guide/processing.md#per-user-transfer-profiles). |

## `doctor`

```bash
octacam doctor [CONFIG_DIR]
```

Diagnose the install and, optionally, a rig. Lists detected cameras and bundled
plugins and checks the encoding toolchain, storage, recording cache, and runtime
conflicts. Passing `CONFIG_DIR` also validates that rig's config, resolves its
save/transfer paths, and cross-checks declared vs detected cameras. It never
opens a camera, so it is safe to run while a session is live.

| Option | Purpose |
| --- | --- |
| `--backend <name>` | Only enumerate this backend (`basler`/`flir`/`spinnaker`/`pycameleon`/`fake`). Default: the whole available cascade. |
| `--json` | Emit machine-readable JSON instead of the report. |
| `--check` | Exit non-zero on warnings too (for CI), not only on errors. |
| `--probe-serial` | Also open each detected serial port briefly to read its firmware identity (skips ports held by a running session; skip if a board may be armed). |
| `--user`, `-u` | Also validate this person's `[transfer.users.<user>]` override resolves correctly. Reported as a warning line, never aborts the rest of the checks. |

Exits `0` when no errors are found, so it works as a pre-flight check in scripts.

## `config`

```bash
octacam config [CONFIG_DIR]
```

Interactively scaffold a new rig's `octacam_config.toml`: auto-detects the
connected cameras, prompts for the record/transfer settings and an optional serial
plugin, then writes the file (visual per-camera placement is left to `octacam
gui`). If `CONFIG_DIR` is omitted you are prompted for one.

| Option | Purpose |
| --- | --- |
| `--backend <name>` | Pin the rig to one backend (`basler`/`flir`/`spinnaker`/`pycameleon`/`fake`). Default: auto-detect through the cascade. |
| `--force` | Overwrite an existing `octacam_config.toml` without asking. |
| `--snapshot-params` / `--no-snapshot-params` | Open each detected camera once to save its current sensor parameters (`.pfs`/`.txt`); busy cameras are skipped. On by default. |
| `--bootstrap-users <nas-root>` | Separate mode: scan `<nas-root>`'s immediate subdirectories for initials-style lab-member folders and add a `[transfer.users.<initials>]` entry to `CONFIG_DIR`'s existing config for each one not already present — see [Processing → per-user transfer profiles](../guide/processing.md#per-user-transfer-profiles). Requires `CONFIG_DIR`. Safe to re-run. |
| `--dry-run` | With `--bootstrap-users`: log what would be added without writing anything. |

## `record`

```bash
octacam record [CONFIG_DIR]
```

Record headlessly (no browser). Encoding, save method, transform, and the
save-directory template come from the config's `[record]` section; the options
override only the day-to-day values. See [Recording](../guide/recording.md).

| Option | Purpose |
| --- | --- |
| `--fps`, `-f` | Frame rate (default: from config). |
| `--duration`, `-d` | Duration in seconds (default: from config). |
| `--output`, `-o` | Save directory, overriding the templated location. |
| `--yes`, `-y` | Don't prompt: reflash a serial plugin's out-of-date board firmware before recording (also lets a headless run flash). |
| `--plugin <name>` | Enable a plugin (repeatable). |
| `--no-plugins` | Disable all plugins for this run. |
| `--user`, `-u` | Apply this person's `[transfer.users.<user>]` save-destination override — resolved once and baked into this recording's own config snapshot, so `process` needs no `--user` for it later. See [Processing → per-user transfer profiles](../guide/processing.md#per-user-transfer-profiles). |

## `flash`

```bash
octacam flash [CONFIG_DIR]
```

Check a serial plugin's board firmware against the bundled Arduino sketch and,
unless `--check`, compile + upload the current sketch with arduino-cli. Pass
`CONFIG_DIR` (whose serial plugins to check), `--plugin`, or both.

| Option | Purpose |
| --- | --- |
| `--plugin <name>` | Serial plugin whose firmware to manage (e.g. `triggerbox`); enables it even if not in the config. |
| `--device <path>` | Serial device override (e.g. `/dev/ttyACM0` or `auto`). |
| `--yes`, `-y` | Flash without prompting when out of date. |
| `--check` | Report only; exit nonzero if any board is out of date. Never flashes. |

## `benchmark`

```bash
octacam benchmark [CONFIG_DIR]
```

A short instrumented dry-run against the cameras in `CONFIG_DIR` (no video is
kept): tests whether the target frame rate is achievable, searches for the
maximum achievable rate, and measures each pipeline stage — **acquire** (trigger
+ exposure + USB transfer), **transform**, **enqueue**, **encode** — so you can
see the limiting step. It reports the acquisition and encode ceilings, a verdict
(achievable / not, with the bottleneck), and targeted recommendations.

Like `record` it opens the cameras, so it cannot run at the same time as a live
GUI or recording on the same rig. Exits nonzero when the target fps is not
achievable, so it works as a pre-flight check in scripts. The same benchmark is
available from the GUI's **Benchmark** tab.

| Option | Default | Purpose |
| --- | --- | --- |
| `--fps`, `-f` | from config | Target fps to test. |
| `--duration`, `-d` | `5` | Seconds spent measuring each scenario. |
| `--find-max` / `--no-find-max` | on | Search for the maximum achievable fps (software trigger only). |
| `--freerun` / `--no-freerun` | on | Also measure the free-run (external-trigger-equivalent) ceiling. |
| `--sink` | `config` | `config` (encode through the rig's real save method — measures the encode cost) or `null` (discard frames to isolate acquisition). |
| `--record-form` | from config | `display` (bake the transform) or `sensor`. |
| `--backend` | from config | Override the camera backend. |
| `--json` | off | Emit the report as JSON instead of the table. |

## `process`

```bash
octacam process [PATHS…]
```

Transcode recordings to mp4, build composite grid videos, and transfer to
storage — all driven by each recording's embedded config snapshot. Pass
recording folders (or parent directories with `-r`), or select from the cache
with `--last` / `--last session` / `--all`. See
[Processing](../guide/processing.md).

**Selecting what to process** (mutually exclusive; can't combine with explicit
`PATHS`):

| Option | Purpose |
| --- | --- |
| `--last` (or `--last recording`) | The most recent recording folder. |
| `--last session` | Every folder from the last GUI session. |
| `--session-id <id>` | Every folder from one exact session id. |
| `--all` | Every recording folder still in the cache. |
| `--twophoton-sweep` | Separate mode (ignores the selectors above and `PATHS`): transfer settled 2P-only folders with no matching behavior take. Requires `--config`. |
| `--migrate-layout` | Separate mode (ignores the selectors above and `PATHS`): reorganize recordings transferred under the old flat layout into `Behavior`/`Renderings` subfolders, in place (a same-filesystem rename, no data copied). Requires `--config`. |

**Controlling the steps:**

| Option | Purpose |
| --- | --- |
| `-r`, `--recursive` | Recurse into the given folders. |
| `--no-transcode` | Skip transcoding; grid/transfer act on existing mp4s. |
| `--no-grid` | Skip building the grid video(s). |
| `--no-transfer` | Skip transferring to the `[transfer]` destination. |
| `--force` | Re-transcode / rebuild grids even if outputs already exist. |
| `--delete-source`, `-d` | Delete each `.mkv`/`.raw` once it transcodes successfully. Also on via `[transcode].delete_source`; `--no-delete-source` overrides either way. |
| `--delete-after-transfer` | Once a folder's transfer (behavior + any matched 2P data) is checksum-verified on the NAS, delete the local recording folder and matched 2P source folder(s). Also on via `[transfer].delete_after_transfer`; `--no-delete-after-transfer` overrides either way. |
| `--no-twophoton-sweep` | Skip the automatic 2P-only sweep this run otherwise does, sequentially, for every `[transfer.twophoton]` source it touched (same logic as `--twophoton-sweep`, run automatically rather than as a separate command). |
| `--config`, `-c` | Fallback config dir for recordings with no embedded snapshot (or the rig config for `--twophoton-sweep`/`--migrate-layout`/`--reassemble-tiffs`). |
| `--user`, `-u` | Apply this person's `[transfer.users.<user>]` save-destination override. Only affects recordings with no embedded config (falling back to `--config`) and the `--twophoton-sweep`/`--migrate-layout`/`--reassemble-tiffs` modes — a recording with its own embedded snapshot already has its destination baked in from record time. Exits with an error listing known users on a typo. |
| `--progress-style` | `octacam` (default) or `ffmpeg` (native output). |
| `--dry-run` | Log the intended grid/transfer work without writing anything. |
| `--detach` | Run the pipeline as a background job that survives SSH disconnect; print its id and return. Manage it with `octacam jobs`. |

See [Processing → 2-photon transfer](../guide/processing.md#2-photon-transfer)
for `[transfer.twophoton]`, the NAS layout it produces, and
`--delete-after-transfer`'s safety contract; see
[Processing → per-user transfer profiles](../guide/processing.md#per-user-transfer-profiles)
for `[transfer.users.<initials>]` and `--user`.

A running job (detached or foreground) auto-pauses while an `octacam gui`/`record`
on the same machine owns the cameras, and resumes when they are free. See
[Processing](../guide/processing.md#running-in-the-background-detached).

## `jobs`

```bash
octacam jobs list
octacam jobs attach [JOB]
octacam jobs pause  [JOB]
octacam jobs resume [JOB]
octacam jobs cancel [JOB]
```

Manage detached `octacam process --detach` jobs. `JOB` is a job id from
`octacam jobs list`; omit it to target the most recent job.

| Command | Purpose |
| --- | --- |
| `list` | Show each job's id, state, phase, progress, and age. |
| `attach` | Follow a job's live log + progress (Ctrl-C detaches — it does **not** cancel). |
| `pause` | Pause a running job (it parks at the next file/folder boundary). |
| `resume` | Clear a manual pause (a gui/record auto-pause clears on its own). |
| `cancel` | Stop a running job cleanly; already-finished work is kept. |

## `cache`

```bash
octacam cache info
octacam cache path
octacam cache clear [--all] [--yes]
```

Inspect and clear octacam's on-disk cache under `~/.cache/octacam` (see
`OCTACAM_CACHE_DIR` below): the recording list backing `octacam process
--last/--all`, detached-job logs, and small activity markers.

| Command | Purpose |
| --- | --- |
| `info` | Show the cache location, total size, and a breakdown (recordings, jobs, live markers). |
| `path` | Print the cache directory (for scripting). |
| `clear` | Remove the recording list and stale activity markers. |

`clear` **never touches** a live capture, transcode, or detached job — those are
reported as kept. Finished detached-job logs are kept by default; add `--all` to
remove them too. `--yes` (`-y`) skips the confirmation prompt.

## Environment variables

| Variable | Effect |
| --- | --- |
| `PYLON_CAMEMU` | Number of emulated Basler cameras (run without hardware). |
| `OCTACAM_CACHE_DIR` | Override the octacam cache location (recording list, job logs, markers; default `~/.cache/octacam`). Inspect/clear it with `octacam cache`. |
| `OCTACAM_FFMPEG` | Path to an ffmpeg binary to use instead of the bundled one. |
