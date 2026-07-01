# Processing recordings

Everything after recording — transcoding to archival video, tiling cameras into
composite **grid** videos, and copying to shared storage — is a single command:

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
| Grid | `--no-grid` | Build one composite tiled video per recording folder. |
| Transfer | `--no-transfer` | Copy the outputs to the `[transfer]` destination. |

So `--no-transcode --no-transfer` regenerates just the grids after a layout
change, and `--no-grid` skips the composite step.

Other options:

| Option | Purpose |
| --- | --- |
| `-r`, `--recursive` | Recurse into the given folders. |
| `--force` | Re-transcode / rebuild grids even when the output already exists. |
| `--remove-source` | Delete each `.mkv`/`.raw` once it transcodes successfully (the summary is always kept). |
| `--config`, `-C` | Fallback config dir for recordings that lack an embedded snapshot. |
| `--dry-run` | Log the intended grid/transfer work without writing anything. |
| `--progress-style` | `octacam` (default) or `ffmpeg` (stream ffmpeg's native output). |

## Transcoding

Each folder is transcoded to mp4 according to its `recording_summary.json`;
encoder settings come from the embedded config's `[transcode].ffmpeg_params`,
not the command line. Recordings are reproduced as saved — any display transform
was already baked in at record time (see
[transformed vs raw](recording.md#transformed-vs-raw-frames)).

`process` accepts any mix of recording folders. Already-transcoded outputs are
skipped unless you pass `--force`. Pass `--remove-source` to delete each source
`.mkv`/`.raw` once it transcodes successfully.

Progress is shown as an octacam-style bar (`[i/N] name`, percent, fps, speed,
elapsed) reformatted live from ffmpeg's output. Use `--progress-style ffmpeg` to
stream ffmpeg's own output verbatim instead.

## Grid video

`process` generates one composite video per recording folder that tiles all
cameras in a configurable grid, right after the individual files are transcoded.
Each grid comes from a `[[visualization]]` entry in the rig's config; list
several to produce several composites.

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

- If a config lists `[[cameras]]` but no `[[visualization]]`, a near-square
  layout is derived from that rig's own cameras.
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
octacam process --session       # every folder from the last GUI session
octacam process --all           # every folder still in the cache
```

- `--session` means the *most recent* session; `--session-id <id>` names an
  exact one (useful when a later recording would otherwise steal "most recent").
- When a GUI session ends, octacam prints ready-to-run `--session` and `--all`
  commands.
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
MB/s per file for transfer, then a verify pass). `--dry-run` logs the intended
grid ffmpeg call and transfer plan without writing anything — handy for
validating paths on a new workstation.

!!! warning "Don't transcode while capturing"
    Transcoding runs slow x264 presets across many files and saturates the CPU,
    so it competes with live capture and can cause dropped frames. `octacam gui`
    and `octacam record` warn at startup when an `octacam process` is already
    transcoding on the same machine.
