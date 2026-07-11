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

    if not self._wait_pending(timeout_ms):
        return None
    # ... fire exactly one device software-trigger, then fetch exactly one frame

which keeps the invariant **one frame recorded per trigger fired**: every drained
pending count fires one FrameStart trigger (one exposure) and fetches one frame.

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

log = logging.getLogger("octacam")

# Small backlog bound: one trigger draining, one queued. Keeps recorded-frame
# host timestamps essentially real-time instead of replaying a stale backlog when
# the requested fps exceeds what the camera can deliver.
PENDING_MAX = 2


class SoftwareTriggerHandoff:
    """Mixin: the counter-based trigger hand-off shared by every backend.

    A backend mixes this in, calls :meth:`_init_trigger_handoff` from ``__init__``,
    bumps in ``trigger_once`` via :meth:`_bump_trigger`, flips the grabbing flag in
    its start/stop with :meth:`_begin_grab` / :meth:`_end_grab`, and gates its
    ``retrieve`` on :meth:`_wait_pending`. The ``_grabbing`` flag it manages is the
    single source of truth for :meth:`is_grabbing` — a lock-protected Python bool,
    never a native SDK query on the trigger thread's wait predicate.
    """

    def _init_trigger_handoff(self) -> None:
        self._cond = threading.Condition()
        self._pending = 0
        self._grabbing = False
        self._dropped_triggers = 0

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
            if self._pending < PENDING_MAX:
                self._pending += 1
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
            self._grabbing = True

    def _end_grab(self) -> bool:
        """Disarm and wake any blocked ``retrieve`` — call BEFORE the native stop.

        Setting ``_grabbing`` False and notifying under the condition wakes a
        ``retrieve`` parked in :meth:`_wait_pending` immediately (so stop latency
        is not the full grab timeout), and it re-checks the flag and returns None.
        Returns ``True`` if it flipped a live grab (so a caller can skip a
        redundant native stop), ``False`` if already stopped.
        """
        with self._cond:
            was_grabbing = self._grabbing
            self._grabbing = False
            self._cond.notify_all()
            return was_grabbing

    def _wait_pending(self, timeout_ms: int) -> bool:
        """Block for one pending trigger; consume it and return True, else False.

        Bounded by ``timeout_ms`` (the grab loop's ``GRAB_TIMEOUT_MS``), so even
        without a notify the wait always returns and the loop can re-check
        ``is_grabbing``. Decrements the counter up front: a trigger whose device
        call or fetch then fails is simply a lost frame (the same accepted
        behaviour the SDKs have on a grab timeout), not a desync.
        """
        with self._cond:
            if self._pending <= 0 and self._grabbing:
                self._cond.wait(timeout_ms / 1000.0)
            if self._pending <= 0 or not self._grabbing:
                return False
            self._pending -= 1
            return True
