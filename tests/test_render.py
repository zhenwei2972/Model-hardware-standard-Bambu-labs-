"""The renderer: correct occlusion and honest scale are what make a preview useful."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from mhs.design.mesh import Mesh, unit_cube, uv_sphere
from mhs.design.render import (
    BACKGROUND,
    VIEWS,
    RenderStyle,
    overhanging_faces,
    rasterise,
    render_to_png,
    render_views,
)


def test_every_named_view_renders():
    cube = unit_cube(10)
    for view in VIEWS:
        pixels, scale = rasterise(cube, view, size=120)
        assert pixels.shape == (120, 120, 3)
        assert scale > 0
        drawn = (pixels != np.array(BACKGROUND)).any(axis=2)
        assert drawn.any(), f"{view} rendered nothing"


def test_unknown_view_is_rejected():
    with pytest.raises(ValueError, match="unknown view"):
        rasterise(unit_cube(10), "sideways")


def test_front_view_of_a_cube_fills_a_square_block():
    pixels, _ = rasterise(unit_cube(10), "front", size=200, style=RenderStyle(show_scale_bar=False))
    drawn = (pixels != np.array(BACKGROUND)).any(axis=2)
    rows, cols = np.where(drawn)
    height, width = np.ptp(rows) + 1, np.ptp(cols) + 1
    assert height == pytest.approx(width, abs=2)  # square from the front
    assert drawn[rows.min():rows.max(), cols.min():cols.max()].mean() > 0.98  # solid, no holes


def test_nearer_geometry_occludes_further_geometry():
    """A small cube in front of a large one must hide part of it, not blend with it."""
    back = unit_cube(30)
    front = Mesh(unit_cube(10).vertices + np.array([10.0, -40.0, 10.0]), unit_cube(10).faces)
    scene = Mesh(np.vstack([back.vertices, front.vertices]),
                 np.vstack([back.faces, front.faces + len(back.vertices)]))
    pixels, _ = rasterise(scene, "front", size=200, style=RenderStyle(show_scale_bar=False))

    # The nearer cube is lit differently; the centre must come from one surface only.
    centre = pixels[95:105, 95:105].reshape(-1, 3)
    assert len({tuple(p) for p in centre}) == 1


def test_shared_scale_keeps_relative_size_truthful():
    big = unit_cube(40)
    sheet = render_views(big, views=("front", "right"), size=100, style=RenderStyle(show_scale_bar=False))
    left = np.array(sheet)[34:134, 0:100]
    right = np.array(sheet)[34:134, 100:200]
    drawn_left = (left != np.array(BACKGROUND)).any(axis=2).sum()
    drawn_right = (right != np.array(BACKGROUND)).any(axis=2).sum()
    assert drawn_left == pytest.approx(drawn_right, rel=0.02)


def test_contact_sheet_layout_and_caption():
    sheet = render_views(unit_cube(10), views=("iso", "front", "top", "bottom"), size=150,
                         title="my part")
    assert sheet.size[0] == 300  # two columns
    assert sheet.size[1] > 300  # plus header and caption


def test_png_output_is_a_real_png(tmp_path):
    data = render_to_png(uv_sphere(5, 16, 12), tmp_path / "out.png", size=120)
    assert data.startswith(b"\x89PNG")
    assert (tmp_path / "out.png").read_bytes() == data
    assert Image.open(io.BytesIO(data)).size[0] == 240


def test_render_is_deterministic():
    first = render_to_png(uv_sphere(8, 20, 14), views=("iso",), size=120)
    second = render_to_png(uv_sphere(8, 20, 14), views=("iso",), size=120)
    assert first == second


# -- overhang detection ----------------------------------------------------
def test_cube_has_no_overhangs_because_its_base_sits_on_the_plate():
    assert overhanging_faces(unit_cube(10)).sum() == 0


def test_protruding_ledge_is_flagged_but_the_bed_face_is_not():
    base = unit_cube(20)
    arm = unit_cube(1).scaled((36.0, 8.0, 3.0))
    arm = Mesh(arm.vertices + np.array([-8.0, 6.0, 15.0]), arm.faces)
    part = Mesh(np.vstack([base.vertices, arm.vertices]),
                np.vstack([base.faces, arm.faces + len(base.vertices)]))

    mask = overhanging_faces(part, 45.0)
    assert mask.sum() == 2  # the arm's underside, two triangles

    downward = part.face_normals[:, 2] < -0.9
    on_bed = downward & ~mask
    assert on_bed.sum() == 2  # the base, excluded because the plate supports it


def test_sphere_reports_a_large_overhang_area():
    sphere = uv_sphere(10, 32, 24)
    mask = overhanging_faces(sphere, 45.0)
    fraction = sphere.face_areas[mask].sum() / sphere.surface_area_mm2
    assert 0.1 < fraction < 0.4  # the lower band, minus the near-vertical middle


def test_overhang_threshold_is_respected():
    sphere = uv_sphere(10, 32, 24)
    assert overhanging_faces(sphere, 60.0).sum() < overhanging_faces(sphere, 30.0).sum()
