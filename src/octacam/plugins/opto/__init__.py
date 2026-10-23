"""opto: optogenetic LED board — a Nano ESP32 running the triggerbox sketch,
used for its opto outputs only (opt-in).

A closed loop (the ``closedloop`` plugin) switches a channel on for a window
through :meth:`OptoPlugin.set_opto`; the board ends each window itself, so a
stalled host never leaves a light on. It never arms the board: the triggerbox (a
board of its own) drives the cameras and the illumination. Channel k is the
k-th ``channels`` entry, pulsing ``pulse_ms`` at ``freq_hz`` while on, or steady
without ``freq_hz``::

    [[plugins]]
    name = "opto"

    [plugins.options]
    device = "/dev/serial/by-id/usb-Arduino_Nano_ESP32_<serial>-if01"
    channels = [
      { pin = "D7" },
      { pin = "D8" },
      { pin = "D9", freq_hz = 20, pulse_ms = 10 },
    ]

Its tab forces a channel on ("override"), whatever else asks of it, until it is
released: kept on in OVERRIDE_WINDOW_S windows renewed every OVERRIDE_STEP_S,
so a crashed octacam leaves no light on for long.

A board is driven only once it identifies as the triggerbox sketch's protocol
(`octacam flash --plugin opto` uploads it): an unknown sketch would read the
packets' bytes as its own commands.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from pathlib import Path

from octacam.plugins.serial import SerialPlugin
from octacam.plugins.triggerbox.plugin import TriggerboxPlugin
from octacam.plugins.triggerbox.protocol import (
    MAX_OPTO,
    PIN_LABELS,
    REJECT_REASONS,
    RESERVED_PINS,
    OptoChannel,
    TriggerboxLink,
)

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/ttyACM1"
DEFAULT_BAUD = 115200
# The longest window set_opto switches a channel on for.
MAX_WINDOW_S = 600.0
# A forced channel's window and how often it is renewed.
OVERRIDE_WINDOW_S = 1.0
OVERRIDE_STEP_S = 0.25


class OptoPlugin(SerialPlugin):
    """Configures the board's opto outputs on every (re)open and switches them."""

    name = "opto"
    drives_opto = True
    web_dir = Path(__file__).parent / "web"
    firmware = TriggerboxPlugin.firmware
    default_device = DEFAULT_DEVICE
    reconnect_path = "/api/opto/reconnect"
    state_topic = "opto_state"
    # A silent board is most likely not flashed yet, not wedged.
    recover_silent = False
    _link: TriggerboxLink

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        auto_flash: bool = False,
        channels: list[OptoChannel] | None = None,
    ):
        super().__init__(
            TriggerboxLink(self._on_state, self._on_link_broken),
            device=device,
            baud=baud,
            auto_flash=auto_flash,
        )
        self.channels = channels or []
        self._forced: set[int] = set()  # channels held on by the override
        self._stop = threading.Event()
        self._keepalive: threading.Thread | None = None

    @classmethod
    def from_options(cls, options: dict) -> OptoPlugin:
        raw = options.get("channels", [])
        if not isinstance(raw, list) or len(raw) > MAX_OPTO:
            raise ValueError(f"'channels' must be an array of at most {MAX_OPTO} tables")
        channels: list[OptoChannel] = []
        for number, entry in enumerate(raw, start=1):
            if not isinstance(entry, dict):
                raise ValueError(f"opto channel {number} must be a table, got {entry!r}")
            pin = str(entry.get("pin", "")).upper()
            if pin not in PIN_LABELS or pin in RESERVED_PINS:
                raise ValueError(f"opto channel {number}: {pin or 'no'} pin is not an output")
            if pin in (c.pin for c in channels):
                raise ValueError(f"opto channel {number}: pin {pin} is already a channel")
            channels.append(
                OptoChannel(
                    pin=pin,
                    freq_hz=max(0.0, float(entry.get("freq_hz", 0.0))),
                    pulse_ms=max(0.0, float(entry.get("pulse_ms", 10.0))),
                )
            )
        return cls(
            device=str(options.get("device") or DEFAULT_DEVICE),
            baud=int(options.get("baud", DEFAULT_BAUD)),
            auto_flash=bool(options.get("auto_flash", False)),
            channels=channels,
        )

    # ------------------------------------------------------------ link

    def _on_state(self, token: str) -> None:
        """Run-state tokens: never armed, so none is expected."""

    def _on_link_broken(self) -> None:
        self._push_state()

    def _identify(self) -> None:
        super()._identify()
        if self.banner is None and self.firmware_ok:
            self.firmware_ok = False
            log.warning(
                "opto: %s did not identify; it is not driven until it runs the "
                "triggerbox sketch (`octacam flash --plugin opto`)",
                self.device,
            )

    def _open(self, *, recover: bool = True) -> str | None:
        error = super()._open(recover=recover)
        if error is None and self.channels and self.firmware_ok:
            self._configure()
        return error

    def setup(self) -> None:
        super().setup()
        self._stop.clear()
        self._keepalive = threading.Thread(target=self._renew, daemon=True, name="opto-override")
        self._keepalive.start()

    def teardown(self) -> None:
        self._stop.set()
        if self._keepalive is not None:
            self._keepalive.join(timeout=1.0)
        self._forced.clear()
        if self.channels and self._link.opto_configured:
            self._link.gate_opto([(index, 0) for index in range(len(self.channels))])
        self._link.close()

    def link_status(self) -> dict:
        return {**super().link_status(), "forced": sorted(self._forced)}

    def busy_reason(self) -> str | None:
        controller = self.controller
        if controller is not None and controller.recording_active:
            return "refusing to flash while a recording is active — stop it first"
        return None

    def status(self) -> dict:
        return {**super().status(), "channels": [asdict(c) for c in self.channels]}

    # ------------------------------------------------------------ outputs

    def _configure(self) -> bool:
        """Send the opto outputs (each switched off); True if the board took them."""
        result = self._link.configure_opto(self.channels)
        if result == "ok":
            log.info(
                "opto: channels %s",
                ", ".join(f"{i}={c.pin}" for i, c in enumerate(self.channels, start=1)),
            )
            return True
        code = self._link.opto_reject or ""
        detail = (
            f"code {code!r}: {REJECT_REASONS.get(code[:1], 'unknown')}"
            if result == "reject"
            else result
        )
        self.report_error(f"{self.device} did not take the opto channels ({detail})")
        return False

    def set_opto(self, channel: int, duration_s: float) -> bool:
        """Switch ``channel`` (1-based) on for ``duration_s`` from now (the board
        ends the window), or off with 0; True once the command is sent. A forced
        channel stays on: True, and nothing is sent."""
        if channel in self._forced:
            return True
        return self._gate(channel, duration_s)

    def opto_forced(self, channel: int) -> bool:
        return channel in self._forced

    def force(self, channel: int, on: bool) -> bool:
        """Hold ``channel`` on until released (``on`` False switches it off);
        True once the board was told."""
        if not 1 <= channel <= len(self.channels):
            return False
        if on:
            self._forced.add(channel)
            ok = self._gate(channel, OVERRIDE_WINDOW_S)
        else:
            self._forced.discard(channel)
            ok = self._gate(channel, 0.0)
        log.info("opto: channel %d %s by the override", channel, "forced on" if on else "released")
        self._push_state()
        return ok

    def _renew(self) -> None:
        while not self._stop.wait(OVERRIDE_STEP_S):
            for channel in sorted(self._forced):
                self._gate(channel, OVERRIDE_WINDOW_S)

    def _gate(self, channel: int, duration_s: float) -> bool:
        if not 1 <= channel <= len(self.channels):
            log.warning("opto: no channel %d (configured: %d)", channel, len(self.channels))
            return False
        if not self._link.is_open or not self.firmware_ok:
            return False
        if not self._link.opto_configured and not self._configure():
            return False
        duration_ms = int(round(min(max(duration_s, 0.0), MAX_WINDOW_S) * 1000))
        return self._link.gate_opto([(channel - 1, duration_ms)])

    def api_router(self):
        from fastapi import Body, HTTPException

        router = super().api_router()

        @router.post("/api/opto/override")
        def override(payload: dict = Body(...)):
            """Force ``{"channel": n, "on": true}`` on until ``"on": false``."""
            channel = int(payload.get("channel", 0))
            if not 1 <= channel <= len(self.channels):
                raise HTTPException(422, f"channel must be 1-{len(self.channels)}")
            ok = self.force(channel, bool(payload.get("on")))
            return {"ok": ok, "forced": sorted(self._forced)}

        return router
