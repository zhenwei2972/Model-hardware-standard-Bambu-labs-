"""Decoding of Bambu Lab HMS codes and print errors.

The printer reports faults as two 32-bit integers (``attr``/``code``). Bambu's
wiki indexes them as ``HMS_AAAA_BBBB_CCCC_DDDD``; ``print_error`` uses the same
hex convention with two groups.

The tables below cover the codes an A1 / A1 mini / P1 is most likely to raise.
Meanings come from Bambu's published code list (``e.bambulab.com/query.php``,
version 202609231145), reworded to be short; the force-sensor entry is not in
that list and comes from Bambu forum reports. A code missing here still gets a
wiki link and a note saying which part of the machine it belongs to.
"""

from __future__ import annotations

import re

from ...models import DeviceAlert

WIKI_BASE = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode"

# Severity lives in the high half of `code`.
_SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}

#: ``hms`` array entries, keyed ``AAAA_BBBB_CCCC_DDDD``.
HMS_MESSAGES: dict[str, str] = {
    # Bed levelling and the force sensor (A1 / A1 mini use the nozzle as the probe).
    "0300_1800_0001_0003": (
        "Extrusion force sensor error, seen during auto bed levelling on the A1 / A1 mini. "
        "If it stays after clear_print_error, power-cycle the printer."
    ),
    "0300_1800_0001_0006": "Bed levelling data is abnormal; check the plate and Z slider for debris",
    "0300_0D00_0001_000B": "The Z axis motor seems stuck; check the Z slider for debris",
    "0300_0D00_0002_0001": "Bed homing abnormal: a bump on the plate or a dirty nozzle tip",
    # Heaters.
    "0300_0100_0001_0001": "Heatbed temperature abnormal: the heater may be short-circuited",
    "0300_0100_0001_0002": "Heatbed temperature abnormal: heater or thermal switch open circuit",
    "0300_0100_0001_0003": "Heatbed over temperature",
    "0300_0100_0001_0006": "Heatbed temperature sensor may be short-circuited",
    "0300_0100_0001_0007": "Heatbed temperature sensor may be disconnected (open circuit)",
    "0300_0200_0001_0001": "Nozzle temperature abnormal: the heater may be short-circuited",
    "0300_0200_0001_0002": "Nozzle temperature abnormal: the heater may be open circuit",
    "0300_0200_0001_0003": "Nozzle over temperature",
    "0300_0200_0001_0006": "Nozzle temperature sensor may be short-circuited",
    "0300_0200_0001_0007": "Nozzle temperature sensor may be disconnected (open circuit)",
    # Fans.
    "0300_0300_0001_0001": "Hotend cooling fan too slow or stopped; check it is not stuck or unplugged",
    "0300_0300_0002_0002": "Hotend cooling fan running slowly; check it for debris",
    "0300_0400_0002_0001": "Part cooling fan too slow or stopped; check it is not stuck or unplugged",
    # Nozzle and motion.
    "0300_1A00_0002_0001": "Nozzle covered in filament, or the build plate is not seated flat",
    "0300_1A00_0002_0002": "Nozzle clogged with filament",
    "0300_2000_0001_0001": "X axis homing abnormal: the toolhead may be stuck",
    "0300_2000_0001_0002": "Y axis homing abnormal: the bed or carriage may be stuck",
    "0300_4000_0002_0003": "The sliced file failed checks several times; the job was cancelled",
    # Storage, network and jobs.
    "0500_0100_0003_0006": "The microSD card is not formatted",
    "0500_0100_0003_0007": "No microSD card; the camera cannot record a timelapse",
    "0500_0200_0002_0001": "Could not connect to the internet",
    "0500_0300_0002_000D": "The microSD card controller is malfunctioning",
    "0500_0300_0002_0020": "The microSD card is too small to cache print files",
    "0500_0400_0001_0003": "The job file could not be read; send it again",
    "0500_0400_0001_0006": "Could not resume the previous print",
    # External spool (no AMS).
    "07FF_2000_0002_0001": "External spool has run out of filament",
    "07FF_2000_0002_0002": "No filament detected from the external spool",
    "07FF_2000_0002_0004": "Pull the external filament out of the extruder",
    # First layer inspection (lidar models).
    "0C00_0300_0002_0001": "First layer inspection: laser reflection too weak on this material",
}

#: ``print_error`` values, keyed ``AAAA_BBBB``.
PRINT_ERROR_MESSAGES: dict[str, str] = {
    "0300_4000": "Z axis homing failed; the job was stopped",
    "0300_4001": "Timed out waiting for the nozzle to cool before homing",
    "0300_4002": "Auto bed levelling failed; the job was stopped",
    "0300_4005": "Hotend cooling fan speed abnormal",
    "0300_4006": "Nozzle clogged",
    "0300_4008": "The AMS failed to change filament",
    "0300_4009": "XY axis homing failed",
    "0300_400A": "Resonance frequency calibration failed",
    "0300_400B": "Internal communication error",
    "0300_400C": "Print cancelled",
    "0300_400D": "Resume after power loss failed",
    "0300_400E": "Motor self-check failed",
    "0300_8000": "Paused for an unknown reason",
    "0300_8001": "Paused by the user",
    "0300_8003": "Paused: AI print monitoring saw spaghetti",
    "0300_8004": "Paused: filament ran out",
    "0300_8007": "A job was unfinished when the printer lost power",
    "0300_8008": "Nozzle temperature malfunction",
    "0300_8009": "Heatbed temperature malfunction",
    "0300_800D": "Paused: the extruder is not extruding normally",
    "0300_800E": "The print file is not available; was the storage removed?",
    "0300_8011": "The fitted build plate does not match the one in the G-code",
    "0300_8013": "Paused by a pause command in the file",
    "0300_8014": "Nozzle covered in filament, or the build plate is fitted wrong",
    "0300_8016": "Nozzle clogged with filament",
    "0300_8017": "Something is on the heatbed; clear it and resume",
    "0500_4001": "Could not connect to Bambu Cloud",
    "0500_4002": "Unsupported file path or name",
    "0500_4003": "The printer could not parse the file",
    "0500_4004": "The printer is busy and cannot start a new job",
    "0500_4005": "Jobs cannot be sent while firmware is updating",
    "0500_4006": "Not enough free storage for the job",
    "0500_4008": "Starting the job failed; power-cycle the printer and send it again",
    "0500_400A": "The file name is not supported",
    "0500_400C": "Insert a microSD card and send the job again",
    "0500_400E": "Print cancelled",
    "0500_4037": "The file was sliced for a different printer model",
    "0500_4038": "The file's nozzle diameter does not match the printer's",
    "0500_C010": "microSD card read/write error",
    "07FF_8010": "The external spool or its filament is stuck",
    "07FF_8011": "External spool has run out of filament",
    "07FF_8013": "Timed out purging the old filament; is it stuck or the extruder clogged?",
}

# AMS run-outs follow one pattern across units A-D and slots 1-4:
# 07UU_2S00_0002_0001, unit UU = 00..03, slot S = 0..3.
_AMS_RUNOUT = re.compile(r"^070([0-3])_2([0-3])00_0002_0001$")

# Which part of the machine a module (first group) belongs to, for codes the
# tables do not list.
_MODULES = {
    "0300": "motion controller: heaters, fans, motors, homing",
    "0500": "system: storage, network, camera, jobs",
    "0700": "AMS A",
    "0701": "AMS B",
    "0702": "AMS C",
    "0703": "AMS D",
    "07FF": "external spool",
    "0C00": "camera / first layer inspection",
}


def format_hms_code(attr: int, code: int) -> str:
    """``(0x0300_0100, 0x0002_0001) -> '0300_0100_0002_0001'``."""
    return (
        f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}_"
        f"{(code >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"
    )


def severity_of(code: int) -> str:
    return _SEVERITY.get((code >> 16) & 0xFFFF, "unknown")


def _unlisted(formatted: str) -> str:
    module = _MODULES.get(formatted[:4])
    where = f" ({module})" if module else ""
    return f"Code not in the bundled table{where}; see the wiki link"


def describe_hms(formatted: str) -> str:
    """Plain-language meaning of an ``AAAA_BBBB_CCCC_DDDD`` code."""
    if formatted in HMS_MESSAGES:
        return HMS_MESSAGES[formatted]
    runout = _AMS_RUNOUT.match(formatted)
    if runout:
        unit = "ABCD"[int(runout.group(1))]
        return f"AMS {unit} slot {int(runout.group(2)) + 1} has run out of filament"
    return _unlisted(formatted)


def describe_print_error(formatted: str) -> str:
    """Plain-language meaning of an ``AAAA_BBBB`` print error."""
    return PRINT_ERROR_MESSAGES.get(formatted) or _unlisted(formatted)


def alert_from_hms(entry: dict) -> DeviceAlert:
    """Build a :class:`DeviceAlert` from one element of the ``hms`` array."""
    attr = int(entry.get("attr", 0) or 0)
    code = int(entry.get("code", 0) or 0)
    formatted = format_hms_code(attr, code)
    return DeviceAlert(
        code=f"HMS_{formatted}",
        severity=severity_of(code),
        message=describe_hms(formatted),
        url=f"{WIKI_BASE}?e={formatted}",
    )


def format_print_error(print_error: int) -> str:
    """``print_error`` is a single 32-bit code, rendered as ``AAAA_BBBB``."""
    return f"{(print_error >> 16) & 0xFFFF:04X}_{print_error & 0xFFFF:04X}"


def alert_from_print_error(print_error: int) -> DeviceAlert | None:
    if not print_error:
        return None
    formatted = format_print_error(print_error)
    return DeviceAlert(
        code=f"PRINT_ERROR_{formatted}",
        severity="serious",
        message=describe_print_error(formatted),
        url=f"{WIKI_BASE}?e={formatted}",
    )
