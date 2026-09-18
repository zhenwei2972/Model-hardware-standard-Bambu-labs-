# Bambu Lab LAN protocol notes

What this driver relies on, why, and where it came from. Wire formats are from
[OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI); Bambu Lab publishes no
official local API, so all of it is community-documented and can change with a
firmware update.

Verified against an A1 mini on firmware `01.06.x`. The P1 series behaves
identically; the X1/H2 differ where noted.

## Transports

| Purpose | Endpoint | TLS | Auth |
| --- | --- | --- | --- |
| Telemetry + control | `mqtts://<ip>:8883` | yes | user `bblp`, password = Access Code |
| Sliced files | `ftps://<ip>:990` (implicit) | yes | same |
| Camera (A1/P1) | `tcp://<ip>:6000` | yes | 80-byte login packet |
| Camera (X1/H2) | `rtsps://<ip>:322/streaming/live/1` | yes | same — **not implemented here** |

### TLS

The certificate is issued by Bambu's own CA (`BBL CA` / `BBL CA2`, bundled as
`src/mhs/drivers/bambu/bambu_ca.pem`) and its **common name is the printer's
serial number**, not its IP. Verifying it therefore needs the pinned CA *and*
SNI set to the serial — `tls.py` does that with an `SSLContext` subclass that
overrides `wrap_socket`'s `server_hostname`. Bambu's CA omits the `keyUsage`
extension, which Python 3.13's strict profile rejects, so `VERIFY_X509_STRICT`
is cleared while everything else stays checked.

## MQTT

Two topics, JSON both ways:

* `device/<serial>/report` — printer → us
* `device/<serial>/request` — us → printer

Every request is `{"<type>": {"sequence_id": "<n>", "command": "<name>", ...}}`
and the reply echoes `sequence_id` plus `result`. The driver waits for that echo
(`publish_and_wait`), which is how "command ignored because Developer Mode is
off" becomes a visible `acknowledged: false` instead of silent success.

### Deltas, not snapshots

**The single most important quirk:** on the A1/A1 mini/P1, `print.push_status`
carries *only the fields that changed*. A naive client that reads the last
message sees `total_layer_num` disappear. `state.py` therefore deep-merges every
report into a cached object and normalises from that. `pushing.pushall` returns
the full object — but OpenBambuAPI warns not to call it more than every few
minutes on these MCUs, so the driver rate-limits it to once per five minutes
(plus once at connect, and once when telemetry looks stale).

### Commands used

| Command | Notes |
| --- | --- |
| `pushing.pushall` | full status resync |
| `info.get_version` | firmware versions per module |
| `print.project_file` | start a sliced job — see below |
| `print.pause` / `resume` / `stop` | `param` must be present and empty; sent at QoS 1 |
| `print.gcode_line` | raw gcode; how temperatures are set (`M104`/`M140`) |
| `print.print_speed` | `"1"`–`"4"` = silent/standard/sport/ludicrous |
| `print.skip_objects` | cancel individual objects mid-print |
| `print.calibration` | bitmask: lidar<<0, bed<<1, vibration<<2, motor<<3 |
| `system.ledctrl` | chamber/work light; timing fields required even when not flashing |

### Starting a print

```json
{"print": {
  "sequence_id": "12", "command": "project_file",
  "param": "Metadata/plate_1.gcode",
  "url": "ftp:///cache/bracket.3mf",
  "subtask_name": "bracket",
  "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0",
  "bed_type": "auto", "bed_levelling": true, "flow_cali": true,
  "vibration_cali": true, "layer_inspect": true, "timelapse": false,
  "use_ams": false, "ams_mapping": ""
}}
```

Three things bite here:

1. `param` names the **plate inside the 3mf**, not the file. Plate 2 is
   `Metadata/plate_2.gcode`.
2. All four id fields must be `"0"` for a LAN print. Non-zero values make the
   firmware try to reconcile the job with a cloud task and it refuses to start.
3. `ams_mapping` is a fixed 5-element, **right-aligned** array of AMS slots per
   colour (`[-1,-1,-1,-1,2]` = single colour from tray 2). Get it wrong and the
   printer sits waiting instead of erroring — hence the validation in
   `commands.build_ams_mapping`.

### Errors

Faults arrive as `hms: [{attr, code}]` plus a scalar `print_error`. Rendered as
`AAAA_BBBB_CCCC_DDDD` (the form Bambu's wiki indexes), with severity in the high
half of `code`: 1 fatal, 2 serious, 3 common, 4 info. `hms.py` does this and
attaches the wiki URL, so an alert is actionable rather than a pair of integers.

### Stages

`stg_cur` is an enum of what the printer is physically doing (`auto bed
levelling`, `heating hotend`, `paused: filament runout`, ...) and it is far more
informative than `gcode_state` while a job starts up. It keeps its last value
after a job ends, so the driver only reports it while a job is live.

## FTPS

Implicit TLS on 990: the socket is wrapped *before* the greeting, so
`ftplib.FTP_TLS` needs the `sock` setter override in `files.py`, and the data
channel reuses the control channel's TLS session. Uploads go to `cache/`
(configurable); the printer also exposes `timelapse/` and `model/`.

Filenames are basenamed and checked against a conservative pattern before use —
the printer's FTP server does not protect itself from `../`.

## Camera (A1 / A1 mini / P1)

Not RTSP: a TLS socket that, after an 80-byte login packet, streams
`[16-byte header][JPEG]` repeatedly at roughly 1 fps, 1280x720.

```
auth packet (80 bytes, little-endian)
  0  u32  payload size (0x40)
  4  u32  type (0x3000)
  8  u32  flags (0)
 12  u32  0
 16  32s  "bblp", null-padded
 48  32s  access code, null-padded

frame header (16 bytes)
  0  u32  payload size
  4  u32  itrack (0)
  8  u32  flags (1)
 12  u32  0
```

Payloads arrive in ~4 KiB chunks, so they must be reassembled; frames are
validated on the `FF D8` / `FF D9` markers and truncated ones are dropped.
Plain JPEG out is exactly what a vision model wants, which is why the snapshot
tool can hand a frame straight to Claude.

## What is deliberately not used

* **The cloud API** (`api.bambulab.com`, `us.mqtt.bambulab.com`). It would add
  an account, a token refresh dance and remote control of a hot machine over the
  internet. LAN-only keeps the blast radius local.
* **Bambu Connect.** The sanctioned path, but it is a desktop app with an
  embedded X.509 identity, not an API. Extracting that identity to bypass the
  Authorization Control System is a documented community workaround; this
  project uses the supported Developer Mode toggle instead.
