"""Camera sensor-parameter read/set/save tests (pure-unit + emulator)."""


import pytest

from octacam.cameras import CameraSystem
from octacam.cameras.basler import _normalize_pfs_triggers

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
    system = CameraSystem(EMULATED_SERIALS, backend="basler")
    assert len(system) == 2, "PYLON_CAMEMU=2 expected"
    system.load_config(tmp_path)  # no .pfs: emulator defaults
    system.start_preview()
    yield system
    system.close()


def test_read_param_shape(previewing_system):
    cam = previewing_system.camera_at(0)
    params = {
        name: cam.read_param(name)
        for name in ("width", "height", "exposure", "gain", "offset_x", "offset_y")
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


def test_set_geometry_resizes_and_keeps_previewing(previewing_system):
    cam = previewing_system.camera_at(0)
    assert cam.backend.is_grabbing()
    cam.set_geometry(width=640, height=480)
    assert (cam.width, cam.height) == (640, 480)
    # display placeholder reshaped to the new ROI; preview resumed
    frame = cam.frame_for_display.pop()
    assert frame is not None and frame.shape == (480, 640)
    assert cam.backend.is_grabbing()
    # the other camera is unaffected
    assert previewing_system.camera_at(1).backend.is_grabbing()


def test_save_params_round_trips_and_normalizes_trigger(previewing_system):
    cam = previewing_system.camera_at(0)
    cam.set_feature("ExposureTime", 2222.0)
    pfs = cam.save_params()
    assert "TriggerMode\t{TriggerSelector=FrameStart}\tOff" in pfs
    # reloads cleanly through the existing load path
    cam.load_params(pfs)
    assert abs(cam.read_param("exposure")["value"] - 2222.0) < 1.0


def test_save_all_params_covers_every_camera(previewing_system):
    out = previewing_system.save_all_params()
    assert set(out) == set(EMULATED_SERIALS)
    assert all(text.strip() for text in out.values())


def test_camera_at_bounds(previewing_system):
    with pytest.raises(IndexError):
        previewing_system.camera_at(99)


# ------------------------------------------------ full device node map (Camera tab)


def test_list_features_grouped_and_typed(previewing_system):
    cam = previewing_system.camera_at(0)
    features = cam.list_features()
    assert len(features) > 10  # a real node map, not the six curated params
    by = {f["name"]: f for f in features}
    # Categorised, and spanning multiple widget kinds.
    assert all(f["category"] for f in features)
    kinds = {f["type"] for f in features}
    assert {"int", "float", "enum"} <= kinds
    # Width is editable while open; enums carry selectable entries.
    assert by["Width"]["type"] == "int" and by["Width"]["writable"] is True
    assert by["Width"]["min"] is not None and by["Width"]["max"] is not None
    pf = by["PixelFormat"]
    assert pf["type"] == "enum" and pf["entries"] and pf["entries"][0]["value"]


def test_managed_features_locked(previewing_system):
    cam = previewing_system.camera_at(0)
    by = {f["name"]: f for f in cam.list_features()}
    # octacam drives these; they show read-only with the managed flag.
    for name in ("PixelFormat", "TriggerMode"):
        assert by[name]["managed"] is True
        assert by[name]["writable"] is False
    with pytest.raises(ValueError):
        cam.set_feature("PixelFormat", "Mono12")


def test_set_feature_live_and_geometry(previewing_system):
    cam = previewing_system.camera_at(0)
    cam.set_feature("ExposureTime", 3210.0)
    assert abs(cam.read_feature("ExposureTime")["value"] - 3210.0) < 2.0
    # A Width write cycles the grab and keeps previewing.
    cam.set_feature("Width", 512)
    assert cam.backend.is_grabbing()
    assert cam.read_feature("Width")["value"] == 512


def test_reset_feature_uses_config_then_factory(previewing_system):
    cam = previewing_system.camera_at(0)
    baseline = cam.read_feature("ExposureTime")["value"]  # first-seen -> factory
    cam.set_feature("ExposureTime", baseline + 2000.0)
    assert abs(cam.read_feature("ExposureTime")["value"] - baseline) > 1.0
    # No config text for this node: falls back to the cached factory value.
    cam.reset_feature("ExposureTime", "")
    assert abs(cam.read_feature("ExposureTime")["value"] - baseline) < 2.0


def test_centering_computes_and_locks_offset(previewing_system):
    cam = previewing_system.camera_at(0)
    cam.set_feature("Width", 512)
    state = cam.set_center("x", True)
    assert state["center_x"] is True
    offset = cam.read_feature("OffsetX")
    assert offset["writable"] is False  # octacam owns it now
    # Centered: roughly (sensor_width - width) / 2.
    full = cam.read_feature("WidthMax")["value"]
    assert abs(offset["value"] - (full - 512) / 2) <= (offset["inc"] or 1)
    # Cannot set it by hand while centered.
    with pytest.raises(ValueError):
        cam.set_feature("OffsetX", 0)
    # Re-centers when the ROI changes.
    cam.set_feature("Width", 1024)
    assert abs(cam.read_feature("OffsetX")["value"] - (full - 1024) / 2) <= (
        cam.read_feature("OffsetX")["inc"] or 1
    )
    # Turning it off frees the field again.
    cam.set_center("x", False)
    assert cam.read_feature("OffsetX")["writable"] is True


def test_execute_command_runs(previewing_system):
    cam = previewing_system.camera_at(0)
    commands = [f["name"] for f in cam.list_features() if f["type"] == "command"]
    assert commands  # the emulator exposes command nodes
    cam.execute_command(commands[0])  # must not raise


def test_basler_declares_offsets_grab_locked(previewing_system):
    # Basler locks the whole ROI (size + offsets) during acquisition, so the
    # offsets ride the grab-cycle path alongside Width/Height.
    locked = previewing_system.camera_at(0).backend.grab_locked_features()
    assert {"Width", "Height", "OffsetX", "OffsetY"} <= locked


def test_offset_editable_and_written_via_grab_cycle(previewing_system):
    cam = previewing_system.camera_at(0)
    # Make room for a non-zero origin, then the ROI offset is presented editable
    # in the node-map browser even while previewing (Basler locks it mid-grab).
    cam.set_feature("Width", 512)
    by = {f["name"]: f for f in cam.list_features()}
    assert by["OffsetX"]["writable"] is True
    # A write cycles the grab (like Width/Height), lands, and preview resumes —
    # a plain mid-grab write would be rejected by the SDK.
    cam.set_feature("OffsetX", 16)
    assert cam.backend.is_grabbing()
    off = cam.read_feature("OffsetX")
    assert abs(off["value"] - 16) <= (off["inc"] or 1)


# ------------------------------------------------ a closed camera's triggers


@pytest.mark.parametrize(
    ("backend", "serials"), [("basler", EMULATED_SERIALS), ("fake", ["FAKE-0"])]
)
def test_trigger_setup_on_a_closed_camera_is_a_no_op(tmp_path, backend, serials):
    # close() drops the device handle; a recording start racing a shutdown must
    # find the trigger setup a no-op, not an AttributeError on that handle.
    system = CameraSystem(serials, backend=backend)
    assert len(system) == len(serials)
    system.load_config(tmp_path)
    system.close()
    system.enable_frame_trigger()
    system.set_trigger_source(True)
    system.set_trigger_source(False)
