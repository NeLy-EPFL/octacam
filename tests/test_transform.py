"""DisplayTransform: orientation correctness and numpy <-> ffmpeg equivalence."""

import numpy as np
import pytest

from octacam.config import CameraConfig
from octacam.transform import (
    DisplayTransform,
    apply_display_transform,
    display_vf_filter,
    from_camera_config,
)

ARR = np.array([[1, 2], [3, 4]], dtype=np.uint8)


def test_identity_is_noop_and_passthrough():
    t = DisplayTransform()
    assert t.is_identity
    out = apply_display_transform(ARR, t)
    assert np.array_equal(out, ARR)
    assert display_vf_filter(t) == ""
    assert t.output_size(640, 480) == (640, 480)


@pytest.mark.parametrize(
    "t, expected",
    [
        (DisplayTransform(90), [[3, 1], [4, 2]]),  # clockwise
        (DisplayTransform(180), [[4, 3], [2, 1]]),
        (DisplayTransform(270), [[2, 4], [1, 3]]),
        (DisplayTransform(0, flip_h=True), [[2, 1], [4, 3]]),
        (DisplayTransform(0, flip_v=True), [[3, 4], [1, 2]]),
    ],
)
def test_numpy_orientation(t, expected):
    out = apply_display_transform(ARR, t)
    assert np.array_equal(out, np.array(expected, dtype=np.uint8))
    assert out.flags["C_CONTIGUOUS"]


def test_output_size_swaps_on_quarter_turns():
    assert DisplayTransform(90).output_size(640, 480) == (480, 640)
    assert DisplayTransform(270).output_size(640, 480) == (480, 640)
    assert DisplayTransform(180).output_size(640, 480) == (640, 480)


def test_from_camera_config_maps_scale_sign_and_rotation():
    cfg = CameraConfig(serial_number="x", scale_x=-1.0, scale_y=2.0, rotation_deg=270)
    t = from_camera_config(cfg)
    assert t == DisplayTransform(rotation_deg=270, flip_h=True, flip_v=False)


def test_from_camera_config_drops_non_quadrant_rotation():
    cfg = CameraConfig(serial_number="x", rotation_deg=45.0)
    assert from_camera_config(cfg).rotation_deg == 0


def test_from_scale_rotation_matches_gui_vocabulary():
    # The View tab sends composed scale_x/scale_y/rotation_deg (negative = flip).
    assert DisplayTransform.from_scale_rotation(-1.0, 1.0, 90) == DisplayTransform(
        rotation_deg=90, flip_h=True, flip_v=False
    )
    assert DisplayTransform.from_scale_rotation(1.0, -1.0, 360) == DisplayTransform(
        rotation_deg=0, flip_h=False, flip_v=True
    )


def test_vf_filter_strings():
    assert display_vf_filter(DisplayTransform(90)) == "transpose=1"
    assert display_vf_filter(DisplayTransform(270)) == "transpose=2"
    assert display_vf_filter(DisplayTransform(180)) == "transpose=1,transpose=1"
    assert display_vf_filter(DisplayTransform(90, flip_h=True)) == "transpose=1,hflip"
    assert display_vf_filter(DisplayTransform(0, flip_v=True)) == "vflip"


def test_dict_roundtrip():
    t = DisplayTransform(180, flip_h=True)
    assert DisplayTransform.from_dict(t.to_dict()) == t


def test_numpy_and_ffmpeg_agree_on_every_combo(tmp_path):
    """The recorded (numpy) and transcoded (ffmpeg -vf) pixels must match."""
    cv2 = pytest.importorskip("cv2")
    from octacam.writer import transcode_raw

    width, height = 8, 6
    frame = np.arange(height * width, dtype=np.uint8).reshape(height, width) * 3
    combos = [
        DisplayTransform(r, fh, fv)
        for r in (0, 90, 180, 270)
        for fh in (False, True)
        for fv in (False, True)
    ]
    for i, t in enumerate(combos):
        raw = tmp_path / f"c{i}.raw"
        raw.write_bytes(frame.tobytes())
        out = tmp_path / f"c{i}.mkv"
        transcode_raw(
            raw,
            ffmpeg_params="-c:v libx264 -preset ultrafast -crf 0 -pix_fmt gray",
            output=out,
            vf=display_vf_filter(t),
            width=width,
            height=height,
            fps=10.0,
        )
        cap = cv2.VideoCapture(str(out))
        ok, decoded = cap.read()
        cap.release()
        assert ok, t
        assert np.array_equal(decoded[:, :, 0], apply_display_transform(frame, t)), t
