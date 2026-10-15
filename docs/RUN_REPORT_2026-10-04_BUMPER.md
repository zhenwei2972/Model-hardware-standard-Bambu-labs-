# Run report: rigid iPhone 17 Pro bumper, end-to-end test of the server

Runs 12 and 13 on the A1 mini, 4 October 2026, followed up on 6 October.
The aim was to drive one real part through the whole flow - design, model
checks, slicing, preflight, printing, watching, reporting - and write down
everything the flow got wrong as well as right.

Numbers are labelled **MEASURED** (printer, run journal, camera timestamps) or
**ESTIMATED** (slicer, or computed from the model).

## Outcome in one paragraph

The part printed cleanly: 23 min of printing (MEASURED, start frame 22:41:37 to
finished frame 23:04:50) against a slicer estimate of 25 min 53 s. The flow
also exposed one serious bug. **The server could restart a print on its own.**
It sent commands at MQTT QoS 1, and the MQTT library re-sends every
unacknowledged QoS 1 message after each reconnect. The run journal caught it
happening: at 00:55, 1 h 50 min after the bumper finished and with the plate
empty, the printer started a job nobody had sent. That is fixed and tested: commands are now sent at QoS 0, and the printer's own
report confirms each one. Eight smaller gaps are listed below.

## 1. The design (v2)

The earlier flex-on bumper had a cut through its bottom wall and failed there.
v2 is a closed ring, so nothing can open:

| | v1 (C-cut) | v2 (closed ring) |
|---|---|---|
| Wall | 1.4 mm, cut at the bottom | 2.0 mm, closed - about 2.9x the bending stiffness (ESTIMATED) |
| Bottom openings | one 46 mm slot | USB-C plus two speaker slots, with solid webs between |
| Fitting | spread the C open | press in from the back past two ramped lips; walls bow 0.82 mm, about 0.16% strain (ESTIMATED) |
| Edges | square | 45 degree bevels: 0.8 mm outside, 0.4 mm round the openings |

Model checks before printing: the seated phone overlaps the ring by 0 mm3, and
pressing it in from the back, the only contact is with the two lips.

## 2. The flow, step by step

| Step | Tool | Result |
|---|---|---|
| Is the printer set up? | `check_setup` | ready; textured plate, PLA loaded |
| Model check | `analyze_model` | printable; **false warning** that "39% of detail is finer than the nozzle" (see gap 7) |
| Look at it | `preview_model` | no supports needed; the 45 degree bevel on the plate side flagged at the limit |
| Trade-offs | `compare_slice_intents` | speed 31 min, balanced 27, quality 50, strong 26 (ESTIMATED); filament reported as null for all (gap 6) |
| Slice | `slice_model` (strong, 5 walls) | 26 min 19 s; chamfered version 25 min 53 s, 10.41 g (ESTIMATED; the gram figure is from the file header) |
| Preflight, attempt 1 | `preflight_record.py` | 8/8 pass, printed run 12 |
| Stop for the chamfer | `stop_print` | stopped 45 s in, during calibration |
| Preflight, attempt 2 | `preflight_record.py` | **blocked**: a fatal force-sensor alert (HMS 0300-1800-0001-0003) latched by the stop |
| Clear it | screen, then `clean_print_error` via the driver | alert stayed in the network status; a power cycle cleared it |
| After the power cycle | - | the printer was running run 12's job again on its own (the bug in section 3) |
| Preflight, attempt 3 | `preflight_record.py` | 8/8 pass (plate checked by hand: the camera could not see it); printed run 13 |
| Watch | `watch_print` | 7 frames: start, first layer, 1/4, 1/2, 3/4, finished, and a "failed" 1 h 50 min later |
| Report | `caption_print_stage`, `print_report`, `log_print_result` | all captioned; outcome success, score 6/10 after your fitting feedback |

## 3. The phantom restart

**Evidence.** Two times a print started with no command behind it:

* On 4 October, after the power cycle, the printer was running "rigid ring"
  (run 12). That job had been stopped, and a stopped job is not something the
  printer's power-loss recovery resumes.
* At 00:55 on 5 October the watcher for run 13 recorded a job that started and
  failed at layer 0, 1 h 50 min after the bumper finished. Its frame shows an
  empty plate.

**Cause.** `BambuMqtt.publish()` defaulted to QoS 1, and every command,
including `project_file` (start print) and `stop`, went through it. paho 2.1
keeps each QoS 1 message until the broker acknowledges it, and on every
reconnect publishes the unacknowledged ones again
(`Client._messages_reconnect_reset_out`). The server process from 4 October
stayed connected for 14 more hours. It held start rigid ring, stop, stop and
start chamfered, so any Wi-Fi drop, printer reboot or keepalive timeout sent
them all again; the last one restarts the chamfered case. Had the finished
part still been on the plate, the nozzle would have printed into it.

**Fix.** Commands now go out at QoS 0, and the printer's own report echoing the
`sequence_id` remains the acknowledgement (`publish_and_wait` already waited
for it). New tests check that `project_file`, `stop` and `pause` leave nothing
queued for resending (they fail on the old code), and that `publish` defaults
to QoS 0. Suite: 489 passed; the 2 failures in `test_config` also fail on the
base branch (they trip over a local `config.toml`). The running server was
restarted, so it is on the fixed code.

## 4. Smaller gaps found

| # | Gap | Effect | Suggested fix |
|---|---|---|---|
| 1 | Alerts arrive undecoded (`message: null`) | had to look the codes up on the web | ship an HMS code table, or fetch Bambu's |
| 2 | `clean_print_error` exists but no tool calls it, though `check_setup` tells you to | needed a hand-written driver script | expose it as a tool |
| 3 | Stopping during start-up calibration latched a fatal force-sensor alert that survived the screen dismissal | needed a power cycle | warn before stopping during calibration; document the power cycle |
| 4 | `print_report` duration is measured to the time the result was logged | showed 1,543 min for a 23 min print | end the run at the finished or failed milestone |
| 5 | The watcher keeps watching after "finished" | the phantom job was filed under run 13 | stop at the first terminal milestone, or open a new run when the job changes |
| 6 | Slicing results return `filament_g: null` | the gram count sits unread in the file header | parse "total filament weight" |
| 7 | `analyze_model` judges detail by mesh edge length | smooth curves made of tiny triangles count as "detail too fine" | measure feature size, not edge length |
| 8 | The first-layer frame shows nothing useful | twice now: bed at the front, edge-on, dark filament on a dark plate | take several frames and keep the one with the bed at the back |

Also seen: every Claude Code session starts its own server process, and each
holds its own MQTT connection to the printer. Before the fix, each of those
could re-send its own queue.

## 5. What changes in v3

From handling the printed v2:

| Your feedback | Cause in v2 | v3 |
|---|---|---|
| Raised sharp edge near the middle of the back | the back lips ended in a knife edge (about 42 degrees) with square ends; next to them the camera-plateau guard rose 2.55 mm in one square step | rounded lip nose (r 0.25), reach 0.55 mm, 5 mm tapers at each end; the guard ramps down over 14 mm, bevelled along the ramp |
| Buttons hard to press | 2.2 mm deep straight-sided cutouts | openings flare at 45 degrees through the outer 1.4 mm; one opening for both volume buttons; 1.2 mm padding along each button |
| Kickstand | - | U-shaped stand hinged on the left wall, which thickens to 4.0 mm there. It lies on the back glass 0.4 mm clear and opens to a hard stop at about 46 degrees, standing the phone in landscape with the screen about 53 degrees to the table (ESTIMATED). Clips on and off by squeezing the arms (about 0.33% strain, ESTIMATED). |

Model checks for v3, all passing: seated phone overlap 0 mm3; stand closed
clears phone and ring (0 mm3); press-in contact only at the lips (wall bow
0.65 mm, 0.13% strain, ESTIMATED); the stand's weight falls 30% of the way
between its two feet, so it neither tips forward nor back.

Sliced as one plate (ring screen-down, stand flat beside it): 33 min 32 s and
12.92 g (ESTIMATED). The file passes every file check: model id, plate, bed
temperature, layer height, start macro, toolpath inside the plate, first layer
clear of the purge line. The printer checks wait until the printer is back on.

Caveat: closed, the stand puts 2 mm bars across the lower back. That may weaken
a MagSafe charger's hold; the stand clips off.
