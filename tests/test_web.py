"""Web backend integration tests against the camera emulator."""

import asyncio
import contextlib
import json
import math
import time
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi.testclient import TestClient
from helpers import wait_until

from octacam.cameras import CameraSystem
from octacam.config import OctacamConfig, RecordingSettings
from octacam.controller import RecordingController
from octacam.transform import RECORDING_INFO_DIRNAME
from octacam.web.app import create_app
from octacam.web.preview import FRAME_HEADER

EMULATED_SERIALS = ["0815-0000", "0815-0001"]


@pytest.fixture
def client(tmp_path):
    system = CameraSystem(EMULATED_SERIALS, backend="basler")
    assert len(system) == 2, "PYLON_CAMEMU=2 expected"
    system.load_config(tmp_path)
    config = OctacamConfig()
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    # As cli.gui does: the app and the controller share the config dir.
    controller = RecordingController(system, settings, config_dir=tmp_path)
    controller.start_preview()
    app = create_app(controller, config, config_dir=str(tmp_path))
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
    system = CameraSystem(EMULATED_SERIALS, backend="basler")
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


def test_shutdown_process_after_flag(shutdown_client):
    state = shutdown_client.app.state.app_state
    # Explicit true is recorded so cli.gui starts detached processing on exit.
    assert shutdown_client.post("/api/shutdown", json={"process_after": True}).status_code == 202
    assert state.process_after is True
    # An empty body (older clients / plain shutdown) leaves it false.
    shutdown_client.post("/api/shutdown")
    assert state.process_after is False
    # Explicit false, too.
    shutdown_client.post("/api/shutdown", json={"process_after": False})
    assert state.process_after is False


def test_state_snapshot_has_recordings_made(client):
    body = client.get("/api/state").json()
    assert body.get("recordings_made") == 0


def test_static_assets_served_no_cache(client):
    # The GUI's static assets are unversioned, so they are served with
    # Cache-Control: no-cache and the browser revalidates on every reload —
    # development always sees up-to-date pages instead of a stale cached copy.
    for path in ("/", "/style.css", "/js/app.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-cache" in response.headers.get("cache-control", ""), path

    # no-cache is not no-store: an unchanged asset still revalidates to a cheap
    # 304 (via ETag), so nothing is re-downloaded unless it actually changed.
    fresh = client.get("/style.css")
    etag = fresh.headers.get("etag")
    assert etag, "static assets should carry an ETag for revalidation"
    revalidated = client.get("/style.css", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304


def test_system_and_state_report_ready(client):
    # A normally-constructed controller is ready; /api/system and /api/state both
    # say so, and the WS handshake sends the current `system` descriptor.
    system = client.get("/api/system").json()
    assert system["ready"] is True and system["init_error"] is None
    assert client.get("/api/state").json()["ready"] is True
    with client.websocket_connect("/api/ws") as ws:
        sys_msg = None
        for _ in range(60):
            message = ws.receive()
            if message.get("text"):
                payload = json.loads(message["text"])
                if payload["type"] == "system":
                    sys_msg = payload
                    break
        assert sys_msg is not None
        assert sys_msg["ready"] is True
        assert len(sys_msg["cameras"]) == 2


def test_deferred_startup_serves_then_fills_in(tmp_path):
    # The GUI serves against a hardware-free placeholder: /api/system reports
    # ready=False + no cameras, and a browser connecting during init gets the
    # not-ready descriptor over the socket, then a ready one once the real system
    # is attached and broadcast — filling the grid without a reload.
    pending = CameraSystem.pending()
    assert len(pending) == 0
    settings = RecordingSettings(fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec"))
    controller = RecordingController(pending, settings, ready=False)
    app = create_app(controller, OctacamConfig(), config_dir=str(tmp_path))
    try:
        with TestClient(app) as client:
            sys0 = client.get("/api/system").json()
            assert sys0["ready"] is False and sys0["cameras"] == []
            assert client.get("/api/state").json()["ready"] is False

            with client.websocket_connect("/api/ws") as ws:

                def next_system():
                    for _ in range(120):
                        message = ws.receive()
                        if message.get("text"):
                            payload = json.loads(message["text"])
                            if payload["type"] == "system":
                                return payload
                    return None

                first = next_system()
                assert first is not None and first["ready"] is False

                # Attach the real (emulated) system as the init thread does, then
                # broadcast; the connected client receives a ready `system`.
                real = CameraSystem(EMULATED_SERIALS, backend="basler")
                real.load_config(tmp_path)
                controller.attach_system(real)
                app.state.app_state.broadcast_system()

                ready_msg = None
                for _ in range(5):
                    msg = next_system()
                    if msg and msg["ready"]:
                        ready_msg = msg
                        break
                assert ready_msg is not None
                assert len(ready_msg["cameras"]) == 2

            assert client.get("/api/system").json()["ready"] is True
    finally:
        controller.close()  # closes the attached real system (once)


def test_system_and_settings_endpoints(client):
    system = client.get("/api/system").json()
    assert len(system["cameras"]) == 2
    assert system["cameras"][0]["width"] > 0
    assert {f["save_method"] for f in system["formats"]} == {
        "ffmpeg",
        "nvenc",
        "raw",
    }
    # no plugins loaded in tests -> empty plugin status, no serial endpoint
    assert system["plugins"] == {}
    # the rig's default GUI theme is surfaced for the client (defaults to dark)
    assert system["theme"] == "dark"
    # No driving plugin loaded -> the "managed" trigger source is unavailable.
    assert system["managed_trigger_available"] is False

    settings = client.get("/api/settings").json()
    assert settings["fps"] == 50.0
    # New recording-output toggles default to display form, CSV off.
    assert settings["record_form"] == "display"
    assert settings["save_frame_timestamps"] is False
    # Preview trigger source defaults to mirroring the recording trigger.
    assert settings["preview_trigger_source"] == "auto"

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
    # Preview trigger source + the managed recording source round-trip; bad values 422.
    pv = client.put("/api/settings", json={"preview_trigger_source": "free_running"})
    assert pv.status_code == 200 and pv.json()["preview_trigger_source"] == "free_running"
    mg = client.put("/api/settings", json={"trigger_source": "managed"})
    assert mg.status_code == 200 and mg.json()["trigger_source"] == "managed"
    assert (
        client.put("/api/settings", json={"preview_trigger_source": "x"}).status_code
        == 422
    )
    assert client.put("/api/settings", json={"trigger_source": "x"}).status_code == 422
    # ffmpeg_params is a live setting (the GUI's Advanced box edits it); a bad
    # save_method is still rejected, and other encoder knobs stay unknown (422).
    ffmpeg = client.put("/api/settings", json={"ffmpeg_params": "-c:v ffv1"})
    assert ffmpeg.status_code == 200
    assert ffmpeg.json()["ffmpeg_params"] == "-c:v ffv1"
    assert client.put("/api/settings", json={"ffmpeg_params": 'a "b'}).status_code == 422
    assert client.put("/api/settings", json={"save_method": "vp9"}).status_code == 422

    # The GPU save method + its params/session-limit knobs round-trip; a negative
    # limit 422s. max_nvenc_sessions=null selects auto-detect.
    gpu = client.put(
        "/api/settings",
        json={
            "save_method": "nvenc",
            "nvenc_params": "-c:v h264_nvenc -cq 20 -pix_fmt yuv420p",
            "max_nvenc_sessions": 4,
        },
    )
    assert gpu.status_code == 200
    assert gpu.json()["save_method"] == "nvenc"
    assert gpu.json()["nvenc_params"] == "-c:v h264_nvenc -cq 20 -pix_fmt yuv420p"
    assert gpu.json()["max_nvenc_sessions"] == 4
    auto = client.put("/api/settings", json={"max_nvenc_sessions": None})
    assert auto.status_code == 200 and auto.json()["max_nvenc_sessions"] is None
    assert (
        client.put("/api/settings", json={"max_nvenc_sessions": -1}).status_code == 422
    )

    # A bad value or unknown key answers 422 with a message naming it, which the
    # Record tab shows as is.
    bad_fps = client.put("/api/settings", json={"fps": -1})
    assert bad_fps.status_code == 422 and bad_fps.json()["detail"].startswith("fps: ")
    bad_duration = client.put("/api/settings", json={"duration_s": -1})
    assert bad_duration.status_code == 422
    assert bad_duration.json()["detail"].startswith("duration_s: ")
    named_self = client.put("/api/settings", json={"self": 1})
    assert named_self.status_code == 422
    assert named_self.json()["detail"] == "Unknown settings: ['self']"
    bogus = client.put("/api/settings", json={"bogus": 1})
    assert bogus.status_code == 422 and "bogus" in bogus.json()["detail"]
    assert client.put("/api/settings", json={"crf": 18}).status_code == 422
    assert client.put("/api/settings", json={"fps": None}).status_code == 422
    assert client.put("/api/settings", json=[1]).status_code == 422

    # writer_queue_size round-trips; sub-1 and non-integer values are rejected.
    assert settings["writer_queue_size"] == 64
    wq = client.put("/api/settings", json={"writer_queue_size": 128})
    assert wq.status_code == 200 and wq.json()["writer_queue_size"] == 128
    assert client.put("/api/settings", json={"writer_queue_size": 0}).status_code == 422
    assert client.put("/api/settings", json={"writer_queue_size": -1}).status_code == 422
    as_bool = client.put("/api/settings", json={"writer_queue_size": True})
    assert as_bool.status_code == 422

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


def test_nvenc_capabilities_endpoint(client, monkeypatch):
    # The GUI fetches this lazily to show/default the GPU session cap. Mock the
    # detector so the test never loads the GPU.
    from octacam.web import system

    monkeypatch.setattr(system, "nvenc_max_sessions", lambda encoder="h264_nvenc": 6)
    data = client.get("/api/nvenc/capabilities").json()
    assert data["available"] is True
    assert data["max_sessions"] == 6
    assert data["encoder"] == "h264_nvenc"
    assert data["default_params"] == system.NVENC_H264_PARAMS


def test_nvenc_capabilities_unavailable(client, monkeypatch):
    from octacam.web import system

    monkeypatch.setattr(system, "nvenc_max_sessions", lambda encoder="h264_nvenc": None)
    data = client.get("/api/nvenc/capabilities").json()
    assert data["available"] is False and data["max_sessions"] is None


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


def test_process_params_are_live_settings(client):
    # The Process section's transcode/transfer knobs are live settings that the
    # snapshot bakes into each recording for `octacam process`.
    r = client.put(
        "/api/settings",
        json={
            "transcode_ffmpeg_params": "-c:v libx264 -crf 20 -pix_fmt yuv420p",
            "transfer_directory": "~/store/TL",
            "transfer_checksum": False,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transcode_ffmpeg_params"] == "-c:v libx264 -crf 20 -pix_fmt yuv420p"
    assert body["transfer_directory"] == "~/store/TL"
    assert body["transfer_checksum"] is False
    # Unparseable transcode args (bad quoting) are rejected up front rather than
    # silently falling back to the default when `octacam process` runs.
    assert (
        client.put(
            "/api/settings", json={"transcode_ffmpeg_params": 'a "b'}
        ).status_code
        == 422
    )


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


def test_websocket_replays_event_backlog(client):
    # A (re)connecting browser should receive the controller's recent event
    # backlog so its log shows history instead of starting blank.
    controller = client.controller
    controller._event("info", "first historical event")
    controller._event("warning", "second historical event")

    seen = []
    with client.websocket_connect("/api/ws") as ws:
        for _ in range(200):
            message = ws.receive()
            if message.get("text"):
                payload = json.loads(message["text"])
                if payload.get("type") == "event":
                    seen.append(payload["message"])
                    if "second historical event" in seen:
                        break
    assert "first historical event" in seen
    assert "second historical event" in seen


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
    (version, kind, camera_index, flags, number, ts, fps, dropped, cx, cy, cw, ch,
     sw, sh) = FRAME_HEADER.unpack(frames[0][: FRAME_HEADER.size])
    assert (version, kind) == (2, 1)
    assert camera_index in (0, 1)
    # A default client is un-cropped: the crop rect is the whole sensor.
    assert (cx, cy) == (0, 0) and (cw, ch) == (sw, sh)
    jpeg = np.frombuffer(frames[0][FRAME_HEADER.size :], np.uint8)
    image = cv2.imdecode(jpeg, cv2.IMREAD_GRAYSCALE)
    assert image is not None and image.size > 0
    assert max(image.shape) <= 640  # downscaled preview


def test_plugin_contributions_wired_into_app(tmp_path):
    """A loaded plugin's router, status, and WS handler reach the app, and the
    plugin gets the controller and a broadcast that reaches the GUI clients."""
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
            self.broadcast("stub_state", {"n": message.get("n")})
            return True

    system = CameraSystem(EMULATED_SERIALS, backend="basler")
    system.load_config(tmp_path)
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    stub = StubPlugin()
    controller = RecordingController(system, settings, PluginManager([stub]))
    controller.start_preview()
    app = create_app(controller, OctacamConfig(), config_dir=str(tmp_path))
    assert stub.controller is controller
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
                for _ in range(200):
                    message = ws.receive()
                    if message.get("text"):
                        payload = json.loads(message["text"])
                        if payload["type"] == "stub_state":
                            break
                else:
                    pytest.fail("the plugin's broadcast never reached the client")
                assert payload == {"type": "stub_state", "n": 5}
            assert len(stub.jogs) == 1
            n, client_id = stub.jogs[0]
            assert n == 5
            assert isinstance(client_id, int) and client_id > 0
    finally:
        controller.close()


def test_plugin_ws_message_exception_does_not_kill_socket(tmp_path):
    """A plugin's on_ws_message raising must not tear down the client socket."""
    from octacam.plugins.base import Plugin, PluginManager

    class RaisingPlugin(Plugin):
        name = "raiser"

        def on_ws_message(self, message, client_id):
            raise ValueError("boom: malformed jog value")

    system = CameraSystem(EMULATED_SERIALS, backend="basler")
    system.load_config(tmp_path)
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(system, settings, PluginManager([RaisingPlugin()]))
    controller.start_preview()
    app = create_app(controller, OctacamConfig(), config_dir=str(tmp_path))
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/api/ws") as ws:
                # A message the plugin blows up on: the socket must survive.
                ws.send_text(json.dumps({"type": "jog", "n": "not-a-number"}))
                # The socket is still live: telemetry/frames keep flowing.
                got_state = False
                for _ in range(60):
                    message = ws.receive()
                    if message.get("text"):
                        payload = json.loads(message["text"])
                        got_state |= payload["type"] in ("state", "telemetry")
                    if got_state:
                        break
                assert got_state
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
        web_dir = assets

    system = CameraSystem(EMULATED_SERIALS, backend="basler")
    system.load_config(tmp_path)
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(system, settings, PluginManager([StubWebPlugin()]))
    controller.start_preview()
    app = create_app(controller, OctacamConfig(), config_dir=str(tmp_path))
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


def test_plugin_with_a_missing_web_dir_gets_no_ui_and_a_warning(tmp_path, caplog):
    from octacam.plugins.base import Plugin, PluginManager

    class StubWebPlugin(Plugin):
        name = "stub"
        web_dir = tmp_path / "not_built"

    system = CameraSystem(EMULATED_SERIALS, backend="basler")
    settings = RecordingSettings(fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec"))
    controller = RecordingController(system, settings, PluginManager([StubWebPlugin()]))
    try:
        app = create_app(controller, OctacamConfig(), config_dir=str(tmp_path))
        assert "web_dir" in caplog.text and "does not exist" in caplog.text
        with TestClient(app) as client:
            assert "web" not in client.get("/api/system").json()["plugins"]["stub"]
            assert client.get("/plugins/stub/stub.js").status_code == 404
    finally:
        controller.close()


def _wait_for_take(client):
    """Poll /api/state until the take is over and counted; return the last state."""
    states = []

    def done():
        states.append(client.get("/api/state").json())
        return states[-1]["state"] == "preview" and states[-1]["cameras"][0]["frames"]

    wait_until(done, timeout=25, interval=0.2)
    return states[-1]


def test_recording_cycle_over_rest(client, tmp_path):
    save_dir = tmp_path / "rec" / "001"
    response = client.post("/api/recording/start", json={"confirm_overwrite": False})
    assert response.status_code == 202, response.text

    busy = client.post("/api/recording/start", json={})
    assert busy.status_code == 409
    assert busy.json()["status"] == "busy"

    state = _wait_for_take(client)
    assert state is not None and state["state"] == "preview"

    videos = sorted(save_dir.glob("*.mkv"))
    assert len(videos) == 2
    # Per-frame timestamps are opt-in now; by default only the compact summary lands.
    assert not (save_dir / RECORDING_INFO_DIRNAME / "timestamps.npz").exists()
    summary = json.loads((save_dir / RECORDING_INFO_DIRNAME / "recording_summary.json").read_text())
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

    state = _wait_for_take(client)
    assert state is not None and state["state"] == "preview"

    # Videos land under base/relative, and the summary records the relative part
    # verbatim (what the transfer step mirrors onto the destination).
    assert len(sorted(save_dir.glob("*.mkv"))) == 2
    summary = json.loads((save_dir / RECORDING_INFO_DIRNAME / "recording_summary.json").read_text())
    assert summary["relative_directory"] == "day/001"

    # Both halves increment together; the base stays fixed.
    settings = client.get("/api/settings").json()
    assert settings["record_directory"] == str(base)
    assert settings["relative_directory"] == "day/002"
    assert settings["save_dir"] == f"{base}/day/002"


def test_recording_writes_timestamps_when_enabled(client, tmp_path):
    import numpy as np

    save_dir = tmp_path / "rec" / "001"
    assert (
        client.put("/api/settings", json={"save_frame_timestamps": True}).status_code
        == 200
    )
    response = client.post("/api/recording/start", json={"confirm_overwrite": False})
    assert response.status_code == 202, response.text

    state = _wait_for_take(client)
    assert state is not None and state["state"] == "preview"

    videos = sorted(save_dir.glob("*.mkv"))
    assert len(videos) == 2
    # A single compressed file for all cameras (no per-camera CSVs).
    assert not any(save_dir.glob("*.csv"))
    with np.load(save_dir / RECORDING_INFO_DIRNAME / "timestamps.npz") as data:
        for video in videos:
            name = video.stem
            timestamps = data[f"{name}/timestamp_ns"]
            assert timestamps.dtype == np.int64
            assert len(timestamps) == len(data[f"{name}/dropped"]) > 0


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
    state = _wait_for_take(client)
    assert state is not None and state["state"] == "preview"

    summary = json.loads((save_dir / RECORDING_INFO_DIRNAME / "recording_summary.json").read_text())
    cams = {c["serial"]: c for c in summary["cameras"]}
    rotated = cams["0815-0000"]
    plain = cams["0815-0001"]
    assert rotated["transform_applied"] is True
    assert plain["transform_applied"] is False
    # A 90deg rotation swaps the recorded dimensions vs the un-rotated camera.
    assert (rotated["width"], rotated["height"]) == (plain["height"], plain["width"])


def _state(client) -> str:
    return client.get("/api/state").json()["state"]


@pytest.mark.parametrize(("route", "aborted"), [("stop", False), ("abort", True)])
def test_recording_stop_and_abort_end_a_running_take(client, tmp_path, route, aborted):
    save_dir = tmp_path / "rec" / "001"
    # A take only the request can end within this test's timeouts.
    assert client.put("/api/settings", json={"duration_s": 120}).status_code == 200
    assert client.post("/api/recording/start", json={}).status_code == 202
    assert wait_until(lambda: _state(client) == "recording", timeout=20)
    # A valid change is locked out; a bad one is a bad request in any state.
    assert client.put("/api/settings", json={"fps": 10}).status_code == 409
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 422
    assert client.put("/api/settings", json={"fps": "abc"}).status_code == 422

    response = client.post(f"/api/recording/{route}")

    assert response.status_code == 202
    assert wait_until(lambda: _state(client) == "preview", timeout=20)
    summary = json.loads(
        (save_dir / RECORDING_INFO_DIRNAME / "recording_summary.json").read_text()
    )
    assert summary["aborted"] is aborted
    assert summary["completed"] is False
    assert all(c["frames"] > 0 for c in summary["cameras"])
    # A stopped take keeps its folder and the next take gets a new one; an
    # aborted take's folder is reused.
    next_save_dir = client.get("/api/settings").json()["save_dir"]
    assert next_save_dir.endswith("001" if aborted else "002")


def _next_message(ws, kind: str, timeout: float = 20.0) -> dict:
    """The next text message of type ``kind`` on ``ws``, skipping everything
    else. Telemetry arrives twice a second, so the deadline is always checked."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = ws.receive()
        if message.get("text"):
            payload = json.loads(message["text"])
            if payload["type"] == kind:
                return payload
    raise AssertionError(f"no {kind!r} message within {timeout} s")


def test_benchmark_cancel_ends_a_running_benchmark(client):
    with client.websocket_connect("/api/ws") as ws:
        # A benchmark only the cancel can end within this test's timeouts.
        started = client.post(
            "/api/diagnostics/run", json={"duration_s": 60, "sink": "null"}
        )
        assert started.status_code == 202
        _next_message(ws, "diagnostics_progress")

        cancelled = client.post("/api/diagnostics/cancel")

        assert cancelled.status_code == 202
        report = _next_message(ws, "diagnostics")
    assert any("cancel" in note.lower() for note in report["notes"])
    assert wait_until(lambda: _state(client) == "preview", timeout=20)


def test_serial_ports_lists_the_detected_ports_without_opening_any(client, monkeypatch):
    from octacam.serial_ports import SerialPort

    ports = [
        SerialPort(
            device="/dev/ttyACM0",
            description="",
            manufacturer="Arduino",
            product=None,
            vid=0x2341,
            pid=0x0070,
            serial_number="SN123",
            hwid="",
            board_name="Arduino Nano ESP32",
            likely_microcontroller=True,
            likely_arduino=True,
        ),
        SerialPort(
            device="/dev/ttyS0",
            description="",
            manufacturer=None,
            product=None,
            vid=None,
            pid=None,
            serial_number=None,
            hwid="",
            board_name="generic serial",
            likely_microcontroller=False,
            likely_arduino=False,
        ),
    ]
    monkeypatch.setattr("octacam.serial_ports.list_serial_ports", lambda: ports)

    def open_port(*args, **kwargs):
        raise AssertionError("listing the serial ports opened one")

    monkeypatch.setattr("serial.Serial", open_port)

    response = client.get("/api/serial/ports")

    assert response.status_code == 200
    assert response.json() == {
        "ports": [
            {
                "device": "/dev/ttyACM0",
                "board_name": "Arduino Nano ESP32",
                "vid_pid": "2341:0070",
                "serial_number": "SN123",
                "likely_arduino": True,
                "likely_microcontroller": True,
            },
            {
                "device": "/dev/ttyS0",
                "board_name": "generic serial",
                "vid_pid": "?:?",
                "serial_number": None,
                "likely_arduino": False,
                "likely_microcontroller": False,
            },
        ]
    }


@contextlib.contextmanager
def _fake_rig_client(tmp_path, serials):
    """A client for a rig of fake cameras whose config lists ``serials``."""
    system = CameraSystem(serials, backend="fake")
    settings = RecordingSettings(save_dir=str(tmp_path / "rec" / "001"))
    controller = RecordingController(system, settings)
    app = create_app(controller, OctacamConfig(), config_dir=str(tmp_path))
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        controller.close()


def test_system_lists_a_configured_camera_that_was_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

    with _fake_rig_client(tmp_path, ["FAKE-0", "FAKE-9", "FAKE-1"]) as client:
        system = client.get("/api/system").json()

    assert system["missing_cameras"] == [{"serial": "FAKE-9", "reason": "not found"}]
    assert [camera["serial"] for camera in system["cameras"]] == ["FAKE-0", "FAKE-1"]


def test_system_lists_a_configured_camera_that_failed_to_open(tmp_path, monkeypatch):
    from octacam.cameras import BackendError
    from octacam.cameras.fake import FakeBackend

    monkeypatch.setenv("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")
    open_camera = FakeBackend.open

    def open_all_but_one(self):
        if self.serial_number == "FAKE-1":
            raise BackendError("simulated: device busy")
        open_camera(self)

    monkeypatch.setattr(FakeBackend, "open", open_all_but_one)

    with _fake_rig_client(tmp_path, ["FAKE-0", "FAKE-1"]) as client:
        system = client.get("/api/system").json()

    [missing] = system["missing_cameras"]
    assert missing["serial"] == "FAKE-1"
    assert "failed to open" in missing["reason"]
    assert "simulated: device busy" in missing["reason"]
    assert [camera["serial"] for camera in system["cameras"]] == ["FAKE-0"]


def test_system_lists_no_missing_camera_for_a_complete_rig(client):
    assert client.get("/api/system").json()["missing_cameras"] == []


def test_transform_endpoint_locked_while_recording(client):
    client.post("/api/recording/start", json={"confirm_overwrite": False})
    wait_until(
        lambda: client.get("/api/state").json()["state"] in ("waiting", "recording"),
        timeout=10,
        interval=0.05,
    )
    blocked = client.put("/api/cameras/0/transform", json={"rotation_deg": 90})
    assert blocked.status_code == 409


# --------------------------------------------- camera parameters / config save


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
    for bad in ("a/b", "   "):
        r = client.put("/api/cameras/0/name", json={"name": bad})
        assert r.status_code == 422
        assert r.json()["detail"] == f"Invalid camera name: {bad!r}"

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

    wait_until(
        lambda: client.get("/api/state").json()["state"] == "preview",
        timeout=25,
        interval=0.2,
    )

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
        assert "Invalid camera name" in r.text

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


# --------------------------------------------- full device node map (Camera tab)


def test_camera_features_endpoint(client):
    system = client.get("/api/system").json()
    # /api/system exposes the live centering flags per camera.
    assert system["cameras"][0]["center_x"] is False
    assert system["cameras"][0]["center_y"] is False

    payload = client.get("/api/cameras/0/features").json()
    assert {"index", "serial", "center_x", "center_y", "features"} <= set(payload)
    by = {f["name"]: f for f in payload["features"]}
    assert len(by) > 10  # full node map, not the six curated params
    assert by["PixelFormat"]["managed"] is True

    assert client.get("/api/cameras/99/features").status_code == 404


def test_camera_feature_write_and_managed_reject(client):
    r = client.put("/api/cameras/0/features", json={"name": "ExposureTime", "value": 2500.0})
    assert r.status_code == 200, r.text
    by = {f["name"]: f for f in r.json()["updated"][0]["features"]}
    assert abs(by["ExposureTime"]["value"] - 2500.0) < 2.0

    # Managed node -> 422; unknown node -> 422; extra field -> 422.
    assert client.put(
        "/api/cameras/0/features", json={"name": "PixelFormat", "value": "Mono12"}
    ).status_code == 422
    assert client.put(
        "/api/cameras/0/features", json={"name": "Bogus", "value": 1}
    ).status_code == 422
    assert client.put(
        "/api/cameras/0/features", json={"name": "Gain", "value": 1, "x": 2}
    ).status_code == 422
    assert client.put(
        "/api/cameras/99/features", json={"name": "Gain", "value": 1}
    ).status_code == 404


def test_camera_feature_scope_all(client):
    r = client.put(
        "/api/cameras/0/features", json={"name": "ExposureTime", "value": 2000.0, "scope": "all"}
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["updated"]) == 2  # both emulated cameras updated


def test_camera_center_endpoint(client):
    client.put("/api/cameras/0/features", json={"name": "Width", "value": 512})
    r = client.put("/api/cameras/0/center", json={"axis": "x", "enabled": True})
    assert r.status_code == 200, r.text
    entry = r.json()["updated"][0]
    assert entry["center_x"] is True
    by = {f["name"]: f for f in entry["features"]}
    assert by["OffsetX"]["writable"] is False  # octacam owns it while centered

    # Bad axis -> 422.
    assert client.put(
        "/api/cameras/0/center", json={"axis": "z", "enabled": True}
    ).status_code == 422
    # Turning it back off frees the offset.
    off = client.put("/api/cameras/0/center", json={"axis": "x", "enabled": False})
    off_by = {f["name"]: f for f in off.json()["updated"][0]["features"]}
    assert off_by["OffsetX"]["writable"] is True


def test_camera_command_endpoint(client):
    features = client.get("/api/cameras/0/features").json()["features"]
    commands = [f["name"] for f in features if f["type"] == "command"]
    assert commands, "emulator exposes command nodes"
    r = client.post("/api/cameras/0/commands", json={"name": commands[0]})
    assert r.status_code == 200, r.text
    # A non-existent command is a clean 422, not a 500.
    assert client.post(
        "/api/cameras/0/commands", json={"name": "NotACommand"}
    ).status_code == 422


def test_camera_feature_reset_prefers_config(client, tmp_path):
    baseline = None
    for f in client.get("/api/cameras/0/features").json()["features"]:
        if f["name"] == "ExposureTime":
            baseline = f["value"]
    assert baseline is not None
    # Save a value other than the first-seen one, so the reset can only find it
    # in the camera's parameter file.
    saved = baseline + 500.0
    client.put("/api/cameras/0/features", json={"name": "ExposureTime", "value": saved})
    client.post("/api/config/save", json={"target": "active", "save_display": False})
    client.put("/api/cameras/0/features", json={"name": "ExposureTime", "value": baseline + 1500.0})
    r = client.post("/api/cameras/0/features/reset", json={"name": "ExposureTime"})
    assert r.status_code == 200, r.text
    restored = {f["name"]: f for f in r.json()["updated"][0]["features"]}["ExposureTime"]["value"]
    assert abs(restored - saved) < 2.0


def _exposure(client) -> float:
    """Camera 0's ExposureTime as the Camera tab reads it (which caches it as
    the first-seen value)."""
    return _exposure_in(client.get("/api/cameras/0/features").json()["features"])


def _exposure_in(features: list[dict]) -> float:
    return {f["name"]: f["value"] for f in features}["ExposureTime"]


def test_camera_feature_reset_without_a_saved_file_restores_the_first_seen_value(client):
    baseline = _exposure(client)
    client.put("/api/cameras/0/features", json={"name": "ExposureTime", "value": baseline + 1500.0})
    r = client.post("/api/cameras/0/features/reset", json={"name": "ExposureTime"})
    assert r.status_code == 200, r.text
    assert abs(_exposure_in(r.json()["updated"][0]["features"]) - baseline) < 2.0


def test_camera_feature_reset_needs_a_config_dir(tmp_path):
    system = CameraSystem(EMULATED_SERIALS, backend="basler")
    settings = RecordingSettings(fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec"))
    controller = RecordingController(system, settings)
    try:
        with TestClient(create_app(controller, OctacamConfig())) as client:
            r = client.post("/api/cameras/0/features/reset", json={"name": "ExposureTime"})
            assert r.status_code == 400
            assert r.json()["detail"] == "No config directory is set for this session"
    finally:
        controller.close()


def test_camera_feature_reset_reads_the_file_the_camera_loads(tmp_path):
    # A mixed rig has two suffixes in play: a stale FAKE-0.pfs must not shadow
    # the FAKE-0.fake that CameraSystem.load_config gives the fake camera.
    system = CameraSystem(["FAKE-0"], backend="fake")
    system.cameras += CameraSystem(EMULATED_SERIALS[:1], backend="basler").cameras
    assert system.extensions == ("fake", "pfs")
    settings = RecordingSettings(fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec"))
    controller = RecordingController(system, settings, config_dir=tmp_path)
    app = create_app(controller, OctacamConfig(), config_dir=str(tmp_path))
    try:
        with TestClient(app) as client:
            baseline = _exposure(client)
            saved = baseline + 500.0
            client.put("/api/cameras/0/features", json={"name": "ExposureTime", "value": saved})
            r = client.post("/api/config/save", json={"target": "active", "save_display": False})
            assert r.json()["cameras_written"] == sorted(["FAKE-0", EMULATED_SERIALS[0]])
            (tmp_path / "FAKE-0.pfs").write_text(f"ExposureTime\t{baseline + 3000.0}\n")
            client.put(
                "/api/cameras/0/features", json={"name": "ExposureTime", "value": baseline + 1500.0}
            )
            r = client.post("/api/cameras/0/features/reset", json={"name": "ExposureTime"})
            assert r.status_code == 200, r.text
            assert abs(_exposure_in(r.json()["updated"][0]["features"]) - saved) < 2.0
    finally:
        controller.close()


def test_camera_features_locked_while_recording(client):
    client.post("/api/config/save", json={"target": "active", "save_display": False})
    started = client.post("/api/recording/start", json={"confirm_overwrite": True})
    assert started.status_code == 202, started.text
    try:
        for call in (
            client.put("/api/cameras/0/features", json={"name": "ExposureTime", "value": 3000.0}),
            client.put("/api/cameras/0/center", json={"axis": "x", "enabled": True}),
            client.post("/api/cameras/0/features/reset", json={"name": "ExposureTime"}),
        ):
            assert call.status_code == 409, call.text
    finally:
        client.controller.stop_recording(abort=True)


def _save_client(tmp_path, config_dir):
    from octacam.config import parse_config

    system = CameraSystem(EMULATED_SERIALS, backend="basler")
    system.load_config(config_dir)
    config = parse_config(config_dir / "octacam_config.toml")
    settings = RecordingSettings(
        fps=50.0, duration_s=1.0, save_dir=str(tmp_path / "rec" / "001")
    )
    controller = RecordingController(system, settings)
    controller.start_preview()
    app = create_app(controller, config, config_dir=str(config_dir))
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


def test_config_save_persists_center_flags(tmp_path):
    active = tmp_path / "rigs" / "active"
    active.mkdir(parents=True)
    (active / "octacam_config.toml").write_text("[gui]\n")
    controller, app = _save_client(tmp_path, active)
    cams = [
        {"serial": EMULATED_SERIALS[0], "center_x": True, "center_y": True},
        {"serial": EMULATED_SERIALS[1], "center_x": False, "center_y": False},
    ]
    try:
        with TestClient(app) as client:
            r = client.post(
                "/api/config/save",
                json={"target": "active", "save_sensor": False, "cameras": cams},
            )
            assert r.status_code == 200, r.text
            toml = (active / "octacam_config.toml").read_text()
            assert "center_x = true" in toml
            # The saved config is adopted live, re-applying centering.
            sysinfo = client.get("/api/system").json()
            assert sysinfo["cameras"][0]["center_x"] is True
            assert sysinfo["cameras"][1]["center_x"] is False
    finally:
        controller.close()


def test_config_save_adopts_the_written_config_even_if_applying_it_fails(
    tmp_path, monkeypatch
):
    # The TOML is on disk once written: a failing apply must not read as a
    # refused save, and the next save must patch what was written.
    active = _config_dir(tmp_path / "rigs" / "active")
    controller, app = _save_client(tmp_path, active)

    def busy(cameras):
        raise RuntimeError("camera busy")

    monkeypatch.setattr(controller.camera_system, "apply_display_config", busy)
    state = app.state.app_state
    try:
        with TestClient(app) as client:
            with pytest.raises(RuntimeError, match="camera busy"):  # an unmapped 500
                client.post(
                    "/api/config/save",
                    json={
                        "target": "active",
                        "save_sensor": False,
                        "cameras": [{"serial": EMULATED_SERIALS[0], "name": "left"}],
                    },
                )
        assert 'name = "left"' in (active / "octacam_config.toml").read_text()
        assert state.raw_config["cameras"][0]["name"] == "left"
        assert [c.name for c in state.config.cameras] == ["left"]
    finally:
        controller.close()


def test_config_saved_as_new_is_written_but_never_adopted(tmp_path):
    active = _config_dir(tmp_path / "rigs" / "active")
    controller, app = _save_client(tmp_path, active)
    state = app.state.app_state
    raw, config = state.raw_config, state.config
    cams = [{"serial": s, "rotation_deg": 90.0} for s in EMULATED_SERIALS]
    try:
        with TestClient(app) as client:
            r = client.post(
                "/api/config/save", json={"target": "new", "name": "other", "cameras": cams}
            )
            assert r.status_code == 200, r.text
            other = tmp_path / "rigs" / "other"
            assert r.json()["config_dir"] == str(other)
            assert "rotation_deg = 90.0" in (other / "octacam_config.toml").read_text()
            # The session keeps running its own config.
            assert state.raw_config is raw and state.config is config
            for camera in client.get("/api/system").json()["cameras"]:
                assert camera["transform"]["rotation_deg"] == 0.0
            assert all(c.display_transform.is_identity for c in controller.camera_system)
    finally:
        controller.close()


def test_consecutive_active_saves_patch_the_last_one(tmp_path):
    # The second save sends one camera; the other keeps what the first wrote.
    active = _config_dir(tmp_path / "rigs" / "active")
    controller, app = _save_client(tmp_path, active)
    first = [{"serial": s, "rotation_deg": 90.0} for s in EMULATED_SERIALS]
    second = [{"serial": EMULATED_SERIALS[0], "rotation_deg": 180.0}]
    try:
        with TestClient(app) as client:
            for cams in (first, second):
                r = client.post(
                    "/api/config/save",
                    json={"target": "active", "save_sensor": False, "cameras": cams},
                )
                assert r.status_code == 200, r.text
            transforms = [
                c["transform"]["rotation_deg"]
                for c in client.get("/api/system").json()["cameras"]
            ]
            assert transforms == [180.0, 90.0]
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


def _config_dir(path, text="[gui]\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "octacam_config.toml").write_text(text)
    return path


def test_a_config_saved_as_new_from_a_relaunched_recording_lands_beside_it(tmp_path):
    # `octacam gui <recording>` runs from the recording's octacam_recording
    # subfolder, whose siblings are the recording's videos, so a config saved
    # as new lands beside the recording folder, not inside it.

    fly = tmp_path / "data" / "Fly1"
    info = _config_dir(fly / "001" / RECORDING_INFO_DIRNAME)
    (info / "recording_summary.json").write_text("{}")
    (info / "fictrac_camera_config.pfs").write_text("aux\n")
    (fly / "001" / "camera_0.mp4").write_bytes(b"v")
    controller, app = _save_client(tmp_path, info)
    cams = [{"serial": s} for s in EMULATED_SERIALS]
    try:
        with TestClient(app) as client:
            r = client.post(
                "/api/config/save",
                json={"target": "new", "name": "variant", "cameras": cams},
            )
            assert r.status_code == 200, r.text
            variant = fly / "variant"
            assert r.json()["config_dir"] == str(variant)
            assert (variant / "octacam_config.toml").exists()
            assert (variant / "fictrac_camera_config.pfs").exists()  # aux copied
            assert not (fly / "001" / "variant").exists()

            # Saving to the active config still writes the recording's own.
            r = client.post(
                "/api/config/save",
                json={"target": "active", "save_sensor": False, "cameras": cams},
            )
            assert r.status_code == 200, r.text
            assert r.json()["config_dir"] == str(info)
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

    from octacam.web.hub import Client

    class _ClosedWS:
        client_state = WebSocketState.CONNECTED  # peer still looks connected

        async def send_text(self, _message):
            raise RuntimeError(
                "Unexpected ASGI message 'websocket.send', after sending "
                "'websocket.close' or response already completed."
            )

        async def send_bytes(self, _message):  # pragma: no cover
            raise RuntimeError("socket already closed")

    client = Client(_ClosedWS())
    client.queue("state", "{}")

    # Must return cleanly (and promptly) instead of propagating RuntimeError.
    asyncio.run(asyncio.wait_for(client.sender(), timeout=1.0))


def test_sender_skips_send_once_peer_disconnected():
    """If the peer is already gone, the sender shouldn't even attempt a send."""
    from starlette.websockets import WebSocketState

    from octacam.web.hub import Client

    ws = Mock()
    ws.client_state = WebSocketState.DISCONNECTED
    client = Client(ws)
    client.queue("state", "{}")
    client.queue_frame(0, b"jpegbytes")

    asyncio.run(asyncio.wait_for(client.sender(), timeout=1.0))
    ws.send_text.assert_not_called()
    ws.send_bytes.assert_not_called()


def test_client_is_ready_for_tracks_unsent_frames():
    """is_ready_for gates preview encoding on the client having drained the
    previous frame, so a backed-up client stops the rig re-encoding previews
    it can't keep up with."""
    from starlette.websockets import WebSocketState

    from octacam.web.hub import Client

    ws = Mock()
    ws.client_state = WebSocketState.CONNECTED
    client = Client(ws)

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


def test_preview_factor_policy():
    """Adaptive decimation: a client that sends nothing is unchanged; a normal
    tile may only go coarser than the 640 baseline; a focused tile may go
    finer, bounded to a mid resolution while recording."""
    from octacam.web.preview import DEFAULT_VIEW, ViewSpec, _preview_factor

    L = 2048  # sensor long edge; baseline ceil(2048/640) = 4
    # Default/legacy spec == today's baseline (backward compatible).
    assert _preview_factor(L, L, DEFAULT_VIEW, False) == 4
    # A small unfocused tile sends less data (coarser)...
    assert _preview_factor(L, L, ViewSpec(need=300), False) == 7
    # ...but a large unfocused tile is still capped at the baseline, so a HiDPI
    # client can't silently upgrade every camera above today's cost.
    assert _preview_factor(L, L, ViewSpec(need=1500), False) == 4
    # A focused (maximized) tile may exceed the baseline, up to the sensor.
    assert _preview_factor(L, L, ViewSpec(need=1500, full=True), False) == 1
    assert _preview_factor(L, L, ViewSpec(need=100000, full=True), False) == 1
    # While recording, a focused tile is bounded so the preview encode can't
    # starve the writer (ceil(2048/1280) = 2 -> 1024 px <= 1280 cap)...
    assert _preview_factor(L, L, ViewSpec(need=100000, full=True), True) == 2
    # ...and the cap is a true ceiling for mid-band regions too (round() would
    # leak factor 1 = full res).
    assert _preview_factor(1600, 1600, ViewSpec(need=100000, full=True), True) == 2
    # A cropped focused tile picks the factor from the CROP's long edge (2nd
    # arg), not the sensor's, so a small crop is delivered near 1:1 (full detail)
    # while a bigger crop still decimates to about the requested size.
    assert _preview_factor(L, 400, ViewSpec(need=400, full=True), False) == 1
    assert _preview_factor(L, 800, ViewSpec(need=400, full=True), False) == 2


def test_parse_views_is_tolerant():
    """A view message's specs are read per camera, and garbage never raises."""
    from octacam.web.preview import ViewSpec, parse_views

    views = parse_views(
        {
            "type": "view",
            "cameras": {
                "0": {"want": True, "need": 512, "full": True,
                      "crop": {"x": 10, "y": 20, "w": 300, "h": 400}},
                "1": {"want": False},
                "2": {"need": -5},  # invalid need -> treated as baseline (None)
                "4": {"crop": {"x": 0, "y": 0, "w": 0, "h": 5}},  # bad crop -> None
                "bad": {"need": 100},  # non-int key -> skipped
                "3": "notadict",  # non-dict entry -> skipped
            },
        }
    )
    assert views[0] == ViewSpec(want=True, need=512, full=True, crop=(10, 20, 300, 400))
    assert views[1].want is False
    assert views[2].need is None
    assert views[4].crop is None  # malformed crop dropped
    assert set(views) == {0, 1, 2, 4}
    # Malformed top-level payloads are ignored, not raised.
    assert parse_views({"cameras": "nope"}) == {}
    assert parse_views({}) == {}


def test_client_keeps_the_newest_message_per_type_and_key():
    """A client keeps one pending message per (type, key) and every event."""
    from octacam.web.hub import Client

    client = Client(Mock())
    for kind, text, key in [
        ("camera_name", "a", 0),
        ("camera_name", "b", 1),
        ("camera_name", "c", 0),
        ("state", "1", None),
        ("state", "2", None),
        ("event", "x", None),
        ("event", "y", None),
    ]:
        client.queue(kind, text, key)
    assert list(client.texts.values()) == ["c", "b", "2"]
    assert list(client.events) == ["x", "y"]


def test_hub_publish_forwards_the_key_from_another_thread():
    from octacam.web.hub import Client, Hub

    hub = Hub()
    client = Client(Mock())
    hub.clients.add(client)

    def publish():
        for index in (0, 1):
            hub.publish("camera_features_dirty", {"index": index}, key=index)

    async def main():
        hub.loop = asyncio.get_running_loop()
        await hub.loop.run_in_executor(None, publish)
        await asyncio.sleep(0)  # run the queued callbacks

    asyncio.run(main())
    assert [json.loads(text)["index"] for text in client.texts.values()] == [0, 1]


def test_cap_variants_bounds_encode_count():
    """Beyond the per-camera cap, the priciest extra variants are demoted to the
    shared full-frame baseline (never cross-merged), so encode count stays
    bounded and no client is dropped."""
    from octacam.web.preview import (
        MAX_PREVIEW_VARIANTS_PER_CAMERA,
        PREVIEW_MAX_DIM,
        _cap_variants,
    )

    W = H = 2048
    baseline_f = math.ceil(W / PREVIEW_MAX_DIM)  # 4
    baseline_key = ((0, 0, W, H), baseline_f)
    # Six distinct (crop, factor) variants — more than the cap of 4.
    groups = {
        baseline_key: ["a"],
        ((0, 0, 400, 400), 1): ["b"],
        ((0, 0, 2048, 2048), 1): ["c"],  # full res — priciest, must demote
        ((0, 0, 1600, 1600), 1): ["d"],
        ((100, 100, 1200, 1200), 1): ["e"],
        ((0, 0, 800, 800), 1): ["f"],
    }
    total = sum(len(v) for v in groups.values())
    _cap_variants(groups, W, H, W)
    assert len(groups) <= MAX_PREVIEW_VARIANTS_PER_CAMERA
    assert sum(len(v) for v in groups.values()) == total  # nobody dropped
    assert baseline_key in groups  # demotion target survives
    # The priciest full-res crop was demoted onto the baseline (client folded in).
    assert ((0, 0, 2048, 2048), 1) not in groups
    assert "c" in groups[baseline_key]


def test_view_message_selects_resolution_and_pauses(client):
    """A `view` message maximizes one camera to full resolution and pauses the
    other: the server sends full-res frames for the focused camera and stops
    sending the paused one."""
    import cv2

    cams = {c["index"]: c for c in client.get("/api/system").json()["cameras"]}
    long0 = max(cams[0]["width"], cams[0]["height"])
    assert long0 > 640, "emulator sensor should exceed the preview cap"

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "view",
                    "cameras": {
                        "0": {"want": True, "need": 100000, "full": True},
                        "1": {"want": False},
                    },
                }
            )
        )
        full_seen = False
        cam1_after_full = 0
        for _ in range(150):
            message = ws.receive()
            buf = message.get("bytes")
            if not buf:
                continue
            index = buf[2]  # header byte 2 is the camera index
            jpeg = np.frombuffer(buf[FRAME_HEADER.size :], np.uint8)
            image = cv2.imdecode(jpeg, cv2.IMREAD_GRAYSCALE)
            if index == 0 and max(image.shape) == long0:
                full_seen = True  # only reachable with the view spec applied
            elif full_seen and index == 1:
                # A stale pre-view frame arrives strictly before the first
                # full-res frame (later tick), so any cam-1 frame after that
                # means the pause was ignored.
                cam1_after_full += 1
            if full_seen and _ > 80:
                break

    assert full_seen, "camera 0 never reached full resolution after the view"
    assert cam1_after_full == 0, "paused camera 1 kept sending frames"


def test_view_message_server_side_crop(client):
    """A `view` message with a crop makes the server send only that sub-rectangle
    (reported in the frame header) rather than the whole sensor."""
    import cv2

    cams = {c["index"]: c for c in client.get("/api/system").json()["cameras"]}
    w, h = cams[0]["width"], cams[0]["height"]
    # A centered quarter-area crop, requested at ~1:1 (need == crop long edge).
    cx, cy, cw, ch = w // 4, h // 4, w // 2, h // 2

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "view",
                    "cameras": {
                        "0": {
                            "want": True,
                            "full": True,
                            "need": max(cw, ch),
                            "crop": {"x": cx, "y": cy, "w": cw, "h": ch},
                        },
                    },
                }
            )
        )
        seen = None
        for _ in range(150):
            message = ws.receive()
            buf = message.get("bytes")
            if not buf or buf[2] != 0:  # header byte 2 is the camera index
                continue
            (_v, _k, _i, _f, _n, _t, _fps, _d, hx, hy, hcw, hch, hsw, hsh) = (
                FRAME_HEADER.unpack(buf[: FRAME_HEADER.size])
            )
            if (hx, hy, hcw, hch) == (cx, cy, cw, ch):
                jpeg = np.frombuffer(buf[FRAME_HEADER.size :], np.uint8)
                seen = (cv2.imdecode(jpeg, cv2.IMREAD_GRAYSCALE), hsw, hsh)
                break

        assert seen is not None, "server never sent the requested crop"
        image, hsw, hsh = seen
        assert (hsw, hsh) == (w, h)  # header reports the full sensor size
        # need == crop long edge -> factor 1 -> the crop is sent at 1:1.
        assert image.shape == (ch, cw)


# --------------------------------------------------------------------------- #
# /api/system exposes the update notice for the GUI banner. The app runs the
# check in the background; conftest's OCTACAM_NO_UPDATE_CHECK keeps it off the
# network. A notice is injected via the app.state test seam to check surfacing.


def test_system_update_check_makes_no_network_call(client):
    state = client.app.state.app_state
    assert wait_until(lambda: state.update_notice is not None)
    data = client.get("/api/system").json()
    assert data["update"]["latest"] is None and data["update"]["available"] is False
    assert data["update"]["note"] == "update check disabled"


def test_system_surfaces_injected_update_notice(client):
    from octacam.updates import UpdateNotice

    state = client.app.state.app_state
    # Inject after the background check has stored its own notice.
    wait_until(lambda: state.update_notice is not None)
    state.update_notice = UpdateNotice(
        current="0.3.0",
        latest="0.9.0",
        update_available=True,
        install_method="uv-tool",
        command="uv tool upgrade octacam",
        note="",
    )
    data = client.get("/api/system").json()
    assert data["update"]["available"] is True
    assert data["update"]["latest"] == "0.9.0"
    assert data["update"]["command"] == "uv tool upgrade octacam"


def test_encode_camera_is_pure_and_shares_one_header_per_camera():
    """_encode_camera takes everything it needs by value.

    It runs on an executor thread (one call per camera per tick), so it must
    touch no shared state and no camera object — that is what lets the cameras
    encode concurrently while cv2 has the GIL released.
    """
    from octacam.web.preview import FRAME_VERSION, EncodeJob, _encode_camera

    frame = (np.random.rand(64, 64) * 255).astype(np.uint8)
    groups = {
        ((0, 0, 64, 64), 1): ["client-a"],
        ((0, 0, 32, 32), 1): ["client-b"],  # a distinct crop -> its own encode
    }
    job = EncodeJob(
        camera=1, frame=frame, groups=groups, number=7, timestamp_ns=123456789,
        fps=42.5, dropped=3, recording=True,
    )
    messages = _encode_camera(job)

    assert len(messages) == 2  # one encode per distinct variant
    for message, group in messages:
        fields = FRAME_HEADER.unpack(message[: FRAME_HEADER.size])
        version, kind, cam, flags, count, ts, fps, dropped = fields[:8]
        assert (version, kind, cam, flags) == (FRAME_VERSION, 1, 1, 1)
        # The per-camera telemetry is shared verbatim across that camera's
        # variants — computed once on the event loop, not re-read per variant.
        assert (count, ts, dropped) == (7, 123456789, 3)
        assert abs(fps - 42.5) < 1e-3
        assert group and group[0] in ("client-a", "client-b")
    # The two variants carry different crop rects (never cross-merged).
    rects = {FRAME_HEADER.unpack(m[: FRAME_HEADER.size])[8:12] for m, _ in messages}
    assert rects == {(0, 0, 64, 64), (0, 0, 32, 32)}


def test_preview_tick_encodes_cameras_concurrently(client, monkeypatch):
    """One executor task per camera, not one task encoding them in sequence.

    cv2.imencode releases the GIL, so per-camera dispatch makes a tick cost the
    slowest single camera instead of the sum. Re-serializing this (a single
    run_in_executor over all cameras) silently reintroduces a cost that grows
    linearly with the rig size — at 2048x2048 it overruns the 33 ms tick outright.
    The encodes here are padded with a sleep so the overlap is observable:
    serialized, peak concurrency can never exceed 1.
    """
    import threading

    from octacam.web import preview

    live = 0
    peak = 0
    seen_cameras = set()
    lock = threading.Lock()
    real = preview._encode_camera

    def instrumented(job):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
            seen_cameras.add(job.camera)
        try:
            time.sleep(0.03)  # wide enough for a concurrent partner to overlap
            return real(job)
        finally:
            with lock:
                live -= 1

    monkeypatch.setattr(preview, "_encode_camera", instrumented)
    with client.websocket_connect("/api/ws") as ws:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (peak < 2 or len(seen_cameras) < 2):
            ws.receive()

    assert seen_cameras == {0, 1}, seen_cameras
    assert peak == 2, f"cameras were encoded serially (peak concurrency {peak})"

