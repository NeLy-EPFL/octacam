# triggerbox — configurable camera-trigger + light controller firmware

Firmware for the **Arduino Nano ESP32** mounted on the EPFL *common CCS light and
camera trigger circuit* (`common-trigger-circuit`). It exposes the board's full
capability to the host:

- **any number of camera-trigger lines** (≤ 12), each on a pin of your choice
  with its own pulse width and phase delay;
- **up to 3 CCS light channels** (D5/D6/D7), each independently one of four
  modes — **off / strobe / continuous / pulse-train**.

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

**D2/D3/D4 are reserved** for the status LED and cannot be used as trigger/light
outputs. Every other broken-out line (D5–D13, A0–A7) may. Note **A2 = GPIO3** is
an ESP32-S3 strapping pin — usable as an output but best avoided if a wired
trigger input imposes a strong pull.

**Boot state.** Until the first arm packet chooses them, all non-status pins are
held LOW via an internal pulldown (they can't float into a camera trigger input).
An external pull-down on trigger lines is still recommended.

**Light polarity.** Each CCS channel drives an NPN sink transistor, so an Arduino
pin driven HIGH sinks the channel to GND → **light ON**. This assumes the CCS
**PD3-3024-3-EI** follows the input level. If it is set to a one-shot/overdrive
strobe mode instead, its on-time is fixed on the unit and the strobe here only
needs to deliver a clean rising edge per frame.

## Serial wire protocol (v2)

Baud rate: **115200** (nominal — USB CDC). All multi-byte fields little-endian.

### Host → Arduino

**Arm** — `0xA5` · `version=2` (u8) · `payload_len` (u16) · *payload* · `checksum` (u8)

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

**Cancel** — `0xCA`. **Identify** — `0x3F` (`?`) → replies `TRIGGERBOX 2 <build>\n`,
where `<build>` is a short hash of the sketch source (see *Firmware fingerprint* below).

A valid new arm re-arms from any state (restarts a run in progress). `duration_ms`
is the master gate — a pulse-train longer than the run is truncated at the end.

### Arduino → Host (newline-terminated ASCII)

| Token            | Meaning                                                        |
|------------------|----------------------------------------------------------------|
| `R`              | running — arm accepted, clocks started                         |
| `D`              | done — requested duration elapsed, back to idle                |
| `C`              | cancelled — cancel received (or a valid re-arm superseded)     |
| `E<c>`           | rejected — arm refused; `<c>`: `v`ersion `c`hecksum `o`versize `l`ength `f`ps `p`in `r`eserved `d`uplicate `m`ode |
| `TRIGGERBOX 2 <build>` | reply to an identify request (`<build>` = source hash) |

## Flashing the firmware

**Let octacam do it (recommended).** octacam knows the fingerprint of the sketch
in this folder and, whenever the board is out of date (or blank, or running a
predecessor like `omniview`), offers to compile + upload it for you — from the
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
