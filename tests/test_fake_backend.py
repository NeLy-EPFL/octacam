"""Fake-backend tests: the SDK-neutral abstraction without any camera SDK.

These exercise what the Basler emulator (PYLON_CAMEMU) cannot: the
backend-selection path, the persistence generalization (a non-".pfs" extension
round-tripping through load_config), and deterministic node behaviour — all in
pure Python with no hardware.
"""

import os

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import pytest

from octacam.cameras import CameraSystem
from octacam.cameras._genicam_config import parse_config

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
    # The fake persists the native GenApi persistence TSV (shared with FLIR).
    values = dict(parse_config(text))
    assert values["ExposureTime"] == "2222"  # _fmt_float drops the trailing .0
    assert "TriggerSource" in values  # the fake stores/round-trips the source
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
    # Free text with no `name<TAB>value` line is a malformed config, rejected as
    # a ValueError (the applier raises rather than silently applying nothing).
    with pytest.raises(ValueError):
        cam.reset_params("this is not a persistence file")
    assert cam._backend.is_grabbing()
    assert cam.frame_for_display.pop() is not None


def test_save_all_params_covers_every_camera(previewing_system):
    out = previewing_system.save_all_params()
    assert set(out) == set(FAKE_SERIALS)
    assert all(text.strip() for text in out.values())


def test_load_config_reads_backend_extension(tmp_path):
    # The persistence generalization: a non-".pfs" per-camera file, named by the
    # backend's extension, round-trips through load_config. The fake persists the
    # native GenApi persistence TSV, so the file uses SFNC feature names.
    (tmp_path / "FAKE-0.fake").write_text(
        "# GenApi persistence file\n"
        "ExposureTime\t9999.0\n"
        "Width\t800\n"
        "Height\t600\n"
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


# ------------------------------------------------ full device node map (Camera tab)


def test_features_span_every_widget_kind(previewing_system):
    cam = previewing_system.camera_at(0)
    by = {f["name"]: f for f in cam.list_features()}
    kinds = {f["type"] for f in by.values()}
    assert {"int", "float", "enum", "bool", "string", "command"} <= kinds
    # Bounds/entries/units are surfaced per kind.
    assert by["Gain"]["min"] == 0.0 and by["Gain"]["unit"] == "dB"
    assert [e["value"] for e in by["ExposureAuto"]["entries"]] == ["Off", "Once", "Continuous"]
    assert by["ReverseX"]["type"] == "bool"
    assert by["DeviceModelName"]["type"] == "string" and by["DeviceModelName"]["writable"] is False


def test_expert_and_guru_visibility(previewing_system):
    cam = previewing_system.camera_at(0)
    by = {f["name"]: f for f in cam.list_features()}
    assert by["AcquisitionFrameRate"]["visibility"] == "expert"
    # Guru nodes (e.g. DeviceReset) are surfaced too; the browser's level
    # selector hides them client-side until the user opts into Guru.
    assert by["DeviceReset"]["visibility"] == "guru"


def test_write_feature_validates(previewing_system):
    cam = previewing_system.camera_at(0)
    cam.set_feature("ReverseX", True)
    assert cam.read_feature("ReverseX")["value"] is True
    cam.set_feature("ExposureAuto", "Continuous")
    assert cam.read_feature("ExposureAuto")["value"] == "Continuous"
    with pytest.raises(ValueError):  # not a valid enum entry
        cam.set_feature("ExposureAuto", "Bogus")
    cam.set_feature("DeviceUserID", "rig-cam")
    assert cam.read_feature("DeviceUserID")["value"] == "rig-cam"


def test_command_execution_counter(previewing_system):
    cam = previewing_system.camera_at(0)
    cam.execute_command("TimestampLatch")
    assert cam.backend._commands_run.get("TimestampLatch") == 1
    with pytest.raises(ValueError):
        cam.execute_command("NotACommand")


def test_reset_feature_prefers_config(previewing_system):
    cam = previewing_system.camera_at(0)
    config_text = "# GenApi persistence file\nGain\t7.0\n"
    cam.set_feature("Gain", 3.0)
    cam.reset_feature("Gain", config_text)  # config value wins over factory
    assert abs(cam.read_feature("Gain")["value"] - 7.0) < 0.2


def test_fake_offsets_are_live_writable(previewing_system):
    # The fake models a FLIR-like camera: only Width/Height are grab-locked, so a
    # ROI-offset write happens live on the running camera (no grab cycle).
    cam = previewing_system.camera_at(0)
    assert cam.backend.grab_locked_features() == frozenset({"Width", "Height"})
    cam.set_feature("OffsetX", 8)
    assert cam.read_feature("OffsetX")["value"] == 8
    assert cam.backend.is_grabbing()  # never stopped


def test_grab_locked_offset_write_cycles_the_preview(previewing_system, monkeypatch):
    # Model a Basler-style camera whose ROI offsets lock during acquisition.
    cam = previewing_system.camera_at(0)
    monkeypatch.setattr(
        cam.backend,
        "grab_locked_features",
        lambda: frozenset({"Width", "Height", "OffsetX", "OffsetY"}),
    )
    cam.set_feature("Width", 512)  # make room for a non-zero origin
    # The offset is now presented editable while previewing...
    by = {f["name"]: f for f in cam.list_features()}
    assert by["OffsetX"]["writable"] is True
    # ...and its write cycles the preview grab (stop -> write -> restart).
    starts: list[int] = []
    real_start = cam.start_preview
    monkeypatch.setattr(
        cam, "start_preview", lambda: (starts.append(1), real_start())[1]
    )
    cam.set_feature("OffsetX", 8)
    assert starts, "offset write did not cycle the preview grab"
    assert cam.backend.is_grabbing()
    assert cam.read_feature("OffsetX")["value"] == 8
