"""The software-trigger hand-off's pairing (cameras/_trigger_handoff.py), through
its public API, on a hand-driven clock so every deadline is exact."""

import logging

import pytest

from octacam.cameras._trigger_handoff import (
    ANSWER_TIMEOUT_S,
    DRAIN_POLL_MS,
    DRAIN_WINDOW_S,
    PENDING_MAX,
    PRIMING_ANSWER_TIMEOUT_S,
    PRIMING_TRIGGER,
    REFERENCE_MIN_SAMPLES,
    UNMATCHED_TRIGGER,
    SoftwareTrigger,
)

CAMERA_AHEAD_NS = 5_000_000_000  # the camera clock reads 5 s ahead of the host's


class Clock:
    """A monotonic ns clock that moves only when told to."""

    def __init__(self):
        self.ns = 1_000_000_000_000

    def __call__(self) -> int:
        return self.ns

    def advance(self, seconds: float) -> None:
        self.ns += int(seconds * 1e9)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def trigger(clock):
    t = SoftwareTrigger("test-handoff", clock)
    t.begin_grab()
    return t


def _fire(trigger) -> None:
    """Offer one trigger and claim it for firing."""
    trigger.offer()
    assert trigger.claim(10) is True


def _answer_after(trigger, clock, delay_ns: int) -> None:
    """Fire one trigger and answer it with an image exposed ``delay_ns`` after."""
    _fire(trigger)
    trigger.answered(clock.ns + CAMERA_AHEAD_NS + delay_ns)


def test_fires_nothing_while_a_trigger_awaits_its_image(trigger):
    _fire(trigger)  # trigger 0
    trigger.offer()
    assert trigger.claim(10) is False  # 0 unanswered: fetch only
    assert trigger.claim(10) is False
    trigger.answered()
    assert trigger.last_index == 0
    assert trigger.claim(10) is True  # trigger 1 fires now
    trigger.answered()
    assert trigger.last_index == 1


def test_gives_up_on_a_trigger_past_its_answer_deadline(trigger, clock):
    trigger.restart_sequence()  # a recording counts: the long deadline applies
    _fire(trigger)
    trigger.offer()
    assert trigger.claim(10) is False
    clock.advance(ANSWER_TIMEOUT_S - 0.01)
    assert trigger.claim(10) is False  # still within the deadline
    clock.advance(0.02)
    assert trigger.claim(10) is True  # 0 given up on; 1 fires
    trigger.answered()
    assert trigger.last_index == 1 and trigger.unanswered_triggers == 1
    # A late image of the abandoned trigger answers nothing outstanding.
    trigger.answered()
    assert trigger.last_index == UNMATCHED_TRIGGER


def test_a_silent_camera_costs_only_the_short_deadline_until_it_answers(
    trigger, clock
):
    # A Grasshopper3 silently ignores its first triggers after acquisition start:
    # before the grab's first answer (and before a recording counts) each costs
    # only PRIMING_ANSWER_TIMEOUT_S and is not reported as unanswered. After the
    # first answer a silent trigger may be a late image: the long deadline.
    _fire(trigger)  # ignored by the camera
    trigger.offer()
    clock.advance(PRIMING_ANSWER_TIMEOUT_S + 0.01)
    assert trigger.claim(10) is True  # 0 given up on after the short deadline
    assert trigger.unanswered_triggers == 0  # expected, not reported
    trigger.answered()
    assert trigger.last_index == 1
    _fire(trigger)
    trigger.offer()
    clock.advance(PRIMING_ANSWER_TIMEOUT_S + 0.01)
    assert trigger.claim(10) is False  # now patient: 2 may still be on its way


def test_a_period_stretches_the_deadline_to_two_periods(trigger, clock):
    # At 5 fps a 150 ms exposure outlasts the 0.1 s priming window: a recording's
    # period makes every deadline at least two periods.
    trigger.configure_period(0.2)
    _fire(trigger)
    trigger.offer()
    clock.advance(0.39)
    assert trigger.claim(10) is False  # still awaiting its image
    clock.advance(0.02)
    assert trigger.claim(10) is True  # given up on after two periods


def test_an_image_older_than_its_trigger_answers_nothing(trigger, clock):
    # The camera clock says when an image was exposed: one exposed before the
    # trigger it would answer was fired is a late image of a trigger already
    # given up on. It answers nothing; the trigger keeps waiting for its own.
    trigger.restart_sequence()
    for _ in range(10):
        _answer_after(trigger, clock, 1_000_000)  # exposed 1 ms after firing
        clock.advance(0.02)
    _fire(trigger)
    trigger.answered(clock.ns + CAMERA_AHEAD_NS - 200_000_000)  # 0.2 s before
    assert trigger.last_index == UNMATCHED_TRIGGER
    assert trigger.stale_images == 1 and trigger.fired_index == 10
    trigger.answered(clock.ns + CAMERA_AHEAD_NS + 1_000_000)  # its own image
    assert trigger.last_index == 10 and trigger.fired_index is None


def test_trusts_no_reference_of_too_few_answers_and_heals_a_bad_one(trigger, clock):
    trigger.restart_sequence()
    _answer_after(trigger, clock, 200_000_000)  # inflated by a 0.2 s host stall
    _answer_after(trigger, clock, 1_000_000)  # normal: too few answers to judge it
    assert trigger.last_index == 1 and trigger.stale_images == 0
    # A reference gone wrong (8 inflated answers): the normal image is rejected,
    # its trigger is given up on, and the reference is cleared, not trusted.
    for _ in range(8):
        _answer_after(trigger, clock, 200_000_000)
    _answer_after(trigger, clock, 1_000_000)
    assert trigger.stale_images == 1 and trigger.fired_index == 10
    clock.advance(ANSWER_TIMEOUT_S + 0.01)
    trigger.offer()
    assert trigger.claim(10) is True  # 10 given up on; 11 fires
    trigger.answered(clock.ns + CAMERA_AHEAD_NS + 1_000_000)
    assert trigger.last_index == 11 and trigger.stale_images == 1


def test_a_priming_give_up_clears_the_reference_at_counting_start(trigger, clock):
    # Priming's answers may be a stalled image's (here all 0.2 s late): once
    # priming gave a trigger up, counting starts with no reference, so the
    # train's first normal image is not judged stale against them.
    for _ in range(REFERENCE_MIN_SAMPLES):
        _answer_after(trigger, clock, 200_000_000)
    _fire(trigger)
    clock.advance(ANSWER_TIMEOUT_S + 0.01)
    assert trigger.claim(10) is False  # given up on: a drain
    trigger.restart_sequence()
    _answer_after(trigger, clock, 1_000_000)
    assert trigger.last_index == 0 and trigger.stale_images == 0


def test_drops_a_trigger_left_pending_for_half_a_period(trigger, clock):
    # While a recording counts at a low rate, a trigger left pending behind an
    # unanswered one must be dropped once it is half a period old — not fired a
    # whole period late under its own pulse number.
    trigger.configure_period(0.2)  # deadline max(1.0, 2P) = 1 s; stale after 0.1 s
    trigger.restart_sequence()
    _fire(trigger)  # never answered
    clock.advance(0.2)
    trigger.offer()  # due now; it will wait out the deadline behind trigger 0
    clock.advance(ANSWER_TIMEOUT_S - 0.2 + 0.01)
    # 0 given up on, 1 too stale to fire: dropped; with nothing left to fire the
    # grab loop only fetches, in case 0's image is merely late.
    assert trigger.claim(10) is False
    assert trigger.stale_triggers == 1 and trigger.unanswered_triggers == 1
    assert trigger.fired_index is None and trigger.pending == 0


def test_priming_never_drops_a_pending_trigger(trigger, clock):
    trigger.configure_period(0.02)
    _fire(trigger)
    trigger.offer()
    clock.advance(PRIMING_ANSWER_TIMEOUT_S + 0.01)
    assert trigger.claim(10) is True  # waited out the ignored one, not dropped
    assert trigger.stale_triggers == 0


def test_after_a_give_up_an_idle_loop_drains_with_a_short_poll(trigger, clock):
    # A given-up trigger's image may still be in the buffer: for the drain window
    # an idle claim asks for a poll fetch, whose answer matches nothing.
    trigger.restart_sequence()
    _fire(trigger)
    clock.advance(ANSWER_TIMEOUT_S + 0.01)
    assert trigger.claim(10) is False  # 0 given up on; nothing pending: drain
    assert trigger.fetch_timeout_ms(100) == DRAIN_POLL_MS
    trigger.answered()
    assert trigger.last_index == UNMATCHED_TRIGGER
    clock.advance(DRAIN_WINDOW_S + 0.01)
    assert trigger.claim(10) is None  # the drain is over: wait for a trigger
    assert trigger.fetch_timeout_ms(100) == 100


def test_a_period_stretches_the_drain_to_two_periods(trigger, clock):
    trigger.configure_period(0.2)
    trigger.restart_sequence()
    _fire(trigger)
    clock.advance(ANSWER_TIMEOUT_S + 0.01)
    assert trigger.claim(10) is False  # given up on: the drain starts
    clock.advance(DRAIN_WINDOW_S + 0.05)  # past the window, short of 2 periods
    assert trigger.claim(10) is False
    assert trigger.fetch_timeout_ms(100) == DRAIN_POLL_MS
    clock.advance(0.4 - DRAIN_WINDOW_S)
    assert trigger.claim(10) is None


def test_counting_start_ends_a_drain(trigger, clock):
    _fire(trigger)
    clock.advance(PRIMING_ANSWER_TIMEOUT_S + 0.01)
    assert trigger.claim(10) is False  # a priming give-up starts a drain
    trigger.restart_sequence()
    trigger.offer()
    assert trigger.claim(10) is True  # the train's first trigger fires at once
    assert trigger.fetch_timeout_ms(100) == 100


def test_a_trigger_the_device_refused_is_not_outstanding(trigger):
    _fire(trigger)
    trigger.unfired()
    trigger.offer()
    assert trigger.claim(10) is True  # nothing to wait for
    trigger.answered()
    assert trigger.last_index == 1


def test_an_answer_to_a_trigger_from_before_the_restart_is_priming(trigger):
    _fire(trigger)  # a priming trigger, image still due
    trigger.restart_sequence()
    trigger.offer()  # the recording's trigger 0
    assert trigger.claim(10) is False  # the priming image comes first
    trigger.answered()
    assert trigger.last_index == PRIMING_TRIGGER
    assert trigger.claim(10) is True
    trigger.answered()
    assert trigger.last_index == 0
    # An image answered before the restart reads as priming, however late the
    # reader looks.
    _fire(trigger)
    trigger.answered()
    trigger.restart_sequence()
    assert trigger.last_index == PRIMING_TRIGGER


def test_pending_max_drops_the_newest_and_numbers_it(trigger):
    for _ in range(PENDING_MAX + 3):
        trigger.offer()
    assert trigger.pending == PENDING_MAX and trigger.dropped_triggers == 3
    assert trigger.next_index == PENDING_MAX + 3  # dropped ones are numbered too
    assert trigger.claim(10) is True
    trigger.answered()
    assert trigger.last_index == 0  # the oldest fires first


def _warnings(caplog, text: str) -> int:
    return sum(
        text in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )


def test_overflow_warns_on_the_first_drop_and_every_hundredth(trigger, caplog):
    caplog.set_level(logging.WARNING, logger="octacam")
    for _ in range(PENDING_MAX + 100):
        trigger.offer()
    assert trigger.dropped_triggers == 100
    assert _warnings(caplog, "dropping software triggers") == 1
    trigger.offer()
    assert _warnings(caplog, "dropping software triggers") == 2


def test_each_lost_trigger_kind_warns_once(trigger, clock, caplog):
    # Unanswered, stale-pending and stale-image losses share the rate limit; each
    # warns on its first event.
    caplog.set_level(logging.WARNING, logger="octacam")
    trigger.configure_period(0.2)
    trigger.restart_sequence()
    _fire(trigger)  # never answered
    clock.advance(0.2)
    trigger.offer()  # left pending behind it
    clock.advance(ANSWER_TIMEOUT_S)
    assert trigger.claim(10) is False
    assert trigger.unanswered_triggers == 1 and trigger.stale_triggers == 1
    for _ in range(REFERENCE_MIN_SAMPLES):
        _answer_after(trigger, clock, 1_000_000)
    _fire(trigger)
    trigger.answered(clock.ns + CAMERA_AHEAD_NS - 200_000_000)
    assert trigger.stale_images == 1
    for text in ("no image arrived", "could no longer", "arrived after its trigger"):
        assert _warnings(caplog, text) == 1, text


def test_offers_outside_a_grab_are_dropped(trigger):
    trigger.end_grab()
    trigger.offer()
    assert trigger.pending == 0 and trigger.claim(10) is None
    trigger.begin_grab()
    assert trigger.pending == 0 and trigger.last_index is None


def test_take_consumes_a_trigger_as_answered(trigger):
    assert trigger.take(1) is None
    trigger.offer()
    trigger.offer()
    assert trigger.take(1) == 0 and trigger.take(1) == 1
    assert trigger.last_index == 1 and trigger.fired_index is None
