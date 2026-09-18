"""The printer device class: what every 3D-printer driver implements."""

from __future__ import annotations

import abc

from .device import Device
from .errors import NotSupported
from .models import Capability, FileEntry, PrinterInfo, PrinterStatus, PrintOptions
from .specs import PrinterSpec, limits_for, spec_for
from .standard.channels import Access, Channel, ChannelTable, SafetyLimit, SafetyViolation


class Printer(Device):
    """Async interface to one 3D printer."""

    device_kind = "fdm_3d_printer"

    @property
    def spec(self) -> PrinterSpec:
        """Hardware specification, used for channel limits and printability checks."""
        return spec_for(self.model_name)

    @abc.abstractmethod
    async def info(self) -> PrinterInfo: ...

    @abc.abstractmethod
    async def status(self) -> PrinterStatus: ...

    def descriptor_profile(self) -> dict:
        """What a 3D printer is, what it can resolve, and what it will not do."""
        spec = self.spec
        limits = limits_for(spec)
        volume = spec.build_volume_mm

        tags = [
            f"{spec.model} desktop FDM 3D printer",
            f"build volume {volume[0]:.0f} x {volume[1]:.0f} x {volume[2]:.0f} mm",
            f"default nozzle {spec.default_nozzle_mm:g} mm",
            "enclosed" if spec.enclosed else "open frame, unheated chamber",
            f"hotend up to {spec.max_nozzle_temp_c:.0f} C, bed up to {spec.max_bed_temp_c:.0f} C",
            "controlled over the local network; no cloud account involved",
        ]
        if spec.ams_slots:
            tags.append(f"{spec.ams_slots}-slot automatic material system")
        if spec.camera_resolution:
            tags.append(
                f"chamber camera {spec.camera_resolution[0]}x{spec.camera_resolution[1]}, "
                "roughly 1 fps"
            )
        tags.extend(spec.notes)
        tags.append(
            "Moves fast and reaches 220 C or more: it can burn, pinch, and start a fire if "
            "left unattended with a fault."
        )

        cannot = [
            "Slice models. It accepts sliced .3mf/.gcode only; slicing happens in Bambu Studio "
            "or OrcaSlicer.",
            "Report absolute dimensions from the camera. Measurement needs a known reference "
            "object in the frame.",
            "Resolve features below the extrusion width in XY or the layer height in Z.",
            "Recover a failed print. A stopped job cannot be resumed.",
        ]
        if not spec.enclosed:
            cannot.append("Hold chamber temperature, so ABS/ASA/PC warp.")
        if self.driver_name == "bambu":
            cannot.append(
                "Accept control commands while the printer is in cloud mode: LAN Only Mode plus "
                "Developer Mode must be enabled on the printer's screen."
            )

        return {
            "tags": tags,
            "cannot": cannot,
            "physical": spec.to_dict(),
            "resolution": limits.to_dict(),
            "capability_heading": "What it can print",
            "capability_summary": [
                f"Build volume: {volume[0]:.0f} x {volume[1]:.0f} x {volume[2]:.0f} mm",
                f"Smallest XY feature: {limits.min_xy_feature_mm:g} mm "
                f"(one {limits.nozzle_mm:g} mm extrusion)",
                f"Smallest usable wall: {limits.min_wall_mm:g} mm (two perimeters)",
                f"Smallest vertical step: {limits.min_vertical_feature_mm:g} mm (one layer)",
                f"Smallest reliable hole: {limits.min_hole_diameter_mm:g} mm; "
                f"thinnest pin: {limits.min_pin_diameter_mm:g} mm",
                f"Unsupported overhang limit: {limits.max_overhang_degrees:g} degrees",
            ],
        }

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

