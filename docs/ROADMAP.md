# Roadmap

Ordered by how much each adds to "Claude can run my printer well", not by
difficulty.

## Next

* **Acting on what the camera shows.** `watch_print` now captures a frame at each
  stage and `print_report` assembles the run, but nothing reacts: the model has
  to be asked to look. Waking the model at a milestone - MCP sampling, or a
  resource-update notification - and letting it pause the job on a confident
  failure verdict is the remaining half. The pieces (`pause_print`, captions,
  the journal) are all here.
* **Cross-run learning.** The journal holds slice settings, stage captions and
  outcomes for every print. Nothing yet reads across runs to say "the last three
  prints at 0.28 mm all show the same first-layer gap"; a tool that does would
  turn the record into advice.
* **Network discovery.** MHS describes devices and agents finding each other
  across a network. Printers are currently named in config; advertising over
  mDNS and discovering them would close that gap (see
  [MHS_ALIGNMENT.md](MHS_ALIGNMENT.md)).
* **Push instead of poll.** The driver already receives MQTT events; surfacing
  them as MCP resource-update notifications would let a client react to a state
  change without polling `get_status`.

## Later

* **Vacuum scheduling.** The scheduler is printer-shaped today (it queues a
  file). Generalising it to "clean the kitchen at 09:00 on weekdays" is mostly
  a matter of making the job payload a device command rather than a path.
* **Obstacle photos.** The Saros reports obstacle snapshots; surfacing them the
  way `capture_snapshot` surfaces the printer camera would let a model see what
  the robot refused to drive over.

* **More drivers.** A Roborock vacuum now shares the device layer with the
  printers, which is what proved the abstraction was not merely printer-shaped.
  Moonraker/Klipper and OctoPrint are the obvious next two for printers; for
  vacuums, Dreame and Ecovacs have comparable community libraries.
* **X1/H2 camera.** RTSPS on port 322 instead of the tcp/6000 JPEG stream;
  needs an RTSP client and a frame grabber.
* **Recurring schedules.** Cron-style repeats ("every weekday at 07:00") on top
  of the existing one-shot queue.
* **Filament awareness.** Cross-check the AMS trays against what the 3mf asks
  for and refuse a job whose material is not loaded.
* **Real thin-wall analysis.** Voxelise or compute a medial axis, replacing the
  edge-length and volume-to-area proxies in `printability.py` with a true
  minimum-feature measurement.
* **Camera undistortion.** A one-off calibration against a printed checkerboard
  would remove the perspective error that currently limits measurements to a few
  percent, and would let a single calibration cover the whole plate rather than
  one plane near the reference object.
* **Multi-printer dispatch.** `print_anywhere(file)` picking the first idle
  printer with the right filament — the pool and capability checks already
  support it.

## Explicitly out of scope

* Cloud control. LAN only, by design (see docs/PROTOCOL.md).
* Bypassing the Authorization Control System with an extracted Bambu Connect
  certificate. Developer Mode is the supported toggle; use it.
* Unattended printing without supervision. The scheduler makes timing reliable,
  not fire safety.
* Claiming MHS conformance. Until the specification is published, this tracks
  the announcement's described shape and says so.
