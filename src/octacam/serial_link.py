"""Serial link to the Arduino stepper controller."""

import logging
import struct
from dataclasses import dataclass

import serial

log = logging.getLogger("octacam")

# Wire format of the packed C++ Command struct (and the matching struct in
# arduino_script): little-endian int16, uint16, uint16, uint8, uint8.
_COMMAND_FORMAT = "<hHHBB"


@dataclass
class Command:
    n_steps: int = 0
    step_interval_us: int = 0
    rest_duration_ms: int = 0
    n_repeats: int = 0
    init_wait_duration_s: int = 0

    def to_bytes(self) -> bytes:
        return struct.pack(
            _COMMAND_FORMAT,
            self.n_steps,
            self.step_interval_us,
            self.rest_duration_ms,
            self.n_repeats,
            self.init_wait_duration_s,
        )


class SerialLink:
    def __init__(self):
        self._serial: serial.Serial | None = None

    def open(self, device: str, baud: int) -> None:
        self.close()
        self._serial = serial.Serial(device, baud, timeout=0.1, write_timeout=1)

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def write_command(self, command: Command) -> None:
        if not self.is_open:
            return
        try:
            self._serial.write(command.to_bytes())
        except serial.SerialException as e:
            log.warning("Serial write failed: %s", e)
