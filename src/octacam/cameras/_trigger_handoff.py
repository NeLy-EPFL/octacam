"""Software-trigger hand-off between the shared trigger timer and grab loops.

The shared :class:`~octacam.trigger.PreciseTimer` calls ``trigger_once`` on every
camera from one thread, so a device trigger there would let a slow camera delay
every other one (a 100 fps Basler fell to ~82 fps behind two FLIRs). Instead
``trigger_once`` only bumps a counter, and each camera's own ``retrieve`` claims
a pending trigger, fires it and fetches its image::

    fire = self._claim_trigger(timeout_ms)
    if fire is None:
        return None
    if fire and not <fire one device software trigger>:
        self._trigger_unfired()
        return None
    # fetch at most one frame; any image the SDK hands over, incomplete too:
    self._trigger_answered()

so one frame is recorded per trigger fired.

Sequence
    Every trigger offered while grabbing is numbered from the grab start or
    :meth:`~SoftwareTriggerHandoff.restart_trigger_sequence`, dropped ones
    included. :attr:`~SoftwareTriggerHandoff.last_trigger_index` names the
    trigger the latest image answers, so a missed pulse is a gap in the sequence.

Pairing
    A fired trigger stays outstanding until an image answers it. While one is
    outstanding and within its answer deadline, ``retrieve`` fires nothing and
    only fetches, so an image always answers the oldest outstanding trigger:
    firing on regardless would label every frame after a late image one pulse
    late. Past the deadline the trigger is given up on (a missed pulse).

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
# trigger offered meanwhile stale (see _fetch_timeout_ms).
DRAIN_POLL_MS = 5
# last_trigger_index of an image answering no trigger of the current sequence: one
# fired before restart_trigger_sequence (a late priming answer) ...
PRIMING_TRIGGER = -1
# ... or one no outstanding trigger accounts for (a given-up trigger's late image).
UNMATCHED_TRIGGER = -2


def _median(values: deque[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


class SoftwareTriggerHandoff:
    """Mixin: the trigger hand-off shared by every backend.

    A backend calls :meth:`_init_trigger_handoff` in ``__init__``,
    :meth:`_bump_trigger` in ``trigger_once`` and :meth:`_begin_grab` /
    :meth:`_end_grab` around streaming, and drives ``retrieve`` as the module
    docstring shows. ``_grabbing`` is the source of truth for ``is_grabbing``: a
    locked Python bool, never a native SDK query.
    """

    def _init_trigger_handoff(self) -> None:
        self._cond = threading.Condition()
        self._pending = 0
        # Sequence numbers and offer times of the pending triggers (parallel).
        self._pending_seqs: deque[int] = deque()
        self._pending_times: deque[float] = deque()
        self._next_seq = 0
        self._period_s: float | None = None
        self._stale_triggers = 0
        # Camera timestamp minus host fire time of the latest answers.
        self._offsets: deque[int] = deque(maxlen=OFFSET_WINDOW)
        self._stale_images = 0
        # (seq, epoch) of the trigger an image was last rejected for.
        self._rejected_for: tuple[int | None, int] | None = None
        # Until when an idle loop fetches anyway; whether this grab gave one up.
        self._drain_until = 0.0
        self._expired_in_grab = False
        self._drain_poll = False  # the latest claim asked only for a drain poll
        # Fired, unanswered triggers, oldest first: (seq, epoch, answer deadline,
        # counted, fire time ns). The epoch advances with each sequence restart,
        # so an earlier sequence's answer is recognized; only counted triggers
        # are reported unanswered.
        self._outstanding: deque[tuple[int | None, int, float, bool, int]] = deque()
        self._epoch = 0
        # A grab's first answer, or counting, ends the priming deadline.
        self._answered_in_grab = False
        self._counting = False
        # (seq, epoch) of the trigger the latest image answered.
        self._answer: tuple[int | None, int] | None = None
        self._grabbing = False
        self._dropped_triggers = 0
        self._unanswered_triggers = 0

    @property
    def last_trigger_index(self) -> int | None:
        """Sequence number of the trigger the latest image answers, or
        :data:`PRIMING_TRIGGER`/:data:`UNMATCHED_TRIGGER`; None before an answer
        in this grab (a hardware-triggered fetch never draws on the hand-off)."""
        answer = self._answer
        if answer is None:
            return None
        seq, epoch = answer
        if seq is not None and epoch != self._epoch:
            return PRIMING_TRIGGER
        return seq

    def configure_trigger_period(self, period_s: float | None) -> None:
        """The period of a counting recording's triggers (None for preview and the
        benchmark); see :data:`STALE_TRIGGER_S`."""
        with self._cond:
            self._period_s = period_s if period_s and period_s > 0 else None

    @property
    def stale_images(self) -> int:
        """Images discarded in this grab because their camera timestamp showed
        them older than the trigger they would have answered."""
        return self._stale_images

    @property
    def stale_triggers(self) -> int:
        """Pending triggers dropped in this grab because they could no longer be
        fired in time."""
        return self._stale_triggers

    @property
    def unanswered_triggers(self) -> int:
        """Fired triggers given up on at their answer deadline in this grab."""
        return self._unanswered_triggers

    def restart_trigger_sequence(self) -> None:
        """Forget pending triggers and number the next one 0: counting starts.

        A priming trigger still awaiting its image stays outstanding at its short
        deadline, and its image reads :data:`PRIMING_TRIGGER`."""
        with self._cond:
            self._pending = 0
            self._pending_seqs.clear()
            self._pending_times.clear()
            self._next_seq = 0
            self._epoch += 1
            self._counting = True
            # The train's first trigger must fire on time, not wait out a drain
            # fetch (the post-priming settle has drained the priming images).
            self._drain_until = 0.0
            # Priming gave a trigger up: its answers may be a stalled image's.
            if self._expired_in_grab:
                self._offsets.clear()

    # --------------------------------------------------------------- triggering

    def _bump_trigger(self) -> None:
        """One pending trigger, without device I/O; dropped at :data:`PENDING_MAX`
        and when not grabbing."""
        with self._cond:
            if not self._grabbing:
                return
            seq = self._next_seq
            self._next_seq += 1
            if self._pending < PENDING_MAX:
                self._pending += 1
                self._pending_seqs.append(seq)
                self._pending_times.append(time.monotonic())
                self._cond.notify()
            else:
                self._dropped_triggers += 1
                if self._dropped_triggers % 100 == 1:
                    log.warning(
                        "Camera %s: dropping software triggers (%d so far) — the "
                        "requested fps exceeds what the camera can deliver; it is "
                        "running at its maximum rate.",
                        getattr(self, "_serial", "?"),
                        self._dropped_triggers,
                    )

    # ----------------------------------------------------------------- grabbing

    def _begin_grab(self) -> None:
        """Arm the hand-off when streaming starts (resets the backlog)."""
        with self._cond:
            self._pending = 0
            self._pending_seqs.clear()
            self._pending_times.clear()
            self._next_seq = 0
            self._outstanding.clear()
            self._answer = None
            self._epoch += 1
            self._answered_in_grab = False
            self._counting = False
            self._unanswered_triggers = 0
            self._stale_triggers = 0
            self._offsets.clear()
            self._stale_images = 0
            self._rejected_for = None
            self._drain_until = 0.0
            self._expired_in_grab = False
            self._grabbing = True

    def _end_grab(self) -> bool:
        """Disarm and wake a ``retrieve`` parked in :meth:`_claim_trigger`; call
        it before the native stop. True if it ended a live grab."""
        with self._cond:
            was_grabbing = self._grabbing
            self._grabbing = False
            self._cond.notify_all()
            return was_grabbing

    def _claim_trigger(self, timeout_ms: int) -> bool | None:
        """What this ``retrieve`` does: True fire then fetch, False only fetch (a
        trigger is outstanding, or a drain), None nothing (no trigger within
        ``timeout_ms``, or not grabbing). A claimed trigger is outstanding at
        once, so a failed device call must report :meth:`_trigger_unfired`.
        """
        with self._cond:
            self._drain_poll = False
            if not self._grabbing:
                return None
            self._expire_unanswered()
            if self._outstanding:
                return False
            self._drop_stale_pending()
            if self._pending <= 0 and time.monotonic() < self._drain_until:
                self._drain_poll = True
                return False  # nothing to fire: fetch a given-up trigger's late image
            if self._pending <= 0:
                self._cond.wait(timeout_ms / 1000.0)
            if self._pending <= 0 or not self._grabbing:
                return None
            self._pending -= 1
            seq = self._pending_seqs.popleft() if self._pending_seqs else None
            if self._pending_times:
                self._pending_times.popleft()
            patient = self._answered_in_grab or self._counting
            timeout = ANSWER_TIMEOUT_S if patient else PRIMING_ANSWER_TIMEOUT_S
            if self._period_s:
                timeout = max(timeout, 2 * self._period_s)
            deadline = time.monotonic() + timeout
            self._outstanding.append(
                (seq, self._epoch, deadline, self._counting, time.monotonic_ns())
            )
            return True

    def _fetch_timeout_ms(self, timeout_ms: int) -> int:
        """This fetch's timeout: a :data:`DRAIN_POLL_MS` poll for a drain."""
        return min(timeout_ms, DRAIN_POLL_MS) if self._drain_poll else timeout_ms

    def _trigger_unfired(self) -> None:
        """The device refused the trigger just claimed: no image will answer it,
        so it is not outstanding (its pulse is a gap in the sequence)."""
        with self._cond:
            if self._outstanding:
                self._outstanding.pop()

    def _trigger_answered(self, timestamp_ns: int | None = None) -> None:
        """An image was fetched, usable or not: it answers the oldest outstanding
        trigger, unless its timestamp (ns, else None) shows it older than that
        trigger (see :data:`STALE_IMAGE_TOLERANCE_NS`)."""
        with self._cond:
            if not self._outstanding:
                self._answer = (UNMATCHED_TRIGGER, self._epoch)
                return
            seq, epoch, _deadline, _counted, fired_ns = self._outstanding[0]
            offset = timestamp_ns - fired_ns if timestamp_ns else None
            if (
                offset is not None
                and len(self._offsets) >= REFERENCE_MIN_SAMPLES
                and offset < _median(self._offsets) - STALE_IMAGE_TOLERANCE_NS
            ):
                self._answer = (UNMATCHED_TRIGGER, self._epoch)
                self._rejected_for = (seq, epoch)
                self._stale_images += 1
                if self._stale_images % 100 == 1:
                    log.warning(
                        "Camera %s: an image arrived after its trigger was given up "
                        "on (%d so far); discarded, so it cannot answer a later one",
                        getattr(self, "_serial", "?"),
                        self._stale_images,
                    )
                return
            self._outstanding.popleft()
            self._answer = (seq, epoch)
            self._answered_in_grab = True
            if offset is not None:
                self._offsets.append(offset)

    def _wait_pending(self, timeout_ms: int) -> bool:
        """Consume one pending trigger without awaiting an answer (the fake's model
        of a hardware trigger line), within ``timeout_ms``."""
        with self._cond:
            if self._pending <= 0 and self._grabbing:
                self._cond.wait(timeout_ms / 1000.0)
            if self._pending <= 0 or not self._grabbing:
                return False
            self._pending -= 1
            seq = self._pending_seqs.popleft() if self._pending_seqs else None
            if self._pending_times:
                self._pending_times.popleft()
            self._answer = (seq, self._epoch)
            return True

    def _drop_stale_pending(self) -> None:
        """Drop pending triggers too old to fire while a recording counts (caller
        holds the condition); their pulses are missed. Priming never drops: it
        waits out an ignored trigger's deadline by design."""
        if not self._period_s or not self._counting:
            return
        limit = max(STALE_TRIGGER_S, 0.5 * self._period_s)
        now = time.monotonic()
        while self._pending > 0 and self._pending_times and (
            now - self._pending_times[0] > limit
        ):
            self._pending -= 1
            self._pending_seqs.popleft()
            self._pending_times.popleft()
            self._stale_triggers += 1
            if self._stale_triggers % 100 == 1:
                log.warning(
                    "Camera %s: dropped %d software trigger(s) that could no longer "
                    "be fired in time (the camera was still waiting on an earlier "
                    "image); their pulses are missed and filled",
                    getattr(self, "_serial", "?"),
                    self._stale_triggers,
                )

    def _expire_unanswered(self) -> None:
        """Give up on the outstanding triggers past their answer deadline (the
        caller holds the condition)."""
        now = time.monotonic()
        while self._outstanding and now > self._outstanding[0][2]:
            seq, epoch, _deadline, counted, _fired = self._outstanding.popleft()
            if self._rejected_for == (seq, epoch):
                # The image rejected for it was probably its own.
                self._offsets.clear()
                self._rejected_for = None
            window = max(DRAIN_WINDOW_S, 2 * self._period_s) if self._period_s else DRAIN_WINDOW_S
            self._drain_until = max(self._drain_until, now + window)
            self._expired_in_grab = True
            if not counted:
                continue  # ignored before the recording counts: expected
            self._unanswered_triggers += 1
            if self._unanswered_triggers % 100 == 1:
                log.warning(
                    "Camera %s: no image arrived for a software trigger within "
                    "%.1f s (%d so far); firing the next one",
                    getattr(self, "_serial", "?"),
                    ANSWER_TIMEOUT_S,
                    self._unanswered_triggers,
                )
