"""closedloop plugin: ball tracker, push rule, light windows, logs, routes, and a
recording on the fake backend."""

import csv
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from helpers import wait_until

from octacam.cameras import CameraSystem
from octacam.cameras.base import FrameTap, TapFrame
from octacam.config import RecordingSettings
from octacam.controller import RecordingController
from octacam.plugins import build_plugins
from octacam.plugins.base import Plugin, PluginManager
from octacam.plugins.closedloop.plugin import (
    EVENTS_FILENAME,
    FRAMES_FILENAME,
    REST_S,
    ClosedLoopPlugin,
    Pair,
    PushDetector,
    Rule,
)
from octacam.plugins.closedloop.tracker import Lane, detect_balls

RADIUS = 30.0
SIZE = (1200, 2048)  # (height, width)
XS = (190, 520, 850, 1175, 1500, 1835)


def arena(ys, *, size=SIZE, xs=XS, radius=RADIUS, seed=0, fly=None, bar=None) -> np.ndarray:
    """A top view: dark floor, each corridor's ball (a bright rim round a dimmer
    center), optional ``fly`` (x, y) blob and a bright horizontal ``bar`` (y0, y1)."""
    h, w = size
    rng = np.random.default_rng(seed)
    frame = rng.normal(28, 4, size).clip(0, 255)
    yy, xx = np.mgrid[:h, :w]
    for x, y in zip(xs, ys, strict=True):
        if y is None:
            continue
        d = np.hypot(xx - x, yy - y) / radius
        frame[(d >= 0.4) & (d < 1.0)] = 235
        frame[d < 0.4] = 80
    if fly is not None:
        fx, fy = fly
        frame[((xx - fx) / 25) ** 2 + ((yy - fy) / 55) ** 2 < 1] = 200
    if bar is not None:
        frame[bar[0] : bar[1]] = 250
    return frame.astype(np.uint8)


# --------------------------------------------------------------- tracker


def test_detect_balls_finds_every_corridor_left_to_right():
    ys = (600, 700, 500, 800, 650, 550)
    balls = detect_balls(arena(ys), 6, RADIUS)
    assert len(balls) == 6
    for ball, x, y in zip(balls, XS, ys, strict=True):
        assert abs(ball.x - x) < 1.5 and abs(ball.y - y) < 1.5


def test_detect_balls_reports_a_missing_ball():
    assert len(detect_balls(arena((600, 700, None, 800, 650, 550)), 6, RADIUS)) == 5


def test_detect_balls_takes_one_per_corridor_column():
    # A second ball-like ring in corridor 3's column (here: lower down) is not a
    # seventh corridor.
    frame = arena((600, 700, 500, 800, 650, 550))
    np.copyto(frame[880:1000, 790:910], arena((None, None, 940, None, None, None))[880:1000, 790:910])
    balls = detect_balls(frame, 7, RADIUS)
    assert [round(b.x, -1) for b in balls] == [190, 520, 850, 1180, 1500, 1840]


def test_lane_follows_a_moving_ball_to_a_subpixel():
    lane = Lane(850, 600.0, RADIUS)
    for step in range(30):
        y = 600.0 - 3.3 * step
        ball = lane.update(arena((600, 700, y, 800, 650, 550), seed=step))
        assert ball is not None and abs(ball.y - y) < 0.6 and abs(ball.x - 850) < 0.6


def test_lane_keeps_the_ball_beside_a_fly():
    lane = Lane(850, 600.0, RADIUS)
    ball = lane.update(arena((600, 700, 600, 800, 650, 550), fly=(850, 690)))
    assert ball is not None and abs(ball.y - 600) < 1


def test_lane_loses_an_occluded_ball_and_finds_it_again():
    lane = Lane(850, 600.0, RADIUS)
    assert lane.update(arena((600,) * 6, bar=(560, 640))) is None
    assert lane.lost and lane.ball.y == pytest.approx(600, abs=1)  # kept
    # Back in view, moved further than one frame's search: found in its column.
    ball = lane.update(arena((600, 700, 900, 800, 650, 550)))
    assert ball is not None and abs(ball.y - 900) < 1 and not lane.lost


def test_lane_tracks_at_another_magnification():
    radius = 16.0
    xs = (100, 300)
    lane = Lane(300, 200.0, radius)
    for y in (200.0, 196.5, 190.0):
        ball = lane.update(arena((150, y), size=(480, 640), xs=xs, radius=radius))
        assert ball is not None and abs(ball.y - y) < 0.6
    balls = detect_balls(arena((150, 200), size=(480, 640), xs=xs, radius=radius), 2, radius)
    assert [round(b.x) for b in balls] == [100, 300]


# --------------------------------------------------------------- push rule


def _pushes(direction, path, dt=0.05):
    """The pushes along ``path`` (y per sample, ``dt`` s apart; None: rest for
    REST_S at the last y)."""
    detector = PushDetector(path[0])
    t, y, got = 0.0, path[0], []
    for step in path[1:]:
        hold = step is None
        for _ in range(int(REST_S / dt) + 2 if hold else 1):
            t += dt
            y = y if hold else step
            pushed = detector.update(t, y, direction, 10.0)
            if pushed is not None:
                got.append(round(pushed, 3))
    return got


@pytest.mark.parametrize(
    "direction, path, pushes",
    [
        # Forward of where it rests, in nudges that add up.
        ("up", [500, 495, 491, 489], [11.0]),
        # Pushes are counted on from where the last one ended.
        ("up", [500, 489, 485, 479], [11.0, 10.0]),
        # Knocked back, it bounces forward without resting: no push...
        ("up", [500, 526, 499, 505], []),
        # ...but once it rests further back, a push is measured from there.
        ("up", [500, 526, None, 515], [11.0]),
        ("up", [500, 530, 560], []),
        ("down", [500, 490, None, 499, 501], [11.0]),
        ("any", [500, 490, None, 501], [10.0, 11.0]),
    ],
)
def test_push_detector(direction, path, pushes):
    assert _pushes(direction, path) == pushes


def test_rule_updates_are_validated():
    rule = Rule().updated({"push_px": 5, "direction": "down", "enabled": False})
    assert (rule.push_px, rule.direction, rule.enabled) == (5.0, "down", False)
    for bad in ({"direction": "left"}, {"push_px": 0}, {"min_on_s": -1}, {"enabled": "yes"},
                {"refractory_s": float("nan")}, {"speed": 1}):
        with pytest.raises(ValueError):
            Rule().updated(bad)


def test_from_options_defaults_to_three_pairs_of_six():
    plugin = ClosedLoopPlugin.from_options({})
    assert plugin.corridors == 6
    assert plugin.pairs == [Pair(1, 2, 1), Pair(3, 4, 2), Pair(5, 6, 3)]
    assert plugin.channels == [1, 2, 3]


@pytest.mark.parametrize(
    "options",
    [
        {"corridors": 0},
        {"pairs": [{"corridor": 7, "channel": 1}]},
        {"pairs": [{"corridor": 1, "control": 9, "channel": 1}]},
        {"pairs": [{"corridor": 1, "channel": 1}, {"corridor": 1, "channel": 2}]},
        {"pairs": [{"corridor": 1}]},
        {"direction": "sideways"},
    ],
)
def test_from_options_refuses_a_bad_config(options):
    with pytest.raises(ValueError):
        ClosedLoopPlugin.from_options(options)


def test_registered_and_buildable():
    config = SimpleNamespace(plugins=[SimpleNamespace(name="closedloop", options={"push_px": 7})])
    (plugin,) = build_plugins(config).plugins
    assert isinstance(plugin, ClosedLoopPlugin) and plugin.rule.push_px == 7


# --------------------------------------------------------------- the loop


class OptoStub(Plugin):
    name = "opto"
    drives_opto = True

    def __init__(self):
        self.calls: list[tuple[int, float]] = []
        self.ok = True
        self.forced: set[int] = set()

    def set_opto(self, channel, duration_s):
        self.calls.append((channel, duration_s))
        return self.ok

    def opto_forced(self, channel):
        return channel in self.forced


REST_YS = [600, 700, 600, 800, 650, 550]


class Loop:
    """A plugin over a stub controller, fed frames by hand: ``frame(ys)``
    processes an arena with the balls at ``ys`` at a time ``t`` (s)."""

    def __init__(self, tmp_path, **options):
        self.opto = OptoStub()
        self.plugin = ClosedLoopPlugin.from_options(options)
        self.save_dir = tmp_path / "rec"
        self.tap = FrameTap()
        camera = SimpleNamespace(name="top", serial_number="1", frame_tap=self.tap)
        self.plugin.controller = SimpleNamespace(  # type: ignore[assignment]
            camera_system=[camera],
            plugins=PluginManager([self.plugin, self.opto]),
            get_settings=lambda: SimpleNamespace(save_dir=str(self.save_dir)),
        )
        self.pulse = 0

    def frame(self, ys, t, *, recording=True):
        pulse = self.pulse if recording else None
        if recording:
            self.pulse += 1
        frame = TapFrame(arena(ys), 1_000_000 + int(t * 1e9), pulse, int(t * 1e9))
        self.plugin._process(frame)

    def settle(self, ys=REST_YS, t=0.0):
        """Preview frames until the arena is adopted (two detections agreeing)."""
        self.frame(ys, t, recording=False)
        self.frame(ys, t + 0.6, recording=False)

    def rows(self, filename):
        with open(self.save_dir / "octacam_recording" / filename, newline="") as f:
            return list(csv.DictReader(f))


REST = REST_YS


def _moved(corridor, y):
    ys = list(REST)
    ys[corridor - 1] = y
    return ys


def test_preview_tracks_pushes_but_never_lights(tmp_path):
    loop = Loop(tmp_path)
    loop.settle(t=1.0)
    loop.frame(_moved(1, 585), 1.7, recording=False)
    assert loop.opto.calls == []
    state = loop.plugin.status()
    assert state["pairs"][0]["pushes"] == 1 and state["pairs"][0]["stims"] == 0
    # Every corridor found, only the pairs' tracked ones followed.
    assert state["corridor_x"] == pytest.approx(XS, abs=1)
    assert [b["corridor"] for b in state["balls"]] == [1, 3, 5]


def _on(loop):
    return [p["light"] for p in loop.plugin.status()["pairs"]]


def test_a_push_lights_its_pair_while_the_ball_moves(tmp_path):
    loop = Loop(tmp_path)
    loop.settle()  # detects the arena
    loop.plugin.on_recording_start(None)
    loop.frame(REST, 1.0)
    loop.frame(_moved(3, 588), 1.1)  # 12 px up: corridor 3 -> channel 2, on for 1 s
    assert loop.opto.calls == [(2, 1.0)]
    # The fly keeps pushing past the minimum: the light is held on, in short
    # extensions the board ends by itself.
    y, t = 588, 1.1
    while t < 2.5:
        y, t = y - 3, round(t + 0.1, 2)
        loop.frame(_moved(3, y), t)
    assert _on(loop) == [False, True, False]
    extensions = loop.opto.calls[1:]
    assert extensions and all(ch == 2 and 0 < s <= 0.25 + 1e-9 for ch, s in extensions)
    # The ball stops: off once it has been still for REST_S.
    for k in range(1, 5):
        loop.frame(_moved(3, y), round(t + 0.1 * k, 2))
    assert _on(loop) == [False, False, False] and loop.opto.calls[-1] == (2, 0.0)
    loop.frame(_moved(4, 785), 3.0)  # the control corridor: not tracked
    loop.plugin.on_recording_stop(aborted=False)

    events = loop.rows(EVENTS_FILENAME)
    kinds = [(e["corridor"], e["channel"], e["event"]) for e in events]
    assert kinds[0] == ("3", "2", "stim") and kinds[-1] == ("3", "2", "light_off")
    assert {k for k in kinds[1:-1]} == {("3", "2", "push")}  # pushes while lit
    assert float(events[-1]["duration_s"]) == pytest.approx(t + 0.3 - 1.1, abs=0.15)
    frames = loop.rows(FRAMES_FILENAME)
    assert [k for k in frames[0] if k.endswith("_y")] == ["c1_y", "c3_y", "c5_y"]
    assert frames[0]["light2"] == "0" and frames[1]["light2"] == "1"
    assert frames[-1]["light2"] == "0"


def test_the_light_stays_on_for_the_minimum(tmp_path):
    loop = Loop(tmp_path, min_on_s=1.0)
    loop.settle()
    loop.plugin.on_recording_start(None)
    loop.frame(REST, 1.0)
    loop.frame(_moved(1, 585), 1.1)  # one push, then the ball rests
    for k in range(1, 10):
        loop.frame(_moved(1, 585), round(1.1 + 0.1 * k, 2))
    assert _on(loop)[0]  # 1.0 s after the push: still on
    loop.frame(_moved(1, 585), 2.2)
    assert not _on(loop)[0]
    assert loop.opto.calls == [(1, 1.0), (1, 0.0)]
    loop.plugin.on_recording_stop(aborted=False)


def test_refractory_after_the_light(tmp_path):
    loop = Loop(tmp_path, min_on_s=0.5, refractory_s=2.0)
    loop.settle()
    loop.plugin.on_recording_start(None)
    loop.frame(REST, 1.0)
    loop.frame(_moved(5, 635), 1.1)  # lit
    for k in range(1, 7):
        loop.frame(_moved(5, 635), round(1.1 + 0.1 * k, 2))  # off at 1.6
    loop.frame(_moved(5, 620), 1.8)  # a push in the refractory: no light
    loop.frame(_moved(5, 605), 3.7)  # after it: lit again
    loop.plugin.on_recording_stop(aborted=False)
    assert [c for c in loop.opto.calls if c[1] != 0.0] == [(3, 0.5), (3, 0.5)]
    assert [e["event"] for e in loop.rows(EVENTS_FILENAME)] == ["stim", "light_off", "push", "stim"]


def test_an_override_holds_the_light_and_is_logged(tmp_path):
    loop = Loop(tmp_path)
    loop.settle()
    loop.plugin.on_recording_start(None)
    loop.frame(REST, 1.0)
    loop.frame(_moved(1, 585), 1.1)  # the loop lights pair 1
    loop.opto.forced.add(1)  # the operator forces LED 1 on: the override takes over
    loop.frame(_moved(1, 585), 1.2)
    loop.frame(_moved(1, 570), 1.3)  # a push while forced: no new light
    assert _on(loop)[0] and loop.plugin.status()["pairs"][0]["forced"]
    loop.frame(_moved(1, 570), 3.0)
    assert loop.opto.calls == [(1, 1.0)]  # the loop never asked again
    loop.opto.forced.discard(1)  # released: off, and the loop is free again
    loop.frame(_moved(1, 570), 3.1)
    assert not _on(loop)[0]
    loop.frame(_moved(1, 555), 3.2)
    assert loop.opto.calls[-1] == (1, 1.0)
    loop.plugin.on_recording_stop(aborted=False)
    events = [e["event"] for e in loop.rows(EVENTS_FILENAME)]
    assert events == ["stim", "override_on", "push", "override_off", "stim"]
    lights = [f["light1"] for f in loop.rows(FRAMES_FILENAME)]
    assert lights == ["0", "1", "1", "1", "1", "0", "1"]


def test_light_in_preview_is_a_test_switch(tmp_path):
    loop = Loop(tmp_path, enabled=False)  # it lights even with the recording rule off
    loop.settle()
    loop.frame(_moved(1, 585), 1.0, recording=False)
    assert loop.opto.calls == []  # off by default: preview only tracks
    loop.plugin.set_preview_live(True)
    loop.frame(_moved(1, 570), 1.1, recording=False)
    assert loop.opto.calls == [(1, 1.0)] and _on(loop)[0]
    loop.plugin.set_preview_live(False)  # off switches the light off
    assert loop.opto.calls[-1] == (1, 0.0) and not _on(loop)[0]
    loop.plugin.set_preview_live(True)
    loop.frame(_moved(1, 555), 2.5, recording=False)
    loop.plugin.on_recording_start(None)  # a recording turns it off, light too
    assert not loop.plugin.preview_live and loop.opto.calls[-1] == (1, 0.0)
    with pytest.raises(RuntimeError):
        loop.plugin.set_preview_live(True)
    loop.plugin.on_recording_stop(aborted=False)
    assert not (Path(loop.save_dir) / "octacam_recording" / EVENTS_FILENAME).read_text().count("stim")


def test_a_pair_can_track_its_other_corridor(tmp_path):
    loop = Loop(tmp_path)
    loop.settle()
    assert [b["corridor"] for b in loop.plugin.status()["balls"]] == [1, 3, 5]
    loop.plugin.set_tracked(0, 2)  # pair 1+2 now tracks corridor 2
    assert loop.plugin.pairs[0] == Pair(2, 1, 1)
    assert loop.plugin.status()["balls"] == []  # re-detecting
    loop.settle(t=1.0)
    assert [b["corridor"] for b in loop.plugin.status()["balls"]] == [2, 3, 5]
    loop.frame(_moved(2, 685), 2.0, recording=False)  # corridor 2 pushed: pair 1 counts it
    loop.frame(_moved(1, 580), 2.1, recording=False)  # corridor 1 is the control now
    assert [p["pushes"] for p in loop.plugin.status()["pairs"]] == [1, 0, 0]
    # The recording's config snapshot carries the switch.
    assert loop.plugin.snapshot_options(None)["pairs"][0] == {"corridor": 2, "control": 1, "channel": 1}
    for bad in ((0, 3), (5, 1)):
        with pytest.raises(ValueError):
            loop.plugin.set_tracked(*bad)
    loop.plugin.on_recording_start(None)
    with pytest.raises(RuntimeError):
        loop.plugin.set_tracked(0, 1)
    loop.plugin.on_recording_stop(aborted=False)
    rows = loop.rows(FRAMES_FILENAME)
    assert not rows or "c2_y" in rows[0]


def test_disabled_loop_logs_pushes_without_light(tmp_path):
    loop = Loop(tmp_path, enabled=False)
    loop.settle()
    loop.plugin.on_recording_start(None)
    loop.frame(_moved(5, 630), 1.0)
    loop.plugin.on_recording_stop(aborted=False)
    assert all(duration == 0 for _, duration in loop.opto.calls)
    assert [e["event"] for e in loop.rows(EVENTS_FILENAME)] == ["push"]


def test_a_refused_light_is_logged(tmp_path):
    loop = Loop(tmp_path)
    loop.opto.ok = False
    loop.settle()
    loop.plugin.on_recording_start(None)
    loop.frame(_moved(1, 585), 1.0)
    loop.plugin.on_recording_stop(aborted=False)
    assert [e["event"] for e in loop.rows(EVENTS_FILENAME)] == ["stim_failed"]


def test_an_incomplete_arena_is_not_tracked(tmp_path):
    loop = Loop(tmp_path)
    loop.frame([600, 700, None, 800, 650, 550], 0.0, recording=False)
    state = loop.plugin.status()
    assert state["message"] == "found 5 of 6 balls" and state["balls"] == []
    loop.frame(REST, 0.6, recording=False)  # retried: a candidate...
    assert loop.plugin.status()["balls"] == []
    loop.frame(REST, 1.2, recording=False)  # ...adopted once the next agrees
    assert loop.plugin.status()["message"] is None


def test_an_unevenly_spaced_arena_is_not_adopted(tmp_path):
    # Six rings, but not one per evenly spaced corridor (a ball missing and
    # something ring-like elsewhere): corridor numbers would shift.
    loop = Loop(tmp_path)
    xs = (190, 520, 850, 1175, 1500, 1700)
    frame = TapFrame(arena(REST, xs=xs), 0, None, 0)
    loop.plugin._process(frame)
    state = loop.plugin.status()
    assert state["balls"] == [] and "evenly spaced" in state["message"]


def test_a_recording_keeps_its_corridors(tmp_path):
    loop = Loop(tmp_path)
    loop.settle()
    loop.plugin.on_recording_start(None)
    shifted = [y + 300 for y in REST]
    for k in range(5):  # corridor 3's ball gone for 2 s: not re-detected
        loop.frame([*shifted[:2], None, *shifted[3:]], 1.0 + 0.5 * k)
    state = loop.plugin.status()
    assert [b["x"] for b in state["balls"]] == pytest.approx(XS[::2], abs=1)
    assert state["corridor_x"] == pytest.approx(XS, abs=1)
    loop.plugin.on_recording_stop(aborted=False)


def test_routes(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    loop = Loop(tmp_path)
    app = FastAPI()
    app.include_router(loop.plugin.api_router())
    with TestClient(app) as client:
        assert client.put("/api/closedloop/rule", json={"push_px": 4}).json()["push_px"] == 4
        assert client.put("/api/closedloop/rule", json={"direction": "x"}).status_code == 422
        assert client.post("/api/closedloop/test", json={"channel": 2, "duration_s": 0.5}).json() == {"ok": True}
        assert client.post("/api/closedloop/test", json={"channel": 4}).status_code == 422
        assert loop.opto.calls == [(2, 0.5)]
        assert loop.plugin.snapshot_options(None) == {
            "direction": "up", "push_px": 4.0, "min_on_s": 1.0, "refractory_s": 0.0, "enabled": True,
        }
        pairs = client.put("/api/closedloop/pair", json={"pair": 1, "tracked": 4}).json()["pairs"]
        assert pairs[1] == {"corridor": 4, "control": 3, "channel": 2}
        assert client.put("/api/closedloop/pair", json={"pair": 1, "tracked": 9}).status_code == 422
        assert loop.plugin.snapshot_options(None)["pairs"] == [
            {"corridor": 1, "control": 2, "channel": 1},
            {"corridor": 4, "control": 3, "channel": 2},
            {"corridor": 5, "control": 6, "channel": 3},
        ]
        assert client.put("/api/closedloop/preview", json={"on": True}).json() == {"preview_live": True}
        assert client.put("/api/closedloop/preview", json={"on": False}).json() == {"preview_live": False}
        loop.plugin.on_recording_start(None)
        assert client.put("/api/closedloop/preview", json={"on": True}).status_code == 409
        assert client.put("/api/closedloop/rule", json={"push_px": 5}).status_code == 409
        assert client.post("/api/closedloop/detect").status_code == 409
        loop.plugin.on_recording_stop(aborted=False)
        assert client.post("/api/closedloop/detect").json() == {"ok": True}


def test_snapshot_options_none_when_the_rule_is_configured():
    plugin = ClosedLoopPlugin.from_options({"push_px": 12})
    assert plugin.snapshot_options(None) is None


# --------------------------------------------------------------- end to end


def test_a_recording_on_the_fake_camera_lights_on_a_push(tmp_path):
    """The real record loop feeds the tap: a ball pushed mid-recording lights its
    pair, and the logs land in the recording's octacam_recording/."""
    radius = 16.0
    xs = (100, 300, 500, 700)
    ys = {"c1": 300.0}

    def render(width, height, index):
        return arena((ys["c1"], 260, 280, 240), size=(height, width), xs=xs, radius=radius)

    system = CameraSystem(["FAKE-0"], backend="fake")
    system.load_config(tmp_path)
    camera = next(iter(system))
    camera.set_geometry(width=800, height=480)
    camera.backend.render = render  # type: ignore[attr-defined]
    opto = OptoStub()
    plugin = ClosedLoopPlugin.from_options(
        {
            "corridors": 4,
            "pairs": [{"corridor": 1, "control": 2, "channel": 1}, {"corridor": 3, "channel": 2}],
            "ball_radius_px": radius,
            "push_px": 8,
        }
    )
    save_dir = tmp_path / "rec" / "001"
    settings = RecordingSettings(
        fps=40.0, duration_s=1.5, save_dir=str(save_dir), trigger_source="software"
    )
    controller = RecordingController(
        system, settings, plugins=PluginManager([plugin, opto]), auto_preview=True
    )
    plugin.setup()
    try:
        controller.start_preview()
        assert wait_until(lambda: plugin.status().get("balls"), timeout=10)
        assert controller.start_recording().ok
        assert wait_until(lambda: plugin.status().get("recording"), timeout=10)
        time.sleep(0.3)
        ys["c1"] = 280.0  # pushed 20 px up
        controller.join(timeout=20)
    finally:
        plugin.teardown()
        controller.close()
        system.close()
    assert (1, 1.0) in opto.calls
    info = Path(save_dir) / "octacam_recording"
    with open(info / EVENTS_FILENAME, newline="") as f:
        events = list(csv.DictReader(f))
    assert events[0]["event"] == "stim" and {e["event"] for e in events} <= {"stim", "light_off"}
    with open(info / FRAMES_FILENAME, newline="") as f:
        frames = list(csv.DictReader(f))
    assert len(frames) >= 30 and int(frames[0]["frame"]) == 0
