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

| Packet   | Bytes | Layout                                                            |
|----------|-------|-------------------------------------------------------------------|
| Arm      | 7     | `0xA5` · fps `uint16_t` LE · duration_ms `uint32_t` LE            |
| Cancel   | 1     | `0xCA`                                                            |
| Identify | 1     | `0x3F` (`?`) — request the identity banner                       |

A new arm packet re-arms the Arduino from any state (including mid-capture).
A cancel packet returns it to IDLE.

### Arduino → Host

| Token               | Meaning                                                          |
|---------------------|------------------------------------------------------------------|
| `A`                 | Armed — waiting for ThorSync rising edge                         |
| `T`                 | Triggered — capture running                                      |
| `D`                 | Done — capture complete, back to IDLE                            |
| `2PHOTON 1 <build>` | Reply to an identify request (newline-terminated; `<build>` = source hash) |

## State machine

```
IDLE ──(arm packet)──▶ ARMED ──(ThorSync ↑)──▶ RUNNING ──(timeout)──▶ IDLE
                         │                                                │
               (cancel / new arm)                                   sends 'D'
                         │
                        IDLE
```

## Flashing the firmware

**Let octacam do it (recommended).** octacam knows the fingerprint of the sketch
in this folder and, whenever the board is out of date (or blank, or running a
predecessor), offers to compile + upload it for you — from the GUI's **Flash
firmware** button, the CLI, or a prompt when `octacam record` starts:

```bash
octacam flash --plugin twophoton --device /dev/arduinoCams   # prompts, then flashes
octacam flash rig_config/ --check                            # report only (CI-friendly)
octacam flash rig_config/ --yes                              # flash without prompting
```

Headless `octacam record` only warns unless you pass `--yes` or set
`auto_flash = true` under `[plugins.options]`. octacam uses `arduino-cli` under the
hood (found on `PATH` or via `OCTACAM_ARDUINO_CLI`).

**By hand.**

1. Open `2photon_trigger.ino` in the Arduino IDE (or use `arduino-cli`).
2. Select **Board: Arduino Mega or Mega 2560** and the correct port.
3. Upload.

### Firmware fingerprint

The identify banner ends with a short hash of the sketch source
(`fw_build_info.h` → `TWOPHOTON_FW_BUILD`), so octacam can tell whether the
*exact* current firmware is running. The committed value is a placeholder
(`UNBAKED`); octacam bakes the real hash into a throwaway copy of the sketch at
flash time (the repo tree is never modified). A board flashed **by hand** reports
`UNBAKED`, which octacam treats as "not the managed build" and offers to reflash —
harmless, but let octacam flash it once to sync the fingerprint.

## octacam plugin configuration

The plugin default device path is `/dev/arduinoCams`. On an existing rig that
already has that symlink, simply enabling the plugin is enough:

```toml
# octacam_config.toml — minimal, uses the default /dev/arduinoCams
[[plugins]]
name = "twophoton"
```

Override the device or other options as needed (settings go under a
`[plugins.options]` sub-table):

```toml
[[plugins]]
name = "twophoton"

[plugins.options]
device = "/dev/arduinoCams"     # default; override for ttyACM1, COM3, etc.
# baud = 115200                 # optional; matches firmware default
# default_fps = 100             # fallback FPS when GUI params are absent
# default_duration_ms = 10000   # fallback duration in ms
```

Or enable at launch time without touching the config:
```bash
octacam gui configs/my_rig --plugin twophoton
```

The plugin uses pyserial, which ships with octacam by default — no extra
install is needed.

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
     SYMLINK+="arduinoCams", MODE="0666"
   ```
   The symlink name must match the plugin's default device `/dev/arduinoCams`
   (override `device` in the config if you use a different name). Add a second
   line with `SYMLINK+="arduinoStepper"` for the stepper Arduino.
3. Reload rules and replug:
   ```bash
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ```
4. Verify: `ls -l /dev/arduinoCams` should point to a `ttyACM*` device.

