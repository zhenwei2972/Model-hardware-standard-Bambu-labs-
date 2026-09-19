"""Application wiring shared by the MCP server and the CLI.

Holds the connection pool, the SQLite store and the scheduler, so both front-ends
behave identically and a printer is connected at most once per process.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .config import Settings, load_settings
from .device import Device
from .drivers import build
from .errors import ControlDisabled
from .scheduler import PrintScheduler
from .store import Store

log = logging.getLogger(__name__)


class DevicePool:
    """Lazily connects printers and keeps them connected."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._devices: dict[str, Device] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def ids(self) -> list[str]:
        return sorted(self.settings.devices)

    async def get(self, device_id: str | None = None) -> Device:
        config = self.settings.get(device_id)
        lock = self._locks.setdefault(config.device_id, asyncio.Lock())
        async with lock:
            device = self._devices.get(config.device_id)
            if device is None:
                device = build(config, state_dir=self.settings.state_dir)
                self._devices[config.device_id] = device
            await device.connect()
            return device

    async def close(self) -> None:
        for device in list(self._devices.values()):
            try:
                await device.disconnect()
            except Exception:  # pragma: no cover - shutdown is best effort
                log.debug("disconnect failed for %s", device.device_id, exc_info=True)
        self._devices.clear()


class MHSApp:
    """One process' worth of state."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.pool = DevicePool(self.settings)
        self.store = Store(self.settings.db_path)
        self._slicer = None
        self.scheduler = PrintScheduler(
            self.store,
            self.pool.get,
            read_only=self.settings.read_only,
        )

    # -- lifecycle ---------------------------------------------------------
    async def startup(self, run_scheduler: bool = True) -> None:
        self.settings.capture_dir.mkdir(parents=True, exist_ok=True)
        if run_scheduler:
            await self.scheduler.start()

    async def shutdown(self) -> None:
        await self.scheduler.stop()
        await self.pool.close()
        self.store.close()

    # -- helpers -----------------------------------------------------------
    async def device(self, device_id: str | None = None) -> Device:
        return await self.pool.get(device_id)

    async def printer(self, device_id: str | None = None) -> Device:
        """Alias kept for the printer-side call sites."""
        return await self.pool.get(device_id)

    def require_writable(self, action: str) -> None:
        if self.settings.read_only:
            raise ControlDisabled(f"refusing to {action}: this MHS server runs read-only")

    def slicer(self):
        """The configured slicer, resolved on first use.

        Built lazily so that a setup with no slicer installed still starts and
        serves every other tool - slicing is the only thing that needs it.
        """
        from .slicing import Slicer

        if self._slicer is None:
            self._slicer = Slicer.discover(
                self.settings.slicer_binary,
                profiles=tuple(Path(p).expanduser() for p in self.settings.slicer_profiles),
            )
        return self._slicer

    def slice_path(self, stem: str, suffix: str) -> Path:
        directory = self.settings.state_dir / "sliced"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{stem}{suffix}"

    def capture_path(self, device_id: str, label: str = "frame") -> Path:
        directory = self.settings.capture_dir / device_id
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return directory / f"{stamp}-{label}.jpg"
