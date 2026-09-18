"""The device reference file an agent consults before acting.

MHS describes drivers as auto-producing "a reference file with information
about a device's general characteristics, such as what it can measure, what can
be adjusted, and what safety limits will be enforced". That is generated here
from three sources already present in the codebase - the driver's channel
table, its declared capabilities, and the hardware specification - so the
reference can never drift from the code that enforces it.

Two renderings: JSON for programmatic discovery, Markdown for a human (or a
model) to read in one pass.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .. import __version__
from ..specs import limits_for, spec_for

if TYPE_CHECKING:  # pragma: no cover
    from ..device import Printer
    from ..models import PrinterInfo

#: Bumped when the descriptor layout changes, so a consumer can tell.
DESCRIPTOR_VERSION = "0.1"


def build_descriptor(printer: Printer, info: PrinterInfo) -> dict:
    """Assemble the machine-readable device description."""
    spec = spec_for(info.model)
    limits = limits_for(spec)
    table = printer.channel_table()

    return {
        "mhs_descriptor_version": DESCRIPTOR_VERSION,
        "generated_at": time.time(),
        "generator": f"mhs-printer/{__version__}",
        "device": {
            "id": info.printer_id,
            "kind": "fdm_3d_printer",
            "model": info.model,
            "vendor": "Bambu Lab" if info.driver == "bambu" else info.driver,
            "driver": info.driver,
            "serial": info.serial,
            "firmware": info.firmware,
            "address": info.host,
        },
        "tags": _tags(info, spec),
        "capabilities": [c.value for c in info.capabilities],
        "channels": table.to_list(),
        "physical": spec.to_dict(),
        "resolution": limits.to_dict(),
        "safety": {
            "enforced_in": "driver",
            "checks": [
                "Every write is range-checked against its channel's declared limits "
                "before it reaches the hardware.",
                "Actions that move or heat the machine require an explicit confirmation flag.",
                "Prints are refused while the printer is busy or reporting a fatal/serious alert.",
                "Scheduled jobs wait for an idle printer and expire rather than starting late.",
                "Raw gcode is restricted to a safe list unless explicitly enabled.",
            ],
            "limits": {
                channel["name"]: channel["limit"]
                for channel in table.to_list()
                if channel["limit"]
            },
        },
        "cannot": _limitations(info, spec),
    }


def _tags(info: PrinterInfo, spec) -> list[str]:
    """Natural-language tags: what this device is, in a form an agent can read."""
    tags = [
        f"{info.model} desktop FDM 3D printer",
        f"build volume {spec.build_volume_mm[0]:.0f} x {spec.build_volume_mm[1]:.0f} x "
        f"{spec.build_volume_mm[2]:.0f} mm",
        f"default nozzle {spec.default_nozzle_mm:g} mm",
        "enclosed" if spec.enclosed else "open frame, unheated chamber",
        f"hotend up to {spec.max_nozzle_temp_c:.0f} C, bed up to {spec.max_bed_temp_c:.0f} C",
        "controlled over the local network; no cloud account involved",
    ]
    if spec.ams_slots:
        tags.append(f"{spec.ams_slots}-slot automatic material system")
    if spec.camera_resolution:
        tags.append(
            f"chamber camera {spec.camera_resolution[0]}x{spec.camera_resolution[1]}, roughly 1 fps"
        )
    tags.extend(spec.notes)
    tags.append(
        "Moves fast and reaches 220 C or more: it can burn, pinch, and start a fire if "
        "left unattended with a fault."
    )
    return tags


def _limitations(info: PrinterInfo, spec) -> list[str]:
    """What the device (or this driver) cannot do - as load-bearing as what it can."""
    cannot = [
        "Slice models. It accepts sliced .3mf/.gcode only; slicing happens in Bambu Studio "
        "or OrcaSlicer.",
        "Report absolute dimensions from the camera. Measurement needs a known reference "
        "object in the frame.",
        "Resolve features below the extrusion width in XY or the layer height in Z.",
        "Recover a failed print. A stopped job cannot be resumed.",
    ]
    if not spec.enclosed:
        cannot.append("Hold chamber temperature, so ABS/ASA/PC warp.")
    if info.driver == "bambu":
        cannot.append(
            "Accept control commands while the printer is in cloud mode: LAN Only Mode plus "
            "Developer Mode must be enabled on the printer's screen."
        )
    return cannot


def descriptor_markdown(descriptor: dict) -> str:
    """Render the descriptor as a reference sheet to read before acting."""
    device = descriptor["device"]
    lines = [
        f"# {device['model']} ({device['id']})",
        "",
        f"MHS device descriptor v{descriptor['mhs_descriptor_version']} - "
        f"generated by {descriptor['generator']}",
        "",
        "## What this device is",
        "",
    ]
    lines += [f"- {tag}" for tag in descriptor["tags"]]

    physical, resolution = descriptor["physical"], descriptor["resolution"]
    volume = physical["build_volume_mm"]
    lines += [
        "",
        "## What it can print",
        "",
        f"- Build volume: {volume[0]:.0f} x {volume[1]:.0f} x {volume[2]:.0f} mm",
        f"- Smallest XY feature: {resolution['min_xy_feature_mm']:g} mm "
        f"(one {resolution['nozzle_mm']:g} mm extrusion)",
        f"- Smallest usable wall: {resolution['min_wall_mm']:g} mm (two perimeters)",
        f"- Smallest vertical step: {resolution['min_vertical_feature_mm']:g} mm (one layer)",
        f"- Smallest reliable hole: {resolution['min_hole_diameter_mm']:g} mm; "
        f"thinnest pin: {resolution['min_pin_diameter_mm']:g} mm",
        f"- Unsupported overhang limit: {resolution['max_overhang_degrees']:g} degrees",
        "",
        "## Channels",
        "",
        "| channel | access | unit | limits | description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for channel in descriptor["channels"]:
        limit = channel["limit"]
        if not limit:
            bounds = "-"
        elif limit["allowed"]:
            bounds = ", ".join(limit["allowed"])
        else:
            low = f"{limit['minimum']:g}" if limit["minimum"] is not None else "-inf"
            high = f"{limit['maximum']:g}" if limit["maximum"] is not None else "inf"
            bounds = f"{low} to {high}"
        if limit and limit["requires_confirmation"]:
            bounds += " (needs confirmation)"
        lines.append(
            f"| `{channel['name']}` | {channel['access']} | {channel['unit'] or '-'} | "
            f"{bounds} | {channel['description']} |"
        )

    lines += ["", "## Safety", "", f"Enforced in the {descriptor['safety']['enforced_in']}:", ""]
    lines += [f"- {check}" for check in descriptor["safety"]["checks"]]
    lines += ["", "## What it cannot do", ""]
    lines += [f"- {item}" for item in descriptor["cannot"]]
    lines.append("")
    return "\n".join(lines)
