# Setting up a Bambu Lab A1 mini

## 1. Put the printer in LAN Only Mode + Developer Mode

On the A1 / A1 mini touchscreen the toggles live under the **network** screen,
not the general one:

**Settings (gear) → WLAN**

1. Toggle **LAN Only Mode** on.
2. **Power-cycle the printer.** Developer Mode does not appear until it has
   restarted — this step is easy to miss and it looks like the option is absent.
3. Back in Settings → WLAN, toggle **Developer Mode** on.

| Toggle | Why |
| --- | --- |
| **LAN Only Mode** | Serves MQTT/FTPS/camera on your network, and is what makes the Access Code appear. |
| **Developer Mode** | Since firmware `01.05.00.00` on the A1 series, Bambu's *Authorization Control System* blocks third-party **control** (start/stop/heat/move) unless this is on. Monitoring still works without it. |

> **LAN Only Mode disconnects the printer from Bambu's cloud, so the Bambu
> Handy phone app stops working with it.** Bambu Studio on the same network
> still does, and so does this server. Turning LAN Only Mode back off restores
> Handy; nothing has to be re-paired.

> If control commands come back with `acknowledged: false` while status keeps
> updating, Developer Mode is off (or the power-cycle was skipped).

## 2. Collect three values

| Value | Where |
| --- | --- |
| **IP address** | Settings → WLAN, on the same screen. Give it a DHCP reservation in your router — the config stores it statically. |
| **Access Code** | Settings → WLAN, visible once LAN Only Mode is on. 8 characters. If it reads all zeros, toggle LAN Only Mode off and on again to regenerate it. It also changes on re-pair or factory reset. |
| **Serial number** | Sticker on the back of the printer, **Settings → Device** on the touchscreen, or Bambu Studio → Device. An A1 mini serial starts `030`. It is the TLS certificate name, so it must be exact. |

## 3. Configure

```bash
cp examples/config.example.toml config.toml
$EDITOR config.toml     # host, serial, access_code
```

or, without a file:

```bash
export BAMBU_HOST=192.168.1.42
export BAMBU_SERIAL=0309CA1234567890
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
| tcp/6000 closed only | The camera is not being served: re-check LAN Only Mode, and power-cycle once. |
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
