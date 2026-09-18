"""Software rendering of a mesh, so a model can look at what it is about to print.

A z-buffered rasteriser in numpy rather than an OpenGL binding: it runs
headless anywhere, it is deterministic (so it can be tested), and it needs no
display server on the machine hosting the MCP server.

Views are orthographic on purpose. Perspective flatters a model; orthographic
keeps proportions honest, which is the point when the question is "is this the
right size?".
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .mesh import Mesh

#: Named camera directions, as (azimuth, elevation) in degrees.
VIEWS: dict[str, tuple[float, float]] = {
    "iso": (-45.0, 30.0),
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "left": (90.0, 0.0),
    "right": (-90.0, 0.0),
    "top": (0.0, 89.9),
    "bottom": (0.0, -89.9),
}

BACKGROUND = (246, 246, 248)
MODEL_COLOR = np.array([214, 158, 106], dtype=np.float64)  # warm filament orange
OVERHANG_COLOR = np.array([206, 86, 86], dtype=np.float64)  # unsupported steep faces
INK = (44, 44, 48)
MUTED = (128, 128, 136)


@dataclass
class RenderStyle:
    ambient: float = 0.32
    diffuse: float = 0.68
    #: Faces steeper than this from vertical are tinted, matching the 45 degree
    #: rule a slicer uses to decide where supports are needed.
    overhang_degrees: float | None = 45.0
    show_scale_bar: bool = True


def _camera_basis(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    """Return a 3x3 matrix whose rows are the camera's right/up/forward axes."""
    az, el = np.radians(azimuth_deg), np.radians(elevation_deg)
    forward = np.array([
        np.cos(el) * np.sin(az),
        -np.cos(el) * np.cos(az),
        np.sin(el),
    ])
    forward /= np.linalg.norm(forward)
    world_up = np.array([0.0, 0.0, 1.0])
    if abs(float(forward @ world_up)) > 0.999:  # looking straight down the Z axis
        world_up = np.array([0.0, 1.0, 0.0])
    right = np.cross(forward, world_up)
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    return np.vstack([right, up, forward])


def overhanging_faces(
    mesh: Mesh, threshold_degrees: float = 45.0, bed_tolerance_mm: float = 0.3
) -> np.ndarray:
    """Boolean mask of faces that would need support.

    A face qualifies when its normal points within ``90 - threshold`` degrees of
    straight down. Faces sitting on the build plate are excluded: they are
    downward-facing too, but the bed supports them, and flagging them would
    turn every flat-bottomed model into a false alarm.
    """
    normals = mesh.face_normals
    tilt_from_down = np.degrees(np.arccos(np.clip(-normals[:, 2], -1.0, 1.0)))
    downward = tilt_from_down < (90.0 - threshold_degrees)
    bed_z = mesh.vertices[:, 2].min()
    on_bed = (mesh.triangles[:, :, 2] <= bed_z + bed_tolerance_mm).all(axis=1)
    return downward & ~on_bed


def rasterise(
    mesh: Mesh,
    view: str = "iso",
    size: int = 420,
    *,
    style: RenderStyle | None = None,
    scale_px_per_mm: float | None = None,
) -> tuple[np.ndarray, float]:
    """Render one view. Returns the RGB pixel array and the px/mm scale used.

    Passing ``scale_px_per_mm`` renders several views at a common scale, so a
    montage shows real relative proportions instead of each view being fitted
    to its own frame.
    """
    style = style or RenderStyle()
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}; expected one of {', '.join(VIEWS)}")

    basis = _camera_basis(*VIEWS[view])
    # basis[2] points from the object towards the camera, so negate it for depth:
    # larger values are further away, which is what the z-test below assumes.
    camera_space = (mesh.vertices - mesh.center) @ basis.T
    camera_space[:, 2] *= -1.0

    if scale_px_per_mm is None:
        extent = np.abs(camera_space[:, :2]).max() * 2
        scale_px_per_mm = (size * 0.82) / extent if extent > 0 else 1.0

    # Screen space: y grows downward, origin at the image centre.
    xs = camera_space[:, 0] * scale_px_per_mm + size / 2
    ys = -camera_space[:, 1] * scale_px_per_mm + size / 2
    depth = camera_space[:, 2]
    screen = np.stack([xs, ys, depth], axis=1)

    normals = mesh.face_normals
    visible = (normals @ basis[2]) > 0  # keep faces whose normal turns towards the camera
    faces = mesh.faces[visible]
    if len(faces) == 0:  # degenerate or inside-out mesh: draw everything
        faces = mesh.faces
        normals_visible = normals
    else:
        normals_visible = normals[visible]

    # A key light just above and to the left of the camera. basis[2] points from
    # the object towards the viewer, so the light sits on the camera's side.
    light = -basis[0] * 0.35 + basis[1] * 0.45 + basis[2] * 0.82
    light /= np.linalg.norm(light)
    intensity = style.ambient + style.diffuse * np.clip(normals_visible @ light, 0, 1)

    colors = np.tile(MODEL_COLOR, (len(faces), 1))
    if style.overhang_degrees is not None:
        colors[overhanging_faces(mesh, style.overhang_degrees)[visible]] = OVERHANG_COLOR
    colors = colors * intensity[:, None]

    frame = np.full((size, size, 3), BACKGROUND, dtype=np.float64)
    zbuffer = np.full((size, size), np.inf)
    _draw_triangles(frame, zbuffer, screen[faces], colors)
    return frame.clip(0, 255).astype(np.uint8), float(scale_px_per_mm)


def _draw_triangles(frame: np.ndarray, zbuffer: np.ndarray, tris: np.ndarray, colors: np.ndarray) -> None:
    """Rasterise with a per-triangle barycentric test over its bounding box.

    Per-triangle setup is hoisted into vectorised arrays because the Python-level
    loop, not the pixel work, is what costs on a dense mesh.
    """
    size = frame.shape[0]
    xs, ys, zs = tris[:, :, 0], tris[:, :, 1], tris[:, :, 2]
    min_xs = np.clip(np.floor(xs.min(axis=1)).astype(np.int64), 0, size)
    max_xs = np.clip(np.ceil(xs.max(axis=1)).astype(np.int64) + 1, 0, size)
    min_ys = np.clip(np.floor(ys.min(axis=1)).astype(np.int64), 0, size)
    max_ys = np.clip(np.ceil(ys.max(axis=1)).astype(np.int64) + 1, 0, size)
    widths = xs.max(axis=1) - xs.min(axis=1)
    heights = ys.max(axis=1) - ys.min(axis=1)
    areas = (xs[:, 1] - xs[:, 0]) * (ys[:, 2] - ys[:, 0]) - (xs[:, 2] - xs[:, 0]) * (ys[:, 1] - ys[:, 0])
    centers_x = np.clip(xs.mean(axis=1).astype(np.int64), 0, size - 1)
    centers_y = np.clip(ys.mean(axis=1).astype(np.int64), 0, size - 1)
    mean_z = zs.mean(axis=1)

    # Painter order first: the depth test then rejects most fragments cheaply.
    for index in np.argsort(-mean_z):
        if min_xs[index] >= max_xs[index] or min_ys[index] >= max_ys[index]:
            continue

        # A dense mesh is mostly sub-pixel triangles. Resolving those with one
        # depth test avoids the meshgrid setup that would otherwise dominate.
        if widths[index] < 1.5 and heights[index] < 1.5:
            cy, cx = centers_y[index], centers_x[index]
            if mean_z[index] < zbuffer[cy, cx]:
                zbuffer[cy, cx] = mean_z[index]
                frame[cy, cx] = colors[index]
            continue

        area = areas[index]
        if abs(area) < 1e-12:  # edge-on sliver
            continue
        min_x, max_x = min_xs[index], max_xs[index]
        min_y, max_y = min_ys[index], max_ys[index]
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = tris[index]

        gx = np.arange(min_x, max_x) + 0.5
        gy = (np.arange(min_y, max_y) + 0.5)[:, None]
        w0 = ((x1 - gx) * (y2 - gy) - (x2 - gx) * (y1 - gy)) / area
        w1 = ((x2 - gx) * (y0 - gy) - (x0 - gx) * (y2 - gy)) / area
        w2 = 1.0 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue

        z = w0 * z0 + w1 * z1 + w2 * z2
        window = zbuffer[min_y:max_y, min_x:max_x]
        closer = inside & (z < window)
        if not closer.any():
            continue
        window[closer] = z[closer]
        frame[min_y:max_y, min_x:max_x][closer] = colors[index]


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10 has a fixed-size default font
        return ImageFont.load_default()


def _draw_scale_bar(draw: ImageDraw.ImageDraw, size: int, px_per_mm: float) -> None:
    """A labelled bar with a round millimetre length - the size cue in a render."""
    target_px = size * 0.25
    nice_mm = [0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200]
    length_mm = min(nice_mm, key=lambda mm: abs(mm * px_per_mm - target_px))
    bar_px = length_mm * px_per_mm
    if not 8 <= bar_px <= size * 0.8:
        return
    x0, y = size - 14 - bar_px, size - 16
    draw.line([(x0, y), (x0 + bar_px, y)], fill=INK, width=2)
    for x in (x0, x0 + bar_px):
        draw.line([(x, y - 4), (x, y + 4)], fill=INK, width=2)
    label = f"{length_mm:g} mm"
    draw.text((x0 + bar_px / 2, y - 14), label, fill=INK, anchor="mm", font=_font(13))


def render_views(
    mesh: Mesh,
    views: tuple[str, ...] = ("iso", "front", "right", "top"),
    size: int = 420,
    *,
    style: RenderStyle | None = None,
    title: str | None = None,
) -> Image.Image:
    """Render several views into one labelled contact sheet.

    One image rather than several keeps a tool result to a single attachment,
    and puts the views side by side where they are easiest to compare.
    """
    style = style or RenderStyle()
    if not views:
        raise ValueError("at least one view is required")
    unknown = [v for v in views if v not in VIEWS]
    if unknown:
        raise ValueError(
            f"unknown view {', '.join(unknown)}; expected any of {', '.join(VIEWS)}"
        )

    # A single scale across every view, so relative proportions stay truthful.
    span = 0.0
    for view in views:
        basis = _camera_basis(*VIEWS[view])
        projected = (mesh.vertices - mesh.center) @ basis.T
        span = max(span, float(np.abs(projected[:, :2]).max()) * 2)
    scale = (size * 0.82) / span if span > 0 else 1.0

    tiles = []
    for view in views:
        pixels, _ = rasterise(mesh, view, size, style=style, scale_px_per_mm=scale)
        tile = Image.fromarray(pixels)
        draw = ImageDraw.Draw(tile)
        draw.text((12, 10), view.upper(), fill=MUTED, font=_font(15))
        if style.show_scale_bar:
            _draw_scale_bar(draw, size, scale)
        tiles.append(tile)

    columns = 2 if len(tiles) > 1 else 1
    rows = (len(tiles) + columns - 1) // columns
    header = 34 if title else 0
    footer = 30
    sheet = Image.new("RGB", (columns * size, header + rows * size + footer), BACKGROUND)
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % columns) * size, header + (index // columns) * size))

    draw = ImageDraw.Draw(sheet)
    if title:
        draw.text((14, 9), title, fill=INK, font=_font(17))
    dims = mesh.dimensions
    caption = (
        f"{dims[0]:.2f} x {dims[1]:.2f} x {dims[2]:.2f} mm    "
        f"volume {mesh.volume_mm3 / 1000:.2f} cm3    {mesh.triangle_count:,} triangles"
    )
    if style.overhang_degrees is not None:
        caption += f"    red = steeper than {style.overhang_degrees:g}deg overhang"
    draw.text((14, sheet.height - 21), caption, fill=MUTED, font=_font(14))

    for index in range(1, len(tiles)):
        if index % columns:
            x = (index % columns) * size
            draw.line([(x, header), (x, header + rows * size)], fill=(226, 226, 230))
        if index >= columns:
            y = header + (index // columns) * size
            draw.line([(0, y), (sheet.width, y)], fill=(226, 226, 230))
    return sheet


def render_to_png(mesh: Mesh, path: str | Path | None = None, **kwargs) -> bytes:
    """Render and return PNG bytes, optionally writing them to ``path``."""
    image = render_views(mesh, **kwargs)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    if path is not None:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return data
