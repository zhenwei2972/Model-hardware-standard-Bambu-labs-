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
from .device import Printer
from .drivers import build
from .errors import ControlDisabled
from .scheduler import PrintScheduler
from .store import Store

log = logging.getLogger(__name__)


class PrinterPool:
    """Lazily connects printers and keeps them connected."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._printers: dict[str, Printer] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def ids(self) -> list[str]:
        return sorted(self.settings.printers)

    async def get(self, printer_id: str | None = None) -> Printer:
        config = self.settings.get(printer_id)
        lock = self._locks.setdefault(config.printer_id, asyncio.Lock())
        async with lock:
            printer = self._printers.get(config.printer_id)
            if printer is None:
                printer = build(config)
                self._printers[config.printer_id] = printer
            await printer.connect()
            return printer

    async def close(self) -> None:
        for printer in list(self._printers.values()):
            try:
                await printer.disconnect()
            except Exception:  # pragma: no cover - shutdown is best effort
                log.debug("disconnect failed for %s", printer.printer_id, exc_info=True)
        self._printers.clear()


class MHSApp:
    """One process' worth of state."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.pool = PrinterPool(self.settings)
        self.store = Store(self.settings.db_path)
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
    async def printer(self, printer_id: str | None = None) -> Printer:
        return await self.pool.get(printer_id)

    def require_writable(self, action: str) -> None:
        if self.settings.read_only:
            raise ControlDisabled(f"refusing to {action}: this MHS server runs read-only")

    def capture_path(self, printer_id: str, label: str = "frame") -> Path:
        directory = self.settings.capture_dir / printer_id
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return directory / f"{stamp}-{label}.jpg"
