# Example: a print report

This is a real report from a real print: a bracelet display stand (two parts, black PLA) on a Bambu Lab
A1 mini on 13 October 2026. It was produced by the report tool, unedited apart from this introduction and
the notes at the end.

**How to get one for your own print.** Ask Claude *"Give me the print report for my last print"*, or run
`mhs report <run number> --markdown`. During the print, `watch_print` takes a photo at each stage; Claude
then looks at each photo and writes down what it sees, and the report puts it all together.

The camera photos are not included here because they show the room around the printer. Each row below
says what was visible in its photo.

---

# Print report: bracelet stand

| | |
|---|---|
| Printer | a1mini |
| File | bracelet_stand_a1mini.gcode.3mf |
| Started | 2026-10-13T13:08:22+08:00 |
| Ended | 2026-10-13T17:11:37+08:00 (from the watcher's final frame) |
| Outcome | finished |
| Time | 243 min actual (MEASURED) vs 173 min slicer estimate (ESTIMATED): over the estimate by 40% (includes time paused, so not like for like) |

## Interruptions

- 2026-10-13T13:40:57+08:00, layer 15: paused at layer 15
- 2026-10-13T13:41:00+08:00, layer 15: PRINT_ERROR_0300_8015: Code not in the bundled table (motion controller: heaters, fans, motors, homing); see the wiki link
- 2026-10-13T14:20:03+08:00, layer 15: HMS_12FF_2000_0002_0002: Code not in the bundled table; see the wiki link
- 2026-10-13T14:20:06+08:00, layer 15: PRINT_ERROR_12FF_C006: Code not in the bundled table; see the wiki link
- 2026-10-13T14:20:28+08:00, layer 15: PRINT_ERROR_12FF_8007: Code not in the bundled table; see the wiki link

## What the camera saw

| When | Stage | Layer | What is visible |
|---|---|---|---|
| 13:08:49 | start | 267 | Job started 13:08 (frame not reviewed individually; plate was checked clear in the preflight frame at 13:07). |
| 13:21:55 | first_layer | 2 | Bed is at the back and in shadow: the first layer is not clearly visible in this frame. |
| 13:40:57 | paused | 15 | Paused at layer 15: both parts visible with their sparse infill grid exposed (display bar at left, base at right). Nothing detached or tangled on the plate. |
| 13:41:00 | alert | 15 | Same state as the pause, 3 s later: layer 15 of both parts, infill exposed, plate otherwise clean. Code 0300_8015 is not in Bambu's published table. |
| 14:20:03 | alert | 15 | Not reviewed separately: taken 25 s before frame 64, during the same filament prompts. 12FF codes are not in Bambu's table; the matching 07FF code means 'external filament is missing, load new filament'. |
| 14:20:06 | alert | 15 | Not reviewed separately: taken 22 s before frame 64. The matching 07FF code means 'feed filament into the PTFE tube'. |
| 14:20:28 | alert | 15 | 40 min after the pause, still at layer 15, parts unchanged and intact. The matching 07FF code means 'observe the nozzle; if filament has been extruded, select Done' - the last step of loading filament. |
| 16:07:11 | quarter | 169 | Printing resumed and continued: the display bar is complete with its rounded top and end stop; the base is done and the post is growing. Black filament, no visible step at the resume height from this angle. |
| 16:28:53 | half | 335 | Halfway: bar complete, post about half its height on the base, straight and centred. No stringing visible. |
| 16:50:55 | three_quarters | 505 | Three quarters: post near full height, straight walls; bar complete beside it. No defects visible. |
| 17:11:37 | finished | 670 | Finished: base and post standing with the tenon on top, upright and intact. The bar is out of frame at the front of the plate in this shot (seen complete in earlier frames). |

_run 15: bracelet stand, finished, 243 min, over the estimate by 40% (includes time paused, so not like for like), 11 frame(s) captured_

---

## What this run shows

- **Why the print took 40% longer than the estimate.** The printer paused at layer 15 for about 40
  minutes. The codes that followed are the steps for loading new filament on an A1 mini, so the spool
  most likely ran out or jammed and someone reloaded it. That last part is inferred: Bambu's published
  list does not include these A1-series codes.
- **The pause did no visible harm.** Photos before and after the pause show both parts intact, and
  printing then ran to the end.
- **Fixed since this run:**
  - The start photo now records layer 0. Above, it shows 267, the previous job's layer count.
  - The A1 mini's `12FF` codes are now explained, read through the matching standard `07FF` codes and
    labelled as inferred. Above, they show as "not in the bundled table".
  - Prints sent from Claude now keep the slicer's time estimate. It was added to this older run by
    reading its sliced file.
