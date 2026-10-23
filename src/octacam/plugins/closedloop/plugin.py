"""closedloop: ball-pushing closed loop — real-time ball tracking that lights a
corridor pair's opto channel when its fly pushes the ball (opt-in).

One camera looks down on an arena of corridors, each with a fly and a ball. The
plugin finds every corridor's ball (corridors are numbered left to right in the
raw camera image) and tracks those of the pairs' tracked corridors on that
camera's frames. While recording, a push in a tracked corridor — its ball moving
``push_px`` the ``direction`` way from where it rested — switches its pair's opto
channel on: for at least ``min_on_s``, and for as long as the ball keeps moving
(up to MAX_LIGHT_S). A push in the ``refractory_s`` after the light goes off
does not light it. The pair's control corridor shares that light (a yoked
control) and is not tracked. The light itself (pin, pulse train) is the ``opto``
plugin's board::

    [[plugins]]
    name = "closedloop"

    [plugins.options]
    camera = "top"           # the camera to track; default the first
    corridors = 6            # balls to find, numbered left to right
    pairs = [
      { corridor = 1, control = 2, channel = 1 },
      { corridor = 3, control = 4, channel = 2 },
      { corridor = 5, control = 6, channel = 3 },
    ]
    direction = "up"         # "up" / "down" in the image, or "any"
    push_px = 10.0
    min_on_s = 1.0
    refractory_s = 0.0
    enabled = true           # false: track and log pushes, never light
    ball_radius_px = 30.0    # the ball's rim radius in the image

In preview the loop only tracks: pushes show in its tab, the light stays off,
unless the tab's "light in preview" test switch is on (off at every start, and
switched off by a recording's start). A recording logs every tracked frame and
every push to
``octacam_recording/closedloop_frames.csv`` and ``closedloop_events.csv``.
"""

from __future__ import annotations

import csv
import logging
import math
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING

from octacam.plugins.base import Plugin
from octacam.plugins.closedloop.tracker import (
    BALL_RADIUS_PX,
    TRACK_SCORE,
    Ball,
    Lane,
    detect_balls,
)
from octacam.recording_format import RECORDING_INFO_DIRNAME

if TYPE_CHECKING:
    from octacam.cameras.base import Camera, FrameTap, TapFrame
    from octacam.cameras.system import CameraSystem

log = logging.getLogger("octacam")

FRAMES_FILENAME = "closedloop_frames.csv"
EVENTS_FILENAME = "closedloop_events.csv"
DIRECTIONS = ("up", "down", "any")
DEFAULT_PAIRS = ((1, 2, 1), (3, 4, 2), (5, 6, 3))  # (corridor, control, channel)
# The longest light window or refractory a rule may ask for.
MAX_WINDOW_S = 600.0
# Outside a recording, a ball lost this long has the arena re-detected (a new
# or moved arena); a ball found again after this long rests where it is found.
REDETECT_AFTER_S = 1.0
DETECT_INTERVAL_S = 0.5
STATE_PERIOD_S = 0.05  # the tab's and the preview overlay's refresh
TAP_WAIT_S = 0.5
FLUSH_PERIOD_S = 1.0
# A ball still within STILL_PX for REST_S rests: a push is measured from there,
# and a lit pair's light goes off (after min_on_s).
REST_S = 0.25
STILL_PX = 2.0
# A light kept on while its ball moves is extended in steps the board ends by
# itself HOLD_S after the last one (a stalled host cannot leave it on), sent
# once the wanted end has moved EXTEND_STEP_S; never past MAX_LIGHT_S.
HOLD_S = 0.25
EXTEND_STEP_S = 0.1
MAX_LIGHT_S = 60.0


@dataclass
class Rule:
    """What counts as a push and what it gets; the tab edits it between
    recordings."""

    direction: str = "up"
    push_px: float = 10.0
    min_on_s: float = 1.0
    refractory_s: float = 0.0
    enabled: bool = True

    def updated(self, values: dict) -> Rule:
        """This rule with ``values`` (rule fields only) applied; raises
        ValueError naming a bad one."""
        rule = replace(self)
        for key, value in values.items():
            if key == "direction":
                if value not in DIRECTIONS:
                    raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
                rule.direction = value
            elif key == "enabled":
                if not isinstance(value, bool):
                    raise ValueError("enabled must be true or false")
                rule.enabled = value
            elif key in ("push_px", "min_on_s", "refractory_s"):
                number = float(value)
                low = 0.0 if key == "refractory_s" else 1e-3
                if not math.isfinite(number) or not low <= number <= MAX_WINDOW_S:
                    raise ValueError(f"{key} must be between {low:g} and {MAX_WINDOW_S:g}")
                setattr(rule, key, number)
            else:
                raise ValueError(f"unknown rule field {key!r}")
        return rule


RULE_FIELDS = tuple(f.name for f in fields(Rule))


@dataclass(frozen=True)
class Pair:
    """A tracked corridor, the control corridor that shares its light, and the
    opto channel of that light."""

    corridor: int
    control: int | None
    channel: int


class PushDetector:
    """A push is the ball moving ``push_px`` the rule's way from its anchor:
    where it was last pushed to, or where it came to rest further back (a pull,
    or for ``"any"`` anywhere). Only a rest moves the anchor back, so a ball
    knocked back that bounces forward is no push, while small forward nudges
    add up."""

    def __init__(self, y: float | None = None):
        self.anchor = y
        self.progress = 0.0  # px the rule's way from the anchor, never negative
        self._recent: deque[tuple[float, float]] = deque()  # (t, y) over REST_S

    def update(self, t: float, y: float, direction: str, push_px: float) -> float | None:
        """The displacement when ``y`` at ``t`` (s) completes a push, else None."""
        if self.anchor is None:
            self.anchor = y
        recent = self._recent
        recent.append((t, y))
        while t - recent[0][0] > REST_S:
            recent.popleft()
        moved = y - self.anchor
        progress = abs(moved) if direction == "any" else (-moved if direction == "up" else moved)
        if progress < 0 or direction == "any":
            resting = t - recent[0][0] >= 0.9 * REST_S and not self.moving
            if resting and progress < push_px:
                self.anchor, progress = y, 0.0
        self.progress = max(progress, 0.0)
        if progress < push_px:
            return None
        self.anchor, self.progress = y, 0.0
        return progress

    @property
    def moving(self) -> bool:
        """The ball moved more than STILL_PX within the last REST_S."""
        ys = [y for _, y in self._recent]
        return bool(ys) and max(ys) - min(ys) > STILL_PX


@dataclass
class _PairState:
    pair: Pair
    ready_at: float = 0.0  # monotonic s: no light starts before
    lit_since: float | None = None  # while the loop's light is on
    lit_until: float = 0.0  # when the board ends it, unless extended
    forced: bool = False  # an operator override holds the LED on
    pushes: int = 0
    stims: int = 0


class LoopLog:
    """One recording's closed-loop files, written as the frames come so a crash
    keeps what ran. ``frame`` is the recording's pulse, which is its video frame;
    ``timestamp_ns`` the camera's, as in timestamps.npz."""

    def __init__(self, info_dir: Path, corridors: list[int], channels: list[int]):
        info_dir.mkdir(parents=True, exist_ok=True)
        self._files = [
            open(info_dir / FRAMES_FILENAME, "w", newline=""),  # noqa: SIM115
            open(info_dir / EVENTS_FILENAME, "w", newline=""),  # noqa: SIM115
        ]
        self._frames, self._events = (csv.writer(f) for f in self._files)
        self._frames.writerow(
            ["frame", "timestamp_ns", "latency_ms"]
            + [f"c{c}_{k}" for c in corridors for k in ("x", "y", "score")]
            + [f"light{channel}" for channel in channels]
        )
        self._events.writerow(
            ["frame", "timestamp_ns", "corridor", "channel", "event", "displacement_px", "duration_s"]
        )
        self._flushed = time.monotonic()

    def frame(
        self, frame: TapFrame, latency_ms: float, balls: list[Ball | None], lights: list[bool]
    ) -> None:
        row: list = [frame.pulse, frame.timestamp_ns, f"{latency_ms:.2f}"]
        for ball in balls:
            row += ["", "", ""] if ball is None else [f"{ball.x:.2f}", f"{ball.y:.2f}", f"{ball.score:.3f}"]
        self._frames.writerow(row + [int(on) for on in lights])
        now = time.monotonic()
        if now - self._flushed >= FLUSH_PERIOD_S:
            self._flushed = now
            for f in self._files:
                f.flush()

    def event(
        self,
        frame: TapFrame | None,
        corridor: int | None,
        channel: int | None,
        kind: str,
        displacement: float | None = None,
        duration: float | None = None,
    ) -> None:
        self._events.writerow(
            [
                "" if frame is None else frame.pulse,
                "" if frame is None else frame.timestamp_ns,
                corridor or "",
                channel or "",
                kind,
                "" if displacement is None else f"{displacement:.2f}",
                "" if duration is None else f"{duration:.3f}",
            ]
        )

    def close(self) -> None:
        for f in self._files:
            f.close()


class _Tracking:
    """The arena as last detected: every corridor's x, and a lane, a push
    detector and a loss time per tracked corridor (by corridor number)."""

    def __init__(self, corridor_x: list[float], lanes: dict[int, Lane]):
        self.corridor_x = corridor_x
        self.lanes = lanes
        self.detectors = {c: PushDetector(lane.ball.y) for c, lane in lanes.items()}
        self.lost_since: dict[int, float | None] = dict.fromkeys(lanes)


class ClosedLoopPlugin(Plugin):
    """Tracks the balls on one camera's frames (its FrameTap) on its own thread,
    and lights a pair's opto channel through the opto plugin (its LED board)."""

    name = "closedloop"
    web_dir = Path(__file__).parent / "web"

    def __init__(
        self,
        *,
        camera: str | None = None,
        corridors: int = 6,
        pairs: list[Pair] | None = None,
        rule: Rule | None = None,
        ball_radius_px: float = BALL_RADIUS_PX,
    ):
        self.camera_name = camera
        self.corridors = corridors
        self.pairs = pairs if pairs is not None else [Pair(*p) for p in DEFAULT_PAIRS]
        self._configured_pairs = list(self.pairs)
        self.rule = rule or Rule()
        self._configured_rule = replace(self.rule)
        self.ball_radius_px = ball_radius_px
        self.channels = sorted({pair.channel for pair in self.pairs})
        self.tracked = [pair.corridor for pair in self.pairs]
        # Guards what the tab, the hooks and the tracker share: the log, the rule
        # and the pair states.
        self._lock = threading.Lock()
        self._states = {pair.corridor: _PairState(pair) for pair in self.pairs}
        self._log: LoopLog | None = None
        # The tab's test switch: pushes light outside recordings too (unlogged).
        self.preview_live = False
        # The tracker thread's own.
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tap: FrameTap | None = None
        self._camera: tuple[str, str] | None = None  # (name, serial) of the tap's camera
        self._tracking: _Tracking | None = None
        self._redetect = True
        self._next_detect = 0.0
        # The last detection and when, for the next one to confirm.
        self._candidate: tuple[float, list[Ball]] | None = None
        self._message: str | None = "waiting for the camera"
        self._frame_size: tuple[int, int] | None = None
        self._rate = 0.0  # tracked frames per second
        self._latency_ms = 0.0
        self._last_arrival = 0.0
        self._next_state = 0.0
        self._failures = 0

    @classmethod
    def from_options(cls, options: dict) -> ClosedLoopPlugin:
        corridors = int(options.get("corridors", 6))
        if corridors < 1:
            raise ValueError("corridors must be at least 1")
        raw_pairs = options.get("pairs")
        pairs = (
            [Pair(*p) for p in DEFAULT_PAIRS if p[0] <= corridors]
            if raw_pairs is None
            else [_pair(entry, corridors) for entry in raw_pairs]
        )
        tracked = [pair.corridor for pair in pairs]
        if len(set(tracked)) != len(tracked):
            raise ValueError("a corridor is tracked by two pairs")
        rule = Rule().updated({k: options[k] for k in RULE_FIELDS if k in options})
        camera = options.get("camera")
        return cls(
            camera=str(camera) if camera else None,
            corridors=corridors,
            pairs=pairs,
            rule=rule,
            ball_radius_px=float(options.get("ball_radius_px", BALL_RADIUS_PX)),
        )

    # ------------------------------------------------------------ lifecycle

    def setup(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="closedloop")
        self._thread.start()

    def teardown(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._tap is not None:
            self._tap.enabled = False
        self.on_recording_stop(aborted=True)

    def status(self) -> dict:
        return self._snapshot(time.monotonic())

    def snapshot_options(self, params: dict | None) -> dict | None:
        options = {} if self.rule == self._configured_rule else asdict(self.rule)
        if self.pairs != self._configured_pairs:
            options["pairs"] = [asdict(pair) for pair in self.pairs]
        return options or None

    def on_recording_start(self, params: dict | None) -> None:
        """Open the recording's log: from now on a push lights its pair."""
        controller = self.controller
        if controller is None:
            return
        info_dir = Path(controller.get_settings().save_dir) / RECORDING_INFO_DIRNAME
        try:
            loop_log = LoopLog(info_dir, self.tracked, self.channels)
        except OSError:
            log.exception("closedloop: could not open its log in %s; the loop is off", info_dir)
            return
        with self._lock:
            self.preview_live = False
            self._lights_out()
            for state in self._states.values():
                state.pushes = state.stims = 0
                state.ready_at = state.lit_until = 0.0
            self._log = loop_log
            for state in self._states.values():
                if state.forced:
                    loop_log.event(None, state.pair.corridor, state.pair.channel, "override_on")
        if self._tracking is None:
            log.warning(
                "closedloop: no balls tracked yet (%s); the loop starts once all %d "
                "corridors' balls are found",
                self._message,
                self.corridors,
            )
        what = "on" if self.rule.enabled else "off (tracking only)"
        log.info("closedloop: closed loop %s — %s", what, self._describe_rule())

    def on_recording_stop(self, aborted: bool) -> None:
        with self._lock:
            loop_log, self._log = self._log, None
            states = list(self._states.values())
            for state in states:
                state.lit_since = None
        if loop_log is None:
            return
        for channel in self.channels:
            self._light(channel, 0.0)
        loop_log.close()
        log.info(
            "closedloop: %s",
            "; ".join(
                f"corridor {s.pair.corridor}: {s.pushes} pushes, lit {s.stims} times"
                for s in states
            ),
        )

    # ------------------------------------------------------------ tracker thread

    def _run(self) -> None:
        while not self._stop.is_set():
            tap = self._attach()
            if tap is None:
                self._stop.wait(TAP_WAIT_S)
                continue
            frame = tap.get(TAP_WAIT_S)
            if frame is None:
                continue
            try:
                self._process(frame)
            except Exception:
                self._failures += 1
                if self._failures == 1 or self._failures % 1000 == 0:
                    log.exception("closedloop: tracking failed (%d times)", self._failures)

    def _attach(self) -> FrameTap | None:
        """The tracked camera's tap, enabled; a different camera (the GUI's
        cameras opened) has its arena detected afresh."""
        controller = self.controller
        camera = None if controller is None else self._find_camera(controller.camera_system)
        tap = None if camera is None else camera.frame_tap
        if tap is not self._tap:
            if self._tap is not None:
                self._tap.enabled = False
            self._tap = tap
            self._camera = None if camera is None else (camera.name, camera.serial_number)
            self._tracking = None
            self._message = "waiting for the camera" if tap is None else "detecting the balls"
            if tap is not None:
                tap.enabled = True
        return tap

    def _find_camera(self, system: CameraSystem) -> Camera | None:
        for camera in system:
            if self.camera_name in (None, camera.name, camera.serial_number):
                return camera
        return None

    def _process(self, frame: TapFrame) -> None:
        """Track the balls in ``frame``; while recording, light a pair whose
        ball was pushed and log the frame."""
        now = frame.arrival_ns / 1e9
        recording = frame.pulse is not None
        self._frame_size = (frame.array.shape[1], frame.array.shape[0])
        if self._should_detect(now, recording):
            self._detect(frame, now)
        tracking = self._tracking
        balls: list[Ball | None] = []
        pushes: list[tuple[int, float]] = []
        moving: dict[int, bool] = {}
        if tracking is not None:
            rule = self.rule
            for corridor, lane in tracking.lanes.items():
                ball = lane.update(frame.array)
                balls.append(ball)
                lost = tracking.lost_since[corridor]
                if ball is None:
                    if lost is None:
                        tracking.lost_since[corridor] = now
                    continue
                if lost is not None:
                    tracking.lost_since[corridor] = None
                    if now - lost >= REDETECT_AFTER_S:
                        tracking.detectors[corridor] = PushDetector(ball.y)
                detector = tracking.detectors[corridor]
                displacement = detector.update(now, ball.y, rule.direction, rule.push_px)
                moving[corridor] = detector.moving
                if displacement is not None:
                    pushes.append((corridor, displacement))
        latency_ms = (time.monotonic_ns() - frame.arrival_ns) / 1e6
        lit = False
        with self._lock:
            loop_log = self._log if recording else None
            live = (loop_log is not None and self.rule.enabled) or (
                self.preview_live and not recording
            )
            lit |= self._follow_overrides(frame, loop_log)
            for corridor, state in self._states.items():
                if state.lit_since is not None:
                    lit |= self._hold(frame, now, state, moving.get(corridor, False), loop_log)
            for corridor, displacement in pushes:
                lit |= self._on_push(frame, now, corridor, displacement, loop_log, live)
            if loop_log is not None and tracking is not None:
                lights = [
                    any(
                        s.lit_since is not None or s.forced
                        for s in self._states.values()
                        if s.pair.channel == ch
                    )
                    for ch in self.channels
                ]
                loop_log.frame(frame, latency_ms, balls, lights)
        self._measure(now, latency_ms)
        if lit or pushes or now >= self._next_state:
            self._publish(now)

    def _on_push(
        self,
        frame: TapFrame,
        now: float,
        corridor: int,
        displacement: float,
        loop_log: LoopLog | None,
        live: bool,
    ) -> bool:
        """Count a push and, when the loop is ``live`` and it earns it, switch its
        pair's light on for min_on_s; True if the light went on. Holds the lock."""
        state = self._states.get(corridor)
        if state is None:  # the pair's tracked side just changed
            return False
        state.pushes += 1
        rule = self.rule
        kind = "push"
        if (
            live
            and state.lit_since is None
            and not state.forced
            and now >= state.ready_at
        ):
            if self._light(state.pair.channel, rule.min_on_s):
                kind = "stim"
                state.stims += 1
                state.lit_since = now
                state.lit_until = now + rule.min_on_s
            else:
                kind = "stim_failed"
        if loop_log is not None:
            loop_log.event(frame, corridor, state.pair.channel, kind, displacement)
        return kind == "stim"

    def _lights_out(self) -> None:
        """Switch off the loop's lit lights (not an override's). Holds the lock."""
        for state in self._states.values():
            if state.lit_since is not None:
                self._light(state.pair.channel, 0.0)
                state.lit_since = None

    def set_tracked(self, index: int, corridor: int) -> None:
        """Track ``corridor`` of pair ``index`` (its other corridor becomes the
        control); the arena is detected afresh for the new lanes. Refused while
        recording."""
        with self._lock:
            if self._log is not None:
                raise RuntimeError("the tracked corridors cannot change while recording")
            if not 0 <= index < len(self.pairs):
                raise ValueError(f"no pair {index}")
            pair = self.pairs[index]
            if corridor == pair.corridor:
                return
            if corridor != pair.control:
                raise ValueError(f"corridor {corridor} is not in the pair {pair.corridor}+{pair.control}")
            self._lights_out()
            self.pairs = [*self.pairs[:index], Pair(corridor, pair.corridor, pair.channel), *self.pairs[index + 1 :]]
            self.tracked = [p.corridor for p in self.pairs]
            states = self._states
            self._states = {
                p.corridor: states.get(p.corridor) or _PairState(p) for p in self.pairs
            }
            self._tracking = None
            self._redetect = True
            self._candidate = None
            self._next_detect = 0.0
            self._message = "detecting the balls"
        log.info("closedloop: pair %d tracks corridor %d (control %d)", index + 1, corridor, pair.corridor)
        self._publish(time.monotonic())

    def set_preview_live(self, on: bool) -> None:
        """The tab's test switch: light on pushes outside recordings too. Off
        switches any such light off; refused while recording."""
        with self._lock:
            if on and self._log is not None:
                raise RuntimeError("a recording lights on pushes already")
            self.preview_live = on
            if not on and self._log is None:
                self._lights_out()
        log.info("closedloop: light in preview %s", "ON (test)" if on else "off")
        self._publish(time.monotonic())

    def _follow_overrides(self, frame: TapFrame, loop_log: LoopLog | None) -> bool:
        """Take in the operator's overrides: a forced pair's LED is on, and the
        loop's own light ends (forcing takes it over, releasing switched it
        off). True if one changed. Holds the lock."""
        opto = self._opto()
        changed = False
        for state in self._states.values():
            forced = opto is not None and opto.opto_forced(state.pair.channel)
            if forced == state.forced:
                continue
            changed = True
            state.forced = forced
            state.lit_since = None
            if loop_log is not None:
                kind = "override_on" if forced else "override_off"
                loop_log.event(frame, state.pair.corridor, state.pair.channel, kind)
        return changed

    def _hold(
        self, frame: TapFrame, now: float, state: _PairState, moving: bool, loop_log: LoopLog | None
    ) -> bool:
        """Keep a lit pair's light on for min_on_s and while its ball moves
        (a lost ball does not), else switch it off; True when it went off.
        Holds the lock."""
        since = state.lit_since
        assert since is not None
        channel = state.pair.channel
        keep_until = since + self.rule.min_on_s
        if moving and now < since + MAX_LIGHT_S:
            keep_until = max(keep_until, now + HOLD_S)
        if now < keep_until:
            if keep_until - state.lit_until >= EXTEND_STEP_S and self._light(channel, keep_until - now):
                state.lit_until = keep_until
            return False
        self._light(channel, 0.0)
        state.lit_since = None
        state.lit_until = now
        state.ready_at = now + self.rule.refractory_s
        if loop_log is not None:
            loop_log.event(frame, state.pair.corridor, channel, "light_off", duration=now - since)
        return True

    def _should_detect(self, now: float, recording: bool) -> bool:
        if now < self._next_detect:
            return False
        tracking = self._tracking
        if tracking is None or self._redetect:
            return True
        # A recording keeps its corridors: a lost ball is searched for in its own.
        if recording:
            return False
        lost = [t for t in tracking.lost_since.values() if t is not None]
        return bool(lost) and now - min(lost) >= REDETECT_AFTER_S

    def _detect(self, frame: TapFrame, now: float) -> None:
        """Find the arena's balls; adopted only when every corridor has one, the
        corridors are evenly spaced and the next detection agrees, so the
        corridor numbers cannot shift and nothing passing is taken for a ball."""
        self._next_detect = now + DETECT_INTERVAL_S
        balls = detect_balls(frame.array, self.corridors, self.ball_radius_px)
        candidate, self._candidate = self._candidate, None
        if len(balls) < self.corridors:
            self._message = f"found {len(balls)} of {self.corridors} balls"
            return
        gaps = [b.x - a.x for a, b in zip(balls, balls[1:], strict=False)]
        if gaps and (min(gaps) < 0.7 * max(gaps)):
            self._message = "the balls found are not one per evenly spaced corridor"
            return
        if (
            candidate is None
            or now - candidate[0] > 2 * DETECT_INTERVAL_S
            or any(
                math.hypot(a.x - b.x, a.y - b.y) > self.ball_radius_px
                for a, b in zip(candidate[1], balls, strict=True)
            )
        ):
            self._candidate = (now, balls)
            return
        with self._lock:  # the tracked corridors as they are now (set_tracked)
            tracked = list(self.tracked)
            lanes = {c: Lane(balls[c - 1].x, balls[c - 1].y, self.ball_radius_px) for c in tracked}
            self._tracking = _Tracking([b.x for b in balls], lanes)
            self._redetect = False
            self._message = None
        log.info(
            "closedloop: found %d corridors at x = %s; tracking %s",
            len(balls),
            ", ".join(f"{b.x:.0f}" for b in balls),
            ", ".join(f"corridor {c}" for c in tracked),
        )

    def _measure(self, now: float, latency_ms: float) -> None:
        if self._last_arrival:
            dt = now - self._last_arrival
            if dt > 0:
                self._rate = 0.9 * self._rate + 0.1 / dt if self._rate else 1.0 / dt
        self._last_arrival = now
        self._latency_ms = 0.9 * self._latency_ms + 0.1 * latency_ms

    def _publish(self, now: float) -> None:
        self._next_state = now + STATE_PERIOD_S
        self.broadcast("closedloop_state", self._snapshot(now))

    def _snapshot(self, now: float) -> dict:
        tracking = self._tracking
        balls = []
        if tracking is not None:
            for corridor, lane in tracking.lanes.items():
                balls.append(
                    {
                        "corridor": corridor,
                        "x": round(lane.ball.x, 1),
                        "y": round(lane.ball.y, 1),
                        "score": round(lane.ball.score, 3),
                        "lost": lane.lost,
                        "weak": not lane.lost and lane.ball.score < TRACK_SCORE,
                        "progress_px": round(tracking.detectors[corridor].progress, 1),
                    }
                )
        with self._lock:
            recording = self._log is not None
            pairs = [
                {
                    **asdict(s.pair),
                    "pushes": s.pushes,
                    "stims": s.stims,
                    "light": s.lit_since is not None or s.forced,
                    "forced": s.forced,
                    "ready_in_s": round(max(0.0, s.ready_at - now), 1),
                }
                for s in self._states.values()
            ]
            rule = asdict(self.rule)
        name, serial = self._camera or (self.camera_name, None)
        return {
            "camera": name,
            "camera_serial": serial,
            "ball_radius_px": self.ball_radius_px,
            "corridors": self.corridors,
            "message": self._message,
            "recording": recording,
            "preview_live": self.preview_live,
            "frame_size": self._frame_size,
            # Every corridor's x, tracked or not, as last detected.
            "corridor_x": [] if tracking is None else [round(x, 1) for x in tracking.corridor_x],
            "rate_fps": round(self._rate, 1),
            "latency_ms": round(self._latency_ms, 2),
            "balls": balls,
            "pairs": pairs,
            "rule": rule,
        }

    # ------------------------------------------------------------ light

    def _opto(self) -> Plugin | None:
        controller = self.controller
        return None if controller is None else controller.plugins.opto_plugin()

    def _light(self, channel: int, seconds: float) -> bool:
        opto = self._opto()
        if opto is None:
            log.error("closedloop: no opto plugin is loaded (the `opto` plugin drives the LEDs)")
            return False
        return opto.set_opto(channel, seconds)

    def _describe_rule(self) -> str:
        rule = self.rule
        return (
            f"a push is {rule.push_px:g} px {rule.direction}; it lights the pair for "
            f"at least {rule.min_on_s:g} s and while the ball moves, then "
            f"{rule.refractory_s:g} s refractory"
        )

    # ------------------------------------------------------------ web

    def api_router(self):
        from fastapi import APIRouter, Body, HTTPException

        router = APIRouter()

        @router.put("/api/closedloop/rule")
        def put_rule(payload: dict = Body(...)):
            """Edit the rule between recordings (a recording keeps the rule its
            config snapshot records)."""
            with self._lock:
                if self._log is not None:
                    raise HTTPException(409, "the rule cannot change while recording")
                try:
                    self.rule = self.rule.updated(payload)
                except (TypeError, ValueError) as e:
                    raise HTTPException(422, str(e)) from e
            self._publish(time.monotonic())
            return asdict(self.rule)

        @router.put("/api/closedloop/pair")
        def put_pair(payload: dict = Body(...)):
            """``{"pair": i, "tracked": corridor}``: which corridor of pair i
            (0-based) is tracked; the other is its control."""
            try:
                self.set_tracked(int(payload.get("pair", -1)), int(payload.get("tracked", 0)))
            except RuntimeError as e:
                raise HTTPException(409, str(e)) from e
            except (TypeError, ValueError) as e:
                raise HTTPException(422, str(e)) from e
            return {"pairs": [asdict(pair) for pair in self.pairs]}

        @router.put("/api/closedloop/preview")
        def put_preview(payload: dict = Body(...)):
            """``{"on": true}``: pushes light outside recordings too (a test)."""
            try:
                self.set_preview_live(bool(payload.get("on")))
            except RuntimeError as e:
                raise HTTPException(409, str(e)) from e
            return {"preview_live": self.preview_live}

        @router.post("/api/closedloop/detect")
        def detect():
            """Find the balls again (a new or moved arena)."""
            with self._lock:
                if self._log is not None:
                    raise HTTPException(409, "the corridors cannot change while recording")
            self._redetect = True
            self._next_detect = 0.0
            return {"ok": True}

        @router.post("/api/closedloop/test")
        def test(payload: dict = Body(...)):
            """Light ``{"channel": n, "duration_s": s}``: a check of the wiring."""
            channel = int(payload.get("channel", 0))
            duration_s = float(payload.get("duration_s", 1.0))
            if channel not in self.channels:
                raise HTTPException(422, f"channel must be one of {self.channels}")
            ok = self._light(channel, min(max(duration_s, 0.0), MAX_WINDOW_S))
            with self._lock:
                if self._log is not None:
                    self._log.event(None, None, channel, "test")
            return {"ok": ok}

        return router


def _pair(entry: object, corridors: int) -> Pair:
    if not isinstance(entry, dict) or "corridor" not in entry or "channel" not in entry:
        raise ValueError(f"a pair is a table with a corridor and a channel, got {entry!r}")
    corridor = int(entry["corridor"])
    control = entry.get("control")
    control = None if control is None else int(control)
    channel = int(entry["channel"])
    for number in (corridor, control):
        if number is not None and not 1 <= number <= corridors:
            raise ValueError(f"corridor {number} is not one of 1-{corridors}")
    if channel < 1:
        raise ValueError(f"opto channel {channel} must be 1 or more")
    return Pair(corridor, control, channel)
