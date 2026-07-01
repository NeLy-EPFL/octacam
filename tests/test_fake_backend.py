"""Fake-backend tests: the SDK-neutral abstraction without any camera SDK.

These exercise what the Basler emulator (PYLON_CAMEMU) cannot: the
backend-selection path, the persistence generalization (a non-".pfs" extension
round-tripping through load_config), and deterministic node behaviour — all in
pure Python with no hardware.
"""

import json
import os

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import pytest

from octacam.cameras import CameraSystem

FAKE_SERIALS = ["FAKE-0", "FAKE-1"]


@pytest.fixture
def previewing_system(tmp_path):
    system = CameraSystem(FAKE_SERIALS, backend="fake")
    assert len(system) == 2
    assert system.extensions == ("fake",)
    system.load_config(tmp_path)  # no param files: defaults
    system.start_preview()
    yield system
    system.close()


def test_backend_selected_and_serials(previewing_system):
    assert [c.serial_number for c in previewing_system] == FAKE_SERIALS
    assert previewing_system.backend == "fake"


def test_read_params_shape(previewing_system):
    cam = previewing_system.camera_at(0)
    params = cam.read_params()
    assert set(params) >= {
        "width",
        "height",
        "exposure",
        "gain",
        "offset_x",
        "offset_y",
    }
    width = params["width"]
    assert width["value"] > 0 and width["writable"] is True
    exposure = params["exposure"]
    assert exposure["min"] is not None and exposure["max"] is not None
    assert "inc" in params["gain"]


def test_read_param_rejects_unknown(previewing_system):
    with pytest.raises(ValueError):
        previewing_system.camera_at(0).read_param("bogus")


def test_set_live_param_echoes_and_snaps(previewing_system):
    cam = previewing_system.camera_at(0)
    desc = cam.set_live_param("exposure", 1234.0)
    assert desc["name"] == "exposure"
    assert cam.read_param("exposure")["value"] == desc["value"]
    # out-of-range clamps to the node max rather than crashing
    high = cam.set_live_param("offset_x", 10**9)["value"]
    assert high <= cam.read_param("offset_x")["max"]


def test_set_live_param_rejects_geometry_name(previewing_system):
    with pytest.raises(ValueError):
        previewing_system.camera_at(0).set_live_param("width", 640)


def test_set_geometry_resizes_and_keeps_previewing(previewing_system):
    cam = previewing_system.camera_at(0)
    assert cam._backend.is_grabbing()
    result = cam.set_geometry(width=640, height=480)
    assert (result["width"], result["height"]) == (640, 480)
    assert (cam.width, cam.height) == (640, 480)
    frame = cam.frame_for_display.pop()
    assert frame is not None and frame.shape == (480, 640)
    assert cam._backend.is_grabbing()
    assert previewing_system.camera_at(1)._backend.is_grabbing()


def test_save_params_round_trips(previewing_system):
    cam = previewing_system.camera_at(0)
    cam.set_live_param("exposure", 2222.0)
    text = cam.save_params()
    data = json.loads(text)  # the fake persists JSON
    assert data["trigger_mode"] == "Off"  # normalized, like the Basler .pfs
    assert data["params"]["exposure"] == 2222.0
    cam.load_params(text)
    assert abs(cam.read_param("exposure")["value"] - 2222.0) < 1.0


def test_reset_params_restores_and_keeps_previewing(previewing_system):
    cam = previewing_system.camera_at(0)
    baseline = cam.save_params()
    original = cam.read_param("exposure")["value"]
    cam.set_live_param("exposure", original + 1000.0)
    assert abs(cam.read_param("exposure")["value"] - original) > 1.0

    result = cam.reset_params(baseline)
    assert cam._backend.is_grabbing()
    assert abs(result["params"]["exposure"]["value"] - original) < 1.0


def test_reset_params_invalid_keeps_previewing(previewing_system):
    cam = previewing_system.camera_at(0)
    assert cam._backend.is_grabbing()
    with pytest.raises(ValueError):
        cam.reset_params("this is not valid json {")
    assert cam._backend.is_grabbing()
    assert cam.frame_for_display.pop() is not None


def test_save_all_params_covers_every_camera(previewing_system):
    out = previewing_system.save_all_params()
    assert set(out) == set(FAKE_SERIALS)
    assert all(text.strip() for text in out.values())


def test_load_config_reads_backend_extension(tmp_path):
    # The persistence generalization: a non-".pfs" per-camera file, named by
    # the backend's extension, round-trips through load_config.
    (tmp_path / "FAKE-0.fake").write_text(
        json.dumps({"params": {"exposure": 9999.0, "width": 800, "height": 600}})
    )
    system = CameraSystem(FAKE_SERIALS, backend="fake")
    try:
        system.load_config(tmp_path)
        cam = system.camera_at(0)
        assert (cam.width, cam.height) == (800, 600)
        assert abs(cam.read_param("exposure")["value"] - 9999.0) < 1.0
        # the camera without a file keeps its defaults
        assert system.camera_at(1).read_param("exposure")["value"] != 9999.0
    finally:
        system.close()
