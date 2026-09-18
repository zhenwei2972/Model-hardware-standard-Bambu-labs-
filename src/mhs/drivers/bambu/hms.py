"""Decoding of Bambu Lab HMS codes and print errors.

The printer reports faults as two 32-bit integers (``attr``/``code``). Bambu's
wiki indexes them as ``HMS_AAAA_BBBB_CCCC_DDDD``; ``print_error`` uses the same
hex convention with two groups.
"""

from __future__ import annotations

from ...models import DeviceAlert

WIKI_BASE = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode"

# Severity lives in the high half of `code`.
_SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}

# A few codes worth explaining inline; anything unknown still gets a wiki link.
_KNOWN: dict[str, str] = {
    "0300_0100_0002_0001": "Nozzle temperature malfunction",
    "0300_1300_0002_0001": "Heatbed temperature malfunction",
    "0700_2000_0002_0001": "Filament ran out",
    "0700_1000_0002_0001": "Filament may be tangled or stuck",
    "0C00_0300_0002_0001": "First layer inspection found a defect",
    "1200_0200_0002_0001": "AMS filament could not be loaded",
}


def format_hms_code(attr: int, code: int) -> str:
    """``(0x0300_0100, 0x0002_0001) -> '0300_0100_0002_0001'``."""
    return (
        f"{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}_"
        f"{(code >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"
    )


def severity_of(code: int) -> str:
    return _SEVERITY.get((code >> 16) & 0xFFFF, "unknown")


def alert_from_hms(entry: dict) -> DeviceAlert:
    """Build a :class:`DeviceAlert` from one element of the ``hms`` array."""
    attr = int(entry.get("attr", 0) or 0)
    code = int(entry.get("code", 0) or 0)
    formatted = format_hms_code(attr, code)
    return DeviceAlert(
        code=f"HMS_{formatted}",
        severity=severity_of(code),
        message=_KNOWN.get(formatted),
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
        message=_KNOWN.get(formatted),
        url=f"{WIKI_BASE}?e={formatted}",
    )
