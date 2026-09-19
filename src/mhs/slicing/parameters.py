"""Slicing settings, and the intents that generate them.

An *intent* is the thing a person actually says: make it fast, make it nice,
make it strong. Turning that into numbers is the part worth automating, and the
numbers are derived from the printer rather than hard-coded - a 0.12 mm layer
is "fine" on a 0.4 mm nozzle and impossible on a 0.2 mm one.

Every value an intent produces can be overridden individually, so the intent is
a starting point rather than a wall.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace

from ..errors import CommandRejected
from ..specs import PrinterSpec


@dataclass
class SliceSettings:
    """Slicer-neutral settings. ``None`` means "leave the profile's value alone".

    Deliberately small: these are the settings that change the outcome enough
    to be worth an agent's attention. Anything else belongs in the profile you
    exported from the slicer's own interface, which this never overwrites.
    """

    layer_height_mm: float | None = None
    first_layer_height_mm: float | None = None
    infill_percent: float | None = None
    wall_loops: int | None = None
    top_layers: int | None = None
    bottom_layers: int | None = None
    supports: bool | None = None
    brim_width_mm: float | None = None
    perimeter_speed_mm_s: float | None = None
    infill_speed_mm_s: float | None = None

    def merged_with(self, other: SliceSettings | None) -> SliceSettings:
        """Overlay ``other``'s set values on top of these."""
        if other is None:
            return self
        overrides = {f.name: getattr(other, f.name) for f in fields(other)
                     if getattr(other, f.name) is not None}
        return replace(self, **overrides)

    def to_dict(self, skip_none: bool = True) -> dict:
        data = asdict(self)
        return {k: v for k, v in data.items() if v is not None} if skip_none else data

    def validate(self, spec: PrinterSpec, nozzle_mm: float | None = None) -> None:
        """Check the settings against what this printer can physically do."""
        nozzle = nozzle_mm or spec.default_nozzle_mm
        low, high = spec.layer_height_range_mm
        if self.layer_height_mm is not None:
            if not low <= self.layer_height_mm <= high:
                raise CommandRejected(
                    f"{self.layer_height_mm:g} mm layers are outside the {spec.model}'s "
                    f"{low:g}-{high:g} mm range",
                )
            if self.layer_height_mm > nozzle:
                raise CommandRejected(
                    f"a {self.layer_height_mm:g} mm layer is taller than the {nozzle:g} mm "
                    "nozzle; the slicer will refuse it",
                )
        if self.infill_percent is not None and not 0 <= self.infill_percent <= 100:
            raise CommandRejected("infill must be between 0 and 100 percent")
        if self.wall_loops is not None and not 1 <= self.wall_loops <= 10:
            raise CommandRejected("wall_loops must be between 1 and 10")
        for name in ("perimeter_speed_mm_s", "infill_speed_mm_s"):
            speed = getattr(self, name)
            if speed is not None and not 5 <= speed <= spec.max_speed_mm_s:
                raise CommandRejected(
                    f"{name} must be between 5 and {spec.max_speed_mm_s:g} mm/s for "
                    f"the {spec.model}"
                )


@dataclass(frozen=True)
class Intent:
    """A named trade-off, expressed relative to the printer's own limits."""

    name: str
    summary: str
    #: Layer height as a fraction of the nozzle diameter, before clamping.
    layer_fraction: float
    infill_percent: float
    wall_loops: int
    top_layers: int
    bottom_layers: int
    #: Multiplier on the profile's speeds; None leaves them alone.
    speed_scale: float | None = None
    trade: str = ""

    def settings(self, spec: PrinterSpec, nozzle_mm: float | None = None,
                 base_speed_mm_s: float = 100.0) -> SliceSettings:
        nozzle = nozzle_mm or spec.default_nozzle_mm
        low, high = spec.layer_height_range_mm
        # Clamp into the printer's range, and never exceed the nozzle: the
        # slicer rejects a layer taller than the nozzle outright.
        layer = min(max(round(nozzle * self.layer_fraction, 2), low), min(high, nozzle))
        settings = SliceSettings(
            layer_height_mm=layer,
            infill_percent=self.infill_percent,
            wall_loops=self.wall_loops,
            top_layers=self.top_layers,
            bottom_layers=self.bottom_layers,
        )
        if self.speed_scale is not None:
            capped = min(base_speed_mm_s * self.speed_scale, spec.max_speed_mm_s)
            settings.perimeter_speed_mm_s = round(capped * 0.6, 1)  # perimeters set the finish
            settings.infill_speed_mm_s = round(capped, 1)
        return settings

    def to_dict(self) -> dict:
        return {"name": self.name, "summary": self.summary, "trade": self.trade}


#: The intents an agent can ask for by name.
INTENTS: dict[str, Intent] = {
    "draft": Intent(
        name="draft",
        summary="As fast as the machine sensibly goes. For test fits and throwaway parts.",
        layer_fraction=0.65, infill_percent=8, wall_loops=2, top_layers=3, bottom_layers=3,
        speed_scale=1.3,
        trade="Visible layer lines and soft detail. Not for anything that has to look good "
              "or bear load.",
    ),
    "speed": Intent(
        name="speed",
        summary="Noticeably quicker than default, still presentable.",
        layer_fraction=0.55, infill_percent=12, wall_loops=2, top_layers=4, bottom_layers=3,
        speed_scale=1.15,
        trade="Layer lines are visible on curves; fine surface relief softens.",
    ),
    "balanced": Intent(
        name="balanced",
        summary="The usual compromise. Start here unless you know what you want instead.",
        layer_fraction=0.5, infill_percent=15, wall_loops=2, top_layers=4, bottom_layers=3,
        trade="Nothing in particular. Leaves the profile's speeds alone.",
    ),
    "quality": Intent(
        name="quality",
        summary="Finer layers and an extra wall, for parts that are looked at.",
        layer_fraction=0.3, infill_percent=20, wall_loops=3, top_layers=5, bottom_layers=4,
        speed_scale=0.8,
        trade="Roughly twice the time of balanced, for the same object.",
    ),
    "fine": Intent(
        name="fine",
        summary="The finest layers this nozzle supports. For small, detailed objects.",
        layer_fraction=0.2, infill_percent=20, wall_loops=3, top_layers=6, bottom_layers=4,
        speed_scale=0.65,
        trade="Slow - often 3-4x balanced. Below about 0.4 mm of detail it buys nothing, "
              "because the nozzle width still sets the XY limit.",
    ),
    "strong": Intent(
        name="strong",
        summary="Functional parts: thick walls and dense infill.",
        layer_fraction=0.5, infill_percent=45, wall_loops=4, top_layers=5, bottom_layers=4,
        trade="Heavy and slow, and uses a lot more filament. Wall count matters more than "
              "infill for strength, so this raises both.",
    ),
}


def settings_for_intent(
    intent: str,
    spec: PrinterSpec,
    nozzle_mm: float | None = None,
    overrides: SliceSettings | None = None,
) -> tuple[SliceSettings, Intent]:
    """Resolve an intent into settings, then apply any explicit overrides."""
    key = str(intent).strip().lower()
    if key not in INTENTS:
        raise CommandRejected(
            f"unknown slicing intent {intent!r}",
            hint="Available: " + ", ".join(sorted(INTENTS)),
        )
    chosen = INTENTS[key]
    settings = chosen.settings(spec, nozzle_mm).merged_with(overrides)
    settings.validate(spec, nozzle_mm)
    return settings, chosen
