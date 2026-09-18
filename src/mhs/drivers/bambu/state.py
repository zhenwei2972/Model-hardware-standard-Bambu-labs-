"""Cache and normalisation of the printer's ``print`` report object.

The A1/A1 mini/P1 series publish *deltas* on ``print.push_status`` and only send
the full object in reply to ``pushing.pushall``. Anything that reads a field
straight off the last MQTT message will therefore see it flicker to ``None``.
:class:`BambuState` keeps a merged view instead.
"""

from __future__ import annotations

import time
from typing import Any

from ...models import (
    DeviceAlert,
    FilamentSlot,
    PrinterStatus,
    PrintState,
    Temperature,
)
from .hms import alert_from_hms, alert_from_print_error

#: ``stg_cur`` -> human readable stage (Bambu's internal enumeration).
STAGES: dict[int, str] = {
    -1: "idle",
    0: "printing",
    1: "auto bed levelling",
    2: "heatbed preheating",
    3: "sweeping XY mech mode",
    4: "changing filament",
    5: "M400 pause",
    6: "paused: filament runout",
    7: "heating hotend",
    8: "calibrating extrusion",
    9: "scanning bed surface",
    10: "inspecting first layer",
    11: "identifying build plate type",
    12: "calibrating micro lidar",
    13: "homing toolhead",
    14: "cleaning nozzle tip",
    15: "checking extruder temperature",
    16: "paused by user",
    17: "paused: front cover falling",
    18: "calibrating lidar",
    19: "calibrating extrusion flow",
    20: "paused: nozzle temperature malfunction",
    21: "paused: bed temperature malfunction",
    22: "filament unloading",
    23: "paused: skipped step",
    24: "filament loading",
    25: "calibrating motor noise",
    26: "paused: AMS lost",
    27: "paused: low heatbreak fan speed",
    28: "paused: chamber temperature control error",
    29: "cooling chamber",
    30: "paused by gcode",
    31: "motor noise showoff",
    32: "paused: nozzle filament covered",
    33: "paused: cutter error",
    34: "paused: first layer error",
    35: "paused: nozzle clog",
}

_GCODE_STATE_MAP = {
    "IDLE": PrintState.IDLE,
    "PREPARE": PrintState.PREPARING,
    "SLICING": PrintState.PREPARING,
    "RUNNING": PrintState.RUNNING,
    "PAUSE": PrintState.PAUSED,
    "FINISH": PrintState.FINISHED,
    "FAILED": PrintState.FAILED,
}

_SPEED_NAMES = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}


def deep_merge(base: dict, delta: dict) -> dict:
    """Recursively merge ``delta`` into ``base`` in place.

    Lists are replaced wholesale: the firmware always resends a complete ``hms``
    or ``ams.ams`` array when any element changes, so merging them element-wise
    would keep stale entries alive.
    """
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _tray_to_slot(tray: dict, label: str, active: bool) -> FilamentSlot:
    material = tray.get("tray_type") or None
    lo, hi = _as_int(tray.get("nozzle_temp_min")), _as_int(tray.get("nozzle_temp_max"))
    remain = tray.get("remain")
    return FilamentSlot(
        slot=label,
        material=material,
        color=(tray.get("tray_color") or None),
        remaining_percent=remain if isinstance(remain, int) and remain >= 0 else None,
        nozzle_temp_range=(lo, hi) if lo and hi else None,
        active=active,
        empty=not material,
    )


class BambuState:
    """Merged view of everything the printer has told us."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.print: dict[str, Any] = {}
        self.info: dict[str, Any] = {}
        self.last_message_at: float | None = None
        self.connected = False

    # -- ingest ------------------------------------------------------------
    def apply(self, payload: dict) -> None:
        """Merge one MQTT report payload."""
        if "print" in payload and isinstance(payload["print"], dict):
            deep_merge(self.print, payload["print"])
            self.last_message_at = time.time()
        if "info" in payload and isinstance(payload["info"], dict):
            deep_merge(self.info, payload["info"])
            self.last_message_at = time.time()
        if "system" in payload and isinstance(payload["system"], dict):
            deep_merge(self.info.setdefault("system", {}), payload["system"])
            self.last_message_at = time.time()

    @property
    def firmware(self) -> str | None:
        for module in self.info.get("module", []) or []:
            if module.get("name") == "ota":
                return module.get("sw_ver")
        return None

    def is_stale(self, max_age_seconds: float) -> bool:
        return self.last_message_at is None or (time.time() - self.last_message_at) > max_age_seconds

    # -- normalise ---------------------------------------------------------
    def to_status(self, stale_after: float = 90.0) -> PrinterStatus:
        p = self.print
        online = self.connected and not self.is_stale(stale_after)
        native = p.get("gcode_state")
        state = PrintState.UNKNOWN
        if native:
            state = _GCODE_STATE_MAP.get(str(native).upper(), PrintState.UNKNOWN)
        if not online:
            state = PrintState.OFFLINE

        stage_id = _as_int(p.get("stg_cur"))
        stage = STAGES.get(stage_id) if stage_id is not None else None
        # `stg_cur` keeps its last value after a job ends; only report it live.
        if state in (PrintState.IDLE, PrintState.FINISHED, PrintState.OFFLINE):
            stage = None

        status = PrinterStatus(
            device_id=self.device_id,
            state=state,
            online=online,
            job_name=p.get("subtask_name") or p.get("gcode_file") or None,
            progress_percent=_as_int(p.get("mc_percent")),
            current_layer=_as_int(p.get("layer_num")),
            total_layers=_as_int(p.get("total_layer_num")),
            remaining_minutes=_as_int(p.get("mc_remaining_time")),
            stage=stage,
            nozzle=Temperature(_as_float(p.get("nozzle_temper")), _as_float(p.get("nozzle_target_temper"))),
            bed=Temperature(_as_float(p.get("bed_temper")), _as_float(p.get("bed_target_temper"))),
            chamber=Temperature(_as_float(p.get("chamber_temper")), None),
            speed_level=_SPEED_NAMES.get(_as_int(p.get("spd_lvl")) or 0),
            speed_percent=_as_int(p.get("spd_mag")),
            wifi_signal=p.get("wifi_signal"),
            sd_card=p.get("sdcard"),
            native_state=native,
        )

        for name, key in (
            ("part_cooling", "cooling_fan_speed"),
            ("aux", "big_fan1_speed"),
            ("chamber", "big_fan2_speed"),
            ("heatbreak", "heatbreak_fan_speed"),
        ):
            raw = _as_int(p.get(key))
            if raw is not None:
                # Reported 0-15 gear value on some firmwares, 0-100 on others.
                status.fans[name] = raw

        for light in p.get("lights_report", []) or []:
            if light.get("node"):
                status.lights[light["node"]] = light.get("mode", "unknown")

        status.filament = self._filament_slots()
        status.alerts = self._alerts()
        return status

    def _filament_slots(self) -> list[FilamentSlot]:
        p = self.print
        slots: list[FilamentSlot] = []
        ams_root = p.get("ams") or {}
        active = _as_int(ams_root.get("tray_now"))
        for unit in ams_root.get("ams", []) or []:
            unit_id = _as_int(unit.get("id")) or 0
            for tray in unit.get("tray", []) or []:
                tray_id = _as_int(tray.get("id")) or 0
                global_id = unit_id * 4 + tray_id
                label = f"AMS{unit_id + 1}-{tray_id + 1}"
                slots.append(_tray_to_slot(tray, label, active == global_id))
        vt = p.get("vt_tray")
        if isinstance(vt, dict):
            slots.append(_tray_to_slot(vt, "external", active == 254))
        return slots

    def _alerts(self) -> list[DeviceAlert]:
        alerts = [alert_from_hms(e) for e in (self.print.get("hms") or []) if isinstance(e, dict)]
        err = _as_int(self.print.get("print_error")) or 0
        print_alert = alert_from_print_error(err)
        if print_alert:
            alerts.append(print_alert)
        return alerts
