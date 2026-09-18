"""The vacuum device class and its MHS channels, against the mock robot."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from mhs.errors import CommandRejected, DeviceBusy, NotSupported
from mhs.models import Capability, CleanOptions, VacuumState
from mhs.standard.channels import SafetyViolation


# -- lifecycle -------------------------------------------------------------
async def test_starts_docked_and_reports_itself(robot):
    status = await robot.status()
    assert status.state is VacuumState.DOCKED
    assert status.online and status.battery_percent == 92
    assert "docked" in status.summary()


async def test_info_describes_the_robot(robot):
    info = await robot.info()
    assert info.kind == "robot_vacuum"
    assert info.model == "Saros 10"
    assert info.room_count == 4
    assert Capability.GO_TO in info.capabilities


async def test_disconnected_robot_reads_offline(robot):
    await robot.disconnect()
    assert (await robot.status()).state is VacuumState.OFFLINE


# -- cleaning --------------------------------------------------------------
async def test_clean_everything_then_pause_resume_stop(robot):
    await robot.start_clean()
    assert (await robot.status()).state is VacuumState.CLEANING
    await robot.pause()
    assert (await robot.status()).state is VacuumState.PAUSED
    await robot.resume()
    assert (await robot.status()).state is VacuumState.CLEANING
    await robot.stop()
    assert (await robot.status()).state is VacuumState.IDLE


async def test_pausing_an_idle_robot_is_refused(robot):
    with pytest.raises(DeviceBusy, match="not cleaning"):
        await robot.pause()
    with pytest.raises(DeviceBusy, match="not paused"):
        await robot.resume()


async def test_clean_rooms_by_segment(robot):
    await robot.clean_rooms([16, 18], CleanOptions(repeat=2))
    name, payload = robot.commands[-1]
    assert name == "clean_rooms" and payload == {"segments": [16, 18], "repeat": 2}


async def test_cleaning_an_unmapped_room_lists_what_exists(robot):
    with pytest.raises(CommandRejected) as excinfo:
        await robot.clean_rooms([99])
    assert "Kitchen" in excinfo.value.hint


async def test_clean_options_apply_suction_and_water(robot):
    await robot.start_clean(CleanOptions(fan_power="turbo", water_level="high"))
    assert robot.fan_power == "turbo" and robot.water_level == "high"
    status = await robot.status()
    assert status.fan_power == "turbo"


async def test_unknown_suction_preset_is_refused(robot):
    with pytest.raises(CommandRejected, match="quiet, balanced, turbo, max"):
        await robot.set_fan_power("ludicrous")


async def test_a_fault_blocks_a_new_job(robot):
    from mhs.models import DeviceAlert

    robot.alert = DeviceAlert(code="ROBOROCK_7", severity="serious", message="Wheel stuck")
    with pytest.raises(DeviceBusy, match="ROBOROCK_7"):
        await robot.start_clean()


async def test_cannot_start_while_already_cleaning(robot):
    await robot.start_clean()
    with pytest.raises(DeviceBusy, match="cleaning"):
        await robot.start_clean()


async def test_paused_robot_can_take_a_new_job(robot):
    """A cleaning run is interruptible, unlike a print."""
    await robot.start_clean()
    await robot.pause()
    await robot.clean_rooms([16])
    assert (await robot.status()).state is VacuumState.CLEANING


# -- pointing at the map ---------------------------------------------------
async def test_go_to_moves_the_robot(robot):
    await robot.go_to(24200, 26200)
    status = await robot.status()
    assert (status.position.x_mm, status.position.y_mm) == (24200, 26200)


async def test_go_to_off_map_is_refused_with_the_real_bounds(robot):
    with pytest.raises(CommandRejected) as excinfo:
        await robot.go_to(1000, 1000)
    assert "outside the mapped area" in excinfo.value.message
    assert "The map covers" in excinfo.value.hint


async def test_zone_must_be_on_the_map(robot):
    with pytest.raises(CommandRejected, match="outside the mapped area"):
        await robot.clean_zone([(1000, 1000, 2000, 2000)])
    await robot.clean_zone([(24000, 24000, 25000, 25000)])


async def test_rooms_have_names_centres_and_areas(robot):
    rooms = {room.name: room for room in await robot.list_rooms()}
    assert set(rooms) == {"Kitchen", "Living room", "Hallway", "Bedroom"}
    kitchen = rooms["Kitchen"]
    assert kitchen.segment_id == 16
    assert kitchen.center is not None and kitchen.area_m2 > 0


async def test_map_snapshot_round_trips_coordinates(robot):
    """A point placed on the map must come back as the same millimetres."""
    snapshot = await robot.map_snapshot()
    assert snapshot.image_png.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(snapshot.image_png)).size == (snapshot.width, snapshot.height)

    from mhs.design.mapviz import MapTransform

    transform = MapTransform.from_calibration(snapshot.calibration)
    for room in snapshot.rooms:
        px, py = transform.to_pixels(room.center.x_mm, room.center.y_mm)
        assert transform.to_mm(px, py) == pytest.approx((room.center.x_mm, room.center.y_mm))


async def test_map_marks_the_robot_and_the_dock(robot):
    await robot.go_to(24200, 26200)
    snapshot = await robot.map_snapshot()
    assert (snapshot.robot.x_mm, snapshot.robot.y_mm) == (24200, 26200)
    assert snapshot.charger is not None


# -- MHS channels ----------------------------------------------------------
async def test_vacuum_channels_are_present(robot):
    names = robot.channel_table().names()
    assert {"vacuum.state", "battery.level", "vacuum.command", "vacuum.goto",
            "clean.rooms", "fan.power", "water.level", "map.image"} <= set(names)


async def test_reading_channels(robot):
    assert await robot.read("vacuum.state") == "docked"
    assert await robot.read("battery.level") == 92
    assert (await robot.read("vacuum.position"))["x_mm"] == 25500
    assert (await robot.read("map.image")).startswith(b"\x89PNG")


async def test_command_channel_needs_confirmation(robot):
    with pytest.raises(SafetyViolation, match="requires confirmation"):
        await robot.write("vacuum.command", "start")
    await robot.write("vacuum.command", "start", confirm=True)
    assert (await robot.status()).state is VacuumState.CLEANING


async def test_command_channel_rejects_unknown_actions(robot):
    with pytest.raises(SafetyViolation, match="not allowed"):
        await robot.write("vacuum.command", "selfdestruct", confirm=True)


async def test_goto_channel_parses_and_validates_its_pair(robot):
    await robot.write("vacuum.goto", "24200,26200", confirm=True)
    assert (await robot.status()).position.x_mm == 24200
    with pytest.raises(SafetyViolation, match='expected "x,y"'):
        await robot.write("vacuum.goto", "over there", confirm=True)


async def test_clean_rooms_channel_accepts_a_list_or_a_string(robot):
    await robot.write("clean.rooms", [16, 17], confirm=True)
    assert robot.commands[-1][1]["segments"] == [16, 17]
    await robot.stop()
    await robot.write("clean.rooms", "18 19", confirm=True)
    assert robot.commands[-1][1]["segments"] == [18, 19]


async def test_fan_channel_limits_come_from_the_device(robot):
    channel = robot.channel_table().get("fan.power")
    assert channel.limit.allowed == ("quiet", "balanced", "turbo", "max")
    with pytest.raises(SafetyViolation, match="not allowed"):
        await robot.write("fan.power", "ludicrous")
    await robot.write("fan.power", "turbo")
    assert robot.fan_power == "turbo"


# -- descriptor ------------------------------------------------------------
async def test_descriptor_describes_a_vacuum_not_a_printer(robot):
    descriptor = await robot.describe()
    assert descriptor["device"]["kind"] == "robot_vacuum"
    assert descriptor["device"]["vendor"] == "MHS"
    assert descriptor["physical"]["suction_pa"] == 22000
    assert any("robot vacuum" in tag for tag in descriptor["tags"])
    assert any("closed door" in item for item in descriptor["cannot"])
    assert "build_volume_mm" not in descriptor.get("physical", {})


async def test_descriptor_markdown_uses_the_vacuum_heading(robot):
    from mhs.standard.descriptor import descriptor_markdown

    text = descriptor_markdown(await robot.describe())
    assert "## What it can reach" in text
    assert "`vacuum.goto`" in text


async def test_unsupported_verbs_say_so():
    from mhs.config import DeviceConfig
    from mhs.vacuum import Vacuum

    class Bare(Vacuum):
        driver_name = "bare"
        capabilities = ()

        async def connect(self): ...
        async def disconnect(self): ...
        async def info(self): ...
        async def status(self): ...
        async def start_clean(self, options=None): ...
        async def pause(self): ...
        async def resume(self): ...
        async def stop(self): ...
        async def return_to_dock(self): ...

    bare = Bare("bare")
    assert isinstance(DeviceConfig, type)
    for coro in (bare.go_to(1, 2), bare.clean_rooms([1]), bare.map_snapshot(), bare.locate()):
        with pytest.raises(NotSupported):
            await coro
