"""Camera sensor-parameter read/set/save tests (pure-unit + emulator)."""

import os

os.environ.setdefault("PYLON_CAMEMU", "2")

import pytest

from octacam.camera import CameraSystem, _normalize_pfs_triggers

EMULATED_SERIALS = ["0815-0000", "0815-0001"]


# ------------------------------------------------------------------- units


def test_normalize_pfs_triggers_only_touches_frame_start():
    src = (
        "# comment\n"
        "TriggerMode\t{TriggerSelector=FrameBurstStart}\tOn\n"
        "TriggerMode\t{TriggerSelector=FrameStart}\tOn\n"
        "TriggerSource\t{TriggerSelector=FrameBurstStart}\tLine1\n"
        "TriggerSource\t{TriggerSelector=FrameStart}\tSoftware\n"
        "ExposureTime\t600.0\n"
    )
    out = _normalize_pfs_triggers(src, "Line1").splitlines()
    # FrameStart reset to the shipped convention...
    assert "TriggerMode\t{TriggerSelector=FrameStart}\tOff" in out
    assert "TriggerSource\t{TriggerSelector=FrameStart}\tLine1" in out
    # ...FrameBurstStart and unrelated lines untouched (no FrameStart substring trap).
    assert "TriggerMode\t{TriggerSelector=FrameBurstStart}\tOn" in out
    assert "TriggerSource\t{TriggerSelector=FrameBurstStart}\tLine1" in out
    assert "ExposureTime\t600.0" in out


def test_normalize_pfs_triggers_keeps_source_when_unknown():
    src = "TriggerSource\t{TriggerSelector=FrameStart}\tSoftware\n"
    # original_source unknown -> leave the source line alone (only mode resets)
    assert "Software" in _normalize_pfs_triggers(src, None)


# ------------------------------------------------- emulator integration


@pytest.fixture
def previewing_system(tmp_path):
    system = CameraSystem(EMULATED_SERIALS)
    assert len(system) == 2, "PYLON_CAMEMU=2 expected"
    system.load_config(tmp_path)  # no .pfs: emulator defaults
    system.start_preview()
    yield system
    system.close()


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
    assert (
        width["value"] > 0 and width["writable"] is True
    )  # geometry editable when open
    exposure = params["exposure"]
    assert exposure["min"] is not None and exposure["max"] is not None
    # float/enum nodes degrade gracefully rather than raising
    assert "inc" in params["gain"]


def test_read_param_rejects_unknown(previewing_system):
    with pytest.raises(ValueError):
        previewing_system.camera_at(0).read_param("bogus")


def test_set_live_param_echoes_and_snaps(previewing_system):
    cam = previewing_system.camera_at(0)
    desc = cam.set_live_param("exposure", 1234.0)
    assert desc["name"] == "exposure"
    assert cam.read_param("exposure")["value"] == desc["value"]
    # out-of-range is clamped to the node max, not crashed
    high = cam.set_live_param("offset_x", 10**9)["value"]
    assert high <= cam.read_param("offset_x")["max"]


def test_set_live_param_rejects_geometry_name(previewing_system):
    with pytest.raises(ValueError):
        previewing_system.camera_at(0).set_live_param("width", 640)


def test_set_geometry_resizes_and_keeps_previewing(previewing_system):
    cam = previewing_system.camera_at(0)
    assert cam._camera.IsGrabbing()
    result = cam.set_geometry(width=640, height=480)
    assert (result["width"], result["height"]) == (640, 480)
    assert (cam.width, cam.height) == (640, 480)
    # display placeholder reshaped to the new ROI; preview resumed
    frame = cam.frame_for_display.pop()
    assert frame is not None and frame.shape == (480, 640)
    assert cam._camera.IsGrabbing()
    # the other camera is unaffected
    assert previewing_system.camera_at(1)._camera.IsGrabbing()


def test_save_params_round_trips_and_normalizes_trigger(previewing_system):
    cam = previewing_system.camera_at(0)
    cam.set_live_param("exposure", 2222.0)
    pfs = cam.save_params()
    assert "TriggerMode\t{TriggerSelector=FrameStart}\tOff" in pfs
    # reloads cleanly through the existing load path
    cam.load_params(pfs)
    assert abs(cam.read_param("exposure")["value"] - 2222.0) < 1.0


def test_reset_params_restores_pfs_and_keeps_previewing(previewing_system):
    cam = previewing_system.camera_at(0)
    baseline = cam.save_params()  # snapshot the config's saved state
    original = cam.read_param("exposure")["value"]
    cam.set_live_param("exposure", original + 1000.0)
    assert abs(cam.read_param("exposure")["value"] - original) > 1.0

    result = cam.reset_params(baseline)
    assert cam._camera.IsGrabbing()  # preview restored after the grab cycle
    assert abs(result["params"]["exposure"]["value"] - original) < 1.0
    assert abs(cam.read_param("exposure")["value"] - original) < 1.0


def test_reset_params_invalid_pfs_keeps_previewing(previewing_system):
    cam = previewing_system.camera_at(0)
    assert cam._camera.IsGrabbing()
    # A .pfs the device rejects surfaces as ValueError but must never strand
    # the live preview (mirrors set_geometry's restore-on-error guarantee).
    with pytest.raises(ValueError):
        cam.reset_params("this is not a valid feature stream\n")
    assert cam._camera.IsGrabbing()
    assert cam.frame_for_display.pop() is not None  # still serving frames


def test_reset_params_empty_is_noop(previewing_system):
    cam = previewing_system.camera_at(0)
    cam.set_live_param("exposure", 1777.0)
    # No .pfs for this camera: reset leaves the live value (and preview) intact.
    result = cam.reset_params("")
    assert cam._camera.IsGrabbing()
    assert abs(result["params"]["exposure"]["value"] - 1777.0) < 1.0


def test_save_all_params_covers_every_camera(previewing_system):
    out = previewing_system.save_all_params()
    assert set(out) == set(EMULATED_SERIALS)
    assert all(text.strip() for text in out.values())


def test_camera_at_bounds(previewing_system):
    with pytest.raises(IndexError):
        previewing_system.camera_at(99)
