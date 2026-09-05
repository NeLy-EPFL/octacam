# Legacy manual 2P mirror: found, verified redundant, removed

**For**: whoever maintains the rendering/presentation scripts on the analysis
workstation (`presentation_<date>/` outputs, e.g. `Fly2_001_synced_full.mp4`).
**Repo/branch**: `octacam`, `feat/twophoton-transfer`.

## What was found

A one-time manual mirror of the whole ThorSync/ThorImage share existed at
`/mnt/upramdya_data/MD/BallPushing_Imaging/2P/MD/<experiment>/<date>/...`,
made **2026-08-14** — before the automated `octacam process` 2P-transfer
pipeline existed. It only ever covered two experiments: `MB247_CI63`
(→ `260813_`) and `Tests`, because it was a one-time copy, never repeated.

This is almost certainly what the rendering/presentation scripts are still
pathed against — it explains why they've worked for `MB247_CI63` (the mirror
still had it) but come up empty for anything transferred only through the
automated pipeline (`Ple_CI63`, `PAM7xCI63`, `PAM07xCI80`), since those never
existed at the old path at all.

## Verification before deleting

Byte-for-byte / folder-set comparison against the automated pipeline's
current output, done before touching anything:

| Legacy path | New canonical path(s) | Result |
| --- | --- | --- |
| `2P/MD/MB247_CI63/260813/` (45 GB, 10 folders) | `260813_/Fly2/{001..005}/2P/` | Identical folder set, identical total bytes (47,604,499,655) |
| `2P/MD/Tests/` (16 GB, 49 folders) | `2p_only/Tests/<date>/` | Every legacy folder present and byte-identical; new layout has 6 more (recorded after the Aug-14 snapshot) |

Zero data-loss risk confirmed. The legacy mirror was a strict, stale subset —
pure duplicate storage (~61 GB). **It has been deleted.**

## What needs to change on the analysis machine

Update whatever generates `presentation_<date>/*.mp4` to read 2P data from
the current convention instead of `2P/MD/<experiment>/<date>/`:

- **Matched to a behavior take**: `<date>_/<Fly>/<take>/2P/<folder-name>/`
  (e.g. `260813_/Fly2/001/2P/SyncData102/`, `.../2P/Fly1/`)
- **Standalone** (no matching behavior take — tuning/focus checks, or an
  experiment the automated matcher couldn't pair): `2p_only/<experiment>/<date>/<folder-name>/`
  where `<experiment>` is the microscope's own folder name (**not** octacam's
  local naming — e.g. octacam calls a session `260903_PAM07xCI80` but the
  microscope folder, and therefore this path, is `PAM7xCI80`, no leading "0").

Two things worth carrying into that script if it doesn't already handle them:
1. A `twophoton_match.json` sidecar sits next to each matched take's
   `recording_summary.json` — it records exactly which 2P folder(s) were
   paired and at what confidence (`verified` = ThorSync edge-count matched,
   `timestamp` = heuristic fallback), so the script can look this up instead
   of re-deriving the pairing itself.
2. `SyncData` folder numbers are **not globally unique** across experiment
   folders — e.g. `MB247_CI63/SyncData102` and `Tests/SyncData102` are two
   different recordings that happen to share a number. Always key off the
   full destination path, never the bare folder name.

## Source-data completeness check (context for a possible follow-up cleanup)

While investigating this, also confirmed the *entire* `windows_share/MD`
2P source tree is now fully accounted for on the NAS — every folder is either
a matched pair's `2P/` destination or a `2p_only/` sweep destination, with no
orphaned/untransferred folders:

| Experiment | Folders on share | Accounted for on NAS |
| --- | --- | --- |
| MB247_CI63 | 10 | 10 (5 matched pairs × 2) |
| PAM7xCI63 | 10 | 4 matched + 6 standalone |
| PAM7xCI80 | 14 | 7 matched + 7 standalone |
| Ple_CI63 | 8 | 0 matched + 8 standalone |
| Tests | 55 | 0 matched + 55 standalone |

This is relevant context if there's a follow-up decision to clean up the
source share and/or the local behavior workstation's raw recording folders —
that investigation is happening separately and isn't blocked on this report.
