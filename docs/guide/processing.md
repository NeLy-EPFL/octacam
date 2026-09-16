# Processing recordings

Everything after recording — transcoding to archival video, optionally tiling
cameras into composite **grid** videos, and copying to shared storage — is a
single command:

```bash
octacam process <paths…>
```

There is no `--config` to pass. Each recording carries an embedded snapshot of
its rig config (see [Recording](recording.md#the-embedded-config-snapshot)), and
`process` reads the encoder args, grid layouts, and transfer destination from
that snapshot. Point it at recording folders (or, with `-r`, parent directories)
and it does the rest.

## The three steps

`process` runs three steps in order; skip any of them:

| Step | Skip flag | What it does |
| --- | --- | --- |
| Transcode | `--no-transcode` | Re-encode each recording to mp4 per its `recording_summary.json`. |
| Grid | `--no-grid` | Build one composite tiled video per `[[visualization]]` entry — **opt-in**, see below. |
| Transfer | `--no-transfer` | Copy the outputs to the `[transfer]` destination. |

Transcode and transfer run by default. The grid step only does something for a
rig whose config asks for one, so `--no-transcode --no-transfer` regenerates just
the grids after a layout change, and `--no-grid` skips even the configured ones.

Other options:

| Option | Purpose |
| --- | --- |
| `-r`, `--recursive` | Recurse into the given folders. |
| `--force` | Re-transcode / rebuild grids even when the output already exists. |
| `--delete-source`, `-d` | Delete each `.mkv`/`.raw` once it transcodes successfully (the summary is always kept). |
| `--config`, `-c` | Fallback config dir for recordings that lack an embedded snapshot. |
| `--dry-run` | List what each step would do (files to transcode, grids to build, files to transfer) without writing, copying, or deleting anything. |
| `--progress-style` | `octacam` (default) or `ffmpeg` (stream ffmpeg's native output). |

## Transcoding

Each folder is transcoded to mp4 according to its `recording_summary.json`;
encoder settings come from the embedded config's `[transcode].ffmpeg_params`,
not the command line. Recordings are reproduced as saved — any display transform
was already baked in at record time (see
[transformed vs raw](recording.md#transformed-vs-raw-frames)).

`process` accepts any mix of recording folders. Already-transcoded outputs are
skipped unless you pass `--force`. Pass `--delete-source`/`-d` to delete each
source `.mkv`/`.raw` once it transcodes successfully.

Progress is shown as an octacam-style bar (`[i/N] name`, percent, fps, speed,
elapsed) reformatted live from ffmpeg's output. Use `--progress-style ffmpeg` to
stream ffmpeg's own output verbatim instead.

## Grid video

`process` can generate one composite video per recording folder that tiles the
cameras in a configurable grid, right after the individual files are transcoded.

This is **off by default**: each grid comes from a `[[visualization]]` entry in
the rig's config, and a config without one builds no composite (compositing is
minutes of extra ffmpeg per folder, which only the rigs that actually watch a
grid should pay). Add an entry to turn it on; list several to produce several
composites.

```toml
[[visualization]]
name = "grid.mp4"            # output filename inside each recording folder
layout = [
    ["camera_LF", "",          "camera_RF"],
    ["camera_LM", "camera_F",  "camera_RM"],
    ["camera_LH", "",          "camera_RH"],
]
```

Each cell is a camera name (as declared in `[[cameras]]`); an empty string `""`
places a black fill. All rows must have the same number of columns.

- With no `[[visualization]]` entry, no grid is built (the step logs that it
  skipped and moves on). `octacam config` offers to write a near-square layout of
  your cameras when scaffolding a rig.
- A cell naming a camera that isn't in `[[cameras]]` is reported (and renders
  black) rather than failing silently.

To regenerate grids for already-transcoded folders without re-running the
transcode:

```bash
# single folder — grid only
octacam process /data/octacam/260620-wt/Fly1/001-bhv --no-transcode --no-transfer

# a whole experiment tree at once — grids only
octacam process /data/octacam/260620-wt -r --no-transcode --no-transfer
```

## Transfer

`process` copies all transcoded mp4s, grid videos, and `recording_summary.json`
to the transfer destination (a network share or any writable path), mirroring
the recording's directory tree.

```toml
[transfer]
directory = "/mnt/store/matthias"    # strftime %-codes expand here too
checksum = true                      # content-verify each copy (default)
```

The destination is `transfer.directory` joined with the recording's
`relative_directory` (resolved at record time and stored in the summary), so a
recording made under `.../260620-wt/Fly1/001-bhv` lands at
`<transfer.directory>/260620-wt/Fly1/001-bhv`, and distinct trials that share a
name never collide.

**Integrity and resume.** Each file is streamed to a temporary name and only
swapped onto its final name once it is whole and content-verified (a blake2b
checksum of the source is compared against the written copy), so an interrupted
copy never leaves a truncated file masquerading as complete. Re-running skips
files already present (by size), so a killed copy simply resumes — at most the
one in-progress file is redone. Set `checksum = false` for a faster size-only
verify on trusted, fast links.

## Selecting recordings from the cache

Instead of typing paths, let octacam remember where it recorded. Every finished
recording (from the GUI or `octacam record`) is noted in a small cache under
`~/.cache/octacam` (override with `OCTACAM_CACHE_DIR`):

```bash
octacam process --last          # the most recent recording folder
octacam process --last session  # every folder from the last GUI session
octacam process --all           # every folder still in the cache
```

- `--last` takes an optional value: bare `--last` (or `--last recording`) is the
  single most recent recording folder; `--last session` is the *most recent*
  session. `--session-id <id>` names an exact session (useful when a later
  recording would otherwise steal "most recent").
- When a GUI session ends, octacam prints ready-to-run `--last session` and
  `--all` commands.
- Folders deleted between recording and processing are silently skipped.
- The cache prunes itself (entries older than 30 days are dropped on each
  write), so it never grows without bound.

## The one-command end-of-day workflow

Because each recording embeds its own config, the entire pipeline — transcode →
grid → transfer — is one command:

```bash
octacam process --all
```

Each step shows a live progress bar (frame/fps/speed for transcode and grid;
MB/s per file for transfer, then a verify pass).

### Seeing what is left to process

Add `--dry-run` to list the work a run *would* do, without doing any of it:

```bash
octacam process --all --dry-run
```

It names each file still to transcode (and, with `-d`, each source it would
delete), each grid still to build, and each file not yet at the transfer
destination, then counts what is already done. A fully processed recording
lists no work, only counts. Nothing is encoded, copied, or deleted, and a dry
run never pauses for a live capture, so it is safe to run mid-session. It is
also a quick way to check transfer paths on a new workstation. A grid whose
input videos already exist is shown as its exact ffmpeg command.

## Running in the background (detached)

Processing a full experiment can take a while, and over SSH a foreground
`octacam process` dies when the connection drops. Add `--detach` to run the same
pipeline as a background job that survives disconnect (no `tmux` needed):

```bash
octacam process --all --detach      # prints a job id, returns immediately
```

Manage detached jobs with `octacam jobs`:

```bash
octacam jobs list                   # id, state, phase, progress, age
octacam jobs attach                 # follow the most recent job's live log + bar
octacam jobs attach <job-id>        # …or a specific one
octacam jobs pause                  # park it at the next file/folder boundary
octacam jobs resume                 # clear a manual pause
octacam jobs cancel                 # stop it cleanly (finished work is kept)
```

`attach` is tmux-like: it streams the job's log and progress, and **Ctrl-C only
detaches the viewer** — the job keeps running, and you can reattach any time.
Job state lives under `~/.cache/octacam/jobs/` (override with `OCTACAM_CACHE_DIR`);
a job that finished is kept for 30 days so you can still read its final log.

From the GUI, the **shut down** button offers a *Shut down & process* choice: it
starts a detached job for the session's recordings as it exits, which you then
watch from a terminal with `octacam jobs attach`.

!!! info "Processing auto-pauses while the cameras are in use"
    Transcoding runs slow x264 presets and saturates the CPU/GPU, so it would
    compete with live capture and risk dropped frames. A running `octacam
    process` (detached or foreground) therefore **pauses between files/folders
    while an `octacam gui`/`octacam record` on the same machine owns the
    cameras**, and resumes automatically when they are free. The pause releases
    the encoder between units (it holds no GPU/NVENC session while paused) and is
    crash-safe — if the gui/record crashes, the job resumes on its own. `octacam
    gui`/`record` also print a one-line note at startup when a job is active.
