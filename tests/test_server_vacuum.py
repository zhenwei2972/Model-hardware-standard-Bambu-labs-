"""The vacuum half of the MCP surface, end to end against the mock robot."""

from __future__ import annotations

import pytest
from tests.conftest import tool_body

from mhs.server import create_server


@pytest.fixture
def server(vacuum_settings):
    srv = create_server(vacuum_settings, run_scheduler=False)
    yield srv
    srv.mhs_app.store.close()


async def call(server, tool, **arguments) -> dict:
    return tool_body(await server.call_tool(tool, arguments))


async def test_vacuum_tools_are_advertised(server):
    names = {t.name for t in await server.list_tools()}
    assert {"vacuum_status", "list_rooms", "start_cleaning", "go_to", "clean_zone",
            "get_map", "save_location", "return_to_dock"} <= names


async def test_status_and_rooms(server):
    status = await call(server, "vacuum_status")
    assert status["ok"] and status["status"]["state"] == "docked"
    rooms = await call(server, "list_rooms")
    assert rooms["count"] == 4
    assert {r["name"] for r in rooms["rooms"]} == {"Kitchen", "Living room", "Hallway", "Bedroom"}


async def test_cleaning_requires_confirmation(server):
    body = await call(server, "start_cleaning", rooms=["kitchen"])
    assert body["error"] == "ConfirmationRequired"
    assert "the kitchen" in body["message"]
    assert (await call(server, "vacuum_status"))["status"]["state"] == "docked"

    body = await call(server, "start_cleaning", rooms=["kitchen"], confirm=True)
    assert body["ok"] and body["result"]["rooms"] == [16]
    assert (await call(server, "vacuum_status"))["status"]["state"] == "cleaning"


async def test_room_names_resolve_case_insensitively_and_partially(server):
    body = await call(server, "start_cleaning", rooms=["KITCHEN", "living"], confirm=True)
    assert body["result"]["rooms"] == [16, 17]


async def test_room_ids_work_too(server):
    body = await call(server, "start_cleaning", rooms=["18"], confirm=True)
    assert body["result"]["rooms"] == [18]


async def test_unknown_room_lists_the_real_ones(server):
    body = await call(server, "start_cleaning", rooms=["bathroom"], confirm=True)
    assert body["ok"] is False
    assert "Kitchen (id 16)" in body["hint"]


async def test_cleaning_everything_when_no_rooms_given(server):
    body = await call(server, "start_cleaning", confirm=True)
    assert body["ok"] and body["run_id"] == 1
    assert body["result"]["scope"] == "whole map"


# -- going to a point ------------------------------------------------------
async def test_go_to_coordinates(server):
    preview = await call(server, "go_to", x_mm=24200, y_mm=26200)
    assert preview["error"] == "ConfirmationRequired"
    assert "24200" in preview["message"]

    body = await call(server, "go_to", x_mm=24200, y_mm=26200, confirm=True)
    assert body["ok"] and body["target"]["x_mm"] == 24200
    assert (await call(server, "vacuum_status"))["status"]["position"]["x_mm"] == 24200


async def test_go_to_a_room_by_name_uses_its_centre(server):
    body = await call(server, "go_to", location="bedroom", confirm=True)
    assert body["ok"] and "Bedroom" in body["target"]["label"]
    assert body["target"]["x_mm"] == pytest.approx(26750)


async def test_save_then_go_to_a_named_location(server):
    saved = await call(server, "save_location", name="Dog Bowl", x_mm=24200, y_mm=26200,
                       note="by the back door")
    assert saved["location"]["name"] == "dog bowl"

    body = await call(server, "go_to", location="dog bowl", confirm=True)
    assert body["ok"] and "saved location" in body["target"]["label"]

    listed = await call(server, "list_locations")
    assert [entry["name"] for entry in listed["locations"]] == ["dog bowl"]
    assert (await call(server, "delete_location", name="dog bowl"))["ok"] is True
    assert (await call(server, "delete_location", name="dog bowl"))["error"] == "NotFound"


async def test_go_to_nothing_known_lists_the_options(server):
    await call(server, "save_location", name="corner", x_mm=24000, y_mm=24000)
    body = await call(server, "go_to", location="atlantis", confirm=True)
    assert body["ok"] is False
    assert "corner" in body["hint"] and "Kitchen" in body["hint"]


async def test_go_to_needs_somewhere_to_go(server):
    body = await call(server, "go_to", confirm=True)
    assert body["ok"] is False and "location name or both" in body["message"]


async def test_go_to_off_map_is_refused(server):
    body = await call(server, "go_to", x_mm=1000, y_mm=1000, confirm=True)
    assert body["ok"] is False and "outside the mapped area" in body["message"]


# -- zones, settings, map --------------------------------------------------
async def test_clean_zone(server):
    preview = await call(server, "clean_zone", x1_mm=24000, y1_mm=24000, x2_mm=25000, y2_mm=25000)
    assert preview["error"] == "ConfirmationRequired"
    body = await call(server, "clean_zone", x1_mm=24000, y1_mm=24000,
                      x2_mm=25000, y2_mm=25000, confirm=True)
    assert body["ok"] is True


async def test_pause_resume_stop_and_dock(server):
    await call(server, "start_cleaning", confirm=True)
    assert (await call(server, "pause_cleaning"))["ok"]
    assert (await call(server, "vacuum_status"))["status"]["state"] == "paused"
    assert (await call(server, "resume_cleaning"))["ok"]

    assert (await call(server, "stop_cleaning"))["error"] == "ConfirmationRequired"
    assert (await call(server, "stop_cleaning", confirm=True))["ok"]
    assert (await call(server, "return_to_dock"))["ok"]
    assert (await call(server, "vacuum_status"))["status"]["state"] == "returning"


async def test_suction_and_water(server):
    body = await call(server, "set_suction", level="turbo")
    assert body["ok"] and "turbo" in body["options"]
    assert (await call(server, "set_suction", level="ludicrous"))["ok"] is False
    assert (await call(server, "set_water_flow", level="high"))["ok"] is True


async def test_find_vacuum(server):
    assert (await call(server, "find_vacuum"))["ok"] is True


async def test_get_map_returns_an_image_and_saves_it(server, vacuum_settings):
    result = await server.call_tool("get_map", {})
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/png"
    assert list((vacuum_settings.capture_dir / "saros").glob("*map*.png"))


# -- device kinds ----------------------------------------------------------
async def test_printer_tools_refuse_a_vacuum(server):
    body = await call(server, "get_status")
    assert body["ok"] is False and body["error"] == "NotSupported"
    assert "not a 3D printer" in body["message"]


async def test_read_only_blocks_cleaning_but_not_looking(vacuum_settings):
    vacuum_settings.read_only = True
    server = create_server(vacuum_settings, run_scheduler=False)
    try:
        assert (await call(server, "vacuum_status"))["ok"] is True
        body = await call(server, "start_cleaning", confirm=True)
        assert body["ok"] is False and body["error"] == "ControlDisabled"
        body = await call(server, "go_to", x_mm=24200, y_mm=26200, confirm=True)
        assert body["error"] == "ControlDisabled"
    finally:
        server.mhs_app.store.close()


async def test_mhs_primitives_work_on_a_vacuum(server):
    channels = (await call(server, "list_channels"))["channels"]
    assert {"vacuum.state", "vacuum.goto"} <= {c["name"] for c in channels}
    assert (await call(server, "read_channel", channel="battery.level"))["value"] == 92

    blocked = await call(server, "write_channel", channel="vacuum.command", value="start")
    assert blocked["error"] == "SafetyViolation"
    assert (await call(server, "write_channel", channel="vacuum.command",
                       value="start", confirm=True))["ok"] is True


async def test_descriptor_for_a_vacuum(server):
    body = await call(server, "describe_device")
    assert body["descriptor"]["device"]["kind"] == "robot_vacuum"
    assert "## What it can reach" in body["reference"]


async def test_device_generic_resources(server):
    listing = list(await server.read_resource("mhs://devices"))
    assert "robot_vacuum" in listing[0].content
    status = list(await server.read_resource("mhs://device/saros/status"))
    assert '"state": "docked"' in status[0].content
    descriptor = list(await server.read_resource("mhs://device/saros/descriptor"))
    assert "robot_vacuum" in descriptor[0].content


async def test_both_device_kinds_in_one_server(tmp_path):
    """A printer and a vacuum side by side: each tool finds the right one."""
    from mhs.config import DeviceConfig, Settings

    settings = Settings(
        devices={
            "a1": DeviceConfig("a1", driver="mock", model="A1 mini"),
            "saros": DeviceConfig("saros", driver="mock_vacuum", model="Saros 10"),
        },
        state_dir=tmp_path / "mixed",
    )
    server = create_server(settings, run_scheduler=False)
    try:
        listing = await call(server, "list_devices")
        kinds = {entry["device_id"]: entry["kind"] for entry in listing["devices"]}
        assert kinds == {"a1": "fdm_3d_printer", "saros": "robot_vacuum"}

        assert (await call(server, "get_status", printer="a1"))["status"]["state"] == "idle"
        assert (await call(server, "vacuum_status", vacuum="saros"))["status"]["state"] == "docked"

        # Naming the wrong kind is an explicit error, not a confusing failure.
        assert "not a robot vacuum" in (await call(server, "vacuum_status", vacuum="a1"))["message"]
        assert "not a 3D printer" in (await call(server, "get_status", printer="saros"))["message"]

        # The MHS primitives work on both.
        assert (await call(server, "read_channel", channel="nozzle.temperature",
                           device="a1"))["ok"] is True
        assert (await call(server, "read_channel", channel="battery.level",
                           device="saros"))["value"] == 92
    finally:
        server.mhs_app.store.close()
