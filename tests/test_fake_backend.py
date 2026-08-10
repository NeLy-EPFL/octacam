"""Fake-backend tests: the SDK-neutral abstraction without any camera SDK.

These exercise what the Basler emulator (PYLON_CAMEMU) cannot: the
backend-selection path, the persistence generalization (a non-".pfs" extension
round-tripping through load_config), and deterministic node behaviour — all in
pure Python with no hardware.
"""

import os

os.environ.setdefault("OCTACAM_FAKE_CAMERAS", "FAKE-0,FAKE-1")

import logging
import threading
import time

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


def test_load_params_grows_roi_past_a_previous_sessions_offset(previewing_system):
    """Launching rig B after rig A must not be clamped by rig A's cropped ROI.

    A camera keeps its ROI until it is power-cycled, and a size node's max is
    (sensor - origin), so applying rig B's full-sensor Height while rig A's
    OffsetY is still on the device is out of range — on the rig this took down the
    whole GUI init ("Value = 2048 must be equal or smaller than Max = 1770").
    The applier clears the origin before programming the size."""
    cam = previewing_system.camera_at(0)
    full_w, full_h = cam.width, cam.height

    def roi(width, height, offset_y=0):
        return (
            "# {octacam GenApi persistence}\n"
            f"Width\t{width}\nHeight\t{height}\nOffsetX\t0\nOffsetY\t{offset_y}\n"
        )

    # Rig A: a cropped, vertically offset ROI (as configs/hexaview ships).
    cam.load_params(roi(full_w, full_h - 640, offset_y=278))
    assert (cam.width, cam.height) == (full_w, full_h - 640)
    assert cam._backend._get_number("OffsetY", True) == 278

    # Rig B: the whole sensor back (as configs/triggerbox ships).
    cam.load_params(roi(full_w, full_h))
    assert (cam.width, cam.height) == (full_w, full_h)
    assert cam._backend._get_number("OffsetY", True) == 0


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


# ---------------------------------------------- cam-core regression fixes


def test_start_record_skips_a_camera_that_raises_unexpectedly(tmp_path, monkeypatch):
    # Regression (system.py:326): a *non*-BackendError raised by one camera's
    # start_record must be logged-and-skipped, not re-raised — the other cameras
    # have already launched their grab thread + ffmpeg child, so propagating would
    # abandon them half-started (and violates start_record's documented contract).
    from octacam.writer import FORMATS

    system = CameraSystem(FAKE_SERIALS, backend="fake")
    try:
        system.load_config(tmp_path)
        good = system.camera_at(0)
        bad = system.camera_at(1)
        monkeypatch.setattr(good, "start_record", lambda *a, **k: True)

        def boom(*a, **k):
            raise RuntimeError("insufficient resources")

        monkeypatch.setattr(bad, "start_record", boom)
        started = system.start_record(tmp_path, 100.0, FORMATS["raw"])
        assert started == [good.name]  # good camera started; no exception propagated
    finally:
        system.close()


def test_open_phase_skips_one_camera_that_fails_to_open(tmp_path, monkeypatch):
    # Regression: a single camera that fails to open must be dropped (logged),
    # not abort the whole rig — mirroring the enumerate "not found" skip and the
    # start_record skip. This is what lets the auto cascade survive a USB3 camera
    # that fell back to USB 2.0 and got claimed by the pycameleon floor (it opens
    # on no backend), instead of one bad camera crashing every camera.
    from octacam.cameras.fake import FakeBackend

    real_open = FakeBackend.open

    def flaky_open(self):
        if self.serial_number == "FAKE-1":
            raise RuntimeError("simulated open failure (USB 2.0 fallback)")
        real_open(self)

    monkeypatch.setattr(FakeBackend, "open", flaky_open)
    # Capture on the octacam logger directly, not via caplog: another test (the
    # CLI's _setup_logging) may leave propagate=False, emptying caplog's capture.
    msgs: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: msgs.append(record.getMessage())
    logger = logging.getLogger("octacam")
    logger.addHandler(handler)
    try:
        system = CameraSystem(["FAKE-0", "FAKE-1"], backend="fake")
    finally:
        logger.removeHandler(handler)
    try:
        assert [c.serial_number for c in system] == ["FAKE-0"]  # bad one dropped
        assert any("FAKE-1" in m for m in msgs)
    finally:
        system.close()


def test_open_phase_raises_only_when_every_camera_fails(monkeypatch):
    # If nothing opens, construction still raises so the caller can surface it
    # (cli.py turns this into a clean fail_init, never a raw traceback).
    from octacam.cameras.fake import FakeBackend

    def always_fail(self):
        raise RuntimeError("all dead")

    monkeypatch.setattr(FakeBackend, "open", always_fail)
    with pytest.raises(RuntimeError, match="all dead"):
        CameraSystem(["FAKE-0", "FAKE-1"], backend="fake")


def test_preview_bounds_the_timestamp_series(tmp_path):
    # Regression (base.py:1006): a continuously-running preview (the GUI's idle
    # steady state) must not grow self._timestamps without bound. The series is
    # trimmed to PREVIEW_TIMESTAMPS_MAX while the rolling fps readout keeps working.
    from octacam.cameras.base import PREVIEW_TIMESTAMPS_MAX

    system = CameraSystem(FAKE_SERIALS, backend="fake")
    try:
        system.load_config(tmp_path)
        system.start_preview("free_running", fps=2000.0)
        cam = system.camera_at(0)
        deadline = time.monotonic() + 3.0
        while cam.frames_recorded < PREVIEW_TIMESTAMPS_MAX and time.monotonic() < deadline:
            cam.frame_for_display.pop()  # drain the display slot so pushes flow
            time.sleep(0.01)
        assert cam.frames_recorded >= PREVIEW_TIMESTAMPS_MAX
        assert cam.resulting_fps > 0  # readout still computed from the tail
        # Keep grabbing well past the cap; without trimming this would be hundreds.
        time.sleep(0.2)
        assert cam.frames_recorded <= PREVIEW_TIMESTAMPS_MAX + 1
    finally:
        system.close()


def test_start_preview_falls_back_to_software_when_freerun_unavailable(
    tmp_path, monkeypatch
):
    # Regression (base.py:873): begin_freerun returning False means the backend
    # could not arm free-run; start_preview must fall back to a software-trigger
    # preview arm rather than grab forever in a mode the camera was never armed for.
    system = CameraSystem(FAKE_SERIALS, backend="fake")
    try:
        system.load_config(tmp_path)
        cam = system.camera_at(0)
        monkeypatch.setattr(cam.backend, "begin_freerun", lambda fps=None: False)
        armed: list[int] = []
        real_arm = cam.backend.begin_software_trigger_preview
        monkeypatch.setattr(
            cam.backend,
            "begin_software_trigger_preview",
            lambda: (armed.append(1), real_arm())[1],
        )
        cam.start_preview("free_running", fps=50.0)
        assert armed, "did not fall back to the software-trigger preview arm"
        assert cam.backend.is_grabbing()
        # The fallback is a software-trigger preview: frames arrive on a trigger.
        deadline = time.monotonic() + 2.0
        while cam.frames_recorded < 1 and time.monotonic() < deadline:
            cam.trigger_once()
            cam.frame_for_display.pop()
            time.sleep(0.02)
        assert cam.frames_recorded >= 1  # frames flowed via the software fallback
    finally:
        system.close()


def test_managed_preview_grab_is_paced_not_busylooped():
    # Regression (fake.py:376): a managed preview grabs via retrieve_freerun with no
    # fps cap (_freerun_fps is None), yet must still block ~ the grab timeout rather
    # than return instantly — otherwise the preview thread busy-loops at 100% CPU.
    from octacam.cameras.fake import FakeBackend

    be = FakeBackend("FAKE-managed")
    be.open()
    be.start_grab_preview()  # managed preview: begin_freerun is NOT called
    assert be._freerun_fps is None
    t0 = time.monotonic()
    frame = be.retrieve_freerun(60, lambda: True)  # 60 ms grab timeout
    elapsed = time.monotonic() - t0
    assert frame is not None
    assert elapsed >= 0.04  # waited ~ the timeout instead of spinning
    be.stop_grab()


def test_record_grab_freerun_returns_immediately():
    # The benchmark's ceiling probe uses start_grab_record (uncapped) and must keep
    # returning a frame immediately, not paced by the preview guard.
    from octacam.cameras.fake import FakeBackend

    be = FakeBackend("FAKE-bench")
    be.open()
    be.start_grab_record()
    assert be._freerun_fps is None
    t0 = time.monotonic()
    frame = be.retrieve_freerun(1000, lambda: True)  # would block 1 s if paced
    elapsed = time.monotonic() - t0
    assert frame is not None
    assert elapsed < 0.1  # returned immediately
    be.stop_grab()


def test_stop_grab_wakes_a_blocked_managed_preview_retrieve():
    # The paced managed-preview wait must stay wakeable by stop_grab's notify so
    # stop latency is not the full grab timeout.
    from octacam.cameras.fake import FakeBackend

    be = FakeBackend("FAKE-wake")
    be.open()
    be.start_grab_preview()
    result: dict[str, object] = {}

    def grab():
        t0 = time.monotonic()
        result["frame"] = be.retrieve_freerun(5000, lambda: True)
        result["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=grab)
    th.start()
    time.sleep(0.05)
    be.stop_grab()  # flips _grabbing and notifies the parked retrieve
    th.join(2.0)
    assert not th.is_alive()
    assert result["frame"] is None  # grabbing flipped, so it returns None
    assert result["elapsed"] < 1.0  # woken promptly, not the full 5 s timeout
