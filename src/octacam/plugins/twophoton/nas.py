"""One walk of the destination tree, for everything the 2P steps ask of it: the
transferred takes (with their summaries and pairing records) and every 2P
folder already there.

Built once per run and destination: the rig's first version walked the whole
tree (years of recordings, every TIFF) once per question. The walk never
enters a 2P folder, a take's metadata or a take's subfolders other than `2P/`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from octacam.plugins.twophoton.layout import (
    LEGACY_BEHAVIOR_DIRNAME,
    LEGACY_RENDERINGS_DIRNAME,
    TWOPHOTON_2P_ONLY_SOURCE_FILENAME,
    TWOPHOTON_DIRNAME,
    TWOPHOTON_ONLY_DIRNAME,
)
from octacam.plugins.twophoton.takes import (
    match_record,
    read_json,
    summary_of,
    take_info,
)
from octacam.recording_format import RECORDING_INFO_DIRNAME, is_recording_dir

# The generic bucket for 2P data no fly claims: 2p_only/<experiment>/<date>/<name>.
GENERIC_DIRNAME = "2p_only"
# A fly's renumbered 2P-only session: RecordingN_2P[_<detail>]/2P/<name>.
RECONCILED_2P_DIR_RE = re.compile(r"^Recording\d+_2P(_.+)?$")
# Files only a ThorImage/ThorSync folder holds: never walked into.
_ACQUISITION_MARKERS = ("Experiment.xml", "ThorRealTimeDataSettings.xml")


@dataclass
class NasTake:
    """A transferred take: its folder, summary, pairing record and plugin facts."""

    path: Path
    summary: dict
    match: dict
    info: dict

    def fly_dir(self, root: Path) -> Path | None:
        """`<root>/<day>/<fly>` the take sits in; None above that depth."""
        parts = self.path.relative_to(root).parts
        return root / parts[0] / parts[1] if len(parts) >= 3 else None


@dataclass
class NasIndex:
    root: Path
    takes: list[NasTake] = field(default_factory=list)
    # Every 2P folder on the destination: in a take's 2P/, a fly's 2P_only/, a
    # RecordingN_2P*/2P/, or the generic bucket.
    twophoton_dirs: list[Path] = field(default_factory=list)
    # A take-less fly's 2P-only folder -> the share experiment it came from.
    source_markers: dict[Path, str] = field(default_factory=dict)

    @classmethod
    def build(cls, root: Path) -> NasIndex:
        index = cls(root)
        if root.is_dir():
            index._walk()
        return index

    def _walk(self) -> None:
        for directory, subdirs, files in os.walk(self.root):
            here = Path(directory)
            rel = here.relative_to(self.root).parts
            if is_recording_dir(here):
                self.takes.append(
                    NasTake(here, summary_of(here), match_record(here), take_info(here))
                )
                self._leaves(here / TWOPHOTON_DIRNAME)
                subdirs[:] = []
            elif here.name in (TWOPHOTON_DIRNAME, TWOPHOTON_ONLY_DIRNAME) or (
                len(rel) == 3 and rel[0] == GENERIC_DIRNAME
            ):
                self._leaves(here)
                subdirs[:] = []
            elif any(marker in files for marker in _ACQUISITION_MARKERS):
                subdirs[:] = []  # a 2P folder somewhere unexpected
            else:
                subdirs[:] = sorted(
                    d
                    for d in subdirs
                    if d
                    not in (
                        RECORDING_INFO_DIRNAME,
                        LEGACY_BEHAVIOR_DIRNAME,
                        LEGACY_RENDERINGS_DIRNAME,
                    )
                )

    def _leaves(self, parent: Path) -> None:
        """Note each 2P folder in *parent* (absent: none)."""
        try:
            leaves = sorted(p for p in parent.iterdir() if p.is_dir())
        except OSError:
            return
        for leaf in leaves:
            self.twophoton_dirs.append(leaf)
            marker = read_json(leaf / TWOPHOTON_2P_ONLY_SOURCE_FILENAME)
            if marker and marker.get("experiment"):
                self.source_markers[leaf] = str(marker["experiment"])

    # ------------------------------------------------------------ updates

    def note_copy(self, leaf: Path, experiment: str | None = None) -> None:
        """Add a 2P folder this run copied (with its source marker)."""
        if leaf not in self.twophoton_dirs:
            self.twophoton_dirs.append(leaf)
        if experiment:
            self.source_markers[leaf] = experiment

    def refresh_take(self, path: Path) -> None:
        """Re-read a take this run paired, as the walk would now see it."""
        for take in self.takes:
            if take.path == path:
                take.match = match_record(path)
                for entry in take.match.get("matched", []):
                    if entry.get("path"):
                        self.note_copy(
                            path / TWOPHOTON_DIRNAME / Path(entry["path"]).name
                        )

    def relocate(self, source: Path, target: Path) -> None:
        """Follow a folder the reconcile moved, with everything under it."""

        def moved(path: Path) -> Path:
            return (
                target / path.relative_to(source)
                if path.is_relative_to(source)
                else path
            )

        for take in self.takes:
            take.path = moved(take.path)
        self.twophoton_dirs = [moved(leaf) for leaf in self.twophoton_dirs]
        self.source_markers = {moved(k): v for k, v in self.source_markers.items()}

    # ------------------------------------------------------------ queries

    def matched_sources(self, source_root: Path) -> set[Path]:
        """Every share folder a transferred take is paired with."""
        return {
            (source_root / entry["path"]).resolve()
            for take in self.takes
            for entry in take.match.get("matched", [])
            if entry.get("path")
        }

    def twophoton_dirs_named(self, name: str) -> list[Path]:
        return [p for p in self.twophoton_dirs if p.name == name]
