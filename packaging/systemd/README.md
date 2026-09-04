# systemd examples

Not installed by octacam — copy, edit the placeholders, and enable by hand on
the machine that has the 2-photon share mounted.

## octacam-twophoton-sweep

Runs `octacam process --twophoton-sweep` periodically to pick up 2-photon-only
folders (ThorSync/ThorImage output with no matching behavior take) — the
replacement for the legacy `move_files.sh`'s second ("2P data only") loop. See
[docs/guide/processing.md#2-photon-transfer](../../docs/guide/processing.md#2-photon-transfer).

```bash
sudo cp octacam-twophoton-sweep.service octacam-twophoton-sweep.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now octacam-twophoton-sweep.timer
sudo systemctl status octacam-twophoton-sweep.timer
journalctl -u octacam-twophoton-sweep.service -f   # watch a run
```

Before enabling, edit `octacam-twophoton-sweep.service`:

- `User=` / `WorkingDirectory=` — the account that can read the windows share
  mount and write the NAS mount.
- `ExecStart=`'s `--config` — the rig config whose `[transfer.twophoton]`
  points at the share to sweep.
- `After=`'s mount unit names — match your actual mounts
  (`systemctl list-units --type=mount`); the sweep is a no-op (not an error) if
  the share isn't mounted yet, but running it before the mount is up just
  wastes a cycle.

The timer's `OnUnitActiveSec=30min` is a starting point — a slow share or many
recordings may warrant a longer interval; a manual `octacam process
--twophoton-sweep --config ...` run is always safe to run in parallel/on-demand
too (unmatched-folder detection re-scans the cache each time).
