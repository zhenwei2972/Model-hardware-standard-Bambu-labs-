"""Builders for the JSON commands accepted on ``device/<serial>/request``.

Pure functions: no sockets, no state. That makes the wire format the easiest part
of this driver to unit-test, and keeps payload quirks documented in one place.
"""

from __future__ import annotations

from ...errors import CommandRejected

SPEED_LEVELS = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}

#: Raw gcode considered safe enough to expose by default. Anything that writes
#: firmware settings, moves without homing or starts a print is deliberately out.
SAFE_GCODE_PREFIXES = (
    "M104",  # set nozzle temperature
    "M140",  # set bed temperature
    "M106",  # fan speed
    "M107",  # fan off
    "M17",   # enable steppers
    "M18",   # disable steppers
    "M400",  # wait for moves to finish
    "G28",   # home
    "G29",   # bed level
    "M105",  # report temperatures
    "M114",  # report position
    "M115",  # report firmware
    "M900",  # k-factor / pressure advance query
)


def _envelope(kind: str, command: str, sequence_id: int, **params: object) -> dict:
    payload = {"sequence_id": str(sequence_id), "command": command}
    payload.update({k: v for k, v in params.items() if v is not None})
    return {kind: payload}


def push_all(sequence_id: int = 0) -> dict:
    """Ask for a full status object.

    The A1/P1 series only send deltas on ``push_status``; a periodic ``pushall``
    re-synchronises the cached state. Do not call it more often than every few
    minutes - the printer's MCU is slow and it will stutter mid-print.
    """
    return _envelope("pushing", "pushall", sequence_id, version=1, push_target=1)


def get_version(sequence_id: int = 0) -> dict:
    return _envelope("info", "get_version", sequence_id)


def pause(sequence_id: int = 0) -> dict:
    return _envelope("print", "pause", sequence_id, param="")


def resume(sequence_id: int = 0) -> dict:
    return _envelope("print", "resume", sequence_id, param="")


def stop(sequence_id: int = 0) -> dict:
    return _envelope("print", "stop", sequence_id, param="")


def set_speed(level: int, sequence_id: int = 0) -> dict:
    if level not in SPEED_LEVELS:
        raise CommandRejected(f"speed level must be one of {sorted(SPEED_LEVELS)} (got {level!r})")
    return _envelope("print", "print_speed", sequence_id, param=str(level))


def gcode_line(gcode: str, sequence_id: int = 0) -> dict:
    lines = [ln.strip() for ln in gcode.replace("\r", "").split("\n") if ln.strip()]
    if not lines:
        raise CommandRejected("empty gcode")
    return _envelope("print", "gcode_line", sequence_id, param="\n".join(lines) + "\n")


def assert_gcode_allowed(gcode: str) -> None:
    """Reject gcode outside :data:`SAFE_GCODE_PREFIXES`."""
    for line in gcode.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        token = line.split()[0].upper()
        if not any(token == p or token.startswith(p) for p in SAFE_GCODE_PREFIXES):
            raise CommandRejected(
                f"gcode {token!r} is not in the safe list",
                hint="Set allow_raw_gcode = true in config.toml to lift this guard.",
            )


def set_temperature(nozzle: float | None = None, bed: float | None = None, sequence_id: int = 0) -> dict:
    """Temperatures are set with plain gcode; there is no dedicated JSON command."""
    lines = []
    if nozzle is not None:
        if not 0 <= nozzle <= 300:
            raise CommandRejected("nozzle target must be 0-300 C")
        lines.append(f"M104 S{int(nozzle)}")
    if bed is not None:
        if not 0 <= bed <= 120:
            raise CommandRejected("bed target must be 0-120 C")
        lines.append(f"M140 S{int(bed)}")
    if not lines:
        raise CommandRejected("set_temperature needs a nozzle and/or bed target")
    return gcode_line("\n".join(lines), sequence_id)


def led_control(
    on: bool,
    node: str = "chamber_light",
    sequence_id: int = 0,
) -> dict:
    if node not in {"chamber_light", "work_light"}:
        raise CommandRejected("led node must be 'chamber_light' or 'work_light'")
    return _envelope(
        "system",
        "ledctrl",
        sequence_id,
        led_node=node,
        led_mode="on" if on else "off",
        # Required by the firmware even when not flashing.
        led_on_time=500,
        led_off_time=500,
        loop_times=0,
        interval_time=0,
    )


def skip_objects(object_ids: list[int], sequence_id: int = 0) -> dict:
    if not object_ids:
        raise CommandRejected("skip_objects needs at least one object id")
    return _envelope("print", "skip_objects", sequence_id, obj_list=[int(i) for i in object_ids])


def calibration(
    bed_level: bool = True,
    vibration: bool = True,
    motor_noise: bool = False,
    lidar: bool = False,
    sequence_id: int = 0,
) -> dict:
    """Bitmask per OpenBambuAPI: lidar<<0, bed<<1, vibration<<2, motor<<3."""
    option = (int(lidar) << 0) | (int(bed_level) << 1) | (int(vibration) << 2) | (int(motor_noise) << 3)
    if option == 0:
        raise CommandRejected("select at least one calibration routine")
    return _envelope("print", "calibration", sequence_id, option=option)


def build_ams_mapping(slots: list[int]) -> list[int]:
    """Right-align AMS slot assignments into the fixed 5-element array.

    ``[2] -> [-1, -1, -1, -1, 2]`` and ``[0, 3] -> [-1, -1, -1, 0, 3]``.
    A wrong mapping makes the printer sit and wait instead of printing, so this is
    validated rather than passed through.
    """
    if len(slots) > 4:
        raise CommandRejected("at most 4 AMS slots can be mapped")
    for slot in slots:
        if not 0 <= int(slot) <= 3:
            raise CommandRejected(f"AMS slot must be 0-3 (got {slot!r})")
    return [-1] * (5 - len(slots)) + [int(s) for s in slots]


def project_file(
    remote_path: str,
    *,
    plate: int = 1,
    use_ams: bool = False,
    ams_mapping: list[int] | None = None,
    bed_leveling: bool = True,
    flow_calibration: bool = True,
    vibration_calibration: bool = True,
    layer_inspect: bool = True,
    timelapse: bool = False,
    job_name: str | None = None,
    sequence_id: int = 0,
) -> dict:
    """Start a sliced ``.3mf`` that already lives on the printer's storage.

    ``remote_path`` is the path on the printer ("cache/foo.3mf" or "/foo.3mf");
    it is turned into the ``ftp:///...`` URL form the firmware expects.
    """
    if plate < 1:
        raise CommandRejected("plate index is 1-based")
    if not remote_path.lower().endswith((".3mf", ".gcode", ".gcode.3mf")):
        raise CommandRejected(f"expected a .3mf or .gcode file, got {remote_path!r}")
    clean = remote_path.lstrip("/")
    mapping = ams_mapping if ams_mapping is not None else ([] if not use_ams else [0])
    return _envelope(
        "print",
        "project_file",
        sequence_id,
        param=f"Metadata/plate_{plate}.gcode",
        url=f"ftp:///{clean}",
        subtask_name=job_name or clean.rsplit("/", 1)[-1].rsplit(".", 1)[0],
        # All four ids are 0 for a LAN print; non-zero values make the printer
        # try to reconcile the job with a cloud task and refuse to start.
        project_id="0",
        profile_id="0",
        task_id="0",
        subtask_id="0",
        md5="",
        bed_type="auto",
        bed_levelling=bool(bed_leveling),
        flow_cali=bool(flow_calibration),
        vibration_cali=bool(vibration_calibration),
        layer_inspect=bool(layer_inspect),
        timelapse=bool(timelapse),
        use_ams=bool(use_ams),
        ams_mapping=build_ams_mapping(mapping) if use_ams else "",
    )
