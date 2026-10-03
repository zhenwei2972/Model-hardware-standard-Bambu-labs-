# Detolf display riser

The worked example from [docs/CASE_STUDY_DETOLF.md](../../docs/CASE_STUDY_DETOLF.md).

```bash
pip install trimesh manifold3d shapely numpy
python3 stack_riser.py        # stack_block.stl, stack_block_print.stl, preview/stack/*.stl
blender -b -P render_detolf.py -- renders   # the cabinet renders (needs the step above)
```

`stack_block_print.stl` is already upside down for printing: display face on
the plate, rails up. Slice it with `slice_model` or Bambu Studio, then run a
preflight before printing.
