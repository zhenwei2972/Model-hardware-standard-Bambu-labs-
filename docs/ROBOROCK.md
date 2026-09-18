# Roborock vacuums (Saros 10 / 10R / Z70)

Sending the robot to a spot on the map, cleaning named rooms, and doing it from
Claude.

## What this driver does and does not implement

**It does not reimplement the Roborock protocol.** That protocol has four wire
versions, AES in ECB, CBC and GCM, HMAC-derived MQTT credentials and a
compressed binary map format. [`python-roborock`](https://github.com/Python-roborock/python-roborock)
already implements and maintains all of it, is what Home Assistant's Roborock
integration uses, and ships a device simulator. Re-deriving that blind, against
hardware that cannot be tested from here, would be a worse decision than taking
the dependency — the opposite call from the Bambu driver, where the protocol is
simple enough (JSON over MQTT, plain FTPS, a 16-byte camera header) that owning
it is cheaper than depending on someone else's.

What this driver adds is the part that library deliberately leaves open:

* the MHS surface — channels, limits enforced in the driver, a device descriptor;
* **aiming**: a map rendered with a millimetre grid, so a model can read a
  coordinate off the picture and send the robot there;
* room names resolved to segment ids, so "clean the kitchen" works;
* **named locations** that survive a restart, for the places a map has no name
  for;
* pre-flight checks: no new job while faulted, or below 15% battery and not
  charging.

## Cloud, not LAN

Unlike the printer, there is no useful LAN-only path: the robot is reached
through Roborock's cloud, so operating it means holding an account credential.
Two consequences, both enforced in code rather than left to you:

* the **password is never stored** — it is exchanged once for a token, and only
  the token is written;
* the token file is created mode `600`, and tightened if it is ever found
  looser. Its contents are never logged or returned by a tool.

It also means this one *can* work from a cloud session, unlike the printer:
Roborock's API is on the internet.

## Setup

```bash
pip install -e ".[roborock]"
mhs roborock-login                 # asks for the email and password
# or, if the account uses a one-time code:
mhs roborock-login --code
```

The password is read without echo and discarded after the exchange. The token
lands in the state directory (`~/.local/share/mhs/roborock-credentials.json`).

Then add the device:

```toml
[devices.saros]
driver = "roborock"
model = "Saros 10"                 # Saros 10 | Saros 10R | Saros Z70
email = "you@example.com"
# device_name = "Saros 10"         # only needed if the account has several robots
# device_uid = "..."               # or pin it by id; errors list what the account has
```

```bash
mhs vacuum        # state, battery, suction, position
mhs rooms         # segment ids and the names from the Roborock app
mhs map -o map.png
```

## Aiming it

The map arrives as a picture plus calibration points relating pixels to the
robot's own millimetre frame. `get_map` renders it with a grid **labelled in
those millimetres**, so a coordinate read off the image goes straight into
`go_to` with no conversion:

```
get_map()                                   → the picture, gridded, rooms named
go_to(x_mm=24200, y_mm=26200, confirm=True) → drives there and stops
```

Three ways to say where:

| Form | Example |
| --- | --- |
| Coordinates off the map | `go_to(x_mm=24200, y_mm=26200)` |
| A room, by name | `go_to(location="kitchen")` → the room's centre |
| A saved point | `save_location("dog bowl", 24200, 26200)` then `go_to(location="dog bowl")` |

Coordinates are checked against the map's real extent before being sent. The
protocol accepts anything in a wide range and an off-map target makes the robot
do nothing at all, which reads as a silent failure — so the driver turns that
into an error with the actual bounds in it.

> The frame is centred near **25500 mm**, not zero. `(0, 0)` is not "the corner
> of the room", it is off the map.

## Cleaning

```
list_rooms()                                          → Kitchen (16), Hallway (18), …
start_cleaning(rooms=["kitchen", "hallway"], confirm=True)
start_cleaning(confirm=True)                          → the whole map
clean_zone(x1_mm=…, y1_mm=…, x2_mm=…, y2_mm=…, confirm=True)   → one rectangle, for a spill
pause_cleaning() / resume_cleaning() / stop_cleaning(confirm=True) / return_to_dock()
```

Room names match case-insensitively and partially, so "kitchen" finds
"Kitchen". An unmatched name lists what is actually mapped rather than failing
blankly. Rooms are named in the Roborock app; this driver reads them, it cannot
set them.

## Channels

The MHS primitives work here exactly as they do on the printer:

```
vacuum.state      read        idle | cleaning | paused | returning | docked | charging | error
battery.level     read   %
vacuum.position   read        where it is, in map millimetres
vacuum.command    write       start | pause | resume | stop | dock   (needs confirmation)
vacuum.goto       write       "x,y" in millimetres                   (needs confirmation)
clean.rooms       write       [16, 17]                               (needs confirmation)
fan.power         read_write  presets read from the device itself
water.level       read_write  presets read from the device itself
map.image         read        the rendered PNG
```

Suction and water presets are read off the connected robot rather than
hard-coded, because they differ by model and firmware.

## What it will refuse

* A new job while a fault is reported, or below 15% battery and not charging.
* A point or zone outside the mapped area.
* A room that is not on the map.
* A suction or water preset this model does not have.
* Anything at all when `MHS_READ_ONLY=1`.

## Limits worth knowing

* **Cleaning runs are advisory.** The robot decides its own path; `go_to` means
  "drive here", not "take this route".
* **The map must exist first.** Run a mapping pass from the Roborock app before
  expecting rooms or coordinates.
* **Positions are the robot's own dead reckoning**, not vision. After it is
  picked up and moved, they are wrong until it relocalises.
* **The Z70's arm is not exposed.** Neither are routines, schedules or the
  dock's wash cycle.
* **Untested against hardware.** The command shapes and status mapping are
  covered by tests; nothing here has run against a physical Saros. The first
  real session should start with `MHS_READ_ONLY=1`.
