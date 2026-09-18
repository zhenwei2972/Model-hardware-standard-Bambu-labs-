"""Making a robot's map something a model can point at.

A vacuum's map arrives as a picture plus a set of calibration points relating
image pixels to the robot's own millimetre frame. Those two halves are what
turn "go to the spot by the back door" into a command: render the map with a
grid labelled in millimetres, let the model read coordinates off it, and send
those coordinates straight to the robot.

The transform is derived from the calibration points rather than hard-coded, so
it survives a parser that rotates, crops or rescales the image.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from ..errors import CommandRejected
from ..models import Position, Room

GRID_INK = (56, 132, 255)
ROBOT_INK = (34, 197, 94)
CHARGER_INK = (250, 176, 5)
ROOM_INK = (24, 24, 28)
LABEL_BG = (255, 255, 255)


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10
        return ImageFont.load_default()


@dataclass(frozen=True)
class MapTransform:
    """Affine mapping between the robot's millimetres and image pixels.

    Stored as the forward (mm -> px) matrix ``[[a, b], [c, d]]`` plus offset
    ``(e, f)``; the inverse is computed on demand. An affine form handles a
    rotated or flipped render without special cases.
    """

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    @property
    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    @classmethod
    def from_calibration(cls, points: list[dict]) -> MapTransform:
        """Build the transform from three ``{"vacuum": ..., "map": ...}`` points.

        This is the shape the vacuum map parsers emit for Home Assistant's
        camera calibration, so it comes straight from the library that rendered
        the image.
        """
        if not points or len(points) < 3:
            raise CommandRejected(
                f"need three calibration points to locate the map, got {len(points or [])}"
            )
        try:
            origin, along_x, along_y = (
                (float(p["vacuum"]["x"]), float(p["vacuum"]["y"]),
                 float(p["map"]["x"]), float(p["map"]["y"]))
                for p in points[:3]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandRejected(f"malformed calibration points: {exc}") from exc

        x0, y0, u0, v0 = origin
        x1, y1, u1, v1 = along_x
        x2, y2, u2, v2 = along_y
        span_x, span_y = x1 - x0, y2 - y0
        if abs(span_x) < 1e-9 or abs(span_y) < 1e-9:
            raise CommandRejected("degenerate calibration points: the axes are not independent")

        a, c = (u1 - u0) / span_x, (v1 - v0) / span_x
        b, d = (u2 - u0) / span_y, (v2 - v0) / span_y
        e = u0 - (a * x0 + b * y0)
        f = v0 - (c * x0 + d * y0)
        transform = cls(a, b, c, d, e, f)
        if abs(transform.determinant) < 1e-12:
            raise CommandRejected("degenerate calibration: the map has no usable scale")
        return transform

    def to_pixels(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        return (self.a * x_mm + self.b * y_mm + self.e, self.c * x_mm + self.d * y_mm + self.f)

    def to_mm(self, px: float, py: float) -> tuple[float, float]:
        det = self.determinant
        u, v = px - self.e, py - self.f
        return ((self.d * u - self.b * v) / det, (self.a * v - self.c * u) / det)

    @property
    def mm_per_pixel(self) -> float:
        """Average scale, for choosing a sensible grid spacing."""
        pixels_per_mm = (math.hypot(self.a, self.c) + math.hypot(self.b, self.d)) / 2
        return 1.0 / pixels_per_mm if pixels_per_mm else 0.0

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "c": self.c, "d": self.d, "e": self.e, "f": self.f,
                "mm_per_pixel": round(self.mm_per_pixel, 3)}


def choose_grid_mm(transform: MapTransform, width_px: int, target_px: int = 110) -> int:
    """Pick a round grid spacing that lands near ``target_px`` on screen."""
    candidates = (100, 250, 500, 1000, 2000, 5000)
    ideal = target_px * transform.mm_per_pixel
    return min(candidates, key=lambda mm: abs(mm - ideal))


def choose_scale(width: int, height: int, minimum_px: int = 900) -> int:
    """Upscale factor that makes a small map legible.

    A vacuum's map is typically a couple of hundred pixels across, where any
    label large enough to read covers a whole room. Enlarging first, then
    annotating, keeps the text proportionate to the floor plan.
    """
    longest = max(width, height) or 1
    return max(1, min(8, -(-minimum_px // longest)))


def annotate_map(
    image_png: bytes,
    transform: MapTransform,
    *,
    rooms: list[Room] | None = None,
    robot: Position | None = None,
    charger: Position | None = None,
    grid_mm: int | None = None,
    scale: int | None = None,
) -> bytes:
    """Overlay a millimetre grid, the robot, the dock and room names.

    The grid is labelled in the robot's own coordinates, so a position read off
    this image can go straight into ``go_to`` with no conversion - which is
    exactly where a hand-rolled pixel-to-millimetre guess would go wrong.

    Axis labels sit in the margins rather than on the lines: a vacuum map is
    small and busy, and labels scattered across the floor plan cover the rooms
    they are meant to help you find.
    """
    image = Image.open(io.BytesIO(image_png)).convert("RGB")
    factor = scale if scale is not None else choose_scale(image.width, image.height)
    if factor > 1:
        image = image.resize((image.width * factor, image.height * factor), Image.NEAREST)
        transform = MapTransform(
            transform.a * factor, transform.b * factor, transform.c * factor,
            transform.d * factor, transform.e * factor, transform.f * factor,
        )

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    step = grid_mm or choose_grid_mm(transform, width)
    tick_font = _font(max(12, min(18, width // 55)))
    room_font = _font(max(13, min(22, width // 42)))

    # Work out the millimetre extent the image covers, then draw the grid in
    # millimetre space and project it - so it stays correct if the render is
    # rotated rather than axis-aligned.
    corners = [transform.to_mm(0, 0), transform.to_mm(width, 0),
               transform.to_mm(0, height), transform.to_mm(width, height)]
    min_x, max_x = min(c[0] for c in corners), max(c[0] for c in corners)
    min_y, max_y = min(c[1] for c in corners), max(c[1] for c in corners)

    for x_mm in _ticks(min_x, max_x, step):
        ends = [transform.to_pixels(x_mm, min_y), transform.to_pixels(x_mm, max_y)]
        draw.line(ends, fill=(*GRID_INK, 80), width=1)
        anchor = min(ends, key=lambda p: p[1])  # the end nearest the top edge
        _label(draw, (_clamp(anchor[0], 22, width - 22), 12), f"x {int(x_mm)}", tick_font,
               centre=True)
    for y_mm in _ticks(min_y, max_y, step):
        ends = [transform.to_pixels(min_x, y_mm), transform.to_pixels(max_x, y_mm)]
        draw.line(ends, fill=(*GRID_INK, 80), width=1)
        anchor = min(ends, key=lambda p: p[0])  # the end nearest the left edge
        _label(draw, (34, _clamp(anchor[1], 12, height - 34)), f"y {int(y_mm)}", tick_font,
               centre=True)

    for room in rooms or []:
        if room.center is None:
            continue
        px, py = transform.to_pixels(room.center.x_mm, room.center.y_mm)
        if 0 <= px < width and 0 <= py < height:
            _label(draw, (px, py), room.label, room_font, ink=ROOM_INK, centre=True)

    marker = max(8, width // 90)
    if charger is not None:
        px, py = transform.to_pixels(charger.x_mm, charger.y_mm)
        draw.rectangle([px - marker, py - marker, px + marker, py + marker],
                       outline=(*CHARGER_INK, 255), width=3)
        _label(draw, (px, py + marker + 11), "dock", tick_font, centre=True)
    if robot is not None:
        px, py = transform.to_pixels(robot.x_mm, robot.y_mm)
        draw.ellipse([px - marker, py - marker, px + marker, py + marker],
                     outline=(*ROBOT_INK, 255), width=4)
        _label(draw, (px, py - marker - 11), "robot", tick_font, centre=True)

    caption = (
        f"grid {step} mm ({step / 1000:g} m)   labels are robot millimetres - "
        f"pass them straight to go_to"
    )
    bar = max(24, width // 34)
    draw.rectangle([0, height - bar, width, height], fill=(20, 20, 24, 205))
    draw.text((8, height - bar + 4), caption, fill=(255, 255, 255, 240), font=tick_font)

    out = io.BytesIO()
    Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB").save(
        out, format="PNG", optimize=True
    )
    return out.getvalue()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ticks(low: float, high: float, step: int) -> list[float]:
    first = math.ceil(low / step) * step
    return [first + i * step for i in range(int((high - first) // step) + 1)]


def _label(draw, xy, text, font, ink=(20, 20, 24), centre: bool = False) -> None:
    anchor = "mm" if centre else "la"
    box = draw.textbbox(xy, text, font=font, anchor=anchor)
    draw.rectangle([box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2], fill=(*LABEL_BG, 205))
    draw.text(xy, text, fill=(*ink, 255), font=font, anchor=anchor)
