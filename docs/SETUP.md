# Setting up a Bambu Lab A1 mini

## 1. Put the printer in LAN Only Mode + Developer Mode

On the printer's touchscreen: **Settings (gear) → General**

| Toggle | Why |
| --- | --- |
| **LAN Only Mode** | Keeps MQTT/FTPS/camera served locally. |
| **Developer Mode** | Since firmware `01.05.00.00` on the A1 series, Bambu's *Authorization Control System* blocks third-party **control** (start/stop/heat/move) unless this is on. Monitoring still works without it. |
| **LAN Mode Liveview** | Required for the camera stream on tcp/6000. |

The printer reboots its network stack after these changes; give it a minute.

> If control commands come back with `acknowledged: false` while status keeps
> updating, this is almost always the cause.

## 2. Collect three values

| Value | Where |
| --- | --- |
| **IP address** | Printer screen → Settings → Network (or your router's DHCP table). Give it a DHCP reservation — the config is static. |
| **Access Code** | Same screen. 8 characters. It changes if you re-pair or factory reset. |
| **Serial number** | Sticker on the back, or Bambu Studio → Device → printer info. Used as the TLS certificate name, so it must be exact. |

## 3. Configure

```bash
cp examples/config.example.toml config.toml
$EDITOR config.toml     # host, serial, access_code
```

or, without a file:

```bash
export BAMBU_HOST=192.168.1.42
export BAMBU_SERIAL=01P00A000000000
export BAMBU_ACCESS_CODE=12345678
```

`config.toml` is git-ignored.

## 4. Verify before involving a model

```bash
mhs doctor
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

### If something is closed or failing

| Symptom | Cause / fix |
| --- | --- |
| all three ports closed | Wrong IP, printer asleep, or client isolation / different VLAN on the Wi-Fi. |
| tcp/6000 closed only | LAN Mode Liveview is off. |
| `CERTIFICATE_VERIFY_FAILED` | Certificate CN does not match the serial you configured. Re-check the serial; if it still fails, set `tls_mode = "ca_only"`. |
| MQTT connects, commands do nothing | Developer Mode off (see step 1). |
| FTPS login refused | Wrong Access Code — it changes after re-pairing. |
| Status is `offline` after a while | Firmware drops idle MQTT sessions; the driver reconnects and re-requests a full status automatically. |

## 5. Connect Claude

```bash
claude mcp add mhs -- mhs-mcp
```

For Claude Desktop, merge `examples/claude_desktop_config.json` into
`claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`).

Use absolute paths in that file — the desktop app does not inherit your shell's
`PATH` (`which mhs-mcp` gives you the right one).

### Suggested first session

> *"Run check_connection, then get_status and capture_snapshot so I can see the
> plate. If it's clear, upload ~/Downloads/plate_1.3mf and schedule it for 6:30
> tomorrow."*

Start with `MHS_READ_ONLY=1` in the env if you want to watch it work before
letting it move anything.

## 6. Keep the scheduler alive

Scheduled prints fire from whichever process is running: the MCP server (while
a client holds it open) or `mhs scheduler`. For unattended overnight jobs, run
the latter as a service:

```ini
# ~/.config/systemd/user/mhs-scheduler.service
[Unit]
Description=MHS print scheduler
[Service]
ExecStart=%h/.local/bin/mhs scheduler
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now mhs-scheduler
```

The queue lives in SQLite (`~/.local/share/mhs/mhs.sqlite3`), so both processes
see the same jobs and a restart loses nothing.

## Safety notes for unattended printing

Bambu's own guidance is not to run prints unattended; this tool does not change
that. The pre-flight checks here (idle printer, no serious HMS alert, expiring
windows) reduce the ways a *scheduled* job can go wrong, not the ways a print
can. Keep a smoke alarm nearby, prefer daytime windows, and use
`capture_frames` plus `get_status` for check-ins.
