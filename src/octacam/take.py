"""One recording: its start sequence, its monitor phases, its teardown and the
files it leaves.

`RecordingController` admits a recording and drives a
`Take` through it: the camera start (under the controller lock), the
start sequence (priming, counting, the arm: off the lock, ending with
`hooks_done`), the first-frame wait, the countdown, and the teardown in its
fixed order: trigger off -> grab loops exit -> writers drain -> sync check ->
summary.
"""

import contextlib
import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import numpy as np

from octacam import config_writer, session_cache
from octacam.cameras import CameraSystem
from octacam.cameras.take import CameraStats, CameraTake
from octacam.config import RecordingSettings
from octacam.plugins.base import PluginManager
from octacam.pulses import PulseClock
from octacam.recording_format import (
    CONFIG_SNAPSHOT_FILENAME,
    RECORDING_INFO_DIRNAME,
    TIMESTAMPS_FILENAME,
    build_recording_summary,
    build_timestamps_arrays,
    write_summary,
    write_timestamps,
)
from octacam.writer import resolve_capture_formats

log = logging.getLogger("octacam")

STARTED_POLL_INTERVAL_S = 0.1
STARTED_WARN_AFTER_S = 3.0
STARTED_FAIL_AFTER_S = 10.0  # then record with whatever cameras started
STOP_GRACE_S = 0.5  # past the duration, for frames still in flight
# Bounds the monitor's wait for the start hooks, so a wedged arm (a serial write
# stalled on its write_timeout) never blocks teardown; a primed recording adds
# its priming (start_sequence_timeout_s).
START_HOOKS_TIMEOUT_S = 5.0
# Sacrificial triggers per priming round (octacam-driven trigger sources only):
# a GS3 ignores its first triggers after an acquisition start (CLAUDE.md,
# hardware quirks). Their frames are discarded.
PRIME_PULSES = 4
# Priming repeats rounds until every camera has answered one, but starts none
# after this long; a camera still silent then is warned about.
PRIME_BUDGET_S = 1.0
# Settle after a priming round before counting starts: above a frame's
# trigger-to-delivery latency and, at a low fps, PRIME_SETTLE_PERIODS periods (a
# long exposure delivers late; a priming straggler is dropped only within a few
# periods of the last primed frame).
PRIME_SETTLE_S = 0.15
PRIME_SETTLE_PERIODS = 4
# A counted train is over this long after its last pulse was due (a GS3
# delivers ~10-30 ms after its pulse); the take then stops on the train's end.
TRAIN_END_MARGIN_S = 0.3


def capture_frame_count(settings: RecordingSettings) -> int | None:
    """`round(fps * duration)`, the pulses an octacam-driven trigger emits, so
    no camera's grab loop takes a trailing pulse the others miss at teardown.

    None (uncapped, bounded by the deadline) for an `external` trigger, whose
    pulse count is unknown, and for a non-positive fps or duration.
    """
    if settings.trigger_source == "external":
        return None
    if settings.fps <= 0 or settings.duration_s <= 0:
        return None
    return max(1, round(settings.fps * settings.duration_s))


def resolve_pulse_clock(
    settings: RecordingSettings,
    plugins: PluginManager | None = None,
    plugin_params: dict | None = None,
) -> PulseClock:
    """The trigger train a recording's frames are counted against.

    `managed`: the train the driving plugin will emit (`trigger_train`), else
    one derived from the settings; `software`: octacam's own timer. Both are
    filled (a missed pulse repeats the previous frame, so video frame k is pulse
    k in every camera). `external` has no known length and is never filled: an
    external clock may be irregular by design.
    """
    period = round(1e9 / settings.fps) if settings.fps > 0 else 0
    if settings.trigger_source == "external":
        return PulseClock(period, None, "external", fill=False)
    if settings.trigger_source == "managed" and plugins is not None:
        train = plugins.trigger_train(plugin_params)
        if train:
            return PulseClock(int(train["period_ns"]), int(train["count"]), "managed")
    return PulseClock(period, capture_frame_count(settings), settings.trigger_source)


def prime_settle_s(period_ns: int) -> float:
    """How long priming waits after a round for its frames to land: at least
    PRIME_SETTLE_S, and PRIME_SETTLE_PERIODS periods at a low fps.
    """
    return max(PRIME_SETTLE_S, PRIME_SETTLE_PERIODS * period_ns / 1e9)


def start_sequence_timeout_s(period_ns: int, primed: bool) -> float:
    """How long the monitor waits for a recording's start sequence (priming,
    then the arm): START_HOOKS_TIMEOUT_S plus, when primed, the priming's upper
    bound (rounds start until PRIME_BUDGET_S, and the last sends PRIME_PULSES a
    period apart and settles; at 1 fps that round alone is 8 s).
    """
    if not primed:
        return START_HOOKS_TIMEOUT_S
    priming = (
        PRIME_BUDGET_S + PRIME_PULSES * period_ns / 1e9 + prime_settle_s(period_ns)
    )
    return START_HOOKS_TIMEOUT_S + priming


# ------------------------------------------------------------ sync check


class DeliveryProfile(NamedTuple):
    """What a frame's trigger-to-host delay depends on (see `check_sync`)."""

    backend: str
    model: str | None
    width: int
    height: int
    pixel_format: str
    exposure_us: int


def read_delivery_profiles(system: CameraSystem) -> dict[str, DeliveryProfile | None]:
    """Each camera's `DeliveryProfile`, by serial, for `check_sync`.

    Read before the record grab, never at teardown (the Camera tab is unlocked
    again by then). A camera whose exposure or size cannot be read gets None and
    is compared with no other.
    """

    def profile(camera) -> DeliveryProfile | None:
        try:
            exposure = float(camera.read_param("exposure")["value"])
            width = int(camera.read_param("width")["value"])
            height = int(camera.read_param("height")["value"])
        except Exception as e:
            log.debug("Could not read the delivery profile of %s: %s", camera.name, e)
            return None
        try:
            model = camera.read_feature("DeviceModelName")["value"]
        except Exception:
            model = None
        # Same-model cameras snap a configured exposure identically, so whole
        # microseconds only absorb float noise.
        return DeliveryProfile(
            type(camera.backend).__name__,
            model,
            width,
            height,
            camera.pixel_format,
            round(exposure),
        )

    try:
        results = system.apply_to_all(profile)
    except Exception:
        log.exception("Could not read the cameras' delivery profiles")
        return {}
    return {
        camera.serial_number: result
        for camera, result in zip(system, results, strict=True)
    }


# The profile fields an operator can act on, and how the sync note shows each.
_PROFILE_FIELDS = {
    "camera backend": lambda p: p.backend,
    "model": lambda p: p.model or "unknown",
    "frame size": lambda p: f"{p.width}\N{MULTIPLICATION SIGN}{p.height}",
    "pixel format": lambda p: p.pixel_format,
    "exposure": lambda p: f"{p.exposure_us} \N{MICRO SIGN}s",
}


def _unlike_profiles_note(groups: dict[DeliveryProfile | str, list[str]]) -> str:
    """The note for cameras whose start alignment could not be compared.

    *groups* maps a delivery profile -- or the name of a camera whose profile
    could not be read -- to the cameras that share it. The note names only the
    fields that differ, so an operator can tell a deliberate difference (two
    ROIs) from an accidental one (a mistyped exposure).
    """

    def label(members: list[str]) -> str:
        return members[0] if len(members) == 1 else f"[{', '.join(members)}]"

    read = [
        (key, members)
        for key, members in groups.items()
        if isinstance(key, DeliveryProfile)
    ]
    unread = [key for key in groups if isinstance(key, str)]
    differences = []
    for field, show in _PROFILE_FIELDS.items():
        values = [(members, show(key)) for key, members in read]
        if len({value for _members, value in values}) > 1:
            shown = ", ".join(f"{label(members)} {value}" for members, value in values)
            differences.append(f"{field} ({shown})")
    reasons = []
    if differences:
        reasons.append("they differ in " + "; ".join(differences))
    if unread:
        reasons.append(f"the delivery profile of {', '.join(unread)} could not be read")
    listing = " vs ".join(f"[{', '.join(members)}]" for members in groups.values())
    return (
        f"Start alignment of {listing} was not checked (informational, not an "
        f"error): {' and '.join(reasons)}, so their frames reach the host after "
        "different delays. Only cameras alike in model, frame size, pixel format "
        "and exposure are compared."
    )


def check_sync(
    stats: list[CameraStats],
    recording: list[str],
    clock: PulseClock,
    profiles: dict[str, DeliveryProfile | None],
    completed: bool,
) -> dict:
    """Whether frame k is the same trigger pulse in every camera, and why not.

    `recording` names the cameras whose record grab started, `profiles` maps
    serials to delivery profiles. Fills keep the cameras aligned, so missed
    pulses and writer drops are reported without breaking sync. What breaks it:
    a camera that did not start or recorded no frame, frames off the trigger
    clock or without timestamps, unfilled misses (external trigger), writer
    skips, unequal ends after a completed train, and a camera that started late.

    A late start shows in when the first frames reached the host, which is
    comparable only between cameras of one delivery profile (a 2048^2 GS3 lands
    ~4.5 ms after an acA1920): those deliver a pulse within ~0.5 ms of each
    other, so a pulse late is a whole period. Across profiles a note says the
    start was not checked.
    """
    warnings: list[str] = []
    notes: list[str] = []
    offsets: dict[str, int | None] = {}
    ok = True
    not_started = [c.name for c in stats if c.name not in recording]
    if not_started:
        ok = False
        warnings.append(
            f"Camera(s) {', '.join(not_started)} did not start recording: the take "
            "has no video from them"
        )
    silent = [c.name for c in stats if c.name in recording and not c.frames]
    if silent:
        ok = False
        warnings.append(
            f"Camera(s) {', '.join(silent)} recorded no frame: there is no video "
            "to align with the other cameras"
        )
    cams = [c for c in stats if c.frames]
    for camera in cams:
        name = camera.name
        missed = camera.missed_pulses
        if missed:
            shown = ", ".join(str(p) for p in missed[:10])
            more = f" and {len(missed) - 10} more" if len(missed) > 10 else ""
            if clock.fill:
                warnings.append(
                    f"Camera {name} missed {len(missed)} trigger pulse(s) "
                    f"({shown}{more}); each was filled with the previous frame"
                )
            else:
                ok = False
                warnings.append(
                    f"Camera {name} missed {len(missed)} trigger pulse(s) "
                    f"({shown}{more}); on an external trigger they are not "
                    "filled \N{EM DASH} map frames to pulses with pulse_index in "
                    f"{TIMESTAMPS_FILENAME}"
                )
        if camera.writer_dropped:
            warnings.append(
                f"Camera {name}: {camera.writer_dropped} frame(s) arrived while "
                "the writer queue was full and were filled with the previous "
                "frame (the encoder could not keep up)"
            )
        if camera.writer_skipped:
            ok = False
            warnings.append(
                f"Camera {name}: {len(camera.writer_skipped)} frame(s) the writer "
                "could not accept were skipped, not filled (the encoder or disk "
                "could not keep up), so video frame k is no longer pulse k; map "
                f"frames to pulses with pulse_index in {TIMESTAMPS_FILENAME}"
            )
        if camera.extra_frames:
            warnings.append(
                f"Camera {name}: {camera.extra_frames} frame(s) belonged to no "
                "pulse of the train and were discarded"
            )
        if camera.clock_mismatch:
            ok = False
            warnings.append(
                f"Camera {name}: its frames did not follow the trigger clock's "
                "period (is it free-running? A trigger pulse longer than the "
                "exposure re-triggers some cameras at their readout limit)"
            )
        if camera.unclocked_frames:
            ok = False
            real_frames = camera.frames - len(camera.dropped_indices)
            if camera.unclocked_frames < real_frames:
                # A stray frame without a timestamp on a clocked camera: it was
                # placed as the next pulse, so only a miss right before it could
                # go unseen.
                warnings.append(
                    f"Camera {name}: {camera.unclocked_frames} frame(s) had no "
                    "hardware timestamp and were placed as the next pulse; a "
                    "pulse missed just before one of them cannot be detected"
                )
            else:
                warnings.append(
                    f"Camera {name}: {camera.unclocked_frames} frame(s) had no "
                    "hardware timestamp, so missed pulses cannot be detected"
                )
        for glitch in camera.timestamp_glitches:
            warnings.append(
                f"Camera {name}: its hardware clock jumped by "
                f"{glitch['jump_ns'] / 1e9:+.6f} s at pulse {glitch['pulse']} "
                "(corrected; not a lost frame)"
            )
    if completed and clock.fill and clock.count:
        short = [c.name for c in cams if c.frames != clock.count]
        if short:
            ok = False
            warnings.append(
                f"Camera(s) {', '.join(short)} did not end on the train's last "
                f"pulse ({clock.count} expected)"
            )
    period = clock.period_ns
    if clock.source == "software":
        # Frame 0 answers trigger 0 in every camera by construction; a camera
        # that merely delivers later must not read as late.
        for camera in cams:
            offsets[camera.name] = 0
        return {
            "ok": ok,
            "warnings": warnings,
            "notes": notes,
            "start_offsets": offsets,
        }
    delays: dict[str, float] = {}
    for camera in cams:
        rows = [
            (arrival, pulse)
            for arrival, pulse in zip(
                camera.arrival_ns, camera.pulse_index, strict=False
            )
            if arrival
        ][:16]
        if period and len(rows) >= 4:
            delays[camera.name] = float(
                np.median([arrival - pulse * period for arrival, pulse in rows])
            )
    # Group the cameras by delivery profile; one whose profile could not be read
    # is a group of its own, under its name, and compared with none.
    groups: dict[DeliveryProfile | str, list[str]] = {}
    for camera in cams:
        if camera.name in delays:
            profile = profiles.get(camera.serial)
            key = profile if profile is not None else camera.name
            groups.setdefault(key, []).append(camera.name)
    for members in groups.values():
        if len(members) < 2:
            continue
        earliest = min(delays[name] for name in members)
        for name in members:
            pulses = (delays[name] - earliest) / period
            nearest = round(pulses)
            if abs(pulses - nearest) > 0.25:
                offsets[name] = None
                ok = False
                warnings.append(
                    f"Camera {name}: could not verify that its first frame is "
                    f"the others' first pulse (first frames arrived "
                    f"{pulses:+.2f} periods from the earliest like camera's)"
                )
            else:
                offsets[name] = nearest
                if nearest:
                    ok = False
                    warnings.append(
                        f"Camera {name} started {nearest} pulse(s) late: its "
                        f"frame i shows the moment of the other cameras' frame "
                        f"i+{nearest} (it missed the train's first pulse(s))"
                    )
    if len(groups) > 1:
        notes.append(_unlike_profiles_note(groups))
    return {"ok": ok, "warnings": warnings, "notes": notes, "start_offsets": offsets}


# -------------------------------------------------------- config snapshot


def snapshot_source(config_dir: Path | None, save_dir: str) -> Path | None:
    """The rig config file a recording into `save_dir` snapshots, or None: no
    config dir or file, or a rig relaunched from that recording, which must not
    rewrite its own config.
    """
    if config_dir is None:
        return None
    src = config_dir / CONFIG_SNAPSHOT_FILENAME
    dst = Path(save_dir) / RECORDING_INFO_DIRNAME / CONFIG_SNAPSHOT_FILENAME
    if not src.exists() or src.resolve() == dst.resolve():
        return None
    return src


def export_camera_params(
    system: CameraSystem, config_dir: Path | None, save_dir: str
) -> dict[str, str]:
    """Each camera's current parameter text (unsaved Camera-tab edits included),
    for the config snapshot of a recording into `save_dir`; {} when there is
    none. A camera that cannot be read is left out.
    """
    if snapshot_source(config_dir, save_dir) is None:
        return {}
    try:
        params = system.save_all_params()
    except Exception:
        log.exception("Could not read the camera parameters for the config snapshot")
        return {}
    missing = [c.name for c in system if c.serial_number not in params]
    if missing:
        log.warning(
            "Could not read the parameters of camera(s) %s; their settings "
            "are not saved with the recording",
            ", ".join(missing),
        )
    return params


# ------------------------------------------------------------------ take


class Take:
    """One recording, from its camera start to the files it leaves.

    The controller drives it: `start_cameras` and `write_start_files`
    under its lock, `start_sequence` off it, then on the monitor thread
    `wait_for_first_frames`, `begin_countdown`, `countdown`,
    `end_capture` and `teardown`. `hooks_done` is set once the
    start sequence returns, however it ends: every later plugin hook and the
    teardown wait on it, so a stop never overtakes the arm or the software
    timer's start.
    """

    def __init__(
        self,
        system: CameraSystem,
        settings: RecordingSettings,
        plugins: PluginManager,
        plugin_params: dict | None,
        *,
        profiles: dict[str, DeliveryProfile | None],
        event: Callable[[str, str], None],
        config_dir: Path | None,
        session_id: str,
        record_kind: str,
    ):
        """A recording of `system` with `settings`. `event(level, message)`
        tells the operator; `config_dir`, `session_id` and `record_kind`
        are where its config snapshot comes from and how the session cache notes
        it.
        """
        self.system = system
        self.settings = settings
        self.plugins = plugins
        self.plugin_params = plugin_params
        self.profiles = profiles
        self.clock = resolve_pulse_clock(settings, plugins, plugin_params)
        # octacam drives the trigger, so it can prime the cameras.
        self.primed = settings.trigger_source in ("software", "managed")
        self.hooks_timeout_s = start_sequence_timeout_s(
            self.clock.period_ns, self.primed
        )
        self.hooks_done = threading.Event()
        self.aborted = False
        self.start_wall_ns = 0
        self.recording: list[str] = []  # the cameras whose record grab started
        self.camera_takes: list[CameraTake] = []  # every camera's, in camera order
        self.primed_pulses = 0
        self.train_end: float | None = None  # monotonic, once the train started
        self.deadline: float | None = None  # monotonic, once the countdown started
        self.completed: bool | None = None  # ran to its end (None: not over yet)
        self.sync: dict | None = None
        self._event = event
        self._config_dir = config_dir
        self._session_id = session_id
        self._record_kind = record_kind
        self._stop = threading.Event()

    def stop(self, abort: bool) -> None:
        """End the take early; the last call says whether it was aborted."""
        self.aborted = abort
        self._stop.set()

    # ---------------------------------------------------------------- start

    def start_cameras(self) -> str | None:
        """Start the record grab on every camera (under the controller lock); None,
        or why no camera could start.
        """
        system, settings = self.system, self.settings
        software = settings.trigger_source == "software"
        try:
            system.stop_software_trigger()
            system.enable_frame_trigger()
            system.set_trigger_source(software)
            system.set_software_trigger_frequency(settings.fps)
            self.start_wall_ns = time.time_ns()
            # One format per camera: cameras past the NVENC session cap (or all,
            # without NVENC) encode on CPU, and the operator is told.
            formats, warnings = resolve_capture_formats(
                settings.video_format(), len(system), settings.max_nvenc_sessions
            )
            for message in warnings:
                self._event("warning", message)
            started = system.start_record(
                Path(settings.save_dir),
                settings.fps,
                formats,
                self.clock,
                record_form=settings.record_form,
                writer_queue_size=settings.writer_queue_size,
                hold=self.primed,
            )
        except Exception as e:
            # Some cameras may already be writing with no monitor to stop them.
            log.exception("Recording failed to start")
            with contextlib.suppress(Exception):
                system.stop()
            return f"Recording failed to start: {e}"
        self.camera_takes = [c.take for c in system if c.take is not None]
        if not started:
            self._event("error", "No camera could start recording")
            return "No camera could start recording"
        self.recording = list(started)
        if len(started) < len(system):
            missing = [c.name for c in system if c.name not in started]
            self._event(
                "warning",
                f"Only {len(started)}/{len(system)} cameras started recording "
                f"(missing: {', '.join(missing)})",
            )
        return None

    def write_start_files(self, camera_params: dict[str, str]) -> None:
        """The config snapshot, and a provisional summary (aborted, no frames) so
        a raw take keeps its geometry and stays transcodable if the process dies
        before the teardown rewrites it.
        """
        self._write_config_snapshot(camera_params)
        self._write_summary([take.stats() for take in self.camera_takes], aborted=True)

    def start_sequence(self) -> None:
        """Prime, then count, then start the train, and set `hooks_done`. Each
        step is skipped once a stop came in, so the hardware is never armed after
        the cameras stopped.
        """
        try:
            if self.primed and not self._stop.is_set():
                self._prime()
                for take in self.camera_takes:
                    take.arm_counting()
            if not self._stop.is_set():
                if self.settings.trigger_source == "software":
                    self.system.start_software_trigger(self.settings.duration_s)
                self.plugins.on_recording_start(self.plugin_params)
                # Anchored after the arm returns: it can take seconds (a board's
                # USB-reset recovery), which an earlier anchor cuts from the take.
                clock = self.clock
                if clock.count and clock.fill:
                    self.train_end = (
                        time.monotonic()
                        + clock.count * clock.period_ns / 1e9
                        + TRAIN_END_MARGIN_S
                    )
        finally:
            self.hooks_done.set()

    def _prime(self) -> None:
        """Send sacrificial triggers until every recording camera has answered
        one, so the triggers a camera ignores after its acquisition start (see
        PRIME_PULSES) are behind it when the train starts.

        Rounds of PRIME_PULSES repeat within PRIME_BUDGET_S, each settling for
        `prime_settle_s`; the record grabs discard what they produce.
        """
        deadline = time.monotonic() + PRIME_BUDGET_S
        settle = prime_settle_s(self.clock.period_ns)
        sent = 0
        while not self._stop.is_set():
            if self.settings.trigger_source == "software":
                self.system.prime_software_trigger(PRIME_PULSES, self.settings.fps)
            elif not self.plugins.prime_trigger(self.plugin_params, PRIME_PULSES):
                # A burst the board never acknowledged may still land: let it
                # land under the hold, not in the count.
                self._stop.wait(settle)
                break
            sent += PRIME_PULSES
            self._stop.wait(settle)
            if not self._unprimed() or time.monotonic() >= deadline:
                break
        self.primed_pulses = sent
        if self._stop.is_set():
            return
        if not sent:
            self._event(
                "warning",
                "The trigger source could not prime the cameras; a camera that "
                "ignores its first triggers after acquisition start (e.g. a FLIR "
                "Grasshopper3) will start this recording a few pulses late",
            )
            return
        silent = self._unprimed()
        if silent:
            names = ", ".join(silent)
            self._event(
                "warning",
                f"Camera{'s' if len(silent) > 1 else ''} {names} answered none of "
                f"the {sent} priming pulses: {'they' if len(silent) > 1 else 'it'} "
                "may start this recording a few pulses late, or not be receiving "
                "the trigger",
            )

    def _unprimed(self) -> list[str]:
        """Recording cameras that have answered no priming pulse yet (a camera
        whose record grab failed to start can never answer one).
        """
        return [
            take.name
            for take in self.camera_takes
            if take.name in self.recording and take.primed_frames == 0
        ]

    # -------------------------------------------------------------- monitor

    def wait_for_first_frames(self) -> bool:
        """Wait until every started camera has delivered a counted frame; False
        once stopped.

        Under the software trigger, give up after STARTED_FAIL_AFTER_S and record
        with what started, so one stalled camera cannot hang the take; under a
        hardware trigger (managed or external) the first pulse may come
        arbitrarily late, so wait until stopped. Both thresholds count from the
        end of the start sequence: no counted frame comes before it, and at a low
        fps priming alone outlasts them.
        """
        hardware = self.settings.trigger_source != "software"
        expected = len(self.recording)
        start = time.monotonic()
        armed_at: float | None = None
        warned = False
        while not self._stop.is_set():
            if self._count_started() >= expected:
                break
            now = time.monotonic()
            if armed_at is None and (
                self.hooks_done.is_set() or now - start >= self.hooks_timeout_s
            ):
                armed_at = now
            elapsed = now - armed_at if armed_at is not None else 0.0
            if not warned and elapsed > STARTED_WARN_AFTER_S:
                if hardware:
                    self._event(
                        "info",
                        "Waiting for the external trigger; recording will begin "
                        "on the first frame",
                    )
                else:
                    self._event(
                        "warning",
                        "Not all cameras delivered a frame within "
                        f"{STARTED_WARN_AFTER_S:g} s; still waiting",
                    )
                warned = True
            if not hardware and elapsed > STARTED_FAIL_AFTER_S:
                self._event(
                    "error",
                    f"Only {self._count_started()}/{expected} cameras delivered a "
                    f"frame within {STARTED_FAIL_AFTER_S:g} s; starting the "
                    "countdown anyway",
                )
                break
            self._stop.wait(STARTED_POLL_INTERVAL_S)
        return not self._stop.is_set()

    def _count_started(self) -> int:
        return sum(1 for take in self.camera_takes if take.started)

    def begin_countdown(self) -> None:
        """Fire the first-frame hooks (flywheel motion), never before the arm, and
        set the deadline.
        """
        self.hooks_done.wait(self.hooks_timeout_s)
        self.plugins.on_first_frame(self.plugin_params)
        self.deadline = time.monotonic() + self.settings.duration_s + STOP_GRACE_S

    def countdown(self) -> None:
        """Run until stopped or the take is over, telling the operator of missed
        pulses meanwhile. A counted train ends on its pulse count: once every
        camera has its last pulse, or once the train is over (a camera that
        missed its last pulses cannot know it before then); the deadline is only
        the backstop.
        """
        deadline = self.deadline
        assert deadline is not None
        reported: dict[str, int] = {}
        takes = self.camera_takes
        while not self._stop.is_set():
            if takes and all(take.tracker.complete for take in takes):
                break
            train_end = self.train_end
            if train_end is not None and time.monotonic() >= train_end:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._report_missed_pulses(reported)
            self._stop.wait(min(remaining, 0.2))

    def _report_missed_pulses(self, reported: dict[str, int]) -> None:
        """Tell the operator, while recording, that a camera is missing pulses
        (`reported`: how many were told so far, by camera).
        """
        for take in self.camera_takes:
            missed = list(take.tracker.missed)
            before = reported.get(take.name, 0)
            if len(missed) > before:
                reported[take.name] = len(missed)
                new = missed[before:]
                shown = ", ".join(str(p) for p in new[:5]) + (
                    " \N{HORIZONTAL ELLIPSIS}" if len(new) > 5 else ""
                )
                self._event(
                    "warning",
                    f"Camera {take.name} missed trigger pulse(s) {shown} "
                    f"({len(missed)} so far)"
                    + (
                        "; filled with the previous frame to keep the cameras aligned"
                        if self.clock.fill
                        else ""
                    ),
                )

    def end_capture(self) -> None:
        """Note whether the take ran to its end, then wait out the start sequence:
        a stop never leaves a trigger running.
        """
        self.completed = not self._stop.is_set()
        self.hooks_done.wait(self.hooks_timeout_s)

    def teardown(self) -> None:
        """Trigger off -> grab loops exit -> writers drain -> sync check ->
        summary, timestamps and the session-cache note.
        """
        self.system.stop_software_trigger()
        # A completed train pads every video to its pulse count (a camera that
        # missed the last pulses still ends on the last pulse); a stopped take
        # ends where it was.
        clock = self.clock
        self.system.stop(clock.count if self.completed and clock.fill else None)
        stats = [take.stats() for take in self.camera_takes]
        self.sync = check_sync(
            stats, self.recording, clock, self.profiles, bool(self.completed)
        )
        for message in self.sync["warnings"]:
            self._event("warning", message)
        for message in self.sync["notes"]:
            self._event("info", message)
        for camera in stats:
            if camera.writer_failed:
                self._event(
                    "error",
                    f"Writer for camera {camera.name} failed during the recording "
                    "(see log for ffmpeg output)",
                )
        # A camera without frames wrote an empty file and its writer did not
        # fail, so say it here (usually an external trigger that never fired).
        empty = [camera.name for camera in stats if camera.frames == 0]
        if empty:
            self._event(
                "error",
                f"{len(empty)} camera(s) captured 0 frames (no video written): "
                f"{', '.join(empty)}. "
                + (
                    "No external trigger pulses were received during the "
                    "recording window."
                    if self.settings.trigger_source == "external"
                    else "The cameras delivered no frames."
                ),
            )
        self._write_summary(stats, aborted=self.aborted)
        if self.settings.save_frame_timestamps:
            self._write_timestamps(stats)
        self._note_in_session_cache()

    # ---------------------------------------------------------------- files

    def _write_config_snapshot(self, camera_params: dict[str, str]) -> None:
        """Save the config into the recording's `octacam_recording` subfolder,
        a config directory `octacam gui <recording>` relaunches the rig from.

        The rig TOML gets the live Record-tab settings, plugin options, View-tab
        transforms and Process params patched in, beside every camera's
        parameter file. Unchanged, it stays a byte-verbatim copy; the directory
        templates are never patched, so a relaunch resolves a fresh folder. A
        failed re-emit falls back to the verbatim copy.
        """
        settings = self.settings
        src = snapshot_source(self._config_dir, settings.save_dir)
        if src is None:
            return
        config_dir = src.parent
        info_dir = Path(settings.save_dir) / RECORDING_INFO_DIRNAME
        dst = info_dir / CONFIG_SNAPSHOT_FILENAME
        try:
            info_dir.mkdir(parents=True, exist_ok=True)
            raw = config_writer.load_raw_config(config_dir)
            patched = config_writer.with_process_params(
                raw,
                transcode_ffmpeg_params=settings.transcode_ffmpeg_params,
                transfer_directory=settings.transfer_directory,
                transfer_checksum=settings.transfer_checksum,
            )
            patched = config_writer.with_record_settings(
                patched, settings.record_config_values()
            )
            patched = config_writer.with_plugin_options(
                patched, self.plugins.snapshot_options(self.plugin_params)
            )
            patched = config_writer.with_camera_transforms(
                patched,
                {c.serial_number: c.display_transform.to_dict() for c in self.system},
            )
            if raw and patched != raw:
                config_writer.write_config(info_dir, patched)
            else:
                shutil.copyfile(src, dst)
        except Exception:
            log.exception("Failed to write patched config snapshot to %s", dst)
            with contextlib.suppress(Exception):
                shutil.copyfile(src, dst)
        try:
            # Beside the TOML: a relaunch loads them as one config directory.
            config_writer.write_pfs_files(
                info_dir, camera_params, self.system.extension_by_serial()
            )
            config_writer.copy_auxiliary_pfs(
                config_dir, info_dir, set(camera_params), self.system.extensions
            )
        except Exception:
            log.exception("Failed to save the camera parameter files to %s", info_dir)

    def _write_summary(self, stats: list[CameraStats], aborted: bool) -> None:
        """Write the summary (its `file` entries name videos in the recording
        folder).
        """
        folder = self.settings.save_dir
        try:
            summary = build_recording_summary(
                self.settings,
                stats,
                self.start_wall_ns,
                aborted,
                pulse_clock=self.clock,
                sync=self.sync,
                primed_pulses=self.primed_pulses,
                completed=self.completed,
            )
            log.info("Wrote recording summary: %s", write_summary(folder, summary))
        except Exception:
            log.exception("Failed to write the recording summary in %s", folder)

    def _write_timestamps(self, stats: list[CameraStats]) -> None:
        """Write every camera's per-frame series into `timestamps.npz`."""
        folder = self.settings.save_dir
        try:
            arrays = build_timestamps_arrays(stats)
            log.info("Wrote frame timestamps: %s", write_timestamps(folder, arrays))
        except Exception:
            log.exception("Failed to write the frame timestamps in %s", folder)

    def _note_in_session_cache(self) -> None:
        """Note the recording's folder in the session cache (`octacam process
        --last`).
        """
        folder = Path(self.settings.save_dir)
        try:
            session_cache.record_recording(folder, self._session_id, self._record_kind)
        except Exception:
            log.exception("Failed to note %s in the recording cache", folder)
