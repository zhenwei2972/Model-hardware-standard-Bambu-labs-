"""Camera measurement: the arithmetic, and the honesty about its limits."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from mhs.design.vision import (
    ACCURACY_CAVEAT,
    REFERENCE_OBJECTS,
    ScaleCalibration,
    annotate_grid,
    annotate_measurement,
    calibrate,
    compare_to_target,
    distance_px,
    resolve_reference,
)
from mhs.errors import CommandRejected


@pytest.fixture
def frame() -> bytes:
    image = Image.new("RGB", (640, 360), (70, 72, 78))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


# -- references ------------------------------------------------------------
def test_named_references_resolve_to_published_sizes():
    assert resolve_reference("sgd_1")[0] == 24.65
    assert resolve_reference("USD Quarter")[0] == 24.26
    assert resolve_reference("eur-2")[0] == 25.75


def test_a_plain_measurement_is_accepted_as_a_reference():
    assert resolve_reference(31.4) == (31.4, "31.4 mm reference")
    assert resolve_reference("18.5")[0] == 18.5


def test_unknown_reference_lists_the_alternatives():
    with pytest.raises(CommandRejected) as excinfo:
        resolve_reference("doubloon")
    assert "usd_quarter" in excinfo.value.hint


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_references_are_rejected(bad):
    with pytest.raises(CommandRejected):
        resolve_reference(bad)


def test_every_reference_is_a_plausible_size():
    for name, (size, label) in REFERENCE_OBJECTS.items():
        assert 1 <= size <= 200, name
        assert label


# -- calibration -----------------------------------------------------------
def test_calibration_converts_pixels_to_millimetres():
    cal = calibrate("usd_quarter", 100)
    assert cal.mm_per_pixel == pytest.approx(0.2426)
    assert cal.measure(200) == pytest.approx(48.52)
    assert cal.pixels_for(24.26) == pytest.approx(100)
    assert cal.reference_name == "US quarter"


def test_calibration_round_trips_through_a_dict_without_losing_precision():
    cal = calibrate("sgd_1", 88, printer_id="a1", note="coin on the plate")
    restored = ScaleCalibration.from_dict(cal.to_dict())
    assert restored.mm_per_pixel == cal.mm_per_pixel
    assert restored.printer_id == "a1" and restored.note == "coin on the plate"


@pytest.mark.parametrize("bad", [0, -20])
def test_calibration_rejects_impossible_pixel_lengths(bad):
    with pytest.raises(CommandRejected):
        calibrate("sgd_1", bad)


def test_negative_measurements_are_rejected():
    with pytest.raises(CommandRejected):
        calibrate("sgd_1", 50).measure(-1)


def test_distance_is_euclidean():
    assert distance_px((0, 0), (3, 4)) == pytest.approx(5.0)


# -- comparing against a target --------------------------------------------
def test_close_enough_is_reported_as_on_target():
    result = compare_to_target(24.4, 24.65)
    assert abs(result["error_percent"]) < 2
    assert "on target" in result["assessment"]


def test_a_real_scaling_error_is_called_out():
    result = compare_to_target(30.0, 24.65)
    assert result["error_mm"] == pytest.approx(5.35)
    assert result["error_percent"] == pytest.approx(21.7, abs=0.1)
    assert "more than 5%" in result["assessment"]
    # Applying the suggested factor would land on the target.
    assert 30.0 * result["suggested_rescale_factor"] == pytest.approx(24.65, rel=1e-3)


def test_target_must_be_positive():
    with pytest.raises(CommandRejected):
        compare_to_target(10, 0)


# -- annotation ------------------------------------------------------------
def test_grid_overlay_preserves_the_frame_size(frame):
    data = annotate_grid(frame, 80)
    assert data.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(data)).size == (640, 360)


def test_grid_overlay_actually_draws_something(frame):
    plain = Image.open(io.BytesIO(frame)).convert("RGB").tobytes()
    gridded = Image.open(io.BytesIO(annotate_grid(frame, 40))).convert("RGB").tobytes()
    assert plain != gridded


def test_grid_step_has_a_floor(frame):
    with pytest.raises(CommandRejected):
        annotate_grid(frame, 2)


def test_uncalibrated_grid_says_so(frame):
    # The caption is rendered, so assert on behaviour instead: both render, and
    # a calibrated grid differs from an uncalibrated one.
    uncalibrated = annotate_grid(frame, 80)
    calibrated = annotate_grid(frame, 80, calibrate("sgd_1", 88))
    assert uncalibrated != calibrated


def test_measurement_annotation_returns_pixels_and_millimetres(frame):
    cal = calibrate("sgd_1", 100)  # 0.2465 mm/px
    data, pixels, millimetres = annotate_measurement(frame, (100, 100), (300, 100), cal)
    assert pixels == pytest.approx(200.0)
    assert millimetres == pytest.approx(49.3)
    assert Image.open(io.BytesIO(data)).size == (640, 360)


def test_measurement_without_calibration_still_reports_pixels(frame):
    _data, pixels, millimetres = annotate_measurement(frame, (0, 0), (30, 40), None)
    assert pixels == pytest.approx(50.0)
    assert millimetres is None


def test_the_caveat_names_the_real_failure_modes():
    for phrase in ("off-axis", "same plane", "calipers"):
        assert phrase in ACCURACY_CAVEAT
