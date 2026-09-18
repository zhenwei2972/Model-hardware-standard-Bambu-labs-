# MHS — Model Hardware Standard for 3D printers

An MCP server plus a vendor-neutral driver layer, aligned to Anthropic's
[Model Hardware Standard](https://www.anthropic.com/news/model-hardware-standard-research-preview),
so Claude (or any MCP client) can operate a **Bambu Lab A1 mini** — and, by
design, other printers — over the local network: start and schedule prints,
watch them, look at the model before printing it, and measure the result through
the camera.

```
                        ┌─ standard/  read/write channels + safety limits + device descriptor
Claude ──MCP(stdio)──▶  │             (the MHS-shaped surface)
                        ├─ design/    mesh · render · printability · camera measurement
       mhs server ──────┤
                        ├─ scheduler + print journal (SQLite)
                        │
                        └─ Printer ABC ──▶ bambu driver ──▶ MQTT 8883  telemetry + control
                                        └▶ mock driver      FTPS  990  sliced file upload
                                                            TCP   6000 JPEG camera frames
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

| Area | Tools |
| --- | --- |
| Discovery | `list_printers`, `get_printer_info`, `check_connection`, `describe_device` |
| MHS primitives | `list_channels`, `read_channel`, `write_channel` |
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

Not included: slicing. Hand the server a sliced `.3mf`/`.gcode` from Bambu
Studio or OrcaSlicer. (Driving a slicer CLI is a natural next driver — see
[docs/ROADMAP.md](docs/ROADMAP.md).)

## Quick start

```bash
pip install -e ".[dev]"

# 1. Try everything without hardware
MHS_MOCK=1 mhs status
MHS_MOCK=1 mhs print cache/benchy.3mf -y

# 2. Point it at a real printer
cp examples/config.example.toml config.toml   # edit host/serial/access code
mhs doctor            # probes tcp/8883, tcp/990, tcp/6000 then each transport
mhs status
mhs snapshot -o bed.jpg
mhs upload ~/Downloads/plate_1.3mf
mhs print cache/plate_1.3mf
mhs schedule cache/plate_1.3mf "2026-09-19T06:30" --window 90
mhs scheduler         # keep running so queued jobs can fire
```

On the printer first: **Settings → General → LAN Only Mode + Developer Mode**
(and **LAN Mode Liveview** for the camera). Since firmware 01.05.00.00 on the
A1 series, Bambu's Authorization Control System blocks third-party *control* in
cloud mode — monitoring still works, but `start_print` will be silently ignored,
which this server reports as `acknowledged: false` rather than pretending it
worked. Full setup walkthrough: [docs/SETUP.md](docs/SETUP.md).

### Wire it to Claude

`claude mcp add mhs -- mhs-mcp` — or add it by hand (see
[examples/claude_desktop_config.json](examples/claude_desktop_config.json)):

```json
{
  "mcpServers": {
    "mhs": {
      "command": "mhs-mcp",
      "env": {
        "BAMBU_HOST": "192.168.1.42",
        "BAMBU_SERIAL": "01P00A000000000",
        "BAMBU_ACCESS_CODE": "12345678",
        "MHS_STATE_DIR": "~/.local/share/mhs"
      }
    }
  }
}
```

Then, in a session:

> *"Is the A1 mini free? If so print `cache/bracket_v3.3mf` at 06:30 tomorrow,
> and take a look at the first layer once it starts."*

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

## Making it generic

`mhs/device.py` defines one small ABC (`Printer`) plus a `Capability` enum;
everything above it — the 25 MCP tools, the scheduler, the CLI, the journal —
talks only to that. A driver is one class and one `register()` call:

```python
class MoonrakerPrinter(Printer):
    driver_name = "moonraker"
    @property
    def capabilities(self): return (Capability.START_PRINT, Capability.FILE_UPLOAD, ...)
    async def status(self) -> PrinterStatus: ...
```

Tools check `printer.require(Capability.CAMERA_SNAPSHOT)` rather than asking
what brand it is, so a driver that lacks a feature degrades with a clear message
instead of a stack trace. `drivers/mock.py` is a complete worked example (and is
what the test suite runs against).

## Layout

```
src/mhs/
  device.py        Printer ABC, capabilities, read/write  models.py   normalised status/telemetry types
  config.py        TOML + env configuration               store.py    SQLite: jobs, journal, calibration
  scheduler.py     deferred prints, pre-flight, windows   app.py      pool + store + scheduler wiring
  specs.py         hardware specs and detail limits       server.py   the MCP surface
  cli.py           the same capabilities for humans
  standard/        channels.py (limits), descriptor.py (the MHS device reference file)
  design/          mesh.py (STL/OBJ/3MF), render.py (z-buffer rasteriser),
                   printability.py (the critique), vision.py (camera measurement)
  drivers/bambu/   mqtt.py files.py camera.py commands.py state.py hms.py tls.py
  drivers/mock.py  simulated printer
docs/              PROTOCOL.md, SETUP.md, DESIGN_LOOP.md, MHS_ALIGNMENT.md, ROADMAP.md
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

## Credits

Wire formats from [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI)
(MQTT, FTPS, the port-6000 camera protocol and the pinned CA bundle). Bambu Lab
does not endorse or support this project, and neither does Anthropic: the MHS
alignment here is this repository's reading of a public announcement, not a
certification.

MIT licensed.
