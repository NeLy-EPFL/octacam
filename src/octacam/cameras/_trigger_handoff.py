"""Software-trigger hand-off between the shared trigger timer and grab loops.

The shared :class:`~octacam.trigger.PreciseTimer` calls ``trigger_once`` on every
camera from one thread, so a device trigger there would let a slow camera delay
every other one (a 100 fps Basler fell to ~82 fps behind two FLIRs). Instead
``trigger_once`` only offers a trigger to the camera's :class:`SoftwareTrigger`,
and the camera's own :meth:`~octacam.cameras.base.CameraBackend.retrieve` claims
it, fires it and fetches its image::

    fire = trigger.claim(timeout_ms)
    if fire is None:
        return None
    if fire and not <fire one device software trigger>:
        trigger.unfired()
        return None
    # fetch at most one frame; any image the SDK hands over, incomplete too:
    trigger.answered(timestamp_ns)

so one frame is recorded per trigger fired.

Sequence
    Every trigger offered while grabbing is numbered from the grab start or
    :meth:`SoftwareTrigger.restart_sequence`, dropped ones included.
    :attr:`SoftwareTrigger.last_index` names the trigger the latest image
    answers, so a missed pulse is a gap in the sequence.

Pairing
    A fired trigger stays outstanding until an image answers it. While one is
    outstanding and within its answer deadline, ``claim`` fires nothing and only
    fetches, so an image always answers the oldest outstanding trigger: firing on
    regardless would label every frame after a late image one pulse late. Past
    the deadline the trigger is given up on (a missed pulse).

Overflow
    At most :data:`PENDING_MAX` triggers wait; more are dropped, newest first,
    with a rate-limited warning, so an fps above the camera's maximum shows as
    missed pulses rather than a stale backlog. A dropped trigger was never fired,
    so frames never outnumber the triggers fired.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import NamedTuple

log = logging.getLogger("octacam")

# One trigger draining, one queued: frame times stay near real time instead of
# replaying a backlog.
PENDING_MAX = 2
# How long a fired trigger may await its image. It must outlast the slowest real
# answer (the exposure, ~11 ms of GS3 readout and transfer, a host stall), since a
# later image is credited to the next trigger; it is also what a lost image costs
# (every pulse meanwhile is missed). A transport failure normally comes back as an
# incomplete image, which answers at once.
ANSWER_TIMEOUT_S = 1.0
# Before a grab's first answer, while no recording counts, a trigger gets only this
# (one fetch) and is not reported: a GS3 silently ignores its first software
# triggers after acquisition start, and at the long deadline each cost 1 s (priming
# answered nothing and blocked the train's first pulses). The controller's
# post-priming settle outlasts it.
PRIMING_ANSWER_TIMEOUT_S = 0.1
# A recording's period stretches both deadlines to at least two periods (a slow
# exposure alone can outlast them), and while it counts, a pending trigger older
# than max(this, half a period) is dropped, not fired: fired late, its image would
# show a moment nearer another pulse than its own.
STALE_TRIGGER_S = 0.02
# Camera-clock check: an image's timestamp minus its trigger's host fire time is a
# near-constant offset. A given-up trigger's late image shows one lower by at least
# the gap between the fires (a deadline, >= 0.1 s), so an image more than this
# below the median of the last OFFSET_WINDOW offsets answers nothing and its
# trigger keeps waiting. The fire time is taken at the claim, before the device
# call, so a host stall only raises an offset. Timestamps must be in ns.
STALE_IMAGE_TOLERANCE_NS = 50_000_000
OFFSET_WINDOW = 64
# The check judges only with this many offsets: a median of one or two is the
# samples themselves, and one stall or 128 s glitch among them would make every
# later image look stale. A trigger given up on after an image was rejected for it
# clears the reference (that image was probably its own).
REFERENCE_MIN_SAMPLES = 8
# The check cannot see a steady one-trigger shift, which starts when a late image
# waits in the buffer while nothing is outstanding (the loop fetches only what it
# fires). So for max(this, two periods) after a give-up, an idle loop fetches
# anyway, and what it gets answers nothing.
DRAIN_WINDOW_S = 0.25
# A drain fetch polls: a real fetch blocks for its whole timeout, which would make a
# trigger offered meanwhile stale (see SoftwareTrigger.fetch_timeout_ms).
DRAIN_POLL_MS = 5
# last_index of an image answering no trigger of the current sequence: one fired
# before restart_sequence (a late priming answer) ...
PRIMING_TRIGGER = -1
# ... or one no outstanding trigger accounts for (a given-up trigger's late image).
UNMATCHED_TRIGGER = -2


class _Fired(NamedTuple):
    """A fired trigger awaiting its image."""

    seq: int
    epoch: int  # advances with each sequence restart: an earlier one's answer shows
    deadline_ns: int
    counted: bool  # fired while a recording counts: reported if never answered
    fired_ns: int


def _ns(seconds: float) -> int:
    return int(seconds * 1e9)


def _median(values: deque[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


class SoftwareTrigger:
    """One camera's software-trigger hand-off (see the module docstring).

    The backend calls :meth:`begin_grab` / :meth:`end_grab` around streaming
    (:attr:`grabbing` is its ``is_grabbing``: a locked Python bool, never a native
    SDK query), :meth:`offer` in ``trigger_once``, and claims, fires and answers in
    ``retrieve``. Every deadline and fire time is read from ``clock`` (monotonic
    ns). The counters are per grab, except :attr:`dropped_triggers`.
    """

    def __init__(self, serial: str, clock: Callable[[], int] = time.monotonic_ns):
        self._serial = serial
        self._clock = clock
        self._cond = threading.Condition()
        self._grabbing = False
        # Offered, unclaimed triggers, oldest first: (sequence number, offer time).
        self._pending: deque[tuple[int, int]] = deque()
        self._next_seq = 0
        self._period_s: float | None = None
        self._outstanding: deque[_Fired] = deque()  # oldest first
        self._epoch = 0
        # A grab's first answer, or counting, ends the priming deadline.
        self._answered_in_grab = False
        self._counting = False
        self._answer: tuple[int, int] | None = None  # (seq, epoch) last answered
        # Camera timestamp minus host fire time of the latest answers.
        self._offsets: deque[int] = deque(maxlen=OFFSET_WINDOW)
        self._rejected_for: tuple[int, int] | None = None  # (seq, epoch)
        # Until when an idle loop fetches anyway; whether this grab gave one up.
        self._drain_until = 0
        self._expired_in_grab = False
        self._drain_poll = False  # the latest claim asked only for a drain poll
        self.dropped_triggers = 0  # offered past PENDING_MAX
        self.stale_triggers = 0  # pending too long to fire in time
        self.unanswered_triggers = 0  # fired, given up at the answer deadline
        self.stale_images = 0  # discarded: older than the trigger they would answer

    @property
    def grabbing(self) -> bool:
        return self._grabbing

    @property
    def pending(self) -> int:
        """Triggers offered and not yet claimed."""
        return len(self._pending)

    @property
    def last_index(self) -> int | None:
        """Sequence number of the trigger the latest image answers, or
        :data:`PRIMING_TRIGGER`/:data:`UNMATCHED_TRIGGER`; None before an answer
        in this grab (a hardware-triggered fetch never draws on the hand-off)."""
        answer = self._answer
        if answer is None:
            return None
        seq, epoch = answer
        return PRIMING_TRIGGER if epoch != self._epoch else seq

    @property
    def fired_index(self) -> int | None:
        """Sequence number of the newest outstanding trigger (the one a retrieve
        just fired), or None."""
        with self._cond:
            return self._outstanding[-1].seq if self._outstanding else None

    @property
    def next_index(self) -> int:
        """The sequence number the next offered trigger gets."""
        return self._next_seq

    def configure_period(self, period_s: float | None) -> None:
        """The period of a counting recording's triggers (None for preview and the
        benchmark); see :data:`STALE_TRIGGER_S`."""
        with self._cond:
            self._period_s = period_s if period_s and period_s > 0 else None

    def restart_sequence(self) -> None:
        """Forget pending triggers and number the next one 0: counting starts.

        A priming trigger still awaiting its image stays outstanding at its short
        deadline, and its image reads :data:`PRIMING_TRIGGER`."""
        with self._cond:
            self._pending.clear()
            self._next_seq = 0
            self._epoch += 1
            self._counting = True
            # The train's first trigger must fire on time, not wait out a drain
            # fetch (the post-priming settle has drained the priming images).
            self._drain_until = 0
            # Priming gave a trigger up: its answers may be a stalled image's.
            if self._expired_in_grab:
                self._offsets.clear()

    def offer(self) -> None:
        """One pending trigger, without device I/O; dropped at :data:`PENDING_MAX`
        and when not grabbing."""
        with self._cond:
            if not self._grabbing:
                return
            seq = self._next_seq
            self._next_seq += 1
            if len(self._pending) < PENDING_MAX:
                self._pending.append((seq, self._clock()))
                self._cond.notify()
                return
            self.dropped_triggers += 1
            if self.dropped_triggers % 100 == 1:
                log.warning(
                    "Camera %s: dropping software triggers (%d so far) — the "
                    "requested fps exceeds what the camera can deliver; it is "
                    "running at its maximum rate.",
                    self._serial,
                    self.dropped_triggers,
                )

    def begin_grab(self) -> None:
        """Arm the hand-off when streaming starts (resets the backlog)."""
        with self._cond:
            self._pending.clear()
            self._next_seq = 0
            self._outstanding.clear()
            self._answer = None
            self._epoch += 1
            self._answered_in_grab = False
            self._counting = False
            self.unanswered_triggers = 0
            self.stale_triggers = 0
            self._offsets.clear()
            self.stale_images = 0
            self._rejected_for = None
            self._drain_until = 0
            self._expired_in_grab = False
            self._grabbing = True

    def end_grab(self) -> bool:
        """Disarm and wake a retrieve parked in :meth:`claim`; call it before the
        native stop. True if it ended a live grab."""
        with self._cond:
            was_grabbing = self._grabbing
            self._grabbing = False
            self._cond.notify_all()
            return was_grabbing

    def claim(self, timeout_ms: int) -> bool | None:
        """What this retrieve does: True fire then fetch, False only fetch (a
        trigger is outstanding, or a drain), None nothing (no trigger within
        ``timeout_ms``, or not grabbing). A claimed trigger is outstanding at
        once, so a failed device call must report :meth:`unfired`.
        """
        with self._cond:
            self._drain_poll = False
            if not self._grabbing:
                return None
            self._expire_unanswered()
            if self._outstanding:
                return False
            self._drop_stale_pending()
            if not self._pending and self._clock() < self._drain_until:
                self._drain_poll = True
                return False  # nothing to fire: fetch a given-up trigger's late image
            if not self._pending:
                self._cond.wait(timeout_ms / 1000.0)
            if not self._pending or not self._grabbing:
                return None
            seq, _offered = self._pending.popleft()
            patient = self._answered_in_grab or self._counting
            timeout = ANSWER_TIMEOUT_S if patient else PRIMING_ANSWER_TIMEOUT_S
            if self._period_s:
                timeout = max(timeout, 2 * self._period_s)
            now = self._clock()
            self._outstanding.append(
                _Fired(seq, self._epoch, now + _ns(timeout), self._counting, now)
            )
            return True

    def fetch_timeout_ms(self, timeout_ms: int) -> int:
        """This fetch's timeout: a :data:`DRAIN_POLL_MS` poll for a drain."""
        return min(timeout_ms, DRAIN_POLL_MS) if self._drain_poll else timeout_ms

    def unfired(self) -> None:
        """The device refused the trigger just claimed: no image will answer it,
        so it is not outstanding (its pulse is a gap in the sequence)."""
        with self._cond:
            if self._outstanding:
                self._outstanding.pop()

    def answered(self, timestamp_ns: int | None = None) -> None:
        """An image was fetched, usable or not: it answers the oldest outstanding
        trigger, unless its timestamp (ns, else None) shows it older than that
        trigger (see :data:`STALE_IMAGE_TOLERANCE_NS`)."""
        with self._cond:
            if not self._outstanding:
                self._answer = (UNMATCHED_TRIGGER, self._epoch)
                return
            oldest = self._outstanding[0]
            offset = timestamp_ns - oldest.fired_ns if timestamp_ns else None
            if (
                offset is not None
                and len(self._offsets) >= REFERENCE_MIN_SAMPLES
                and offset < _median(self._offsets) - STALE_IMAGE_TOLERANCE_NS
            ):
                self._answer = (UNMATCHED_TRIGGER, self._epoch)
                self._rejected_for = (oldest.seq, oldest.epoch)
                self.stale_images += 1
                if self.stale_images % 100 == 1:
                    log.warning(
                        "Camera %s: an image arrived after its trigger was given up "
                        "on (%d so far); discarded, so it cannot answer a later one",
                        self._serial,
                        self.stale_images,
                    )
                return
            self._outstanding.popleft()
            self._answer = (oldest.seq, oldest.epoch)
            self._answered_in_grab = True
            if offset is not None:
                self._offsets.append(offset)

    def take(self, timeout_ms: int) -> int | None:
        """Consume one pending trigger (the fake's model of a hardware trigger
        line): its sequence number, or None when none came within ``timeout_ms``
        or the grab ended."""
        with self._cond:
            if not self._pending and self._grabbing:
                self._cond.wait(timeout_ms / 1000.0)
            if not self._pending or not self._grabbing:
                return None
            seq, _offered = self._pending.popleft()
            return seq

    def wait(self, timeout_s: float) -> None:
        """Block up to ``timeout_s``, until a trigger is offered or the grab ends;
        at once when not grabbing (the fake's device waits here)."""
        with self._cond:
            if self._grabbing:
                self._cond.wait(timeout_s)

    def _drop_stale_pending(self) -> None:
        """Drop pending triggers too old to fire while a recording counts (caller
        holds the condition); their pulses are missed. Priming never drops: it
        waits out an ignored trigger's deadline by design."""
        if not self._period_s or not self._counting:
            return
        limit = _ns(max(STALE_TRIGGER_S, 0.5 * self._period_s))
        now = self._clock()
        while self._pending and now - self._pending[0][1] > limit:
            self._pending.popleft()
            self.stale_triggers += 1
            if self.stale_triggers % 100 == 1:
                log.warning(
                    "Camera %s: dropped %d software trigger(s) that could no longer "
                    "be fired in time (the camera was still waiting on an earlier "
                    "image); their pulses are missed and filled",
                    self._serial,
                    self.stale_triggers,
                )

    def _expire_unanswered(self) -> None:
        """Give up on the outstanding triggers past their answer deadline (the
        caller holds the condition)."""
        now = self._clock()
        while self._outstanding and now > self._outstanding[0].deadline_ns:
            fired = self._outstanding.popleft()
            if self._rejected_for == (fired.seq, fired.epoch):
                # The image rejected for it was probably its own.
                self._offsets.clear()
                self._rejected_for = None
            window = max(DRAIN_WINDOW_S, 2 * self._period_s) if self._period_s else DRAIN_WINDOW_S
            self._drain_until = max(self._drain_until, now + _ns(window))
            self._expired_in_grab = True
            if not fired.counted:
                continue  # ignored before the recording counts: expected
            self.unanswered_triggers += 1
            if self.unanswered_triggers % 100 == 1:
                log.warning(
                    "Camera %s: no image arrived for a software trigger within "
                    "%.1f s (%d so far); firing the next one",
                    self._serial,
                    ANSWER_TIMEOUT_S,
                    self.unanswered_triggers,
                )
