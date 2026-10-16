"""2-photon rig hardware trigger plugin (opt-in).

An Arduino that waits for ThorSync's edge, then triggers the cameras at the
recording's fps. By default ("match ThorImage") it keeps going until ThorImage
stops -- its ThorSync input LOW for `stop_idle_ms` -- and the plugin then ends
the take, so no duration is set in octacam (`max_duration_s` is only a safety
ceiling). With `match_thorimage = false` (or the Record tab's "Synced with
ThorImage" unticked) the take runs for the set duration instead::

    [[plugins]]
    name = "twophoton"

    [plugins.options]
    device = "/dev/arduinoCams"  # a udev symlink, /dev/ttyACM0, COM3 or "auto"
    baud = 115200
    default_fps = 100            # for a start slice without one
    default_duration_ms = 10000
    match_thorimage = true       # end the take when ThorImage stops
    stop_idle_ms = 2000          # ThorSync input LOW this long = ThorImage stopped
    max_duration_s = 3600        # the take's safety ceiling while matching
    source = "/mnt/share/MD"     # the ThorImage/ThorSync share (ThorSync check)
    auto_flash = false           # headless: reflash a stale board without asking

Wire protocol, host -> Arduino (little-endian):
  [0xA5][fps u16][duration_ms u32][idle_ms u16]  arm (idle_ms 0: fixed duration)
  [0xCA]                                         cancel
  [0x3F] '?'                    identify: "2PHOTON <version> <build>" + newline
Arduino -> host: one byte 'A' armed (waiting for ThorSync), 'T' triggered, 'D'
done -- the 'D' preceded by a "STOP <I|C> <edges> <high_ms> <run_ms>" line (I:
ThorImage stopped, C: the duration ran out). The banner's build is checked and
flashed via `octacam.firmware`.

The take's 2P facts (armed, synced with ThorImage, the board's STOP report) go
to `octacam_recording/twophoton.json` when it ends; `octacam process` pairs the
take with its ThorImage/ThorSync data from there (`process`, set up by the
`source` option and the others in `settings`).
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from octacam import firmware as fw
from octacam.plugins.serial import SerialLink, SerialPlugin
from octacam.plugins.twophoton.layout import TAKE_INFO_FILENAME
from octacam.plugins.twophoton.process import TwoPhotonSteps

log = logging.getLogger("octacam")

DEFAULT_DEVICE = "/dev/arduinoCams"
DEFAULT_BAUD = 115200
DEFAULT_FPS = 100
DEFAULT_DURATION_MS = 10_000
# "Match ThorImage": the board ends the run once its ThorSync input has been LOW
# this long. Real ThorSync episodes: streaming/FastZ captures never go quiet for
# more than one frame (<= 130 ms), anatomical Z-stacks pause up to ~1.35 s
# between slices. The trailing camera frames are on ThorSync's DAQ too, so a
# short tail costs nothing.
DEFAULT_MATCH_THORIMAGE = True
DEFAULT_STOP_IDLE_MS = 2_000
# A shorter wait would end a take between two 2P frames.
MIN_STOP_IDLE_MS = 200
# The take's safety ceiling while matching ThorImage (seconds); only reached
# when the stop never comes.
DEFAULT_MAX_DURATION_S = 3_600.0

# The Mega 2560 resets when its port opens (DTR): its bootloader runs this long
# before the sketch answers an identify.
BOOT_SETTLE_S = 2.5

# How long an arm waits for the board's 'A' (sent within ms) before reporting it.
ACK_TIMEOUT_S = 1.0
# How long an arm waits for a ThorSync read still running (headless `record`
# arms right after startup began one).
THORSYNC_READ_WAIT_S = 5.0

_ARM_MAGIC = 0xA5
_CANCEL_MAGIC = 0xCA
_ARM_FORMAT = "<BHIH"

_STATE_LABELS = {"A": "armed", "T": "triggered", "D": "done"}

# Starts with '2', never a status byte, so the reader tells the two apart.
_EXPECTED_BANNER = "2PHOTON"
# v2: the arm carries idle_ms and a run ends with a STOP line. A v1 board would
# misparse the longer arm, so it is refused (WRONG_VERSION) until reflashed.
_PROTOCOL_VERSION = 2
_STOP_PREFIX = "STOP"
_NO_SOURCE_MSG = (
    "twophoton: no `source` option \N{EM DASH} `octacam process` will not pair "
    "takes with their ThorImage/ThorSync data"
)
_LEGACY_SETTINGS_MSG = (
    "twophoton: move [transfer.twophoton] into the plugin's [plugins.options] "
    "(it is still read for now)"
)
_STOP_REASONS = {"I": "thorimage", "C": "ceiling"}


def _as_bool(value, default: bool) -> bool:
    """A config or payload boolean that may arrive as a string ("false")."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_number[T: (int, float)](
    options: dict, key: str, kind: type[T], default: T
) -> T:
    try:
        return kind(options.get(key, default))
    except TypeError, ValueError:
        log.warning(
            "twophoton plugin: invalid %s %r; using %s", key, options[key], default
        )
        return default


def _fmt_duration(seconds: float) -> str:
    """320 -> "320 s", 3600 -> "60 min", 1000 -> "~16.7 min"."""
    if seconds >= 120 and seconds % 60 == 0:
        return f"{seconds / 60:g} min"
    if seconds < 600:
        return f"{seconds:.0f} s"
    return f"~{seconds / 60:.1f} min"


def thorsync_limit_message(
    folder: str, limit_s: float, longest_take_s: float, take_source: str | None = None
) -> str:
    """The ThorSync-too-short warning (GUI, arm log, doctor). *take_source* names
    where the take's length came from (the newest ThorImage acquisition's plan),
    else the safety limit is meant.
    """
    fmt = _fmt_duration
    take = (
        f"ThorImage is set to acquire {fmt(longest_take_s)} (read from {take_source})"
        if take_source
        else f"takes can run up to {fmt(longest_take_s)}"
    )
    return (
        f"ThorSync is set to stop after {fmt(limit_s)} (its acquisition duration, "
        f"as of its last recording, {folder}) but {take} \N{EM DASH} ThorSync "
        "would miss the rest of it (timing, AirFlow and every other channel). "
        "Raise the duration in ThorSync; this check sees it after ThorSync's next "
        "recording."
    )


def parse_stop_report(line: str) -> dict | None:
    """The firmware's "STOP <reason> <edges> <high_ms> <run_ms>" line as
    `{"reason": "thorimage"|"ceiling", "edges", "high_ms", "run_ms"}`, or None
    for a garbled one.
    """
    parts = line.split()
    if len(parts) != 5 or parts[0] != _STOP_PREFIX or parts[1] not in _STOP_REASONS:
        return None
    try:
        edges, high_ms, run_ms = (int(x) for x in parts[2:])
    except ValueError:
        return None
    return {
        "reason": _STOP_REASONS[parts[1]],
        "edges": edges,
        "high_ms": high_ms,
        "run_ms": run_ms,
    }


@dataclass
class ArmParams:
    fps: int
    duration_ms: int
    # >0: end the run once the ThorSync input has been LOW this long (match
    # ThorImage); 0: run for exactly duration_ms.
    idle_ms: int = 0

    def to_bytes(self) -> bytes:
        return struct.pack(
            _ARM_FORMAT, _ARM_MAGIC, self.fps, self.duration_ms, self.idle_ms
        )

    @classmethod
    def from_payload(
        cls, payload: dict, default_fps: int, default_duration_ms: int
    ) -> ArmParams:
        """From a start slice, each field falling back to its default."""
        try:
            fps = int(payload.get("fps", default_fps))
        except TypeError, ValueError:
            fps = default_fps
        try:
            duration_ms = int(payload.get("duration_ms", default_duration_ms))
        except TypeError, ValueError:
            duration_ms = default_duration_ms
        fps = max(1, min(10_000, fps))
        # Within the wire field: a struct.error in arm() would skip the arm.
        duration_ms = max(1, min(0xFFFF_FFFF, duration_ms))
        return cls(fps=fps, duration_ms=duration_ms)


class TwoPhotonLink(SerialLink):
    """The serial link to the 2-photon trigger: bare status bytes, plus two
    newline-terminated lines -- the banner in reply to identify, and the STOP
    report that ends a run.
    """

    name = "twophoton"
    banner_prefix = _EXPECTED_BANNER

    def __init__(
        self,
        on_status: Callable[[str], None],
        on_broken: Callable[[], None],
        on_stop_report: Callable[[dict], None],
    ):
        super().__init__(on_broken)
        self._on_status = on_status
        self._on_stop_report = on_stop_report
        self._armed = threading.Event()  # set by the board's 'A'

    def arm(self, params: ArmParams) -> str:
        """Send an arm: `"ok"` ('A' within ACK_TIMEOUT_S), `"timeout"` or
        `"write_failed"`.
        """
        self._armed.clear()
        if not self._write(params.to_bytes()):
            return "write_failed"
        return "ok" if self._armed.wait(ACK_TIMEOUT_S) else "timeout"

    def send_cancel(self) -> None:
        self._write(bytes([_CANCEL_MAGIC]))

    def identify(self, timeout: float = 0.5) -> str | None:
        """The banner, asking once more after the Mega's boot: opening its port
        resets it (DTR), and its bootloader swallows a query sent in the first
        ~2 s. Asking repeatedly would only extend the bootloader's wait.
        """
        banner = super().identify(timeout)
        if banner is None:
            time.sleep(BOOT_SETTLE_S)
            banner = super().identify(timeout)
        return banner

    def _feed(self, chunk: bytes) -> None:
        # Neither line starts with a status byte, so a status byte with an empty
        # buffer is a status; anything else builds a line.
        for byte in chunk:
            if byte == 0x0A:
                line = self._buf.decode("ascii", "replace").strip()
                self._buf.clear()
                if not self._identified(line) and line.startswith(_STOP_PREFIX):
                    report = parse_stop_report(line)
                    if report is not None:
                        self._dispatch(self._on_stop_report, report)
            elif self._buf:
                self._buf.append(byte)
                if len(self._buf) > 64:  # a runaway or garbled line
                    self._buf.clear()
            elif chr(byte) in _STATE_LABELS:
                if byte == ord("A"):
                    self._armed.set()
                self._dispatch(self._on_status, chr(byte))
            else:
                self._buf.append(byte)  # the start of a line


class TwoPhotonPlugin(SerialPlugin[TwoPhotonLink]):
    """Arms the 2-photon trigger with the recording's fps, and ends a take synced
    with ThorImage when ThorImage stops.
    """

    name = "twophoton"
    web_dir = Path(__file__).parent / "web"
    firmware = fw.FirmwareSpec(
        name="twophoton",
        sketch_dir=fw.resolve_sketch_dir("2photon_trigger"),
        fqbn="arduino:avr:mega",  # Arduino Mega 2560
        banner_prefix=_EXPECTED_BANNER,
        protocol_version=_PROTOCOL_VERSION,
        build_define="TWOPHOTON_FW_BUILD",
    )
    default_device = DEFAULT_DEVICE
    reconnect_path = "/api/twophoton/reconnect"
    state_topic = "twophoton_state"
    process_steps = TwoPhotonSteps
    # Each user's own 2P folder on the share ([users.MD.twophoton] source).
    user_options: ClassVar[dict[str, str]] = {
        "source": "Your 2P data on the share (ThorImage/ThorSync)"
    }

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        baud: int = DEFAULT_BAUD,
        default_fps: int = DEFAULT_FPS,
        default_duration_ms: int = DEFAULT_DURATION_MS,
        auto_flash: bool = False,
        match_thorimage: bool = DEFAULT_MATCH_THORIMAGE,
        stop_idle_ms: int = DEFAULT_STOP_IDLE_MS,
        max_duration_s: float = DEFAULT_MAX_DURATION_S,
        source: str = "",
    ):
        super().__init__(
            TwoPhotonLink(self._on_status, self._on_link_broken, self._on_stop_report),
            device=device,
            baud=baud,
            auto_flash=bool(auto_flash),
        )
        self._default_fps = default_fps
        self._default_duration_ms = default_duration_ms
        self.match_thorimage = bool(match_thorimage)
        if int(stop_idle_ms) < MIN_STOP_IDLE_MS:
            log.warning(
                "twophoton: stop_idle_ms=%s is shorter than ThorImage's own frame "
                "gaps allow; using %d ms",
                stop_idle_ms,
                MIN_STOP_IDLE_MS,
            )
        # Within the uint16 wire field; 0 would mean "fixed duration".
        self.stop_idle_ms = max(MIN_STOP_IDLE_MS, min(0xFFFF, int(stop_idle_ms)))
        self.max_duration_s = max(1.0, float(max_duration_s))
        self.source = source
        self._configured_source = source  # the rig's, when the user sets none
        # The take in progress (or the last one): its folder, whether it was
        # armed and synced with ThorImage, and the board's STOP report for it.
        self._take_dir: Path | None = None
        self._take_armed = False
        self._take_synced = False
        self._stop_report: dict | None = None
        # ThorSync's duration limit as last read from the share, off-thread: the
        # state pushes run on the serial reader, which must never wait on it.
        self.thorsync: dict | None = None
        self._thorsync_lock = threading.Lock()
        self._thorsync_thread: threading.Thread | None = None
        self._thorsync_stale = False

    @classmethod
    def from_options(cls, options: dict) -> TwoPhotonPlugin:
        return cls(
            device=str(options.get("device") or DEFAULT_DEVICE),
            baud=_as_number(options, "baud", int, DEFAULT_BAUD),
            default_fps=_as_number(options, "default_fps", int, DEFAULT_FPS),
            default_duration_ms=_as_number(
                options, "default_duration_ms", int, DEFAULT_DURATION_MS
            ),
            auto_flash=_as_bool(options.get("auto_flash"), False),
            match_thorimage=_as_bool(
                options.get("match_thorimage"), DEFAULT_MATCH_THORIMAGE
            ),
            stop_idle_ms=_as_number(options, "stop_idle_ms", int, DEFAULT_STOP_IDLE_MS),
            max_duration_s=_as_number(
                options, "max_duration_s", float, DEFAULT_MAX_DURATION_S
            ),
            source=str(options.get("source") or ""),
        )

    def on_user(self, options: dict) -> None:
        """The chosen user's 2P share folder (else the rig's); the ThorSync
        check then reads theirs.
        """
        source = str(options.get("source") or self._configured_source)
        if source != self.source:
            self.source = source
            self.thorsync = None
            self.refresh_thorsync_limit()
            self._push_state()

    def snapshot_options(self, params: dict | None) -> dict | None:
        """A recording made for a user keeps their share in its snapshot, so
        `octacam process` looks for its 2P data there.
        """
        return (
            {"source": self.source} if self.source != self._configured_source else None
        )

    def busy_reason(self) -> str | None:
        if self.board_state in ("armed", "triggered"):
            return (
                "refusing to flash while the trigger is armed/running \N{EM DASH} "
                "stop the recording first"
            )
        return None

    def setup(self) -> None:
        super().setup()
        self.refresh_thorsync_limit()

    def _identify(self) -> None:
        """As the base, but a board that does not identify itself is not armed:
        a silent protocol-v1 board would read the 9-byte arm's extra bytes as
        commands (cancel or re-arm).
        """
        super()._identify()
        if self.banner is None and self._link.is_open:
            self.firmware_ok = False
            self.last_error = (
                f"the board on {self.device} did not identify itself \N{EM DASH} not "
                "arming it (an old protocol-v1 board would misread the arm packet); "
                "reconnect, or reflash with `octacam flash`"
            )

    def link_status(self) -> dict:
        """The base fields plus the "match ThorImage" config the tab seeds its
        checkbox from, and ThorSync's duration limit as last read from the share.
        """
        return {
            **super().link_status(),
            "match_thorimage": self.match_thorimage,
            "max_duration_s": self.max_duration_s,
            "stop_idle_ms": self.stop_idle_ms,
            "thorsync": self.thorsync,
        }

    def _on_status(self, status: str) -> None:
        state = _STATE_LABELS[status]
        if state == "armed":
            self.last_error = None
        # A synced take ends with the board's 'D' (ThorImage stopped, or the
        # ceiling). Only a 'D' ending a run seen to trigger counts: a stale byte
        # after on_recording_stop reset the state must not end a later take.
        ends_take = (
            state == "done" and self._take_synced and self.board_state == "triggered"
        )
        self._set_state(state)
        controller = self.controller
        if ends_take and controller is not None:
            log.info("twophoton: ThorImage stopped \N{EM DASH} ending the take")
            # Never on the reader: the controller lock can be held for seconds
            # by a camera operation, and the reader must keep draining the board.
            threading.Thread(
                target=controller.finish_recording,
                args=(self.name,),
                name="twophoton-finish",
                daemon=True,
            ).start()

    def _on_stop_report(self, report: dict) -> None:
        """The board's end-of-run report (just before 'D'). Only a run seen to
        trigger owns it, so a late one cannot land in the next take's file.
        """
        if self.board_state != "triggered":
            log.info(
                "twophoton: ignoring an end-of-run report outside a run: %s", report
            )
            return
        self._stop_report = report
        log.info(
            "twophoton: run ended (%s) after %.1f s \N{EM DASH} the input saw %d "
            "rising edge(s), HIGH for %.1f s",
            "ThorImage stopped" if report["reason"] == "thorimage" else "ceiling",
            report["run_ms"] / 1000,
            report["edges"],
            report["high_ms"] / 1000,
        )

    def _on_link_broken(self) -> None:
        # Pushes ready=False: the GUI stops offering to arm a dead link.
        self._set_state("idle")

    def teardown(self) -> None:
        self._link.send_cancel()
        self._link.close()
        self.board_state = "idle"

    # ---------------------------------------------- ThorSync duration check

    def _source_dir(self) -> Path | None:
        if not self.source:
            return None
        from octacam.config import resolve_dir_template

        return Path(resolve_dir_template(self.source))

    def refresh_thorsync_limit(self, *, wait: bool = False) -> None:
        """Re-read ThorSync's duration limit (newest SyncData folder on the
        share) and ThorImage's plan, on a thread, then push the state. One reader
        at a time: a hung share must not pile up threads; a request during a
        read makes it read once more.
        """
        if self._source_dir() is None:
            return
        with self._thorsync_lock:
            self._thorsync_stale = True
            thread = self._thorsync_thread
            if thread is None or not thread.is_alive():
                thread = threading.Thread(
                    target=self._read_thorsync, name="twophoton-thorsync", daemon=True
                )
                self._thorsync_thread = thread
                thread.start()
        if wait:
            thread.join(10)

    def _read_thorsync(self) -> None:
        from octacam.plugins.twophoton.matching import latest_thorimage_plan
        from octacam.plugins.twophoton.signals import latest_thorsync_limit

        while True:
            with self._thorsync_lock:
                if not self._thorsync_stale:
                    self._thorsync_thread = None
                    return
                self._thorsync_stale = False
            source = self._source_dir()
            latest = latest_thorsync_limit(source) if source else None
            plan = latest_thorimage_plan(source) if source else None
            self.thorsync = (
                {
                    "folder": f"{latest[0].parent.name}/{latest[0].name}",
                    "limit_s": latest[1],
                    # What ThorImage is set to record (a synced take lasts that).
                    "planned_folder": (
                        f"{plan[0].parent.name}/{plan[0].name}" if plan else None
                    ),
                    "planned_s": plan[1] if plan else None,
                }
                if latest
                else None
            )
            self._push_state()

    def thorsync_warning(
        self, longest_take_s: float, *, synced: bool = False
    ) -> str | None:
        """The warning when ThorSync would stop before the take ends: every
        ThorSync channel (timing, AirFlow, ...) is lost for the rest of it (real
        2026-10-14: a 320 s limit, 394 s takes). A *synced* take lasts as long
        as ThorImage records: its newest plan when known, not the ceiling.
        """
        info = self.thorsync
        limit = (info or {}).get("limit_s")
        if not info or not limit:
            return None
        source = None
        if synced and info.get("planned_s"):
            longest_take_s, source = info["planned_s"], info["planned_folder"]
        if limit >= longest_take_s:
            return None
        return thorsync_limit_message(info["folder"], limit, longest_take_s, source)

    # -------------------------------------------------- recording lifecycle

    def _wants_match(self, params: dict) -> bool:
        """Whether a start slice asks to stop with ThorImage (the Record tab's
        checkbox); one without the key follows the configured default.
        """
        return _as_bool(params.get("match_thorimage"), self.match_thorimage)

    def default_start_params(
        self, fps: float, duration_s: float, *, fixed_duration: bool = False
    ) -> dict:
        """The arm slice for headless `octacam record`, without which the board
        is never armed and the cameras wait forever. An explicit `--duration`
        (*fixed_duration*) means exactly that long, not until ThorImage stops.
        """
        return {
            "fps": round(fps),
            "duration_ms": max(1, round(duration_s * 1000)),
            "match_thorimage": self.match_thorimage and not fixed_duration,
        }

    def take_ceiling_s(self, params: dict | None) -> float | None:
        """A synced take ends when ThorImage stops (`_on_status`); its deadline
        is only the safety ceiling.
        """
        if params is None or not self._wants_match(params):
            return None
        return self.max_duration_s

    def on_recording_start(self, params: dict | None) -> None:
        """Arm the board when the start request holds a twophoton slice (the
        tab's "Arm with recording"); a missing fps or duration takes its default.
        A synced take runs until ThorImage stops, up to `max_duration_s`.
        """
        controller = self.controller
        self._take_dir = (
            Path(controller.get_settings().save_dir) if controller else None
        )
        self._take_armed = params is not None
        self._take_synced = params is not None and self._wants_match(params)
        self._stop_report = None
        if params is None:
            return
        arm = ArmParams.from_payload(
            params, self._default_fps, self._default_duration_ms
        )
        if self._take_synced:
            arm.duration_ms = round(self.max_duration_s * 1000)
            arm.idle_ms = self.stop_idle_ms
        if not self._link.is_open:
            log.warning(
                "twophoton: link to %s is not open; recording will NOT be "
                "hardware-armed (cameras may wait for a trigger that never fires)",
                self.device,
            )
            return
        if not self.firmware_ok:
            log.error(
                "twophoton: firmware on %s (%r) is incompatible; refusing to "
                "arm \N{EM DASH} reflash with `octacam flash` or the Flash firmware "
                "button",
                self.device,
                self.banner,
            )
            return
        reading = self._thorsync_thread
        if self.thorsync is None and reading is not None:
            reading.join(THORSYNC_READ_WAIT_S)
        warning = self.thorsync_warning(
            arm.duration_ms / 1000, synced=self._take_synced
        )
        if warning and controller is not None:
            controller.event("warning", warning)
        elif warning:
            log.warning("twophoton: %s", warning)
        if self._take_synced:
            log.info(
                "twophoton: arming at %d fps until ThorImage stops (input idle "
                "%d ms; ceiling %d s)",
                arm.fps,
                arm.idle_ms,
                arm.duration_ms // 1000,
            )
        else:
            log.info("twophoton: arming at %d fps for %d ms", arm.fps, arm.duration_ms)
        result = self._link.arm(arm)
        # Shown in the GUI too, since its checkbox still reads "armed"; a
        # dropped or garbled packet is otherwise silent.
        if result == "write_failed":
            self.report_error(f"arm write to {self.device} failed")
        elif result == "timeout":
            self.report_error(
                f"no arm acknowledgement from {self.device} within "
                f"{ACK_TIMEOUT_S:.1f}s; "
                "the board may not have armed (cameras could wait for a trigger "
                "that never fires)"
            )

    def on_recording_stop(self, aborted: bool) -> None:
        # Cancel on every stop: a manual stop (aborted=False) can leave the board
        # running, and an idle board ignores it. The firmware goes idle silently,
        # so the state is reset here.
        self._link.send_cancel()
        self._set_state("idle")
        self._write_take_info()
        # The take's own SyncData folder is now the newest: re-read its settings.
        self.refresh_thorsync_limit()

    def take_info(self) -> dict:
        """The take's 2P facts for `process`: whether it was armed (the
        operator's intent, not the handshake: it says whether to look for its 2P
        data at all), synced with ThorImage, and the board's STOP report (absent
        when octacam ended the run: a manual stop or abort).
        """
        info: dict = {"armed": self._take_armed, "match_thorimage": self._take_synced}
        if self._stop_report is not None:
            info["stop"] = self._stop_report
        return info

    def _write_take_info(self) -> None:
        from octacam.recording_format import RECORDING_INFO_DIRNAME

        take, self._take_dir = self._take_dir, None
        if take is None or not take.is_dir():
            return
        path = take / RECORDING_INFO_DIRNAME / TAKE_INFO_FILENAME
        try:
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(self.take_info(), indent=2) + "\n")
        except OSError as e:
            log.error("twophoton: could not write %s: %s", path, e)

    # ------------------------------------------------------------ doctor

    @classmethod
    def doctor(
        cls, options: dict, config, config_dir: Path | None
    ) -> list[tuple[str, str]]:
        """The 2P checks: a share to pair takes with, reachable, and ThorSync
        set to record as long as a take can run (it silently stops at its own
        acquisition duration: real 2026-10-14, 320 s against 394 s takes).
        """
        from octacam.config import duration_to_seconds
        from octacam.plugins.twophoton.matching import latest_thorimage_plan
        from octacam.plugins.twophoton.settings import settings_for
        from octacam.plugins.twophoton.signals import latest_thorsync_limit

        config_file = config_dir / "octacam_config.toml" if config_dir else None
        settings = settings_for(config, config_file)
        if settings is not None and options.get("source"):
            # The chosen user's share (the GUI's Ready? strip passes theirs).
            settings = settings.model_copy(update={"source": str(options["source"])})
        moved: list[tuple[str, str]] = []
        if settings is not None and not options.get("source"):
            moved.append(("warn", _LEGACY_SETTINGS_MSG))
        if settings is None or settings.source_root is None:
            return [("warn", _NO_SOURCE_MSG)]
        source = settings.source_root
        if not source.is_dir():
            return [
                *moved,
                ("warn", f"twophoton: the 2P share {source} is not reachable"),
            ]
        checks = [*moved, ("ok", f"twophoton: 2P share {source}")]
        latest = latest_thorsync_limit(source)
        if latest is None:
            return [
                *checks,
                ("info", f"ThorSync duration: no SyncData folder under {source}"),
            ]
        folder, limit = latest
        label = f"{folder.parent.name}/{folder.name}"
        if not limit:
            return [*checks, ("info", f"ThorSync duration: not readable from {label}")]
        take_source = None
        if not _as_bool(options.get("match_thorimage"), DEFAULT_MATCH_THORIMAGE):
            record = config.record
            longest = duration_to_seconds(
                record.duration, record.duration_unit, record.fps
            )
        elif (plan := latest_thorimage_plan(source)) is not None:
            longest, take_source = plan[1], f"{plan[0].parent.name}/{plan[0].name}"
        else:
            longest = _as_number(
                options, "max_duration_s", float, DEFAULT_MAX_DURATION_S
            )
        if limit < longest:
            return [
                *checks,
                ("warn", thorsync_limit_message(label, limit, longest, take_source)),
            ]
        return [
            *checks,
            (
                "ok",
                f"ThorSync duration {limit:g} s (from {label}) covers the longest take",
            ),
        ]
