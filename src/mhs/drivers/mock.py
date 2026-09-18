"""An in-process fake printer.

Exists for three reasons: the test suite runs without hardware, `MHS_MOCK=1`
lets someone wire up Claude and try every tool before touching a real machine,
and it documents the minimum a new driver has to implement.
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

from ..config import PrinterConfig
from ..device import Printer
from ..errors import DeviceBusy, FileTransferError
from ..models import (
    Capability,
    DeviceAlert,
    FilamentSlot,
    FileEntry,
    PrinterInfo,
    PrinterStatus,
    PrintOptions,
    PrintState,
    Temperature,
)

# A 1x1 JPEG: enough for the image plumbing to be exercised end to end.
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


class MockPrinter(Printer):
    """Simulates an A1 mini closely enough to exercise every tool."""

    driver_name = "mock"

    #: Simulated wall-clock length of a print, in seconds.
    print_duration = 60.0
    total_layers = 120

    def __init__(self, config: PrinterConfig) -> None:
        super().__init__(config.printer_id)
        self.config = config
        self.connected = False
        self.files: dict[str, FileEntry] = {
            "cache/benchy.3mf": FileEntry("benchy.3mf", "cache/benchy.3mf", 1_048_576),
        }
        self._state = PrintState.IDLE
        self._job: str | None = None
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self.lights = {"chamber_light": "off"}
        self.speed_level = "standard"
        self.commands: list[tuple[str, dict]] = []
        self.alerts: list[DeviceAlert] = []
        #: Tests set this to make an operation fail.
        self.fail_next: str | None = None
        self.now = time.monotonic

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability.START_PRINT,
            Capability.PAUSE_RESUME,
            Capability.STOP,
            Capability.FILE_LIST,
            Capability.FILE_UPLOAD,
            Capability.CAMERA_SNAPSHOT,
            Capability.TEMPERATURE_CONTROL,
            Capability.LIGHT,
            Capability.SPEED,
            Capability.AMS,
            Capability.RAW_GCODE,
        )

    async def info(self) -> PrinterInfo:
        return PrinterInfo(
            printer_id=self.printer_id,
            driver=self.driver_name,
            model=self.config.model or "Mock A1 mini",
            host="127.0.0.1",
            serial="MOCK0000000001",
            firmware="01.06.00.00",
            build_volume_mm=(180, 180, 180),
            capabilities=self.capabilities,
        )

    # -- simulated progress -------------------------------------------------
    def _elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._paused_at if self._paused_at is not None else self.now()
        return max(0.0, end - self._started_at - self._paused_total)

    def _tick(self) -> None:
        if self._state is PrintState.RUNNING and self._elapsed() >= self.print_duration:
            self._state = PrintState.FINISHED

    async def status(self) -> PrinterStatus:
        self._tick()
        fraction = min(1.0, self._elapsed() / self.print_duration) if self._started_at else 0.0
        active = self._state.is_active
        return PrinterStatus(
            printer_id=self.printer_id,
            state=self._state if self.connected else PrintState.OFFLINE,
            online=self.connected,
            job_name=self._job,
            progress_percent=int(fraction * 100) if self._started_at else None,
            current_layer=int(fraction * self.total_layers) if self._started_at else None,
            total_layers=self.total_layers if self._started_at else None,
            remaining_minutes=max(0, int((self.print_duration - self._elapsed()) / 60)) if active else None,
            stage="printing" if self._state is PrintState.RUNNING else None,
            nozzle=Temperature(220.0 if active else 25.0, 220.0 if active else 0.0),
            bed=Temperature(60.0 if active else 25.0, 60.0 if active else 0.0),
            speed_level=self.speed_level,
            speed_percent=100,
            lights=dict(self.lights),
            filament=[
                FilamentSlot("AMS1-1", "PLA", "FF8800FF", 85, (190, 240), active=True, empty=False),
                FilamentSlot("external", None, None, None, None, empty=True),
            ],
            alerts=list(self.alerts),
            native_state=self._state.value.upper(),
            sd_card=True,
        )

    # -- files ---------------------------------------------------------------
    async def list_files(self, directory: str = "") -> list[FileEntry]:
        prefix = directory.strip("/")
        return [f for p, f in sorted(self.files.items()) if p.startswith(prefix)]

    async def upload_file(self, local_path: str, remote_name: str | None = None) -> FileEntry:
        if self.fail_next == "upload":
            self.fail_next = None
            raise FileTransferError("simulated upload failure")
        source = Path(local_path)
        name = remote_name or source.name
        size = source.stat().st_size if source.is_file() else 1024
        entry = FileEntry(name=name, path=f"cache/{name}", size_bytes=size)
        self.files[entry.path] = entry
        return entry

    async def delete_file(self, remote_path: str) -> None:
        self.files.pop(remote_path.strip("/"), None)

    # -- job control ----------------------------------------------------------
    async def start_print(self, remote_path: str, options: PrintOptions | None = None) -> dict:
        options = options or PrintOptions()
        if self.fail_next == "start_print":
            self.fail_next = None
            raise DeviceBusy("simulated printer busy")
        if not self._state.accepts_new_job:
            raise DeviceBusy(f"{self.printer_id} is {self._state.value}")
        if remote_path.strip("/") not in self.files:
            raise FileTransferError(f"{remote_path} is not on the printer")
        self._state = PrintState.RUNNING
        self._job = options.job_name or Path(remote_path).stem
        self._started_at = self.now()
        self._paused_at = None
        self._paused_total = 0.0
        self.commands.append(("start_print", {"file": remote_path, **options.to_dict()}))
        return {"acknowledged": True, "success": True, "file": remote_path, "plate": options.plate}

    async def pause_print(self) -> dict:
        if self._state is not PrintState.RUNNING:
            raise DeviceBusy(f"{self.printer_id} is {self._state.value}, not running")
        self._state = PrintState.PAUSED
        self._paused_at = self.now()
        self.commands.append(("pause", {}))
        return {"acknowledged": True, "success": True}

    async def resume_print(self) -> dict:
        if self._state is not PrintState.PAUSED:
            raise DeviceBusy(f"{self.printer_id} is {self._state.value}, not paused")
        if self._paused_at is not None:
            self._paused_total += self.now() - self._paused_at
            self._paused_at = None
        self._state = PrintState.RUNNING
        self.commands.append(("resume", {}))
        return {"acknowledged": True, "success": True}

    async def stop_print(self) -> dict:
        self._state = PrintState.IDLE
        self._started_at = None
        self._job = None
        self.commands.append(("stop", {}))
        return {"acknowledged": True, "success": True}

    async def set_temperature(self, nozzle: float | None = None, bed: float | None = None) -> dict:
        self.commands.append(("set_temperature", {"nozzle": nozzle, "bed": bed}))
        return {"acknowledged": True, "success": True}

    async def set_light(self, on: bool, node: str = "chamber_light") -> dict:
        self.lights[node] = "on" if on else "off"
        self.commands.append(("set_light", {"on": on, "node": node}))
        return {"acknowledged": True, "success": True}

    async def set_speed(self, level: int) -> dict:
        self.speed_level = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}[level]
        self.commands.append(("set_speed", {"level": level}))
        return {"acknowledged": True, "success": True}

    async def send_gcode(self, gcode: str) -> dict:
        self.commands.append(("send_gcode", {"gcode": gcode}))
        return {"acknowledged": True, "success": True}

    async def snapshot(self) -> bytes:
        await asyncio.sleep(0)
        return _TINY_JPEG

    async def stream_frames(self, count: int, interval: float = 1.0):
        for index in range(count):
            if index:
                await asyncio.sleep(interval)
            yield await self.snapshot()
