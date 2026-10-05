"""triggerbox rig trigger + light-controller plugin (opt-in).

Drives the Nano ESP32 running ``arduino/triggerbox`` on the EPFL
common-trigger-circuit board: camera-trigger lines plus three interchangeable
CCS light channels, each off, strobe, continuous or a pulse train on its own
clock. Pins are chosen at run time, so rewiring needs only the config::

    [[plugins]]
    name = "triggerbox"

    [plugins.options]
    device = "auto"            # a udev symlink, /dev/ttyACM0, COM3 or "auto"
    baud = 115200
    auto_flash = false         # headless: reflash a stale board without asking
    strobe_guard_us = 100      # added to the longest exposure by an auto duty
    cameras = [ { pin = "D13", pulse_us = 500 } ]
    lights = [
      { channel = 1, mode = "strobe", duty_mode = "auto" },
      { channel = 2, mode = "strobe", duty_mode = "manual", duty_percent = 20 },
      { channel = 3, mode = "off" },
    ]

Without ``cameras``/``lights`` it drives the classic rig: a D13 camera line and
channels 1 and 2 strobing at ``default_duty_percent``. An auto-duty strobe stays
on for ``max(TriggerDelay + ExposureTime) + strobe_guard_us`` over the live
cameras. Recordings use ``trigger_source = "managed"``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

from octacam import firmware as fw
from octacam.plugins.serial import SerialPlugin
from octacam.plugins.triggerbox.protocol import (
    ACK_TIMEOUT_S,
    BANNER,
    DEFAULT_DUTY_PERCENT,
    LIGHT_MODE_IDS,
    LIGHT_MODE_NAMES,
    LIGHT_PIN_BY_CHANNEL,
    MAX_CAM,
    MAX_FPS,
    MAX_LIGHT,
    PIN_LABELS,
    PROTOCOL_VERSION,
    REJECT_REASONS,
    RESERVED_PINS,
    ArmSpec,
    CameraLine,
    LightChannel,
    TriggerboxLink,
)
from octacam.plugins.triggerbox.train import period_us, plan_train, pulse_count

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/ttyACM0"
DEFAULT_BAUD = 115200
DEFAULT_FPS = 80
DEFAULT_DURATION_MS = 10_000
DEFAULT_CAM_PULSE_US = 0  # 0 → firmware default pulse width
DEFAULT_DUTY_AUTO = False
# Added to the longest TriggerDelay + ExposureTime by an auto-duty strobe, for
# trigger latency and jitter at the exposure's end (TriggerDelay covers its start).
DEFAULT_STROBE_GUARD_US = 100


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _camera_from_dict(d, default_pulse: int) -> CameraLine | None:
    if not isinstance(d, dict):
        log.warning("triggerbox: ignoring non-table camera entry %r", d)
        return None
    pin = str(d.get("pin", "D13")).upper()
    if pin not in PIN_LABELS:
        log.warning("triggerbox: unknown camera pin %r; using D13", pin)
        pin = "D13"
    elif pin in RESERVED_PINS:
        log.warning("triggerbox: camera pin %s is reserved for the status LED; "
                    "the board will reject it", pin)
    return CameraLine(
        pin=pin,
        pulse_us=_coerce_int(d.get("pulse_us"), default_pulse),
        delay_us=_coerce_int(d.get("delay_us"), 0),
    )


def _light_from_dict(d, default_duty_percent: float, default_duty_auto: bool) -> LightChannel | None:
    if not isinstance(d, dict):
        log.warning("triggerbox: ignoring non-table light entry %r", d)
        return None
    channel = _coerce_int(d.get("channel"), 1)
    if channel not in LIGHT_PIN_BY_CHANNEL:
        log.warning("triggerbox: light channel must be 1/2/3, got %r; using 1", channel)
        channel = 1
    pin = str(d.get("pin", LIGHT_PIN_BY_CHANNEL[channel])).upper()
    if pin not in PIN_LABELS:
        log.warning("triggerbox: unknown light pin %r; using %s", pin, LIGHT_PIN_BY_CHANNEL[channel])
        pin = LIGHT_PIN_BY_CHANNEL[channel]
    mode = str(d.get("mode", "off")).lower()
    if mode not in LIGHT_MODE_IDS:
        log.warning("triggerbox: unknown light mode %r; using off", mode)
        mode = "off"
    mode = LIGHT_MODE_NAMES[LIGHT_MODE_IDS[mode]]  # canonicalize (pulse → pulse_train)
    duty_mode = str(d.get("duty_mode", "auto" if default_duty_auto else "manual")).lower()
    if duty_mode not in ("auto", "manual"):
        duty_mode = "manual"
    return LightChannel(
        channel=channel,
        pin=pin,
        mode=mode,
        duty_mode=duty_mode,
        duty_percent=_coerce_float(d.get("duty_percent"), default_duty_percent),
        delay_us=_coerce_int(d.get("delay_us"), 0),
        freq_hz=_coerce_float(d.get("freq_hz"), 10.0),
        pulse_us=_coerce_int(d.get("pulse_us"), 1000),
        start_delay_ms=_coerce_float(d.get("start_delay_ms"), 0.0),
        train_ms=_coerce_float(d.get("train_ms"), 0.0),
    )


def _cameras_from(raw: list, default_pulse: int) -> list[CameraLine]:
    lines = (_camera_from_dict(entry, default_pulse) for entry in raw[:MAX_CAM])
    return [line for line in lines if line is not None]


def _lights_from(raw: list, default_duty_percent: float, default_duty_auto: bool) -> list[LightChannel]:
    lights = (
        _light_from_dict(entry, default_duty_percent, default_duty_auto)
        for entry in raw[:MAX_LIGHT]
    )
    return [light for light in lights if light is not None]


class TriggerboxPlugin(SerialPlugin):
    """Arms the board with the fps, duration and every camera line and light
    channel; the board then runs them on its own clock."""

    name = "triggerbox"
    generates_trigger = True
    web_dir = Path(__file__).parent / "web"
    firmware = fw.FirmwareSpec(
        name="triggerbox",
        sketch_dir=fw.resolve_sketch_dir("triggerbox"),
        fqbn="arduino:esp32:nano_nora",
        banner_prefix=BANNER,
        protocol_version=PROTOCOL_VERSION,
        build_define="TRIGGERBOX_FW_BUILD",
    )
    default_device = DEFAULT_DEVICE
    reconnect_path = "/api/triggerbox/reconnect"
    state_topic = "triggerbox_state"
    recover_silent = True
    _link: TriggerboxLink

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        auto_flash: bool = False,
        default_fps: int = DEFAULT_FPS,
        default_duration_ms: int = DEFAULT_DURATION_MS,
        default_duty_percent: float = DEFAULT_DUTY_PERCENT,
        default_duty_auto: bool = DEFAULT_DUTY_AUTO,
        strobe_guard_us: int = DEFAULT_STROBE_GUARD_US,
        default_cam_pulse_us: int = DEFAULT_CAM_PULSE_US,
        cameras: list[CameraLine] | None = None,
        lights: list[LightChannel] | None = None,
    ):
        super().__init__(
            TriggerboxLink(self._on_state, self._on_link_broken),
            device=device,
            baud=baud,
            auto_flash=bool(auto_flash),
        )
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self._default_duty_percent = default_duty_percent
        self._default_duty_auto = default_duty_auto
        self._strobe_guard_us = max(0, strobe_guard_us)
        self._default_cam_pulse_us = default_cam_pulse_us
        self._cameras: list[CameraLine] = cameras or [CameraLine(pin="D13", pulse_us=default_cam_pulse_us)]
        self._lights: list[LightChannel] = lights or []
        # Tab edits replace _cameras/_lights; snapshot_options compares with these.
        self._configured_cameras = [replace(c) for c in self._cameras]
        self._configured_lights = [replace(lt) for lt in self._lights]
        # A tab edit re-arms the board only during an (indefinite) preview arm.
        self._preview_armed = False

    @classmethod
    def from_options(cls, options: dict) -> TriggerboxPlugin:
        def _opt_int(key: str, default: int) -> int:
            try:
                return int(options.get(key, default))
            except (TypeError, ValueError):
                log.warning("triggerbox plugin: invalid %s %r; using %d", key, options.get(key), default)
                return default

        def _opt_float(*keys: str, default: float) -> float:
            # The first key present: configs spell some options two ways.
            for key in keys:
                if key in options:
                    try:
                        return float(options[key])
                    except (TypeError, ValueError):
                        log.warning("triggerbox plugin: invalid %s %r; using %g", key, options[key], default)
                        return default
            return default

        def _opt_bool(*keys: str, default: bool) -> bool:
            for key in keys:
                if key not in options:
                    continue
                val = options[key]
                if isinstance(val, bool):
                    return val
                if isinstance(val, str):
                    return val.strip().lower() in ("1", "true", "yes", "on")
                try:
                    return bool(int(val))
                except (TypeError, ValueError):
                    log.warning("triggerbox plugin: invalid %s %r; using %s", key, val, default)
                    return default
            return default

        default_duty_percent = _opt_float("duty_percent", "default_duty_percent", default=DEFAULT_DUTY_PERCENT)
        default_duty_auto = _opt_bool("duty_auto", "default_duty_auto", default=DEFAULT_DUTY_AUTO)
        default_cam_pulse_us = _opt_int("cam_pulse_us", DEFAULT_CAM_PULSE_US) \
            if "cam_pulse_us" in options else _opt_int("default_cam_pulse_us", DEFAULT_CAM_PULSE_US)

        # Camera lines: explicit array, else the classic single D13 line.
        raw_cams = options.get("cameras")
        if raw_cams is not None and not isinstance(raw_cams, list):
            log.warning("triggerbox plugin: 'cameras' must be an array of tables; ignoring %r", raw_cams)
        cameras = _cameras_from(raw_cams, default_cam_pulse_us) if isinstance(raw_cams, list) else []
        if not cameras:
            cameras = [CameraLine(pin="D13", pulse_us=default_cam_pulse_us)]

        # Light channels: explicit array, else the classic ch1+ch2 strobe.
        raw_lights = options.get("lights")
        if isinstance(raw_lights, list):
            lights = _lights_from(raw_lights, default_duty_percent, default_duty_auto)
        elif raw_lights is None:
            dm = "auto" if default_duty_auto else "manual"
            lights = [
                LightChannel(channel=1, pin="D5", mode="strobe", duty_mode=dm, duty_percent=default_duty_percent),
                LightChannel(channel=2, pin="D6", mode="strobe", duty_mode=dm, duty_percent=default_duty_percent),
            ]
        else:
            log.warning("triggerbox plugin: 'lights' must be an array of tables; ignoring %r", raw_lights)
            lights = []

        return cls(
            device=str(options.get("device") or DEFAULT_DEVICE),
            baud=_opt_int("baud", DEFAULT_BAUD),
            auto_flash=_opt_bool("auto_flash", default=False),
            default_fps=_opt_int("default_fps", DEFAULT_FPS),
            default_duration_ms=_opt_int("default_duration_ms", DEFAULT_DURATION_MS),
            default_duty_percent=default_duty_percent,
            default_duty_auto=default_duty_auto,
            strobe_guard_us=_opt_int("strobe_guard_us", DEFAULT_STROBE_GUARD_US),
            default_cam_pulse_us=default_cam_pulse_us,
            cameras=cameras,
            lights=lights,
        )

    def busy_reason(self) -> str | None:
        # The controller's state comes first: it flips before the board's 'R' arrives.
        controller = self.controller
        if controller is not None and controller.recording_active:
            return "refusing to flash while a recording is active — stop it first"
        if self.board_state == "running":
            return "refusing to flash while the board is armed/running — stop the recording first"
        return None

    def _on_state(self, token: str) -> None:
        state = {"R": "running", "D": "done", "C": "idle"}[token]
        if state == "running":
            self.last_error = None  # an arm took
        self._set_state(state)

    def _on_link_broken(self) -> None:
        self._set_state("idle")

    def teardown(self) -> None:
        self._link.send_cancel()
        self._link.close()
        self.board_state = "idle"

    def status(self) -> dict:
        return {
            **super().status(),
            "guard_us": self._strobe_guard_us,
            "cameras": [asdict(c) for c in self._cameras],
            "lights": [asdict(lt) for lt in self._lights],
        }

    # -------------------------------------------------- spec -> arm packet

    def _camera_windows(self) -> list[tuple[str, float, float | None]]:
        """``(name, trigger delay, exposure)`` µs of each live camera (the
        controller's), for the auto strobe duty."""
        if self.controller is None:
            return []
        return [
            (camera.name or f"cam{index}", *camera.trigger_window_us())
            for index, camera in enumerate(self.controller.camera_system)
        ]

    def _auto_led_on_us(self) -> float | None:
        """The strobe on-time covering the longest exposure plus the guard, or
        None when no exposure can be read."""
        coverages = [
            delay + exposure
            for _name, delay, exposure in self._camera_windows()
            if exposure is not None
        ]
        if not coverages:
            return None
        return max(coverages) + self._strobe_guard_us

    def _cameras_from_spec(self, spec: dict) -> list[CameraLine]:
        raw = spec.get("cameras")
        if isinstance(raw, list):
            return _cameras_from(raw, self._default_cam_pulse_us)
        return [replace(c) for c in self._cameras]

    def _lights_from_spec(self, spec: dict) -> list[LightChannel]:
        raw = spec.get("lights")
        if isinstance(raw, list):
            return _lights_from(raw, self._default_duty_percent, self._default_duty_auto)
        return [replace(lt) for lt in self._lights]

    def _spec_duration_ms(self, spec: dict) -> int:
        return max(
            1,
            min(0xFFFF_FFFF, _coerce_int(spec.get("duration_ms"), self._default_duration_ms)),
        )

    def _resolve_arm(
        self,
        spec: dict,
        lights: Sequence[LightChannel] = (),
        auto_led_on_us: float | None = None,
    ) -> ArmSpec:
        """The packet a start slice arms, running until cancelled (duration 0):
        its fps and camera lines, and *lights* with an auto strobe on for
        *auto_led_on_us* (its manual duty when None). Pure: no camera reads."""
        fps = max(1, min(MAX_FPS, _coerce_int(spec.get("fps"), self._default_fps)))
        return ArmSpec(
            fps=fps,
            duration_ms=0,
            cameras=[c.record() for c in self._cameras_from_spec(spec)[:MAX_CAM]],
            lights=[lt.resolve(1_000_000.0 / fps, auto_led_on_us) for lt in lights[:MAX_LIGHT]],
        )

    # -------------------------------------------------- recording lifecycle

    def default_start_params(self, fps: float, duration_s: float) -> dict:
        """The configured spec as the arm slice for headless ``octacam record``."""
        return {
            "fps": int(round(fps)),
            "duration_ms": max(1, int(round(duration_s * 1000))),
            "cameras": [asdict(c) for c in self._cameras],
            "lights": [asdict(lt) for lt in self._lights],
        }

    def snapshot_options(self, params: dict | None) -> dict | None:
        """The camera lines and light channels a recording armed, as config
        options; None when it armed none or what the config says. Off channels
        are left out on both sides: the tab never sends them, and an empty
        ``lights`` list reloads as all-off."""
        if params is None:
            return None
        cameras = self._cameras_from_spec(params)
        lights = [lt for lt in self._lights_from_spec(params) if lt.mode != "off"]
        configured = [lt for lt in self._configured_lights if lt.mode != "off"]
        if cameras == self._configured_cameras and lights == configured:
            return None
        return {
            "cameras": [asdict(c) for c in cameras],
            "lights": [asdict(lt) for lt in lights],
        }

    def trigger_train(self, params: dict | None) -> dict | None:
        """The exact period and pulse count on_recording_start emits for
        *params*. The count depends on the camera lines only: the lights' auto
        duty needs a camera read, which this pure hook (called under the
        controller lock) must not make."""
        if params is None:
            return None
        arm = self._resolve_arm(params)
        wanted = pulse_count(arm.fps, self._spec_duration_ms(params))
        plan = plan_train(arm.fps, wanted, arm.cameras)
        return {"period_ns": period_us(arm.fps) * 1000, "count": plan.count}

    def prime_trigger(self, params: dict | None, pulses: int) -> bool:
        """Emit *pulses* sacrificial pulses on the camera lines, lights dark (see
        "Priming" in CLAUDE.md). Returns once the board reports the burst done,
        or once it is cancelled, so the train starts on a fresh clock."""
        if params is None or not self._link.is_open or not self.firmware_ok:
            return False
        try:
            arm = self._resolve_arm(params)
            if not arm.cameras or pulses <= 0:
                return False
            arm = replace(arm, duration_ms=plan_train(arm.fps, pulses, arm.cameras).duration_ms)
        except Exception:
            log.exception("triggerbox: could not build the priming packet")
            return False
        if self._link.arm(arm) != "ok":
            return False
        if not self._link.wait_done(arm.duration_ms / 1000 + ACK_TIMEOUT_S):
            log.warning(
                "triggerbox: no end-of-run from %s after the priming pulses; "
                "cancelling", self.device,
            )
            self._cancel()
        self._set_state("idle")
        return True

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the board when the start request holds a triggerbox slice."""
        self._preview_armed = False
        if params is not None:
            self._arm(params, recording=True)

    def on_recording_stop(self, aborted: bool) -> None:
        self._link.send_cancel()  # on every end; an idle board ignores it
        self._set_state("idle")

    def on_preview_start(self, params: dict | None) -> None:
        """Arm the board until cancelled, with the recording's spec."""
        if params is None:
            return
        self._preview_armed = True
        self._arm(params, recording=False)

    def on_preview_stop(self) -> None:
        # Waits for the 'C': a recording's record grab starts next and must not
        # see a stray preview pulse.
        self._preview_armed = False
        self._cancel()
        self._set_state("idle")

    def on_ws_message(self, message: dict, client_id: int) -> bool:
        """Adopt a camera/light edit the tab pushes, so the preview arm and the
        timing follow the tab. A running preview is re-armed with it: a same-fps
        re-arm keeps the board's frame clock and takes effect at the next edge,
        so the exposing cameras never see a stray trigger."""
        if not isinstance(message, dict) or message.get("type") != "triggerbox_spec":
            return False
        spec = message.get("spec")
        if not isinstance(spec, dict):
            return True  # ours, but malformed
        new_cams = self._cameras_from_spec(spec)
        new_lights = self._lights_from_spec(spec)
        changed = new_cams != self._cameras or new_lights != self._lights
        self._cameras = new_cams
        self._lights = new_lights
        # The tab also pushes on redraws: re-arm only on a real change.
        if changed and self._preview_armed and self._link.is_open:
            self._arm(spec, recording=False)
        return True

    # -------------------------------------------------- arming

    def _arm(self, spec: dict, *, recording: bool) -> None:
        """Arm the board from a slice: a recording plans its finite train from
        the slice, a preview runs until cancelled with the same packet otherwise,
        so it strobes as the recording will."""
        subject = "external-triggered cameras" if recording else "preview"
        if not self._link.is_open:
            self.report_error(
                f"the board on {self.device} is not connected; {subject} will not "
                "be triggered (cameras wait for a trigger that never fires). "
                "Check the cable / reconnect the board."
            )
            return
        if not self.firmware_ok:
            self.report_error(
                f"the firmware on {self.device} ({self.banner!r}) is incompatible; "
                f"{subject} will not be triggered. Reflash arduino/triggerbox to "
                f"TRIGGERBOX {PROTOCOL_VERSION}."
            )
            return
        try:
            lights = self._lights_from_spec(spec)
            auto_led_on_us = None
            if any(lt.wants_auto() for lt in lights):
                auto_led_on_us = self._auto_led_on_us()
                if auto_led_on_us is None:
                    log.warning(
                        "triggerbox: auto strobe duty requested but no camera exposure "
                        "could be read; falling back to the manual duty percent"
                    )
            arm = self._resolve_arm(spec, lights, auto_led_on_us)
            if recording:  # the train trigger_train counts
                wanted = pulse_count(arm.fps, self._spec_duration_ms(spec))
                plan = plan_train(arm.fps, wanted, arm.cameras, arm.lights)
                arm = replace(arm, duration_ms=min(0xFFFF_FFFF, plan.duration_ms))
                if plan.count != wanted:
                    log.info(
                        "triggerbox: at %d fps the board's millisecond run clock "
                        "cannot end a train cleanly after pulse %d; arming %d pulses "
                        "instead (the recording counts %d)",
                        arm.fps, wanted, plan.count, plan.count,
                    )
                for warning in plan.warnings(arm.fps, arm.lights):
                    log.warning("triggerbox: %s", warning)
                what = f"{plan.count} pulses ({arm.duration_ms} ms)"
            else:
                what = "preview (until cancel)"
            arm.to_bytes()  # it must pack before arming is announced
        except Exception:
            log.exception("triggerbox: could not build arm packet; not arming")
            return
        log.info(
            "triggerbox: arming %d fps for %s — %d camera line(s), %d light channel(s)",
            arm.fps, what, len(arm.cameras), len(arm.lights),
        )
        self._send_arm(arm, subject)

    def _send_arm(self, arm: ArmSpec, subject: str) -> None:
        """Arm and report any failure. A failed write or a missing ack, never a
        reject, is a wedged USB link: one bus reset and re-arm."""
        result = self._link.arm(arm)
        if result in ("write_failed", "timeout"):
            what = (
                "the serial write failed" if result == "write_failed"
                else f"no acknowledgement within {ACK_TIMEOUT_S:.1f}s"
            )
            self.report_error(
                f"the board on {self.device} did not arm ({what}); {subject} "
                "will not be triggered. Attempting a USB reset…"
            )
            if self._recover_usb("the board stopped responding during arm"):
                log.info("triggerbox: re-arming %s after USB reset", self.device)
                result = self._link.arm(arm)
        if result == "reject":
            code = self._link.reject
            reason = REJECT_REASONS.get((code or "")[:1], "unknown")
            self.report_error(
                f"{self.device} REJECTED the arm (code {code!r}: {reason}); "
                "the board is not running — cameras will wait for a trigger that never fires"
            )
        elif result != "ok":
            self.report_error(
                f"the board on {self.device} still did not arm after a USB-reset "
                f"attempt — {subject} will wait for a trigger that never fires. "
                "Power-cycle or replug the board and check the cable."
            )

    def _cancel(self) -> None:
        if not self._link.cancel():
            log.warning("triggerbox: %s did not acknowledge the cancel", self.device)

    # -------------------------------------------------- web contributions

    def api_router(self):
        router = super().api_router()

        @router.get("/api/triggerbox/exposures")
        def get_exposures():
            """Live per-camera exposure timings for the tab's timing plot."""
            return {
                "guard_us": self._strobe_guard_us,
                "duty_auto_default": self._default_duty_auto,
                "cameras": [
                    {
                        "index": index,
                        "name": name,
                        "exposure_us": exposure,
                        "trigger_delay_us": delay,
                    }
                    for index, (name, delay, exposure) in enumerate(self._camera_windows())
                ],
            }

        return router
