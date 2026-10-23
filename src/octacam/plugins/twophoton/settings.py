"""The 2P processing settings: what `octacam process` pairs, copies and renders
for a rig whose recordings enable the twophoton plugin.

They are the plugin's own options (`[plugins.options]` of its `[[plugins]]`
entry), beside the trigger's::

    [[plugins]]
    name = "twophoton"

    [plugins.options]
    device = "/dev/arduinoCams"
    source = "/mnt/windows_share/MD"   # ThorImage/ThorSync experiments, %-codes ok
    settle_s = 300

A recording made before the move kept them in its config snapshot's
`[transfer.twophoton]` table, which is still read when its plugin options
carry no `source`.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from octacam.config import OctacamConfig, resolve_dir_template

log = logging.getLogger("octacam")

PLUGIN_NAME = "twophoton"


class TwoPhotonSettings(BaseModel):
    """`source`: the share's root, whose experiment folders hold the ThorSync
    (`SyncData*`) and ThorImage folders; empty disables the 2P processing.

    Pairing: a take's 2P folders are found by the ThorSync signals (`DI/Cameras`
    edges = the take's frames) when `verify_with_signals`, among folders within
    `verify_window_s` of it; else by start time within `match_window_s` (the
    take starts on ThorImage's trigger). A folder is copied once quiet for
    `settle_s`.

    Copying: `assemble_tiff_stacks` turns ThorImage's per-frame TIFFs into one
    stack per channel (and FastZ plane); `attribute_unclaimed_to_fly` files 2P
    data no take claims under its fly (by ThorImage's name), else under
    `2p_only/<experiment>/<date>/`; `reconcile_recordings` renumbers the flies a
    run touched into one `RecordingN_<Beh|2P|Synced>` sequence;
    `write_manifest` rewrites each day's `2p_reconciliation.md`.

    Renders (last): `render_synced_video` (behavior + 2P side by side, with the
    two channels' colors and FastZ projection below) and `render_zstack_mip`
    (each anatomical Z-stack's max projections).

    `delete_share_data`: with `[transfer].delete_after_transfer`, also delete
    each share folder once its copy is verified at the destination (paired or
    2P-only). Off unless set, and meant for a user's own `[users.<X>.twophoton]`
    table: each person decides for their own 2P data, and nothing outside the
    `source` folder is ever deleted.

    `airflow_logs`: airflow_control's log folder; a take paired without ThorSync
    gets the air commands logged during it, in place of ThorSync's AirFlow
    channel (`airflow`).
    """

    model_config = ConfigDict(extra="ignore")

    source: str = ""
    match_window_s: float = 5.0
    verify_window_s: float = 3600.0
    settle_s: float = 300.0
    verify_with_signals: bool = True
    assemble_tiff_stacks: bool = True
    attribute_unclaimed_to_fly: bool = True
    reconcile_recordings: bool = True
    write_manifest: bool = True
    render_synced_video: bool = True
    synced_green_channel: str = "ChanA"
    synced_red_channel: str = "ChanB"
    synced_fastz_projection: Literal["mean", "max"] = "mean"
    render_zstack_mip: bool = True
    airflow_logs: str = ""
    delete_share_data: bool = False

    @property
    def source_root(self) -> Path | None:
        return Path(resolve_dir_template(self.source)) if self.source else None

    @property
    def airflow_log_dir(self) -> Path | None:
        if not self.airflow_logs:
            return None
        return Path(resolve_dir_template(self.airflow_logs)).expanduser()


def _plugin_options(config: OctacamConfig) -> dict | None:
    for entry in config.plugins:
        if entry.name == PLUGIN_NAME:
            return dict(entry.options)
    return None


def _legacy_table(snapshot: Path | None) -> dict:
    """`[transfer.twophoton]` of an older recording's config snapshot."""
    if snapshot is None or not snapshot.is_file():
        return {}
    try:
        data = tomllib.loads(snapshot.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("twophoton: could not read %s: %s", snapshot, e)
        return {}
    table = (data.get("transfer") or {}).get("twophoton")
    return table if isinstance(table, dict) else {}


def settings_for(
    config: OctacamConfig, snapshot: Path | None = None
) -> TwoPhotonSettings | None:
    """The 2P processing settings of a recording (or rig) *config*, None when
    it has no 2P source: its plugin options, else *snapshot*'s legacy
    `[transfer.twophoton]`. An invalid value is warned about and defaulted.
    """
    options = _plugin_options(config) or {}
    if not options.get("source"):
        legacy = _legacy_table(snapshot)
        if legacy.get("source"):
            options = {**legacy, **{k: v for k, v in options.items() if k != "source"}}
    if not options.get("source"):
        return None
    try:
        return TwoPhotonSettings.model_validate(options)
    except ValidationError as e:
        bad = {str(err["loc"][0]) for err in e.errors() if err["loc"]}
        log.warning(
            "twophoton: invalid option(s) %s; using their defaults",
            ", ".join(sorted(bad)),
        )
        return TwoPhotonSettings.model_validate(
            {k: v for k, v in options.items() if k not in bad}
        )
