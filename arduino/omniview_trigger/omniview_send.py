#!/usr/bin/env python3
"""Standalone driver for the omniview trigger firmware.

Sends an arm packet to the Arduino Nano ESP32 running ``omniview_trigger.ino``
and prints its status tokens, so you can bench-test camera triggering and LED
strobing without launching octacam. Ctrl-C sends a cancel.

Examples::

    # 100 fps for 10 s, LEDs strobed at 20% duty
    python omniview_send.py --fps 100 --duration 10 --duty 20

    # explicit device, 5 s, 50% duty, 300 µs camera pulse
    python omniview_send.py -d /dev/ttyACM0 --fps 60 --duration 5 \
        --duty 50 --cam-pulse 300

    # just check what's on the port
    python omniview_send.py --identify

The wire protocol mirrors the firmware header:

    Arm     (11 bytes): 0xA5 | fps u16 | duration_ms u32 | duty_permille u16
                             | cam_pulse_us u16      (all little-endian)
    Cancel  (1 byte) : 0xCA
    Identify(1 byte) : 0x3F ('?')  -> "OMNIVIEW <version>\\n"
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
CANCEL_MAGIC = 0xCA
IDENTIFY_MAGIC = 0x3F
ARM_FORMAT = "<BHIHH"  # magic u8, fps u16, duration_ms u32, duty u16, cam_pulse u16

MAX_FPS = 5000


def build_arm(fps: int, duration_ms: int, duty_permille: int, cam_pulse_us: int) -> bytes:
    return struct.pack(
        ARM_FORMAT, ARM_MAGIC, fps, duration_ms, duty_permille, cam_pulse_us
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the omniview camera-trigger + LED-strobe firmware.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-d", "--device", default="/dev/ttyACM0", help="serial port")
    p.add_argument("-b", "--baud", type=int, default=115200, help="baud rate")
    p.add_argument("--fps", type=float, default=100.0, help="frames per second")
    p.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="run length in seconds (0 = run until Ctrl-C)",
    )
    p.add_argument(
        "--duty",
        type=float,
        default=20.0,
        help="LED strobe duty cycle in percent (0-100)",
    )
    p.add_argument(
        "--cam-pulse",
        type=int,
        default=0,
        help="camera trigger pulse width in µs (0 = firmware default)",
    )
    p.add_argument(
        "--identify",
        action="store_true",
        help="just query the board's identity and exit",
    )
    return p.parse_args(argv)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def read_tokens(port: serial.Serial, deadline: float) -> None:
    """Print newline-terminated tokens from the board until the deadline."""
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
            if text == "D":  # done
                return


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    fps = int(round(clamp(args.fps, 1, MAX_FPS)))
    # Clamp to the uint32 wire field (~49.7 days) so a huge --duration can't crash
    # struct.pack; every other field is likewise clamped to fit its type.
    duration_ms = min(int(round(max(0.0, args.duration) * 1000)), 0xFFFFFFFF)
    duty_permille = int(round(clamp(args.duty, 0, 100) * 10))  # % -> permille
    cam_pulse_us = int(clamp(args.cam_pulse, 0, 65535))

    try:
        port = serial.Serial(args.device, args.baud, timeout=0.2, write_timeout=2)
    except serial.SerialException as e:
        sys.exit(f"cannot open {args.device}: {e}")

    with port:
        # The ESP32 USB-CDC resets on port open; give it a moment to boot.
        time.sleep(1.5)
        port.reset_input_buffer()

        if args.identify:
            port.write(bytes([IDENTIFY_MAGIC]))
            read_tokens(port, time.monotonic() + 1.0)
            return 0

        arm = build_arm(fps, duration_ms, duty_permille, cam_pulse_us)
        print(
            f"-> arm: fps={fps} duration={duration_ms} ms "
            f"duty={duty_permille / 10:.1f}% cam_pulse="
            f"{cam_pulse_us or 'default'} µs  [{arm.hex()}]"
        )
        port.write(arm)

        # Wait a bit past the requested duration for the 'D' token; if the run is
        # open-ended (duration 0), read until Ctrl-C.
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
