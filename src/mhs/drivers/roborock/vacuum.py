"""The Roborock driver: Saros 10 / 10R / Z70 and the wider V1 vacuum family."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from pathlib import Path
from typing import Any

from PIL import Image

from ...config import DeviceConfig
from ...design.mapviz import MapTransform, annotate_map
from ...errors import (
    CommandRejected,
    ConfigError,
    ConnectionFailed,
    DeviceBusy,
    MHSError,
    NotSupported,
)
from ...models import Capability, CleanOptions, Position, Room, VacuumInfo, VacuumState
from ...vacuum import MapSnapshot, Vacuum
from . import SAROS_MODELS, commands
from .client import RoborockAccount, load_credentials
from .state import to_status

log = logging.getLogger(__name__)

_CAPABILITIES = (
    Capability.CLEAN,
    Capability.PAUSE_RESUME,
    Capability.STOP,
    Capability.ROOM_CLEAN,
    Capability.ZONE_CLEAN,
    Capability.GO_TO,
    Capability.RETURN_TO_DOCK,
    Capability.MAP,
    Capability.LOCATE,
    Capability.SPEED,
    Capability.WATER_FLOW,
)


class RoborockVacuum(Vacuum):
    """A Roborock robot vacuum, reached through the Roborock cloud."""

    driver_name = "roborock"

    def __init__(self, config: DeviceConfig, state_dir=None) -> None:
        super().__init__(config.device_id, config.model)
        self.config = config
        self._state_dir = Path(state_dir) if state_dir else Path.home() / ".local/share/mhs"
        self.account = RoborockAccount.from_config(config, self._state_dir)
        self._manager: Any = None
        self._device: Any = None
        self._connect_lock = asyncio.Lock()
        #: Set when a map is fetched, so coordinates can be checked against the
        #: area that actually exists rather than the protocol's wide range.
        self._transform: MapTransform | None = None
        self._map_size: tuple[int, int] = (0, 0)

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        async with self._connect_lock:
            if self._device is not None:
                return
            payload = load_credentials(self.account.credentials_file)
            try:
                from roborock.data.containers import UserData
                from roborock.devices.device_manager import UserParams, create_device_manager

                params = UserParams(
                    username=payload.get("email", self.account.email),
                    user_data=UserData.from_dict(payload["user_data"]),
                    base_url=payload.get("base_url") or self.account.base_url,
                )
                self._manager = await create_device_manager(params)
                devices = await self._manager.get_devices()
            except ImportError as exc:
                raise ConfigError(
                    f"the roborock driver needs python-roborock: {exc}",
                    hint='Install it with: pip install "mhs-printer[roborock]"',
                ) from exc
            except MHSError:
                raise
            except Exception as exc:
                raise ConnectionFailed(
                    f"could not reach the Roborock cloud: {exc}",
                    hint="Check internet access, and re-run `mhs roborock-login` if the "
                         "token has expired.",
                ) from exc

            self._device = self._select(devices)
            try:
                await self._device.connect()
            except Exception as exc:
                raise ConnectionFailed(f"could not connect to {self._device.name}: {exc}") from exc
            if not self.model_name:
                self.model_name = self._model_name()
            # Suction and water presets are read off the device, so the channel
            # table built before connecting was missing them.
            self._channels = None
            log.info("connected to Roborock %s (%s)", self._device.name, self._model_name())

    def _select(self, devices: list) -> Any:
        """Pick the configured robot, and say what the alternatives were if not found."""
        if not devices:
            raise ConnectionFailed(
                "the Roborock account has no devices",
                hint="Add the robot in the Roborock app first.",
            )
        wanted_uid = self.account.device_uid
        wanted_name = (self.account.device_name or "").strip().lower()
        for device in devices:
            if wanted_uid and device.duid == wanted_uid:
                return device
            if wanted_name and device.name.strip().lower() == wanted_name:
                return device
        if wanted_uid or wanted_name:
            available = ", ".join(f"{d.name!r} ({d.duid})" for d in devices)
            raise ConnectionFailed(
                f"no Roborock device matching {wanted_uid or wanted_name!r}",
                hint=f"The account has: {available}",
            )
        if len(devices) > 1:
            available = ", ".join(f"{d.name!r}" for d in devices)
            raise ConnectionFailed(
                f"the account has several devices ({available}); name one",
                hint='Set device_name = "..." (or device_uid) under the device in config.toml.',
            )
        return devices[0]

    async def disconnect(self) -> None:
        device, manager, self._device, self._manager = self._device, self._manager, None, None
        for closeable in (device, manager):
            if closeable is None:
                continue
            try:
                result = closeable.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # pragma: no cover - shutdown is best effort
                log.debug("close failed", exc_info=True)

    async def _ensure(self) -> Any:
        if self._device is None:
            await self.connect()
        return self._device

    @property
    def _traits(self) -> Any:
        traits = getattr(self._device, "v1_properties", None)
        if traits is None:
            raise NotSupported(
                f"{self.device_id} is not a V1-protocol vacuum; this driver supports the "
                "Saros/S-series/Q-series robots."
            )
        return traits

    def _model_name(self) -> str:
        product = getattr(self._device, "product", None)
        model = getattr(product, "model", None) or ""
        return SAROS_MODELS.get(model, getattr(product, "name", None) or model or "Roborock vacuum")

    # -- introspection -----------------------------------------------------
    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return _CAPABILITIES

    async def info(self) -> VacuumInfo:
        device = await self._ensure()
        rooms: list[Room] = []
        # A robot that has not finished mapping still has an identity worth reporting.
        with contextlib.suppress(MHSError):
            rooms = await self.list_rooms()
        return VacuumInfo(
            device_id=self.device_id,
            driver=self.driver_name,
            model=self._model_name(),
            serial=getattr(device, "duid", None),
            firmware=getattr(getattr(device, "device_info", None), "fv", None),
            capabilities=self.capabilities,
            room_count=len(rooms) or None,
            has_mop=True,
        )

    async def status(self):
        device = await self._ensure()
        trait = self._traits.status
        try:
            await trait.refresh()
        except Exception as exc:
            raise ConnectionFailed(f"could not read status from {self.device_id}: {exc}") from exc
        position = None
        map_name = None
        try:
            snapshot = await self._map_data()
            position, map_name = snapshot[0], snapshot[1]
        except Exception:
            log.debug("position unavailable", exc_info=True)
        return to_status(
            self.device_id,
            trait,
            online=bool(getattr(device, "is_connected", True)),
            position=position,
            map_name=map_name,
        )

    # -- cleaning ----------------------------------------------------------
    async def _send(self, command: str, params: list | dict | None = None) -> dict:
        await self._ensure()
        try:
            result = await self._traits.command.send(command, params)
        except Exception as exc:
            raise CommandRejected(f"{command} was rejected by the robot: {exc}") from exc
        return {"command": command, "params": params, "result": result, "acknowledged": True}

    async def _require_ready(self, action: str) -> None:
        status = await self.status()
        if not status.online:
            raise ConnectionFailed(f"{self.device_id} is offline")
        if status.error is not None:
            raise DeviceBusy(
                f"{self.device_id} reports {status.error.code}"
                f"{f' ({status.error.message})' if status.error.message else ''}; "
                f"clear it before {action}",
            )
        if status.battery_percent is not None and status.battery_percent < 15 and not status.charging:
            raise DeviceBusy(
                f"{self.device_id} is at {status.battery_percent}% and not charging; "
                f"it would dock partway through {action}",
                hint="Send it to the dock and try again when it has charged.",
            )

    async def start_clean(self, options: CleanOptions | None = None) -> dict:
        await self._require_ready("cleaning")
        await self._apply_options(options)
        return await self._send(*commands.simple(commands.START))

    async def pause(self) -> dict:
        return await self._send(*commands.simple(commands.PAUSE))

    async def resume(self) -> dict:
        status = await self.status()
        if status.state is not VacuumState.PAUSED:
            raise DeviceBusy(f"{self.device_id} is {status.state.value}, not paused")
        return await self._send(*commands.simple(commands.RESUME))

    async def stop(self) -> dict:
        return await self._send(*commands.simple(commands.STOP))

    async def return_to_dock(self) -> dict:
        return await self._send(*commands.simple(commands.DOCK))

    async def locate(self) -> dict:
        return await self._send(*commands.simple(commands.LOCATE))

    async def clean_rooms(self, segment_ids: list[int], options: CleanOptions | None = None) -> dict:
        await self._require_ready("cleaning those rooms")
        known = {room.segment_id for room in await self.list_rooms()}
        unknown = [s for s in segment_ids if known and s not in known]
        if unknown:
            raise CommandRejected(
                f"no mapped room with segment id {', '.join(str(u) for u in unknown)}",
                hint="Call list_rooms; ids come from the map, not from the room's position "
                     "in the list.",
            )
        await self._apply_options(options)
        repeat = options.repeat if options else 1
        return await self._send(*commands.segment_clean(segment_ids, repeat))

    async def clean_zone(self, zones_mm, options: CleanOptions | None = None) -> dict:
        await self._require_ready("cleaning that zone")
        for zone in zones_mm:
            await self._check_on_map(zone[0], zone[1])
            await self._check_on_map(zone[2], zone[3])
        await self._apply_options(options)
        repeat = options.repeat if options else 1
        return await self._send(*commands.zone_clean(list(zones_mm), repeat))

    async def go_to(self, x_mm: float, y_mm: float) -> dict:
        await self._require_ready("driving there")
        await self._check_on_map(x_mm, y_mm)
        return await self._send(*commands.goto(x_mm, y_mm))

    async def _apply_options(self, options: CleanOptions | None) -> None:
        if options is None:
            return
        if options.fan_power:
            await self.set_fan_power(options.fan_power)
        if options.water_level:
            await self.set_water_level(options.water_level)

    # -- settings ----------------------------------------------------------
    def _mapping(self, attribute: str) -> dict[int, str]:
        try:
            return dict(getattr(self._traits.status, attribute) or {})
        except Exception:  # pragma: no cover - trait not loaded yet
            return {}

    @property
    def fan_power_options(self) -> tuple[str, ...]:
        if self._device is None:
            return ()
        return tuple(self._mapping("fan_speed_mapping").values())

    @property
    def water_level_options(self) -> tuple[str, ...]:
        if self._device is None:
            return ()
        return tuple(self._mapping("water_box_mode_mapping").values())

    async def _code_for(self, attribute: str, level: str, what: str) -> int:
        await self._ensure()
        mapping = self._mapping(attribute)
        if not mapping:
            raise NotSupported(f"{self.device_id} does not report its {what} presets")
        wanted = str(level).strip().lower()
        for code, name in mapping.items():
            if str(name).strip().lower() == wanted:
                return int(code)
        raise CommandRejected(
            f"{level!r} is not one of this robot's {what} presets",
            hint="Available: " + ", ".join(sorted(str(v) for v in mapping.values())),
        )

    async def set_fan_power(self, level: str) -> dict:
        code = await self._code_for("fan_speed_mapping", level, "suction")
        return await self._send(*commands.set_fan_power(code))

    async def set_water_level(self, level: str) -> dict:
        code = await self._code_for("water_box_mode_mapping", level, "water flow")
        return await self._send(*commands.set_water_level(code))

    # -- map ---------------------------------------------------------------
    async def list_rooms(self) -> list[Room]:
        await self._ensure()
        trait = getattr(self._traits, "rooms", None)
        if trait is None:
            raise NotSupported(f"{self.device_id} does not expose a room list")
        try:
            await trait.refresh()
            named = trait.with_room_names() if callable(getattr(trait, "with_room_names", None)) else None
        except Exception as exc:
            raise ConnectionFailed(f"could not read the room list: {exc}") from exc

        rooms: list[Room] = []
        for entry in named or getattr(trait, "rooms", None) or []:
            segment = getattr(entry, "segment_id", None)
            if segment is None:
                segment = getattr(entry, "id", None)
            name = getattr(entry, "name", None)
            if segment is None:
                continue
            rooms.append(Room(segment_id=int(segment), name=str(name) if name else None))
        return rooms

    async def _map_data(self) -> tuple[Position | None, str | None, Any]:
        """Return (robot position, map name, parsed map) from the map trait."""
        await self._ensure()
        trait = getattr(self._traits, "map_content", None)
        if trait is None:
            raise NotSupported(f"{self.device_id} does not expose map content")
        if callable(getattr(trait, "refresh", None)):
            await trait.refresh()
        data = getattr(trait, "map_data", None)
        position = None
        robot = getattr(data, "vacuum_position", None) if data else None
        if robot is not None:
            position = Position(float(robot.x), float(robot.y), getattr(robot, "a", None))
        return position, getattr(data, "map_name", None), data

    async def map_snapshot(self) -> MapSnapshot:
        _position, map_name, data = await self._map_data()
        trait = self._traits.map_content
        image_png = getattr(trait, "image_content", None)
        if not image_png:
            raise NotSupported(
                f"{self.device_id} returned no map image",
                hint="Let the robot finish a mapping run first.",
            )
        calibration = data.calibration() if data is not None else None
        if not calibration:
            raise NotSupported("the map has no calibration points, so it cannot be aimed at")
        transform = MapTransform.from_calibration(calibration)
        self._transform = transform

        rooms = await self.list_rooms()
        by_id = {r.segment_id: r for r in rooms}
        for number, parsed in (getattr(data, "rooms", None) or {}).items():
            room = by_id.get(int(number))
            if room is not None and getattr(parsed, "point", None) is not None:
                room.center = Position(float(parsed.point.x), float(parsed.point.y))

        robot_point = getattr(data, "vacuum_position", None)
        charger_point = getattr(data, "charger", None)
        robot = Position(float(robot_point.x), float(robot_point.y)) if robot_point else None
        charger = Position(float(charger_point.x), float(charger_point.y)) if charger_point else None

        annotated = annotate_map(image_png, transform, rooms=rooms, robot=robot, charger=charger)
        with Image.open(io.BytesIO(annotated)) as rendered:
            width, height = rendered.size
        self._map_size = (width, height)
        return MapSnapshot(
            image_png=annotated,
            width=width,
            height=height,
            calibration=calibration,
            rooms=rooms,
            robot=robot,
            charger=charger,
            map_name=map_name,
        )

    async def _check_on_map(self, x_mm: float, y_mm: float) -> None:
        """Reject a point that is not on the known map.

        The protocol range is wide enough to accept nonsense, and the robot's
        response to an off-map target is to do nothing - which reads as a
        silent failure. Checking against the rendered map's own extent turns
        that into an error with the actual bounds in it.
        """
        transform = self._transform
        if transform is None or self._map_size == (0, 0):
            return  # no map fetched yet; the protocol-level check still applies
        width, height = self._map_size
        px, py = transform.to_pixels(x_mm, y_mm)
        if not (0 <= px <= width and 0 <= py <= height):
            corners = [transform.to_mm(0, 0), transform.to_mm(width, height)]
            xs = sorted(c[0] for c in corners)
            ys = sorted(c[1] for c in corners)
            raise CommandRejected(
                f"({x_mm:.0f}, {y_mm:.0f}) mm is outside the mapped area",
                hint=f"The map covers x {xs[0]:.0f}-{xs[1]:.0f} mm, y {ys[0]:.0f}-{ys[1]:.0f} mm.",
            )

    _last_map_size: tuple[int, int] = (0, 0)
