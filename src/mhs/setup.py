"""What a new machine needs configured before its first print.

A printer that is reachable is not a printer that is ready. The settings that
matter most are the ones nothing checks: which build plate is fitted, what
material is loaded, whether the server may write at all. Each one is invisible
until a print is already ruined - a file sliced for the wrong plate heats the
bed to 35 C, PLA never sticks, and the machine prints the whole job over a part
that came loose while reporting success.

This module answers "is this machine set up, and if not, what exactly is
missing?" It reads configuration and status; it changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .preflight import Finding
from .specs import spec_for


@dataclass(frozen=True)
class Recommended:
    """Sensible starting settings for a model, and why.

    These are defaults to slice against, not the only workable values. The
    reasons matter more than the numbers: they say which way to move when a
    print misbehaves.
    """

    plate_type: str
    material: str
    bed_c: float
    nozzle_c: float
    layer_height_mm: float
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "plate_type": self.plate_type,
            "material": self.material,
            "bed_c": self.bed_c,
            "nozzle_c": self.nozzle_c,
            "layer_height_mm": self.layer_height_mm,
            "notes": list(self.notes),
        }


#: Per model. The A1 mini is the worked example: open frame, textured plate in
#: the box, no chamber heating.
RECOMMENDED: dict[str, Recommended] = {
    "a1 mini": Recommended(
        plate_type="Textured PEI Plate",
        material="PLA",
        bed_c=65.0,
        nozzle_c=220.0,
        layer_height_mm=0.2,
        notes=(
            "The textured PEI plate ships with the machine; it needs 65 C for PLA, "
            "not the slicer's 35 C Cool Plate default.",
            "Open frame, so ABS and ASA warp. PLA and PETG are the safe choices.",
            "LAN Only Mode plus Developer Mode must be on (Settings > Network) or "
            "the printer ignores every command sent over the network.",
        ),
    ),
    "a1": Recommended(
        plate_type="Textured PEI Plate",
        material="PLA",
        bed_c=65.0,
        nozzle_c=220.0,
        layer_height_mm=0.2,
        notes=("Open frame: PLA and PETG print well, ABS and ASA warp.",),
    ),
    "p1s": Recommended(
        plate_type="Textured PEI Plate",
        material="PLA",
        bed_c=65.0,
        nozzle_c=220.0,
        layer_height_mm=0.2,
        notes=("Enclosed, so ABS and ASA are practical here; raise the bed to 90 C for them.",),
    ),
}

GENERIC = Recommended(
    plate_type="Textured PEI Plate",
    material="PLA",
    bed_c=60.0,
    nozzle_c=220.0,
    layer_height_mm=0.2,
)


def recommended_for(model: str | None) -> Recommended:
    """Defaults for a model name, matched the way :func:`spec_for` matches."""
    if not model:
        return GENERIC
    key = " ".join(model.lower().replace("-", " ").split())
    if key in RECOMMENDED:
        return RECOMMENDED[key]
    for name, rec in RECOMMENDED.items():
        if key.endswith(name) or name in key:
            return rec
    return GENERIC


@dataclass
class SetupReport:
    device_id: str
    model: str
    ready: bool
    findings: list[Finding] = field(default_factory=list)
    recommended: Recommended = GENERIC
    observed: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "model": self.model,
            "ready": self.ready,
            "findings": [f.to_dict() for f in self.findings],
            "recommended": self.recommended.to_dict(),
            "observed": self.observed,
        }


def check_config(device_id: str, config, read_only: bool) -> list[Finding]:
    """Configuration problems, found without touching the machine."""
    findings: list[Finding] = []
    rec = recommended_for(getattr(config, "model", None))

    if not getattr(config, "plate_type", ""):
        findings.append(
            Finding(
                severity="blocker",
                code="plate_type_unset",
                message="no plate_type set, so a file sliced for the wrong plate cannot be caught",
                suggestion=(
                    f'Add plate_type = "{rec.plate_type}" under [devices.{device_id}] in the '
                    "config, or name the plate that is actually fitted."
                ),
            )
        )

    if read_only:
        findings.append(
            Finding(
                severity="note",
                code="read_only",
                message="this server runs read-only: status and camera work, printing does not",
                suggestion="Unset MHS_READ_ONLY to allow prints, then restart the server.",
            )
        )
    return findings


def check_status(status, config) -> list[Finding]:
    """Problems visible in the machine's own reported state."""
    findings: list[Finding] = []
    spec = spec_for(getattr(config, "model", None))

    blocking = [a for a in getattr(status, "alerts", []) if a.severity in {"fatal", "serious"}]
    for alert in blocking:
        findings.append(
            Finding(
                severity="blocker",
                code="active_alert",
                message=f"{alert.code} is active; prints are refused while it is",
                suggestion=(
                    "Clear it on the screen. A print error that survives being dismissed "
                    "needs clean_print_error or a power cycle."
                ),
            )
        )

    if not getattr(status, "online", True):
        findings.append(
            Finding(severity="blocker", code="offline", message="the printer is not reachable")
        )

    loaded = [f for f in getattr(status, "filament", []) if not getattr(f, "empty", False)]
    if not loaded:
        findings.append(
            Finding(
                severity="warning",
                code="no_filament",
                message="no filament reported as loaded",
                suggestion="Load filament, or ignore this if you use an untracked external spool.",
            )
        )

    if spec.heated_chamber is False and getattr(config, "model", ""):
        findings.append(
            Finding(
                severity="note",
                code="open_frame",
                message="no heated chamber: ABS and ASA will warp on this machine",
            )
        )
    return findings


def build_report(device_id: str, config, status, read_only: bool) -> SetupReport:
    """Everything the setup gate knows, in one object."""
    findings = check_config(device_id, config, read_only)
    if status is not None:
        findings += check_status(status, config)

    observed: dict = {}
    if status is not None:
        observed = {
            "state": getattr(status.state, "value", str(status.state)),
            "alerts": [a.code for a in getattr(status, "alerts", [])],
            "filament": [
                {"slot": f.slot, "material": f.material} for f in getattr(status, "filament", [])
            ],
            "nozzle_c": getattr(status.nozzle, "current", None),
            "bed_c": getattr(status.bed, "current", None),
        }
    observed["plate_type"] = getattr(config, "plate_type", "") or None

    return SetupReport(
        device_id=device_id,
        model=getattr(config, "model", "") or "unknown",
        ready=not any(f.severity == "blocker" for f in findings),
        findings=findings,
        recommended=recommended_for(getattr(config, "model", None)),
        observed=observed,
    )
