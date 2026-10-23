# triggerbox — configurable camera-trigger + light controller firmware

Firmware for the **Arduino Nano ESP32** mounted on the EPFL *common CCS light and
camera trigger circuit* (`common-trigger-circuit`). It exposes the board's full
capability to the host:

- **any number of camera-trigger lines** (≤ 12), each on a pin of your choice
  with its own pulse width and phase delay;
- **up to 3 CCS light channels** (D5/D6/D7), each independently one of four
  modes — **off / strobe / continuous / pulse-train**;
- **dimmed outputs**: any free pin held at a 0–100 % PWM level (11-bit, ~19.5 kHz) whatever the board is doing, e.g. the white ambient light of an arena. Arming, a run's end and a cancel never change it.

All output pins are chosen **at run time** from the arm packet, so you can rewire
which screw-terminal lines drive the cameras (or which light channels are used)
by editing the octacam config — **no reflashing**. The three CCS channels are
electrically identical (each an NPN sink, active-HIGH), so any channel can do any
job; there is no fixed "illumination vs. stimulation" role.

The host (octacam's `triggerbox` plugin, or the bundled
[`triggerbox_send.py`](triggerbox_send.py)) sends one *arm* packet over USB
serial; the Arduino generates the whole capture in software-timed loops and
reports back when it starts, finishes, or rejects the packet.

## How it works

Two clocks run while armed:

- a **frame clock** (`micros()`) — every frame-locked output (camera lines and
  `strobe` lights) derives its edges from one per-frame `t0`, so they stay
  mutually phase-locked; and
- a **run clock** (64-bit `esp_timer`) — `pulse_train` lights run on this
  independent clock, so an optogenetic train is *not* tied to the frame rate.

```
frame period = 1/FPS
├─ camera line   ▁▔▔▁▁▁▁▁▁▁▁▁▁▁▁   rising edge at (t0 + delay), width = pulse_us
├─ light strobe  ▁▔▔▔▔▔▁▁▁▁▁▁▁▁▁   on at (t0 + delay) for on_us  (frame-locked)
├─ light contin. ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔   on for the whole run
└─ light pulse   ▁▔▁▁▁▁▁▁▁▁▔▁▁▁▁   pulse_us every interval_us  (run clock, opto)
```

Outputs are written with `digitalWrite` on transitions only. Several edges on the
same loop iteration are emitted sequentially (~1–2 µs apart on the S3) — well
inside the frame period and the strobe guard, but not literally simultaneous.

A **dimmed output** runs on the ESP32-S3's LEDC peripheral, in hardware, so it costs the frame clock nothing. Level 0 drives the pin LOW and 100 % drives it steadily HIGH, both as plain GPIO (no PWM edges at either end); in between it is 11-bit PWM at 40 MHz / 2048 ≈ 19.5 kHz (the command carries a 12-bit level, halved on the board). On the 2.0.x core the LEDC runs from the 40 MHz crystal, where 12 bits tops out at 9.8 kHz: `ledcSetup(ch, 19531, 12)` fails on a real Nano ESP32. Applying a new level takes one pass of the main loop, like an arm; crossing 0 % or 100 % attaches or detaches the PWM, which takes longer (not yet measured), so a frame-locked edge due at that moment comes out that much late. The sketch uses the Arduino-ESP32 core 3.x LEDC API (`ledcAttach`) and falls back to the 2.0.x one (`ledcSetup` + `ledcAttachPin`) on an older core, such as a 2.0.x `arduino:esp32` Nano ESP32 package.

## Hardware wiring

Pins follow the `common-trigger-circuit` schematic. The **defaults** below match
the classic rig, but the host may pick any allowlisted pin for a camera line and
any of D5/D6/D7 for a light channel.

| Signal              | Nano ESP32 pin | Notes                                                    |
|---------------------|----------------|----------------------------------------------------------|
| Camera trigger(s)   | any of D5–D13, A0–A7 | LVTTL 3.3 V; classic rig uses **D13** to all cameras |
| CCS channel 1 sink  | **D5**         | light channel 1 (via on-board NPN sink)                  |
| CCS channel 2 sink  | **D6**         | light channel 2                                          |
| CCS channel 3 sink  | **D7**         | light channel 3                                          |
| Status LED (red)    | D2             | board indicator: **red = recording**                    |
| Status LED (green)  | D3             | board indicator: **green = idle / ready**                |
| Status LED (blue)   | D4             | unused                                                   |

**D2/D3/D4 are reserved** for the status LED and cannot be used as trigger/light/dim
outputs. Every other broken-out line (D5–D13, A0–A7) may. Note **A2 = GPIO3** is
an ESP32-S3 strapping pin — usable as an output but best avoided if a wired
trigger input imposes a strong pull.

**Lab LED driver board (replaces the CCS PD3).** The board's two logic-level N-MOSFET low-side switches take raw 3.3 V lines straight from the Nano (pin HIGH = LEDs on), so wire them to free lines outside the CCS sinks, e.g. one of D8–D12 / A0–A7 for each:

| Driver input | Example pin | Configure as                                                   |
|--------------|-------------|----------------------------------------------------------------|
| IR LEDs      | D10         | a light channel in `strobe` mode with `pin = "D10"`            |
| White LEDs   | D9          | a dim output (`dim = [{ pin = "D9", percent = 40 }]`)          |

Share the driver board's ground with the Nano's GND. A dimmed pin can't also be a camera line or light: the board rejects an arm that uses it while its level is above 0.

**Boot state.** Until the first arm packet chooses them, all non-status pins are
held LOW via an internal pulldown (they can't float into a camera trigger input).
An external pull-down on trigger lines is still recommended. **Dim outputs come up at full**: the board remembers in NVS (namespace `triggerbox`, key `dim_pins`, normally kept across reflashing) every pin a dim command has set and drives it steadily HIGH from power-up until octacam sets its level on connecting. So with the driver board the white LEDs are on at full as soon as the trigger board has power, and flies in the arena see full white (about 5,000 lux) until octacam connects, which matters for CsChrimson flies. An arm that drives such a pin takes it over and the board forgets it (it was rewired); any dim command, 0 % included, keeps it remembered. The set is written only when it changes, and only while the board is idle.

**Light polarity.** Each CCS channel drives an NPN sink transistor, so an Arduino
pin driven HIGH sinks the channel to GND → **light ON**. This assumes the CCS
**PD3-3024-3-EI** follows the input level. If it is set to a one-shot/overdrive
strobe mode instead, its on-time is fixed on the unit and the strobe here only
needs to deliver a clean rising edge per frame.

## Serial wire protocol (v4)

Baud rate: **115200** (nominal — USB CDC). All multi-byte fields little-endian.

### Host → Arduino

**Arm** — `0xA5` · `version=3` (u8) · `payload_len` (u16) · *payload* · `checksum` (u8)

The **payload** (`payload_len` bytes) is:

| Field         | Type | Notes                                             |
|---------------|------|---------------------------------------------------|
| fps           | u16  | 1..5000                                           |
| duration_ms   | u32  | 0 = run until cancel                              |
| n_cam         | u8   | number of camera records (≤ 12)                   |
| n_light       | u8   | number of light records (≤ 3)                     |
| camera_record | 5 B  | `pin_id` u8 · `pulse_us` u16 · `delay_us` u16     |
| light_record  | 18 B | `pin_id` u8 · `mode` u8 · `p0`,`p1`,`p2`,`p3` u32 |

`payload_len == 8 + 5·n_cam + 18·n_light`. **checksum** = XOR of `version`, the two
`payload_len` bytes, and every payload byte. **pin_id** is an index into the pin
table (0=D2 … 11=D13, 12=A0 … 19=A7). `pulse_us` 0 selects the firmware default
(500 µs).

Light **mode** fields:

| mode | name        | p0            | p1           | p2               | p3                     |
|------|-------------|---------------|--------------|------------------|------------------------|
| 0    | off         | —             | —            | —                | —                      |
| 1    | strobe      | phase_delay_us| on_us        | —                | — (frame-locked)       |
| 2    | continuous  | —             | —            | —                | — (on for whole run)   |
| 3    | pulse_train | pulse_us      | interval_us  | start_delay_us   | train_us (0 = forever) |

**Dim** — `0xD1` · `version=3` (u8) · `payload_len=3` (u16) · `pin_id` (u8) · `duty` (u16) · `checksum` (u8), framed and checksummed like an arm. `duty` is 0–4095: 0 drives the pin LOW, 4095 steadily HIGH, anything between is PWM at `duty`/4096 (a larger value counts as 4095). The board answers `P`, or `E<c>`; the level holds until the next dim of that pin or a board reset, whatever the run state. Dimming a pin that the running spec (or a staged re-arm) drives is rejected with `Eu`, and so is an arm that uses a pin dimmed above 0. Dim 0 frees the pin for arms again.

**Cancel** — `0xCA`. **Identify** — `0x3F` (`?`) → replies `TRIGGERBOX 4 <build>\n`,
where `<build>` is a short hash of the sketch source (see *Firmware fingerprint* below).

A valid new arm re-arms from any state, superseding a run in progress (`C`, then
`R`). When the fps is unchanged the **frame clock keeps its phase** and the new
spec **takes effect at the next frame edge**: outputs that stay in the spec carry
their level across the edge, so no output gets an early, late or extra edge; pins
that leave the spec are parked LOW and new pins start LOW. So a light edited live
during a managed preview never disturbs the trigger under cameras already exposing
on it — an overlapped-readout camera honors a re-phased trigger late and slides its
exposure out from under the strobe for several frames. A changed fps starts a fresh
clock at once. The run clock restarts with the new spec: `duration_ms` is the master
gate — a pulse-train longer than the run is truncated at the end — and pulse-train
`start_delay_us` counts from it.

### Arduino → Host (newline-terminated ASCII)

| Token            | Meaning                                                        |
|------------------|----------------------------------------------------------------|
| `R`              | running — arm accepted, clocks started                         |
| `D`              | done — requested duration elapsed, back to idle                |
| `C`              | cancelled — cancel received (or a valid re-arm superseded)     |
| `P`              | dimmed — dim command applied                                   |
| `E<c>`           | rejected — arm or dim refused; `<c>`: `v`ersion `c`hecksum `o`versize `l`ength `f`ps `p`in `r`eserved `d`uplicate `m`ode, `u` pin in use (dimmed, or driven by the run), `w` no free PWM channel (at most 8 pins dim between 0 and 100 %) |
| `TRIGGERBOX 4 <build>` | reply to an identify request (`<build>` = source hash) |

## Flashing the firmware

**Let octacam do it (recommended).** octacam knows the fingerprint of the sketch
in this folder and, whenever the board is out of date (or blank), offers to
compile + upload it for you — from the
GUI's **Flash firmware** button, the CLI, or a prompt when `octacam record`
starts:

```bash
octacam flash --plugin triggerbox --device /dev/ttyACM0   # prompts, then flashes
octacam flash rig_config/ --check                         # report only (CI-friendly)
octacam flash rig_config/ --yes                           # flash without prompting
```

Headless `octacam record` only warns unless you pass `--yes` or set
`auto_flash = true` under `[plugins.options]`. octacam uses `arduino-cli` under
the hood (found on `PATH` or via `OCTACAM_ARDUINO_CLI`); the sketch is located
relative to the repo, or via `OCTACAM_ARDUINO_DIR`.

**By hand.** Requires the **Arduino ESP32** core (board: *Arduino Nano ESP32*).

```bash
arduino-cli core install arduino:esp32
arduino-cli compile -b arduino:esp32:nano_nora arduino/triggerbox
arduino-cli upload  -b arduino:esp32:nano_nora -p /dev/ttyACM0 arduino/triggerbox
```

DFU upload may need a udev rule granting write access to the Nano ESP32 (VID
`2341`, PID `303a`), e.g. `MODE=0666` in `/etc/udev/rules.d/`.

### Firmware fingerprint

The identify banner ends with a short hash of the sketch source
(`fw_build_info.h` → `TRIGGERBOX_FW_BUILD`), so octacam can tell whether the
*exact* current firmware is running. The committed value is a placeholder
(`UNBAKED`); octacam bakes the real hash into a throwaway copy of the sketch at
flash time (the repo tree is never modified). A board flashed **by hand** reports
`UNBAKED`, which octacam treats as "not the managed build" and offers to reflash —
harmless, but let octacam flash it once to sync the fingerprint. Editing the
sketch changes the hash, so octacam will notice the drift and offer to reflash.

## Bench-testing without octacam

Put the cameras into hardware-trigger mode (or just watch the LEDs / a scope):

```bash
# today's rig: one camera on D13, ch1+ch2 strobed 2 ms, 80 fps 5 s
python arduino/triggerbox/triggerbox_send.py --fps 80 --duration 5 \
    --camera D13 --light 1,strobe,2000 --light 2,strobe,2000

# camera on a different line + an opto pulse train on ch3 (5 ms @ 10 Hz)
python arduino/triggerbox/triggerbox_send.py \
    --camera D10,300 --light 3,pulse,5000,100000,0,0

# hold a white light on D9 at 40 % without arming anything
python arduino/triggerbox/triggerbox_send.py --dim D9,40

python arduino/triggerbox/triggerbox_send.py --identify   # who's on the port?
```

See `triggerbox_send.py --help` for the full spec syntax. Ctrl-C sends a cancel.

## Startup ordering note

The arm packet *is* the start signal — the Arduino begins pulsing immediately, so
the cameras must already be armed and waiting for their trigger. octacam's
`triggerbox` plugin arms the board from `on_recording_start` (after the cameras
are put into triggered acquisition), so this ordering is handled for you.

## Persistent device symlink (Linux, optional)

To always find this board at a stable path, add a udev rule keyed on its serial
number and point the plugin's `device` option at the symlink, e.g.
`/dev/arduinoTriggerbox`.
