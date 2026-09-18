"""Normalising Roborock's status trait into the vendor-neutral model."""

from __future__ import annotations

from typing import Any

from ...models import DeviceAlert, Position, VacuumState, VacuumStatus

#: Roborock's own state names, lower-cased, mapped onto the neutral lifecycle.
#: Unlisted names fall through to UNKNOWN rather than being guessed at.
_STATE_MAP: dict[str, VacuumState] = {
    "starting": VacuumState.CLEANING,
    "charger disconnected": VacuumState.IDLE,
    "idle": VacuumState.IDLE,
    "remote control active": VacuumState.CLEANING,
    "cleaning": VacuumState.CLEANING,
    "returning home": VacuumState.RETURNING,
    "manual mode": VacuumState.CLEANING,
    "charging": VacuumState.CHARGING,
    "charging problem": VacuumState.ERROR,
    "paused": VacuumState.PAUSED,
    "spot cleaning": VacuumState.CLEANING,
    "error": VacuumState.ERROR,
    "shutting down": VacuumState.IDLE,
    "updating": VacuumState.IDLE,
    "docking": VacuumState.RETURNING,
    "going to target": VacuumState.CLEANING,
    "zoned cleaning": VacuumState.CLEANING,
    "segment cleaning": VacuumState.CLEANING,
    "emptying the bin": VacuumState.DOCKED,
    "washing the mop": VacuumState.DOCKED,
    "going to wash the mop": VacuumState.RETURNING,
    "in call": VacuumState.IDLE,
    "mapping": VacuumState.MAPPING,
    "egg attack": VacuumState.CLEANING,
    "patrol": VacuumState.CLEANING,
    "attaching the mop": VacuumState.DOCKED,
    "detaching the mop": VacuumState.DOCKED,
    "charging complete": VacuumState.DOCKED,
    "device offline": VacuumState.OFFLINE,
    "locked": VacuumState.ERROR,
    "air drying": VacuumState.DOCKED,
    "robot status full": VacuumState.IDLE,
    "sleeping": VacuumState.IDLE,
    "unknown": VacuumState.UNKNOWN,
}

_CHARGING_STATES = {VacuumState.CHARGING}
_DOCKED_STATES = {VacuumState.DOCKED, VacuumState.CHARGING}


def _get(source: Any, *names: str) -> Any:
    """First attribute that exists and is not None. The trait's shape varies by model."""
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def map_state(native: str | None) -> VacuumState:
    if not native:
        return VacuumState.UNKNOWN
    return _STATE_MAP.get(str(native).strip().lower(), VacuumState.UNKNOWN)


def to_status(
    device_id: str,
    trait: Any,
    *,
    online: bool = True,
    position: Position | None = None,
    map_name: str | None = None,
) -> VacuumStatus:
    """Build a :class:`VacuumStatus` from the library's status trait."""
    native = _get(trait, "state_name")
    state = map_state(native) if online else VacuumState.OFFLINE

    battery = _get(trait, "battery")
    charge_status = _get(trait, "charge_status")
    charging = state in _CHARGING_STATES or bool(charge_status)
    if state is VacuumState.CHARGING and battery == 100:
        state = VacuumState.DOCKED

    error_code = _get(trait, "error_code")
    error_name = _get(trait, "error_code_name")
    alert = None
    # Roborock reports 0 / "none" when healthy; only a real fault becomes an alert.
    if error_code and str(error_code) not in {"0", "none"} and str(error_name).lower() not in {"none", ""}:
        alert = DeviceAlert(
            code=f"ROBOROCK_{error_code}",
            severity="serious",
            message=str(error_name) if error_name else None,
        )

    clean_area = _get(trait, "square_meter_clean_area", "clean_area")
    if clean_area is not None and clean_area > 1000:
        clean_area = clean_area / 1_000_000  # raw mm2 on some firmwares

    clean_time = _get(trait, "clean_time")
    if clean_time is not None and clean_time > 600:
        clean_time = round(clean_time / 60)  # seconds on some firmwares

    attached = _get(trait, "water_box_attached")
    return VacuumStatus(
        device_id=device_id,
        state=state,
        online=online,
        battery_percent=int(battery) if battery is not None else None,
        charging=charging,
        fan_power=_get(trait, "fan_power_name", "current_cleaning_mode_name"),
        water_level=_get(trait, "water_box_mode_name", "mop_mode_name"),
        mop_attached=bool(attached) if attached is not None else None,
        cleaned_area_m2=round(float(clean_area), 2) if clean_area is not None else None,
        cleaning_time_minutes=int(clean_time) if clean_time is not None else None,
        position=position,
        current_map=map_name,
        error=alert,
        dock_state=_get(trait, "dock_error_status_name", "dock_state_name"),
        native_state=str(native) if native else None,
    )


def docked(state: VacuumState) -> bool:
    return state in _DOCKED_STATES
