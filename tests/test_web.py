"""Web backend integration tests against the camera emulator."""

import asyncio
import json
import os
import time
from unittest.mock import Mock

os.environ.setdefault("PYLON_CAMEMU", "2")

import numpy as np
import pytest
from fastapi.testclient import TestClient

from octacam.camera import CameraSystem
from octacam.config import OctacamConfig
from octacam.controller import RecordingController, RecordingSettings
from octacam.web.app import FRAME_HEADER, create_app

EMULATED_SERIALS = ["0815-0000", "0815-0001"]


@pytest.fixture
def client(tmp_path):
    system = CameraSystem(EMULATED_SERIALS)
    assert len(system) == 2, "PYLON_CAMEMU=2 expected"
    system.load_config(tmp_path)
    config = OctacamConfig()
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(system, settings)
    controller.start_preview()
    app = create_app(controller, config, None, config_dir=str(tmp_path))
    try:
        with TestClient(app) as test_client:
            test_client.controller = controller
            yield test_client
    finally:
        controller.close()


@pytest.fixture
def shutdown_client(tmp_path):
    # Same as `client`, but with an injected shutdown callback so POSTing
    # /api/shutdown invokes a mock instead of signalling the test process.
    system = CameraSystem(EMULATED_SERIALS)
    system.load_config(tmp_path)
    config = OctacamConfig()
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(system, settings)
    controller.start_preview()
    shutdown = Mock()
    app = create_app(
        controller,
        config,
        None,
        config_dir=str(tmp_path),
        shutdown_callback=shutdown,
    )
    try:
        with TestClient(app) as test_client:
            test_client.controller = controller
            test_client.shutdown_mock = shutdown
            yield test_client
    finally:
        controller.close()


def test_shutdown_endpoint(shutdown_client):
    response = shutdown_client.post("/api/shutdown")
    assert response.status_code == 202
    assert response.json()["status"] == "shutting_down"
    # TestClient runs the response's BackgroundTasks before returning, so the
    # injected callback has already fired by now.
    assert shutdown_client.shutdown_mock.called


def test_shutdown_refused_while_recording(shutdown_client):
    started = shutdown_client.post(
        "/api/recording/start", json={"confirm_overwrite": False}
    )
    assert started.status_code == 202, started.text

    refused = shutdown_client.post("/api/shutdown")
    assert refused.status_code == 409
    assert shutdown_client.shutdown_mock.called is False

    shutdown_client.controller.stop_recording(abort=True)


def test_system_and_settings_endpoints(client):
    system = client.get("/api/system").json()
    assert len(system["cameras"]) == 2
    assert system["cameras"][0]["width"] > 0
    assert {f["save_method"] for f in system["formats"]} == {
        "ffmpeg",
        "raw",
    }
    # no plugins loaded in tests -> empty plugin status, no serial endpoint
    assert system["plugins"] == {}

    settings = client.get("/api/settings").json()
    assert settings["fps"] == 50.0
    # New recording-output toggles default to display form, CSV off.
    assert settings["record_form"] == "display"
    assert settings["save_frame_timestamps"] is False

    response = client.put("/api/settings", json={"fps": 60.0, "save_method": "raw"})
    assert response.status_code == 200
    assert response.json()["fps"] == 60.0
    assert response.json()["save_method"] == "raw"

    # record_form/save_frame_timestamps patch; invalid record_form is rejected.
    patched = client.put(
        "/api/settings",
        json={"record_form": "sensor", "save_frame_timestamps": True},
    )
    assert patched.status_code == 200
    assert patched.json()["record_form"] == "sensor"
    assert patched.json()["save_frame_timestamps"] is True
    assert client.put("/api/settings", json={"record_form": "bogus"}).status_code == 422
    # ffmpeg_params is a live setting (the GUI's Advanced box edits it); a bad
    # save_method is still rejected, and other encoder knobs stay unknown (422).
    ffmpeg = client.put("/api/settings", json={"ffmpeg_params": "-c:v ffv1"})
    assert ffmpeg.status_code == 200
    assert ffmpeg.json()["ffmpeg_params"] == "-c:v ffv1"
    assert client.put("/api/settings", json={"save_method": "vp9"}).status_code == 422

    assert client.put("/api/settings", json={"fps": -1}).status_code == 422
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 422
    assert client.put("/api/settings", json={"crf": 18}).status_code == 422

    validation = client.post(
        "/api/save-dir/validate", json={"path": "~/somewhere"}
    ).json()
    assert validation["resolved"].startswith("/")
    assert validation["free_bytes"] > 0

    # the serial endpoint is contributed by the (absent) flywheel plugin, so it
    # is not served here (404/405 from the static catch-all, never 200/503)
    command = dict.fromkeys(
        (
            "n_steps",
            "step_interval_us",
            "rest_duration_ms",
            "n_repeats",
            "init_wait_duration_s",
        ),
        1,
    )
    assert client.post("/api/serial/command", json=command).status_code in (404, 405)


def test_directory_split_recomposes_save_dir(client, tmp_path):
    base = str(tmp_path / "data" / "TL")
    # Setting the base directory alone recomposes save_dir under it (no relative
    # part yet, so save_dir == the normalized base).
    r = client.put("/api/settings", json={"record_directory": base})
    assert r.status_code == 200
    assert r.json()["record_directory"] == base
    assert r.json()["save_dir"] == base

    # Adding a relative sub-path joins it onto the base; the base is untouched.
    r = client.put("/api/settings", json={"relative_directory": "250701/Fly1/001"})
    assert r.status_code == 200
    assert r.json()["record_directory"] == base
    assert r.json()["relative_directory"] == "250701/Fly1/001"
    assert r.json()["save_dir"] == f"{base}/250701/Fly1/001"

    # Repointing the base keeps the relative part and re-joins under the new base.
    other = str(tmp_path / "scratch")
    r = client.put("/api/settings", json={"record_directory": other})
    assert r.status_code == 200
    assert r.json()["save_dir"] == f"{other}/250701/Fly1/001"


def _wait_for_presence(ws, tries=200):
    """Return the client count from the next presence message on ``ws``.

    The socket also carries preview frames, state, settings and telemetry, so
    skip past those until a presence message arrives.
    """
    for _ in range(tries):
        message = ws.receive()
        if message.get("text"):
            payload = json.loads(message["text"])
            if payload["type"] == "presence":
                return payload["clients"]
    raise AssertionError("no presence message received")


def test_websocket_broadcasts_presence(client):
    # One browser: it learns it is alone.
    with client.websocket_connect("/api/ws") as ws1:
        assert _wait_for_presence(ws1) == 1
        # A second browser connects: both are told the count rose to 2.
        with client.websocket_connect("/api/ws") as ws2:
            assert _wait_for_presence(ws2) == 2
            assert _wait_for_presence(ws1) == 2
        # After it leaves, the first sees the count fall back to 1.
        assert _wait_for_presence(ws1) == 1


def test_websocket_preview_and_telemetry(client):
    import cv2

    got_state = got_settings = False
    frames = []
    with client.websocket_connect("/api/ws") as ws:
        for _ in range(60):
            message = ws.receive()
            if message.get("text"):
                payload = json.loads(message["text"])
                got_state |= payload["type"] in ("state", "telemetry")
                got_settings |= payload["type"] == "settings"
            elif message.get("bytes"):
                frames.append(message["bytes"])
            if got_state and got_settings and len(frames) >= 4:
                break

    assert got_state and got_settings
    assert len(frames) >= 4
    version, kind, camera_index, flags, number, ts, fps, dropped = FRAME_HEADER.unpack(
        frames[0][: FRAME_HEADER.size]
    )
    assert (version, kind) == (1, 1)
    assert camera_index in (0, 1)
    jpeg = np.frombuffer(frames[0][FRAME_HEADER.size :], np.uint8)
    image = cv2.imdecode(jpeg, cv2.IMREAD_GRAYSCALE)
    assert image is not None and image.size > 0
    assert max(image.shape) <= 640  # downscaled preview


def test_plugin_contributions_wired_into_app(tmp_path):
    """A loaded plugin's router, status, and WS handler reach the app."""
    from fastapi import APIRouter

    from octacam.plugins.base import Plugin, PluginManager

    class StubPlugin(Plugin):
        name = "stub"

        def __init__(self):
            self.jogs = []

        def status(self):
            return {"hello": "world"}

        def api_router(self):
            router = APIRouter()

            @router.post("/api/stub/ping")
            def ping():
                return {"pong": True}

            return router

        def on_ws_message(self, message, client_id):
            if message.get("type") != "stubjog":
                return False
            self.jogs.append((message.get("n"), client_id))
            return True

    system = CameraSystem(EMULATED_SERIALS)
    system.load_config(tmp_path)
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(system, settings)
    controller.start_preview()
    stub = StubPlugin()
    app = create_app(
        controller, OctacamConfig(), PluginManager([stub]), config_dir=str(tmp_path)
    )
    try:
        with TestClient(app) as client:
            # generic plugin status surfaced on /api/system
            system_info = client.get("/api/system").json()
            assert system_info["plugins"] == {"stub": {"ready": True, "hello": "world"}}
            # contributed REST endpoint is mounted
            assert client.post("/api/stub/ping").json() == {"pong": True}
            # WS messages are dispatched to the plugin with the client id
            with client.websocket_connect("/api/ws") as ws:
                ws.send_text(json.dumps({"type": "stubjog", "n": 5}))
                deadline = time.monotonic() + 3
                while not stub.jogs and time.monotonic() < deadline:
                    time.sleep(0.05)
            assert len(stub.jogs) == 1
            n, client_id = stub.jogs[0]
            assert n == 5
            assert isinstance(client_id, int) and client_id > 0
    finally:
        controller.close()


def test_plugin_web_assets_served_and_advertised(tmp_path):
    """A plugin's co-located JS/CSS are mounted at /plugins/<name>/ (before the
    SPA catch-all) and advertised in /api/system so app.js can import them."""
    from octacam.plugins.base import Plugin, PluginManager

    assets = tmp_path / "stub_assets"
    assets.mkdir()
    (assets / "stub.js").write_text("export default class StubTab {}\n")
    (assets / "stub.css").write_text(".stub {}\n")

    class StubWebPlugin(Plugin):
        name = "stub"

        def web_assets(self):
            return assets

    system = CameraSystem(EMULATED_SERIALS)
    system.load_config(tmp_path)
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(system, settings)
    controller.start_preview()
    app = create_app(
        controller,
        OctacamConfig(),
        PluginManager([StubWebPlugin()]),
        config_dir=str(tmp_path),
    )
    try:
        with TestClient(app) as client:
            # /api/system advertises the entry module + css under the plugin entry
            web = client.get("/api/system").json()["plugins"]["stub"]["web"]
            assert web == {
                "module": "/plugins/stub/stub.js",
                "css": "/plugins/stub/stub.css",
            }
            # The JS is served as a script, NOT the SPA's index.html. A
            # text/html response here would mean the "/" catch-all (html=True)
            # shadowed the plugin mount — the browser would then refuse to run
            # it as a module. This guards the mount ordering in create_app.
            r = client.get("/plugins/stub/stub.js")
            assert r.status_code == 200
            assert "javascript" in r.headers["content-type"]
            assert "<!doctype html" not in r.text.lower()
            # A missing plugin asset 404s (no html=True on the plugin mount, so
            # it must not fall through to the SPA).
            assert client.get("/plugins/stub/missing.js").status_code == 404
    finally:
        controller.close()


def test_recording_cycle_over_rest(client, tmp_path):
    save_dir = tmp_path / "rec" / "001"
    response = client.post("/api/recording/start", json={"confirm_overwrite": False})
    assert response.status_code == 202, response.text

    busy = client.post("/api/recording/start", json={})
    assert busy.status_code == 409
    assert busy.json()["status"] == "busy"

    deadline = time.monotonic() + 25
    state = None
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        if state["state"] == "preview" and state["cameras"][0]["frames"]:
            break
        time.sleep(0.2)
    assert state is not None and state["state"] == "preview"

    videos = sorted(save_dir.glob("*.mkv"))
    assert len(videos) == 2
    # Per-frame CSVs are opt-in now; by default only the compact summary lands.
    assert not any(v.with_suffix(".csv").exists() for v in videos)
    summary = json.loads((save_dir / "recording_summary.json").read_text())
    assert summary["record_form"] == "display"
    assert len(summary["cameras"]) == 2
    assert all(c["frames"] > 0 for c in summary["cameras"])
    assert "dropped_frames_note" in summary
    # save dir auto-incremented for the next trial
    assert client.get("/api/settings").json()["save_dir"].endswith("002")

    # an existing dir requires confirmation
    save_dir.with_name("002").mkdir(parents=True, exist_ok=True)
    needs_confirm = client.post("/api/recording/start", json={})
    assert needs_confirm.status_code == 409
    assert needs_confirm.json()["status"] == "needs_confirm"


def test_recording_with_split_directory(client, tmp_path):
    base = tmp_path / "data" / "TL"
    assert (
        client.put(
            "/api/settings",
            json={"record_directory": str(base), "relative_directory": "day/001"},
        ).status_code
        == 200
    )
    save_dir = base / "day" / "001"

    response = client.post("/api/recording/start", json={"confirm_overwrite": False})
    assert response.status_code == 202, response.text

    deadline = time.monotonic() + 25
    state = None
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        if state["state"] == "preview" and state["cameras"][0]["frames"]:
            break
        time.sleep(0.2)
    assert state is not None and state["state"] == "preview"

    # Videos land under base/relative, and the summary records the relative part
    # verbatim (what the transfer step mirrors onto the NAS).
    assert len(sorted(save_dir.glob("*.mkv"))) == 2
    summary = json.loads((save_dir / "recording_summary.json").read_text())
    assert summary["relative_directory"] == "day/001"

    # Both halves increment together; the base stays fixed.
    settings = client.get("/api/settings").json()
    assert settings["record_directory"] == str(base)
    assert settings["relative_directory"] == "day/002"
    assert settings["save_dir"] == f"{base}/day/002"


def test_recording_writes_csv_when_enabled(client, tmp_path):
    save_dir = tmp_path / "rec" / "001"
    assert (
        client.put("/api/settings", json={"save_frame_timestamps": True}).status_code
        == 200
    )
    response = client.post("/api/recording/start", json={"confirm_overwrite": False})
    assert response.status_code == 202, response.text

    deadline = time.monotonic() + 25
    state = None
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        if state["state"] == "preview" and state["cameras"][0]["frames"]:
            break
        time.sleep(0.2)
    assert state is not None and state["state"] == "preview"

    videos = sorted(save_dir.glob("*.mkv"))
    assert len(videos) == 2
    assert all(v.with_suffix(".csv").exists() for v in videos)
    for video in videos:
        lines = video.with_suffix(".csv").read_text().splitlines()
        assert lines[0] == "frame_index,timestamp,dropped"


def test_live_transform_is_baked_into_display_recording(client, tmp_path):
    save_dir = tmp_path / "rec" / "001"
    # The View tab pushes the composed transform here; rotate camera 0 by 90deg.
    r = client.put("/api/cameras/0/transform", json={"rotation_deg": 90})
    assert r.status_code == 200
    assert r.json()["transform"] == {
        "rotation_deg": 90,
        "flip_h": False,
        "flip_v": False,
    }

    response = client.post("/api/recording/start", json={"confirm_overwrite": False})
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 25
    state = None
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        if state["state"] == "preview" and state["cameras"][0]["frames"]:
            break
        time.sleep(0.2)
    assert state is not None and state["state"] == "preview"

    summary = json.loads((save_dir / "recording_summary.json").read_text())
    cams = {c["serial"]: c for c in summary["cameras"]}
    rotated = cams["0815-0000"]
    plain = cams["0815-0001"]
    assert rotated["transform_applied"] is True
    assert plain["transform_applied"] is False
    # A 90deg rotation swaps the recorded dimensions vs the un-rotated camera.
    assert (rotated["width"], rotated["height"]) == (plain["height"], plain["width"])


def test_transform_endpoint_locked_while_recording(client):
    client.post("/api/recording/start", json={"confirm_overwrite": False})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if client.get("/api/state").json()["state"] in ("waiting", "recording"):
            break
        time.sleep(0.05)
    blocked = client.put("/api/cameras/0/transform", json={"rotation_deg": 90})
    assert blocked.status_code == 409


# --------------------------------------------- camera parameters / config save


def test_camera_param_endpoints(client):
    system = client.get("/api/system").json()
    assert "params" in system["cameras"][0]  # descriptors served on /api/system

    params = client.get("/api/cameras/0/params").json()
    assert {"index", "serial", "width", "height", "params"} <= set(params)
    assert params["params"]["exposure"]["value"] > 0

    assert client.get("/api/cameras/99/params").status_code == 404

    # live exposure: 200 + echoed device value
    r = client.put("/api/cameras/0/params", json={"name": "exposure", "value": 1500.0})
    assert r.status_code == 200, r.text
    assert r.json()["updated"][0]["params"]["exposure"]["value"] == 1500.0

    # geometry change echoes the new width
    r = client.put("/api/cameras/0/params", json={"name": "width", "value": 640})
    assert r.status_code == 200 and r.json()["updated"][0]["width"] == 640

    # apply to all cameras
    r = client.put(
        "/api/cameras/0/params",
        json={"name": "exposure", "value": 900.0, "scope": "all"},
    )
    assert r.status_code == 200 and len(r.json()["updated"]) == 2

    # validation
    assert (
        client.put(
            "/api/cameras/0/params", json={"name": "bogus", "value": 1}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/cameras/0/params", json={"name": "gain", "value": 1, "x": 2}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/cameras/99/params", json={"name": "gain", "value": 1}
        ).status_code
        == 404
    )


def test_browse_endpoint(client, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "afile.txt").write_text("x")

    listing = client.post("/api/browse", json={"path": str(tmp_path)}).json()
    assert listing["path"] == str(tmp_path)
    # sorted, directories only, dotfiles and plain files omitted
    assert listing["entries"] == ["alpha", "beta"]
    assert listing["parent"] == str(tmp_path.parent)
    assert listing["writable"] is True

    # descend into a subfolder
    deeper = client.post("/api/browse", json={"path": str(tmp_path / "alpha")}).json()
    assert deeper["path"] == str(tmp_path / "alpha")
    assert deeper["parent"] == str(tmp_path)

    # a not-yet-created path falls back to its nearest existing ancestor
    nope = client.post(
        "/api/browse", json={"path": str(tmp_path / "alpha" / "x" / "y")}
    ).json()
    assert nope["path"] == str(tmp_path / "alpha")

    # blank path opens at the current save dir's nearest existing ancestor
    # (the fixture's save_dir is tmp_path/rec/001, which does not exist yet)
    assert client.post("/api/browse", json={"path": ""}).json()["path"] == str(tmp_path)

    # default body (no path) is accepted; unknown field is rejected
    assert client.post("/api/browse").status_code == 200
    assert client.post("/api/browse", json={"x": 1}).status_code == 422


def test_camera_name_endpoint(client):
    r = client.put("/api/cameras/0/name", json={"name": "left"})
    assert r.status_code == 200, r.text
    assert r.json() == {"index": 0, "serial": EMULATED_SERIALS[0], "name": "left"}

    # the live rename is reflected by /api/system and /api/state
    assert client.get("/api/system").json()["cameras"][0]["name"] == "left"
    assert client.get("/api/state").json()["cameras"][0]["name"] == "left"

    # surrounding whitespace is trimmed
    trimmed = client.put("/api/cameras/1/name", json={"name": "  right  "})
    assert trimmed.json()["name"] == "right"

    # a name already taken by another camera is rejected
    assert client.put("/api/cameras/1/name", json={"name": "left"}).status_code == 422
    # renaming a camera to its own current name is a no-op success
    assert client.put("/api/cameras/0/name", json={"name": "left"}).status_code == 200

    # path separators and blank names are rejected (the name is a filename stem)
    assert client.put("/api/cameras/0/name", json={"name": "a/b"}).status_code == 422
    assert client.put("/api/cameras/0/name", json={"name": "   "}).status_code == 422

    # bad index, and strict-model violations
    assert client.put("/api/cameras/9/name", json={"name": "x"}).status_code == 404
    assert (
        client.put("/api/cameras/0/name", json={"name": "x", "y": 1}).status_code == 422
    )
    assert client.put("/api/cameras/0/name", json={}).status_code == 422


def test_camera_name_locked_while_recording(client):
    started = client.post("/api/recording/start", json={"confirm_overwrite": True})
    assert started.status_code == 202, started.text
    try:
        locked = client.put("/api/cameras/0/name", json={"name": "left"})
        assert locked.status_code == 409
    finally:
        client.controller.stop_recording(abort=True)


def test_camera_name_used_for_recording_file(client, tmp_path):
    save_dir = tmp_path / "rec" / "001"
    assert (
        client.put("/api/cameras/0/name", json={"name": "cam-left"}).status_code == 200
    )
    assert (
        client.put("/api/cameras/1/name", json={"name": "cam-right"}).status_code == 200
    )

    response = client.post("/api/recording/start", json={"confirm_overwrite": True})
    assert response.status_code == 202, response.text

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if client.get("/api/state").json()["state"] == "preview":
            break
        time.sleep(0.2)

    # the per-camera video files are named after the renamed cameras
    assert (save_dir / "cam-left.mkv").exists()
    assert (save_dir / "cam-right.mkv").exists()


def test_config_save_rejects_unsafe_camera_name(client, tmp_path):
    # Path-traversal / separator names are rejected at the save boundary, just
    # as the live-rename endpoint rejects them (the name is a filename stem).
    for bad in ("a/b", "..", "."):
        r = client.post(
            "/api/config/save",
            json={
                "target": "active",
                "save_sensor": False,
                "cameras": [{"serial": EMULATED_SERIALS[0], "name": bad}],
            },
        )
        assert r.status_code == 422, (bad, r.text)

    # Two cameras sharing a name would collide on one video file -> rejected.
    dup = client.post(
        "/api/config/save",
        json={
            "target": "active",
            "save_sensor": False,
            "cameras": [
                {"serial": EMULATED_SERIALS[0], "name": "same"},
                {"serial": EMULATED_SERIALS[1], "name": "same"},
            ],
        },
    )
    assert dup.status_code == 422, dup.text

    # A safe, unique name is accepted and persisted trimmed.
    ok = client.post(
        "/api/config/save",
        json={
            "target": "active",
            "save_sensor": False,
            "cameras": [{"serial": EMULATED_SERIALS[0], "name": "  cam-left  "}],
        },
    )
    assert ok.status_code == 200, ok.text
    toml = (tmp_path / "octacam_config.toml").read_text()
    assert 'name = "cam-left"' in toml


def test_camera_param_reset_endpoint(client):
    # The fixture starts with no <serial>.pfs, so save the current params into
    # the active config dir to create a baseline to reset back to.
    saved = client.post(
        "/api/config/save", json={"target": "active", "save_display": False}
    )
    assert saved.status_code == 200, saved.text

    baseline = client.get("/api/cameras/0/params").json()["params"]["exposure"]["value"]
    moved = client.put(
        "/api/cameras/0/params", json={"name": "exposure", "value": baseline + 1000.0}
    ).json()
    assert abs(moved["updated"][0]["params"]["exposure"]["value"] - baseline) > 1.0

    # reset the selected camera (no body -> default scope "selected")
    reset = client.post("/api/cameras/0/params/reset")
    assert reset.status_code == 200, reset.text
    restored = reset.json()["updated"][0]["params"]["exposure"]["value"]
    assert abs(restored - baseline) < 1.0

    # reset all cameras
    reset_all = client.post("/api/cameras/0/params/reset", json={"scope": "all"})
    assert reset_all.status_code == 200, reset_all.text
    assert len(reset_all.json()["updated"]) == 2

    assert client.post("/api/cameras/99/params/reset").status_code == 404
    # unknown body field rejected by the strict model
    assert client.post("/api/cameras/0/params/reset", json={"x": 1}).status_code == 422


def test_camera_param_reset_without_config_pfs(client):
    # No <serial>.pfs in the active config dir -> nothing to reset to (422).
    assert client.post("/api/cameras/0/params/reset").status_code == 422


def test_camera_param_reset_rejects_invalid_pfs(client, tmp_path):
    # A hand-edited / externally-placed .pfs the device rejects must yield a
    # clean 422 (not a 500) and must not strand the live preview.
    (tmp_path / f"{EMULATED_SERIALS[0]}.pfs").write_text("not a feature stream\n")
    r = client.post("/api/cameras/0/params/reset")
    assert r.status_code == 422, r.text
    # the camera is still previewing afterward, so live edits keep working
    ok = client.put("/api/cameras/0/params", json={"name": "exposure", "value": 1200.0})
    assert ok.status_code == 200, ok.text


def test_camera_param_reset_locked_while_recording(client):
    client.post("/api/config/save", json={"target": "active", "save_display": False})
    started = client.post("/api/recording/start", json={"confirm_overwrite": True})
    assert started.status_code == 202, started.text
    try:
        locked = client.post("/api/cameras/0/params/reset")
        assert locked.status_code == 409
    finally:
        client.controller.stop_recording(abort=True)


def test_camera_params_locked_while_recording(client):
    started = client.post("/api/recording/start", json={"confirm_overwrite": True})
    assert started.status_code == 202, started.text
    try:
        locked = client.put(
            "/api/cameras/0/params", json={"name": "width", "value": 640}
        )
        assert locked.status_code == 409
    finally:
        client.controller.stop_recording(abort=True)


def _save_client(tmp_path, config_dir):
    from octacam.config import parse_config

    system = CameraSystem(EMULATED_SERIALS)
    system.load_config(config_dir)
    config = parse_config(config_dir / "octacam_config.toml")
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(system, settings)
    controller.start_preview()
    app = create_app(controller, config, None, config_dir=str(config_dir))
    return controller, app


def test_config_save_active_and_new(tmp_path):
    active = tmp_path / "rigs" / "active"
    active.mkdir(parents=True)
    (active / "octacam_config.toml").write_text(
        '[gui]\nsave_directory_default = "/data/%y%m%d/001"\n'
    )
    (active / "fictrac_camera_config.pfs").write_text("aux\n")  # helper config

    controller, app = _save_client(tmp_path, active)
    cams = [
        {"serial": s, "rotation_deg": 90.0, "scale_x": -1.0} for s in EMULATED_SERIALS
    ]
    try:
        with TestClient(app) as client:
            # save to active: writes .pfs + .toml, refreshes the live config
            r = client.post(
                "/api/config/save", json={"target": "active", "cameras": cams}
            )
            assert r.status_code == 200, r.text
            assert sorted(r.json()["cameras_written"]) == EMULATED_SERIALS
            assert (active / f"{EMULATED_SERIALS[0]}.pfs").exists()
            toml = (active / "octacam_config.toml").read_text()
            assert "rotation_deg = 90.0" in toml
            assert "%y%m%d" in toml  # strftime template preserved
            # /api/system reflects the saved transform immediately
            sysinfo = client.get("/api/system").json()
            assert sysinfo["cameras"][0]["transform"]["rotation_deg"] == 90.0

            # save to a new sibling dir
            r = client.post(
                "/api/config/save",
                json={"target": "new", "name": "variant", "cameras": cams},
            )
            assert r.status_code == 200, r.text
            new_dir = tmp_path / "rigs" / "variant"
            assert (new_dir / "octacam_config.toml").exists()
            assert (new_dir / f"{EMULATED_SERIALS[0]}.pfs").exists()
            assert (new_dir / "fictrac_camera_config.pfs").exists()  # aux copied

            # collision without overwrite, then with
            again = client.post(
                "/api/config/save",
                json={"target": "new", "name": "variant", "cameras": cams},
            )
            assert again.status_code == 409
            forced = client.post(
                "/api/config/save",
                json={
                    "target": "new",
                    "name": "variant",
                    "overwrite": True,
                    "cameras": cams,
                },
            )
            assert forced.status_code == 200

            # path-traversal name rejected
            bad = client.post(
                "/api/config/save",
                json={"target": "new", "name": "../evil", "cameras": cams},
            )
            assert bad.status_code == 422
    finally:
        controller.close()


def test_config_save_refused_while_recording(tmp_path):
    active = tmp_path / "rigs" / "active"
    active.mkdir(parents=True)
    (active / "octacam_config.toml").write_text("[gui]\nfps_default = 50.0\n")
    controller, app = _save_client(tmp_path, active)
    try:
        with TestClient(app) as client:
            started = client.post(
                "/api/recording/start", json={"confirm_overwrite": True}
            )
            assert started.status_code == 202, started.text
            refused = client.post(
                "/api/config/save", json={"target": "active", "cameras": []}
            )
            assert refused.status_code == 409
            controller.stop_recording(abort=True)
    finally:
        controller.close()


def test_sender_survives_send_after_socket_close():
    """A send racing socket teardown must not crash the ASGI app.

    When the connection has already closed (client gone, or uvicorn sent the
    close frame on shutdown), the ASGI layer raises a bare RuntimeError from
    send_* rather than WebSocketDisconnect. The sender task must swallow it so
    the endpoint's `await sender` teardown doesn't re-raise it and surface as
    the "Unexpected ASGI message 'websocket.send'..." crash.
    """
    from starlette.websockets import WebSocketState

    from octacam.web.app import _Client

    class _ClosedWS:
        client_state = WebSocketState.CONNECTED  # peer still looks connected

        async def send_text(self, _message):
            raise RuntimeError(
                "Unexpected ASGI message 'websocket.send', after sending "
                "'websocket.close' or response already completed."
            )

        async def send_bytes(self, _message):  # pragma: no cover
            raise RuntimeError("socket already closed")

    client = _Client(_ClosedWS())
    client.queue_text("state", "{}")

    # Must return cleanly (and promptly) instead of propagating RuntimeError.
    asyncio.run(asyncio.wait_for(client.sender(), timeout=1.0))


def test_sender_skips_send_once_peer_disconnected():
    """If the peer is already gone, the sender shouldn't even attempt a send."""
    from starlette.websockets import WebSocketState

    from octacam.web.app import _Client

    ws = Mock()
    ws.client_state = WebSocketState.DISCONNECTED
    client = _Client(ws)
    client.queue_text("state", "{}")
    client.queue_frame(0, b"jpegbytes")

    asyncio.run(asyncio.wait_for(client.sender(), timeout=1.0))
    ws.send_text.assert_not_called()
    ws.send_bytes.assert_not_called()


def test_client_is_ready_for_tracks_unsent_frames():
    """is_ready_for gates preview encoding on the client having drained the
    previous frame, so a backed-up client stops the rig re-encoding previews
    it can't keep up with."""
    from starlette.websockets import WebSocketState

    from octacam.web.app import _Client

    ws = Mock()
    ws.client_state = WebSocketState.CONNECTED
    client = _Client(ws)

    # Fresh client: nothing pending, ready for every camera.
    assert client.is_ready_for(0)
    assert client.is_ready_for(1)

    # A queued-but-unsent frame marks that camera not-ready (a new encode would
    # only overwrite it), while other cameras stay independently ready.
    client.queue_frame(0, b"jpeg0")
    assert not client.is_ready_for(0)
    assert client.is_ready_for(1)

    # Draining the pending dict (what sender() does on each wakeup) clears it.
    client.frames.clear()
    assert client.is_ready_for(0)
