# omniview — camera trigger + synchronized LED strobe firmware

Firmware for the **Arduino Nano ESP32** mounted on the EPFL *common CCS light and
camera trigger circuit* (`common-trigger-circuit`). It drives the shared trigger
line for the omniview rig's **4 Basler + 2 FLIR** cameras and, phase-locked to
every trigger edge, strobes **two CCS LED ring lights** (CCS channels 1 & 2).

The host (octacam, or the bundled [`omniview_send.py`](omniview_send.py)) sends
one *arm* packet over USB serial carrying the **FPS, duration, and strobe duty
cycle**; the Arduino generates the whole capture in hardware-timed software and
reports back when it starts and finishes.

## How it works

On each frame the Arduino:

1. raises the **camera trigger** (D13) — a short pulse that gives all six cameras
   a clean rising edge, then returns LOW before the next frame; and
2. at the **same instant**, raises **CCS ch1 (D5)** and **ch2 (D6)** and holds
   them for `duty × frame_period`, then drops them (the strobe).

Because both the trigger pulse and the light window are derived from a single
per-frame `t0`, the lights are guaranteed ON while the sensors integrate, with no
cross-channel skew. Timing is done in software with `micros()`; on the 240 MHz
ESP32-S3 the per-edge jitter is well under 1 µs.

```
frame period = 1/FPS
│
├─ camera D13  ▔▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁   (short pulse, rising edge = frame start)
│              ▔▔▔▔▔▁▁▁▁▁▁▁▁▁▁▁▁
├─ CCS ch1 D5  strobe on ──┘         on-time = duty × period
│              ▔▔▔▔▔▁▁▁▁▁▁▁▁▁▁▁▁
└─ CCS ch2 D6  strobe on ──┘         (identical to ch1)
   ↑ both light channels rise on the same edge as the camera trigger
```

Set the duty so the light on-time comfortably covers each camera's
`ExposureTime`. Duty **0%** = lights off; **100%** = lights stay on continuously
(no strobing).

## Hardware wiring

Pins follow the `common-trigger-circuit` schematic — no changes to the board are
needed; you only wire the camera trigger inputs to D13.

| Signal              | Nano ESP32 pin | Notes                                             |
|---------------------|----------------|---------------------------------------------------|
| Camera trigger      | **D13**        | shared LVTTL (3.3 V) line to all 6 camera trigger inputs |
| CCS channel 1 sink  | D5             | LED ring light 1 (via on-board NPN sink)          |
| CCS channel 2 sink  | D6             | LED ring light 2 (via on-board NPN sink)          |
| CCS channel 3 sink  | D7             | reserved / unused — held LOW                       |
| Status LED (red)    | D2             | board indicator: **red = recording**              |
| Status LED (green)  | D3             | board indicator: **green = idle / ready**         |
| Status LED (blue)   | D4             | unused                                            |

**Light polarity.** Each CCS channel is driven through an NPN sink transistor, so
an Arduino pin driven HIGH sinks the channel to GND → **light ON**. This assumes
the CCS **PD3-3024-3-EI** is in an external-trigger mode that *follows the input
level* (light on while the sink line is active). If instead the controller is set
to a one-shot / overdrive strobe mode, the on-time is fixed on the unit and the
duty here only needs to be non-zero to deliver a clean rising edge per frame.

**Trigger levels.** D13 outputs 3.3 V LVTTL, compatible with the Basler
a2A-series trigger inputs. Configure the cameras for hardware triggering
(`TriggerMode=On`, `TriggerSource` = the wired line, `TriggerActivation=RisingEdge`)
before starting a run.

## Serial wire protocol

Baud rate: **115200** (nominal — it's USB CDC).

### Host → Arduino

| Packet   | Bytes | Layout (little-endian)                                                                 |
|----------|-------|----------------------------------------------------------------------------------------|
| Arm      | 11    | `0xA5` · fps `u16` · duration_ms `u32` · duty_permille `u16` · cam_pulse_us `u16`      |
| Cancel   | 1     | `0xCA`                                                                                  |
| Identify | 1     | `0x3F` (`?`) → replies `OMNIVIEW <version>\n`                                           |

- **fps** — 1..5000. **duration_ms** — 0 means *run until cancel*.
- **duty_permille** — LED on-time as ‰ of the frame period (0..1000, i.e.
  0.0–100.0%).
- **cam_pulse_us** — camera pulse width in µs; **0 selects the firmware default
  (500 µs)**. Always clamped below the frame period.

A new arm packet re-arms from any state (it restarts a run in progress). A cancel
returns the board to idle.

### Arduino → Host (newline-terminated ASCII)

| Token                    | Meaning                                             |
|--------------------------|-----------------------------------------------------|
| `R`                      | running — arm accepted, frame clock started         |
| `D`                      | done — requested duration elapsed, back to idle     |
| `C`                      | cancelled — cancel received (or a re-arm superseded)|
| `OMNIVIEW <version>`     | reply to an identify request                        |

## Flashing the firmware

Requires the **Arduino ESP32** core (board: *Arduino Nano ESP32*).

Arduino IDE:
1. Boards Manager → install **esp32 by Arduino** and select **Arduino Nano ESP32**.
2. Open `omniview_trigger.ino`, pick the port, Upload.

`arduino-cli`:
```bash
arduino-cli core install arduino:esp32
arduino-cli compile -b arduino:esp32:nano_nora arduino/omniview_trigger
arduino-cli upload  -b arduino:esp32:nano_nora -p /dev/ttyACM0 arduino/omniview_trigger
```

## Bench-testing without octacam

Put the cameras into hardware-trigger mode (or just watch the LEDs / a scope),
then:

```bash
python arduino/omniview_trigger/omniview_send.py --fps 100 --duration 10 --duty 20
python arduino/omniview_trigger/omniview_send.py --identify      # who's on the port?
```

See `omniview_send.py --help` for all options (device, baud, cam-pulse, …).
Ctrl-C during a run sends a cancel.

## Startup ordering note

The arm packet *is* the start signal — the Arduino begins pulsing immediately.
Make sure the cameras are already armed and waiting for their trigger before the
arm packet is sent, or the first frame(s) can be missed. octacam's `omniview`
plugin arms the board from `on_recording_start`, i.e. after the cameras are put
into triggered acquisition, so this ordering is handled for you.

## Persistent device symlink (Linux, optional)

To always find this board at a stable path regardless of USB port, add a udev
rule keyed on its serial number (see the `2photon_trigger` README for the full
procedure) and point the plugin's `device` option at the symlink, e.g.
`/dev/arduinoOmniview`.
