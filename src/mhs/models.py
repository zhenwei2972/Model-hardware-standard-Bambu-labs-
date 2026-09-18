"""Vendor-neutral data model.

Every driver normalises its native telemetry into :class:`PrinterStatus`, so the
MCP tools, the scheduler and the CLI never have to know whether they are talking
to a Bambu Lab A1 mini or something else.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum


class Capability(str, Enum):
    """Optional features a driver may advertise."""

    START_PRINT = "start_print"
    PAUSE_RESUME = "pause_resume"
    STOP = "stop"
    FILE_LIST = "file_list"
    FILE_UPLOAD = "file_upload"
    CAMERA_SNAPSHOT = "camera_snapshot"
    TEMPERATURE_CONTROL = "temperature_control"
    RAW_GCODE = "raw_gcode"
    AMS = "ams"
    LIGHT = "light"
    SPEED = "speed"
    SKIP_OBJECTS = "skip_objects"
    CALIBRATION = "calibration"


class PrintState(str, Enum):
    """Coarse lifecycle state, comparable across vendors."""

    OFFLINE = "offline"
    IDLE = "idle"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def is_active(self) -> bool:
        return self in (PrintState.PREPARING, PrintState.RUNNING, PrintState.PAUSED)

    @property
    def accepts_new_job(self) -> bool:
        return self in (PrintState.IDLE, PrintState.FINISHED, PrintState.FAILED)


@dataclass(frozen=True)
class PrinterInfo:
    """Static identity of a device."""

    printer_id: str
    driver: str
    model: str
    host: str | None = None
    serial: str | None = None
    firmware: str | None = None
    build_volume_mm: tuple[int, int, int] | None = None
    capabilities: tuple[Capability, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["capabilities"] = [c.value for c in self.capabilities]
        data["build_volume_mm"] = list(self.build_volume_mm) if self.build_volume_mm else None
        return data


@dataclass
class Temperature:
    current: float | None = None
    target: float | None = None

    def to_dict(self) -> dict:
        return {"current": self.current, "target": self.target}


@dataclass
class FilamentSlot:
    """One AMS tray, or the external spool."""

    slot: str
    material: str | None = None
    color: str | None = None
    remaining_percent: int | None = None
    nozzle_temp_range: tuple[int, int] | None = None
    active: bool = False
    empty: bool = True

    def to_dict(self) -> dict:
        data = asdict(self)
        data["nozzle_temp_range"] = list(self.nozzle_temp_range) if self.nozzle_temp_range else None
        return data


@dataclass
class DeviceAlert:
    """A health-management / error code reported by the device."""

    code: str
    severity: str = "unknown"
    message: str | None = None
    url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FileEntry:
    name: str
    path: str
    size_bytes: int | None = None
    modified: str | None = None
    is_dir: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PrinterStatus:
    """Snapshot of what a printer is doing right now."""

    printer_id: str
    state: PrintState = PrintState.UNKNOWN
    online: bool = False
    job_name: str | None = None
    progress_percent: int | None = None
    current_layer: int | None = None
    total_layers: int | None = None
    remaining_minutes: int | None = None
    stage: str | None = None
    nozzle: Temperature = field(default_factory=Temperature)
    bed: Temperature = field(default_factory=Temperature)
    chamber: Temperature = field(default_factory=Temperature)
    speed_level: str | None = None
    speed_percent: int | None = None
    fans: dict[str, int] = field(default_factory=dict)
    lights: dict[str, str] = field(default_factory=dict)
    filament: list[FilamentSlot] = field(default_factory=list)
    alerts: list[DeviceAlert] = field(default_factory=list)
    wifi_signal: str | None = None
    sd_card: bool | None = None
    native_state: str | None = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "printer_id": self.printer_id,
            "state": self.state.value,
            "online": self.online,
            "job_name": self.job_name,
            "progress_percent": self.progress_percent,
            "current_layer": self.current_layer,
            "total_layers": self.total_layers,
            "remaining_minutes": self.remaining_minutes,
            "stage": self.stage,
            "nozzle": self.nozzle.to_dict(),
            "bed": self.bed.to_dict(),
            "chamber": self.chamber.to_dict(),
            "speed_level": self.speed_level,
            "speed_percent": self.speed_percent,
            "fans": self.fans,
            "lights": self.lights,
            "filament": [f.to_dict() for f in self.filament],
            "alerts": [a.to_dict() for a in self.alerts],
            "wifi_signal": self.wifi_signal,
            "sd_card": self.sd_card,
            "native_state": self.native_state,
            "updated_at": self.updated_at,
        }

    def summary(self) -> str:
        """One-line human/LLM readable digest."""
        if not self.online:
            return f"{self.printer_id}: offline"
        bits = [f"{self.printer_id}: {self.state.value}"]
        if self.job_name:
            bits.append(f"job={self.job_name!r}")
        if self.progress_percent is not None and self.state.is_active:
            layers = ""
            if self.current_layer is not None and self.total_layers:
                layers = f" layer {self.current_layer}/{self.total_layers}"
            bits.append(f"{self.progress_percent}%{layers}")
        if self.remaining_minutes and self.state.is_active:
            bits.append(f"~{self.remaining_minutes} min left")
        if self.nozzle.current is not None:
            bits.append(f"nozzle {self.nozzle.current:.0f}/{self.nozzle.target or 0:.0f}C")
        if self.bed.current is not None:
            bits.append(f"bed {self.bed.current:.0f}/{self.bed.target or 0:.0f}C")
        if self.alerts:
            bits.append(f"{len(self.alerts)} alert(s): " + ", ".join(a.code for a in self.alerts[:3]))
        return ", ".join(bits)


@dataclass
class PrintOptions:
    """Slicer-independent knobs accepted by :meth:`Printer.start_print`."""

    plate: int = 1
    use_ams: bool = False
    ams_mapping: list[int] | None = None
    bed_leveling: bool = True
    flow_calibration: bool = True
    vibration_calibration: bool = True
    layer_inspect: bool = True
    timelapse: bool = False
    job_name: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
