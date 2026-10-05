"""One camera's recording: the record loop and its accounting.

:class:`CameraTake` assigns every delivered frame to the trigger pulse that
exposed it (:mod:`octacam.pulses`), fills what the camera missed so that video
frame k is pulse k, and writes the video. :class:`CameraStats` is the record it
leaves for the summary, the sync check and the timestamps file.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from octacam.cameras._trigger_handoff import PRIMING_TRIGGER
from octacam.pulses import Assignment, PulseClock, PulseTracker
from octacam.transform import DisplayTransform, apply_display_transform
from octacam.writer import VideoFormat, WriteResult

if TYPE_CHECKING:
    from octacam.cameras.base import Camera

log = logging.getLogger("octacam")

# How long one retrieve blocks: a grab loop sees its stop flag at least this often.
GRAB_TIMEOUT_MS = 100
# After priming (see CameraTake.arm_counting), a frame within
# max(PRIME_STRAGGLER_NS, PRIME_STRAGGLER_PERIODS periods) of the last priming
# frame is a priming straggler, not pulse 0. Both must stay below the take's
# settle before the train (octacam.take.PRIME_SETTLE_S, at least four periods).
PRIME_STRAGGLER_NS = 50_000_000
PRIME_STRAGGLER_PERIODS = 2.5


@dataclass
class CameraStats:
    """One camera's take, as its summary and timestamps file record it.

    The five series are parallel, one row per video frame: fills included,
    frames skipped under writer overload not.
    """

    name: str
    serial: str
    transform: DisplayTransform
    # (width, height) as written: the transform's output when it was baked in.
    frame_size: tuple[int, int] | None
    pixel_format: str
    # ns: the camera's timestamp, else host time (see host_fallback_count); a
    # fill carries the time its pulse was due, never 0.
    timestamp_ns: list[int]
    pulse_index: list[int]
    dropped: list[bool]  # a fill: a repeat of the previous frame
    missed: list[bool]  # of the fills, one for a pulse the camera missed
    arrival_ns: list[int]  # host wall-clock delivery, 0 for a fill
    missed_pulses: list[int]  # pulses the camera delivered no frame for
    # Pulses exposed markedly later than the clock predicts (e.g. a camera
    # firing on the trigger pulse's falling edge).
    late_pulses: list[int]
    timestamp_glitches: list[dict]  # camera-clock jumps the accounting corrected
    clock_mismatch: bool  # the frames did not follow the clock's period
    writer_dropped: int  # delivered frames the writer refused, each filled
    # The pulses of frames refused under writer overload: skipped, not filled.
    writer_skipped: list[int]
    extra_frames: int  # frames of no pulse of the train, discarded
    primed_frames: int  # frames answering the priming, discarded
    unclocked_frames: int  # frames placed without a hardware timestamp
    # Rows whose timestamp the camera did not supply (stamped by host time or by
    # when the pulse was due), and fills stamped from them: normally 0 or every
    # row (a host-clocked backend); in between is a stray-zero anomaly.
    host_fallback_count: int
    # The SDK's transport counters over the take: a missed pulse with none of
    # them moving was never exposed by the camera.
    stream: dict[str, int]
    writer_failed: bool

    @property
    def frames(self) -> int:
        return len(self.timestamp_ns)

    @property
    def dropped_indices(self) -> list[int]:
        """The video frames that are fills."""
        return [i for i, dropped in enumerate(self.dropped) if dropped]

    @property
    def start_timestamp_ns(self) -> int | None:
        return self.timestamp_ns[0] if self.timestamp_ns else None

    @property
    def mean_fps(self) -> float:
        """The rate over the whole take; 0 when the span is not positive (a clock
        that jumped back has no rate)."""
        timestamps = self.timestamp_ns
        if len(timestamps) < 2:
            return 0.0
        span_ns = timestamps[-1] - timestamps[0]
        return (len(timestamps) - 1) * 1e9 / span_ns if span_ns > 0 else 0.0


class CameraTake:
    """One camera's recording: its record grab thread, its writer and its pulse
    accounting.

    Its thread is the only writer of its state. Live while it runs: ``started``,
    ``primed_frames``, ``dropped_count``, :attr:`frames` and ``tracker``; the
    rest through :meth:`stats`, final once the take is joined.
    """

    def __init__(
        self,
        camera: "Camera",
        clock: PulseClock,
        *,
        video_format: VideoFormat,
        queue_size: int,
        record_form: str,
        hold: bool,
    ):
        """A take of ``camera`` counted against ``clock``, written in
        ``video_format`` behind a queue of ``queue_size``. ``record_form``
        "display" bakes the display transform into the video; ``hold`` discards
        frames until :meth:`arm_counting`, while the recording primes."""
        self.name = camera.name
        self.serial = camera.serial_number
        self.clock = clock
        self.tracker = PulseTracker(clock)
        self.writer = video_format.create_writer(max(1, queue_size))
        self.started = False  # delivered a counted frame
        self.primed_frames = 0
        self.dropped_count = 0  # fills so far
        self.frame_size: tuple[int, int] | None = None
        self._backend = camera.backend
        self._display = camera.frame_for_display
        self._transform = camera.display_transform
        self._bake = record_form == "display" and not self._transform.is_identity
        self._pixel_format = camera.pixel_format
        # A software clock's frames answer the hand-off's triggers; any other
        # clock's are fetched un-gated (retrieve would wait for a software
        # trigger forever).
        self._software = clock.source == "software"
        self._armed = threading.Event()
        if not hold:
            self._armed.set()
        self._stop = threading.Event()
        self._fill_to: int | None = None
        self._thread: threading.Thread | None = None
        # The rows (see CameraStats) and the counters.
        self._timestamp_ns: list[int] = []
        self._pulse_index: list[int] = []
        self._dropped: list[bool] = []
        self._missed: list[bool] = []
        self._arrival_ns: list[int] = []
        self._writer_dropped = 0
        self._writer_skipped: list[int] = []
        self._extra_frames = 0
        self._unclocked_frames = 0
        self._host_fallback_count = 0
        self._stream_at_start: dict[str, int] = {}
        self._stream_at_stop: dict[str, int] = {}
        # The record loop's state: frames owed to the video and not yet queued
        # (fills, refused frames), which ride on the next queued frame or on
        # close so the video keeps one frame per pulse; the last priming frame's
        # timestamp; and (timestamp, pulse, unstamped) of the last delivered
        # frame, which fills are stamped from (_fill_stamp).
        self._owed = 0
        self._last_primed_ts: int | None = None
        self._anchor: tuple[int, int, bool] | None = None
        self._straggler_ns = max(
            PRIME_STRAGGLER_NS, int(PRIME_STRAGGLER_PERIODS * clock.period_ns)
        )

    @property
    def frames(self) -> int:
        return len(self._timestamp_ns)

    def start(self, save_path: str, fps: float) -> bool:
        """Open the writer and start the record grab; True iff the record loop
        was launched."""
        sensor = (self._backend.width(), self._backend.height())
        self.frame_size = self._transform.output_size(*sensor) if self._bake else sensor
        if not self.writer.open(save_path, fps, self.frame_size):
            log.error("Failed to open video writer for: %s", save_path)
            return False
        # The writer's ffmpeg child is live: close it on any failure below.
        try:
            # A software train's period sets the hand-off's deadlines.
            period_ns = self.clock.period_ns
            self._backend.configure_trigger_period(
                period_ns / 1e9 if self._software and period_ns else None
            )
            if not self._backend.start_grab_record():
                log.error("Failed to start grabbing for recording on camera %s", self.serial)
                self.writer.close()
                return False
        except Exception:
            self.writer.close()
            raise
        # Baseline the transport counters before any trigger: some restart with
        # each acquisition, others run on; both diff to this take's.
        self._stream_at_start = self._read_stream_statistics()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def arm_counting(self) -> None:
        """End the priming ``hold``: the next trigger is the take's first, and a
        software-trigger sequence restarts so it is trigger 0."""
        self._backend.restart_trigger_sequence()
        self._armed.set()

    def stop(self, fill_to: int | None = None) -> None:
        """Stop the record loop. ``fill_to`` (a completed train's count) pads a
        camera that missed the last pulses, so it ends aligned."""
        if fill_to is not None:
            self._fill_to = fill_to
        self._stop.set()

    def join(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()

    def stats(self) -> CameraStats:
        """The take so far, as a copy: final once the take is joined. It copies
        every series, so read it once per take, not per tick."""
        tracker = self.tracker
        start, stop = self._stream_at_start, self._stream_at_stop
        return CameraStats(
            name=self.name,
            serial=self.serial,
            transform=self._transform,
            frame_size=self.frame_size,
            pixel_format=self._pixel_format,
            timestamp_ns=list(self._timestamp_ns),
            pulse_index=list(self._pulse_index),
            dropped=list(self._dropped),
            missed=list(self._missed),
            arrival_ns=list(self._arrival_ns),
            missed_pulses=list(tracker.missed),
            late_pulses=list(tracker.late),
            timestamp_glitches=[
                {"pulse": g.pulse, "kind": g.kind, "jump_ns": g.jump_ns}
                for g in tracker.glitches
            ],
            clock_mismatch=tracker.clock_mismatch,
            writer_dropped=self._writer_dropped,
            writer_skipped=list(self._writer_skipped),
            extra_frames=self._extra_frames,
            primed_frames=self.primed_frames,
            unclocked_frames=self._unclocked_frames,
            host_fallback_count=self._host_fallback_count,
            # A counter the SDK restarts at acquisition start reads below its
            # start value; its stop value is then already the take's own count.
            stream={
                k: v - start.get(k, 0) if v >= start.get(k, 0) else v
                for k, v in stop.items()
            },
            writer_failed=self.writer.failed,
        )

    # ---------------------------------------------------------- record loop

    def _run(self) -> None:
        backend = self._backend
        retrieve = backend.retrieve if self._software else backend.retrieve_external
        while not self._stop.is_set() and backend.is_grabbing():
            # Stop on the pulse count, so every camera ends on the same pulse.
            if self.tracker.complete:
                break
            frame = retrieve(GRAB_TIMEOUT_MS, _always)
            # Record always requests the array; a frame without one is defensive.
            if frame is not None and frame[0] is not None:
                self._on_frame(frame[0], frame[1])
        self._finish()

    def _on_frame(self, array: np.ndarray, timestamp: int) -> None:
        """Discard a priming or stray frame, else record it."""
        host_ns = time.monotonic_ns()
        if not self._armed.is_set():  # an answer to a priming trigger
            self._count_primed(timestamp)
            return
        # Read after the armed check: an image answered before the sequence
        # restart reports PRIMING_TRIGGER however late this read comes.
        index = self._backend.last_trigger_index
        if index is not None and index < 0:
            # Answers no trigger of the recording (see _trigger_handoff).
            if index == PRIMING_TRIGGER:
                self.primed_frames += 1
            else:
                self._extra_frames += 1
            return
        if (
            index is None
            and timestamp
            and self._last_primed_ts is not None
            and 0 <= timestamp - self._last_primed_ts < self._straggler_ns
        ):
            # A priming frame still buffered at arm time (a hardware trigger has
            # no sequence number: only the timestamp tells).
            self._count_primed(timestamp)
            return
        self._record(array, timestamp, index, host_ns)

    def _count_primed(self, timestamp: int) -> None:
        self.primed_frames += 1
        self._last_primed_ts = timestamp or self._last_primed_ts

    def _record(
        self, array: np.ndarray, timestamp: int, index: int | None, host_ns: int
    ) -> None:
        """Place a counted frame on its pulse, fill the pulses it shows missed,
        write it and add its row."""
        tracker = self.tracker
        arrival = time.time_ns()
        assignment, placed = self._place(index, timestamp, host_ns)
        stamp = placed or arrival
        if assignment.extra:
            if self.clock.fill:
                self._extra_frames += 1
                return
            # An external clock octacam cannot see: report, never discard.
            pulse = tracker.last_pulse if tracker.last_pulse is not None else 0
        else:
            pulse = assignment.pulse
            assert pulse is not None
        if self.clock.fill and assignment.missed:
            # A leading miss counts back from this frame.
            self._fill(assignment.missed, self._anchor or (stamp, pulse, not timestamp))
            log.warning(
                "Camera %s missed trigger pulse(s) %s; filled with the previous frame",
                self.serial,
                _format_pulses(assignment.missed),
            )
        self._anchor = (stamp, pulse, not timestamp)
        result = self._write(array, pulse)
        if result is not WriteResult.SKIPPED:  # a skipped frame has no video frame
            self._append_row(
                stamp,
                pulse,
                missed=False,
                dropped=result is WriteResult.REFUSED,
                arrival=arrival,
                unstamped=not timestamp,
            )
        if self._display.push(array):
            self._display.refresh_fps(self._timestamp_ns)
        self.started = True

    def _write(self, array: np.ndarray, pulse: int) -> WriteResult:
        """Write the frame of ``pulse`` with the fills it owes, and account for
        what the writer did with it."""
        # The preview gets the raw array: the browser applies the transform.
        frame = apply_display_transform(array, self._transform) if self._bake else array
        result = self.writer.write(frame, fill_before=self._owed)
        if result is WriteResult.WRITTEN:
            self._owed = 0
        elif result is WriteResult.REFUSED:
            self._owed += 1
            self._writer_dropped += 1
            log.warning(
                "Frame for pulse %d dropped for camera %s (writer queue full); "
                "it will be filled with the previous frame",
                pulse,
                self.serial,
            )
        else:
            if not self._writer_skipped:
                log.error(
                    "Camera %s: the video writer cannot keep up — %d frames "
                    "refused before it caught up. From pulse %d on, a frame it "
                    "refuses is skipped instead of filled: this camera's video "
                    "will be short of the train (pulse_index in the timestamps "
                    "maps each frame to its pulse). The encoder or disk is "
                    "slower than the camera: use a faster save method or disk, "
                    "or a lower frame rate.",
                    self.serial,
                    self.writer.max_queue_size,
                    pulse,
                )
            self._writer_skipped.append(pulse)
        return result

    def _finish(self) -> None:
        """Stop the grab, pad a completed train and close the video."""
        self._stream_at_stop = self._read_stream_statistics()
        self._backend.stop_grab()
        tracker, clock, fill_to = self.tracker, self.clock, self._fill_to
        # A train that ran to its end: pad the last pulses this camera missed so
        # it ends with the others (given a frame to repeat). A stopped or aborted
        # take (fill_to None) ends where it was.
        if (
            clock.fill
            and fill_to is not None
            and clock.count is not None
            and self._timestamp_ns
            and self._anchor is not None
        ):
            end = min(fill_to, clock.count)
            trailing = range(tracker.next_pulse, end)
            self._fill(trailing, self._anchor)
            if trailing:
                tracker.missed.extend(trailing)
                tracker.last_pulse = end - 1
                log.warning(
                    "Camera %s missed the last trigger pulse(s) %s; filled with "
                    "the previous frame",
                    self.serial,
                    _format_pulses(trailing),
                )
        self.writer.close(fill_after=self._owed)
        self._reconcile_unwritten_frames()
        log.info(
            "Camera %s: %d frames recorded (%d missed pulses and %d writer drops "
            "filled, %d writer drops skipped), %d extra frames discarded",
            self.serial,
            len(self._timestamp_ns),
            sum(self._missed),
            self._writer_dropped,
            len(self._writer_skipped),
            self._extra_frames,
        )

    def _place(
        self, index: int | None, timestamp: int, host_ns: int
    ) -> tuple[Assignment, int | None]:
        """Assign a delivered frame to its pulse; returns ``(assignment, stamp)``,
        ``stamp`` being the timestamp its row carries (None: host arrival time).

        ``index`` is the software-trigger sequence number of the trigger the frame
        answers (exact), else the frame is placed from its camera ``timestamp``.
        """
        tracker = self.tracker
        if index is not None:
            return tracker.assign_index(index), (timestamp or None)
        if timestamp:
            if tracker.last_pulse is not None and tracker.expected_ts(tracker.next_pulse) is None:
                # The first timed frame after untimed ones: anchor the camera
                # clock at this frame's pulse, not the train's first (which would
                # restart the count and shift every frame).
                tracker.first_pulse = tracker.next_pulse
            return tracker.assign(timestamp, host_ns), timestamp
        self._unclocked_frames += 1
        due = tracker.expected_ts(tracker.next_pulse)
        if due is not None and due > 0:
            # A stray untimed frame on a clocked camera: the next pulse, stamped
            # when it was due, so the next interval is measured from an anchor
            # that agrees with it (else it reads as two periods: an invented
            # miss). Host arrival is no clock: buffered frames arrive bunched. A
            # real miss just before stays local: this frame is a pulse early.
            return tracker.assign(due, host_ns), due
        # A host-clocked backend: nothing places the frame, so it is the next pulse.
        return tracker.assign_index(tracker.next_pulse), None

    def _fill(self, pulses: range, anchor: tuple[int, int, bool]) -> None:
        """Owe a fill for each of ``pulses``, each with its row (stamped by
        :func:`_fill_stamp`)."""
        for pulse in pulses:
            due, unstamped = _fill_stamp(self.tracker, pulse, anchor)
            self._append_row(
                due, pulse, missed=True, dropped=True, arrival=0, unstamped=unstamped
            )
        self._owed += len(pulses)

    def _append_row(
        self,
        timestamp: int,
        pulse: int,
        *,
        missed: bool,
        dropped: bool,
        arrival: int,
        unstamped: bool,
    ) -> None:
        """One video frame's row. ``unstamped``: the camera did not supply its
        timestamp (see CameraStats.host_fallback_count)."""
        self._timestamp_ns.append(timestamp)
        self._pulse_index.append(pulse)
        self._missed.append(missed)
        self._dropped.append(dropped)
        self._arrival_ns.append(arrival)
        if dropped:
            self.dropped_count += 1
        if unstamped:
            self._host_fallback_count += 1

    def _reconcile_unwritten_frames(self) -> None:
        """After a writer failure the file ends at ``frames_written``: mark the
        later rows dropped."""
        if not self.writer.failed:
            return
        for index in range(self.writer.frames_written, len(self._dropped)):
            if not self._dropped[index]:
                self._dropped[index] = True
                self.dropped_count += 1

    def _read_stream_statistics(self) -> dict[str, int]:
        try:
            return self._backend.stream_statistics()
        except Exception as e:  # diagnostics must never break a recording
            log.debug("Could not read stream statistics of %s: %s", self.serial, e)
            return {}


def _fill_stamp(
    tracker: PulseTracker, pulse: int, anchor: tuple[int, int, bool]
) -> tuple[int, bool]:
    """A fill's timestamp: when ``pulse`` was due, on the camera clock where the
    tracker follows it, else whole periods from ``anchor`` (timestamp, pulse,
    unstamped) of a delivered frame. Returns it with whether it inherits an
    unstamped anchor; never 0 or negative.
    """
    due = tracker.expected_ts(pulse)
    if due is not None:
        return max(due, 1), False
    stamp, at_pulse, unstamped = anchor
    # An integer offset: a host-time stamp (~1.8e18 ns) is past float precision.
    offset = int(round((pulse - at_pulse) * tracker.period_ns))
    return max(stamp + offset, 1), unstamped


def _always() -> bool:
    return True


def _format_pulses(pulses: range) -> str:
    if len(pulses) == 1:
        return str(pulses.start)
    return f"{pulses.start}-{pulses.stop - 1}"
