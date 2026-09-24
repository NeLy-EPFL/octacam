"""Shared software-trigger hand-off between the trigger timer and grab loops.

Every hardware backend is driven by the same shared :class:`~octacam.trigger.
PreciseTimer`: it calls :meth:`~octacam.cameras.base.Camera.trigger_once` on
*every* camera, serially, from one thread. If ``trigger_once`` did the device
software-trigger there (a native, GIL-holding SDK call), a camera that is slow
to accept a trigger — a FLIR whose exposure pipeline is busy — would block that
shared thread and delay *every other* camera's trigger. On a mixed rig this
measurably drags a fast camera down (a 100 fps Basler fell to ~82 fps behind two
slow FLIRs, the shared tick going from 0.7 ms to 2.6 ms p50 / 7.3 ms max).

The fix, already used by the fake and pycameleon backends and generalised here so
all backends behave identically: ``trigger_once`` only **bumps a counter** under a
:class:`threading.Condition` (cheap, no device access), and each camera's own
grab-loop :meth:`retrieve` drains one pending trigger and fires the device
software-trigger itself. The shared timer thread therefore never touches a device,
so no camera can throttle another.

Backend ``retrieve`` bodies become::

    fire = self._claim_trigger(timeout_ms)
    if fire is None:
        return None
    if fire and not <fire exactly one device software-trigger>:
        self._trigger_unfired()
        return None
    # ... fetch at most one frame; as soon as the SDK hands over an image (an
    # incomplete one too — it answers its trigger all the same):
    self._trigger_answered()

which keeps the invariant **one frame recorded per trigger fired**: every drained
pending count fires one FrameStart trigger (one exposure), and the next image
fetched is paired with it.

Trigger sequence
    Every trigger offered while grabbing gets a sequence number (0, 1, 2, … from
    the grab start, or from :meth:`SoftwareTriggerHandoff.restart_trigger_sequence`),
    including the ones the overflow policy drops. :attr:`last_trigger_index` is
    the number of the trigger the latest fetched image answers, so the recording's
    pulse accounting knows exactly which trigger each frame belongs to — a dropped
    trigger, or one whose image never arrived, shows up as a gap in the sequence.

Pairing images with triggers
    An image can arrive after its fetch timed out (a long exposure, a stalled USB
    transfer). If the next ``retrieve`` fired the next trigger regardless, it
    would fetch the late image and credit it to the *next* trigger, and every
    later frame would be labeled one pulse late. So a fired trigger stays
    **outstanding** until an image answers it, and while one is outstanding and
    younger than :data:`ANSWER_TIMEOUT_S`, ``retrieve`` fires nothing and only
    fetches: an image always answers the oldest outstanding trigger. Past the
    deadline the trigger is given up on (its pulse is then a gap in the
    sequence, i.e. a missed pulse) and firing resumes. In the normal case the
    image arrives within the fetch that follows its trigger, so nothing changes.
    A trigger fired before :meth:`~SoftwareTriggerHandoff.restart_trigger_sequence`
    and answered after it reports :data:`PRIMING_TRIGGER`: the recording discards
    that image as a priming answer rather than counting it as pulse 0.

    A Grasshopper3 ignores its first software triggers after acquisition start,
    like its hardware ones, silently (no image, no error): at the long deadline
    each cost a second (measured: priming answered nothing, and the stale priming
    trigger then blocked the train's first 37 pulses at 50 fps). So until a grab's
    first answer, and before a recording counts, a trigger gets only
    :data:`PRIMING_ANSWER_TIMEOUT_S` — that is the ignore window, and no frame is
    counted yet. Once the camera has answered once, or the recording counts, a
    silent trigger is more likely a late image, which must keep its own trigger:
    :data:`ANSWER_TIMEOUT_S`. Only triggers fired while counting are reported as
    unanswered.

Overflow policy (:data:`PENDING_MAX`)
    Requesting a higher fps than a camera can deliver (its exposure + readout is
    longer than the trigger period) would make the counter grow without bound.
    That buys nothing: the grab loop has *no* drain-on-stop, so a trigger still
    pending (or fired-but-unfetched) when recording stops is discarded from the
    recording regardless — an unbounded backlog would only replay stale frames
    and hide the misconfiguration. So the counter is **capped** at
    :data:`PENDING_MAX` and excess triggers are dropped-newest with a rate-limited
    warning, turning a silent fps shortfall into a visible one and letting an
    over-provisioned rig degrade cleanly to the camera's true maximum rate. The
    cap only ever drops a trigger that was *never fired at the device*, so it can
    never make ``frames_recorded`` exceed the number of triggers actually fired.
"""

import logging
import threading
import time
from collections import deque

log = logging.getLogger("octacam")

# Small backlog bound: one trigger draining, one queued. Keeps recorded-frame
# host timestamps essentially real-time instead of replaying a stale backlog when
# the requested fps exceeds what the camera can deliver.
PENDING_MAX = 2
# How long a fired software trigger may go unanswered before its image is given
# up on and the next trigger fires. It must outlast the slowest legitimate answer
# — the exposure (at most a period of the slowest rate anyone records at), plus
# readout and USB transfer (~11 ms for a full GS3 frame), plus a host or bus stall
# — because an image later than this is credited to the next trigger. It is also
# what an image that never arrives costs: every pulse in that window is missed
# (and filled). 1 s covers exposures up to ~0.9 s and bounds that loss to a
# second; an image dropped without a word is rare (a transport failure normally
# comes back as an incomplete image, which answers its trigger at once).
ANSWER_TIMEOUT_S = 1.0
# The deadline in a camera's ignore window — before a grab's first answer, while
# no recording counts (see "Pairing images with triggers"): one grab-loop fetch,
# as before the pairing. The controller's post-priming settle outlasts it.
PRIMING_ANSWER_TIMEOUT_S = 0.1
# A recording's trigger period stretches both deadlines to at least two periods
# (see configure_trigger_period): at a low rate the exposure alone can outlast
# them. And while a recording counts, a pending trigger older than max(this, half
# a period) is dropped instead of fired: fired that late, its image would show a
# moment nearer the next pulse than the one it is labeled with (a trigger kept
# pending through an answer wait fired up to a second late).
STALE_TRIGGER_S = 0.02
# An image answering a trigger is checked against the camera's own clock: its
# timestamp minus the host time its trigger fired is a near-constant offset (the
# clocks' difference plus the fire-to-exposure delay, which varies by a few ms).
# An image older than the trigger it would answer — the late image of a trigger
# already given up on — shows an offset lower by at least the gap between the two
# fires, which is an answer deadline (>= 0.1 s) in every way that happens. So an
# image more than this far below the median offset of the last OFFSET_WINDOW
# answers is stale: it answers nothing, and its trigger keeps waiting for its own.
# The fire time is taken as the trigger is claimed, before the device call, so a
# host stall can only raise a real image's offset, never lower it; the median
# shrugs off such outliers. (Timestamps must be in ns; a backend passes one only
# when they are.)
STALE_IMAGE_TOLERANCE_NS = 50_000_000
OFFSET_WINDOW = 64
# The offset check sees an image older than its trigger by more than the
# tolerance, not a steady one-trigger shift (a period, 20 ms at 50 fps). That
# shift starts when a late image is still in the buffer while nothing is
# outstanding — the grab loop does not fetch when it has nothing to fire, so the
# image waits for the next trigger's fetch. So for max(this, two periods) after a
# trigger is given up on, an idle grab loop fetches anyway: anything it gets
# answers nothing and is discarded. (A priming round whose first image stalled
# leaves its late images to this drain during the post-priming settle.)
DRAIN_WINDOW_S = 0.25
# A drain fetch polls: a real SDK fetch blocks for its whole timeout when no image
# comes, and a trigger offered meanwhile would wait it out (and be dropped as
# stale, or fire late). See _fetch_timeout_ms.
DRAIN_POLL_MS = 5
# ``last_trigger_index`` of an image that answers no trigger of the current
# sequence: one fired before ``restart_trigger_sequence`` (a recording's priming
# trigger whose image arrived after counting started) ...
PRIMING_TRIGGER = -1
# ... or one no outstanding trigger accounts for (e.g. the late image of a
# trigger already given up on at its answer deadline).
UNMATCHED_TRIGGER = -2


def _median(values: deque[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


class SoftwareTriggerHandoff:
    """Mixin: the counter-based trigger hand-off shared by every backend.

    A backend mixes this in, calls :meth:`_init_trigger_handoff` from ``__init__``,
    bumps in ``trigger_once`` via :meth:`_bump_trigger`, flips the grabbing flag in
    its start/stop with :meth:`_begin_grab` / :meth:`_end_grab`, and drives its
    ``retrieve`` with :meth:`_claim_trigger`, :meth:`_trigger_unfired` and
    :meth:`_trigger_answered` (see the module docstring). The ``_grabbing`` flag
    it manages is the single source of truth for :meth:`is_grabbing` — a
    lock-protected Python bool, never a native SDK query on the trigger thread's
    wait predicate.
    """

    def _init_trigger_handoff(self) -> None:
        self._cond = threading.Condition()
        self._pending = 0
        # Sequence numbers of the pending triggers (parallel to _pending) and the
        # next number to hand out.
        self._pending_seqs: deque[int] = deque()
        # When each pending trigger was offered (parallel to _pending_seqs).
        self._pending_times: deque[float] = deque()
        self._next_seq = 0
        # The recording's trigger period, when one counts (configure_trigger_period).
        self._period_s: float | None = None
        self._stale_triggers = 0
        # Camera timestamp minus host fire time of the latest answers (see
        # STALE_IMAGE_TOLERANCE_NS), and the images found older than their trigger.
        self._offsets: deque[int] = deque(maxlen=OFFSET_WINDOW)
        self._stale_images = 0
        # Until when an idle grab loop fetches anyway (see DRAIN_WINDOW_S), and
        # whether this grab has given up on any trigger yet.
        self._drain_until = 0.0
        self._expired_in_grab = False
        # Whether the fetch the latest claim asked for is a drain poll.
        self._drain_poll = False
        # Fired triggers no image has answered yet, oldest first, as (sequence
        # number, sequence epoch, monotonic answer deadline, counted, monotonic
        # fire time in ns). The epoch advances with every sequence restart, so an
        # answer to an earlier sequence's trigger is recognized as one; ``counted``
        # is whether it was fired while a recording counts (only those are
        # reported unanswered).
        self._outstanding: deque[tuple[int | None, int, float, bool, int]] = deque()
        self._epoch = 0
        # Whether this grab has had a real answer yet, and whether a recording
        # counts its triggers (restart_trigger_sequence): together they pick a
        # fired trigger's answer deadline (see "Pairing images with triggers").
        self._answered_in_grab = False
        self._counting = False
        # (sequence number, epoch) of the trigger the latest image answered.
        self._answer: tuple[int | None, int] | None = None
        self._grabbing = False
        self._dropped_triggers = 0
        self._unanswered_triggers = 0

    @property
    def last_trigger_index(self) -> int | None:
        """Sequence number of the trigger the latest fetched image answers.

        None until an image answers one in this grab (e.g. on a
        hardware-triggered fetch, which never draws on the hand-off);
        :data:`PRIMING_TRIGGER` when the sequence restarted after the trigger it
        answers was fired, and :data:`UNMATCHED_TRIGGER` when no fired trigger
        accounts for it."""
        answer = self._answer
        if answer is None:
            return None
        seq, epoch = answer
        if seq is not None and epoch != self._epoch:
            return PRIMING_TRIGGER
        return seq

    def configure_trigger_period(self, period_s: float | None) -> None:
        """The period this camera's software triggers come at, when a recording
        counts them (None otherwise — preview, benchmark). Stretches the answer
        deadlines to at least two periods and drops pending triggers too stale to
        fire (see :data:`STALE_TRIGGER_S`)."""
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
        """Forget pending triggers and number the next one 0.

        Called when a recording starts counting after the cameras were primed,
        so the recording's first trigger is sequence 0 in every camera. A
        priming trigger still awaiting its image stays outstanding — nothing is
        fired until it is answered or given up on, at its own short deadline — and
        that image then reports :data:`PRIMING_TRIGGER`. Triggers fired from here
        on are counted, so they get the long :data:`ANSWER_TIMEOUT_S`."""
        with self._cond:
            self._pending = 0
            self._pending_seqs.clear()
            self._pending_times.clear()
            self._next_seq = 0
            self._epoch += 1
            self._counting = True
            # The train's first trigger must fire on time: a drain fetch blocks
            # for a whole fetch timeout, and its trigger would then be dropped as
            # stale (the post-priming settle has drained the priming images).
            self._drain_until = 0.0
            # A fresh reference for the counted answers when priming gave up on a
            # trigger: its answers may be a stalled first image's, and would make
            # a stale offset look normal.
            if self._expired_in_grab:
                self._offsets.clear()

    # --------------------------------------------------------------- triggering

    def _bump_trigger(self) -> None:
        """Record one pending software trigger (called from ``trigger_once``).

        Does no device I/O, so the shared trigger thread never blocks here. Drops
        the trigger (with a rate-limited warning) once the backlog is at
        :data:`PENDING_MAX`, and is a no-op when not grabbing — matching the old
        ``if is_grabbing()`` guard without a native call.
        """
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
            self._drain_until = 0.0
            self._expired_in_grab = False
            self._grabbing = True

    def _end_grab(self) -> bool:
        """Disarm and wake any blocked ``retrieve`` — call BEFORE the native stop.

        Setting ``_grabbing`` False and notifying under the condition wakes a
        ``retrieve`` parked in :meth:`_claim_trigger` immediately (so stop latency
        is not the full grab timeout), and it re-checks the flag and returns None.
        Returns ``True`` if it flipped a live grab (so a caller can skip a
        redundant native stop), ``False`` if already stopped.
        """
        with self._cond:
            was_grabbing = self._grabbing
            self._grabbing = False
            self._cond.notify_all()
            return was_grabbing

    def _claim_trigger(self, timeout_ms: int) -> bool | None:
        """What this ``retrieve`` does: fire then fetch, only fetch, or nothing.

        False while a fired trigger still awaits its image: fire nothing, only
        fetch, so a late image answers its own trigger. Otherwise blocks up to
        ``timeout_ms`` (the grab loop's ``GRAB_TIMEOUT_MS``, so the loop can always
        re-check ``is_grabbing``) for a pending trigger and returns True once it
        has claimed one — the caller fires it, then fetches; the claimed trigger
        is outstanding from here on, so a device call that then fails must report
        :meth:`_trigger_unfired`. None when there is nothing to do (no trigger
        pending, or not grabbing).
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
        """The timeout for the fetch this ``retrieve`` makes: the caller's, or a
        short poll when the claim only asked for a drain (see
        :data:`DRAIN_POLL_MS`). Called by the grab thread right after its claim."""
        return min(timeout_ms, DRAIN_POLL_MS) if self._drain_poll else timeout_ms

    def _trigger_unfired(self) -> None:
        """The device refused the trigger just claimed: no image will answer it,
        so it is not outstanding (its pulse is a gap in the sequence)."""
        with self._cond:
            if self._outstanding:
                self._outstanding.pop()

    def _trigger_answered(self, timestamp_ns: int | None = None) -> None:
        """An image was fetched, usable or not: it answers the oldest outstanding
        trigger (see :attr:`last_trigger_index`) — unless its camera timestamp
        (``timestamp_ns``, ns; None for a failed image or a clock in other units)
        shows it older than that trigger (see :data:`STALE_IMAGE_TOLERANCE_NS`)."""
        with self._cond:
            if not self._outstanding:
                self._answer = (UNMATCHED_TRIGGER, self._epoch)
                return
            seq, epoch, _deadline, _counted, fired_ns = self._outstanding[0]
            offset = timestamp_ns - fired_ns if timestamp_ns else None
            if (
                offset is not None
                and self._offsets
                and offset < _median(self._offsets) - STALE_IMAGE_TOLERANCE_NS
            ):
                self._answer = (UNMATCHED_TRIGGER, self._epoch)
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
        """Consume one pending trigger at once, without awaiting its answer.

        For a trigger source whose frames arrive whether or not an earlier one
        was answered — the fake's model of a *hardware* trigger line; a real
        software-trigger ``retrieve`` uses :meth:`_claim_trigger`. Bounded by
        ``timeout_ms`` like it; :attr:`last_trigger_index` then reports the
        consumed trigger.
        """
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
        """Drop the pending triggers too old to fire (only while a recording
        counts, at a known period; the caller holds the condition): their pulses
        become gaps in the sequence, i.e. missed pulses. Priming triggers are
        never stale — they wait out an ignored one's deadline by design."""
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
            _seq, _epoch, _deadline, counted, _fired = self._outstanding.popleft()
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
