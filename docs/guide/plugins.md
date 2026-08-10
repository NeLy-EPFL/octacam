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

### `triggerbox` — configurable camera trigger + lights

Drives the EPFL `common-trigger-circuit` board (Arduino Nano ESP32) as a full
camera-trigger + light controller. Adds the web GUI's **triggerbox** tab and,
when "arm with recording" is on, arms the board at recording start so external
hardware-triggered cameras are driven for the recording's fps + duration.

Every output is chosen at run time from the config — no reflashing to rewire:

- **Camera lines** — one or more `{ pin, pulse_us, delay_us }`. `pin` is any
  broken-out line (`D5`–`D13`, `A0`–`A7`); `D2`/`D3`/`D4` are the status LED.
- **Light channels** — the three CCS channels (1→D5, 2→D6, 3→D7), each
  independently `off`, `strobe` (frame-locked; `duty_mode = "auto"` sizes the
  on-time to bracket the longest live camera exposure + `strobe_guard_us`, or a
  `manual` `duty_percent`), `continuous`, or an optogenetic `pulse_train`
  (`freq_hz`, `pulse_us`, `start_delay_ms`, `train_ms`). All three channels are
  electrically identical, so any channel can play any role.

```toml
[[plugins]]
name = "triggerbox"

[plugins.options]
device = "auto"
strobe_guard_us = 100
cameras = [ { pin = "D13", pulse_us = 500 } ]
lights = [
  { channel = 1, mode = "strobe", duty_mode = "auto" },
  { channel = 2, mode = "strobe", duty_mode = "manual", duty_percent = 20 },
  { channel = 3, mode = "pulse_train", freq_hz = 10, pulse_us = 5000 },
]
```

Omitting `cameras`/`lights` defaults to the classic rig: one `D13` line plus
channels 1 and 2 strobing. The firmware and wiring notes are in
[arduino/triggerbox/](https://github.com/NeLy-EPFL/octacam/tree/main/arduino/triggerbox).

If the board fails to arm — the ESP32-S3 USB link can occasionally *wedge*
(every transfer stalls while the port stays enumerated), which would otherwise
leave external-triggered cameras waiting forever — the plugin says so loudly in
the GUI and log and, on Linux, automatically attempts a USB bus reset to clear
the stall and re-arm. If recovery still fails, power-cycle or replug the board.

These plugins talk to their Arduino over serial via pyserial, which ships with
octacam.

## Finding the serial device

Each serial plugin's `device` option is the OS path to the board (e.g.
`/dev/ttyACM0`, `/dev/arduinoCams`, `COM3`). To see what's connected and how
octacam classifies it, run:

```bash
octacam doctor            # lists a "Serial devices" section (board, path, VID:PID)
octacam doctor --probe-serial   # also reads each board's firmware identity
```

`octacam doctor <config_dir>` cross-checks the device each enabled plugin is
configured for against what's actually plugged in, and flags a missing or wrong
one (with a suggested udev rule for a stable path).

Instead of a fixed path you can set `device = "auto"`: the plugin picks the sole
microcontroller-class port at launch, and reports a clear error if zero or more
than one is present (so it never grabs the wrong board). An explicit path always
wins over `"auto"`.

```toml
[[plugins]]
name = "triggerbox"
options = { device = "auto" }   # or "/dev/ttyACM0", or a udev symlink
```

In the GUI, each plugin tab's connection panel has a **port dropdown** so you can
pick a detected board and (re)connect to it without editing the config — handy
after an unplug/replug or when the path changed.
