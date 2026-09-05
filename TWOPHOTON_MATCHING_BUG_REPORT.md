# Bug report: behavior/2P takes finalized before their pair settles never get matched

**Repo/branch**: `octacam`, `origin/feat/twophoton-transfer` (commit `6514dd1e88eb79431f4938e9c6bf4c4a466f52cb`,
"feat(twophoton): auto-pair and transfer ThorSync/ThorImage data with behavior recordings").
Relevant files:
- `src/octacam/twophoton_transfer.py` — `discover_twophoton_folders`, `match_takes_to_twophoton_batch`,
  `TwoPhotonFolder`, `TakeInfo`, `_thorimage_start_time`, `_folder_last_mtime`, `is_settled`.
- `src/octacam/twophoton_signals.py` — edge-count verification against ThorSync.
- `cli.py` — `process` command orchestration: per-take matching, then "Phase 3c: automatic 2P-only sweep"
  (`_sweep_unclaimed_twophoton`, `_run_twophoton_sweep`). Config defaults: `match_window_s=60.0`,
  `verify_window_s=3600.0`.

## How pairing is supposed to work

Operator workflow (confirmed): duration is estimated on the 2P side, octacam is armed with a matching
duration, then the 2P scope triggers octacam's recording start (`trigger_source: "external"`). Because
the duration is only estimated, a **true pair should start within ~1–10s of each other** — a gap of ~30s+
means it's a different recording, not a mismatched pair.

`match_takes_to_twophoton_batch` has two tiers:
1. **Verified** (`confidence="verified"`): edge-count match against ThorSync's DAQ trace, within
   `verify_window_s` (3600s).
2. **Timestamp-only** (`confidence="timestamp"`): fallback when no ThorSync exists or verification fails,
   using `match_window_s` (60s) between the take's `[start, start+duration]` and the ThorImage folder's
   `[start_time, last_mtime]` (see `_overlaps`/`_gap_seconds`).

## Confirmed real bug (not a window-size issue)

Session `260903_PAM7xCI63/Fly1` (no `SyncData*`/ThorSync folder exists anywhere for this session — tier 1
was never available, everything fell to tier-2 timestamp matching):

- Takes **004, 005, 006, 007** were correctly paired. Their `twophoton_match.json` files are all
  timestamped **2026-09-04, 20:40:04–20:50:13** — one batch, one `process` run. Real gaps between
  behavior start (`recording_summary.json` `start_time_ns`) and 2P start (`Experiment.xml`
  `<Date uTime=...>`) were **1.8s, 9.6s, 2.2s, 2.5s** — matches the expected ~1–10s signature.
- Take **002** (`start=1788423912.28`, `duration=129s`, so window is `[1788423912.28, 1788424041.28]`)
  **truly overlaps** ThorImage folder `Fly1_005` (`uTime=1788423928`, i.e. 15.7s after take 002's start —
  well inside the window, a real overlap, not a near-miss on the 60s cutoff). It was **never paired**.
- `Fly1_005` was instead later found in the unclaimed sweep destination
  (`2p_only/PAM7xCI63/260903/Fly1_005`) with a **birth time of 2026-09-04 21:16:44** — about 26 minutes
  *after* the matching pass that successfully paired 004–007 had already finished (by 20:50:13).
- Take **001** (duration 25s, an outlier — every other take here is 129s) has no plausible candidate
  within the expected gap at all; treat as genuinely standalone, not a pipeline miss.

**Root cause (hypothesis, not yet traced to the exact line)**: `process` only feeds newly-discovered,
not-yet-transferred behavior takes into `match_takes_to_twophoton_batch` on a given run. Once a take is
transferred/finalized, later runs don't reconsider it. The 2P-only sweep only handles folders unclaimed
*at that same run* — it has no mechanism to go back and check a newly-settled 2P folder against a take
that was already finalized in an earlier run. So: if a behavior take gets finalized before its true 2P
counterpart has appeared/settled on the network share, the pair is permanently missed, regardless of
`match_window_s`/`verify_window_s` values.

This is **not** limited to legacy/pre-setup data — the successful and failed pairs above are from the same
run, same day, using the current pipeline. It can recur any time the 2P side's folder becomes
visible/settled on the share later than the behavior side's finalization within a `process` run boundary.

## Suggested fix directions (for design, not prescriptive)

1. Don't finalize/transfer a behavior take as "done" if its 2P side isn't settled yet — hold it
   provisionally for a grace period so a later-arriving 2P folder still has something to pair against.
2. Alternatively, keep a durable record (time window + destination path) of already-transferred-but-
   unmatched takes that the 2P-only sweep checks against *before* dumping a candidate into `2p_only` —
   i.e. let the sweep retroactively pair into an already-finalized take's folder, not just forward-match
   within a single run.
3. Confirm `Fly1_Zstack_*`-named folders (anatomical Z-stacks, not per-trial functional scans) are
   excluded from take-pairing entirely, or handled as intentionally-standalone, to avoid noise/false
   candidates in this logic (e.g. `Fly1_Zstack_000/001` in this same session were correctly never forced
   into a pair, but worth confirming that's deliberate rather than incidental).
4. Keep the ~1–10s expected-gap / ~30s+-means-different-recording heuristic in mind when tuning or
   testing — it's now empirically confirmed on real data, not just a guess.

## Reproduction data (for reference, if the paths are reachable from this workstation)

NAS paths as mounted on the analysis machine (`//sv-nas1.rcp.epfl.ch/upramdya/data`, `/mnt/upramdya_data`
here — likely a different mount point on the experimental workstation):
- `MD/BallPushing_Imaging/260903_PAM7xCI63/Fly1/{001..008}/recording_summary.json` (behavior side)
- `MD/BallPushing_Imaging/260903_PAM7xCI63/Fly1/{004,005,006,007}/twophoton_match.json` (successful pairs)
- `MD/BallPushing_Imaging/2p_only/PAM7xCI63/260903/Fly1_005/Experiment.xml` (the missed pair)
