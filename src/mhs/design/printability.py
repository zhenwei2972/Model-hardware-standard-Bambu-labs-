"""Judging a model against what a specific printer can physically reproduce.

The question this answers is the one that matters before committing 40 minutes
of filament: *at this size, on this machine, which of my details actually
survive?* FDM resolution is anisotropic and bounded by the extrusion width in
XY and the layer height in Z, so a model can look perfect on screen and come
out as a smooth blob.

Honest about its limits: feature size is estimated from mesh edge lengths and
the volume-to-area ratio, not from a medial-axis or voxel analysis. It reliably
catches "this detail is far too fine" and "this wall is too thin"; it will not
find every thin region in a complex part. The slicer's preview remains the
authority, and the camera closes the loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..specs import DetailLimits, PrinterSpec, limits_for
from .mesh import Mesh
from .render import overhanging_faces

#: 1.75 mm filament cross-section, for length estimates.
_FILAMENT_AREA_MM2 = math.pi * (1.75 / 2) ** 2

SEVERITIES = ("blocker", "warning", "note")


@dataclass
class Finding:
    """One thing worth knowing before printing."""

    severity: str
    code: str
    message: str
    suggestion: str | None = None

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "suggestion": self.suggestion}


@dataclass
class PrintabilityReport:
    printer: str
    limits: DetailLimits
    dimensions_mm: tuple[float, float, float]
    findings: list[Finding] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocker"]

    @property
    def printable(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "printer": self.printer,
            "printable": self.printable,
            "verdict": self.verdict(),
            "limits": self.limits.to_dict(),
            "dimensions_mm": {
                axis: round(v, 3) for axis, v in zip("xyz", self.dimensions_mm, strict=True)
            },
            "findings": [f.to_dict() for f in self.findings],
            "metrics": self.metrics,
        }

    def verdict(self) -> str:
        """A sentence stating what this machine can and cannot resolve here."""
        lost = self.metrics.get("detail_below_nozzle_percent", 0.0)
        head = (
            f"At a {self.limits.nozzle_mm:g} mm nozzle and {self.limits.layer_height_mm:g} mm layers, "
            f"{self.printer} resolves about {self.limits.min_xy_feature_mm:g} mm in XY and "
            f"{self.limits.min_vertical_feature_mm:g} mm in Z."
        )
        if lost >= 25:
            body = (f" {lost:.0f}% of this model's detail is finer than that and will merge or "
                    "disappear - scale up or simplify.")
        elif lost >= 5:
            body = f" About {lost:.0f}% of its detail is below that threshold and will soften."
        else:
            body = " Essentially all of its modelled detail is reproducible at this size."
        counts = {s: len([f for f in self.findings if f.severity == s]) for s in SEVERITIES}
        tail = (f" {counts['blocker']} blocker(s), {counts['warning']} warning(s)."
                if counts["blocker"] or counts["warning"] else " No blocking issues.")
        return head + body + tail


def analyze(
    mesh: Mesh,
    spec: PrinterSpec,
    *,
    nozzle_mm: float | None = None,
    layer_height_mm: float | None = None,
    material: str = "PLA",
    infill_fraction: float = 0.15,
) -> PrintabilityReport:
    """Check a mesh against a printer's specification and physics."""
    limits = limits_for(spec, nozzle_mm, layer_height_mm)
    dims = mesh.dimensions
    report = PrintabilityReport(
        printer=spec.model, limits=limits, dimensions_mm=(float(dims[0]), float(dims[1]), float(dims[2]))
    )
    metrics = report.metrics
    findings = report.findings

    # -- does it fit ------------------------------------------------------
    volume = np.asarray(spec.build_volume_mm, dtype=np.float64)
    fits_as_is = bool((dims <= volume).all())
    fits_rotated = bool((np.sort(dims)[::-1] <= np.sort(volume)[::-1]).all())
    metrics["fits_build_volume"] = fits_as_is
    metrics["build_volume_usage_percent"] = round(float((dims / volume).max() * 100), 1)
    if not fits_as_is:
        oversize = ", ".join(
            f"{axis}: {d:.1f} > {v:.0f} mm"
            for axis, d, v in zip("XYZ", dims, volume, strict=True)
            if d > v
        )
        findings.append(Finding(
            "blocker", "too_large",
            f"Larger than the {spec.model} build volume ({oversize}).",
            "Rotate it onto another axis, scale it down, or split it into parts."
            if fits_rotated else
            f"Scale to at most {float((volume / dims).min()):.2f}x, or split it into parts.",
        ))

    # -- detail vs extrusion width ---------------------------------------
    edges = mesh.edge_lengths
    edges = edges[edges > 1e-9]
    if len(edges):
        below = float((edges < limits.min_xy_feature_mm).mean() * 100)
        metrics["detail_below_nozzle_percent"] = round(below, 1)
        metrics["edge_length_mm"] = {
            "p01": round(float(np.percentile(edges, 1)), 4),
            "p05": round(float(np.percentile(edges, 5)), 4),
            "median": round(float(np.percentile(edges, 50)), 4),
        }
        if below >= 25:
            needed = limits.min_xy_feature_mm / max(float(np.percentile(edges, 5)), 1e-6)
            findings.append(Finding(
                "warning", "detail_too_fine",
                f"{below:.0f}% of the mesh's edges are shorter than the {limits.nozzle_mm:g} mm "
                "extrusion width, so that detail cannot be drawn.",
                f"Scale up by about {needed:.1f}x, fit a smaller nozzle, or remove the fine "
                "detail so it does not mislead you.",
            ))
        elif below >= 5:
            findings.append(Finding(
                "note", "detail_partially_fine",
                f"{below:.0f}% of the mesh's edges are below the extrusion width; the finest "
                "surface relief will soften.",
            ))

    # -- characteristic thickness ----------------------------------------
    area = mesh.surface_area_mm2
    if area > 0:
        # For a plate of thickness t, volume/area tends to t/2; a serviceable
        # global proxy for "is this thing thin overall".
        thickness = 2 * mesh.volume_mm3 / area
        metrics["characteristic_thickness_mm"] = round(float(thickness), 3)
        if thickness < limits.min_wall_mm:
            findings.append(Finding(
                "warning", "thin_walls",
                f"Average wall thickness is around {thickness:.2f} mm, below the "
                f"{limits.min_wall_mm:g} mm needed for two perimeters.",
                "Thicken the walls, or expect a fragile single-wall print.",
            ))

    smallest = float(dims.min())
    metrics["smallest_dimension_mm"] = round(smallest, 3)
    if smallest < limits.min_wall_mm:
        findings.append(Finding(
            "blocker", "below_minimum_feature",
            f"The smallest overall dimension is {smallest:.2f} mm, thinner than a "
            f"{limits.min_wall_mm:g} mm two-perimeter wall.",
            "Scale up, or redesign that axis to be thicker.",
        ))

    # -- Z resolution -----------------------------------------------------
    height = float(dims[2])
    if height > 0:
        layers = height / limits.layer_height_mm
        metrics["layer_count"] = int(round(layers))
        remainder = abs(layers - round(layers)) * limits.layer_height_mm
        if remainder > limits.layer_height_mm * 0.15:
            metrics["z_quantisation_error_mm"] = round(remainder, 3)
            findings.append(Finding(
                "note", "z_quantisation",
                f"Height {height:.2f} mm is not a whole number of "
                f"{limits.layer_height_mm:g} mm layers; the top will land up to "
                f"{remainder:.2f} mm off.",
                "Adjust the height or the layer height if the exact dimension matters.",
            ))
        if layers > 1500:
            findings.append(Finding(
                "note", "many_layers",
                f"{int(layers):,} layers at {limits.layer_height_mm:g} mm - this will be a long print.",
                f"A {min(spec.layer_height_range_mm[1], limits.layer_height_mm * 1.4):.2f} mm "
                "layer height would cut it substantially.",
            ))

    if not (spec.layer_height_range_mm[0] <= limits.layer_height_mm <= spec.layer_height_range_mm[1]):
        low, high = spec.layer_height_range_mm
        findings.append(Finding(
            "blocker", "layer_height_out_of_range",
            f"{limits.layer_height_mm:g} mm layers are outside the {spec.model}'s "
            f"{low:g}-{high:g} mm range for this nozzle.",
        ))
    if limits.nozzle_mm not in spec.nozzle_options_mm:
        findings.append(Finding(
            "note", "unusual_nozzle",
            f"A {limits.nozzle_mm:g} mm nozzle is not one of the {spec.model}'s standard sizes "
            f"({', '.join(f'{n:g}' for n in spec.nozzle_options_mm)}).",
        ))

    # -- supports and bed adhesion ---------------------------------------
    overhangs = overhanging_faces(mesh, limits.max_overhang_degrees)
    overhang_area = float(mesh.face_areas[overhangs].sum())
    metrics["overhang_area_percent"] = round(overhang_area / area * 100, 1) if area else 0.0
    if metrics["overhang_area_percent"] >= 10:
        findings.append(Finding(
            "warning", "needs_supports",
            f"{metrics['overhang_area_percent']:.0f}% of the surface overhangs beyond "
            f"{limits.max_overhang_degrees:g} degrees and will need supports.",
            "Reorient the model, or enable supports when slicing.",
        ))
    elif metrics["overhang_area_percent"] >= 2:
        findings.append(Finding(
            "note", "minor_overhangs",
            f"{metrics['overhang_area_percent']:.1f}% of the surface overhangs; small areas "
            "usually bridge acceptably.",
        ))

    bed_z = mesh.vertices[:, 2].min()
    on_bed = (mesh.triangles[:, :, 2] <= bed_z + 0.3).all(axis=1)
    contact = float(mesh.face_areas[on_bed].sum())
    metrics["bed_contact_area_mm2"] = round(contact, 2)
    if contact < 100 and height > 20:
        findings.append(Finding(
            "warning", "small_bed_contact",
            f"Only {contact:.0f} mm2 touches the plate for a {height:.0f} mm tall model.",
            "Add a brim, or reorient for a flatter footprint.",
        ))

    footprint = float(min(dims[0], dims[1]))
    if footprint > 0 and height / footprint > 4:
        findings.append(Finding(
            "warning", "tall_and_thin",
            f"Aspect ratio {height / footprint:.1f}:1 - tall, narrow prints wobble and can be "
            "knocked off the plate.",
            "Print it lying down if the geometry allows, or add a brim and slow it down.",
        ))

    # -- mesh health -------------------------------------------------------
    metrics["watertight"] = mesh.is_watertight()
    if not metrics["watertight"]:
        findings.append(Finding(
            "warning", "not_watertight",
            "The mesh is not closed (some edges are not shared by exactly two faces); "
            "slicers may fill it incorrectly.",
            "Run it through a mesh repair step before slicing.",
        ))

    # -- material ----------------------------------------------------------
    mass = mesh.estimated_mass_g(infill_fraction=infill_fraction, material=material)
    metrics["estimated_mass_g"] = round(mass, 2)
    metrics["estimated_filament_m"] = round(
        (mass / 1.24) * 1000 / _FILAMENT_AREA_MM2 / 1000, 2
    )
    if not spec.enclosed and material.upper() in {"ABS", "ASA", "PC"}:
        findings.append(Finding(
            "warning", "material_needs_enclosure",
            f"{material.upper()} warps badly on the open-frame {spec.model}.",
            "Use PLA or PETG, or print it on an enclosed machine.",
        ))

    for note in spec.notes:
        findings.append(Finding("note", "printer_note", note))

    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: order.get(f.severity, 9))
    return report


def scale_advice(mesh: Mesh, spec: PrinterSpec, target_mm: float, axis: str = "max",
                 nozzle_mm: float | None = None, layer_height_mm: float | None = None) -> dict:
    """What scaling to ``target_mm`` would do to the detail, without writing a file.

    Reports before/after so the trade-off is explicit: shrinking a model to coin
    size is exactly when fine detail stops being printable.
    """
    limits = limits_for(spec, nozzle_mm, layer_height_mm)
    scaled, factor = mesh.scale_to_dimension(target_mm, axis)
    edges = mesh.edge_lengths
    edges = edges[edges > 1e-9]
    before = float((edges < limits.min_xy_feature_mm).mean() * 100) if len(edges) else 0.0
    after = float((edges * factor < limits.min_xy_feature_mm).mean() * 100) if len(edges) else 0.0
    return {
        "factor": round(factor, 4),
        "target_mm": target_mm,
        "axis": axis,
        "dimensions_before_mm": [round(float(v), 3) for v in mesh.dimensions],
        "dimensions_after_mm": [round(float(v), 3) for v in scaled.dimensions],
        "detail_below_nozzle_percent_before": round(before, 1),
        "detail_below_nozzle_percent_after": round(after, 1),
        "estimated_mass_g_after": round(scaled.estimated_mass_g(), 2),
        "note": (
            f"Scaling by {factor:.3f}x moves {after - before:+.0f} percentage points of the model's "
            f"detail across the {limits.min_xy_feature_mm:g} mm printable threshold."
        ),
    }
