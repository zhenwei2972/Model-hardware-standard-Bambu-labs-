# MHS — an MCP server for Bambu Lab printers and Roborock vacuums

<!-- mcp-name: io.github.zhenwei2972/mhs-printer -->

Connect Claude (or any MCP client) to real hardware:

* **Bambu Lab A1 mini, A1, P1P, P1S, X1** over your local network, no cloud
  account — start and schedule prints, monitor them, read the chamber camera,
  measure printed parts against a reference object, and check whether a model
  is printable before slicing it.
* **Roborock Saros 10, 10R, Z70** — send the robot to a point you picked off its
  map, clean rooms by name, save named locations, and drive the dock.

Built on a vendor-neutral driver layer and aligned to Anthropic's
[Model Hardware Standard](https://www.anthropic.com/news/model-hardware-standard-research-preview):
`read`/`write` primitives over named channels, a discoverable device
descriptor, and safety limits enforced in the driver rather than the prompt.

**Supported hardware:** Bambu Lab A1 mini · A1 · P1P · P1S · X1 Carbon · X1E
(LAN MQTT 8883, FTPS 990, chamber camera 6000) · Roborock Saros 10 · Saros 10R ·
Saros Z70 and the wider V1 vacuum family (Roborock cloud).
**Works with:** Claude Code, Claude Desktop, and any MCP-compatible client.

```
                     ┌─ standard/  read/write channels + limits + device descriptor
Claude ─MCP(stdio)─▶ │             (the MHS surface — identical for every device)
                     ├─ design/    mesh · render · printability · camera & map measurement
    mhs server ──────┤
                     ├─ scheduler + print journal + named locations (SQLite)
                     │
                     └─ Device ─┬─ Printer ─┬─ bambu     MQTT 8883 · FTPS 990 · cam 6000
                                │           └─ mock
                                └─ Vacuum ──┬─ roborock  cloud MQTT (via python-roborock)
                                            └─ mock_vacuum
```

## Relationship to Anthropic's Model Hardware Standard

[MHS](https://www.anthropic.com/news/model-hardware-standard-research-preview) is
Anthropic's specification for letting AI agents safely operate physical
equipment, opened as a research preview on 27 August 2026 with HHMI Janelia,
Genentech, Tecan, Universal Robots and others. Its full specification and
reference implementation are **not public yet** - Anthropic has said it will
open-source them after the preview - so nothing here can claim conformance to a
published schema. What this repository does is implement the four properties the
announcement describes, in the places where they change the code rather than the
marketing:

| MHS property | Where it lives here |
| --- | --- |
| `read`/`write` primitives over named channels | `Printer.read()` / `Printer.write()`, built in `device.py` from each driver's capabilities |
| A device description an agent can enumerate | `describe_device` / `mhs://printer/{id}/descriptor`, generated in `standard/descriptor.py` |
| Natural-language tags for what the device is | the `tags` and `cannot` sections of that descriptor |
| Safety limits in the driver, not the prompt | `SafetyLimit` on each channel, checked before dispatch (`standard/channels.py`) |

The last one is the load-bearing difference. `write("nozzle.temperature", 350)`
is refused by the driver with the reason attached - "the A1 mini hotend is rated
to 300 C" - and no prompt wording, tool argument or retry gets around it,
because the check runs before anything reaches the hardware.

When the specification lands, `standard/descriptor.py` is the one file that has
to change to emit the official format.

Bambu Lab publishes no official local API of its own, so the transport layer is
built on the community-documented interfaces in
[OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI). Other Bambu MCP servers
exist ([griches](https://github.com/griches/bambu-mcp),
[schwarztim](https://github.com/schwarztim/bambu-mcp),
[DMontgomery40](https://github.com/DMontgomery40/mcp-3D-printer-server)); they are
mostly MQTT wrappers. What is different here is the MHS channel layer, a driver
interface that is not Bambu-specific, a persistent scheduler, and the design loop
below.

## What works today

Discovery and the MHS primitives are shared by every device; the rest is per
device kind.

| Area | Tools |
| --- | --- |
| Discovery | `list_devices`, `get_printer_info`, `check_connection`, `describe_device` |
| MHS primitives | `list_channels`, `read_channel`, `write_channel` |

### 3D printers

| Area | Tools |
| --- | --- |
| Monitoring | `get_status` (state, layer, progress, temps, filament, decoded HMS alerts) |
| Files | `list_files`, `upload_file` |
| Printing | `start_print`, `upload_and_print`, `pause_print`, `resume_print`, `stop_print` |
| Controls | `set_temperature`, `set_light`, `set_print_speed`, `send_gcode` (safe-listed) |
| Camera | `capture_snapshot` (returns the JPEG to the model), `capture_frames`, `read_capture` |
| Scheduling | `schedule_print`, `list_scheduled_jobs`, `cancel_scheduled_job` |
| Model | `analyze_model`, `preview_model`, `scale_model` |
| Measuring | `camera_grid`, `camera_calibrate`, `camera_measure`, `read_annotated_image` |
| Iteration | `log_print_result`, `record_observation`, `list_print_history`, `get_print_run` |
| Resources | `mhs://printers`, `mhs://printer/{id}/status`, `mhs://printer/{id}/history`, `mhs://printer/{id}/descriptor`, `mhs://reference-objects` |
| Prompts | `diagnose_print`, `tune_settings`, `design_iteration` |

### Robot vacuums

| Area | Tools |
| --- | --- |
| Monitoring | `vacuum_status` (state, battery, suction, position, faults) |
| Map | `get_map` (rendered with a millimetre grid, rooms, robot and dock), `list_rooms` |
| Cleaning | `start_cleaning` (whole map or rooms by name), `clean_zone`, `pause_cleaning`, `resume_cleaning`, `stop_cleaning`, `return_to_dock` |
| Aiming | `go_to` (coordinates, a room name, or a saved location), `save_location`, `list_locations`, `delete_location` |
| Settings | `set_suction`, `set_water_flow`, `find_vacuum` |

Full guide: [docs/ROBOROCK.md](docs/ROBOROCK.md).

#### Sending it somewhere

The map arrives as a picture plus calibration points relating pixels to the
robot's own millimetre frame. `get_map` renders it with a grid **labelled in
those millimetres**, so a coordinate read off the image goes straight into
`go_to`:

> *"Show me the map. Send it to the spot just inside the back door, and save
> that as 'back door' so I can say it next time."*

Coordinates are checked against the map's real extent first — the protocol
accepts anything in a wide range, and an off-map target makes the robot do
nothing at all, which reads as a silent failure.

Not included: slicing. The design tools work on `.stl`/`.obj`/`.3mf` meshes and
stop at "ready to slice"; hand the printer a sliced plate from Bambu Studio or
OrcaSlicer. (Driving a slicer CLI is the next step — see
[docs/ROADMAP.md](docs/ROADMAP.md).)

## The design loop

The point of the model-side tools is that Claude can size a part against a real
object, see what it will look like, know in advance which detail the machine
cannot reproduce, print it, and then measure the result through the camera:

```
analyze_model  -> dimensions, volume, and every finding that matters:
                  fits the plate? detail below the extrusion width? thin walls?
                  overhangs? bed contact? layer count? grams of filament?
preview_model  -> orthographic views at a shared scale, with a mm scale bar and
                  overhangs tinted red — an image, so the model can actually look
camera_grid    -> a frame with a labelled pixel grid, so coordinates are read
                  rather than guessed
camera_calibrate("sgd_1", 88)   -> a coin of known diameter fixes mm-per-pixel
scale_model(target_mm=24.65)    -> dry run first: it reports how much detail
                                   crosses the printable threshold either way
camera_measure -> measure the printed part, compare against the target, and get
                  the rescale factor that would correct it
log_print_result -> the journal, so the next iteration starts from evidence
```

Worked example: [docs/DESIGN_LOOP.md](docs/DESIGN_LOOP.md).

Two honest limits. Feature size is estimated from mesh edge lengths and the
volume-to-area ratio, not a medial-axis analysis: it reliably catches "far too
fine" and "wall too thin", not every thin region in a complex part. And the
chamber camera is off-axis with lens distortion, so measurements are good to a
few percent with a reference object in the same plane — not caliper-grade. Both
caveats are returned with the results rather than buried here.

## Install

Python 3.10+.

```bash
git clone https://github.com/zhenwei2972/Model-hardware-standard-Bambu-labs-.git
cd Model-hardware-standard-Bambu-labs-
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Configure a printer

For a vacuum instead, see [docs/ROBOROCK.md](docs/ROBOROCK.md) — it is a
different setup (an account token, not a LAN code).

On the A1 / A1 mini touchscreen: **Settings (gear) → WLAN** — the network
screen, not the general one.

1. Toggle **LAN Only Mode** on — this is also what makes the Access Code appear.
2. **Power-cycle the printer.** Developer Mode does not show up until it has
   restarted.
3. Back in Settings → WLAN, toggle **Developer Mode** on — without it, firmware
   ≥ 01.05 on the A1 series silently ignores start/stop/heat commands.

Note that LAN Only Mode cuts the printer off from Bambu's cloud, so **the Bambu
Handy phone app stops working with it**. Bambu Studio on the same network is
unaffected, and turning the mode back off restores Handy with no re-pairing.

Then collect three values — **IP address** and **Access Code** from
Settings → Network, and the **serial number** from the sticker or Bambu Studio →
Device (it is the TLS certificate name, so it must match exactly):

```bash
export BAMBU_HOST=192.168.1.42
export BAMBU_SERIAL=0309CA1234567890
export BAMBU_ACCESS_CODE=12345678
```

or copy `examples/config.example.toml` to `config.toml` (git-ignored) for
multiple printers. Full walkthrough, including what each failure mode looks
like: [docs/SETUP.md](docs/SETUP.md).

## Connect it to Claude

```bash
claude mcp add mhs \
  --env BAMBU_HOST=192.168.1.42 \
  --env BAMBU_SERIAL=0309CA1234567890 \
  --env BAMBU_ACCESS_CODE=12345678 \
  --env MHS_READ_ONLY=1 \
  -- "$PWD/.venv/bin/mhs-mcp"
```

For Claude Desktop, merge [examples/claude_desktop_config.json](examples/claude_desktop_config.json)
into `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`,
Windows `%APPDATA%\Claude\`). Use absolute paths there — the desktop app does
not inherit your shell's `PATH`; `which mhs-mcp` gives you the right one.

Then, in a session:

> *"Is the A1 mini free? If so print `cache/bracket_v3.3mf` at 06:30 tomorrow,
> and look at the first layer once it starts."*

## Testing it

Three tiers. The first two need no hardware, and each stands on its own.

### 1. No hardware, no Claude

Exercises every code path except the real transports:

```bash
.venv/bin/pytest -q                  # 253 tests, no hardware required
export MHS_MOCK=1                    # a simulated A1 mini
.venv/bin/mhs describe               # the MHS device descriptor
.venv/bin/mhs channels               # every read/write channel and its limits
.venv/bin/mhs write nozzle.temperature 400   # refused by the driver, with the reason
.venv/bin/mhs inspect your-model.stl # printability critique
.venv/bin/mhs preview your-model.stl -o preview.png
.venv/bin/mhs grid && .venv/bin/mhs calibrate sgd_1 100 && .venv/bin/mhs measure 0 0 200 0
.venv/bin/mhs doctor

# and the vacuum half, against a simulated Saros
printf '[server]\ndefault_device="saros"\n[devices.saros]\ndriver="mock_vacuum"\n' > config.toml
.venv/bin/mhs vacuum && .venv/bin/mhs rooms
.venv/bin/mhs map -o map.png          # open it: the grid is in robot millimetres
.venv/bin/mhs goto "dog bowl" --x 24200 --y 26200 -y
```

With `MHS_MOCK=1` each CLI command is a fresh process, so `mhs print` followed
by `mhs status` shows idle again — the fake printer has no memory between
invocations. Inside one MCP session it persists normally.

### 2. Wired to Claude, still no hardware

Checks the tools from the model's side before hardware is involved:

```bash
claude mcp add mhs-demo --env MHS_MOCK=1 -- "$PWD/.venv/bin/mhs-mcp"
```

> *"Run describe_device and tell me what this printer can and cannot do. Then
> analyze_model on ~/models/thing.stl, show me the preview, and tell me what
> scaling it to 24.65 mm would cost in detail."*

A rendered image and a printability verdict means the whole stack works — only
the transports are untested.

### 3. Real hardware

```bash
.venv/bin/mhs doctor
```

```
printer 'a1mini' driver=bambu model=A1 mini
  host 192.168.1.42  serial 01P0***  tls=verify
  tcp/8883  mqtt    open
  tcp/990   ftps    open
  tcp/6000  camera  open
  telemetry  ok    a1mini: idle, nozzle 24/0C, bed 24/0C
  files      ok    3 items/bytes
  camera     ok    98431 items/bytes
all good
```

`doctor` probes each port, then each transport, and names the misconfiguration:

| Symptom | Cause |
| --- | --- |
| all three ports closed | wrong IP, printer asleep, or Wi-Fi client isolation / separate VLAN |
| tcp/6000 closed only | camera not served — re-check LAN Only Mode and power-cycle once |
| `CERTIFICATE_VERIFY_FAILED` | serial mismatch — re-check it, or set `tls_mode = "ca_only"` |
| FTPS login refused | wrong Access Code (it changes after re-pairing) |
| telemetry fine, commands `acknowledged: false` | Developer Mode is off |

Start with `MHS_READ_ONLY=1` so the first session can only look, then drop it.

## Safety model

Hardware that heats to 220 °C deserves more care than a web API:

* **Confirmation gate.** `start_print`, `upload_and_print`, `stop_print` and
  `send_gcode` refuse to act unless called with `confirm=true`; the first call
  returns what *would* happen. The model has to decide twice.
* **Read-only mode.** `MHS_READ_ONLY=1` leaves monitoring and camera tools
  working and rejects every write.
* **Gcode allow-list.** Raw gcode is limited to temperature/fan/homing/report
  commands unless `allow_raw_gcode = true`.
* **Pre-flight.** Prints are refused while the printer is busy or reporting a
  fatal/serious HMS alert; scheduled jobs wait for an idle printer and expire
  instead of starting hours late.
* **TLS is verified.** Bambu's CA is pinned and the certificate's CN (the
  serial) is checked via SNI — `tls_mode = "verify"` by default, with
  `ca_only`/`insecure` as explicit, documented downgrades.
* **Credentials stay local.** The access code lives in `config.toml` or the
  environment; both are git-ignored, and nothing is sent anywhere but the
  printer on your LAN.

## Is it actually generic?

It claimed to be, and then a robot vacuum tested the claim. Three layers:

```
Device   channels · read/write · safety limits · descriptor · capabilities
  ├─ Printer   nozzle, bed, files, plates, prints
  └─ Vacuum    rooms, zones, coordinates, dock
```

`Device` (`mhs/device.py`) is what the MCP tools, the CLI and the descriptor
generator talk to. A **device class** adds the verbs for its kind of hardware
and declares its channels; a **driver** implements one of those. Adding the
vacuum needed no change to the channel machinery, the limit enforcement, the
descriptor generator, the CLI plumbing or the confirmation gates — they were
already device-shaped. What it did change, honestly: `printer_id` became
`device_id` throughout (with a SQLite migration), the descriptor's
printer-specific sections moved behind a `descriptor_profile()` hook that each
device class fills in, and `[printers.*]` in config.toml became `[devices.*]`
(the old spelling still loads).

A new driver is one class and one `register()` call:

```python
class MoonrakerPrinter(Printer):
    driver_name = "moonraker"
    @property
    def capabilities(self): return (Capability.START_PRINT, Capability.FILE_UPLOAD, ...)
    async def status(self) -> PrinterStatus: ...
```

…and it gets the MHS read/write channels, the enforced limits and a descriptor
for free, because the base class builds them from the capabilities it declares.
Tools call `device.require(Capability.CAMERA_SNAPSHOT)` rather than asking what
brand it is, so a driver that lacks a feature degrades with a clear message
instead of a stack trace — and naming a vacuum where a printer tool expects one
is an explicit error, not a confusing failure. `drivers/mock.py` and
`drivers/mock_vacuum.py` are complete worked examples, and are what the test
suite runs against.

## Layout

```
src/mhs/
  device.py          Device base: channels, read/write, descriptor, capabilities
  printer.py         Printer device class      vacuum.py   Vacuum device class
  models.py          normalised status types   specs.py    hardware specs and limits
  config.py          TOML + env configuration  store.py    SQLite: jobs, journal,
  scheduler.py       deferred prints                       calibration, locations
  app.py             pool + store + scheduler  server.py   the MCP surface
  cli.py             the same capabilities for humans
  standard/          channels.py (limits), descriptor.py (the MHS reference file)
  design/            mesh.py (STL/OBJ/3MF), render.py (z-buffer rasteriser),
                     printability.py (the critique), vision.py (camera measurement),
                     mapviz.py (map grid + pixel/millimetre transform)
  drivers/bambu/     mqtt.py files.py camera.py commands.py state.py hms.py tls.py
  drivers/roborock/  client.py (credentials) commands.py state.py vacuum.py
  drivers/mock.py    simulated printer         drivers/mock_vacuum.py  simulated robot
docs/                PROTOCOL.md, SETUP.md, ROBOROCK.md, DESIGN_LOOP.md,
                     MHS_ALIGNMENT.md, PUBLISHING.md, ROADMAP.md
```

## Tests

```bash
pytest          # no hardware required — every test runs against the mock driver
ruff check .
```

The parts that are expensive to debug against a real machine are tested
directly: command payloads, delta-merged telemetry, camera framing, FTPS path
safety, scheduler windows, channel safety limits, and the geometry - mesh volume
and area are checked against analytic values, and the renderer against known
occlusion cases.

## Publishing

[docs/PUBLISHING.md](docs/PUBLISHING.md) covers the registry manifest
([`server.json`](server.json), validated against the official schema), PyPI
release, and the discoverability checklist.

## Credits

Wire formats from [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI)
(MQTT, FTPS, the port-6000 camera protocol and the pinned CA bundle). Bambu Lab
does not endorse or support this project, and neither does Anthropic: the MHS
alignment here is this repository's reading of a public announcement, not a
certification.

MIT licensed.
