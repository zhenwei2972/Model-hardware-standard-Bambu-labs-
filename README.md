# MHS — Model Hardware Standard for 3D printers

An MCP server plus a small vendor-neutral driver layer, so Claude (or any
MCP client) can drive a **Bambu Lab A1 mini** — and, by design, other printers —
over the local network: start prints, watch them, schedule them for later, and
look at the camera to decide what to change next.

```
Claude  ──MCP(stdio)──▶  mhs server  ──▶  Printer ABC  ──▶  bambu driver  ──▶  MQTT  8883  telemetry + control
                             │                            └▶ mock driver      FTPS  990   sliced file upload
                             ├─ scheduler (SQLite)                            TCP   6000  JPEG camera frames
                             └─ print journal (SQLite)
```

## Is there an "Anthropic hardware standard" for the A1 mini?

Short answer: **no official one, and nothing from Anthropic that is printer- or
Bambu-specific.** Worth separating three things:

1. **Anthropic's standard is MCP** (the Model Context Protocol) — a general
   protocol for exposing tools, resources and prompts to a model. It is not a
   hardware standard: there is no Anthropic-defined device profile, no
   "printer" schema, no certification. Hardware support is whatever an MCP
   server chooses to expose.
2. **Bambu Lab publishes no official local API.** Everything below is built on
   the community-reverse-engineered LAN interfaces documented by
   [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI), which Bambu
   tolerates but does not support. Their officially blessed third-party path is
   Bambu Connect / the cloud API.
3. **Community MCP servers for Bambu already exist** — e.g.
   [griches/bambu-mcp](https://github.com/griches/bambu-mcp),
   [schwarztim/bambu-mcp](https://github.com/schwarztim/bambu-mcp),
   [DMontgomery40/mcp-3D-printer-server](https://github.com/DMontgomery40/mcp-3D-printer-server).
   They are mostly one-vendor wrappers around MQTT. This repo differs in three
   ways that matter for the end state you asked about: a **driver interface** so
   the same tools cover non-Bambu hardware, a **persistent scheduler** for
   "print at 06:30", and a **print journal + camera capture** so a model can
   iterate on settings across prints instead of reacting to one status blob.

So: not implemented as a standard — but the standard you actually need (MCP)
plus the printer's LAN interfaces are enough to build it, which is what is here.

## What works today

| Area | Tools |
| --- | --- |
| Discovery | `list_printers`, `get_printer_info`, `check_connection` |
| Monitoring | `get_status` (state, layer, progress, temps, filament, decoded HMS alerts) |
| Files | `list_files`, `upload_file` |
| Printing | `start_print`, `upload_and_print`, `pause_print`, `resume_print`, `stop_print` |
| Controls | `set_temperature`, `set_light`, `set_print_speed`, `send_gcode` (safe-listed) |
| Camera | `capture_snapshot` (returns the JPEG to the model), `capture_frames`, `read_capture` |
| Scheduling | `schedule_print`, `list_scheduled_jobs`, `cancel_scheduled_job` |
| Iteration | `log_print_result`, `record_observation`, `list_print_history`, `get_print_run` |
| Resources | `mhs://printers`, `mhs://printer/{id}/status`, `mhs://printer/{id}/history` |
| Prompts | `diagnose_print`, `tune_settings` |

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
  device.py        Printer ABC + Capability checks        models.py   normalised status/telemetry types
  config.py        TOML + env configuration               store.py    SQLite: job queue + print journal
  scheduler.py     deferred prints, pre-flight, windows   app.py      pool + store + scheduler wiring
  server.py        the MCP surface                        cli.py      the same thing for humans
  drivers/bambu/   mqtt.py files.py camera.py commands.py state.py hms.py tls.py
  drivers/mock.py  simulated printer
docs/              PROTOCOL.md (wire formats), SETUP.md, ROADMAP.md
```

## Tests

```bash
pytest          # no hardware required — every test runs against the mock driver
ruff check .
```

The protocol-shaped parts (command payloads, delta-merged telemetry, the camera
framing, FTPS path safety, scheduler windows) are tested directly, because those
are the parts that are expensive to debug against a real machine.

## Credits

Wire formats from [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI)
(MQTT, FTPS, the port-6000 camera protocol and the pinned CA bundle). Bambu Lab
does not endorse or support this project.

MIT licensed.
