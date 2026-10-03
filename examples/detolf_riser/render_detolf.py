"""Render the stack blocks inside a full IKEA Detolf cabinet with Blender.

Run headless:
  /Applications/Blender.app/Contents/MacOS/Blender -b -P render_detolf.py -- OUTDIR

Cabinet (IKEA's listing): 43 x 37 x 163 cm, glass on four sides, four glass
display levels. Interior per level (owners' measurements): about 390 x 330 mm,
with 394 / 387 / 387 / 381 mm between shelves, bottom to top.

Model units are millimetres, scaled 0.001 so Blender works in metres.
Coordinates match stack_riser.py: x across the shelf, y from the front glass
backward, z up; the shelf's front-left corner is the origin of a level.
"""

import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

OUT = Path(sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "renders")
OUT.mkdir(parents=True, exist_ok=True)
HERE = Path(__file__).resolve().parent
S = 0.001                                   # mm -> m

W, D, H = 430.0, 370.0, 1630.0              # outside
IN_W, IN_D = 390.0, 330.0                   # usable glass shelf
X0, Y0 = (W - IN_W) / 2, (D - IN_D) / 2     # interior origin inside the cabinet
PLINTH, TOP, GLASS = 45.0, 25.0, 5.0
GAPS = [394.0, 387.0, 387.0, 381.0]         # bottom to top
LEVEL = 2                                   # third level from the bottom: eye height

# --- scene ---------------------------------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def material(name, rgb, rough=0.5, transmission=0.0, alpha=1.0, metallic=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metallic
    for key in ("Transmission Weight", "Transmission"):
        if key in bsdf.inputs:
            bsdf.inputs[key].default_value = transmission
            break
    if "IOR" in bsdf.inputs:
        bsdf.inputs["IOR"].default_value = 1.45
    bsdf.inputs["Alpha"].default_value = alpha
    return m


GLASS_M = material("glass", (0.92, 0.97, 0.98), rough=0.02, transmission=1.0)
FRAME_M = material("frame", (0.06, 0.05, 0.045), rough=0.55)
PLA_M = material("pla", (0.13, 0.13, 0.14), rough=0.45)              # the real print is dark grey
FIG_M = material("figure", (0.78, 0.80, 0.83), rough=0.35)
FLOOR_M = material("floor", (0.86, 0.84, 0.80), rough=0.8)
WALL_M = material("wall", (0.93, 0.92, 0.90), rough=0.9)
DIM_M = material("dimension", (0.85, 0.25, 0.08), rough=0.4)
_e = next(n for n in DIM_M.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
for key in ("Emission Color", "Emission"):
    if key in _e.inputs:
        _e.inputs[key].default_value = (0.95, 0.35, 0.08, 1)
        break
_e.inputs["Emission Strength"].default_value = 2.0


def cube(name, x0, y0, z0, x1, y1, z1, mat):
    bpy.ops.mesh.primitive_cube_add(size=1)
    o = bpy.context.object
    o.name = name
    o.scale = ((x1 - x0) * S, (y1 - y0) * S, (z1 - z0) * S)
    o.location = ((x0 + x1) / 2 * S, (y0 + y1) / 2 * S, (z0 + z1) / 2 * S)
    o.data.materials.append(mat)
    return o


# Cabinet: frame top and bottom, glass on four sides, glass shelves.
cube("plinth", 0, 0, 0, W, D, PLINTH, FRAME_M)
cube("top", 0, 0, H - TOP, W, D, H, FRAME_M)
cube("glass_left", 6, 6, PLINTH, 10, D - 6, H - TOP, GLASS_M)
cube("glass_right", W - 10, 6, PLINTH, W - 6, D - 6, H - TOP, GLASS_M)
cube("glass_back", 6, D - 10, PLINTH, W - 6, D - 6, H - TOP, GLASS_M)
cube("glass_door", 6, 2, PLINTH + 2, W - 6, 6, H - TOP - 2, GLASS_M)
# Thin dark corner posts, as on the real cabinet's edges.
for cx in (0, W - 8):
    for cy in (0, D - 8):
        cube(f"post_{cx:.0f}_{cy:.0f}", cx, cy, PLINTH, cx + 8, cy + 8, H - TOP, FRAME_M)

level_z, z = [], PLINTH
for i, gap in enumerate(GAPS):
    level_z.append(z)
    z += gap
    if i < len(GAPS) - 1:
        cube(f"shelf_{i + 1}", X0, Y0, z, X0 + IN_W, Y0 + IN_D, z + GLASS, GLASS_M)
        z += GLASS


def import_stl(path, name, mat, offset):
    before = set(bpy.data.objects)
    try:
        bpy.ops.wm.stl_import(filepath=str(path))
    except AttributeError:
        bpy.ops.import_mesh.stl(filepath=str(path))
    o = next(iter(set(bpy.data.objects) - before))
    o.name = name
    o.scale = (S, S, S)
    o.location = (offset[0] * S, offset[1] * S, offset[2] * S)
    o.data.materials.clear()
    o.data.materials.append(mat)
    bpy.ops.object.shade_flat()
    return o


# The design, placed on its level exactly as stack_riser.py laid it out.
base = (X0, Y0, level_z[LEVEL])          # level_z is already the top of the glass
import_stl(HERE / "preview/stack/two_blocks.stl", "stack_blocks", PLA_M, base)
import_stl(HERE / "preview/stack/two_figs.stl", "figures", FIG_M, base)

# Room: floor and back wall, so the glass has something to show.
cube("floor", -1500, -2500, -10, 1900, 1200, 0, FLOOR_M)
cube("wall", -1500, D + 40, 0, 1900, D + 60, 2600, WALL_M)

# --- dimension marks for the close-up ---------------------------------------
dims = []


def dim_bar(name, a, b, label, label_at, size=22):
    a, b = Vector(a) * S, Vector(b) * S
    bpy.ops.mesh.primitive_cylinder_add(radius=1.6 * S, depth=(b - a).length)
    bar = bpy.context.object
    bar.name = name
    bar.location = (a + b) / 2
    bar.rotation_mode = "QUATERNION"
    bar.rotation_quaternion = Vector((0, 0, 1)).rotation_difference((b - a).normalized())
    bar.data.materials.append(DIM_M)
    bpy.ops.object.text_add(location=Vector(label_at) * S)
    t = bpy.context.object
    t.name = name + "_label"
    t.data.body = label
    t.data.size = size * S
    t.data.align_x = "CENTER"
    t.rotation_euler = (math.radians(90), 0, 0)              # face the front (-y)
    t.data.materials.append(DIM_M)
    dims.extend([bar, t])


lz = base[2]
top_gap = level_z[LEVEL] + GAPS[LEVEL]
dim_bar("dim_width", (X0, Y0 - 4, lz + 2), (X0 + IN_W, Y0 - 4, lz + 2),
        "390 mm shelf", (X0 + IN_W / 2, Y0 - 6, lz - 30))
dim_bar("dim_blocks", (X0 + 19, Y0 - 4, lz - 52), (X0 + 19 + 352, Y0 - 4, lz - 52),
        "2 blocks side by side = 352 mm (19 mm spare each side)", (X0 + IN_W / 2, Y0 - 6, lz - 80), size=17)
dim_bar("dim_height", (X0 + IN_W - 6, Y0 - 4, lz), (X0 + IN_W - 6, Y0 - 4, top_gap),
        "", (0, 0, 0))
headroom = GAPS[LEVEL] - (2 + 2 * 50 + 210)        # 2 blocks high + a 210 mm figure
bpy.ops.object.text_add(location=Vector((X0 + IN_W / 2, Y0 - 6, top_gap - 30)) * S)
t = bpy.context.object
t.data.body = f"{GAPS[LEVEL]:.0f} mm to the shelf above, {headroom:.0f} mm spare"
t.data.size = 17 * S
t.data.align_x = "CENTER"
t.rotation_euler = (math.radians(90), 0, 0)
t.data.materials.append(DIM_M)
dims.append(t)

# --- light, camera, render ---------------------------------------------------
world = bpy.data.worlds.new("world")
scene.world = world
world.use_nodes = True
bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
bg.inputs["Color"].default_value = (0.78, 0.80, 0.84, 1)
bg.inputs["Strength"].default_value = 0.25

bpy.ops.object.light_add(type="AREA", location=(-1.2, -1.6, 2.4))
key = bpy.context.object
key.data.energy = 140
key.data.size = 1.6
key.rotation_euler = (math.radians(50), 0, math.radians(-35))
bpy.ops.object.light_add(type="AREA", location=(1.4, -1.0, 1.4))
fill = bpy.context.object
fill.data.energy = 40
fill.data.size = 1.2
fill.rotation_euler = (math.radians(70), 0, math.radians(50))
bpy.ops.object.light_add(type="POINT", location=(W / 2 * S, D / 2 * S, (top_gap - 30) * S))
bpy.context.object.data.energy = 4                       # a cabinet lamp over the level

bpy.ops.object.camera_add()
cam = bpy.context.object
scene.camera = cam


def aim(location, target, lens):
    cam.location = location
    direction = Vector(target) - Vector(location)
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    cam.data.lens = lens


scene.render.engine = "CYCLES"
scene.cycles.samples = 96
scene.cycles.use_denoising = True
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "METAL"
    prefs.get_devices()
    for d in prefs.devices:
        d.use = True
    scene.cycles.device = "GPU"
except Exception as exc:  # noqa: BLE001 - CPU is fine, only slower
    print("GPU unavailable, rendering on CPU:", exc)
scene.render.resolution_x, scene.render.resolution_y = 1600, 1200
scene.view_settings.view_transform = "AgX" if "AgX" in [
    i.identifier for i in scene.view_settings.bl_rna.properties["view_transform"].enum_items] else "Filmic"

centre = Vector((W / 2, D / 2, 0)) * S
# 1. The whole cabinet, no dimension marks.
for o in dims:
    o.hide_render = True
scene.render.resolution_x, scene.render.resolution_y = 1200, 1600
aim((-0.95, -2.25, 1.25), (centre.x, centre.y, 0.8), 42)
scene.render.filepath = str(OUT / "detolf_cabinet.png")
bpy.ops.render.render(write_still=True)

# 2. Close-up of the level, with the fit marked - door taken off for the view.
for o in dims:
    o.hide_render = False
bpy.data.objects["glass_door"].hide_render = True
scene.render.resolution_x, scene.render.resolution_y = 1600, 1200
mid_z = (lz + top_gap) / 2 * S
aim((-0.40, -1.10, mid_z + 0.22), (centre.x + 0.01, centre.y, mid_z - 0.07), 36)
scene.render.filepath = str(OUT / "detolf_fit.png")
bpy.ops.render.render(write_still=True)

bpy.ops.wm.save_as_mainfile(filepath=str(OUT / "detolf_scene.blend"))
print("rendered:", OUT)
