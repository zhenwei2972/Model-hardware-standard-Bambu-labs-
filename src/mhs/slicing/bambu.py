"""Bambu Studio's command line, driven so the output actually prints.

Unlike PrusaSlicer, Bambu Studio's CLI does not take ``--layer-height`` style
overrides - it rejects them outright. Settings go in through profile JSON files
passed to ``--load-settings`` / ``--load-filaments``. Getting those files right
took four failed prints on an A1 mini, and each rule here is one of them:

* **Profiles are flattened.** The CLI does not follow a profile's ``inherits``
  chain, so a named system profile silently slices at built-in defaults
  (0.2 mm layers when 0.08 was asked for).
* **The machine's own G-code is loaded.** The A1 mini's start, end and
  filament-change macros live in ``<machine> template <key>.json`` files that
  nothing inherits from. Without them the generic 2023 AMS macros are used -
  and the filament-change macro is what drives the A1's cutter.
* **The plate type is forced.** The CLI defaults to the Cool Plate and emits
  ``M190 S35``. PLA does not stick to a textured PEI plate at 35 C; the print
  runs to completion over a part that came loose.
* **printer_model_id is written afterwards.** The CLI leaves it blank and the
  firmware refuses the file, reporting it as HMS 0500_C010, an SD card error.

Nothing leaves this module until :func:`verify` passes on the sliced file.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..errors import CommandRejected, ConfigError, MHSError
from .parameters import SliceSettings

log = logging.getLogger(__name__)

DEFAULT_PROFILE_ROOT = Path("/Applications/BambuStudio.app/Contents/Resources/profiles/BBL")


@dataclass(frozen=True)
class BambuMachine:
    """Which system profiles a printer model slices with, and what its firmware expects."""

    machine: str          # machine preset name
    process: str          # default process preset name
    filament: str         # default filament preset name
    model_id: str         # printer_model_id the firmware checks
    default_plate: str    # plate the machine ships with


#: Keyed like ``specs.spec_for``: lower-case model names.
MACHINES: dict[str, BambuMachine] = {
    "a1 mini": BambuMachine(
        machine="Bambu Lab A1 mini 0.4 nozzle",
        process="0.20mm Standard @BBL A1M",
        filament="Generic PLA @BBL A1M",
        model_id="N1",
        default_plate="Textured PEI Plate",
    ),
}

#: Neutral settings -> keys in Bambu's process JSON.
PROCESS_KEYS: dict[str, str] = {
    "layer_height_mm": "layer_height",
    "first_layer_height_mm": "initial_layer_print_height",
    "infill_percent": "sparse_infill_density",
    "wall_loops": "wall_loops",
    "top_layers": "top_shell_layers",
    "bottom_layers": "bottom_shell_layers",
    "supports": "enable_support",
    "brim_width_mm": "brim_width",
    "perimeter_speed_mm_s": "outer_wall_speed",
    "infill_speed_mm_s": "sparse_infill_speed",
}

#: Lowest first-layer bed temperature that holds each material down.
MIN_BED_C = {"PLA": 50.0, "PETG": 60.0, "ABS": 90.0, "ASA": 90.0, "TPU": 35.0}


class SliceRejected(MHSError):
    """The slicer produced a file, but it would not print as asked."""


def machine_for(model: str | None) -> BambuMachine:
    """The Bambu profiles for a printer model, matched like ``spec_for``."""
    key = " ".join((model or "").lower().replace("-", " ").split())
    for name, machine in MACHINES.items():
        if key == name or key.endswith(name):
            return machine
    raise ConfigError(
        f"no Bambu Studio profile mapping for printer model {model!r}",
        hint="Add it to mhs.slicing.bambu.MACHINES with its machine, process and "
             "filament preset names and the firmware's printer_model_id.",
    )


def profile_root(env: dict | None = None) -> Path:
    env = dict(os.environ if env is None else env)
    root = Path(env.get("MHS_BAMBU_PROFILES", DEFAULT_PROFILE_ROOT)).expanduser()
    if not root.is_dir():
        raise ConfigError(
            f"Bambu Studio system profiles not found at {root}",
            hint="Install Bambu Studio, or set MHS_BAMBU_PROFILES to its profiles/BBL folder.",
        )
    return root


def index_profiles(root: Path) -> dict[tuple[str, str], dict]:
    found: dict[tuple[str, str], dict] = {}
    for kind in ("machine", "process", "filament"):
        for path in glob.glob(str(root / kind / "**" / "*.json"), recursive=True):
            try:
                data = json.loads(Path(path).read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if "name" in data:
                found[(kind, data["name"])] = data
    return found


def flatten(index: dict, kind: str, name: str) -> dict:
    """A profile merged with everything it inherits from - the CLI will not do this."""
    try:
        data = index[(kind, name)]
    except KeyError:
        raise ConfigError(f"no {kind} profile named {name!r} in the Bambu Studio install") from None
    base = flatten(index, kind, data["inherits"]) if data.get("inherits") else {}
    merged = {**base, **data}
    merged.pop("inherits", None)
    return merged


def machine_gcode(root: Path, machine: str) -> dict[str, str]:
    """The machine's own start / end / filament-change / time-lapse macros."""
    macros: dict[str, str] = {}
    for path in root.glob(f"machine/{machine} template *.json"):
        data = json.loads(path.read_text())
        macros.update({k: v for k, v in data.items() if k.endswith("gcode")})
    if not macros:
        raise ConfigError(f"no G-code templates found for {machine!r}",
                          hint="The Bambu Studio install looks incomplete.")
    return macros


def _set(profile: dict, key: str, value) -> None:
    """Write a value the way the profile already stores that key (list or scalar)."""
    if isinstance(value, bool):
        text = "1" if value else "0"
    elif key == "sparse_infill_density":
        text = f"{value:g}%"
    elif isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    profile[key] = [text] if isinstance(profile.get(key), list) else text


def write_profiles(work: Path, root: Path, target: BambuMachine, plate: str,
                   settings: SliceSettings) -> tuple[Path, Path, Path]:
    index = index_profiles(root)

    machine = flatten(index, "machine", target.machine)
    machine.update(machine_gcode(root, target.machine))
    machine["from"] = "system"
    machine["curr_bed_type"] = plate

    process = flatten(index, "process", target.process)
    process["from"] = "system"
    process["compatible_printers"] = [target.machine]
    process["compatible_printers_condition"] = ""
    process["curr_bed_type"] = plate
    for name, value in settings.to_dict().items():
        key = PROCESS_KEYS.get(name)
        if key is None:
            raise CommandRejected(f"bambustudio has no mapping for {name!r}",
                                  hint="Supported: " + ", ".join(sorted(PROCESS_KEYS)))
        _set(process, key, value)

    filament = flatten(index, "filament", target.filament)
    filament["from"] = "system"
    filament["compatible_printers"] = [target.machine]
    filament["compatible_printers_condition"] = ""

    paths = (work / "machine.json", work / "process.json", work / "filament.json")
    for path, data in zip(paths, (machine, process, filament)):
        path.write_text(json.dumps(data, indent=1))
    return paths


def set_model_id(path: Path, model_id: str) -> None:
    """Write the printer_model_id the CLI leaves blank, leaving all else alone."""
    staged = path.with_suffix(".staged")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "Metadata/slice_info.config":
                data = re.sub(rb'key="printer_model_id" value="[^"]*"',
                              f'key="printer_model_id" value="{model_id}"'.encode(), data)
            dst.writestr(item, data)
    shutil.move(staged, path)


def verify(path: Path, *, model_id: str, plate: str, machine_name: str,
           layer_height: float | None = None) -> list[str]:
    """Every check here is a mistake that already reached a printer once."""
    problems: list[str] = []
    with zipfile.ZipFile(path) as archive:
        gcode_name = next((n for n in archive.namelist() if n.endswith(".gcode")), None)
        if gcode_name is None:
            return ["the sliced file contains no G-code"]
        head = "\n".join(archive.read(gcode_name).decode(errors="replace").splitlines()[:4000])
        info = archive.read("Metadata/slice_info.config").decode(errors="replace")

    def header(key: str) -> str | None:
        match = re.search(rf"^; {re.escape(key)} = (.*)$", head, re.M)
        return match.group(1).strip() if match else None

    if f'key="printer_model_id" value="{model_id}"' not in info:
        problems.append(f"printer_model_id is not {model_id}; the firmware will refuse the file")

    if header("curr_bed_type") != plate:
        problems.append(f"sliced for the {header('curr_bed_type')}, not the {plate}")

    bed_values = re.findall(r"^M190\s+S([0-9.]+)", head, re.M)
    bed_c = max(float(v) for v in bed_values) if bed_values else None
    filament = (header("filament_type") or "").split(";")[0].strip('"').upper()
    needed = MIN_BED_C.get(filament)
    if bed_c is None:
        problems.append("no bed temperature command found")
    elif needed and bed_c + 10 < needed:
        problems.append(f"bed heats to {bed_c:g} C, too cold for {filament} (needs about {needed:g} C)")

    sliced_layer = header("layer_height")
    if layer_height and sliced_layer and abs(float(sliced_layer) - layer_height) > 1e-6:
        problems.append(f"sliced at {sliced_layer} mm layers, not the {layer_height} mm asked for")

    # The machine's own macros must be in there; the generic ones drive an AMS
    # cutter differently and the print dies during the filament load.
    short = machine_name.replace("Bambu Lab ", "").replace(" 0.4 nozzle", "")
    if short not in (header("machine_start_gcode") or ""):
        problems.append(f"machine_start_gcode is not the {short}'s - the generic macro is in there")
    return problems


def slice_bambu(binary: Path, model: Path, output: Path, settings: SliceSettings, *,
                printer_model: str | None, plate: str = "", timeout_s: float = 600.0,
                root: Path | None = None) -> list[str]:
    """Slice with flattened, machine-correct profiles; return the command used.

    Raises :class:`SliceRejected` - and leaves ``output`` unwritten - when the
    sliced file fails :func:`verify`.
    """
    target = machine_for(printer_model)
    plate = plate or target.default_plate
    root = root or profile_root()
    work = Path(tempfile.mkdtemp(prefix="mhs-bambu-"))
    machine_json, process_json, filament_json = write_profiles(work, root, target, plate, settings)

    staged_model = work / model.name
    shutil.copy(model, staged_model)
    name = output.name if output.name.endswith(".3mf") else output.name + ".3mf"
    argv = [str(binary), "--arrange", "1", "--orient", "0", "--slice", "0",
            "--outputdir", str(work),
            "--load-settings", f"{machine_json};{process_json}",
            "--load-filaments", str(filament_json),
            "--export-3mf", name, str(staged_model)]
    log.info("slicing: %s", " ".join(argv))
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                                   check=False, cwd=work)
    except subprocess.TimeoutExpired as exc:
        raise MHSError(f"Bambu Studio did not finish within {timeout_s:.0f}s") from exc
    staged = work / name
    if completed.returncode != 0 or not staged.exists():
        tail = (completed.stdout or "")[-600:] + (completed.stderr or "")[-300:]
        raise MHSError(f"Bambu Studio refused to slice (exit {completed.returncode})",
                       hint=tail.strip().splitlines()[-1] if tail.strip() else None)

    set_model_id(staged, target.model_id)
    problems = verify(staged, model_id=target.model_id, plate=plate, machine_name=target.machine,
                      layer_height=settings.layer_height_mm)
    if problems:
        raise SliceRejected(
            "the sliced file would not print as asked: " + "; ".join(problems),
            hint=f"The file was kept for inspection at {staged}.",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(staged, output)
    return argv
