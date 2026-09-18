# 2-photon + behavior workflow

A practical, day-to-day guide to running a 2-photon (ThorSync/ThorImage) rig
alongside octacam. For the underlying mechanics (config options, matching
tiers, the CLI reference) see [Processing recordings → 2-photon
transfer](processing.md#2-photon-transfer) — this page is the "what do I
actually do" companion to that reference.

## The three things that have to be true

1. The rig's `octacam_config.toml` has `[transfer.twophoton]` configured
   (`source` pointing at the network share ThorImage/ThorSync write to).
2. The `twophoton` plugin is enabled (see [Plugins](plugins.md#twophoton-2-photon-hardware-trigger)).
3. "Arm with recording" is checked in the GUI's **2-Photon** tab (or
   `default_start_params` arms it automatically for a headless `octacam
   record`) — this is what tells octacam's own recording that it should be
   looked for a 2P pair at all.

If all three are true, everything past this point is automatic. You never
manually copy a ThorSync/ThorImage folder anywhere.

## Recording a paired take

1. Start ThorSync/ThorImage as usual — nothing about how you drive them
   changes.
2. Start the octacam recording. The 2-photon Arduino trigger waits for
   ThorSync's own start edge, so octacam's capture begins right when the 2P
   acquisition does.
3. Stop both when you're done. (There's currently no automatic "stop
   octacam when ThorImage finishes" signal — see [Known limitations](#known-limitations-worth-knowing-about)
   below.)

That's it — pairing, transfer, and organizing all happen later, when you run
`octacam process`.

## Recording 2P-only data (no behavior camera at all)

Sometimes you only need ThorImage/ThorSync — a Z-stack, a calibration scan,
a fly you're not running a behavior take for. You don't need to open octacam
at all for this. Just record with ThorImage/ThorSync as normal, and later
run:

```bash
octacam process --twophoton-sweep --config <rig-config-dir>
```

This needs nothing but the rig's config file — no camera, no local
recording. It finds every settled ThorSync/ThorImage folder on the share
that no behavior take has already claimed, and files it away (see
[Where things land](#where-things-land-on-the-nas) below). A normal
`octacam process` run for *other* takes also does this sweep automatically,
so in practice you often don't need to run it by hand at all — it only
matters when a day/session had *no* behavior recordings whatsoever.

## Naming your ThorImage/ThorSync sessions

You don't have to follow a strict convention — the actual pairing between a
behavior take and its 2P data is decided **entirely by timing and DAQ
signals, never by name**, so a mis-typed or missing sample name can never
break a pairing or corrupt data. That said, following a simple habit gets
you nicer, more useful results:

- **Start your sample name with `Fly<N>`**, matching the fly you're
  currently working on (the number doesn't need to match octacam's own Fly
  numbering — it never does in practice, and that's fine). This is what
  lets octacam file a 2P-only acquisition (a Z-stack done before any
  behavior take) under the right fly's own folder instead of a
  disconnected generic bucket.
- **Add a short word after that for what the acquisition actually is** —
  `Fly1_Zstack`, `Fly1_Streaming`, `Fly1_FastZ_Test`. This word ends up baked
  into the folder name octacam creates on the NAS (`Recording2_2P_Zstack`),
  so you can tell what's inside without opening it.
- If you skip either of these, nothing breaks — you just get a plainer
  folder name, or your 2P-only data lands in a generic, unorganized bucket
  instead of under the fly it belongs to (never silently misfiled — see
  [Where things land](#where-things-land-on-the-nas)).

## Running `octacam process`

Run it as normal — for a synced take, transfer, matching, and 2P sweep are
all part of the one command:

```bash
octacam process --config <rig-config-dir> --last
```

What happens, in order, for everything that shares one
`[transfer.twophoton].source`:

1. **Transfer & transcode** the behavior take(s) as usual.
2. **Match** each armed take against ThorSync/ThorImage folders on the
   share — verified via the DAQ signal when a `SyncData*` folder exists,
   timestamp-only otherwise (see [Verified matching](processing.md#verified-matching)).
3. **Sweep** for any settled 2P folder no take claimed — filed under its
   fly if the name/timing lets octacam recognize it, generic bucket
   otherwise.
4. **Reconcile** every fly this run touched into one chronological
   `RecordingN` sequence (on by default — `--no-auto-reconcile-recordings`
   opts a single run out).
5. **Rebuild the day's reconciliation manifest** (`2p_reconciliation.md`) —
   a one-page summary of what matched, what didn't, and what's still
   unclaimed that day.

## Where things land on the NAS

```
<transfer.directory>/260916_AllPAM_G151xCI80/
  2p_reconciliation.md          # this day's whole 2P picture, at a glance
  Fly1/
    Recording1_2P_Zstack/2P/... # 2P-only — a Z-stack, before any behavior take
    Recording2_Synced_Streaming/
      Behavior/...              # per-camera archival .mp4
      Renderings/grid.mp4
      2P/...                    # the paired ThorImage/ThorSync data
      recording_summary.json
      twophoton_match.json      # what matched, how confidently, and how close in time
    reconciliation_log.md       # append-only history of every rename here
```

- **`RecordingN_Beh`** — behavior only, no 2P pair found (or none expected).
- **`RecordingN_2P_<detail>`** — 2P-only, no behavior take at all.
- **`RecordingN_Synced_<detail>`** — both, paired together.
- The `_<detail>` suffix is only there when your ThorImage sample name had
  one (see [Naming](#naming-your-thorimagethorsync-sessions) above) — a
  bare `Fly1` name just gives a plain `RecordingN_2P`.

**If a 2P folder couldn't be attributed to any fly** (no matching name
prefix, or it matched more than one fly — never guessed), it lands in a
day-independent, generic bucket instead:

```
<transfer.directory>/2p_only/<experiment>/<date>/<name>/
```

This isn't a failure — it's octacam being honest that it isn't sure whose
data this is. It's still fully transferred and safe; it's just not filed
under a specific fly yet. Two ways to resolve it:

- Rename it by hand on the NAS, or
- Run `octacam process --reconcile-recordings --all --config <rig-config-dir>`,
  which also promotes generic-bucket data (grouped by ThorImage name) into
  brand-new Fly folders when it's confident enough to. **Always look at the
  `--dry-run` output first** — this is the one part of this whole pipeline
  that doesn't have an existing behavior take anchoring the decision, so
  it's worth a human glance before running for real.

## Checking your work

- **`2p_reconciliation.md`** (one per day/session folder) — the fastest way
  to see, for that day, every take against its matched 2P folder (or
  "unmatched"), plus every 2P folder on the share that day nothing claimed.
- **`octacam process --twophoton-verify --config <rig-config-dir>`** —
  read-only report of which 2P *source* folders (still sitting on the
  Windows share) are fully, checksum-verified present on the NAS, and
  therefore safe to delete by hand. Never deletes or modifies anything
  itself.
- **`reconciliation_log.md`** (one per fly, append-only) — if a
  `--reconcile-recordings` rename ever needs reverting by hand, this is
  the original name/location, preserved even after later runs renumber
  things again.
- **`--dry-run`** works on every one of these; nothing here is ever
  destructive without you having previewed it first.

## Known limitations worth knowing about

- **End time isn't synced.** octacam and the 2P acquisition each stop
  independently — a genuine pair's start lines up closely, but its
  reported end/duration can differ by tens of seconds. This is normal, not
  a sign of a bad pairing.
- **A purely timestamp-matched pairing (no `SyncData` folder to verify
  against) is the least certain kind.** It's still gated tightly (start
  time within a few seconds), but if you have the choice, running ThorSync
  alongside ThorImage gives you a verified, not just guessed, pairing.
- **A 2P folder someone already deleted from the share can't be
  reconstructed** — the reconciliation manifest can only flag drift that's
  still visible on disk.
- **No background daemon.** Nothing here runs on a timer — every step is
  triggered by an actual `octacam process` invocation (by hand, or via the
  GUI's "shut down & process" option), never silently in the background.
