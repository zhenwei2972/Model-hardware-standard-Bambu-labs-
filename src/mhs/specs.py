"""Hardware specifications, and the printable-detail limits derived from them.

This is the reference data behind "can this printer actually reproduce that
feature?". Values are the manufacturer's published figures plus the practical
rules of thumb that FDM printing imposes on top of them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PrinterSpec:
    """What a printer can physically do."""

    model: str
    build_volume_mm: tuple[float, float, float]
    nozzle_options_mm: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)
    default_nozzle_mm: float = 0.4
    layer_height_range_mm: tuple[float, float] = (0.08, 0.28)
    default_layer_height_mm: float = 0.2
    max_nozzle_temp_c: float = 300.0
    max_bed_temp_c: float = 100.0
    max_speed_mm_s: float = 500.0
    enclosed: bool = False
    heated_chamber: bool = False
    ams_slots: int = 0
    camera_resolution: tuple[int, int] | None = None
    #: Repeatable positioning accuracy; not the same as printable feature size.
    xy_positioning_mm: float = 0.05
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("build_volume_mm", "nozzle_options_mm", "layer_height_range_mm",
                    "camera_resolution", "notes"):
            value = data.get(key)
            data[key] = list(value) if value is not None else None
        return data


@dataclass(frozen=True)
class DetailLimits:
    """The smallest features that survive, for one nozzle + layer height.

    FDM detail is anisotropic: XY is governed by the extrusion width, Z by the
    layer height. A model can look fine in a slicer preview and still lose every
    feature below these numbers.
    """

    nozzle_mm: float
    layer_height_mm: float

    @property
    def min_xy_feature_mm(self) -> float:
        """Nothing narrower than one extrusion can be drawn at all."""
        return self.nozzle_mm

    @property
    def min_wall_mm(self) -> float:
        """A wall that should survive handling: two perimeters."""
        return self.nozzle_mm * 2

    @property
    def min_embossed_detail_mm(self) -> float:
        """Raised/engraved text and surface relief need ~1.5 extrusions to read."""
        return self.nozzle_mm * 1.5

    @property
    def min_vertical_feature_mm(self) -> float:
        """Anything shorter than one layer is quantised away."""
        return self.layer_height_mm

    @property
    def min_hole_diameter_mm(self) -> float:
        """Holes print undersize; below this they usually close up."""
        return max(2.0, self.nozzle_mm * 4)

    @property
    def min_pin_diameter_mm(self) -> float:
        """Thin posts snap or wobble during printing."""
        return max(1.5, self.nozzle_mm * 3)

    @property
    def max_overhang_degrees(self) -> float:
        """Beyond this from vertical, an unsupported wall droops."""
        return 45.0

    def to_dict(self) -> dict:
        return {
            "nozzle_mm": self.nozzle_mm,
            "layer_height_mm": self.layer_height_mm,
            "min_xy_feature_mm": self.min_xy_feature_mm,
            "min_wall_mm": self.min_wall_mm,
            "min_embossed_detail_mm": self.min_embossed_detail_mm,
            "min_vertical_feature_mm": self.min_vertical_feature_mm,
            "min_hole_diameter_mm": self.min_hole_diameter_mm,
            "min_pin_diameter_mm": self.min_pin_diameter_mm,
            "max_overhang_degrees": self.max_overhang_degrees,
        }


#: Density in g/cm3, for mass estimates.
FILAMENT_DENSITY = {"PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PA": 1.15}

A1_MINI = PrinterSpec(
    model="A1 mini",
    build_volume_mm=(180.0, 180.0, 180.0),
    default_nozzle_mm=0.4,
    layer_height_range_mm=(0.08, 0.28),
    max_nozzle_temp_c=300.0,
    max_bed_temp_c=80.0,
    max_speed_mm_s=500.0,
    enclosed=False,
    ams_slots=4,
    camera_resolution=(1280, 720),
    notes=(
        "Open frame: no chamber heating, so ABS/ASA warp badly. PLA/PETG are the safe choices.",
        "AMS lite is external; multi-colour prints purge a lot of filament.",
        "The camera is a fixed low-rate (~1 fps) chamber cam, not a metrology device.",
    ),
)

SPECS: dict[str, PrinterSpec] = {
    "a1 mini": A1_MINI,
    "a1": PrinterSpec(
        model="A1",
        build_volume_mm=(256.0, 256.0, 256.0),
        max_bed_temp_c=100.0,
        ams_slots=4,
        camera_resolution=(1280, 720),
        notes=("Open frame: no chamber heating.",),
    ),
    "p1p": PrinterSpec(
        model="P1P", build_volume_mm=(256.0, 256.0, 256.0), max_bed_temp_c=100.0,
        ams_slots=4, camera_resolution=(1280, 720),
    ),
    "p1s": PrinterSpec(
        model="P1S", build_volume_mm=(256.0, 256.0, 256.0), max_bed_temp_c=100.0,
        enclosed=True, ams_slots=4, camera_resolution=(1280, 720),
    ),
    "x1c": PrinterSpec(
        model="X1 Carbon", build_volume_mm=(256.0, 256.0, 256.0), max_nozzle_temp_c=300.0,
        max_bed_temp_c=110.0, enclosed=True, ams_slots=4, camera_resolution=(1920, 1080),
        notes=("Has a lidar for first-layer inspection and flow calibration.",),
    ),
}

#: Used when a printer model is unknown: conservative, and clearly labelled.
GENERIC = PrinterSpec(model="generic FDM", build_volume_mm=(200.0, 200.0, 200.0))


def spec_for(model: str | None) -> PrinterSpec:
    """Look up a spec by model name, case- and spacing-insensitively."""
    if not model:
        return GENERIC
    key = " ".join(model.lower().replace("-", " ").split())
    if key in SPECS:
        return SPECS[key]
    for name, spec in SPECS.items():  # "Bambu Lab A1 mini" -> "a1 mini"
        if key.endswith(name) or name in key:
            return spec
    return GENERIC


def limits_for(
    spec: PrinterSpec, nozzle_mm: float | None = None, layer_height_mm: float | None = None
) -> DetailLimits:
    return DetailLimits(
        nozzle_mm=nozzle_mm or spec.default_nozzle_mm,
        layer_height_mm=layer_height_mm or spec.default_layer_height_mm,
    )
