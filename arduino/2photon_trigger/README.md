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

## Setting up a persistent device symlink (Linux)

On the 2-photon rig there may be multiple Arduinos (stepper motor + camera
trigger). A udev rule gives each one a stable, descriptive name regardless of
which USB port it is plugged into.

1. Find the device's serial number:
   ```bash
   udevadm info -a -n /dev/ttyACM0 | grep '{serial}' | head -1
   ```
2. Create `/etc/udev/rules.d/99-octacam.rules` with one line per Arduino:
   ```
   SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{serial}=="<SERIAL>", \
     SYMLINK+="ArduinoCam", MODE="0666"
   ```
   Repeat with a different `SYMLINK` name (e.g. `ArduinoStepper`) for the
   stepper-motor Arduino.
3. Reload rules:
   ```bash
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```
4. The device is now available as `/dev/ArduinoCam`.

## octacam plugin configuration

In `octacam_config.toml`:

```toml
[[plugins]]
name = "twophoton"
device = "/dev/ArduinoCam"   # or /dev/ttyACM0, COM3, etc.
# baud = 115200              # optional; matches firmware default
# default_fps = 100          # fallback FPS when GUI params are absent
# default_duration_ms = 10000  # fallback duration in ms
```

Or at launch time:
```bash
octacam gui configs/my_rig --plugin twophoton
```

The plugin requires pyserial:
```bash
uv add "octacam[twophoton]"
# or: pip install "octacam[twophoton]"
```

## Windows

Use the COM port shown in Device Manager (e.g. `COM3`):

```toml
[[plugins]]
name = "twophoton"
device = "COM3"
```
