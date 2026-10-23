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

The loop program can be set per rig, so it is on screen at launch instead of
being retyped in the tab, and a recording's config snapshot can restore it:

```toml
[[plugins]]
name = "flywheel"

[plugins.options]
device = "/dev/ttyACM0"

# One sweep: 2048 steps (negative starts counter-clockwise) at 1200 µs per step,
# resting 500 ms between sweeps, 5 sweeps, starting 2 s after the first frame.
[plugins.options.command]
n_steps = -2048
step_interval_us = 1200
rest_duration_ms = 500
n_repeats = 5
init_wait_duration_s = 2
```

!!! warning "`options` must be a sub-table, not an inline table"

    TOML inline tables are immutable, so `options = { device = "..." }` followed
    by `options.command = { ... }` is a **parse error**, not a merge. Use the
    `[plugins.options]` / `[plugins.options.command]` sub-table form above — it
    is also what `octacam config` writes back.

Omit `command` to keep the tab's own defaults. Whatever the tab is armed with at
record time is written into that recording's config snapshot, so the motion can
be reproduced later.

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

The firmware also has up to six opto outputs a host switches on for a window;
the [`opto`](#opto-optogenetic-led-board) plugin drives them, on a board of
their own.

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

The firmware speaks protocol v3: a board on an older build is not driven until
it is reflashed (`octacam flash`, or the tab's **Flash firmware** button).

Omitting `cameras`/`lights` defaults to the classic rig: one `D13` line plus
channels 1 and 2 strobing. The firmware and wiring notes are in
[arduino/triggerbox/](https://github.com/NeLy-EPFL/octacam/tree/main/arduino/triggerbox).

With `trigger_source = "managed"` the board also drives the cameras during live
preview, strobing exactly as the recording will. When a recording starts,
octacam cancels that preview arm, switches the cameras into the recording grab,
and only then arms the board for the recording, so the lights go dark for a
fraction of a second between preview and recording. That is deliberate: the
recording's first frame is the board's first pulse (pulse trains and
`start_delay_ms` count from it), and no frame is exposed under a strobe that was
re-phased while the camera was mid-readout.

If the board fails to arm — the ESP32-S3 USB link can occasionally *wedge*
(every transfer stalls while the port stays enumerated), which would otherwise
leave external-triggered cameras waiting forever — the plugin says so loudly in
the GUI and log and, on Linux, automatically attempts a USB bus reset to clear
the stall and re-arm. If recovery still fails, power-cycle or replug the board.

These plugins talk to their Arduino over serial via pyserial, which ships with
octacam.

### `opto` — optogenetic LED board

A second Nano ESP32 running the triggerbox sketch, used only for its opto
outputs: each channel is off until a closed loop (or the
[`closedloop`](#closedloop-ball-pushing-closed-loop) tab's **Test**) switches it
on for a window; it then stays on (`freq_hz` absent or 0) or pulses `pulse_ms`
at `freq_hz`. The board ends each window itself, so a stalled host never leaves
a light on. Channel *k* is the *k*-th entry. It never arms its board: the
triggerbox drives the cameras and the illumination. Adds an **Opto LEDs** panel
(a section of the **Closed loop** tab when that plugin is loaded, else a tab of
its own): the link and firmware, and an **override** per channel — its **On** button holds
that LED on, whatever the closed loop does, until it is pressed again (renewed
every 0.25 s in 1 s windows, so a crashed octacam leaves it on for at most a
second). A recording's closed-loop log records each override (`override_on`,
`override_off`) and counts the LED as lit meanwhile; the loop starts no light of
its own on a forced LED. An override set while the board is unplugged takes
effect once it is reconnected.

```toml
[[plugins]]
name = "opto"

[plugins.options]
# By serial number: two identical boards can swap /dev/ttyACM0 and 1 on replug.
device = "/dev/serial/by-id/usb-Arduino_Nano_ESP32_DCDA0C20DDFC-if01"
channels = [ { pin = "D7" }, { pin = "D8" }, { pin = "D9", freq_hz = 20, pulse_ms = 10 } ]
```

Flash it once with `octacam flash --plugin opto` (or the tab's **Flash
firmware**); a board that does not identify as the triggerbox sketch is never
sent anything. With two Nano ESP32s attached, flashing names the board's USB
serial number to the DFU uploader, which otherwise refuses to choose.

### `closedloop` — ball-pushing closed loop

Tracks the balls of a multi-corridor ball-pushing arena in real time and, while
recording, lights a corridor pair's opto channel when its fly pushes the ball.
It has no board of its own: the light is an [`opto`](#opto-optogenetic-led-board)
channel. Adds the web GUI's **Closed loop** tab, and draws on the tracked
camera's preview: each tracked ball ringed (green: tracked; orange: a weak
match; red, dashed: lost), an LED dot above it (filled red while its pair's
light is on), the pair's two corridors tinted red while lit, and a yellow ring
as a push lands. The drawing is in the preview's own image, so it follows zoom,
rotation and flips.

- **Tracking.** The ball of each pair's tracked corridor (not the controls) is
  followed on the camera's frames — a ring (the ball's bright rim between a dark
  center and a dark halo) matched by normalized cross-correlation in its
  corridor's column, at half resolution, near its last position. Corridors are
  numbered **left to right in the raw camera image**; every corridor's ball is
  found once to number them. It costs ~0.2 ms per ball per 2048² frame, so it keeps up with
  80 fps; a frame it could not get to in time is skipped, never queued. The
  arena is detected when the camera opens, when a ball is lost for a second
  outside a recording (a moved or replaced arena), and on the tab's **Detect
  balls**; it is adopted only with one ball in each of `corridors` evenly spaced
  columns, found twice half a second apart, so the corridor numbers cannot shift
  and nothing passing is taken for a ball (else the tab says what it found and
  it retries). A recording keeps its corridors; a ball hidden
  for a moment (an object over it) reads as lost — never as a push — and is found
  again in its column.
- **Which corridor of a pair is tracked.** The tab's **Track** column shows each
  pair's corridors left to right; click the other one to track it instead (its
  partner becomes the control). The arena is then detected afresh, so the new
  ball is locked onto within a second. Not while recording; a recording's config
  snapshot records the pairs it ran with.
- **Pushes.** A push is the ball moving `push_px` the `direction` way (`"up"`
  or `"down"` in the image, or `"any"`) from where it last came to rest or was
  last pushed to. Small forward nudges add up; a ball knocked back that bounces
  forward is not a push.
- **Light.** While recording, a push in a pair's tracked corridor switches the
  pair's opto channel on for at least `min_on_s`, and keeps it on while the
  ball keeps moving (either way): it goes off once the ball has been still
  (within 2 px) for 0.25 s and `min_on_s` is over, after at most 60 s. The light
  is held in 0.25 s steps the board ends by itself. Pushes while it is on, and
  in the `refractory_s` after it, earn no new light. The control corridor shares the light
  (a yoked control) and is not tracked. In preview nothing is lit:
  the tab shows the balls and counts the pushes, so the tracking can be checked
  before a recording. To test the light too (a magnet on a ball), tick **Light in
  preview (test)**: pushes then light their LEDs outside recordings, unlogged.
  It is off at every start and a recording's start turns it off, so a session
  never stimulates the flies outside a recording by accident. `enabled = false`
  tracks and logs recordings without ever lighting.

```toml
[[plugins]]          # first: its log opens before the boards arm
name = "closedloop"

[plugins.options]
camera = "top"       # the camera to track (name or serial); default the first
corridors = 6
pairs = [
  { corridor = 1, control = 2, channel = 1 },
  { corridor = 3, control = 4, channel = 2 },
  { corridor = 5, control = 6, channel = 3 },
]
direction = "up"
push_px = 10.0
min_on_s = 1.0
refractory_s = 0.0
enabled = true
ball_radius_px = 30.0   # the ball's rim in the image (the GS3 over the arena)
```

The tab edits the push rule between recordings; a recording's config snapshot
records the rule it ran with. Each recording writes, in its `octacam_recording/`
folder (and `octacam process` transfers them with it):

- `closedloop_frames.csv` — one row per tracked frame: `frame` (the recording's
  pulse, which is the video frame), the camera `timestamp_ns` (as in
  `timestamps.npz`), `latency_ms` (from the frame reaching octacam to the
  decision), each tracked corridor's ball `c<k>_x`, `c<k>_y`, `c<k>_score`
  (empty while lost) and each opto channel's `light<k>` (1 while lit).
- `closedloop_events.csv` — `frame`, `timestamp_ns`, `corridor`, `channel`,
  `event` and its `displacement_px` or `duration_s`: `stim` (a push that lit
  the pair), `push` (one that did not: the light already on, the refractory,
  or the loop disabled), `light_off` (with how long it was on), `stim_failed`
  (the board did not take the command) and `test` (the tab's **Test**).

Before the first recording with a new arena or wiring:

1. `octacam flash <config>` once if a board still runs an older triggerbox build.
2. In the **Closed loop** tab, press each pair's **Test**: the LED over that
   pair of corridors must light (check the `opto` pins and the driver's
   polarity if not).
3. Check the map: six corridors, numbered as you count them (left to right in
   the camera image), a ball in each tracked one.
4. Push a ball with a brush in preview: its pair's **Pushes** count goes up
   (in the configured `direction` only); the light stays off until you record.

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
