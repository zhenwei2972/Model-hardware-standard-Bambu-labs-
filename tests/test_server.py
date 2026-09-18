"""End-to-end through the MCP tool surface, against the mock driver."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from tests.conftest import tool_body

from mhs.server import create_server


@pytest.fixture
def server(settings):
    srv = create_server(settings, run_scheduler=False)
    yield srv
    srv.mhs_app.store.close()


async def call(server, name, **arguments) -> dict:
    return tool_body(await server.call_tool(name, arguments))


async def test_every_tool_is_advertised_with_a_description(server):
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"get_status", "start_print", "schedule_print", "capture_snapshot",
            "log_print_result", "check_connection"} <= names
    assert all(t.description for t in tools)


async def test_list_devices_reports_capabilities(server):
    body = await call(server, "list_devices")
    assert body["ok"] is True
    entry = body["devices"][0]
    assert entry["device_id"] == "mock" and entry["connected"] is True
    assert "camera_snapshot" in entry["capabilities"]


async def test_status_has_summary_and_structured_fields(server):
    body = await call(server, "get_status")
    assert body["ok"] is True
    assert body["status"]["state"] == "idle"
    assert "mock" in body["summary"]


async def test_start_print_requires_confirmation(server):
    body = await call(server, "start_print", file="cache/benchy.3mf")
    assert body["ok"] is False and body["error"] == "ConfirmationRequired"
    assert (await call(server, "get_status"))["status"]["state"] == "idle"

    body = await call(server, "start_print", file="cache/benchy.3mf", confirm=True)
    assert body["ok"] is True and body["result"]["success"] is True
    assert body["run_id"] == 1
    assert (await call(server, "get_status"))["status"]["state"] == "running"


async def test_stop_print_requires_confirmation(server):
    await call(server, "start_print", file="cache/benchy.3mf", confirm=True)
    assert (await call(server, "stop_print"))["error"] == "ConfirmationRequired"
    assert (await call(server, "get_status"))["status"]["state"] == "running"
    assert (await call(server, "stop_print", confirm=True))["ok"] is True
    assert (await call(server, "get_status"))["status"]["state"] == "idle"


async def test_pause_and_resume(server):
    await call(server, "start_print", file="cache/benchy.3mf", confirm=True)
    assert (await call(server, "pause_print"))["ok"] is True
    assert (await call(server, "get_status"))["status"]["state"] == "paused"
    assert (await call(server, "resume_print"))["ok"] is True
    assert (await call(server, "get_status"))["status"]["state"] == "running"


async def test_printing_twice_is_refused_with_a_reason(server):
    await call(server, "start_print", file="cache/benchy.3mf", confirm=True)
    body = await call(server, "start_print", file="cache/benchy.3mf", confirm=True)
    assert body["ok"] is False and body["error"] == "DeviceBusy"


async def test_unknown_file_is_refused(server):
    body = await call(server, "start_print", file="cache/nope.3mf", confirm=True)
    assert body["ok"] is False and body["error"] == "FileTransferError"


async def test_upload_and_print(server, tmp_path):
    sliced = tmp_path / "part.3mf"
    sliced.write_bytes(b"fake 3mf")
    body = await call(server, "upload_and_print", local_path=str(sliced), confirm=True)
    assert body["ok"] is True
    assert body["uploaded"]["path"] == "cache/part.3mf"
    assert (await call(server, "get_status"))["status"]["state"] == "running"


async def test_gcode_safe_list_is_enforced(server):
    assert (await call(server, "send_gcode", gcode="M104 S210", confirm=True))["ok"] is True
    blocked = await call(server, "send_gcode", gcode="M997", confirm=True)
    assert blocked["ok"] is False and "safe list" in blocked["message"]


async def test_read_only_mode_blocks_writes_but_not_reads(settings):
    settings.read_only = True
    server = create_server(settings, run_scheduler=False)
    try:
        assert (await call(server, "get_status"))["ok"] is True
        body = await call(server, "start_print", file="cache/benchy.3mf", confirm=True)
        assert body["ok"] is False and body["error"] == "ControlDisabled"
        assert "read-only" in body["message"]
    finally:
        server.mhs_app.store.close()


async def test_snapshot_returns_an_image_and_saves_it(server, settings):
    result = await server.call_tool("capture_snapshot", {})
    content = result.content[0]
    assert content.type == "image" and content.mime_type == "image/jpeg"
    saved = list((settings.capture_dir / "mock").glob("*.jpg"))
    assert len(saved) == 1
    observations = server.mhs_app.store.list_observations(device_id="mock")
    assert observations[0]["image_path"] == str(saved[0])


async def test_capture_frames_writes_a_series(server, settings):
    body = await call(server, "capture_frames", count=3, interval_seconds=1)
    assert body["ok"] is True and body["count"] == 3
    assert len(list((settings.capture_dir / "mock").glob("*series*.jpg"))) == 3


async def test_capture_frames_validates_bounds(server):
    assert (await call(server, "capture_frames", count=0))["ok"] is False
    assert (await call(server, "capture_frames", count=60, interval_seconds=120))["ok"] is False


async def test_read_capture_refuses_paths_outside_the_capture_dir(server):
    with pytest.raises(Exception, match="outside the capture directory"):
        await server.call_tool("read_capture", {"path": "/etc/passwd"})


async def test_schedule_then_list_then_cancel(server):
    body = await call(server, "schedule_print", file="cache/benchy.3mf", when="+2h", note="overnight")
    job_id = body["job"]["id"]
    assert body["ok"] is True and body["job"]["status"] == "pending"
    assert 7100 < body["starts_in_seconds"] <= 7200

    listed = await call(server, "list_scheduled_jobs")
    assert [j["id"] for j in listed["jobs"]] == [job_id]

    cancelled = await call(server, "cancel_scheduled_job", job_id=job_id)
    assert cancelled["job"]["status"] == "cancelled"
    assert (await call(server, "cancel_scheduled_job", job_id="job_nope"))["error"] == "NotFound"


async def test_schedule_rejects_a_file_that_is_not_on_the_printer(server):
    body = await call(server, "schedule_print", file="cache/ghost.3mf", when="+1h")
    assert body["ok"] is False and "not on" in body["message"]


async def test_schedule_rejects_unparseable_times(server):
    body = await call(server, "schedule_print", file="cache/benchy.3mf", when="soonish")
    assert body["ok"] is False and body["error"] == "ConfigError"


async def test_scheduled_job_fires_through_the_app_scheduler(server):
    await call(server, "schedule_print", file="cache/benchy.3mf", when="+1h")
    app = server.mhs_app
    job = app.store.list_jobs()[0]
    # Pretend the target time has arrived.
    app.store._execute(
        "UPDATE scheduled_jobs SET start_at = ? WHERE id = ?", (time.time() - 5, job.id)
    )
    [result] = await app.scheduler.tick()
    assert result["action"] == "started"
    assert (await call(server, "get_status"))["status"]["state"] == "running"


async def test_journal_supports_settings_iteration(server):
    started = await call(server, "start_print", file="cache/benchy.3mf", confirm=True,
                         job_name="bracket", flow_calibration=False)
    run_id = started["run_id"]
    await call(server, "record_observation", text="layer 40: stringing", run_id=run_id, layer=40)
    logged = await call(server, "log_print_result", run_id=run_id, outcome="partial",
                        quality_score=6, notes="corners lifted")
    assert logged["run"]["outcome"] == "partial"

    history = await call(server, "list_print_history")
    assert history["runs"][0]["settings"]["flow_calibration"] is False
    detail = await call(server, "get_print_run", run_id=run_id)
    assert detail["run"]["observations"][0]["text"] == "layer 40: stringing"


async def test_log_print_result_validates_input(server):
    assert (await call(server, "log_print_result", run_id=1, outcome="meh"))["ok"] is False
    await call(server, "start_print", file="cache/benchy.3mf", confirm=True)
    bad_score = await call(server, "log_print_result", run_id=1, outcome="success", quality_score=99)
    assert bad_score["ok"] is False
    assert (await call(server, "get_print_run", run_id=999))["error"] == "NotFound"


async def test_check_connection_reports_each_transport(server):
    body = await call(server, "check_connection")
    assert body["all_ok"] is True
    assert body["telemetry"]["ok"] and body["files"]["ok"] and body["camera"]["ok"]


async def test_resources_are_readable(server):
    contents = list(await server.read_resource("mhs://printers"))
    assert "mock" in contents[0].content
    status = list(await server.read_resource("mhs://printer/mock/status"))
    assert '"state": "idle"' in status[0].content


async def test_prompts_are_available(server):
    prompts = await server.list_prompts()
    assert {p.name for p in prompts} == {"diagnose_print", "tune_settings", "design_iteration"}


# -- design loop ------------------------------------------------------------
@pytest.fixture
def stl(tmp_path):
    from mhs.design.mesh import save_stl, uv_sphere

    return str(save_stl(uv_sphere(30, 48, 32), tmp_path / "ball.stl"))


async def test_analyze_model_reports_geometry_and_a_verdict(server, stl):
    body = await call(server, "analyze_model", path=stl)
    assert body["ok"] is True
    assert body["model"]["dimensions_mm"]["x"] == pytest.approx(60.0)
    assert body["printability"]["printable"] is True
    assert "resolves about 0.4 mm in XY" in body["verdict"]


async def test_analyze_model_blocks_something_too_big_for_the_plate(server, tmp_path):
    from mhs.design.mesh import save_stl, unit_cube

    path = str(save_stl(unit_cube(300), tmp_path / "huge.stl"))
    body = await call(server, "analyze_model", path=path)
    assert body["printability"]["printable"] is False
    assert "too_large" in {f["code"] for f in body["printability"]["findings"]}


async def test_analyze_model_rejects_a_non_mesh(server, tmp_path):
    sliced = tmp_path / "plate.3mf.gcode"
    sliced.write_text("G28")
    body = await call(server, "analyze_model", path=str(sliced))
    assert body["ok"] is False and "not a mesh format" in body["message"]
    assert (await call(server, "analyze_model", path="/nope/ghost.stl"))["ok"] is False


async def test_preview_model_returns_a_png(server, stl):
    result = await server.call_tool("preview_model", {"path": stl, "views": ["iso", "bottom"]})
    content = result.content[0]
    assert content.type == "image" and content.mime_type == "image/png"


async def test_preview_rejects_an_unknown_view(server, stl):
    with pytest.raises(Exception, match="unknown view"):
        await server.call_tool("preview_model", {"path": stl, "views": ["sideways"]})


async def test_scale_model_dry_run_does_not_write(server, stl, settings):
    body = await call(server, "scale_model", path=stl, target_mm=24.65)
    assert body["ok"] is True and body["applied"] is False
    assert body["factor"] == pytest.approx(24.65 / 60, rel=1e-3)
    assert body["dimensions_after_mm"][0] == pytest.approx(24.65)
    assert not list((settings.capture_dir / "models").glob("*.stl"))


async def test_scale_model_apply_writes_a_scaled_stl(server, stl):
    from mhs.design.mesh import load_mesh

    body = await call(server, "scale_model", path=stl, target_mm=24.65, apply=True)
    assert body["applied"] is True
    written = load_mesh(body["output"])
    assert written.dimensions.max() == pytest.approx(24.65, rel=1e-3)
    assert body["printability"]["printable"] is True
    # Shrinking costs detail, and the report says so rather than hiding it.
    assert (body["detail_below_nozzle_percent_after"]
            >= body["detail_below_nozzle_percent_before"])


# -- measuring against the camera -------------------------------------------
async def test_camera_grid_returns_an_image_and_remembers_the_frame(server):
    result = await server.call_tool("camera_grid", {})
    assert result.content[0].type == "image"
    assert server.mhs_app.store.latest_snapshot("mock") is not None


async def test_measuring_before_calibration_reports_pixels_only(server):
    await server.call_tool("camera_grid", {})
    body = await call(server, "camera_measure", point_a=[10, 10], point_b=[110, 10])
    assert body["pixels"] == pytest.approx(100.0)
    assert body["millimetres"] is None
    assert "camera_calibrate" in body["hint"]


async def test_calibrate_then_measure_against_a_target(server):
    await server.call_tool("camera_grid", {})
    calibrated = await call(server, "camera_calibrate", reference="sgd_1", pixel_length=100)
    assert calibrated["calibration"]["mm_per_pixel"] == pytest.approx(0.2465)
    assert "off-axis" in calibrated["caveat"]

    body = await call(server, "camera_measure", point_a=[0, 0], point_b=[100, 0], target_mm=24.65)
    assert body["millimetres"] == pytest.approx(24.65)
    assert body["comparison"]["error_percent"] == pytest.approx(0.0)
    assert Path(body["annotated_image"]).is_file()


async def test_calibration_persists_and_is_replaced_by_a_newer_one(server):
    await call(server, "camera_calibrate", reference="usd_quarter", pixel_length=100)
    await call(server, "camera_calibrate", reference="sgd_1", pixel_length=100)
    stored = server.mhs_app.store.get_calibration("mock")
    assert stored["reference_name"] == "Singapore $1"


async def test_measure_without_a_frame_says_what_to_do(server):
    body = await call(server, "camera_measure", point_a=[0, 0], point_b=[10, 0])
    assert body["ok"] is False and "camera_grid first" in body["hint"]


async def test_measure_validates_its_points(server):
    await server.call_tool("camera_grid", {})
    assert (await call(server, "camera_measure", point_a=[1], point_b=[2, 3]))["ok"] is False


async def test_unknown_reference_object_is_rejected(server):
    body = await call(server, "camera_calibrate", reference="doubloon", pixel_length=50)
    assert body["ok"] is False and "usd_quarter" in body["hint"]


async def test_annotated_images_can_be_reopened_but_not_arbitrary_paths(server):
    await server.call_tool("camera_grid", {})
    await call(server, "camera_calibrate", reference="sgd_1", pixel_length=100)
    body = await call(server, "camera_measure", point_a=[0, 0], point_b=[50, 0])
    reopened = await server.call_tool("read_annotated_image", {"path": body["annotated_image"]})
    assert reopened.content[0].type == "image"
    with pytest.raises(Exception, match="outside the capture directory"):
        await server.call_tool("read_annotated_image", {"path": "/etc/hostname"})


# -- MHS standard surface ---------------------------------------------------
async def test_describe_device_returns_descriptor_and_reference_sheet(server):
    body = await call(server, "describe_device")
    assert body["descriptor"]["device"]["kind"] == "fdm_3d_printer"
    assert body["descriptor"]["safety"]["enforced_in"] == "driver"
    assert "## Channels" in body["reference"]


async def test_list_and_read_channels(server):
    channels = (await call(server, "list_channels"))["channels"]
    assert {"printer.state", "nozzle.temperature"} <= {c["name"] for c in channels}
    body = await call(server, "read_channel", channel="nozzle.temperature")
    assert body["value"] == 25.0 and body["unit"] == "celsius"


async def test_reading_a_binary_channel_describes_it_rather_than_dumping_it(server):
    body = await call(server, "read_channel", channel="camera.frame")
    assert body["value_type"] == "binary" and body["bytes"] > 0


async def test_write_channel_enforces_limits_from_the_driver(server):
    over = await call(server, "write_channel", channel="nozzle.temperature", value=350)
    assert over["ok"] is False and over["error"] == "SafetyViolation"
    assert "rated to 300" in over["hint"]

    ok = await call(server, "write_channel", channel="nozzle.temperature", value=215)
    assert ok["ok"] is True


async def test_write_channel_requires_confirmation_for_physical_actions(server):
    await call(server, "start_print", file="cache/benchy.3mf", confirm=True)
    blocked = await call(server, "write_channel", channel="job.control", value="stop")
    assert blocked["ok"] is False and "requires confirmation" in blocked["message"]
    assert (await call(server, "get_status"))["status"]["state"] == "running"

    allowed = await call(server, "write_channel", channel="job.control", value="stop", confirm=True)
    assert allowed["ok"] is True
    assert (await call(server, "get_status"))["status"]["state"] == "idle"


async def test_write_channel_is_blocked_in_read_only_mode(settings):
    settings.read_only = True
    server = create_server(settings, run_scheduler=False)
    try:
        body = await call(server, "write_channel", channel="nozzle.temperature", value=200)
        assert body["ok"] is False and body["error"] == "ControlDisabled"
    finally:
        server.mhs_app.store.close()


async def test_reference_objects_resource_lists_coins(server):
    contents = list(await server.read_resource("mhs://reference-objects"))
    assert "usd_quarter" in contents[0].content
    assert "camera_calibrate" in contents[0].content


async def test_descriptor_resource(server):
    contents = list(await server.read_resource("mhs://printer/mock/descriptor"))
    assert "fdm_3d_printer" in contents[0].content
