"""Read a sliced file's own header before printing it.

A sliced file carries the settings it was made with, and some of those are
wrong in ways the printer cannot notice. The one that cost us a print: a file
sliced for the Cool Plate heats the bed to 35 C, which PLA will not stick to on
a textured PEI plate. The printer heats to 35 C, prints all 198 layers, and
reports success while the part is being dragged around the chamber - the
firmware has no idea the model left the plate.

Nothing here talks to a printer. It reads the file, says what it found, and
leaves the decision to the caller.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

#: Lowest first-layer bed temperature that reliably holds each material down.
#: Below this the part releases mid-print. Keyed by the filament type string
#: slicers write into the gcode header ("PLA", "PETG", "ABS", ...).
MIN_BED_C: dict[str, float] = {
    "PLA": 50.0,
    "PLA-CF": 50.0,
    "PETG": 60.0,
    "PETG-CF": 60.0,
    "PET-CF": 60.0,
    "ABS": 90.0,
    "ASA": 90.0,
    "PC": 90.0,
    "PA": 90.0,
    "PAHT-CF": 90.0,
    "PVA": 45.0,
    "TPU": 35.0,
}

#: How far below :data:`MIN_BED_C` we tolerate before calling it a blocker;
#: plates and ambient temperatures vary, a 10 C shortfall usually still sticks.
BED_TOLERANCE_C = 10.0

_HEADER_KEYS = {
    "curr_bed_type": "bed_type",
    "filament_type": "filament_type",
    "nozzle_temperature_initial_layer": "nozzle_initial_c",
    "hot_plate_temp_initial_layer": "hot_plate_c",
    "textured_plate_temp_initial_layer": "textured_plate_c",
    "cool_plate_temp_initial_layer": "cool_plate_c",
    "supertack_plate_temp_initial_layer": "supertack_plate_c",
}

_COMMENT = re.compile(r"^;\s*([a-z_0-9]+)\s*=\s*(.*?)\s*$")
_BED_COMMAND = re.compile(r"^M1(?:40|90)\s+S([0-9.]+)", re.MULTILINE)


@dataclass
class Finding:
    """One thing worth saying before the machine starts moving."""

    severity: str  # "blocker" | "warning" | "note"
    code: str
    message: str
    suggestion: str | None = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class SlicedFile:
    """What a sliced file says about itself."""

    path: str
    plate: int = 1
    bed_type: str = ""
    bed_c: float | None = None
    filaments: list[str] = field(default_factory=list)
    nozzle_c: float | None = None
    plate_temps: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "plate": self.plate,
            "bed_type": self.bed_type,
            "bed_c": self.bed_c,
            "filaments": self.filaments,
            "nozzle_c": self.nozzle_c,
            "plate_temps": self.plate_temps,
        }


def _split_list(value: str) -> list[str]:
    """Header list values arrive as ``"PLA";"PETG"`` or a bare ``PLA``."""
    parts = [p.strip().strip('"') for p in value.split(";")]
    return [p for p in parts if p]


def _as_float(value: str) -> float | None:
    try:
        return float(value.strip().strip('"').split(";")[0])
    except (TypeError, ValueError):
        return None


def _read_gcode(path: Path, plate: int) -> str:
    if path.suffix.lower() == ".gcode":
        return path.read_text(errors="replace")
    with zipfile.ZipFile(path) as archive:
        name = f"Metadata/plate_{plate}.gcode"
        if name not in archive.namelist():
            raise FileNotFoundError(f"{path.name} has no {name}")
        return archive.read(name).decode("utf-8", errors="replace")


def inspect(path: str | Path, plate: int = 1, max_header_lines: int = 4000) -> SlicedFile:
    """Read the settings a sliced ``.3mf``/``.gcode`` was produced with.

    Only the header is parsed - the settings comments sit at the top, and the
    body can be hundreds of megabytes.
    """
    path = Path(path).expanduser()
    text = _read_gcode(path, plate)
    info = SlicedFile(path=str(path), plate=plate)

    head: list[str] = []
    for index, line in enumerate(text.splitlines()):
        if index >= max_header_lines:
            break
        head.append(line)
        match = _COMMENT.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        field_name = _HEADER_KEYS.get(key)
        if field_name == "bed_type":
            info.bed_type = value.strip().strip('"')
        elif field_name == "filament_type":
            info.filaments = _split_list(value)
        elif field_name == "nozzle_initial_c":
            info.nozzle_c = _as_float(value)
        elif field_name and field_name.endswith("_plate_c"):
            temp = _as_float(value)
            if temp is not None:
                info.plate_temps[field_name[: -len("_plate_c")]] = temp

    # The bed target the machine will actually act on, not a profile value.
    command = _BED_COMMAND.search("\n".join(head))
    if command:
        info.bed_c = float(command.group(1))
    return info


def check(info: SlicedFile, plate_type: str = "") -> list[Finding]:
    """Judge a sliced file against the plate that is really on the machine.

    ``plate_type`` is the device's configured plate (e.g. "Textured PEI Plate").
    Left empty, only the filament-versus-bed-temperature check runs.
    """
    findings: list[Finding] = []

    if plate_type and info.bed_type and info.bed_type.lower() != plate_type.lower():
        findings.append(
            Finding(
                severity="warning",
                code="plate_type_mismatch",
                message=(
                    f"sliced for the {info.bed_type} but {plate_type} is on the machine"
                ),
                suggestion=(
                    f"Re-slice with the plate set to {plate_type}; the bed temperature "
                    "and first-layer settings differ per plate."
                ),
            )
        )

    if info.bed_c is None:
        findings.append(
            Finding(
                severity="note",
                code="bed_temperature_unknown",
                message="could not find the bed temperature in the file's header",
            )
        )
        return findings

    for filament in info.filaments or []:
        needed = MIN_BED_C.get(filament.upper())
        if needed is None:
            continue
        if info.bed_c + BED_TOLERANCE_C < needed:
            findings.append(
                Finding(
                    severity="blocker",
                    code="bed_too_cold",
                    message=(
                        f"bed heats to {info.bed_c:g} C, too cold for {filament} "
                        f"(needs about {needed:g} C)"
                    ),
                    suggestion=(
                        "The part releases mid-print and the toolhead drags it around; "
                        "the printer prints on regardless and reports success. Re-slice "
                        "with the right plate selected."
                    ),
                )
            )
            break

    return findings


def preflight(path: str | Path, plate: int = 1, plate_type: str = "") -> tuple[SlicedFile, list[Finding]]:
    """:func:`inspect` then :func:`check`, the pairing every caller wants."""
    info = inspect(path, plate=plate)
    return info, check(info, plate_type=plate_type)
