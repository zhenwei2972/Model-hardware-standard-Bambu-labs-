"""The device contract every driver implements.

Keeping this deliberately small is what makes the MCP surface portable: add a
Moonraker/OctoPrint/Prusa Connect driver and every tool, the scheduler and the
CLI work unchanged.
"""

from __future__ import annotations

import abc
import asyncio
import time
from typing import Any

from .errors import NotSupported, TimeoutExceeded
from .models import Capability, FileEntry, PrinterInfo, PrinterStatus, PrintOptions, PrintState
from .specs import PrinterSpec, spec_for
from .standard.channels import Access, Channel, ChannelTable, SafetyLimit, SafetyViolation


class Printer(abc.ABC):
    """Async interface to one physical printer."""

    #: Driver name used in config files and in `PrinterInfo.driver`.
    driver_name: str = "abstract"

    def __init__(self, printer_id: str, model_name: str = "") -> None:
        self.printer_id = printer_id
        self.model_name = model_name
        self._channels: ChannelTable | None = None

    @property
    def spec(self) -> PrinterSpec:
        """Hardware specification, used for channel limits and printability checks."""
        return spec_for(self.model_name)

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

    # -- MHS read/write primitives -----------------------------------------
    def channel_table(self) -> ChannelTable:
        """The device's channels, built once per instance."""
        if self._channels is None:
            self._channels = self._build_channels()
        return self._channels

    def _build_channels(self) -> ChannelTable:
        """Standard 3D-printer channels, derived from declared capabilities.

        Building these in the base class is what makes the standard layer
        portable: a new driver implements the ordinary methods and gets a
        conforming read/write surface, with limits taken from its spec, for free.
        """
        spec = self.spec
        table = ChannelTable()

        async def status_field(getter):
            return getter(await self.status())

        table.add(Channel(
            "printer.state", Access.READ,
            "Lifecycle state: idle, preparing, running, paused, finished, failed or offline.",
            value_type="string", tags=("status",),
            reader=lambda: status_field(lambda s: s.state.value),
        ))
        table.add(Channel(
            "printer.status", Access.READ,
            "Full normalised status object: job, progress, temperatures, filament, alerts.",
            value_type="object", tags=("status",),
            reader=lambda: status_field(lambda s: s.to_dict()),
        ))
        table.add(Channel(
            "printer.alerts", Access.READ,
            "Active fault codes reported by the machine, with severity and a link.",
            value_type="object", tags=("status", "safety"),
            reader=lambda: status_field(lambda s: [a.to_dict() for a in s.alerts]),
        ))
        table.add(Channel(
            "job.name", Access.READ, "Name of the job currently loaded or printing.",
            value_type="string", tags=("job",),
            reader=lambda: status_field(lambda s: s.job_name),
        ))
        table.add(Channel(
            "job.progress", Access.READ, "Completion of the running job.",
            unit="percent", tags=("job",),
            reader=lambda: status_field(lambda s: s.progress_percent),
        ))
        table.add(Channel(
            "job.layer", Access.READ, "Layer currently being printed.",
            unit="layer", tags=("job",),
            reader=lambda: status_field(lambda s: s.current_layer),
        ))
        table.add(Channel(
            "job.layers_total", Access.READ, "Total layers in the running job.",
            unit="layer", tags=("job",),
            reader=lambda: status_field(lambda s: s.total_layers),
        ))
        table.add(Channel(
            "job.remaining", Access.READ, "Printer's own estimate of time left.",
            unit="minute", tags=("job",),
            reader=lambda: status_field(lambda s: s.remaining_minutes),
        ))
        table.add(Channel(
            "nozzle.temperature", Access.READ_WRITE,
            "Hotend temperature. Writing sets the target.",
            unit="celsius", tags=("thermal",),
            limit=SafetyLimit(
                minimum=0, maximum=spec.max_nozzle_temp_c,
                rationale=f"The {spec.model} hotend is rated to {spec.max_nozzle_temp_c:.0f} C; "
                          "beyond that the heater block and PTFE degrade.",
            ),
            reader=lambda: status_field(lambda s: s.nozzle.current),
            writer=lambda value: self.set_temperature(nozzle=float(value)),
        ) if Capability.TEMPERATURE_CONTROL in self.capabilities else Channel(
            "nozzle.temperature", Access.READ, "Hotend temperature.", unit="celsius",
            tags=("thermal",), reader=lambda: status_field(lambda s: s.nozzle.current),
        ))
        table.add(Channel(
            "bed.temperature", Access.READ_WRITE,
            "Heated bed temperature. Writing sets the target.",
            unit="celsius", tags=("thermal",),
            limit=SafetyLimit(
                minimum=0, maximum=spec.max_bed_temp_c,
                rationale=f"The {spec.model} plate is rated to {spec.max_bed_temp_c:.0f} C.",
            ),
            reader=lambda: status_field(lambda s: s.bed.current),
            writer=lambda value: self.set_temperature(bed=float(value)),
        ) if Capability.TEMPERATURE_CONTROL in self.capabilities else Channel(
            "bed.temperature", Access.READ, "Heated bed temperature.", unit="celsius",
            tags=("thermal",), reader=lambda: status_field(lambda s: s.bed.current),
        ))
        table.add(Channel(
            "job.control", Access.WRITE,
            "Change the running job: pause, resume or stop. Stopping cannot be undone.",
            value_type="string", tags=("job", "motion"),
            limit=SafetyLimit(
                allowed=("pause", "resume", "stop"), requires_confirmation=True,
                rationale="Each of these changes what the machine is physically doing.",
            ),
            writer=self._job_control,
        ))
        table.add(Channel(
            "job.file", Access.WRITE,
            "Start a print from a sliced file already on the printer's storage.",
            value_type="string", tags=("job", "motion", "thermal"),
            limit=SafetyLimit(
                requires_confirmation=True,
                rationale="Starting a print heats the machine and runs it unattended; "
                          "the plate must be clear first.",
            ),
            writer=lambda value: self.start_print(str(value)),
        ))
        if Capability.SPEED in self.capabilities:
            table.add(Channel(
                "print.speed_level", Access.READ_WRITE,
                "Speed preset: 1 silent, 2 standard, 3 sport, 4 ludicrous.",
                tags=("motion",),
                limit=SafetyLimit(minimum=1, maximum=4,
                                  rationale="The firmware defines exactly four presets."),
                reader=lambda: status_field(lambda s: s.speed_level),
                writer=lambda value: self.set_speed(int(value)),
            ))
        if Capability.LIGHT in self.capabilities:
            table.add(Channel(
                "light.chamber", Access.READ_WRITE, "Chamber LED.",
                value_type="string", tags=("convenience",),
                limit=SafetyLimit(allowed=("on", "off")),
                reader=lambda: status_field(lambda s: s.lights.get("chamber_light")),
                writer=lambda value: self.set_light(str(value).lower() == "on"),
            ))
        if Capability.AMS in self.capabilities:
            table.add(Channel(
                "filament.slots", Access.READ,
                "Loaded filament per AMS tray and the external spool.",
                value_type="object", tags=("material",),
                reader=lambda: status_field(lambda s: [f.to_dict() for f in s.filament]),
            ))
        if Capability.CAMERA_SNAPSHOT in self.capabilities:
            table.add(Channel(
                "camera.frame", Access.READ,
                "One JPEG frame from the chamber camera.",
                value_type="binary", tags=("vision",), reader=self.snapshot,
            ))
        return table

    async def _job_control(self, action: str) -> dict:
        action = str(action).lower()
        if action == "pause":
            return await self.pause_print()
        if action == "resume":
            return await self.resume_print()
        if action == "stop":
            return await self.stop_print()
        raise SafetyViolation(f"job.control: unknown action {action!r}")

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
                hint="Repeat the call with confirm=true once the printer is clear.",
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
