# Plugins

Optional hardware and integration features ship as **opt-in plugins**. The
default launch loads **none** — you choose what each rig needs.

## Enabling plugins

Enable a plugin persistently in the rig's `octacam_config.toml`:

```toml
# Bare names work too: plugins = ["flywheel"]
[[plugins]]
name = "flywheel"
options = { device = "/dev/ttyACM0", baud = 115200 }
```

…or per launch with `--plugin` (repeatable; adds to the config selection), and
disable everything for one run with `--no-plugins`:

```bash
octacam gui <config_dir> --plugin flywheel --plugin twophoton
octacam gui <config_dir> --no-plugins
```

Both `octacam gui` and `octacam record` accept these flags.

The bundled plugins' dependencies (pyserial) ship with octacam by default, so
they need no extra install. Run `octacam doctor` to see the bundled plugins and
whether each one can load.

## Bundled plugins

### `flywheel` — turntable stepper motor

Drives an Arduino stepper-motor controller over serial. Adds the web GUI's
**Flywheel** tab (a loop program plus hold-to-jog manual control), and fires an
armed loop command at the first captured frame so motion is synced to capture.

The matching firmware is in
[arduino/stepper_motor/](https://github.com/NeLy-EPFL/octacam/tree/main/arduino/stepper_motor).

### `twophoton` — 2-photon hardware trigger

Arms an Arduino hardware camera trigger for a 2-photon rig. The Arduino waits for
a ThorSync rising edge, then emits a square-wave trigger at the recording's fps
for its duration. Adds the web GUI's **2-Photon** tab (live Arduino state plus
"arm with recording") and arms at recording start, so capture is synced to the
ThorSync edge.

The firmware and wiring notes are in
[arduino/2photon_trigger/](https://github.com/NeLy-EPFL/octacam/tree/main/arduino/2photon_trigger).

Both plugins talk to their Arduino over serial via pyserial, which ships with
octacam.
