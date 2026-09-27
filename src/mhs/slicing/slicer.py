"""Driving an installed slicer from the command line.

Three slicers matter for a Bambu workflow - OrcaSlicer, Bambu Studio and
PrusaSlicer - and all three descend from Slic3r, so all three take
``--setting value`` overrides on the argv. They disagree about the setting
*names*, which is the only reason this file has a mapping table.

That disagreement is safe rather than dangerous, and it is worth saying why:
an unrecognised option makes the slicer **exit non-zero with "Unknown option
--x" and write no file** (verified against PrusaSlicer 2.7). A key sent to the
wrong flavour therefore fails loudly instead of quietly printing something
wrong.

After a successful slice the gcode's own footer is read back, so what the
slicer actually applied - not what we asked for - is what gets reported.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..errors import CommandRejected, ConfigError, MHSError
from .parameters import SliceSettings

log = logging.getLogger(__name__)

#: Slicing a large model is slow; this is a ceiling, not an expectation.
DEFAULT_TIMEOUT_S = 600.0


class SlicerFlavour(str, Enum):
    ORCA = "orcaslicer"
    BAMBU = "bambustudio"
    PRUSA = "prusaslicer"


#: Executable names to look for, in preference order: the Bambu-native slicers
#: first, since their profiles match the printers this project targets.
BINARIES: dict[SlicerFlavour, tuple[str, ...]] = {
    SlicerFlavour.ORCA: ("orca-slicer", "orcaslicer", "OrcaSlicer", "Orca Slicer"),
    SlicerFlavour.BAMBU: ("bambu-studio", "bambustudio", "BambuStudio", "bambu-studio-cli"),
    SlicerFlavour.PRUSA: ("prusa-slicer", "prusaslicer", "PrusaSlicer"),
}

#: Neutral setting -> the flag each flavour calls it.
#:
#: The PrusaSlicer column is verified empirically against 2.7.2. The Orca and
#: Bambu columns follow their documented setting keys (underscores become
#: hyphens); if a version renames one, the slice fails with "Unknown option"
#: naming the flag, and the mapping can be corrected in config.
FLAG_MAP: dict[SlicerFlavour, dict[str, str]] = {
    SlicerFlavour.PRUSA: {
        "layer_height_mm": "--layer-height",
        "first_layer_height_mm": "--first-layer-height",
        "infill_percent": "--fill-density",
        "wall_loops": "--perimeters",
        "top_layers": "--top-solid-layers",
        "bottom_layers": "--bottom-solid-layers",
        "supports": "--support-material",
        "brim_width_mm": "--brim-width",
        "perimeter_speed_mm_s": "--perimeter-speed",
        "infill_speed_mm_s": "--infill-speed",
    },
    SlicerFlavour.ORCA: {
        "layer_height_mm": "--layer-height",
        "first_layer_height_mm": "--initial-layer-print-height",
        "infill_percent": "--sparse-infill-density",
        "wall_loops": "--wall-loops",
        "top_layers": "--top-shell-layers",
        "bottom_layers": "--bottom-shell-layers",
        "supports": "--enable-support",
        "brim_width_mm": "--brim-width",
        "perimeter_speed_mm_s": "--outer-wall-speed",
        "infill_speed_mm_s": "--sparse-infill-speed",
    },
}
FLAG_MAP[SlicerFlavour.BAMBU] = dict(FLAG_MAP[SlicerFlavour.ORCA])  # same lineage, same keys

#: Settings the slicer wants as a percentage string rather than a bare number.
_PERCENT_SETTINGS = {SlicerFlavour.PRUSA: {"infill_percent"}}


@dataclass
class SliceResult:
    """What came out, and what the slicer says it actually did."""

    output_path: Path
    flavour: SlicerFlavour
    requested: dict
    applied: dict = field(default_factory=dict)
    estimated_time: str | None = None
    estimated_time_minutes: int | None = None
    filament_mm: float | None = None
    filament_cm3: float | None = None
    filament_g: float | None = None
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "output_path": str(self.output_path),
            "size_bytes": self.output_path.stat().st_size if self.output_path.exists() else None,
            "slicer": self.flavour.value,
            "requested": self.requested,
            "applied": self.applied,
            "estimated_time": self.estimated_time,
            "estimated_time_minutes": self.estimated_time_minutes,
            "filament_mm": self.filament_mm,
            "filament_cm3": self.filament_cm3,
            "filament_g": self.filament_g,
        }

    def summary(self) -> str:
        bits = [f"{self.output_path.name} via {self.flavour.value}"]
        if self.estimated_time:
            bits.append(f"~{self.estimated_time}")
        if self.filament_g:
            bits.append(f"{self.filament_g:.1f} g")
        elif self.filament_cm3:
            bits.append(f"{self.filament_cm3:.1f} cm3")
        applied = self.applied
        if applied.get("layer_height"):
            bits.append(f"{applied['layer_height']} mm layers")
        if applied.get("fill_density"):
            bits.append(f"{applied['fill_density']} infill")
        return ", ".join(bits)


def find_slicer(explicit: str | None = None, env: dict | None = None) -> tuple[Path, SlicerFlavour]:
    """Locate an installed slicer, preferring the Bambu-native ones.

    ``explicit`` (or ``MHS_SLICER``) pins a specific binary; its flavour is
    inferred from the filename, which is why the name has to be recognisable.
    """
    env = dict(os.environ if env is None else env)
    candidate = explicit or env.get("MHS_SLICER")
    if candidate:
        path = Path(candidate).expanduser()
        resolved = path if path.is_file() else (shutil.which(candidate) or None)
        if not resolved:
            raise ConfigError(
                f"no slicer at {candidate}",
                hint="Point MHS_SLICER at the slicer binary, or unset it to search the PATH.",
            )
        return Path(resolved), flavour_of(Path(resolved).name)

    for flavour, names in BINARIES.items():
        for name in names:
            found = shutil.which(name)
            if found:
                return Path(found), flavour
    raise ConfigError(
        "no slicer found on the PATH",
        hint="Install OrcaSlicer, Bambu Studio or PrusaSlicer, or set MHS_SLICER to the "
             "binary. On macOS the CLI lives inside the .app, e.g. "
             "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    )


def flavour_of(binary_name: str) -> SlicerFlavour:
    lowered = binary_name.lower().replace("-", "").replace("_", "").replace(" ", "")
    if "orca" in lowered:
        return SlicerFlavour.ORCA
    if "bambu" in lowered:
        return SlicerFlavour.BAMBU
    if "prusa" in lowered or "slic3r" in lowered:
        return SlicerFlavour.PRUSA
    raise ConfigError(
        f"cannot tell which slicer {binary_name!r} is",
        hint="Rename or symlink it so the name contains orca, bambu or prusa.",
    )


@dataclass
class Slicer:
    """An installed slicer, driven headlessly."""

    binary: Path
    flavour: SlicerFlavour
    profiles: tuple[Path, ...] = ()
    timeout_s: float = DEFAULT_TIMEOUT_S
    extra_args: tuple[str, ...] = ()

    @classmethod
    def discover(cls, explicit: str | None = None, **kwargs) -> Slicer:
        binary, flavour = find_slicer(explicit)
        return cls(binary=binary, flavour=flavour, **kwargs)

    # -- argv ---------------------------------------------------------------
    def build_args(self, model: Path, output: Path, settings: SliceSettings) -> list[str]:
        """Assemble the command line. Pure, so the mapping can be tested directly."""
        flags = FLAG_MAP[self.flavour]
        percent = _PERCENT_SETTINGS.get(self.flavour, set())
        argv: list[str] = [str(self.binary)]

        if self.flavour is SlicerFlavour.PRUSA:
            argv += ["--export-gcode"]
        else:
            # Orca and Bambu need an explicit --slice, and emit a .gcode.3mf:
            # a 3MF archive with the toolpaths inside, not a bare .gcode.
            argv += ["--slice", "1"]

        for profile in self.profiles:
            argv += (["--load", str(profile)] if self.flavour is SlicerFlavour.PRUSA
                     else ["--load-settings", str(profile)])

        for name, value in settings.to_dict().items():
            flag = flags.get(name)
            if flag is None:
                raise CommandRejected(
                    f"{self.flavour.value} has no mapping for {name!r}",
                    hint="Supported: " + ", ".join(sorted(flags)),
                )
            if isinstance(value, bool):
                argv.append(f"{flag}={1 if value else 0}")
            elif name in percent:
                argv += [flag, f"{value:g}%"]
            else:
                argv += [flag, f"{value:g}" if isinstance(value, float) else str(value)]

        argv += list(self.extra_args)
        if self.flavour is SlicerFlavour.PRUSA:
            argv += ["--output", str(output)]
        else:
            argv += ["--export-3mf", str(output)]
        argv.append(str(model))
        return argv

    def default_output(self, model: Path, directory: Path, label: str = "") -> Path:
        suffix = ".gcode" if self.flavour is SlicerFlavour.PRUSA else ".gcode.3mf"
        stem = model.stem + (f"-{label}" if label else "")
        return directory / f"{stem}{suffix}"

    # -- run ----------------------------------------------------------------
    def slice(self, model: str | Path, output: str | Path, settings: SliceSettings) -> SliceResult:
        """Slice ``model`` to ``output``. Raises on anything the slicer refuses."""
        source = Path(model).expanduser()
        if not source.is_file():
            raise CommandRejected(f"{source} does not exist")
        target = Path(output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()  # so "file exists" is a reliable success signal

        argv = self.build_args(source, target, settings)
        log.info("slicing: %s", " ".join(argv))
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise SlicerFailed(
                f"the slicer did not finish within {self.timeout_s:.0f}s",
                hint="Large or very fine models can take minutes; raise the timeout.",
            ) from exc
        except OSError as exc:
            raise SlicerFailed(f"could not run {self.binary}: {exc}") from exc

        if completed.returncode != 0 or not target.exists():
            raise SlicerFailed(
                f"{self.flavour.value} refused to slice: {_first_error(completed)}",
                hint="The message above is the slicer's own. An 'Unknown option' means this "
                     "slicer version renamed that setting.",
            )

        result = SliceResult(
            output_path=target,
            flavour=self.flavour,
            requested=settings.to_dict(),
            command=argv,
        )
        _read_back(result)
        return result


class SlicerFailed(MHSError):
    """The slicer ran but would not produce a file."""


def _first_error(completed: subprocess.CompletedProcess) -> str:
    """The most useful line of a failed run - slicers put it on either stream."""
    for stream in (completed.stderr, completed.stdout):
        for line in (stream or "").splitlines():
            text = line.strip()
            if text and not text.startswith(("Usage:", "Actions:", "	")):
                return text
    return f"exit code {completed.returncode} with no message"


_SUMMARY_PATTERNS = {
    "estimated_time": re.compile(r"^;\s*estimated printing time[^=]*=\s*(.+?)\s*$", re.M),
    "filament_mm": re.compile(r"^;\s*filament used \[mm\]\s*=\s*([\d.]+)", re.M),
    "filament_cm3": re.compile(r"^;\s*filament used \[cm3\]\s*=\s*([\d.]+)", re.M),
    "filament_g": re.compile(r"^;\s*total filament used \[g\]\s*=\s*([\d.]+)", re.M),
}

#: Settings worth echoing back, under the slicer's own names.
_APPLIED_KEYS = (
    "layer_height", "first_layer_height", "initial_layer_print_height", "fill_density",
    "sparse_infill_density", "perimeters", "wall_loops", "support_material", "enable_support",
    "nozzle_diameter", "temperature", "bed_temperature", "top_solid_layers", "bottom_solid_layers",
)


def _read_back(result: SliceResult) -> None:
    """Parse the slicer's own summary out of the output.

    This is the check that a wrong profile is visible *before* printing: what
    comes back is what the slicer applied, which is not always what was asked
    for. A .gcode.3mf is a zip, so its gcode member is read from inside.
    """
    try:
        text = _read_gcode_text(result.output_path)
    except Exception:  # pragma: no cover - unreadable output is not fatal
        log.debug("could not read back %s", result.output_path, exc_info=True)
        return

    for name, pattern in _SUMMARY_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1)
        setattr(result, name, raw if name == "estimated_time" else float(raw))
    result.estimated_time_minutes = _minutes(result.estimated_time)

    for key in _APPLIED_KEYS:
        match = re.search(rf"^;\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text, re.M)
        if match:
            result.applied[key] = match.group(1)


def _read_gcode_text(path: Path, tail_bytes: int = 400_000) -> str:
    """Return the gcode text, from a bare file or from inside a .gcode.3mf zip."""
    import zipfile

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".gcode")]
            if not names:
                return ""
            with archive.open(names[0]) as handle:
                return handle.read().decode("utf-8", errors="replace")
    size = path.stat().st_size
    with path.open("rb") as handle:
        # The summary lives in the footer; reading the tail keeps a 200 MB
        # gcode file from being loaded in full just to find six lines.
        handle.seek(max(0, size - tail_bytes))
        return handle.read().decode("utf-8", errors="replace")


def _minutes(estimate: str | None) -> int | None:
    """'1h 17m 51s' -> 78. Slicers vary the wording, so parse what is there."""
    if not estimate:
        return None
    total = 0.0
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", estimate):
        total += float(value) * {"d": 1440, "h": 60, "m": 1, "s": 1 / 60}[unit]
    return int(round(total)) or None
