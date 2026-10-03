"""A stackable riser block for an IKEA Detolf shelf: print several, build the stairs.

One block is 176 wide x 100 deep x 50 mm tall. Two rails under it drop into two
grooves on top of the block below, so a stack cannot slide forward or back. A
staircase is columns of 1, 2, 3 ... blocks, one column behind the other, so the
back rows climb to use the shelf's height.

Detolf compartment (community measurements, MyFigureCollection / Rebelscum):
about 390 x 330 mm, 381 mm to the shelf above.

Coordinates: x across the shelf, y from the front backward, z up (mm).

Run:  python3 stack_riser.py   ->  stack_block.stl, stack_block_print.stl
                                    + preview/stack/*.stl
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import Polygon, box as rect

SHELF_W, SHELF_D, SHELF_H = 390.0, 330.0, 381.0

W, D, H = 176.0, 100.0, 50.0           # one block; D holds a 95 mm base
RAIL_Y = (20.0, 80.0)                   # rail centres, from the block's front
RAIL_W, RAIL_H = 6.0, 2.0               # the tongue under the block
GROOVE_W, GROOVE_D = 6.6, 2.2           # 0.3 mm a side, 0.2 mm under the rail

SMALL, MEDIUM, LARGE = (60.0, 100.0), (75.0, 150.0), (95.0, 210.0)


def profile() -> Polygon:
    """The block's side outline (y, z): body, grooves cut in the top, rails below."""
    shape = rect(0, 0, D, H)
    for y in RAIL_Y:
        shape = shape.difference(rect(y - GROOVE_W / 2, H - GROOVE_D, y + GROOVE_W / 2, H + 1))
        shape = shape.union(rect(y - RAIL_W / 2, -RAIL_H, y + RAIL_W / 2, 0.01))
    return shape.buffer(0)


def block() -> trimesh.Trimesh:
    """The block resting on its rails: rails' underside at z=0."""
    m = trimesh.creation.extrude_polygon(profile(), height=W)      # extruded along z
    # (y, z, x) -> (x, y, z): the profile's first axis is depth, its second height
    m.apply_transform(np.array([[0, 0, 1, 0], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1]], float))
    m.apply_translation((0, 0, RAIL_H))
    return m


def for_printing(m: trimesh.Trimesh) -> trimesh.Trimesh:
    """Upside down: the display top on the plate, rails pointing up.

    The top face gets the plate's texture, the rails need no support, and the
    only bridged surface is the underside, which nobody sees.
    """
    p = m.copy()
    p.apply_transform(trimesh.transformations.rotation_matrix(np.pi, (1, 0, 0)))
    p.apply_translation(-p.bounds[0])
    return p


def figure(x, y, z, size) -> trimesh.Trimesh:
    base_d, height = size
    base = trimesh.creation.cylinder(radius=base_d / 2, height=4, sections=48)
    base.apply_translation((x, y, z + 2))
    body_h = height * 0.62
    body = trimesh.creation.cylinder(radius=base_d * 0.22, height=body_h, sections=32)
    body.apply_translation((x, y, z + 4 + body_h / 2))
    head = trimesh.creation.icosphere(subdivisions=2, radius=base_d * 0.2)
    head.apply_translation((x, y, z + 4 + body_h + base_d * 0.18))
    return trimesh.util.concatenate([base, body, head])


def layout(columns: list[int], sizes: list[tuple], front_row: list | None):
    """Columns of stacked blocks, front to back, two blocks wide, against the back glass.

    Returns blocks, figures, and the height of each row's display surface.
    """
    one = block()
    x0 = (SHELF_W - 2 * W) / 2
    y_back = SHELF_D - D * len(columns)
    blocks, figs, tops = [], [], []
    for row, count in enumerate(columns):
        y = y_back + row * D
        for side in range(2):
            for level in range(count):
                b = one.copy()
                b.apply_translation((x0 + side * W, y, level * H))
                blocks.append(b)
        top = RAIL_H + count * H
        tops.append(top)
        for i in range(4):
            figs.append(figure(x0 + W * (i + 0.5) / 2, y + D / 2, top, sizes[row]))
    if front_row:
        for i, size in enumerate(front_row):
            figs.append(figure(55 + i * 93, y_back / 2, 0, size))
    return trimesh.util.concatenate(blocks), trimesh.util.concatenate(figs), tops, len(blocks)


def main() -> None:
    one = block()
    one.export("stack_block.stl")
    flat = for_printing(one)
    flat.export("stack_block_print.stl")

    out = Path("preview/stack")
    out.mkdir(parents=True, exist_ok=True)
    flat.export(out / "print.stl")

    # Big figures: glass front row, then 1 and 2 blocks high.
    a = layout([1, 2], [MEDIUM, LARGE], [SMALL, MEDIUM, SMALL, MEDIUM])
    # More tiers for smaller figures: 1, 2 and 3 blocks high, no glass row.
    b = layout([1, 2, 3], [SMALL, MEDIUM, MEDIUM], None)
    for name, (blocks, figs, tops, n) in (("two", a), ("three", b)):
        blocks.export(out / f"{name}_blocks.stl")
        figs.export(out / f"{name}_figs.stl")
        print(f"layout {name}: {n} blocks, display heights {[round(t) for t in tops]} mm")

    print(f"block: {[round(v, 1) for v in one.bounding_box.extents]} mm, watertight={one.is_watertight}")
    print(f"print orientation: {[round(v, 1) for v in flat.bounding_box.extents]} mm")
    print(f"stack pitch: {H:.0f} mm per block (rail {RAIL_H} mm sits in a {GROOVE_D} mm groove)")
    for blocks_high, fig in ((2, LARGE), (3, LARGE), (3, MEDIUM)):
        top = RAIL_H + blocks_high * H + fig[1]
        print(f"  {blocks_high} high + {fig[1]:.0f} mm figure = {top:.0f} mm, "
              f"{SHELF_H - top:.0f} mm under the {SHELF_H:.0f} mm gap")


if __name__ == "__main__":
    main()
