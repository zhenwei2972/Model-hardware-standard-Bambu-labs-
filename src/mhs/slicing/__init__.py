"""Slicing: turning a mesh into something the printer can actually run.

This is a wrapper around the slicer you already have installed, not a slicer.
OrcaSlicer, Bambu Studio and PrusaSlicer all descend from Slic3r and all accept
per-setting overrides on the command line, so the whole feature is: pick the
settings, build the argv, run it, and read back what the slicer says it did.

The value added over calling the binary yourself is in three places:

* :mod:`.parameters` turns an *intent* - "fast", "fine", "strong" - into
  concrete settings derived from the printer's own nozzle and layer-height
  limits, rather than magic numbers that only suit one machine;
* :mod:`.slicer` reads the finished gcode back and reports what was actually
  applied, so a wrong profile is visible before anything is printed;
* the same result feeds the print journal, so "0.16 mm held the lettering,
  0.2 mm lost it" survives to the next print.
"""

from .parameters import INTENTS, SliceSettings, settings_for_intent
from .slicer import Slicer, SliceResult, SlicerFlavour, find_slicer

__all__ = [
    "INTENTS",
    "SliceResult",
    "SliceSettings",
    "Slicer",
    "SlicerFlavour",
    "find_slicer",
    "settings_for_intent",
]
