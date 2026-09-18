# Roadmap

Ordered by how much each adds to "Claude can run my printer well", not by
difficulty.

## Next

* **Slicer driver.** Shell out to `bambu-studio --slice` / OrcaSlicer CLI so the
  design loop runs end to end without leaving the session: `analyze_model` ->
  `scale_model` -> slice -> print. It also gives the model a real settings knob
  (load a profile, override layer height or temperature), which is the missing
  half of settings iteration.
* **Vision loop as a first-class tool.** `watch_print(printer, until_layer=...)`
  that samples frames, calls back into the model at checkpoints, and can pause
  the job on a confident failure verdict. The pieces (`capture_frames`,
  `pause_print`, the journal) are here; the loop is not.
* **Network discovery.** MHS describes devices and agents finding each other
  across a network. Printers are currently named in config; advertising over
  mDNS and discovering them would close that gap (see
  [MHS_ALIGNMENT.md](MHS_ALIGNMENT.md)).
* **Push instead of poll.** The driver already receives MQTT events; surfacing
  them as MCP resource-update notifications would let a client react to a state
  change without polling `get_status`.

## Later

* **More drivers.** Moonraker/Klipper and OctoPrint are the obvious next two —
  both have documented HTTP APIs and would exercise whether the `Printer` ABC is
  genuinely vendor-neutral. Prusa Connect and Duet after that.
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
