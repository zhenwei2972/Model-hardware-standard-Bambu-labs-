"""Geometry: parsing, measurement and scaling. Checked against analytic values."""

from __future__ import annotations

import math
import struct
import zipfile

import numpy as np
import pytest

from mhs.design.mesh import Mesh, MeshError, load_mesh, save_stl, unit_cube, uv_sphere

ASCII_STL = """solid tri
facet normal 0 0 1
  outer loop
    vertex 0 0 0
    vertex 10 0 0
    vertex 0 10 0
  endloop
endfacet
endsolid tri
"""


def test_cube_measurements_are_exact():
    cube = unit_cube(10)
    assert cube.triangle_count == 12
    assert cube.volume_mm3 == pytest.approx(1000.0)
    assert cube.surface_area_mm2 == pytest.approx(600.0)
    assert list(cube.dimensions) == [10, 10, 10]
    assert cube.is_watertight()


def test_sphere_approaches_the_analytic_volume():
    coarse, fine = uv_sphere(10, 16, 12), uv_sphere(10, 96, 64)
    exact = 4 / 3 * math.pi * 10**3
    # A faceted sphere is inscribed, so it under-reports and converges from below.
    assert coarse.volume_mm3 < fine.volume_mm3 < exact
    assert fine.volume_mm3 == pytest.approx(exact, rel=0.002)


def test_binary_stl_round_trip_welds_shared_vertices(tmp_path):
    cube = unit_cube(8)
    path = save_stl(cube, tmp_path / "cube.stl")
    loaded = load_mesh(path)
    assert loaded.triangle_count == 12
    assert loaded.vertex_count == 8  # 36 loose corners welded back to 8
    assert loaded.is_watertight()
    assert loaded.volume_mm3 == pytest.approx(cube.volume_mm3)


def test_ascii_stl(tmp_path):
    path = tmp_path / "tri.stl"
    path.write_text(ASCII_STL)
    mesh = load_mesh(path)
    assert mesh.triangle_count == 1
    assert mesh.surface_area_mm2 == pytest.approx(50.0)


def test_obj_with_polygons_and_negative_indices(tmp_path):
    path = tmp_path / "quad.obj"
    path.write_text(
        "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n"
        "f 1/1 2/2 3/3 4/4\n"   # quad, fan-triangulated
        "f -4 -3 -2\n"          # negative indices
    )
    mesh = load_mesh(path)
    assert mesh.triangle_count == 3
    assert mesh.vertex_count == 4


def write_3mf(path, unit="millimeter", transform=None):
    item = f'<item objectid="1" transform="{transform}"/>' if transform else '<item objectid="1"/>'
    xml = f"""<?xml version="1.0"?>
<model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources><object id="1" type="model"><mesh>
  <vertices>
   <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
   <vertex x="10" y="10" z="0"/><vertex x="0" y="0" z="10"/>
  </vertices>
  <triangles>
   <triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/>
   <triangle v1="1" v2="2" v3="3"/><triangle v1="0" v2="2" v3="3"/>
  </triangles>
 </mesh></object></resources>
 <build>{item}</build>
</model>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", xml)
    return path


def test_3mf_tetrahedron(tmp_path):
    mesh = load_mesh(write_3mf(tmp_path / "part.3mf"))
    assert mesh.triangle_count == 4
    assert mesh.vertex_count == 4
    assert mesh.volume_mm3 == pytest.approx(10 * 10 / 2 * 10 / 3)
    assert mesh.is_watertight()


def test_3mf_units_are_converted_to_mm(tmp_path):
    mesh = load_mesh(write_3mf(tmp_path / "inches.3mf", unit="inch"))
    assert mesh.unit_scale_applied == 25.4
    assert mesh.dimensions[0] == pytest.approx(254.0)


def test_3mf_build_item_transform_is_applied(tmp_path):
    # Identity rotation with a +100 mm X translation.
    mesh = load_mesh(write_3mf(tmp_path / "moved.3mf", transform="1 0 0 0 1 0 0 0 1 100 0 0"))
    assert mesh.bounds[0][0] == pytest.approx(100.0)


def test_unsupported_and_missing_files(tmp_path):
    with pytest.raises(MeshError, match="does not exist"):
        load_mesh(tmp_path / "ghost.stl")
    step = tmp_path / "part.step"
    step.write_text("not a mesh")
    with pytest.raises(MeshError, match="unsupported"):
        load_mesh(step)


def test_corrupt_stl_is_rejected(tmp_path):
    path = tmp_path / "bad.stl"
    path.write_bytes(b"\0" * 80 + struct.pack("<I", 9999) + b"\0" * 20)
    with pytest.raises(MeshError):
        load_mesh(path)


def test_scale_to_dimension_keeps_proportions():
    cube = unit_cube(10).scaled((1, 2, 3))
    scaled, factor = cube.scale_to_dimension(15.0, "max")
    assert factor == pytest.approx(0.5)
    assert list(scaled.dimensions) == pytest.approx([5, 10, 15])

    by_axis, factor = cube.scale_to_dimension(5.0, "x")
    assert factor == pytest.approx(0.5)
    assert by_axis.dimensions[0] == pytest.approx(5.0)


@pytest.mark.parametrize("bad", [0, -3])
def test_scale_rejects_nonsense(bad):
    with pytest.raises(MeshError):
        unit_cube(10).scale_to_dimension(bad)
    with pytest.raises(MeshError):
        unit_cube(10).scaled(bad)


def test_scale_to_fit_shrinks_but_never_enlarges():
    big = unit_cube(400)
    fitted, factor = big.scale_to_fit((180, 180, 180))
    assert factor == pytest.approx(0.45)
    assert fitted.dimensions.max() == pytest.approx(180)

    small = unit_cube(20)
    unchanged, factor = small.scale_to_fit((180, 180, 180))
    assert factor == 1.0 and unchanged is small


def test_centering_places_the_model_on_the_plate():
    mesh = unit_cube(20).centered_on_bed((180, 180, 180))
    low, _high = mesh.bounds
    assert low[2] == pytest.approx(0.0)
    assert mesh.center[0] == pytest.approx(90.0)


def test_non_watertight_mesh_is_detected():
    cube = unit_cube(10)
    open_box = Mesh(cube.vertices, cube.faces[:-2])  # remove a face
    assert not open_box.is_watertight()


def test_empty_mesh_is_rejected():
    with pytest.raises(MeshError):
        Mesh(np.zeros((3, 3)), np.zeros((0, 3)))
    with pytest.raises(MeshError, match="out of range"):
        Mesh(np.zeros((3, 3)), np.array([[0, 1, 9]]))


def test_mass_estimate_scales_with_volume():
    small, large = unit_cube(10), unit_cube(20)
    assert large.estimated_mass_g() == pytest.approx(small.estimated_mass_g() * 8)
    assert unit_cube(10).estimated_mass_g(material="ABS") < unit_cube(10).estimated_mass_g(material="PLA")
