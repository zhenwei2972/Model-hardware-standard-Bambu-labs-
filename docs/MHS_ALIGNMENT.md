# Alignment with the Model Hardware Standard

Anthropic announced the [Model Hardware Standard](https://www.anthropic.com/news/model-hardware-standard-research-preview)
as a research preview on 27 August 2026: a shared specification for AI agents to
safely operate physical lab and manufacturing equipment, developed with HHMI
Janelia and used in early testing at Genentech and Janelia, with AWS, Tecan,
Universal Robots and QIAGEN among the partners adding support.

**The specification is not public yet.** Anthropic has said it will open-source
MHS after the preview. There is no published schema, no reference
implementation, and no repository under the `anthropics` organisation. So this
document describes an *alignment* with what the announcement states, not
conformance to a spec — and says where the seams are, so the gap is easy to
close later.

## What the announcement specifies

1. **A standardised driver** exposing "a simple set of primitives — commands
   like `read` (for example, "get temperature") or `write` (for example, "set
   temperature") — that any hardware device can understand and act on."
2. **Discovery**: "each device discoverable in a standard format, so that
   devices and agents can find each other and communicate across networks
   without needing a bespoke translator program in between."
3. **Natural-language tags** recording device characteristics — weight, safety
   limits, what it can measure — from which the driver "automatically produces a
   reference file" the agent consults before acting.
4. **Safety in the driver**: limits enforced at the device level with
   pre-execution checks, "rather than in the prompt".
5. **MCP as the primary integration path**, alongside a CLI and code-file APIs.

## How each maps here

### 1. read/write primitives

`Printer.read(channel)` and `Printer.write(channel, value, confirm=)` in
`src/mhs/device.py`. Channels are named, typed and unit-bearing:

```
printer.state        read        string    idle | preparing | running | paused | ...
nozzle.temperature   read_write  celsius   0 .. 300
bed.temperature      read_write  celsius   0 .. 80
job.control          write       -         pause | resume | stop  (needs confirmation)
job.file             write       -         start a sliced file    (needs confirmation)
print.speed_level    read_write  -         1 .. 4
light.chamber        read_write  -         on | off
camera.frame         read        binary    one JPEG frame
```

The table is **built in the base class from each driver's declared
capabilities**, not hand-written per device. A new driver implements the ordinary
methods (`status`, `set_temperature`, `pause_print`, …) and gets a conforming
read/write surface, with limits taken from its hardware spec, for free. That is
the property that makes the standard worth having: it has to hold for hardware
the author never saw.

### 2. Discovery

`describe_device` (tool) and `mhs://printer/{id}/descriptor` (resource) return a
machine-readable descriptor; `mhs describe` prints the same thing as a reference
sheet. It carries the device identity, transports, capabilities, every channel
with its units and limits, the physical specification, the achievable
resolution, and what the device cannot do.

Not yet implemented: **network discovery**. MHS describes devices and agents
finding each other across a network; here a printer is named in `config.toml` or
the environment. mDNS/SSDP advertisement is the natural next step and is listed
in the roadmap.

### 3. Natural-language tags and the reference file

Generated in `src/mhs/standard/descriptor.py` from the driver's channels plus
`src/mhs/specs.py`, so the reference can never drift from the code that enforces
it. Alongside the tags there is a `cannot` section, which in practice does as
much work:

```
- Slice models. It accepts sliced .3mf/.gcode only.
- Report absolute dimensions from the camera. Measurement needs a known
  reference object in the frame.
- Resolve features below the extrusion width in XY or the layer height in Z.
- Recover a failed print. A stopped job cannot be resumed.
- Hold chamber temperature, so ABS/ASA/PC warp.
- Accept control commands while the printer is in cloud mode: LAN Only Mode
  plus Developer Mode must be enabled on the printer's screen.
```

An agent that reads this before acting does not waste a cycle discovering these
by failing.

### 4. Safety in the driver

`SafetyLimit` (`src/mhs/standard/channels.py`) declares a range, an enumeration,
or a confirmation requirement, each with a rationale. `Printer.write()` checks it
**before dispatching to the transport**:

```python
>>> await printer.write("nozzle.temperature", 350)
SafetyViolation: nozzle.temperature: 350 exceeds the safe maximum 300
  hint: The A1 mini hotend is rated to 300 C; beyond that the heater block
        and PTFE degrade.
```

The limits come from the hardware specification, so the same code enforces 80 °C
on an A1 mini's plate and 110 °C on an X1 Carbon's. Nothing reaches the printer
until the check passes: there is no prompt wording, tool argument or retry that
gets around it, and a test asserts the transport was never touched on a
rejection.

Layered on top, outside the channel model: physical actions require
`confirm=true`, `MHS_READ_ONLY=1` disables every write, raw gcode is restricted
to a safe list, prints are refused while the machine is busy or reporting a
serious fault, and scheduled jobs expire instead of starting late.

### 5. MCP, CLI, code

* **MCP** — `mhs-mcp` over stdio; 36 tools, 5 resources, 3 prompts.
* **CLI** — `mhs describe | channels | read | write | status | print | ...`
* **Code** — `from mhs.drivers import build; await printer.read("printer.state")`

All three run against the same driver layer, so the safety checks cannot be
skipped by picking a different front door.

## Where this will need to change

| When the spec lands | Expected work |
| --- | --- |
| Descriptor format | Rewrite `standard/descriptor.py` to emit the official schema. The data is already collected. |
| Channel naming | Rename the channel keys; the table is generated in one place. |
| Discovery/advertisement | New: advertise over the network rather than being named in config. |
| Conformance tests | New: run the official suite against `drivers/mock.py`. |

The deliberate bet is that the *shape* — primitives, descriptor, tags,
driver-side limits — is stable enough to build on, while the wire details are
not.

## What this is not

Not endorsed, certified or reviewed by Anthropic; this is a reading of a public
announcement. Not a lab-grade device driver — a desktop FDM printer is a much
simpler machine than a liquid handler or a robotic arm, and the safety model
here is correspondingly modest. And the standard's own guidance for deploying
safely has not been published yet; when it is, it should be read before trusting
any of this unattended.
