"""The vacuum device class: what every robot-vacuum driver implements.

The verbs differ from a printer's, but everything above this layer - the MHS
channels, the descriptor, the safety limits, the MCP tools - is the same
machinery. That reuse is the point: it is the test of whether the device
abstraction was actually generic or merely printer-shaped.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from .device import Device
from .errors import NotSupported
from .models import Capability, CleanOptions, Position, Room, VacuumInfo, VacuumStatus
from .specs import VacuumSpec, vacuum_spec_for
from .standard.channels import Access, Channel, ChannelTable, SafetyLimit, SafetyViolation


@dataclass
class MapSnapshot:
    """A rendered map plus what is needed to point at things on it.

    ``calibration`` carries the correspondence between image pixels and the
    robot's millimetre frame, which is what makes "go to this spot on the
    picture" a thing an agent can actually ask for. See
    :mod:`mhs.drivers.roborock.mapping`.
    """

    image_png: bytes
    width: int
    height: int
    calibration: list[dict]
    rooms: list[Room]
    robot: Position | None = None
    charger: Position | None = None
    map_name: str | None = None

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "map_name": self.map_name,
            "rooms": [r.to_dict() for r in self.rooms],
            "robot": self.robot.to_dict() if self.robot else None,
            "charger": self.charger.to_dict() if self.charger else None,
            "calibration": self.calibration,
        }


class Vacuum(Device):
    """Async interface to one robot vacuum."""

    device_kind = "robot_vacuum"

    @property
    def spec(self) -> VacuumSpec:
        return vacuum_spec_for(self.model_name)

    @abc.abstractmethod
    async def info(self) -> VacuumInfo: ...

    @abc.abstractmethod
    async def status(self) -> VacuumStatus: ...

    # -- cleaning ----------------------------------------------------------
    @abc.abstractmethod
    async def start_clean(self, options: CleanOptions | None = None) -> dict:
        """Start a full clean of the current map."""

    @abc.abstractmethod
    async def pause(self) -> dict: ...

    @abc.abstractmethod
    async def resume(self) -> dict: ...

    @abc.abstractmethod
    async def stop(self) -> dict:
        """Stop cleaning and stay where it is."""

    @abc.abstractmethod
    async def return_to_dock(self) -> dict: ...

    async def clean_rooms(self, segment_ids: list[int], options: CleanOptions | None = None) -> dict:
        raise NotSupported(f"{self.driver_name} cannot clean individual rooms")

    async def clean_zone(
        self, zones_mm: list[tuple[float, float, float, float]], options: CleanOptions | None = None
    ) -> dict:
        raise NotSupported(f"{self.driver_name} cannot clean a rectangular zone")

    async def go_to(self, x_mm: float, y_mm: float) -> dict:
        raise NotSupported(f"{self.driver_name} cannot be sent to a point")

    async def locate(self) -> dict:
        raise NotSupported(f"{self.driver_name} cannot be asked to announce itself")

    # -- settings ----------------------------------------------------------
    async def set_fan_power(self, level: str) -> dict:
        raise NotSupported(f"{self.driver_name} cannot set suction")

    async def set_water_level(self, level: str) -> dict:
        raise NotSupported(f"{self.driver_name} cannot set water flow")

    @property
    def fan_power_options(self) -> tuple[str, ...]:
        """Suction presets this model accepts, read from the device where possible."""
        return ()

    @property
    def water_level_options(self) -> tuple[str, ...]:
        return ()

    # -- map ---------------------------------------------------------------
    async def list_rooms(self) -> list[Room]:
        raise NotSupported(f"{self.driver_name} cannot list rooms")

    async def map_snapshot(self) -> MapSnapshot:
        raise NotSupported(f"{self.driver_name} has no map")

    # -- MHS channels ------------------------------------------------------
    def _build_channels(self) -> ChannelTable:
        """Standard robot-vacuum channels, derived from declared capabilities."""
        table = ChannelTable()

        async def status_field(getter):
            return getter(await self.status())

        table.add(Channel(
            "vacuum.state", Access.READ,
            "Lifecycle state: idle, cleaning, paused, returning, docked, charging, "
            "error or offline.",
            value_type="string", tags=("status",),
            reader=lambda: status_field(lambda s: s.state.value),
        ))
        table.add(Channel(
            "vacuum.status", Access.READ,
            "Full normalised status: battery, suction, position, area cleaned, errors.",
            value_type="object", tags=("status",),
            reader=lambda: status_field(lambda s: s.to_dict()),
        ))
        table.add(Channel(
            "battery.level", Access.READ, "Charge remaining.",
            unit="percent", tags=("status", "power"),
            reader=lambda: status_field(lambda s: s.battery_percent),
        ))
        table.add(Channel(
            "vacuum.position", Access.READ,
            "Where the robot currently is, in its own millimetre map frame.",
            value_type="object", tags=("status", "navigation"),
            reader=lambda: status_field(lambda s: s.position.to_dict() if s.position else None),
        ))
        table.add(Channel(
            "vacuum.error", Access.READ, "Active fault, if any.",
            value_type="object", tags=("status", "safety"),
            reader=lambda: status_field(lambda s: s.error.to_dict() if s.error else None),
        ))
        table.add(Channel(
            "vacuum.command", Access.WRITE,
            "Change what the robot is doing: start, pause, resume, stop or dock.",
            value_type="string", tags=("motion",),
            limit=SafetyLimit(
                allowed=("start", "pause", "resume", "stop", "dock"),
                requires_confirmation=True,
                rationale="Each of these sends the robot somewhere or stops it mid-job.",
            ),
            writer=self._command,
        ))
        if Capability.GO_TO in self.capabilities:
            table.add(Channel(
                "vacuum.goto", Access.WRITE,
                'Send the robot to a point, as "x,y" in millimetres on the current map.',
                value_type="string", tags=("motion", "navigation"),
                limit=SafetyLimit(
                    requires_confirmation=True,
                    rationale="The robot will drive across the floor to get there.",
                ),
                writer=self._goto_channel,
            ))
        if Capability.ROOM_CLEAN in self.capabilities:
            table.add(Channel(
                "clean.rooms", Access.WRITE,
                "Clean the listed room segment ids, e.g. [16, 17].",
                value_type="object", tags=("motion", "cleaning"),
                limit=SafetyLimit(
                    requires_confirmation=True,
                    rationale="Starts a cleaning run, which is noisy and takes time.",
                ),
                writer=self._clean_rooms_channel,
            ))
        if self.fan_power_options:
            table.add(Channel(
                "fan.power", Access.READ_WRITE, "Suction preset.",
                value_type="string", tags=("cleaning",),
                limit=SafetyLimit(allowed=tuple(self.fan_power_options)),
                reader=lambda: status_field(lambda s: s.fan_power),
                writer=lambda value: self.set_fan_power(str(value)),
            ))
        if self.water_level_options:
            table.add(Channel(
                "water.level", Access.READ_WRITE, "Mop water flow.",
                value_type="string", tags=("cleaning",),
                limit=SafetyLimit(allowed=tuple(self.water_level_options)),
                reader=lambda: status_field(lambda s: s.water_level),
                writer=lambda value: self.set_water_level(str(value)),
            ))
        if Capability.MAP in self.capabilities:
            table.add(Channel(
                "map.image", Access.READ, "The current map as a PNG.",
                value_type="binary", tags=("navigation",),
                reader=lambda: self._map_bytes(),
            ))
        return table

    async def _map_bytes(self) -> bytes:
        return (await self.map_snapshot()).image_png

    async def _command(self, action: str) -> dict:
        action = str(action).lower()
        handlers = {
            "start": self.start_clean,
            "pause": self.pause,
            "resume": self.resume,
            "stop": self.stop,
            "dock": self.return_to_dock,
        }
        handler = handlers.get(action)
        if handler is None:
            raise SafetyViolation(f"vacuum.command: unknown action {action!r}")
        return await handler()

    async def _goto_channel(self, value: str) -> dict:
        try:
            x_text, y_text = str(value).replace(" ", "").split(",")
            x_mm, y_mm = float(x_text), float(y_text)
        except ValueError:
            raise SafetyViolation(
                f"vacuum.goto: expected \"x,y\" in millimetres, got {value!r}"
            ) from None
        return await self.go_to(x_mm, y_mm)

    async def _clean_rooms_channel(self, value) -> dict:
        if isinstance(value, str):
            value = [v for v in value.replace(",", " ").split() if v]
        try:
            segments = [int(v) for v in value]
        except (TypeError, ValueError):
            raise SafetyViolation(f"clean.rooms: expected a list of segment ids, got {value!r}") from None
        return await self.clean_rooms(segments)

    # -- descriptor --------------------------------------------------------
    def descriptor_profile(self) -> dict:
        spec = self.spec
        tags = [f"{spec.model} robot vacuum"]
        if spec.suction_pa:
            tags.append(f"rated {spec.suction_pa:,} Pa suction")
        if spec.navigation:
            tags.append(f"navigation: {spec.navigation}")
        if spec.mop_type:
            tags.append(f"mop: {spec.mop_type}")
        if spec.height_mm:
            tags.append(f"{spec.height_mm:.0f} mm tall, so it fits under low furniture")
        if spec.has_arm:
            tags.append("has a robotic arm for moving small obstacles")
        tags.extend(spec.notes)
        tags.append(
            "Drives itself around occupied rooms: it can bump furniture, tangle in cables, "
            "spread a spill it drives through, and it is loud."
        )

        cannot = [
            "Know where anything is by name unless the map has been built and the rooms "
            "named in the Roborock app.",
            "Navigate to a point outside the mapped area, or through a closed door.",
            "Clean a room it has not mapped: build the map first.",
            "See what it is cleaning. Positions come from its own map, not from vision.",
            "Pick things up off the floor (except a Saros Z70's arm, which this driver "
            "does not expose).",
        ]

        return {
            "tags": tags,
            "cannot": cannot,
            "physical": spec.to_dict(),
            "capability_heading": "What it can reach",
            "capability_summary": [
                "Any point on the current map, given as millimetre coordinates or picked "
                "off the rendered map image.",
                "Any named room the app has mapped, by segment id or name.",
                "A rectangular zone given in map millimetres.",
                "Its dock, on command or when the battery runs low.",
            ],
        }
