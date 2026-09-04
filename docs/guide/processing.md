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
| `--delete-source`, `-d` | Delete each `.mkv`/`.raw` once it transcodes successfully (the summary is always kept). Also on when `[transcode].delete_source` is set in config; `--no-delete-source` forces it off regardless. |
| `--delete-after-transfer` | Once a folder's *entire* transfer (behavior + any matched 2P data) is checksum-verified on the NAS, delete the local recording folder and matched 2P source folder(s). Also on when `[transfer].delete_after_transfer` is set; `--no-delete-after-transfer` forces it off regardless. See [2-photon transfer](#2-photon-transfer). |
| `--twophoton-sweep` | Separate mode: transfer settled 2P-only folders with no matching behavior take instead of processing recording folders. Requires `--config`. |
| `--migrate-layout` | Separate mode: reorganize recordings already transferred under the old flat layout into `Behavior`/`Renderings` subfolders, in place. Requires `--config`. |
| `--config`, `-c` | Fallback config dir for recordings that lack an embedded snapshot (or the rig config for `--twophoton-sweep`). |
| `--dry-run` | Log the intended grid/transfer work without writing anything. |
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

`process` copies each recording to the transfer destination (a network share or
any writable path), mirroring the recording's directory tree and splitting the
destination folder by kind:

```
<transfer.directory>/<relative_directory>/
  recording_summary.json     # + octacam_config.toml, Experiment.xml if 2P-matched
  Behavior/                  # each camera's own archival .mp4
  Renderings/                # grid.mp4 and other composited/presentation output
  2P/                        # matched ThorSync/ThorImage folders, verbatim — see below
```

```toml
[transfer]
directory = "/mnt/store/matthias"    # strftime %-codes expand here too
checksum = true                      # content-verify each copy (default)
```

The destination root is `transfer.directory` joined with the recording's
`relative_directory` (resolved at record time and stored in the summary), so a
recording made under `.../260620-wt/Fly1/001-bhv` lands at
`<transfer.directory>/260620-wt/Fly1/001-bhv/...`, and distinct trials that
share a name never collide.

**Integrity and resume.** Each file is streamed to a temporary name and only
swapped onto its final name once it is whole and content-verified (a blake2b
checksum of the source is compared against the written copy), so an interrupted
copy never leaves a truncated file masquerading as complete. Re-running skips
files already present (by size), so a killed copy simply resumes — at most the
one in-progress file is redone. Set `checksum = false` for a faster size-only
verify on trusted, fast links.

## 2-photon transfer

Rigs with the `twophoton` plugin (see [Recording](recording.md)) can pair each
behavior take with its ThorSync/ThorImage folder(s) and transfer both together,
replacing a separate manual copy step:

```toml
[transfer.twophoton]
source = "/mnt/windows_share/MD"   # root to scan for ThorSync/ThorImage folders
match_window_s = 60                 # max deviation on EACH endpoint (start and end) for a timestamp-only pairing
settle_s = 300                      # quiescence delay before a 2P folder is "done"
verify_with_signals = true          # confirm a pairing by reading its actual DAQ signal (default on)
verify_window_s = 3600              # wider window bounding which SyncData folders get opened to check
```

`match_window_s` requires a candidate's start, its end, *and* its own overall
duration to each land within that many seconds of the take's own — not just
"the two windows overlap somewhere". A proper behavior/2P pair runs for
essentially the same length of time; a short, unrelated 2-photon snapshot (a
focus check, an ROI tune) nested entirely inside a much longer take satisfies
a plain overlap check, and can even satisfy "both endpoints individually
close" (a much *longer* candidate can loosely straddle a short take with both
ends close while its own duration is nothing alike), while being a spurious
match. Confirmed on a real rig with no ThorSync to verify against: its
ThorImage folders split cleanly into two populations — several 20–33s
snapshots (rejected) and several 147–159s sessions (matched, each within the
expected margin of its take's 129s duration). 60s comfortably covers the
documented ~20–40s ThorSync startup lag plus a further ~25–30s ThorImage disk
write-out lag (see **Verified matching** below) while still rejecting
deviations of 90s or more, which the real spurious snapshots showed.

When a take's `recording_summary.json` reports `plugins.twophoton.armed` (the
"arm with recording" checkbox was checked), `process` scans `source` for
ThorSync (`SyncData*`)/ThorImage (any folder with an `Experiment.xml`) folders
that pair with the take (see **Verified matching** below), waits for each
match to go mtime-quiescent for `settle_s`, then copies it verbatim into
`2P/<original-folder-name>/` next to that take's `Behavior/`/`Renderings/` —
so behavior and 2-photon data for one experiment always land at the same
place. A `twophoton_match.json` sidecar records what was matched (kind, time
gap, and whether it was `verified` or a `timestamp`-only guess) for auditing.
ThorSync and ThorImage are started independently by the operator, so a take
may match zero, one, or both kinds — nothing is forced. Every take sharing one
`[transfer.twophoton].source` is matched together, chronologically, in one
`process` run — a 2P folder already claimed by an earlier take is never
double-booked to a different one.

Neither ThorSync nor ThorImage leaves a definitive "acquisition complete"
marker; `settle_s` is the same mtime-quiescence idea a manual copy script would
use. A folder not yet settled is simply retried on the next `octacam process`
run.

### Verified matching

Real-data testing found the coarse timestamp match alone isn't reliable —
ThorImage's own folder naming doesn't track chronological order, and most
real pairings turned out to have more than one plausible candidate. Whenever
a `SyncData*` folder actually exists for a session, `process` confirms the
pairing instead of guessing: ThorSync's `Episode001.h5` records the same
camera-trigger pulse the take's cameras responded to, on its own DAQ clock
(the `Cameras` digital-input channel) — so a segment whose edge count exactly
matches the take's own recorded frame count is decisive, even if its
timestamp gap is looser than `match_window_s` would normally allow. The
paired ThorImage folder is then confirmed the same way, via the `FrameOut`
channel's edge count against that folder's own `Experiment.xml
<Timelapse timepoints="...">`. `twophoton_match.json` records `"confidence":
"verified"` for these; `"timestamp"` for a plain heuristic guess (verification
unavailable — no `h5py`, no `SyncData` folder for the session, or no edge
count actually matched).

This needs the `twophoton` extra's `h5py` dependency
(`pip install octacam[twophoton]`); without it, or when `verify_with_signals
= false`, matching falls back to timestamps only. A `SyncData` folder that
spans more than one take (ThorSync left running across several behavior
takes) can still verify-match each one individually, via a different segment
of the same file.

ThorImage's own files are **not** written in real time — it buffers frames
during acquisition and flushes them to `.tif` on disk afterward (confirmed by
comparing a verified pairing's `FrameOut` edge timing, from the DAQ, against
its files' raw mtimes: the real acquisition had already ended by the time the
first file appeared, with the whole write-out taking a further ~25–30s). This
matters for a `SyncData`-verified pairing not at all — the edge count is
authoritative regardless — but it's why a purely timestamp-only `image`-kind
match (no `SyncData` folder available to verify against at all) is
inherently the least certain part of this feature: there's no independent
signal to check it against, so `match_window_s` accommodating that write-out
lag is the best available mitigation, not a guarantee.

**Recordings made before this feature existed** have no `plugins` key at all
(`recording_summary.json`'s `schema_version < 4`) — there was never a chance
for them to record `armed`. For those, `process` falls back to attempting the
time-window match anyway (still gated on `[transfer.twophoton]` being
configured) rather than silently never pairing anything made before the
upgrade. A **schema_version 4+** take with the checkbox left unchecked
(`armed: false`) is not a legacy case and is skipped as intended.

**2P-only recordings** (2P acquired with no behavior camera running) never get
an `armed` take to pair with. A normal `octacam process` run **automatically**
sweeps every `[transfer.twophoton]` source it touched for settled folders no
take claimed — sequentially, right after per-take matching, in the same
command — and transfers each one to
`<transfer.directory>/2p_only/<experiment>/<date>/<name>/`. This is what
tells apart a real behavior/2P pair from a standalone check/tuning recording:
anything the duration-aware matcher above correctly didn't pair up lands here
instead, safely, rather than being silently dropped. Pass
`--no-twophoton-sweep` to skip it for a given run. `octacam process
--twophoton-sweep --config <rig-config>` is the same logic as its own
standalone mode — useful run by hand or on a periodic systemd timer (see the
packaging example) when there's no behavior recording to process at all; it
ignores `PATHS`/`--last`/`--all`/`--detach`.

## Migrating recordings transferred before this layout existed

Anything transferred before the `Behavior`/`Renderings`/`2P` split landed sits
flat at its destination (`camera_LF.mp4` directly under the take folder) —
indistinguishable, to the layout-aware skip check, from "not transferred yet",
so a plain re-run would copy a second set of files into the new subfolders
alongside the old flat ones. `octacam process --migrate-layout --config
<rig-config>` reorganizes those in place instead: it walks
`[transfer].directory` for every already-transferred take and **renames** each
flat file into its `Behavior`/`Renderings` subfolder — a same-filesystem
rename (true for a local disk, and for CIFS/SMB, whose rename is itself a
server-side directory-entry change), so no bytes are copied, re-read, or
re-verified, and data on the NAS is never at risk the way a copy-based
approach could be. It's idempotent (safe to re-run; already-migrated folders
are simply skipped) and self-contained (classifies each file by comparing its
name against `recording_summary.json`'s own camera list, so it works even if
the local recording is long gone). Run it once with `--dry-run` first to see
the plan; a same-named file already present at the target with a *different*
size is left alone on both sides and reported, never silently overwritten.

## Deleting local copies once verified

`--delete-after-transfer` deletes the local recording folder — and any matched
2P source folder(s) on the share — once every file in it is confirmed present
on the NAS. It always forces full checksum verification for that folder's
transfer (never a size-only check, and never weaker than `[transfer].checksum`),
and only deletes when the transfer had **zero** failures and the folder had no
transcode failure this run — a camera whose transcode failed is never deleted,
even if every other camera's output transferred cleanly. Try it explicitly per
run first (`--delete-after-transfer`); once comfortable, set
`[transfer] delete_after_transfer = true` in the rig config to make it the
default (`--no-delete-after-transfer` still overrides that either way).

This is a different, stronger safety contract than `--delete-source`/
`[transcode].delete_source`, which deletes the raw `.mkv`/`.raw` right after a
successful *transcode* — a local, fast check with no NAS round-trip at all, and
no relation to whether the transfer (or a 2P pairing) ever happens. Both are
opt-in and independent; use `--delete-source` to keep local disk usage down
between transfers, and `--delete-after-transfer` to reclaim the rest once
everything is safely off-machine.

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
MB/s per file for transfer, then a verify pass). `--dry-run` logs the intended
grid ffmpeg call and transfer plan without writing anything — handy for
validating paths on a new workstation.

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
