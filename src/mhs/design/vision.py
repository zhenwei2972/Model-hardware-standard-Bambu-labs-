"""Turning camera frames into millimetres.

The printer camera is a fixed, off-axis chamber cam, not a metrology device, so
there is no intrinsic way to know how big something on the plate is. What works
is the oldest trick in photography: put an object of known size in the frame,
in the same plane, and measure everything relative to it. A coin is ideal -
flat, circular, and machined to a published diameter.

Division of labour: the model looks at the frame and says where things are (it
is good at that); these functions do the arithmetic, keep the calibration, and
draw back exactly what was measured so the reading can be checked rather than
trusted.
"""

from __future__ import annotations

import io
import math
import time
from dataclasses import asdict, dataclass

from PIL import Image, ImageDraw, ImageFont

from ..errors import CommandRejected

#: Published diameters in mm. A coin is the most available precision artefact
#: most people own.
REFERENCE_OBJECTS: dict[str, tuple[float, str]] = {
    "sgd_1": (24.65, "Singapore $1"),
    "sgd_50c": (23.00, "Singapore 50 cents"),
    "sgd_20c": (21.36, "Singapore 20 cents"),
    "sgd_10c": (18.50, "Singapore 10 cents"),
    "usd_quarter": (24.26, "US quarter"),
    "usd_nickel": (21.21, "US nickel"),
    "usd_penny": (19.05, "US penny"),
    "usd_dime": (17.91, "US dime"),
    "eur_2": (25.75, "2 euro"),
    "eur_1": (23.25, "1 euro"),
    "eur_50c": (24.25, "50 euro cents"),
    "gbp_2": (28.40, "UK 2 pound"),
    "gbp_1": (23.43, "UK 1 pound"),
    "aud_1": (25.00, "Australian $1"),
    "cad_quarter": (23.88, "Canadian quarter"),
    "inr_10": (27.00, "India 10 rupee"),
    "jpy_100": (22.60, "Japan 100 yen"),
    "cny_1": (25.00, "China 1 yuan"),
    # Non-coin references that are always to hand around a printer.
    "filament_1_75": (1.75, "1.75 mm filament"),
    "sd_card": (24.00, "SD card width"),
    "credit_card_long": (85.60, "credit card, long edge"),
    "credit_card_short": (53.98, "credit card, short edge"),
    "a1_mini_bed": (180.00, "A1 mini build plate edge"),
}

GRID_INK = (255, 90, 60)
LABEL_BG = (20, 20, 24)
MEASURE_INK = (60, 200, 255)


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10
        return ImageFont.load_default()


@dataclass
class ScaleCalibration:
    """Millimetres per pixel, derived from one known object in the frame."""

    mm_per_pixel: float
    reference_name: str
    reference_mm: float
    pixel_length: float
    printer_id: str | None = None
    note: str | None = None
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if self.mm_per_pixel <= 0 or not math.isfinite(self.mm_per_pixel):
            raise CommandRejected("calibration must be a positive, finite mm-per-pixel value")
        self.created_at = self.created_at or time.time()

    def measure(self, pixel_length: float) -> float:
        if pixel_length < 0:
            raise CommandRejected("pixel length cannot be negative")
        return pixel_length * self.mm_per_pixel

    def pixels_for(self, mm: float) -> float:
        return mm / self.mm_per_pixel

    def to_dict(self) -> dict:
        # Stored at full precision: this dict is what gets persisted and read
        # back, so rounding here would quietly degrade every later measurement.
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ScaleCalibration:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def resolve_reference(name_or_mm: str | float) -> tuple[float, str]:
    """Accept either a known reference name or a literal size in millimetres."""
    if isinstance(name_or_mm, (int, float)):
        if name_or_mm <= 0:
            raise CommandRejected("reference size must be positive")
        return float(name_or_mm), f"{name_or_mm:g} mm reference"
    key = str(name_or_mm).strip().lower().replace(" ", "_").replace("-", "_")
    if key in REFERENCE_OBJECTS:
        size, label = REFERENCE_OBJECTS[key]
        return size, label
    try:
        return resolve_reference(float(name_or_mm))
    except ValueError:
        raise CommandRejected(
            f"unknown reference {name_or_mm!r}",
            hint="Pass a diameter in mm, or one of: " + ", ".join(sorted(REFERENCE_OBJECTS)),
        ) from None


def calibrate(
    reference: str | float,
    pixel_length: float,
    *,
    printer_id: str | None = None,
    note: str | None = None,
) -> ScaleCalibration:
    """Derive mm-per-pixel from a known object measured in pixels."""
    if pixel_length <= 0:
        raise CommandRejected("pixel length must be positive")
    reference_mm, label = resolve_reference(reference)
    return ScaleCalibration(
        mm_per_pixel=reference_mm / pixel_length,
        reference_name=label,
        reference_mm=reference_mm,
        pixel_length=float(pixel_length),
        printer_id=printer_id,
        note=note,
    )


def distance_px(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    return math.dist(point_a, point_b)


# -- annotation ------------------------------------------------------------
def _open(frame: bytes | Image.Image) -> Image.Image:
    if isinstance(frame, Image.Image):
        return frame.convert("RGB")
    return Image.open(io.BytesIO(frame)).convert("RGB")


def _to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def annotate_grid(
    frame: bytes | Image.Image,
    step_px: int = 80,
    calibration: ScaleCalibration | None = None,
) -> bytes:
    """Overlay a labelled pixel grid on a camera frame.

    This is what makes "the coin spans from x=410 to x=498" a thing a model can
    state accurately: without a grid it is guessing at coordinates in a
    featureless photo.
    """
    image = _open(frame)
    if step_px < 10:
        raise CommandRejected("grid step must be at least 10 px")
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for x in range(0, width, step_px):
        draw.line([(x, 0), (x, height)], fill=(*GRID_INK, 90), width=1)
    for y in range(0, height, step_px):
        draw.line([(0, y), (width, y)], fill=(*GRID_INK, 90), width=1)

    font = _font(13)
    for x in range(0, width, step_px * 2):
        draw.rectangle([x + 1, 1, x + 34, 16], fill=(*LABEL_BG, 170))
        draw.text((x + 3, 3), str(x), fill=(255, 255, 255, 235), font=font)
    for y in range(step_px * 2, height, step_px * 2):
        draw.rectangle([1, y + 1, 34, y + 16], fill=(*LABEL_BG, 170))
        draw.text((3, y + 3), str(y), fill=(255, 255, 255, 235), font=font)

    caption = f"grid {step_px} px"
    if calibration:
        caption += (f"  =  {step_px * calibration.mm_per_pixel:.2f} mm   "
                    f"({calibration.mm_per_pixel:.4f} mm/px via {calibration.reference_name})")
    else:
        caption += "   uncalibrated - measure a known object to set the scale"
    draw.rectangle([0, height - 22, width, height], fill=(*LABEL_BG, 190))
    draw.text((6, height - 18), caption, fill=(255, 255, 255, 240), font=_font(14))

    return _to_png(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"))


def annotate_measurement(
    frame: bytes | Image.Image,
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    calibration: ScaleCalibration | None = None,
    label: str | None = None,
) -> tuple[bytes, float, float | None]:
    """Draw the measured span back onto the frame.

    Returns the annotated PNG, the pixel distance, and the millimetre distance
    when a calibration is available. Drawing it back is the point: a measurement
    you cannot see is a measurement you cannot check.
    """
    image = _open(frame)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    pixels = distance_px(point_a, point_b)
    millimetres = calibration.measure(pixels) if calibration else None

    draw.line([point_a, point_b], fill=(*MEASURE_INK, 255), width=3)
    for point in (point_a, point_b):
        draw.ellipse([point[0] - 5, point[1] - 5, point[0] + 5, point[1] + 5],
                     outline=(*MEASURE_INK, 255), width=3)

    text = label or (f"{millimetres:.2f} mm" if millimetres is not None else f"{pixels:.0f} px")
    if millimetres is not None and label is None:
        text += f"  ({pixels:.0f} px)"
    mid = ((point_a[0] + point_b[0]) / 2, (point_a[1] + point_b[1]) / 2 - 18)
    font = _font(16)
    box = draw.textbbox(mid, text, font=font, anchor="mm")
    draw.rectangle([box[0] - 6, box[1] - 4, box[2] + 6, box[3] + 4], fill=(*LABEL_BG, 205))
    draw.text(mid, text, fill=(255, 255, 255, 255), font=font, anchor="mm")

    composed = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    return _to_png(composed), pixels, millimetres


def compare_to_target(measured_mm: float, target_mm: float) -> dict:
    """How far a measured print is from its intended size."""
    if target_mm <= 0:
        raise CommandRejected("target size must be positive")
    error = measured_mm - target_mm
    percent = error / target_mm * 100
    if abs(percent) <= 2:
        assessment = "within 2% - at the limit of what this camera can resolve, treat as on target"
    elif abs(percent) <= 5:
        assessment = "within 5% - plausibly real; confirm with calipers before changing anything"
    else:
        assessment = "off by more than 5% - large enough to be a real scaling error"
    return {
        "measured_mm": round(measured_mm, 2),
        "target_mm": round(target_mm, 2),
        "error_mm": round(error, 2),
        "error_percent": round(percent, 1),
        "suggested_rescale_factor": round(target_mm / measured_mm, 4) if measured_mm else None,
        "assessment": assessment,
    }


#: Stated plainly wherever a measurement is returned, because the failure mode
#: here is a confident number that is quietly wrong.
ACCURACY_CAVEAT = (
    "The chamber camera is off-axis and has lens distortion, so this is an estimate. "
    "It is only trustworthy when the reference object lies flat on the plate, in the same "
    "plane and close to what is being measured. Expect a few percent of error; use calipers "
    "when the exact dimension matters."
)
