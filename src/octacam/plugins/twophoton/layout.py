"""Where the 2-photon module keeps its files, beside octacam's own recording
layout (`recording_format`): the take's 2P sidecars live in its
`octacam_recording/` info folder, the paired ThorImage/ThorSync folders under
`<take>/2P/<name>`, and its renders next to the take's grid.
"""

from pathlib import Path

# The take's 2P facts, written by the plugin when the take ends: whether it was
# armed, synced with ThorImage, and the board's end-of-run report.
TAKE_INFO_FILENAME = "twophoton.json"
# What `process` paired the take with (and how), once its 2P data is copied.
MATCH_FILENAME = "twophoton_match.json"
# Written instead of MATCH_FILENAME while the take's 2P data isn't settled yet.
PENDING_FILENAME = "twophoton_pending.json"
# Which share experiment a 2P-only folder came from (attribution across runs).
TWOPHOTON_2P_ONLY_SOURCE_FILENAME = ".octacam_2p_source.json"
# One per fly: every RecordingN rename, append-only (a revert's record).
RECONCILIATION_LOG_FILENAME = "reconciliation_log.md"
# One per day: every take against its 2P data, rebuilt by each run.
MANIFEST_FILENAME = "2p_reconciliation.md"
# The paired 2P folders inside a take.
TWOPHOTON_DIRNAME = "2P"
# A fly's 2P-only acquisitions before the RecordingN renumber.
TWOPHOTON_ONLY_DIRNAME = "2P_only"
# The behavior + 2P quick-look video, beside the take's grid.
SYNCED_VIDEO_FILENAME = "synced_2p.mp4"
# Older takes (the rig's first NAS layout) kept videos and renders in these
# subfolders; develop's layout keeps both flat at the take's root.
LEGACY_BEHAVIOR_DIRNAME = "Behavior"
LEGACY_RENDERINGS_DIRNAME = "Renderings"
GRID_FILENAME = "grid.mp4"


def info_file(take: Path, name: str) -> Path:
    """Where *take* keeps the 2P sidecar *name*: its `octacam_recording/` info
    folder, or the take itself for an older flat take (either layout reads).
    """
    from octacam.recording_format import recording_info_dir

    nested = recording_info_dir(take) / name
    flat = take / name
    return flat if not nested.exists() and flat.exists() else nested


def renders_dir(take: Path) -> Path:
    """Where *take*'s renders go: beside its grid (the take's root), or its
    `Renderings/` subfolder for an older take that already has one.
    """
    legacy = take / LEGACY_RENDERINGS_DIRNAME
    return legacy if legacy.is_dir() else take


def behavior_video(take: Path) -> Path | None:
    """The video a synced render shows for *take*: its grid, else its first
    camera video (either layout), else None.
    """
    for folder in (take, take / LEGACY_RENDERINGS_DIRNAME):
        if (folder / GRID_FILENAME).is_file():
            return folder / GRID_FILENAME
    for folder in (take, take / LEGACY_BEHAVIOR_DIRNAME):
        videos = sorted(
            p for p in folder.glob("*.mp4") if p.name != SYNCED_VIDEO_FILENAME
        )
        if videos:
            return videos[0]
    return None
