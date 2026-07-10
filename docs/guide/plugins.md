# Plugins

Serial-hardware and integration features ship as **opt-in plugins**. The default
launch loads **none** — you enable only what a given rig needs.

octacam bundles two plugins, both of which talk to an Arduino over a serial
link:

| Name | Purpose | Firmware |
| --- | --- | --- |
| `flywheel` | Arduino stepper-motor / turntable controller | [`arduino/stepper_motor/`](https://github.com/NeLy-EPFL/octacam/tree/main/arduino/stepper_motor) |
| `twophoton` | Arduino hardware camera trigger for a 2-photon rig | [`arduino/2photon_trigger/`](https://github.com/NeLy-EPFL/octacam/tree/main/arduino/2photon_trigger) |

Their only dependency, **pyserial**, ships with octacam by default, so neither
needs an extra install.

## Listing the bundled plugins

`octacam list-plugins` prints each bundled plugin and whether it can currently
load. Output is tab-separated (`name`, status, summary):

```bash
octacam list-plugins
```

```
flywheel     available    Flywheel stepper-motor controller plugin (opt-in).
twophoton    available    2-photon rig hardware trigger plugin (opt-in).
```

`available` means the plugin's dependencies are present (they ship by default);
an `unavailable` line carries the reason in its summary.

## Enabling a plugin

### In the rig config (persistent)

Add a `[[plugins]]` entry to the rig's `octacam_config.toml`. Per-plugin settings
go under a `[plugins.options]` sub-table:

```toml
[[plugins]]
name = "flywheel"

[plugins.options]
device = "/dev/ttyACM0"
baud = 115200
```

A bare-name array works too when you don't need options:

```toml
plugins = ["flywheel", "twophoton"]
```

Malformed or duplicate entries are warned about and skipped — the rig still
launches.

### At launch (per run)

Both `octacam gui` and `octacam record` accept the same two flags:

```bash
# Add plugins on top of the config selection (--plugin is repeatable)
octacam gui <config_dir> --plugin flywheel --plugin twophoton

# Disable every plugin for this one run, ignoring the config
octacam gui <config_dir> --no-plugins
```

!!! note "How the flags combine"
    `--plugin NAME` is **added** to whatever the config already selects (names
    already in the config aren't loaded twice). `--no-plugins` overrides
    everything and loads none. With neither flag, the config is used as-is.

Unknown names and any plugin whose dependency is missing are logged and skipped
— octacam always keeps running.

!!! tip "Legacy name"
    The `flywheel` plugin was previously called `arduino`. A config entry or
    `--plugin arduino` still resolves to `flywheel`, with a deprecation warning,
    so upgrading never silently drops a configured plugin.

## Bundled plugins

### `flywheel` — stepper-motor controller

Drives an Arduino stepper motor over serial. When loaded it adds a **Flywheel**
tab to the web GUI (a loop program plus hold-to-jog manual control) and, at the
first captured frame of a recording, fires an armed loop command so the motion
is synchronised to actual capture.

| Option | Default | Meaning |
| --- | --- | --- |
| `device` | `/dev/ttyACM0` | OS path to the board (e.g. `/dev/ttyACM0`, a udev symlink, or `COM3`) |
| `baud` | `115200` | Serial baud rate |

The matching firmware lives in
[`arduino/stepper_motor/`](https://github.com/NeLy-EPFL/octacam/tree/main/arduino/stepper_motor).

### `twophoton` — 2-photon hardware trigger

Arms an Arduino camera trigger for a 2-photon rig. The Arduino waits for a
ThorSync rising edge, then emits a square-wave camera trigger at the recording's
frame rate for its duration. When loaded it adds a **2-Photon** tab to the web
GUI (an "arm with recording" control plus live Arduino state, broadcast over the
GUI WebSocket as *armed* → *triggered* → *done*), and arms the board at recording
start so capture is synced to the ThorSync edge.

| Option | Default | Meaning |
| --- | --- | --- |
| `device` | `/dev/arduinoCams` | OS path to the board (udev symlink, `/dev/ttyACM0`, `COM3`, …) |
| `baud` | `115200` | Serial baud rate |
| `default_fps` | `100` | Fallback frame rate when the GUI doesn't send one |
| `default_duration_ms` | `10000` | Fallback capture duration, in milliseconds |

```toml
[[plugins]]
name = "twophoton"

[plugins.options]
device = "/dev/arduinoCams"
default_fps = 100
default_duration_ms = 10000
```

Because this rig is externally triggered, run it with an **external** trigger
source so octacam lets the Arduino fire the frames rather than pacing a software
trigger — set `trigger_source_default_index = 1` in the rig config's `[gui]`
table (or pick *external* in the GUI), or pass `--trigger hardware` to
`octacam record`. The firmware and wiring notes are in
[`arduino/2photon_trigger/`](https://github.com/NeLy-EPFL/octacam/tree/main/arduino/2photon_trigger).

## Finding the serial device

Each plugin's `device` option is the OS path to its board — for example
`/dev/ttyACM0` on Linux, a stable udev symlink such as `/dev/arduinoCams`, or
`COM3` on Windows.

!!! warning "A missing board doesn't stop launch"
    If the configured board is absent (or unplugged) when octacam starts, the
    plugin logs a warning and the GUI keeps running. Once the board is
    connected, use the plugin tab's **Reconnect** control to re-open the port
    without restarting the server. The plugin also warns loudly if it can't arm
    the hardware at recording start, so an external-triggered rig doesn't wait
    forever on a trigger that never fires.

## Third-party plugins

Beyond the two bundled plugins, a separate package can register its own plugin
under the `octacam.plugins` entry-point group; octacam discovers it at launch
and it becomes selectable by name just like a builtin (the bundled names always
win on a conflict). See `octacam.plugins.base` for the plugin contract.
