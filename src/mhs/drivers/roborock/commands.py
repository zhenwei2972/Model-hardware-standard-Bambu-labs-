"""Builders for the Roborock RPC commands this driver sends.

Pure functions, as with the Bambu driver: the parameter shapes are the part
most likely to be wrong and the easiest to get wrong silently - a malformed
segment list makes the robot sit still rather than return an error - so they
are built and validated in one place, and tested directly.
"""

from __future__ import annotations

from ...errors import CommandRejected

#: The robot's map frame is centred near 25500 mm; anything far outside the
#: plausible map extent is a coordinate mix-up, not a place.
MAP_MIN_MM = 0
MAP_MAX_MM = 51200

START = "app_start"
STOP = "app_stop"
PAUSE = "app_pause"
RESUME = "app_start"  # the firmware resumes a paused job with the same verb
DOCK = "app_charge"
GOTO = "app_goto_target"
SEGMENT_CLEAN = "app_segment_clean"
ZONE_CLEAN = "app_zoned_clean"
SPOT_CLEAN = "app_spot"
LOCATE = "find_me"
SET_FAN_POWER = "set_custom_mode"
SET_WATER_LEVEL = "set_water_box_custom_mode"
GET_STATUS = "get_status"
GET_ROOM_MAPPING = "get_room_mapping"


def _check_point(x_mm: float, y_mm: float) -> tuple[int, int]:
    for axis, value in (("x", x_mm), ("y", y_mm)):
        if not MAP_MIN_MM <= value <= MAP_MAX_MM:
            raise CommandRejected(
                f"{axis}={value:g} mm is outside the map's coordinate range "
                f"({MAP_MIN_MM}-{MAP_MAX_MM} mm)",
                hint="Read coordinates off the annotated map; the frame is centred near "
                     "25500,25500, not on zero.",
            )
    return int(round(x_mm)), int(round(y_mm))


def goto(x_mm: float, y_mm: float) -> tuple[str, list]:
    """Drive to a point and stop there. Params are ``[x, y]`` in millimetres."""
    return GOTO, list(_check_point(x_mm, y_mm))


def segment_clean(segment_ids: list[int], repeat: int = 1) -> tuple[str, list]:
    """Clean whole rooms by segment id.

    Current firmware wants ``[{"segments": [...], "repeat": n}]``; the bare list
    form is what very old firmware accepted and is no longer sent.
    """
    if not segment_ids:
        raise CommandRejected("no rooms given", hint="Call list_rooms to see what is mapped.")
    segments = []
    for value in segment_ids:
        try:
            segments.append(int(value))
        except (TypeError, ValueError):
            raise CommandRejected(f"room segment id must be a number, got {value!r}") from None
    if len(segments) > 32:
        raise CommandRejected("at most 32 rooms can be queued in one command")
    return SEGMENT_CLEAN, [{"segments": segments, "repeat": _check_repeat(repeat)}]


def zone_clean(zones_mm: list[tuple[float, float, float, float]], repeat: int = 1) -> tuple[str, list]:
    """Clean rectangles: ``[[x1, y1, x2, y2, repeats], ...]`` in millimetres."""
    if not zones_mm:
        raise CommandRejected("no zone given")
    if len(zones_mm) > 5:
        raise CommandRejected("at most 5 zones per command")
    repeats = _check_repeat(repeat)
    payload = []
    for zone in zones_mm:
        if len(zone) != 4:
            raise CommandRejected(f"a zone is (x1, y1, x2, y2) in mm, got {zone!r}")
        x1, y1 = _check_point(zone[0], zone[1])
        x2, y2 = _check_point(zone[2], zone[3])
        # The firmware expects a bottom-left/top-right pair; normalise so a
        # corner-swapped rectangle still cleans instead of being ignored.
        low_x, high_x = sorted((x1, x2))
        low_y, high_y = sorted((y1, y2))
        if high_x - low_x < 200 or high_y - low_y < 200:
            raise CommandRejected("a cleaning zone must be at least 200 mm on each side")
        payload.append([low_x, low_y, high_x, high_y, repeats])
    return ZONE_CLEAN, payload


def set_fan_power(code: int) -> tuple[str, list]:
    return SET_FAN_POWER, [int(code)]


def set_water_level(code: int) -> tuple[str, list]:
    return SET_WATER_LEVEL, [int(code)]


def simple(command: str) -> tuple[str, list]:
    """A command that takes no parameters."""
    return command, []


def _check_repeat(repeat: int) -> int:
    try:
        value = int(repeat)
    except (TypeError, ValueError):
        raise CommandRejected(f"repeat must be a number, got {repeat!r}") from None
    if not 1 <= value <= 3:
        raise CommandRejected("repeat must be between 1 and 3")
    return value
