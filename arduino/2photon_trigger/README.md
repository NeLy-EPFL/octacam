# 2-Photon Camera Trigger Firmware

Arduino Mega 2560 firmware for the 2-photon microscope rig.  
Receives fps and duration from octacam over USB serial, arms itself, then waits
for a ThorSync rising-edge signal to start a precise camera trigger square wave.

## Hardware wiring

| Signal         | Arduino Mega pin | Notes                                    |
|----------------|-----------------|------------------------------------------|
| ThorSync in    | D4              | Active-HIGH; connect through a 3.3 V–5 V level shifter if ThorSync outputs 3.3 V |
| Camera trigger | A0              | Square wave out to camera Line 2 / Trigger In |
| DAQ sync       | A1              | Copy of the camera trigger for the DAQ  |
| Gate           | D38             | HIGH for the whole capture window        |
| Status LED     | LED_BUILTIN     | Mirrors Gate state                       |

> **Pin 4 does not support hardware interrupts on the Mega.**  The firmware
> polls for the rising edge in the main loop, which is fast enough for a rig
> where ThorSync and the arm command are sent well before capture begins.
> If sub-millisecond trigger latency is required, move the ThorSync wire to
> pin 2 or 3 (which support `attachInterrupt`) and adapt the firmware.

## Serial wire protocol

Baud rate: **115 200**.

### Host → Arduino

| Packet  | Bytes | Layout                                                              |
|---------|-------|---------------------------------------------------------------------|
| Arm     | 7     | `0xA5` · fps `uint16_t` LE · duration_ms `uint32_t` LE             |
| Cancel  | 1     | `0xCA`                                                              |

A new arm packet re-arms the Arduino from any state (including mid-capture).
A cancel packet returns it to IDLE.

### Arduino → Host

| Byte | Meaning                                   |
|------|-------------------------------------------|
| `A`  | Armed — waiting for ThorSync rising edge  |
| `T`  | Triggered — capture running               |
| `D`  | Done — capture complete, back to IDLE     |

## State machine

```
IDLE ──(arm packet)──▶ ARMED ──(ThorSync ↑)──▶ RUNNING ──(timeout)──▶ IDLE
                         │                                                │
               (cancel / new arm)                                   sends 'D'
                         │
                        IDLE
```

## Flashing the firmware

1. Open `2photon_trigger.ino` in the Arduino IDE (or use `arduino-cli`).
2. Select **Board: Arduino Mega or Mega 2560** and the correct port.
3. Upload.

## octacam plugin configuration

The plugin default device path is `/dev/arduinoCams`. On an existing rig that
already has that symlink, simply enabling the plugin is enough:

```toml
# octacam_config.toml — minimal, uses the default /dev/arduinoCams
[[plugins]]
name = "twophoton"
```

Override the device or other options as needed:

```toml
[[plugins]]
name = "twophoton"
device = "/dev/arduinoCams"   # default; override for ttyACM1, COM3, etc.
# baud = 115200              # optional; matches firmware default
# default_fps = 100          # fallback FPS when GUI params are absent
# default_duration_ms = 10000  # fallback duration in ms
```

Or enable at launch time without touching the config:
```bash
octacam gui configs/my_rig --plugin twophoton
```

The plugin requires pyserial:
```bash
uv sync --extra twophoton
# or: pip install "octacam[twophoton]"
```

## Setting up a persistent device symlink (Linux — new rigs only)

Existing rigs already have `/dev/arduinoCams`; skip this section.

On a fresh machine, multiple Arduinos (camera trigger + stepper motor) need
stable, descriptive names so the correct board is always at the expected path
regardless of USB port.

1. Find the camera-trigger Arduino's USB serial number:
   ```bash
   udevadm info -a -n /dev/ttyACM0 | grep '{serial}' | head -1
   ```
2. Create `/etc/udev/rules.d/99-octacam.rules`:
   ```
   SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{serial}=="<SERIAL>", \
     SYMLINK+="ArduinoCam", MODE="0666"
   ```
   Add a second line with `SYMLINK+="ArduinoStepper"` for the stepper Arduino.
3. Reload rules and replug:
   ```bash
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```
4. Verify: `ls -l /dev/arduinoCams` should point to a `ttyACM*` device.

## Windows

Use the COM port shown in Device Manager (e.g. `COM3`):

```toml
[[plugins]]
name = "twophoton"
device = "COM3"
```
