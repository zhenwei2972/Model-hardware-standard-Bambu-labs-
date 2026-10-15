"""The critique: does it flag the right things, and stay quiet about the rest?"""

from __future__ import annotations

import numpy as np
import pytest

from mhs.design.mesh import Mesh, unit_cube, uv_sphere
from mhs.design.printability import analyze, scale_advice
from mhs.specs import A1_MINI, spec_for


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


def box(low, high) -> tuple[np.ndarray, np.ndarray]:
    cube = unit_cube(1)
    unit = cube.vertices - cube.vertices.min(axis=0)
    return unit * (np.asarray(high) - low) + low, cube.faces


def combine(*parts) -> Mesh:
    vertices, faces, offset = [], [], 0
    for v, f in parts:
        vertices.append(v)
        faces.append(f + offset)
        offset += len(v)
    return Mesh(np.vstack(vertices), np.vstack(faces))


def finned_block(fin_mm: float) -> Mesh:
    """A 20 x 20 x 2 mm base carrying nine 6 mm tall fins, ``fin_mm`` thick."""
    fins = [box((1 + 2 * i, 0, 2), (1 + 2 * i + fin_mm, 20, 8)) for i in range(9)]
    return combine(box((0, 0, 0), (20, 20, 2)), *fins)


def tube(outer: float, wall: float, height: float, segments: int) -> Mesh:
    """A closed ring, like a phone bumper's wall, with ``segments`` slivers round it."""
    a = np.linspace(0, 2 * np.pi, segments, endpoint=False)

    def ring(r, z):
        return np.c_[r * np.cos(a), r * np.sin(a), np.full(segments, z)]

    inner = outer - wall
    vertices = np.vstack([ring(outer, 0), ring(outer, height), ring(inner, 0), ring(inner, height)])
    i = np.arange(segments)
    j = (i + 1) % segments
    ob, ot, ib, it = i, i + segments, i + 2 * segments, i + 3 * segments
    obj, otj, ibj, itj = j, j + segments, j + 2 * segments, j + 3 * segments
    faces = np.vstack([
        np.c_[ob, obj, otj], np.c_[ob, otj, ot],  # outside
        np.c_[ib, it, itj], np.c_[ib, itj, ibj],  # inside
        np.c_[ot, otj, itj], np.c_[ot, itj, it],  # top
        np.c_[ob, ib, ibj], np.c_[ob, ibj, obj],  # bottom
    ])
    return Mesh(vertices, faces)


def test_a_sensible_part_has_no_blockers():
    report = analyze(unit_cube(30), A1_MINI)
    assert report.printable
    assert "too_large" not in codes(report)
    assert "detail_too_fine" not in codes(report)
    assert report.metrics["fits_build_volume"] is True


def test_oversized_model_is_blocked_with_a_scale_suggestion():
    report = analyze(unit_cube(400), A1_MINI)
    assert not report.printable
    blocker = next(f for f in report.blockers if f.code == "too_large")
    assert "180" in blocker.message
    assert "0.45" in blocker.suggestion  # 180/400


def test_a_model_that_exceeds_one_axis_is_blocked():
    oversize = unit_cube(1).scaled((200.0, 100.0, 100.0))
    fits = unit_cube(1).scaled((100.0, 100.0, 179.0))
    assert analyze(fits, A1_MINI).printable
    report = analyze(oversize, A1_MINI)
    assert not report.printable
    assert "X: 200.0 > 180 mm" in next(f for f in report.blockers if f.code == "too_large").message


def test_detail_finer_than_the_nozzle_is_reported_quantitatively():
    # 0.25 mm fins under a 0.4 mm nozzle: most of the surface is on them.
    report = analyze(finned_block(0.25), A1_MINI)
    assert report.metrics["detail_below_nozzle_percent"] > 50
    assert report.metrics["feature_thickness_mm"]["median"] == pytest.approx(0.25, abs=0.01)
    assert "detail_too_fine" in codes(report)
    assert "will merge or disappear" in report.verdict()


def test_fins_one_extrusion_wide_are_not_too_fine():
    report = analyze(finned_block(1.0), A1_MINI)
    assert report.metrics["detail_below_nozzle_percent"] < 2
    assert "detail_too_fine" not in codes(report)


def test_small_triangles_are_not_fine_detail():
    """The 4 Oct bumper: 2.0 mm walls drawn with tiny triangles (5th-percentile
    edge 0.007 mm) were reported as "39% of detail finer than the nozzle"."""
    ring = tube(outer=20, wall=2.0, height=10, segments=3000)
    report = analyze(ring, A1_MINI)
    assert report.metrics["edge_length_mm"]["p05"] < 0.1  # far below the nozzle
    assert report.metrics["detail_below_nozzle_percent"] < 1
    assert not codes(report) & {"detail_too_fine", "detail_partially_fine", "thin_walls"}
    assert "reproducible at this size" in report.verdict()

    dense, _ = uv_sphere(20, 120, 80).scale_to_dimension(8.0)  # 8 mm solid, tiny facets
    assert "detail_too_fine" not in codes(analyze(dense, A1_MINI))


def test_local_thickness_reads_the_wall_not_the_triangles():
    thickness, weight = tube(outer=20, wall=2.0, height=10, segments=3000).local_thickness(12.0)
    assert weight.sum() == pytest.approx(1.0)
    assert np.median(thickness) == pytest.approx(2.0, abs=0.01)
    # Beyond the reach, nothing is met: reported as inf, never as thin.
    capped, _ = tube(outer=20, wall=2.0, height=10, segments=3000).local_thickness(1.5)
    assert np.isinf(capped).mean() > 0.9


def test_a_thin_ring_is_both_too_fine_and_thin_walled():
    report = analyze(tube(outer=20, wall=0.3, height=10, segments=600), A1_MINI)
    assert {"detail_too_fine", "thin_walls"} <= codes(report)


def test_a_coarse_model_at_a_sane_size_keeps_its_detail():
    report = analyze(uv_sphere(25, 24, 16), A1_MINI)
    assert report.metrics["detail_below_nozzle_percent"] < 5
    assert "reproducible at this size" in report.verdict()


def test_thin_walls_are_flagged():
    sheet = Mesh(unit_cube(1).scaled((40.0, 40.0, 0.3)).vertices, unit_cube(1).faces)
    report = analyze(sheet, A1_MINI)
    assert "below_minimum_feature" in codes(report)
    assert report.metrics["characteristic_thickness_mm"] < A1_MINI.default_nozzle_mm * 2


def test_overhangs_and_bed_contact():
    report = analyze(uv_sphere(20, 32, 24), A1_MINI)
    assert "needs_supports" in codes(report)
    assert report.metrics["overhang_area_percent"] > 10
    assert "small_bed_contact" in codes(report)


def test_tall_and_thin_is_warned_about():
    tower = Mesh(unit_cube(1).scaled((8.0, 8.0, 120.0)).vertices, unit_cube(1).faces)
    assert "tall_and_thin" in codes(analyze(tower, A1_MINI))


def test_layer_count_and_quantisation():
    report = analyze(unit_cube(20.05), A1_MINI, layer_height_mm=0.2)
    assert report.metrics["layer_count"] == 100
    assert "z_quantisation" in codes(report)
    # An exact multiple of the layer height has nothing to report.
    assert "z_quantisation" not in codes(analyze(unit_cube(20.0), A1_MINI, layer_height_mm=0.2))


def test_layer_height_outside_the_printer_range_is_a_blocker():
    report = analyze(unit_cube(20), A1_MINI, layer_height_mm=0.4)
    assert not report.printable
    assert "layer_height_out_of_range" in codes(report)


def test_open_frame_warns_about_abs():
    assert "material_needs_enclosure" in codes(analyze(unit_cube(20), A1_MINI, material="ABS"))
    assert "material_needs_enclosure" not in codes(analyze(unit_cube(20), A1_MINI, material="PLA"))
    enclosed = spec_for("P1S")
    assert "material_needs_enclosure" not in codes(analyze(unit_cube(20), enclosed, material="ABS"))


def test_non_watertight_mesh_is_warned_about():
    cube = unit_cube(20)
    assert "not_watertight" in codes(analyze(Mesh(cube.vertices, cube.faces[:-2]), A1_MINI))


def test_mass_and_filament_estimates_are_present_and_ordered():
    small = analyze(unit_cube(10), A1_MINI).metrics
    large = analyze(unit_cube(20), A1_MINI).metrics
    assert 0 < small["estimated_mass_g"] < large["estimated_mass_g"]
    assert large["estimated_filament_m"] > small["estimated_filament_m"]


def test_report_serialises_for_a_tool_result():
    payload = analyze(unit_cube(30), A1_MINI).to_dict()
    assert payload["printable"] is True
    assert set(payload) >= {"printer", "verdict", "limits", "dimensions_mm", "findings", "metrics"}
    assert all(f["severity"] in {"blocker", "warning", "note"} for f in payload["findings"])
    severities = [f["severity"] for f in payload["findings"]]
    assert severities == sorted(severities, key=["blocker", "warning", "note"].index)


# -- scale advice ----------------------------------------------------------
def test_scale_advice_quantifies_the_detail_lost():
    # 1 mm fins on a 20 mm block, shrunk to 7 mm: the fins become 0.35 mm.
    advice = scale_advice(finned_block(1.0), A1_MINI, 7.0)
    assert advice["factor"] == pytest.approx(7 / 20, rel=1e-3)
    assert advice["dimensions_after_mm"][0] == pytest.approx(7.0)
    assert advice["detail_below_nozzle_percent_before"] < 2
    assert advice["detail_below_nozzle_percent_after"] > 40
    assert "printable threshold" in advice["note"]


def test_scaling_up_recovers_detail():
    advice = scale_advice(finned_block(0.25), A1_MINI, 60.0)
    assert advice["factor"] > 1
    assert advice["detail_below_nozzle_percent_before"] > 50
    assert advice["detail_below_nozzle_percent_after"] < 2


def test_scale_advice_and_analyze_agree_at_the_same_size():
    model = finned_block(0.25)
    advice = scale_advice(model, A1_MINI, 20.0)
    assert advice["detail_below_nozzle_percent_before"] == pytest.approx(
        analyze(model, A1_MINI).metrics["detail_below_nozzle_percent"], abs=3)


def test_scale_advice_respects_the_axis():
    model = unit_cube(1).scaled((10.0, 20.0, 40.0))
    assert scale_advice(model, A1_MINI, 10.0, axis="x")["factor"] == pytest.approx(1.0)
    assert scale_advice(model, A1_MINI, 10.0, axis="max")["factor"] == pytest.approx(0.25)


def test_unknown_printer_falls_back_to_a_generic_spec():
    generic = spec_for("Some Other Printer")
    assert generic.model == "generic FDM"
    assert analyze(unit_cube(20), generic).printable


def test_face_normals_are_unit_length():
    normals = uv_sphere(10, 24, 16).face_normals
    assert np.allclose(np.linalg.norm(normals, axis=1), 1.0)
