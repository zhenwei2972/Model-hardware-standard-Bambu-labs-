"""The device contract every driver implements.

Keeping this deliberately small is what makes the MCP surface portable: add a
Moonraker/OctoPrint/Prusa Connect driver and every tool, the scheduler and the
CLI work unchanged.
"""

from __future__ import annotations

import abc
import asyncio
import time

from .errors import NotSupported, TimeoutExceeded
from .models import Capability, FileEntry, PrinterInfo, PrinterStatus, PrintOptions, PrintState


class Printer(abc.ABC):
    """Async interface to one physical printer."""

    #: Driver name used in config files and in `PrinterInfo.driver`.
    driver_name: str = "abstract"

    def __init__(self, printer_id: str) -> None:
        self.printer_id = printer_id

    # -- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    async def connect(self) -> None:
        """Open transports and begin receiving telemetry. Idempotent."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close transports. Idempotent."""

    # -- introspection -----------------------------------------------------
    @abc.abstractmethod
    async def info(self) -> PrinterInfo: ...

    @abc.abstractmethod
    async def status(self) -> PrinterStatus: ...

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    @abc.abstractmethod
    def capabilities(self) -> tuple[Capability, ...]: ...

    def require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise NotSupported(
                f"{self.printer_id} ({self.driver_name}) does not support {capability.value}"
            )

    # -- files -------------------------------------------------------------
    async def list_files(self, directory: str = "") -> list[FileEntry]:
        raise NotSupported(f"{self.driver_name} cannot list files")

    async def upload_file(self, local_path: str, remote_name: str | None = None) -> FileEntry:
        raise NotSupported(f"{self.driver_name} cannot upload files")

    async def delete_file(self, remote_path: str) -> None:
        raise NotSupported(f"{self.driver_name} cannot delete files")

    # -- job control -------------------------------------------------------
    @abc.abstractmethod
    async def start_print(self, remote_path: str, options: PrintOptions | None = None) -> dict: ...

    @abc.abstractmethod
    async def pause_print(self) -> dict: ...

    @abc.abstractmethod
    async def resume_print(self) -> dict: ...

    @abc.abstractmethod
    async def stop_print(self) -> dict: ...

    # -- optional controls -------------------------------------------------
    async def set_temperature(self, nozzle: float | None = None, bed: float | None = None) -> dict:
        raise NotSupported(f"{self.driver_name} cannot set temperatures")

    async def set_light(self, on: bool, node: str = "chamber_light") -> dict:
        raise NotSupported(f"{self.driver_name} cannot control lights")

    async def set_speed(self, level: int) -> dict:
        raise NotSupported(f"{self.driver_name} cannot set speed")

    async def send_gcode(self, gcode: str) -> dict:
        raise NotSupported(f"{self.driver_name} cannot send raw gcode")

    async def snapshot(self) -> bytes:
        """Return a single JPEG frame from the device camera."""
        raise NotSupported(f"{self.driver_name} has no camera")

    # -- helpers shared by all drivers -------------------------------------
    async def wait_for_state(
        self,
        states: set[PrintState],
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> PrinterStatus:
        """Poll until the printer reports one of ``states``.

        Polling (rather than pushing) keeps the contract trivial for drivers whose
        transport has no event stream.
        """
        deadline = time.monotonic() + timeout
        last = await self.status()
        while last.state not in states:
            if time.monotonic() >= deadline:
                raise TimeoutExceeded(
                    f"{self.printer_id} stayed in {last.state.value} for {timeout:.0f}s "
                    f"(waiting for {sorted(s.value for s in states)})"
                )
            await asyncio.sleep(poll_interval)
            last = await self.status()
        return last

    async def __aenter__(self) -> Printer:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()
