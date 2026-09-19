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
from .errors import ControlDisabled, NotSupported
from .models import Capability
from .monitor import MILESTONES, PrintMonitor
from .printer import Printer
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
        self.monitors: dict[str, PrintMonitor] = {}
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
        for monitor in list(self.monitors.values()):
            await monitor.stop()
        self.monitors.clear()
        await self.pool.close()
        self.store.close()

    # -- print monitoring --------------------------------------------------
    async def watch_print(
        self,
        device_id: str | None = None,
        *,
        run_id: int | None = None,
        poll_interval: float | None = None,
        milestones: tuple[str, ...] | None = None,
        camera: bool = True,
    ) -> PrintMonitor:
        """Start (or replace) the stage watcher for one printer.

        One watcher per printer: starting a second would photograph the same
        milestones twice and record both, so the existing one is stopped first.
        """
        device = await self.pool.get(device_id)
        if not isinstance(device, Printer):
            raise NotSupported(f"{device.device_id} is not a 3D printer, so it has no print stages")

        await self.stop_watching(device.device_id)
        use_camera = camera and device.supports(Capability.CAMERA_SNAPSHOT)
        monitor = PrintMonitor(
            store=self.store,
            device_id=device.device_id,
            status_fn=device.status,
            snapshot_fn=device.snapshot if use_camera else None,
            capture_path_fn=lambda label: self.capture_path(device.device_id, label),
            run_id=run_id,
            poll_interval=poll_interval or self.settings.monitor_poll_seconds,
            watch=tuple(milestones) if milestones else tuple(MILESTONES),
        )
        self.monitors[device.device_id] = monitor
        await monitor.start()
        return monitor

    async def stop_watching(self, device_id: str | None = None) -> PrintMonitor | None:
        """Stop the watcher for a printer, if one is running."""
        resolved = self.settings.get(device_id).device_id
        monitor = self.monitors.pop(resolved, None)
        if monitor is not None:
            await monitor.stop()
        return monitor

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
