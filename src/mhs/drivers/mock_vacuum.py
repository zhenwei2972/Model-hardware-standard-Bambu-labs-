"""An in-process fake robot vacuum.

Same role as :mod:`mhs.drivers.mock`: the test suite runs without hardware,
`MHS_MOCK_VACUUM=1` lets someone try every tool before pointing it at a real
robot, and it documents the minimum a vacuum driver must implement.

The map is generated rather than recorded, so the floor plan, the room
segments and the calibration points are all known exactly - which is what makes
the coordinate round-trip testable.
"""

from __future__ import annotations

import asyncio
import io
import time

from PIL import Image, ImageDraw

from ..config import DeviceConfig
from ..design.mapviz import MapTransform, annotate_map
from ..errors import CommandRejected, DeviceBusy
from ..models import (
    Capability,
    CleanOptions,
    DeviceAlert,
    Position,
    Room,
    VacuumInfo,
    VacuumState,
    VacuumStatus,
)
from ..vacuum import MapSnapshot, Vacuum

#: The simulated flat, in the robot's millimetre frame. Roborock maps are
#: centred near 25500 mm, so the fake uses the same convention.
ORIGIN_MM = 25500.0
MM_PER_PIXEL = 50.0
MAP_WIDTH_PX = 120
MAP_HEIGHT_PX = 90

#: segment id -> (name, x0, y0, x1, y1) in millimetres.
FLOOR_PLAN: dict[int, tuple[str, float, float, float, float]] = {
    16: ("Kitchen", ORIGIN_MM - 2500, ORIGIN_MM - 1800, ORIGIN_MM - 200, ORIGIN_MM + 400),
    17: ("Living room", ORIGIN_MM - 100, ORIGIN_MM - 1800, ORIGIN_MM + 2600, ORIGIN_MM + 900),
    18: ("Hallway", ORIGIN_MM - 2500, ORIGIN_MM + 500, ORIGIN_MM - 200, ORIGIN_MM + 1700),
    19: ("Bedroom", ORIGIN_MM - 100, ORIGIN_MM + 1000, ORIGIN_MM + 2600, ORIGIN_MM + 1900),
}

FAN_POWERS = ("quiet", "balanced", "turbo", "max")
WATER_LEVELS = ("off", "low", "medium", "high")


class MockVacuum(Vacuum):
    """Simulates a Saros closely enough to exercise every tool."""

    driver_name = "mock_vacuum"

    #: Simulated wall-clock length of a whole-flat clean, in seconds.
    clean_duration = 60.0

    def __init__(self, config: DeviceConfig) -> None:
        super().__init__(config.device_id, config.model or "Saros 10")
        self.config = config
        self.connected = False
        self._state = VacuumState.DOCKED
        self._started_at: float | None = None
        self.battery = 92
        self.fan_power = "balanced"
        self.water_level = "medium"
        self.position = Position(ORIGIN_MM, ORIGIN_MM + 1600)  # on the dock
        self.charger = Position(ORIGIN_MM, ORIGIN_MM + 1600)
        self.commands: list[tuple[str, dict]] = []
        self.alert: DeviceAlert | None = None
        #: Tests set this to make the next operation fail.
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

    @property
    def fan_power_options(self) -> tuple[str, ...]:
        return FAN_POWERS

    @property
    def water_level_options(self) -> tuple[str, ...]:
        return WATER_LEVELS

    async def info(self) -> VacuumInfo:
        return VacuumInfo(
            device_id=self.device_id,
            driver=self.driver_name,
            model=self.model_name,
            serial="MOCKDUID0001",
            firmware="02.00.00",
            capabilities=self.capabilities,
            room_count=len(FLOOR_PLAN),
            map_count=1,
            has_mop=True,
        )

    # -- simulated progress -------------------------------------------------
    def _elapsed(self) -> float:
        return 0.0 if self._started_at is None else max(0.0, self.now() - self._started_at)

    def _tick(self) -> None:
        finished = self._elapsed() >= self.clean_duration
        if finished and self._state in (VacuumState.CLEANING, VacuumState.RETURNING):
            self._state = VacuumState.DOCKED
            self.position = Position(self.charger.x_mm, self.charger.y_mm)
            self._started_at = None

    async def status(self) -> VacuumStatus:
        self._tick()
        active = self._state.is_active
        return VacuumStatus(
            device_id=self.device_id,
            state=self._state if self.connected else VacuumState.OFFLINE,
            online=self.connected,
            battery_percent=self.battery,
            charging=self._state is VacuumState.CHARGING,
            fan_power=self.fan_power,
            water_level=self.water_level,
            mop_attached=True,
            cleaned_area_m2=round(self._elapsed() / self.clean_duration * 42.0, 1) if active else None,
            cleaning_time_minutes=int(self._elapsed() / 60) if active else None,
            position=self.position,
            current_map="Ground floor",
            error=self.alert,
            dock_state="idle",
            native_state=self._state.value,
        )

    # -- cleaning ----------------------------------------------------------
    def _start(self, label: str, detail: dict, state: VacuumState = VacuumState.CLEANING) -> dict:
        if self.fail_next == label:
            self.fail_next = None
            raise DeviceBusy(f"simulated failure for {label}")
        if self.alert is not None:
            raise DeviceBusy(f"{self.device_id} reports {self.alert.code}")
        if not self._state.accepts_new_job:
            raise DeviceBusy(f"{self.device_id} is {self._state.value}")
        self._state = state
        self._started_at = self.now()
        self.commands.append((label, detail))
        return {"acknowledged": True, "command": label, **detail}

    async def start_clean(self, options: CleanOptions | None = None) -> dict:
        await self._apply(options)
        return self._start("start_clean", {"scope": "whole map"})

    async def pause(self) -> dict:
        if self._state is not VacuumState.CLEANING:
            raise DeviceBusy(f"{self.device_id} is {self._state.value}, not cleaning")
        self._state = VacuumState.PAUSED
        self.commands.append(("pause", {}))
        return {"acknowledged": True, "command": "pause"}

    async def resume(self) -> dict:
        if self._state is not VacuumState.PAUSED:
            raise DeviceBusy(f"{self.device_id} is {self._state.value}, not paused")
        self._state = VacuumState.CLEANING
        self.commands.append(("resume", {}))
        return {"acknowledged": True, "command": "resume"}

    async def stop(self) -> dict:
        self._state = VacuumState.IDLE
        self._started_at = None
        self.commands.append(("stop", {}))
        return {"acknowledged": True, "command": "stop"}

    async def return_to_dock(self) -> dict:
        self._state = VacuumState.RETURNING
        self._started_at = self.now()
        self.commands.append(("dock", {}))
        return {"acknowledged": True, "command": "dock"}

    async def locate(self) -> dict:
        self.commands.append(("locate", {}))
        return {"acknowledged": True, "command": "locate"}

    async def clean_rooms(self, segment_ids: list[int], options: CleanOptions | None = None) -> dict:
        unknown = [s for s in segment_ids if s not in FLOOR_PLAN]
        if unknown:
            raise CommandRejected(
                f"no mapped room with segment id {', '.join(str(u) for u in unknown)}",
                hint="Mapped rooms: " + ", ".join(f"{i} ({n[0]})" for i, n in FLOOR_PLAN.items()),
            )
        await self._apply(options)
        return self._start(
            "clean_rooms",
            {"segments": list(segment_ids), "repeat": options.repeat if options else 1},
        )

    async def clean_zone(self, zones_mm, options: CleanOptions | None = None) -> dict:
        for zone in zones_mm:
            for x, y in ((zone[0], zone[1]), (zone[2], zone[3])):
                self._require_on_map(x, y)
        await self._apply(options)
        return self._start("clean_zone", {"zones": [list(z) for z in zones_mm]})

    async def go_to(self, x_mm: float, y_mm: float) -> dict:
        self._require_on_map(x_mm, y_mm)
        result = self._start("go_to", {"x_mm": x_mm, "y_mm": y_mm})
        self.position = Position(x_mm, y_mm)
        return result

    async def _apply(self, options: CleanOptions | None) -> None:
        if options is None:
            return
        if options.fan_power:
            await self.set_fan_power(options.fan_power)
        if options.water_level:
            await self.set_water_level(options.water_level)

    async def set_fan_power(self, level: str) -> dict:
        if level not in FAN_POWERS:
            raise CommandRejected(f"{level!r} is not one of {', '.join(FAN_POWERS)}")
        self.fan_power = level
        self.commands.append(("set_fan_power", {"level": level}))
        return {"acknowledged": True, "command": "set_fan_power", "level": level}

    async def set_water_level(self, level: str) -> dict:
        if level not in WATER_LEVELS:
            raise CommandRejected(f"{level!r} is not one of {', '.join(WATER_LEVELS)}")
        self.water_level = level
        self.commands.append(("set_water_level", {"level": level}))
        return {"acknowledged": True, "command": "set_water_level", "level": level}

    # -- map ---------------------------------------------------------------
    def _require_on_map(self, x_mm: float, y_mm: float) -> None:
        low_x, low_y = self._bounds[0]
        high_x, high_y = self._bounds[1]
        if not (low_x <= x_mm <= high_x and low_y <= y_mm <= high_y):
            raise CommandRejected(
                f"({x_mm:.0f}, {y_mm:.0f}) mm is outside the mapped area",
                hint=f"The map covers x {low_x:.0f}-{high_x:.0f} mm, y {low_y:.0f}-{high_y:.0f} mm.",
            )

    @property
    def _bounds(self) -> tuple[tuple[float, float], tuple[float, float]]:
        half_w = MAP_WIDTH_PX * MM_PER_PIXEL / 2
        half_h = MAP_HEIGHT_PX * MM_PER_PIXEL / 2
        return ((ORIGIN_MM - half_w, ORIGIN_MM - half_h), (ORIGIN_MM + half_w, ORIGIN_MM + half_h))

    @property
    def transform(self) -> MapTransform:
        return MapTransform.from_calibration(self.calibration)

    @property
    def calibration(self) -> list[dict]:
        """Three points relating millimetres to pixels, as a real parser emits.

        The y axis is flipped, because image rows grow downwards while the
        robot's y grows upwards - the exact convention that makes hand-rolled
        coordinate maths get it backwards.
        """
        (low_x, low_y), _high = self._bounds

        def to_px(x_mm: float, y_mm: float) -> dict:
            return {
                "x": (x_mm - low_x) / MM_PER_PIXEL,
                "y": MAP_HEIGHT_PX - (y_mm - low_y) / MM_PER_PIXEL,
            }

        return [
            {"vacuum": {"x": ORIGIN_MM, "y": ORIGIN_MM}, "map": to_px(ORIGIN_MM, ORIGIN_MM)},
            {"vacuum": {"x": ORIGIN_MM + 10000, "y": ORIGIN_MM},
             "map": to_px(ORIGIN_MM + 10000, ORIGIN_MM)},
            {"vacuum": {"x": ORIGIN_MM, "y": ORIGIN_MM + 10000},
             "map": to_px(ORIGIN_MM, ORIGIN_MM + 10000)},
        ]

    async def list_rooms(self) -> list[Room]:
        await asyncio.sleep(0)
        rooms = []
        for segment_id, (name, x0, y0, x1, y1) in FLOOR_PLAN.items():
            area = abs((x1 - x0) * (y1 - y0)) / 1_000_000
            rooms.append(
                Room(
                    segment_id=segment_id,
                    name=name,
                    center=Position((x0 + x1) / 2, (y0 + y1) / 2),
                    area_m2=round(area, 1),
                )
            )
        return rooms

    def _render_floor_plan(self) -> bytes:
        transform = self.transform
        image = Image.new("RGB", (MAP_WIDTH_PX, MAP_HEIGHT_PX), (232, 234, 238))
        draw = ImageDraw.Draw(image)
        shades = [(206, 221, 240), (214, 232, 214), (240, 226, 206), (226, 214, 238)]
        for index, (_segment, (_name, x0, y0, x1, y1)) in enumerate(FLOOR_PLAN.items()):
            corner_a = transform.to_pixels(x0, y0)
            corner_b = transform.to_pixels(x1, y1)
            box = [
                min(corner_a[0], corner_b[0]), min(corner_a[1], corner_b[1]),
                max(corner_a[0], corner_b[0]), max(corner_a[1], corner_b[1]),
            ]
            draw.rectangle(box, fill=shades[index % len(shades)], outline=(90, 96, 110))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    async def map_snapshot(self) -> MapSnapshot:
        if self.fail_next == "map":
            self.fail_next = None
            raise CommandRejected("simulated map failure")
        rooms = await self.list_rooms()
        annotated = annotate_map(
            self._render_floor_plan(),
            self.transform,
            rooms=rooms,
            robot=self.position,
            charger=self.charger,
        )
        with Image.open(io.BytesIO(annotated)) as rendered:
            width, height = rendered.size
        return MapSnapshot(
            image_png=annotated,
            width=width,
            height=height,
            calibration=self.calibration,
            rooms=rooms,
            robot=self.position,
            charger=self.charger,
            map_name="Ground floor",
        )
