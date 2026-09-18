# The design loop

Sizing a part against a real object, checking what the printer can reproduce,
printing it, and measuring the result — with the tool calls that do each step.

The running example is the one that motivated this: *"it should be as big as
this coin."*

---

## 1. Look at the model, and measure it

```
analyze_model(path="~/models/badge.stl")
```

```json
{
  "model": {"dimensions_mm": {"x": 60.0, "y": 60.0, "z": 4.2}, "volume_mm3": 8021.5,
            "triangles": 48216, "watertight": true},
  "verdict": "At a 0.4 mm nozzle and 0.2 mm layers, A1 mini resolves about 0.4 mm in XY
              and 0.2 mm in Z. About 7% of its detail is below that threshold and will
              soften. 0 blocker(s), 1 warning(s).",
  "printability": {"findings": [...], "metrics": {"detail_below_nozzle_percent": 7.1, ...}}
}
```

```
preview_model(path="~/models/badge.stl")
```

Returns an image: four orthographic views at a **shared** scale (so the views are
comparable), a millimetre scale bar in each, and faces overhanging more than 45°
tinted red. Overhangs face downwards, so pass `views=["bottom", "iso"]` when the
question is supports.

## 2. Fix the scale against something real

Put the coin flat on the plate, next to where the part will print.

```
camera_grid()                       → a frame with a labelled pixel grid
```

Read the coin's span off the grid — say it runs from x=410 to x=498, so 88 px.

```
camera_calibrate(reference="sgd_1", pixel_length=88)
```

```json
{"calibration": {"mm_per_pixel": 0.2801, "reference_name": "Singapore $1"},
 "example": "1 mm is now 3.6 px; the 180 mm plate would span 643 px."}
```

`mhs://reference-objects` lists the sizes this knows: coins in several
currencies, a credit card, 1.75 mm filament, the A1 mini's own plate. Any
diameter in mm works too.

The calibration is saved per printer, and only holds for that camera position
and that plane.

## 3. Scale, and see what it costs

Dry run first — this is the step where detail dies:

```
scale_model(path="~/models/badge.stl", target_mm=24.65)
```

```json
{"factor": 0.4108, "dimensions_after_mm": [24.65, 24.65, 1.73],
 "detail_below_nozzle_percent_before": 7.1,
 "detail_below_nozzle_percent_after": 34.8,
 "note": "Scaling by 0.411x moves +28 percentage points of the model's detail across
          the 0.4 mm printable threshold.",
 "applied": false}
```

A third of the detail has gone below what a 0.4 mm nozzle can draw. The options
are now explicit: accept a softer part, fit a 0.2 mm nozzle, simplify the
geometry, or split the difference on size. Decide, then:

```
scale_model(path="~/models/badge.stl", target_mm=24.65, apply=true)
```

writes a scaled STL and re-runs the printability check on it.

## 4. Slice, upload, print

Slicing happens in Bambu Studio or OrcaSlicer — this server does not slice.
Export the plate, then:

```
upload_and_print(local_path="~/Downloads/badge_plate.3mf", confirm=true)
```

or queue it for later:

```
schedule_print(file="cache/badge_plate.3mf", when="2026-09-19T06:30", window_minutes=90)
```

Scheduled jobs only fire on an idle, alert-free printer, and expire rather than
starting hours late.

## 5. Watch it

```
get_status()        → state, layer, progress, temperatures, decoded alerts
capture_snapshot()  → the frame itself, for the model to look at
capture_frames(count=6, interval_seconds=120)  → a series, for comparing over time
```

First-layer adhesion, stringing and spaghetti are all visible in a frame; layer
shift usually needs two frames a few minutes apart.

## 6. Measure the result, and close the loop

Leave the coin on the plate:

```
camera_grid()
camera_measure(point_a=[612, 388], point_b=[700, 388], target_mm=24.65)
```

```json
{"pixels": 88.0, "millimetres": 24.65, "annotated_image": "...measure.png",
 "comparison": {"error_mm": 0.0, "error_percent": 0.0,
                "assessment": "within 2% - at the limit of what this camera can resolve,
                               treat as on target"},
 "caveat": "The chamber camera is off-axis and has lens distortion..."}
```

The measurement is drawn back onto the frame so the reading can be checked
rather than trusted. If it is off by more than a few percent,
`suggested_rescale_factor` is the correction to apply.

Then record what happened, so the next iteration starts from evidence:

```
log_print_result(run_id=7, outcome="partial", quality_score=6,
                 notes="lettering below 0.5 mm merged; corners lifted slightly")
list_print_history()   → what was tried, at what settings, and how it scored
```

---

## What to trust

| Measurement | Trust | Why |
| --- | --- | --- |
| Mesh dimensions, volume, triangle count | exact | read straight off the geometry |
| Overhang area, bed contact, layer count | reliable | direct consequences of the geometry and layer height |
| "% of detail below the nozzle" | indicative | edge length is a proxy for feature size, not a medial-axis analysis |
| Characteristic wall thickness | rough | a global volume-to-area ratio; it catches thin *parts*, not every thin *region* |
| Camera measurements | a few percent | off-axis camera, lens distortion, reference must be in the same plane |
| Mass and filament estimates | rough | the slicer's numbers are authoritative |

Where a number is approximate, the tool says so in its result. Use calipers when
a dimension actually matters.
