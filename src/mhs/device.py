"""The device contract every driver implements.

Deliberately small, and deliberately not printer-shaped: :class:`Device` knows
about channels, limits and the MHS primitives, and nothing about nozzles or
suction. A device class (:class:`~mhs.printer.Printer`,
:class:`~mhs.vacuum.Vacuum`) adds the verbs for its kind of hardware; a driver
implements one of those. Everything above - the MCP tools, the scheduler, the
CLI - talks to this.
"""

from __future__ import annotations

import abc
import asyncio
import time
from typing import Any

from .errors import NotSupported, TimeoutExceeded
from .models import Capability, DeviceInfo
from .standard.channels import ChannelTable, SafetyViolation


class Device(abc.ABC):
    """Async interface to one physical device."""

    #: Driver name used in config files and in `DeviceInfo.driver`.
    driver_name: str = "abstract"

    #: Broad hardware class, reported in the MHS descriptor.
    device_kind: str = "device"

    def __init__(self, device_id: str, model_name: str = "") -> None:
        self.device_id = device_id
        self.model_name = model_name
        self._channels: ChannelTable | None = None

    # -- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    async def connect(self) -> None:
        """Open transports and begin receiving telemetry. Idempotent."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close transports. Idempotent."""

    # -- introspection -----------------------------------------------------
    @abc.abstractmethod
    async def info(self) -> DeviceInfo: ...

    @abc.abstractmethod
    async def status(self) -> Any:
        """Current state. Drivers return their device class's status object."""

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    @abc.abstractmethod
    def capabilities(self) -> tuple[Capability, ...]: ...

    def require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise NotSupported(
                f"{self.device_id} ({self.driver_name}) does not support {capability.value}"
            )

    # -- MHS read/write primitives -----------------------------------------
    def channel_table(self) -> ChannelTable:
        """The device's channels, built once per instance."""
        if self._channels is None:
            self._channels = self._build_channels()
        return self._channels

    def _build_channels(self) -> ChannelTable:
        """Channels this device exposes. Device classes override this."""
        return ChannelTable()

    def descriptor_profile(self) -> dict:
        """The parts of the MHS descriptor only this kind of hardware knows.

        Device classes return ``tags`` (natural language: what this is),
        ``cannot`` (what it will not do), and any extra sections worth
        publishing - a printer's build volume and resolution, a vacuum's map
        and coverage. Keeping it here means the descriptor generator never has
        to special-case a device class.
        """
        return {"tags": [], "cannot": []}

    async def read(self, channel: str) -> Any:
        """Read one channel. The MHS read primitive."""
        entry = self.channel_table().get(channel)
        if not entry.access.readable:
            raise SafetyViolation(f"{channel} is write-only")
        if entry.unavailable_reason:
            raise NotSupported(f"{channel} is unavailable: {entry.unavailable_reason}")
        assert entry.reader is not None
        return await entry.reader()

    async def write(self, channel: str, value: Any, confirm: bool = False) -> Any:
        """Write one channel, after checking its declared limits.

        The check happens here, in the driver, before anything reaches the
        hardware - not in a prompt, and not in the caller.
        """
        entry = self.channel_table().get(channel)
        if not entry.access.writable:
            raise SafetyViolation(f"{channel} is read-only")
        if entry.unavailable_reason:
            raise NotSupported(f"{channel} is unavailable: {entry.unavailable_reason}")
        entry.validate(value)
        if entry.limit is not None and entry.limit.requires_confirmation and not confirm:
            raise SafetyViolation(
                f"{channel} requires confirmation: writing {value!r} changes what the "
                "machine is physically doing.",
                hint="Repeat the call with confirm=true once it is safe to proceed.",
            )
        assert entry.writer is not None
        return await entry.writer(value)

    async def describe(self) -> dict:
        """The MHS device descriptor: what this device is, exposes and refuses."""
        from .standard.descriptor import build_descriptor

        return build_descriptor(self, await self.info())

    # -- helpers shared by all drivers -------------------------------------
    async def wait_for_state(
        self,
        states: set[Any],
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> Any:
        """Poll until the device reports one of ``states``.

        Polling (rather than pushing) keeps the contract trivial for drivers whose
        transport has no event stream.
        """
        deadline = time.monotonic() + timeout
        last = await self.status()
        while last.state not in states:
            if time.monotonic() >= deadline:
                raise TimeoutExceeded(
                    f"{self.device_id} stayed in {last.state.value} for {timeout:.0f}s "
                    f"(waiting for {sorted(s.value for s in states)})"
                )
            await asyncio.sleep(poll_interval)
            last = await self.status()
        return last

    async def __aenter__(self) -> Device:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()
