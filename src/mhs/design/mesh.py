"""Triangle-mesh loading, measurement and scaling.

Formats are parsed directly (no CAD dependency) because the three that matter
for a Bambu workflow - STL, OBJ and 3MF - are all simple, and because a
deterministic parser is testable without fixtures from a vendor tool.

All geometry is held in millimetres: STL/OBJ are unitless by convention, and 3MF
declares its unit, which is converted on load.
"""

from __future__ import annotations

import math
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..errors import MHSError

SUPPORTED_SUFFIXES = (".stl", ".obj", ".3mf")

#: 3MF declares its own unit; everything downstream works in millimetres.
_UNIT_TO_MM = {
    "micron": 0.001, "millimeter": 1.0, "centimeter": 10.0,
    "inch": 25.4, "foot": 304.8, "meter": 1000.0,
}


class MeshError(MHSError):
    """The file could not be read as a triangle mesh."""


@dataclass
class Mesh:
    """A triangle soup with the measurements a print decision needs."""

    vertices: np.ndarray  # (N, 3) float64, millimetres
    faces: np.ndarray  # (M, 3) int64 indices into vertices
    source: str | None = None
    unit_scale_applied: float = 1.0

    # -- basic properties --------------------------------------------------
    def __post_init__(self) -> None:
        self.vertices = np.asarray(self.vertices, dtype=np.float64).reshape(-1, 3)
        self.faces = np.asarray(self.faces, dtype=np.int64).reshape(-1, 3)
        if len(self.faces) == 0:
            raise MeshError(f"{self.source or 'mesh'} contains no triangles")
        if self.faces.max() >= len(self.vertices):
            raise MeshError("face index out of range - corrupt mesh file")

    @property
    def triangle_count(self) -> int:
        return len(self.faces)

    @property
    def vertex_count(self) -> int:
        return len(self.vertices)

    @property
    def triangles(self) -> np.ndarray:
        """(M, 3, 3) array of triangle corner coordinates."""
        return self.vertices[self.faces]

    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    @property
    def dimensions(self) -> np.ndarray:
        low, high = self.bounds
        return high - low

    @property
    def center(self) -> np.ndarray:
        low, high = self.bounds
        return (low + high) / 2

    @property
    def face_normals(self) -> np.ndarray:
        tris = self.triangles
        normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        return np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)

    @property
    def face_areas(self) -> np.ndarray:
        tris = self.triangles
        return np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1) / 2

    @property
    def surface_area_mm2(self) -> float:
        return float(self.face_areas.sum())

    @property
    def volume_mm3(self) -> float:
        """Signed tetrahedron sum; meaningful only for a closed mesh."""
        a, b, c = self.triangles[:, 0], self.triangles[:, 1], self.triangles[:, 2]
        return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)

    @property
    def edge_lengths(self) -> np.ndarray:
        tris = self.triangles
        return np.concatenate([
            np.linalg.norm(tris[:, 1] - tris[:, 0], axis=1),
            np.linalg.norm(tris[:, 2] - tris[:, 1], axis=1),
            np.linalg.norm(tris[:, 0] - tris[:, 2], axis=1),
        ])

    def is_watertight(self) -> bool:
        """True when every edge is shared by exactly two triangles.

        A cheap manifold test: enough to warn that a slicer may produce holes,
        not a full mesh-repair diagnosis.
        """
        edges = np.vstack([self.faces[:, [0, 1]], self.faces[:, [1, 2]], self.faces[:, [2, 0]]])
        edges = np.sort(edges, axis=1)
        _unique, counts = np.unique(edges, axis=0, return_counts=True)
        return bool((counts == 2).all())

    def welded(self, decimals: int = 6) -> Mesh:
        """Merge coincident vertices and drop the degenerate faces that leaves.

        STL stores three loose corners per triangle with no sharing, so an
        unwelded STL never looks watertight and reports 3x the real vertex
        count. Every other check here depends on shared topology.
        """
        keys = np.round(self.vertices, decimals)
        _unique, first_index, inverse = np.unique(
            keys, axis=0, return_index=True, return_inverse=True
        )
        vertices = self.vertices[np.sort(first_index)]
        # np.unique sorts its output; remap so indices follow the kept order.
        order = np.argsort(np.argsort(first_index))
        faces = order[inverse.reshape(-1)][self.faces]
        keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
        return Mesh(vertices, faces[keep], self.source, self.unit_scale_applied)

    # -- transforms --------------------------------------------------------
    def scaled(self, factor: float | tuple[float, float, float]) -> Mesh:
        """Return a scaled copy. A scalar keeps proportions."""
        factors = np.asarray([factor] * 3 if np.isscalar(factor) else factor, dtype=np.float64)
        if factors.shape != (3,) or (factors <= 0).any() or not np.isfinite(factors).all():
            raise MeshError(f"invalid scale factor {factor!r}")
        return Mesh(self.vertices * factors, self.faces.copy(), self.source, self.unit_scale_applied)

    def scale_to_dimension(self, target_mm: float, axis: str = "max") -> tuple[Mesh, float]:
        """Scale uniformly so one dimension becomes ``target_mm``.

        ``axis`` is x/y/z, ``max`` (longest side - the usual "make it this big"),
        or ``min``. Returns the new mesh and the factor applied, because the
        factor is what a caller needs in order to explain the change.
        """
        if target_mm <= 0:
            raise MeshError("target dimension must be positive")
        dims = self.dimensions
        current = {
            "x": dims[0], "y": dims[1], "z": dims[2],
            "max": float(dims.max()), "min": float(dims.min()),
        }.get(axis.lower())
        if current is None:
            raise MeshError(f"axis must be x, y, z, max or min (got {axis!r})")
        if current <= 0:
            raise MeshError(f"model is flat along {axis}; cannot scale to a target there")
        factor = target_mm / float(current)
        return self.scaled(factor), factor

    def scale_to_fit(
        self, volume_mm: tuple[float, float, float], margin_mm: float = 0.0
    ) -> tuple[Mesh, float]:
        """Shrink (never enlarge) so the model fits a build volume."""
        usable = np.asarray(volume_mm, dtype=np.float64) - 2 * margin_mm
        if (usable <= 0).any():
            raise MeshError("margin leaves no usable build volume")
        dims = self.dimensions
        factor = float(min(1.0, (usable / np.where(dims > 0, dims, np.inf)).min()))
        return (self if factor == 1.0 else self.scaled(factor)), factor

    def centered_on_bed(self, volume_mm: tuple[float, float, float]) -> Mesh:
        """Centre in XY and drop onto Z=0, the way a slicer would place it."""
        low, _high = self.bounds
        offset = np.array([
            volume_mm[0] / 2 - self.center[0],
            volume_mm[1] / 2 - self.center[1],
            -low[2],
        ])
        return Mesh(self.vertices + offset, self.faces.copy(), self.source, self.unit_scale_applied)

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict:
        low, high = self.bounds
        dims = self.dimensions
        return {
            "source": self.source,
            "triangles": self.triangle_count,
            "vertices": self.vertex_count,
            "dimensions_mm": {"x": round(float(dims[0]), 3), "y": round(float(dims[1]), 3),
                              "z": round(float(dims[2]), 3)},
            "bounds_mm": {"min": [round(float(v), 3) for v in low],
                          "max": [round(float(v), 3) for v in high]},
            "longest_side_mm": round(float(dims.max()), 3),
            "volume_mm3": round(self.volume_mm3, 3),
            "surface_area_mm2": round(self.surface_area_mm2, 3),
            "watertight": self.is_watertight(),
            "unit_scale_applied": self.unit_scale_applied,
        }

    def estimated_mass_g(self, infill_fraction: float = 0.15, material: str = "PLA",
                         wall_fraction: float = 0.35) -> float:
        """Rough mass: solid shell fraction plus infill through the remainder.

        Deliberately approximate - a slicer's estimate is authoritative. This
        exists so a scale decision can be sanity-checked before slicing.
        """
        from ..specs import FILAMENT_DENSITY

        density = FILAMENT_DENSITY.get(material.upper(), 1.24)
        solid_fraction = wall_fraction + (1 - wall_fraction) * max(0.0, min(1.0, infill_fraction))
        return (self.volume_mm3 / 1000.0) * solid_fraction * density


# -- loading ---------------------------------------------------------------
def load_mesh(path: str | Path) -> Mesh:
    """Load an STL (binary or ASCII), OBJ or 3MF file."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise MeshError(f"{file} does not exist")
    suffix = file.suffix.lower()
    if suffix == ".stl":
        mesh = _load_stl(file)
    elif suffix == ".obj":
        mesh = _load_obj(file)
    elif suffix == ".3mf":
        mesh = _load_3mf(file)
    else:
        raise MeshError(
            f"unsupported file type {suffix!r}",
            hint=f"Supported: {', '.join(SUPPORTED_SUFFIXES)}",
        )
    return mesh


def _load_stl(file: Path) -> Mesh:
    data = file.read_bytes()
    if len(data) < 84:
        raise MeshError(f"{file.name} is too short to be an STL")
    # An ASCII STL starts with "solid", but so do some binary ones: trust the
    # declared triangle count against the real file size instead.
    count = struct.unpack("<I", data[80:84])[0]
    if len(data) == 84 + count * 50:
        return _load_binary_stl(data, count, file)
    if data[:5].lower() == b"solid":
        return _load_ascii_stl(data.decode("utf-8", errors="replace"), file)
    raise MeshError(f"{file.name} is not a valid STL (size does not match {count} triangles)")


def _load_binary_stl(data: bytes, count: int, file: Path) -> Mesh:
    record = np.dtype([("normal", "<3f4"), ("corners", "<3,3f4"), ("attr", "<u2")])
    records = np.frombuffer(data, dtype=record, count=count, offset=84)
    vertices = records["corners"].reshape(-1, 3).astype(np.float64)
    faces = np.arange(len(vertices), dtype=np.int64).reshape(-1, 3)
    return Mesh(vertices, faces, source=str(file)).welded()


def _load_ascii_stl(text: str, file: Path) -> Mesh:
    values = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    if not values:
        raise MeshError(f"{file.name}: no vertices found")
    if len(values) % 3:
        raise MeshError(f"{file.name}: vertex count {len(values)} is not a multiple of 3")
    vertices = np.array(values, dtype=np.float64)
    faces = np.arange(len(vertices), dtype=np.int64).reshape(-1, 3)
    return Mesh(vertices, faces, source=str(file)).welded()


def _load_obj(file: Path) -> Mesh:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for line in file.read_text(errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif parts[0] == "f" and len(parts) >= 4:
            # "f v", "f v/vt", "f v//vn"; OBJ indices are 1-based and may be negative.
            idx = [int(p.split("/")[0]) for p in parts[1:]]
            idx = [i - 1 if i > 0 else len(vertices) + i for i in idx]
            for k in range(1, len(idx) - 1):  # fan-triangulate polygons
                faces.append((idx[0], idx[k], idx[k + 1]))
    if not faces:
        raise MeshError(f"{file.name}: no faces found")
    return Mesh(np.array(vertices), np.array(faces), source=str(file))


def _load_3mf(file: Path) -> Mesh:
    """Read the 3MF core model part, merging every object the build references."""
    try:
        archive = zipfile.ZipFile(file)
    except zipfile.BadZipFile as exc:
        raise MeshError(f"{file.name} is not a valid 3MF archive") from exc

    with archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".model")]
        if not names:
            raise MeshError(f"{file.name} contains no 3D model part")
        # The core part is conventionally 3D/3dmodel.model.
        primary = next((n for n in names if n.lower().endswith("3d/3dmodel.model")), names[0])
        root = ET.fromstring(archive.read(primary))

    namespace = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    tag = (lambda name: f"{{{namespace}}}{name}") if namespace else (lambda name: name)
    unit_scale = _UNIT_TO_MM.get((root.get("unit") or "millimeter").lower(), 1.0)

    objects: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for obj in root.iter(tag("object")):
        mesh_el = obj.find(tag("mesh"))
        if mesh_el is None:
            continue
        verts = np.array(
            [[float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))]
             for v in mesh_el.iter(tag("vertex"))],
            dtype=np.float64,
        )
        tris = np.array(
            [[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
             for t in mesh_el.iter(tag("triangle"))],
            dtype=np.int64,
        )
        if len(verts) and len(tris):
            objects[obj.get("id", "")] = (verts, tris)

    if not objects:
        raise MeshError(f"{file.name}: no mesh geometry in the 3MF model part")

    # Honour <build><item> transforms when present, so a project 3MF lands where
    # the slicer put it; otherwise merge the raw objects.
    placements: list[tuple[np.ndarray, np.ndarray]] = []
    for item in root.iter(tag("item")):
        geometry = objects.get(item.get("objectid", ""))
        if geometry is None:
            continue
        placements.append((_apply_3mf_transform(geometry[0], item.get("transform")), geometry[1]))
    if not placements:
        placements = list(objects.values())

    vertices_parts: list[np.ndarray] = []
    faces_parts: list[np.ndarray] = []
    offset = 0
    for verts, tris in placements:
        vertices_parts.append(verts)
        faces_parts.append(tris + offset)
        offset += len(verts)

    mesh = Mesh(
        np.vstack(vertices_parts) * unit_scale,
        np.vstack(faces_parts),
        source=str(file),
        unit_scale_applied=unit_scale,
    )
    return mesh


def _apply_3mf_transform(vertices: np.ndarray, transform: str | None) -> np.ndarray:
    """3MF transforms are a row-major 4x3 matrix in a space-separated string."""
    if not transform:
        return vertices
    values = [float(v) for v in transform.split()]
    if len(values) != 12:
        return vertices
    matrix = np.array(values, dtype=np.float64).reshape(4, 3)
    return vertices @ matrix[:3] + matrix[3]


# -- saving ----------------------------------------------------------------
def save_stl(mesh: Mesh, path: str | Path) -> Path:
    """Write a binary STL - the format every slicer accepts."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    tris = mesh.triangles.astype(np.float32)
    normals = mesh.face_normals.astype(np.float32)
    record = np.zeros(
        len(tris), dtype=np.dtype([("normal", "<3f4"), ("corners", "<3,3f4"), ("attr", "<u2")])
    )
    record["normal"] = normals
    record["corners"] = tris
    header = f"mhs {Path(mesh.source).name if mesh.source else 'mesh'}".encode()[:79]
    with target.open("wb") as fh:
        fh.write(header.ljust(80, b"\0"))
        fh.write(struct.pack("<I", len(tris)))
        fh.write(record.tobytes())
    return target


# -- primitives, for tests and for sanity-checking a setup -----------------
def unit_cube(size: float = 10.0) -> Mesh:
    """An axis-aligned cube with a corner at the origin."""
    v = np.array([
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
    ], dtype=np.float64) * size
    f = np.array([
        [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ], dtype=np.int64)
    return Mesh(v, f, source="cube")


def uv_sphere(radius: float = 10.0, segments: int = 24, rings: int = 16) -> Mesh:
    """A closed UV sphere: useful for exercising curvature and overhangs."""
    vertices = [[0.0, 0.0, radius]]
    for i in range(1, rings):
        phi = math.pi * i / rings
        for j in range(segments):
            theta = 2 * math.pi * j / segments
            vertices.append([
                radius * math.sin(phi) * math.cos(theta),
                radius * math.sin(phi) * math.sin(theta),
                radius * math.cos(phi),
            ])
    vertices.append([0.0, 0.0, -radius])
    bottom = len(vertices) - 1

    faces = []
    for j in range(segments):
        faces.append([0, 1 + j, 1 + (j + 1) % segments])
    for i in range(rings - 2):
        row, nxt = 1 + i * segments, 1 + (i + 1) * segments
        for j in range(segments):
            a, b = row + j, row + (j + 1) % segments
            c, d = nxt + j, nxt + (j + 1) % segments
            faces.extend([[a, c, d], [a, d, b]])
    last = 1 + (rings - 2) * segments
    for j in range(segments):
        faces.append([bottom, last + (j + 1) % segments, last + j])
    return Mesh(np.array(vertices), np.array(faces), source="sphere")
