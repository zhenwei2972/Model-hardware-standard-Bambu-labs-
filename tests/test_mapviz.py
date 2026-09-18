"""Pixel-to-millimetre mapping: the arithmetic that aiming depends on.

If this is wrong the robot drives to the wrong place and nothing errors, so the
transform is tested against a known-good Roborock-shaped calibration - centre
25500 mm, 50 mm per pixel, y flipped - rather than only round-tripping itself.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from mhs.design.mapviz import MapTransform, annotate_map, choose_grid_mm, choose_scale
from mhs.errors import CommandRejected
from mhs.models import Position, Room

# 50 mm/px, origin at pixel (100, 400), y increasing upwards in millimetres.
ROBOROCK_CALIBRATION = [
    {"vacuum": {"x": 25500, "y": 25500}, "map": {"x": 100, "y": 400}},
    {"vacuum": {"x": 35500, "y": 25500}, "map": {"x": 300, "y": 400}},
    {"vacuum": {"x": 25500, "y": 35500}, "map": {"x": 100, "y": 200}},
]


@pytest.fixture
def transform() -> MapTransform:
    return MapTransform.from_calibration(ROBOROCK_CALIBRATION)


def test_scale_matches_the_roborock_grid(transform):
    assert transform.mm_per_pixel == pytest.approx(50.0)


def test_calibration_points_map_to_their_own_pixels(transform):
    for point in ROBOROCK_CALIBRATION:
        px, py = transform.to_pixels(point["vacuum"]["x"], point["vacuum"]["y"])
        assert (px, py) == pytest.approx((point["map"]["x"], point["map"]["y"]))


def test_the_y_axis_is_flipped(transform):
    """Millimetres grow upwards, image rows grow downwards."""
    _px_low, py_low = transform.to_pixels(25500, 25500)
    _px_high, py_high = transform.to_pixels(25500, 30500)
    assert py_high < py_low


def test_round_trip_is_exact(transform):
    for x_mm, y_mm in ((25500, 25500), (24000, 26200), (31234, 22987)):
        assert transform.to_mm(*transform.to_pixels(x_mm, y_mm)) == pytest.approx((x_mm, y_mm))


def test_a_rotated_render_still_inverts_correctly():
    """The transform is affine, not axis-aligned scaling: rotation must survive."""
    rotated = [
        {"vacuum": {"x": 0, "y": 0}, "map": {"x": 50, "y": 50}},
        {"vacuum": {"x": 1000, "y": 0}, "map": {"x": 50, "y": 70}},   # +x goes down
        {"vacuum": {"x": 0, "y": 1000}, "map": {"x": 70, "y": 50}},   # +y goes right
    ]
    transform = MapTransform.from_calibration(rotated)
    assert transform.to_pixels(1000, 1000) == pytest.approx((70, 70))
    assert transform.to_mm(70, 70) == pytest.approx((1000, 1000))


@pytest.mark.parametrize(
    "points",
    [
        [],
        ROBOROCK_CALIBRATION[:2],
        [{"vacuum": {"x": 0, "y": 0}, "map": {"x": 0, "y": 0}}] * 3,  # no independent axes
        [{"nope": 1}, {"nope": 2}, {"nope": 3}],
    ],
)
def test_bad_calibration_is_rejected_rather_than_guessed(points):
    with pytest.raises(CommandRejected):
        MapTransform.from_calibration(points)


def test_grid_spacing_is_a_round_number(transform):
    assert choose_grid_mm(transform, 400) in (100, 250, 500, 1000, 2000, 5000)


def test_small_maps_are_upscaled_for_legibility():
    assert choose_scale(120, 90) >= 4
    assert choose_scale(1600, 1200) == 1


def test_annotation_produces_a_larger_readable_png(transform):
    source = io.BytesIO()
    Image.new("RGB", (120, 90), (240, 240, 240)).save(source, format="PNG")
    out = annotate_map(
        source.getvalue(),
        transform,
        rooms=[Room(16, "Kitchen", Position(25500, 25500))],
        robot=Position(25600, 25600),
        charger=Position(25500, 25500),
    )
    assert out.startswith(b"\x89PNG")
    rendered = Image.open(io.BytesIO(out))
    assert rendered.width >= 900  # upscaled, so the labels fit
    assert rendered.size[0] / rendered.size[1] == pytest.approx(120 / 90, abs=0.01)


def test_annotation_is_deterministic(transform):
    source = io.BytesIO()
    Image.new("RGB", (100, 100), (200, 200, 200)).save(source, format="PNG")
    blank = source.getvalue()
    assert annotate_map(blank, transform) == annotate_map(blank, transform)
