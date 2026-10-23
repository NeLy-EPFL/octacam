#!/usr/bin/env python3
"""Standalone driver for the triggerbox firmware (protocol v5).

Sends an arm packet to the Arduino Nano ESP32 running `triggerbox.ino` and
prints its status tokens, so you can bench-test camera triggering, the light
channels and the dimmed (PWM) outputs without launching octacam. Ctrl-C sends a
cancel.

Camera lines and light channels are specified with repeatable flags. A light
channel is 1/2/3 (CCS ch1/ch2/ch3 = D5/D6/D7); all three are interchangeable.

Examples::

    # today's rig: one camera on D13, ch1+ch2 strobed 2 ms each, 80 fps 5 s
    python triggerbox_send.py --fps 80 --duration 5 \
        --camera D13 --light 1,strobe,2000 --light 2,strobe,2000

    # a camera on a different line, plus an optogenetic pulse train on ch3
    python triggerbox_send.py --camera D10,300 \
        --light 3,pulse,5000,100000,0,0     # 5 ms pulse, 10 Hz, from t0, forever

    # continuous illumination on ch1, nothing else
    python triggerbox_send.py --light 1,continuous

    # hold a white light on D9 at 40 % (no arm); it stays until changed or reset
    python triggerbox_send.py --dim D9,40

    # just check what's on the port
    python triggerbox_send.py --identify

    # light an opto LED wired to D8 for 3 s, 20 Hz x 10 ms pulses (no arm)
    python triggerbox_send.py --opto D8,20,10 --duration 3

Opto spec:    PIN[,freq_hz[,pulse_ms]]            (freq 0/omitted = steady)
Camera spec:  PIN[,pulse_us[,delay_us]]           (pulse 0/omitted = firmware default)
Light spec:   CH,off
              CH,strobe,on_us[,delay_us]
              CH,continuous
              CH,pulse,pulse_us,interval_us[,start_delay_us[,train_us]]
Dim spec:     PIN,percent                       (0 = LOW, 100 = steadily HIGH)

Dims are sent before the arm; with only --dim given, nothing is armed.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

ARM_MAGIC = 0xA5
DIM_MAGIC = 0xD1
OPTO_CONFIG_MAGIC = 0xB0
OPTO_GATE_MAGIC = 0xB1
CANCEL_MAGIC = 0xCA
IDENTIFY_MAGIC = 0x3F
PROTOCOL_VERSION = 5
DIM_FULL_SCALE = 4095  # 12-bit PWM duty; full scale holds the pin HIGH

# Canonical pin-label table -- must match kPinTable in triggerbox.ino (and
# PIN_LABELS in the octacam plugin). Index on the wire; D2/D3/D4 are the status
# LED and are rejected by the firmware.
PIN_LABELS = [
    "D2",
    "D3",
    "D4",
    "D5",
    "D6",
    "D7",
    "D8",
    "D9",
    "D10",
    "D11",
    "D12",
    "D13",
    "A0",
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
    "A6",
    "A7",
]
LIGHT_PINS = {1: "D5", 2: "D6", 3: "D7"}  # CCS channel -> default pin label
MODES = {"off": 0, "strobe": 1, "continuous": 2, "pulse": 3, "pulse_train": 3}

MAX_FPS = 5000
U16 = 0xFFFF
U32 = 0xFFFFFFFF

_FIXED = struct.Struct("<HIBB")  # fps, duration_ms, n_cam, n_light
_CAM = struct.Struct("<BHH")  # pin_id, pulse_us, delay_us
_LIGHT = struct.Struct("<BBIIII")  # pin_id, mode, p0, p1, p2, p3
_DIM = struct.Struct("<BH")  # pin_id, duty


def pin_id(label: str) -> int:
    try:
        return PIN_LABELS.index(label.upper())
    except ValueError:
        raise SystemExit(
            f"unknown pin {label!r}; valid: {', '.join(PIN_LABELS)}"
        ) from None


def frame(magic: int, payload: bytes) -> bytes:
    """A framed command: magic, version, payload_len, payload, XOR checksum."""
    body = bytes([PROTOCOL_VERSION]) + struct.pack("<H", len(payload)) + payload
    checksum = 0
    for byte in body:
        checksum ^= byte
    return bytes([magic]) + body + bytes([checksum])


def build_arm(
    fps: int, duration_ms: int, cams: list[tuple], lights: list[tuple]
) -> bytes:
    payload = _FIXED.pack(fps, duration_ms, len(cams), len(lights))
    for pid, pulse, delay in cams:
        payload += _CAM.pack(pid, pulse, delay)
    for pid, mode, p0, p1, p2, p3 in lights:
        payload += _LIGHT.pack(pid, mode, p0, p1, p2, p3)
    return frame(ARM_MAGIC, payload)


def build_dim(pid: int, duty: int) -> bytes:
    return frame(DIM_MAGIC, _DIM.pack(pid, duty))


def parse_camera(spec: str) -> tuple[int, int, int]:
    parts = spec.split(",")
    label = parts[0]
    pulse = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    delay = int(parts[2]) if len(parts) > 2 and parts[2] else 0
    return pin_id(label), min(pulse, U16), min(delay, U16)


def parse_light(spec: str) -> tuple[int, int, int, int, int, int]:
    parts = spec.split(",")
    if len(parts) < 2:
        raise SystemExit(f"light spec {spec!r} needs at least CH,MODE")
    ch = int(parts[0])
    if ch not in LIGHT_PINS:
        raise SystemExit(f"light channel must be 1/2/3, got {ch}")
    mode_name = parts[1].lower()
    if mode_name not in MODES:
        raise SystemExit(
            f"unknown light mode {parts[1]!r}; valid: off/strobe/continuous/pulse"
        )
    mode = MODES[mode_name]
    pid = pin_id(LIGHT_PINS[ch])
    nums = [int(x) for x in parts[2:] if x != ""]
    p = [0, 0, 0, 0]
    if mode == 1:  # strobe: on_us[, delay_us]
        p[1] = min(nums[0] if len(nums) > 0 else 0, U32)  # on_us
        p[0] = min(nums[1] if len(nums) > 1 else 0, U32)  # phase delay
    elif mode == 3:  # pulse_train: pulse_us, interval_us[, start_delay_us[, train_us]]
        if len(nums) < 2:
            raise SystemExit("pulse mode needs pulse_us,interval_us")
        p[0] = min(nums[0], U32)  # pulse_us
        p[1] = min(nums[1], U32)  # interval_us
        p[2] = min(nums[2] if len(nums) > 2 else 0, U32)  # start_delay_us
        p[3] = min(nums[3] if len(nums) > 3 else 0, U32)  # train_us
    return (pid, mode, p[0], p[1], p[2], p[3])


def parse_dim(spec: str) -> tuple[int, int]:
    label, _, percent = spec.partition(",")
    try:
        level = float(percent)
    except ValueError:
        raise SystemExit(f"dim spec {spec!r} needs PIN,percent") from None
    level = max(0.0, min(100.0, level))
    return pin_id(label), round(level / 100.0 * DIM_FULL_SCALE)


def parse_opto(spec: str) -> tuple[int, int, int]:
    """PIN[,freq_hz[,pulse_ms]] -> (pin_id, pulse_us, period_us)."""
    parts = spec.split(",")
    freq = float(parts[1]) if len(parts) > 1 else 0.0
    pulse_ms = float(parts[2]) if len(parts) > 2 else 10.0
    if freq <= 0:
        return (pin_id(parts[0]), 0, 0)
    return (pin_id(parts[0]), round(pulse_ms * 1000), round(1e6 / freq))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the triggerbox camera-trigger + light firmware (v5).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-d", "--device", default="/dev/ttyACM0", help="serial port")
    p.add_argument("-b", "--baud", type=int, default=115200, help="baud rate")
    p.add_argument("--fps", type=float, default=80.0, help="frames per second")
    p.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="run length in seconds (0 = run until Ctrl-C)",
    )
    p.add_argument(
        "--camera",
        action="append",
        default=[],
        metavar="PIN[,pulse[,delay]]",
        help="a camera trigger line (repeatable)",
    )
    p.add_argument(
        "--light",
        action="append",
        default=[],
        metavar="CH,MODE[,...]",
        help="a light channel (repeatable)",
    )
    p.add_argument(
        "--dim",
        action="append",
        default=[],
        metavar="PIN,percent",
        help="hold a pin at a PWM level (repeatable)",
    )
    p.add_argument(
        "--identify",
        action="store_true",
        help="just query the board's identity and exit",
    )
    p.add_argument(
        "--opto",
        action="append",
        default=[],
        metavar="PIN[,freq[,pulse_ms]]",
        help="light opto outputs for --duration instead of arming (repeatable)",
    )
    return p.parse_args(argv)


def read_tokens(port: serial.Serial, deadline: float, stop: str = "D") -> None:
    """Print the board's tokens until `stop` (done, by default), a reject or
    the deadline.
    """
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(64)
        if not chunk:
            continue
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            text = line.decode("ascii", "replace").strip()
            if text:
                print(f"  <- {text}")
            if text == stop or text[:1] in ("E", "o", "g"):  # done, or refused
                return


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fps = round(max(1.0, min(float(args.fps), MAX_FPS)))
    duration_ms = min(round(max(0.0, args.duration) * 1000), U32)

    try:
        port = serial.Serial(args.device, args.baud, timeout=0.2, write_timeout=2)
    except serial.SerialException as e:
        sys.exit(f"cannot open {args.device}: {e}")

    with port:
        time.sleep(1.5)  # ESP32 USB-CDC resets on open; let it boot
        port.reset_input_buffer()

        if args.identify:
            port.write(bytes([IDENTIFY_MAGIC]))
            read_tokens(port, time.monotonic() + 1.0)
            return 0

        for pid, duty in (parse_dim(s) for s in args.dim):
            print(f"-> dim: {PIN_LABELS[pid]} duty={duty}/{DIM_FULL_SCALE}")
            port.write(build_dim(pid, duty))
            read_tokens(port, time.monotonic() + 1.0, stop="P")
        if args.dim and not (args.camera or args.light or args.opto):
            return 0

        if args.opto:
            records = [parse_opto(s) for s in args.opto]
            payload = bytes([len(records)]) + b"".join(
                struct.pack("<BII", *r) for r in records
            )
            port.write(frame(OPTO_CONFIG_MAGIC, payload))
            read_tokens(port, time.monotonic() + 0.5, stop="O")
            gates = b"".join(
                struct.pack("<BI", k, duration_ms) for k in range(len(records))
            )
            print(f"-> opto on for {duration_ms} ms: {', '.join(args.opto)}")
            port.write(frame(OPTO_GATE_MAGIC, gates))
            read_tokens(port, time.monotonic() + duration_ms / 1000 + 0.5)
            return 0

        cams = [parse_camera(s) for s in args.camera] or [parse_camera("D13")]
        lights = [parse_light(s) for s in args.light]
        arm = build_arm(fps, duration_ms, cams, lights)
        print(
            f"-> arm: fps={fps} duration={duration_ms} ms "
            f"cams={len(cams)} lights={len(lights)}  [{arm.hex()}]"
        )
        port.write(arm)

        run_s = duration_ms / 1000 if duration_ms else float("inf")
        deadline = time.monotonic() + run_s + 2.0
        try:
            read_tokens(port, deadline)
        except KeyboardInterrupt:
            print("\n-> cancel")
            port.write(bytes([CANCEL_MAGIC]))
            read_tokens(port, time.monotonic() + 1.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
