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
| `--backend <name>` | Only enumerate this backend (`basler`/`flir`/`fake`). Default: every available backend. |
| `--json` | Emit machine-readable JSON instead of the report. |
| `--check` | Exit non-zero on warnings too (for CI), not only on errors. |

Exits `0` when no errors are found, so it works as a pre-flight check in scripts.

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
| `--plugin <name>` | Enable a plugin (repeatable). |
| `--no-plugins` | Disable all plugins for this run. |

## `process`

```bash
octacam process [PATHS…]
```

Transcode recordings to mp4, build composite grid videos, and transfer to
storage — all driven by each recording's embedded config snapshot. Pass
recording folders (or parent directories with `-r`), or select from the cache
with `--last` / `--session` / `--all`. See [Processing](../guide/processing.md).

**Selecting what to process** (mutually exclusive; can't combine with explicit
`PATHS`):

| Option | Purpose |
| --- | --- |
| `--last` | The most recent recording folder. |
| `--session` | Every folder from the last GUI session. |
| `--session-id <id>` | Every folder from one exact session id. |
| `--all` | Every recording folder still in the cache. |

**Controlling the steps:**

| Option | Purpose |
| --- | --- |
| `-r`, `--recursive` | Recurse into the given folders. |
| `--no-transcode` | Skip transcoding; grid/transfer act on existing mp4s. |
| `--no-grid` | Skip building the grid video(s). |
| `--no-transfer` | Skip transferring to the `[transfer]` destination. |
| `--force` | Re-transcode / rebuild grids even if outputs already exist. |
| `--remove-source` | Delete each `.mkv`/`.raw` once it transcodes successfully. |
| `--config`, `-C` | Fallback config dir for recordings with no embedded snapshot. |
| `--progress-style` | `octacam` (default) or `ffmpeg` (native output). |
| `--dry-run` | Log the intended grid/transfer work without writing anything. |

## Environment variables

| Variable | Effect |
| --- | --- |
| `PYLON_CAMEMU` | Number of emulated Basler cameras (run without hardware). |
| `OCTACAM_CACHE_DIR` | Override the recording cache location (default `~/.cache/octacam`). |
| `OCTACAM_FFMPEG` | Path to an ffmpeg binary to use instead of the bundled one. |
