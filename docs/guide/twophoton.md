# 2-photon rigs

The `twophoton` plugin runs octacam beside a ThorImage/ThorSync 2-photon
microscope: the behavior cameras start and stop with the 2P acquisition, and
`octacam process` pairs each take with its ThorImage and ThorSync data, copies
both to the destination and organizes them per fly. This page is the
day-to-day guide; the plugin's trigger is described under
[Plugins](plugins.md#twophoton-2-photon-hardware-trigger).

## Setup

One `[[plugins]]` entry holds both the trigger's and the processing's options:

```toml
[[plugins]]
name = "twophoton"

[plugins.options]
device = "/dev/arduinoCams"         # the trigger Arduino
source = "/mnt/windows_share/MD"    # where ThorImage/ThorSync save (%-codes ok)
settle_s = 300                      # a 2P folder quiet this long is finished

[transfer]
directory = "/mnt/store/MD/BallPushing_Imaging"
checksum = true
```

| Option | Default | Meaning |
| --- | --- | --- |
| `source` | `""` | The share's root: its experiment folders hold the `SyncData*` and ThorImage folders. Empty: no 2P processing. |
| `match_thorimage` | `true` | A take ends when ThorImage stops (the Record tab's **Synced with ThorImage**). |
| `airflow_logs` | `""` | airflow_control's log folder (e.g. `~/airflow_control_logs`): a take without ThorSync gets the air commands logged during it. |
| `stop_idle_ms` | `2000` | The ThorSync input quiet this long means ThorImage stopped (at least 200). |
| `max_duration_s` | `3600` | A synced take's safety limit. |
| `match_window_s` | `5` | Pairing by start time: a take and a 2P folder starting this close. |
| `verify_window_s` | `3600` | ThorSync folders this close to a take are checked by their signals. |
| `settle_s` | `300` | A 2P folder is copied once nothing in it changed for this long. |
| `verify_with_signals` | `true` | Pair by ThorSync's recorded camera pulses (needs `h5py`). |
| `assemble_tiff_stacks` | `true` | ThorImage's per-frame TIFFs become one stack per channel (needs `tifffile`). |
| `attribute_unclaimed_to_fly` | `true` | 2P data no take claims goes under its fly, by ThorImage's names. |
| `reconcile_recordings` | `true` | Renumber the touched flies' sessions `RecordingN_<Beh/2P/Synced>`. |
| `write_manifest` | `true` | Rewrite each touched day's `2p_reconciliation.md`. |
| `render_synced_video` | `true` | Render each paired take's behavior + 2P video. |
| `synced_green_channel`, `synced_red_channel` | `ChanA`, `ChanB` | The ThorImage channels shown green and red. |
| `synced_fastz_projection` | `mean` | How a FastZ timepoint's planes are combined (`mean` or `max`). |
| `render_zstack_mip` | `true` | Render each anatomical Z-stack's max projections. |

Each person keeps their own share folder as a user (see
[Users](configuration.md#users)): `[users.MD.twophoton] source = ...`, set from
the Record tab's **+ Add yourself…**, which suggests it from the other users'.
Picking the user points the ThorSync check and `octacam process` at it (the
recording's snapshot keeps it).

Install the extra for the signal pairing and the stacks:
`pip install 'octacam[twophoton]'`. A recording made with the older
`[transfer.twophoton]` table is still processed with it; `octacam doctor`
says when a rig config still has one.

!!! warning "ThorSync's acquisition duration must outlast your longest take"
    ThorSync stops recording after its own *duration* setting, whatever the
    capture is doing. Shorter than a take, it silently loses the rest of it:
    the timing signals, AirFlow and every other ThorSync channel (seen on real
    data: a 320 s limit against 394 s takes). octacam checks it: the Record tab
    warns when the newest `SyncData` folder's setting is too short for the take
    (compared with ThorImage's planned acquisition), `octacam doctor` reports
    it, and `octacam process` flags any ThorSync recording that was cut off (in
    its log, the take's `twophoton_match.json`, and a **Warnings** section in
    the day's `2p_reconciliation.md`).

!!! warning "Tick ThorSync in ThorImage"
    ThorImage records ThorSync only with the **ThorSync** box in its Capture
    panel ticked. Unticked, the 2P images are saved but nothing else: no AirFlow,
    no ThorSync channel, and `octacam process` can pair the take by time only
    (no check of the camera pulses). octacam reminds you: the GUI's first armed
    take after launch asks whether the box is ticked before it starts. About 10 s
    after each armed take it looks for that take's `SyncData` folder on the
    share; if there is none, a popup says so (and the next take asks again).
    The Record tab's **Ready?** strip and `octacam doctor` keep flagging it
    until ThorSync records a capture again, and `octacam process` warns about
    each take paired without ThorSync.

## Recording a paired take

1. Start the octacam recording first. With **Arm with recording** checked in
   the 2-Photon tab and **Synced with ThorImage** in the Record tab (both
   defaults), there is no duration to set: octacam arms the Arduino and waits.
2. Start ThorSync and ThorImage as usual. The Arduino starts the cameras on
   ThorSync's start edge, so the capture begins with the 2P acquisition.
3. When ThorImage finishes, the cameras stop and octacam ends the take by
   itself (~2 s later). Meanwhile the Record tab shows the elapsed time. To
   record for a fixed time instead, untick **Synced with ThorImage**; the
   Duration field comes back. Headless, `octacam record` follows
   `match_thorimage` unless `--duration` is given.

Pairing, copying and organizing happen later, in `octacam process`.

2P-only data (a Z-stack, a calibration scan) needs no octacam recording: it
is found and filed by the next `octacam process` run, or by
`octacam twophoton sweep --config <rig>`.

## Naming ThorImage acquisitions

Pairing a take with its 2P data never reads a name: only times and ThorSync's
signals. Names help organize what has no take:

- start the sample name with `Fly<N>` (it need not match octacam's fly
  number): a 2P-only acquisition is then filed under its fly;
- add what it is: `Fly1_Zstack`, `Fly1_Streaming`. The word ends up in the
  session's name on the destination (`Recording2_2P_Zstack`).

Without either nothing breaks: the folder gets a plainer name, or lands in
the generic bucket (below).

## What `octacam process` does

For the recordings of a run that enable the plugin, after their own videos
and metadata are transferred:

1. **Pair** each armed take with its ThorSync and ThorImage folders: by
   ThorSync's recorded camera pulses (the take's frame count) when there is a
   `SyncData` folder, else by start time; copy them into the take as
   `2P/<name>`, ThorImage's frames as one stack per channel (every page
   compared with its frame when copies are verified; a capture stopped early
   is stacked with the frames it recorded, and FastZ as one stack per
   plane). A take whose 2P data is
   still being written waits (`twophoton_pending.json`) and is paired by a
   later run.

   A take ThorSync did not record is aligned on the trigger board's clock
   instead (`octacam_recording/twophoton_board_sync.json`): the board starts
   the cameras on the first 2P frame and its end-of-run report counts the 2P
   frames and times them. Every 2P frame lands within one camera frame of
   where ThorSync would put it (checked against ThorSync on real takes); a
   capture stopped early in ThorImage borrows the 2P frame period of the
   day's completed captures. The synced video and scapepp use it in place of
   ThorSync. The other ThorSync channels are lost; AirFlow is stood in for
   when `airflow_logs` names airflow_control's log folder: the air commands it
   logged during the take go beside it (`octacam_recording/airflow_commands.jsonl`;
   they give the AirFlow sensor's on/off on 99.9 % of camera frames, checked
   against ThorSync).
2. **Sweep** the share for settled 2P data no take claims, filed under its
   fly when ThorImage's name tells which, else in the generic bucket.
3. **Rewrite** the touched days' `2p_reconciliation.md`.
4. **Renumber** the touched flies' sessions into one sequence. From here the
   data is at its final place.
5. After the grids: render the synced behavior + 2P videos and the Z-stack
   projections.

The share is scanned and the destination walked once per run. Each ThorSync
episode is read once (its capture windows are kept in octacam's cache), and a
2P folder whose copy was verified is not compared again by a later run unless
its files changed (`.octacam_verified.json` in the copy; `octacam twophoton
verify` always compares afresh). With
`[transfer].delete_after_transfer`, a share folder is deleted once its copy is
verified: a paired take's, and 2P-only data the sweep files (a Z-stack, a
test). The share's account needs delete rights on the 2P PC for that (on a
`noperm` CIFS mount the Windows server alone decides); without them octacam
says so once and keeps going. `octacam twophoton verify` tells which folders
are safe to delete, and `octacam twophoton verify --delete` deletes exactly
those, for a backlog.

## Where things land

```
<transfer.directory>/261014_PAM7xCI80/
  2p_reconciliation.md            # the day: every take and its 2P data
  Fly1/
    Recording1_2P_Zstack/2P/...   # 2P-only: a Z-stack with its ThorSync
    Recording2_Synced_Streaming/
      camera_*.mp4, grid.mp4, synced_2p.mp4
      2P/Fly1_Streaming/ChanA.tif ...
      2P/SyncData166/...
      octacam_recording/          # summary, timestamps, twophoton_match.json
    reconciliation_log.md         # every rename, append-only
```

- `RecordingN_Beh`: behavior only. `RecordingN_2P_<detail>`: 2P only.
  `RecordingN_Synced_<detail>`: both.
- 2P data octacam cannot attribute to one fly goes to
  `<transfer.directory>/2p_only/<experiment>/<date>/<name>/`: safe, just not
  filed. Rename it by hand, or let
  `octacam twophoton reconcile --all --dry-run --config <rig>` show how it
  would group it into new flies (look at the dry run first: no take anchors
  that decision).

Recordings transferred by the rig's first layout (`Behavior/`, `Renderings/`)
are read the same way.

## Commands

| Command | What it does |
| --- | --- |
| `octacam twophoton verify [SHARE_FOLDERS] -c <rig>` | Which share folders are verifiably at the destination (read-only; a stacked channel is compared page by page). `--delete` deletes the ones verified. |
| `octacam twophoton sweep -c <rig>` | File the share's unclaimed 2P data, pair waiting takes. |
| `octacam twophoton manifest -c <rig>` | Rewrite every day's `2p_reconciliation.md`. |
| `octacam twophoton reconcile DAYS_OR_FLIES -c <rig>` | Renumber flies (`--all`: every fly, and group the generic bucket into new flies). |
| `octacam twophoton reassemble -c <rig> [FOLDERS]` | Stack ThorImage folders still holding per-frame TIFFs, then delete the frames. |
| `octacam twophoton render PATHS` | Render (or re-render with `--force`) synced videos and Z-stack MIPs. |
| `octacam twophoton board-sync PATHS` | Align takes transferred without ThorSync on the trigger board's clock (give a whole day: its completed captures time the stopped-early ones). A take from before the board's report (2026-10-14) needs `--reference` takes with ThorSync or a report (any day) to measure the rig's scan timing; `--airflow-logs` also attaches airflow_control's air commands. |

Each takes `--dry-run` (but `verify`, which changes nothing anyway).

## Not supported yet

- **A Z-stack repeated over time** (a step-and-shoot Z-stack with ThorImage's
  Timelapse above 1, streaming off): no such capture has been recorded on the
  rig. octacam stacks its frames one file per plane and assumes ThorSync
  records one capture window per slice of every volume, but neither has been
  checked against real data. Study a first real capture before relying on it.
