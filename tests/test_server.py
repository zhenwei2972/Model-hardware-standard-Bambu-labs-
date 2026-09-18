"""End-to-end through the MCP tool surface, against the mock driver."""

from __future__ import annotations

import time

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


async def test_list_printers_reports_capabilities(server):
    body = await call(server, "list_printers")
    assert body["ok"] is True
    entry = body["printers"][0]
    assert entry["printer_id"] == "mock" and entry["connected"] is True
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
    observations = server.mhs_app.store.list_observations(printer_id="mock")
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
    assert {p.name for p in prompts} == {"diagnose_print", "tune_settings"}
