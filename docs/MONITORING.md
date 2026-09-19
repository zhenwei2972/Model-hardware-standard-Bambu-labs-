# Watching a print, and reporting on it

A print is a sequence, not an event. The moments worth a photograph are few and
predictable, and they are not evenly spaced in value: the first layer is worth
more than all the others put together, because adhesion, squish, warping and a
shifted origin are visible there and nowhere else so cheaply. By the time a
failure is obvious in a frame at 80 %, an hour of filament has gone.

This is the tooling for that: a watcher that photographs the stages, a place to
write down what each frame shows, and a report that puts the two together
alongside what the slicer predicted.

## The stages

| Stage | Fires when | Why the frame is worth having |
| --- | --- | --- |
| `start` | the job becomes active | Confirms the plate was clear and the right file started |
| `first_layer` | the printer moves to layer 2 | Adhesion, squish, warping, a shifted origin |
| `quarter` | 25 % of the layers are down | Early enough that stopping still saves most of the filament |
| `half` | 50 % | Layer shift, the beginning of stringing |
| `three_quarters` | 75 % | Overhangs and top surfaces are usually underway |
| `finished` | the state becomes finished | The result, before the plate is touched |
| `failed` | the state becomes failed | Whatever it ended as, for diagnosis |
| `paused` | the state becomes paused | Usually a runout or a detected defect |
| `alert` | a new HMS code appears | Pairs the machine's own error code with what it looks like |

`mhs monitor --list-stages` prints the same table, and `list_print_stages`
returns it to the model.

### Each stage fires exactly once

Crossings are computed from the *transition* between two observations, not from
the current value. A printer that reports `paused` for two hundred consecutive
polls produces one `paused` record, and a `first_layer` that has long since
passed is not re-photographed at layer 90. The converse also holds: a poll slow
enough to step from layer 2 straight to layer 80 still records `quarter`, `half`
and `three_quarters`, because the interval is tested, not the endpoint.

The one deliberate exception is `alert`, which is keyed by the fault itself — a
second, different HMS code during the same print is captured separately, while a
standing fault is not recaptured every poll.

Two consequences worth knowing. A watcher started *mid-print* has nothing to
compare its first observation against, so it records `start` and picks up
crossings from there; a first layer that happened before you started watching is
gone, and the report says so rather than inventing it. And a stage whose frame
could not be taken — camera off, LAN Mode Liveview disabled — is still recorded
as text, because knowing the first layer finished at 14:02 is worth having even
without the picture.

## The loop

```
start_print(file=..., confirm=True)      -> run_id
watch_print(run_id=...)                  -> polls in the background
review_print_stages(run_id=...)          -> what was captured; which frames have
                                            no caption yet
read_capture(path)                       -> look at one
caption_print_stage(observation_id, "…") -> write down what is actually there
print_report(run_id=...)                 -> the whole run
```

Captioning is the model's job on purpose. The driver takes the photograph;
reading it is what a model is for, and storing the reading as text is what makes
the report survive. Images leave the context window; `"first layer down, corners
flat, slight gap between the outer two perimeters on the left"` does not, and it
is what the next run's settings should be argued from.

Caption what is **there**, not what ought to be. "Looks good" is worth nothing
three prints later.

## Worked example

Against the simulated printer (`MHS_MOCK=1`), with the mock's clock driven
forward so the whole print happens in a second — which is why the duration below
reads as zero minutes and the drift as −100 %:

```console
$ mhs stages --run 1
#1    2026-09-19T15:07:24+00:00  start           -
      coin-tray started
      ~/.local/share/mhs/captures/a1mini/20260919-150724-stage-start.jpg
#2    2026-09-19T15:07:24+00:00  first_layer     layer 2
      layer 1 complete, now on layer 2
      ~/.local/share/mhs/captures/a1mini/20260919-150724-stage-first_layer.jpg
...

$ mhs caption 2 "first layer down, corners flat, slight gap between the outer two perimeters on the left"
#2 first_layer: first layer down, corners flat, slight gap between the outer two perimeters on the left

$ mhs report 1
run 1: coin-tray, success, 0 min, under the estimate by 100%, 6 frame(s) captured, 4 awaiting a caption

  2026-09-19T15:07:24+00:00  start           coin-tray started
                                             ~/.local/share/mhs/captures/a1mini/20260919-150724-stage-start.jpg (uncaptioned)
  2026-09-19T15:07:24+00:00  first_layer     layer 1 complete, now on layer 2
                                             "first layer down, corners flat, slight gap between the outer two perimeters on the left"
  2026-09-19T15:07:25+00:00  quarter         layer 32 of 120
                                             ~/.local/share/mhs/captures/a1mini/20260919-150724-stage-quarter.jpg (uncaptioned)
  ...
  2026-09-19T15:07:25+00:00  finished        the printer reports the job finished
                                             "finished and clean; one whisker of stringing on the tallest column"

time: 0 min actual vs 54 min predicted - under the estimate by 100%
```

## What the report contains

`print_report(run_id)` returns:

* **`timeline`** — every stage in order, with its layer, its detail, its frame
  path and its caption.
* **`settings`** — what the run was started with, including the slice settings
  when it came through `slice_and_print`.
* **`time_vs_estimate`** — actual minutes against the slicer's own prediction,
  as a percentage drift with a plain-English note. Consistent drift in one
  direction is the cheapest signal that a profile's speeds do not match the
  machine.
* **`uncaptioned_images`** — frames nobody has read yet. Caption them and call
  again.
* **`missing_milestones`** — the key stages (`start`, `first_layer`, `finished`)
  that were never captured. A report that quietly omitted them would read like a
  clean run.
* **`outcome`, `quality_score`, `notes`** — whatever `log_print_result` recorded.
* **`summary`** — one line, for when that is all anyone wants.

## Practical notes

* **One watcher per printer.** Starting a second replaces the first, rather than
  photographing every milestone twice.
* **Polling is slow on purpose** — 20 s by default (`[monitor] poll_seconds` in
  `config.toml`, or `MHS_MONITOR_POLL_SECONDS`). A print is slow; polling faster
  only costs the printer's MCU. `watch_print` accepts 2–600 s per call and
  refuses anything outside that.
* **Frames land in the capture directory** next to every other snapshot, named
  `<timestamp>-stage-<milestone>.jpg`, and are indexed in the SQLite journal, so
  they outlive the process.
* **The watcher stops with the server.** Over stdio that means it lives as long
  as the client session; `mhs monitor` in a terminal runs it in the foreground
  until Ctrl-C.
* **Nothing reacts automatically.** The watcher records; it never pauses or
  stops a print on its own. Acting on a failure is a decision, and decisions go
  through `pause_print`/`stop_print` with `confirm=True` like every other
  physical action. See [ROADMAP.md](ROADMAP.md).
