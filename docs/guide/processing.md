# Post-processing recordings

Recording writes fast, near-lossless capture files. Everything after that —
re-encoding to compact archival video, tiling the cameras into a composite
**grid**, and mirroring results to a NAS or shared store — is done offline by
three commands:

| Command | Purpose |
| --- | --- |
| [`octacam transcode`](#transcoding) | Re-encode each recording to a compressed `.mp4`. Can also chain the grid and NAS steps. |
| [`octacam grid`](#grid-composites) | Build a single composite grid video from a folder's transcoded `.mp4` files. |
| [`octacam nas`](#nas-mirroring) | Copy transcoded results to a NAS or any writable path, mirroring the directory tree. |

`octacam transcode` is the main entry point and can run all three phases in one
pass; `grid` and `nas` are standalone commands for re-running just that phase.

!!! warning "Don't transcode while capturing"
    Transcoding runs slow x264 presets across many files and saturates the CPU,
    so it competes with live capture and can cause dropped frames. `octacam gui`
    and `octacam record` warn at startup when an `octacam transcode` is already
    running on the same machine.

## What a recording folder contains

Each recording's save directory holds the files these commands consume:

| File | When present |
| --- | --- |
| `<camera>.mkv` | Per camera, with the default `x264` codec (H.264 in a Matroska container). |
| `<camera>.raw` + `<camera>.json` | Per camera, with the `raw` codec — a Mono8 dump plus a small geometry sidecar (`width`/`height`/`pixel_format`/`fps`). |
| `recording_summary.json` | Always. Lists each camera's file, resolution, fps, frame count, and display transform. |
| `<camera>.csv` | Only when frame-timestamp saving was enabled — a per-frame `frame_index,timestamp,dropped` log. |

A folder is treated as a recording directory when it contains a
`recording_summary.json`; that is also how `-r/--recursive` identifies
recordings nested under a parent directory.

!!! note "ffmpeg"
    All three commands shell out to ffmpeg. The bundled `imageio-ffmpeg` binary
    is used by default; set `OCTACAM_FFMPEG=/path/to/ffmpeg` to override it (a
    system ffmpeg on `PATH` is the last fallback).

## Transcoding

```bash
octacam transcode <paths…>
```

`transcode` re-encodes each recording to a compressed `.mp4`. Captures are
written with a fast preset to keep up with the cameras, so this offline pass —
defaulting to `--preset veryslow --crf 20` — is where the compression is
actually earned. `.mkv`/`.mp4` sources are **always re-encoded** (never
stream-copied); a `.raw` source is encoded from its Mono8 dump, with the exact
frame count derived from the file size.

`<camera>.mkv` becomes `<camera>.mp4` in the same folder. A folder with a
`recording_summary.json` is driven by it (each listed camera file is transcoded
with its recorded settings); a folder without one has its loose `.mkv`/`.raw`
files transcoded with defaults and a warning.

| Option | Default | Effect |
| --- | --- | --- |
| `-r`, `--recursive` | off | Recurse into the given folders. |
| `--format` | `mp4` | Output container. |
| `--crf` | `20` | x264 quality (lower = better; 0 = lossless). |
| `--preset` | `veryslow` | x264 speed preset. |
| `--pix-fmt` | `gray` | Output pixel format. |
| `--x264-params` | *(none)* | Extra libx264 options, e.g. `"keyint=30:scenecut=0"`. |
| `--as-displayed` / `--as-saved` | `--as-saved` | Bake each camera's recorded rotation/flip transform into the video, or reproduce it as saved. |
| `--remove-source` | off | Delete each source `.mkv`/`.raw` (and a `.raw`'s `.json` sidecar) once it transcodes successfully. `recording_summary.json` is always kept. |
| `--progress-style` | `octacam` | `octacam` reformats ffmpeg's progress into an octacam-style bar; `ffmpeg` streams ffmpeg's own output verbatim. |

```bash
# One trial, reproduced as saved
octacam transcode /data/octacam/260620-wt/Fly1/001-bhv

# A whole experiment tree, baking in each camera's display transform
octacam transcode /data/octacam/260620-wt -r --as-displayed
```

!!! info "Display transform"
    With `--as-displayed`, each camera's recorded rotation/flip is applied via an
    ffmpeg `-vf` filter chain — unless the transform was already baked into the
    pixels at record time, in which case it is skipped so it is never applied
    twice.

!!! note "Empty recordings are skipped"
    A camera that captured 0 frames leaves a header-only file (the usual cause is
    an external trigger that never fired). `transcode` detects this from the
    summary and skips it with a clear message instead of feeding ffmpeg an empty
    file and producing a cryptic error.

**Interrupted transcodes are safe.** Each encode is written to a hidden
`.<name>.octacam-part.<ext>` temp and atomically renamed onto the final `.mp4`
only once it is whole; any failure or Ctrl-C removes the temp, so a partial
encode never appears at the final name and never clobbers an existing output.
Ctrl-C stops the batch where it stands — files already finished keep their
outputs.

## Selecting recordings from the cache

Instead of typing paths, let octacam recall where it recorded. Every finished
recording (from the GUI or `octacam record`) is noted in a small cache under
`~/.cache/octacam/recordings.jsonl` (override with `OCTACAM_CACHE_DIR`, or
relocate via `XDG_CACHE_HOME`):

```bash
octacam transcode --last          # the most recent recording folder
octacam transcode --session       # every folder from the last GUI session
octacam transcode --all           # every folder still in the cache
```

| Selector | Selects |
| --- | --- |
| `--last` (`--last-recording`) | The single most recent recording folder. |
| `--session` (`--last-session`) | Every folder from the most recent GUI/record session. |
| `--session-id <id>` | Every folder from one exact session — the id the GUI prints on exit. Unlike `--session`, a later recording can't hijack it. |
| `--all` | Every folder the cache still holds (all sessions, all days). |

- The four selectors are mutually exclusive and cannot be combined with explicit
  `PATHS`; giving neither is an error.
- When a GUI session ends, octacam prints the ready-to-run `--session` and
  `--all` commands for what it just recorded.
- Folders deleted between recording and transcoding are silently skipped.
- The cache prunes itself on each write (entries older than 30 days are dropped),
  so it never grows without bound.

## Grid composites

```bash
octacam grid <paths…>
```

`grid` tiles a recording folder's per-camera **transcoded** `.mp4` files into a
single composite video (`grid.mp4` by default) using ffmpeg's `xstack`. Run it
after `transcode`, or generate the grid as part of transcoding (see
[below](#one-pass-transcode-grid-nas)).

The layout is a 2-D grid of camera names; an empty string `""` is a black fill
cell. It is resolved in this order:

1. The `[grid] layout` section of the rig config passed with `--config`/`-C`.
2. Otherwise, a near-square layout derived from that config's `[[cameras]]`.
3. Otherwise (no `--config`), the built-in 7-camera default layout.

| Option | Default | Effect |
| --- | --- | --- |
| `-r`, `--recursive` | off | Process every recording directory found beneath each path. |
| `--config`, `-C` | *(none)* | Rig config directory whose `[grid]` section supplies the layout. |
| `--output-name`, `-o` | `grid.mp4` | Output filename inside each folder. |
| `--crf` | `20` | x264 quality. |
| `--preset` | `veryslow` | x264 speed preset. |
| `--pix-fmt` | `yuv420p` | Output pixel format; the default is required for QuickTime / Keynote playback. |
| `--dry-run` | off | Log the ffmpeg command (and, with `-r`, the folders) without running it. |

```bash
# Every trial in an experiment, using the rig's configured layout
octacam grid ~/data/MD/260624_ -r --config configs/2p_1
```

- A cell naming a camera whose `.mp4` is missing is filled with a black frame, so
  a partial set still composites.
- Cells whose resolution differs from the reference are letterboxed (centred with
  black bars), never stretched.
- The output is always `yuv420p` with integer fps for broad player compatibility,
  regardless of the per-camera pixel format.

## NAS mirroring

```bash
octacam nas <paths…> --nas-path /mnt/nas/matthias
```

`nas` copies each recording's `.mp4` files (individual cameras and `grid.mp4`
when present) plus its `recording_summary.json` to a destination root — a NAS
mount or any writable path. `--nas-path` is required.

| Option | Default | Effect |
| --- | --- | --- |
| `--nas-path` | *(required)* | Destination root, e.g. `/mnt/nas/matthias`. |
| `--nas-local-base` | *(auto)* | Local root to strip when building the destination sub-path, so the directory tree is mirrored. |
| `-r`, `--recursive` | off | Copy every recording directory found beneath each path. |
| `--verify` / `--no-verify` | `--verify` | Content-verify (blake2b) each copy before promoting it; `--no-verify` falls back to a size-only check for trusted/fast links. |
| `--checksum` | off | For files already on the NAS, decide skip-vs-recopy by full checksum rather than size (repair mode). |
| `--dry-run` | off | Log what would be copied without touching any files. |

**Path mirroring.** With `--nas-local-base /home/nely/data/MD`, a recording at
`…/MD/260624_/Fly1/001-bhv` lands at `<nas-path>/260624_/Fly1/001-bhv`, so fly
and trial identity are preserved. When the base is omitted and several
recordings are copied at once, their common parent is used automatically so
same-named trials (two `001-bhv`, say) never collide; a colliding destination is
detected and refused before any copy runs.

```bash
octacam nas ~/data/MD/260624_ -r \
    --nas-path /mnt/nas/matthias \
    --nas-local-base ~/data/MD
```

**Integrity and resume.** Each file is streamed to a unique hidden temp in the
destination directory, fsync'd, optionally content-verified against the source,
and only then atomically renamed onto its final name — so an interrupted copy
never leaves a truncated file masquerading as complete. Re-running skips files
already present (by size, or by full checksum with `--checksum`), so a killed
copy resumes at file granularity rather than starting over.

## One pass: transcode + grid + NAS

`octacam transcode` can run the grid and NAS phases automatically after each
folder is encoded, so the whole end-of-day pipeline is a single command:

```bash
octacam transcode --all --config configs/2p_1
```

The extra options:

| Option | Effect |
| --- | --- |
| `--grid` / `--no-grid` | Force the grid on or off. When omitted, the `[grid] default` in `--config` decides. |
| `--config`, `-C` | Rig config directory supplying the grid layout, the `[grid]`/`[nas]` defaults, and the NAS local-base. |
| `--nas-path` | Copy results here after each folder. Overrides `[nas] path` from `--config`. |
| `--nas-local-base` | Local root to strip for NAS mirroring. Overrides `[nas] local_base`. |
| `--nas-verify` / `--no-nas-verify` | Content-verify each NAS copy. When omitted, `[nas] verify` decides (default on). |
| `--nas-checksum` | Skip-by-checksum repair mode for already-present NAS files. |
| `--dry-run` | For the grid and NAS phases only, log what would be done without running ffmpeg or copying — transcoding still runs normally. |

Effective settings resolve as **explicit CLI flag > `--config` value > off**. The
grid produced inside `transcode` is always `yuv420p` (the per-camera transcode
`--pix-fmt` is not forwarded to it), and the grid `.mp4` is appended to each
folder's NAS copy list. The grid and NAS phases run sequentially so their
progress bars don't compete for the console.

## Config reference

Both post-processing steps read their defaults from the rig's
`octacam_config.toml` when a config directory is supplied.

```toml
[grid]
default = true              # auto-generate the grid during `octacam transcode --config …`
layout = [
    ["camera_LF", "",          "camera_RF"],
    ["camera_LM", "camera_F",  "camera_RM"],
    ["camera_LH", "",          "camera_RH"],
]

[nas]
path = "/mnt/nas/matthias"  # destination root; enables the NAS phase in `transcode --config …`
local_base = "/home/nely/data/MD"
verify = true               # content-checksum each copy before promoting it (false = size-only)
```

- `[grid] layout` cells are camera names as declared in `[[cameras]]`; every row
  must be the same length, and `""` is a black cell. A cell naming an unknown
  camera is reported and renders black.
- `[grid] default = true` makes `octacam transcode --config <dir>` build the grid
  without needing `--grid`.
- `[nas] path` set, plus `--config`, makes `octacam transcode` copy to the NAS
  without needing `--nas-path`.

Any CLI flag overrides the matching config value.
