"""Web backend integration tests against the camera emulator."""

import json
import os
import struct
import time

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


def test_system_and_settings_endpoints(client):
    system = client.get("/api/system").json()
    assert len(system["cameras"]) == 2
    assert system["cameras"][0]["width"] > 0
    assert {f["codec"] for f in system["formats"]} == {
        "x264", "raw", "mjpg", "h264",
    }
    assert system["serial_available"] is False

    settings = client.get("/api/settings").json()
    assert settings["fps"] == 50.0

    response = client.put("/api/settings", json={"fps": 60.0, "crf": 18})
    assert response.status_code == 200
    assert response.json()["fps"] == 60.0

    assert client.put("/api/settings", json={"fps": -1}).status_code == 422
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 422

    validation = client.post(
        "/api/save-dir/validate", json={"path": "~/somewhere"}
    ).json()
    assert validation["resolved"].startswith("/")
    assert validation["free_bytes"] > 0

    # no serial port in tests
    command = dict.fromkeys(
        ("n_steps", "step_interval_us", "rest_duration_ms",
         "n_repeats", "init_wait_duration_s"), 1,
    )
    assert client.post("/api/serial/command", json=command).status_code == 503


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
    version, kind, camera_index, flags, number, ts, fps, dropped = (
        FRAME_HEADER.unpack(frames[0][: FRAME_HEADER.size])
    )
    assert (version, kind) == (1, 1)
    assert camera_index in (0, 1)
    jpeg = np.frombuffer(frames[0][FRAME_HEADER.size :], np.uint8)
    image = cv2.imdecode(jpeg, cv2.IMREAD_GRAYSCALE)
    assert image is not None and image.size > 0
    assert max(image.shape) <= 640  # downscaled preview


def test_recording_cycle_over_rest(client, tmp_path):
    save_dir = tmp_path / "rec" / "001"
    response = client.post(
        "/api/recording/start", json={"confirm_overwrite": False}
    )
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
    assert all(v.with_suffix(".csv").exists() for v in videos)
    # save dir auto-incremented for the next trial
    assert client.get("/api/settings").json()["save_dir"].endswith("002")

    # an existing dir requires confirmation
    save_dir.with_name("002").mkdir(parents=True, exist_ok=True)
    needs_confirm = client.post("/api/recording/start", json={})
    assert needs_confirm.status_code == 409
    assert needs_confirm.json()["status"] == "needs_confirm"
