"""Milestone detection, the watcher, and the run report."""

from __future__ import annotations

import time

import pytest
from tests.conftest import tool_body

from mhs.app import MHSApp
from mhs.models import DeviceAlert, PrinterStatus, PrintOptions, PrintState
from mhs.monitor import PrintMonitor, build_report, detect_milestones, stage_name
from mhs.server import create_server
from mhs.store import Store


def status(
    state: PrintState = PrintState.RUNNING,
    layer: int | None = None,
    total: int | None = 100,
    alerts: tuple[DeviceAlert, ...] = (),
) -> PrinterStatus:
    return PrinterStatus(
        device_id="mock",
        state=state,
        online=True,
        job_name="benchy",
        current_layer=layer,
        total_layers=total,
        alerts=list(alerts),
    )


# -- detection --------------------------------------------------------------
def test_start_fires_when_the_job_becomes_active():
    crossed = detect_milestones(status(PrintState.IDLE), status(PrintState.RUNNING, layer=0))
    assert [name for name, _ in crossed] == ["start"]


def test_start_does_not_fire_again_while_it_keeps_running():
    running = status(PrintState.RUNNING, layer=0)
    assert detect_milestones(running, status(PrintState.RUNNING, layer=0)) == []


def test_first_layer_fires_when_layer_two_begins():
    crossed = detect_milestones(status(layer=1), status(layer=2))
    assert [name for name, _ in crossed] == ["first_layer"]
    assert "layer 2" in crossed[0][1]


def test_first_layer_does_not_fire_later_in_the_print():
    assert detect_milestones(status(layer=40), status(layer=41)) == []


def test_progress_points_fire_on_the_crossing_only():
    assert [n for n, _ in detect_milestones(status(layer=24), status(layer=25))] == ["quarter"]
    assert detect_milestones(status(layer=25), status(layer=26)) == []


def test_a_jump_reports_every_point_it_skipped_over():
    """A slow poll should not lose the milestones it flew past."""
    names = [n for n, _ in detect_milestones(status(layer=2), status(layer=80))]
    assert names == ["quarter", "half", "three_quarters"]


def test_progress_needs_a_layer_count():
    assert detect_milestones(status(layer=2, total=None), status(layer=80, total=None)) == []


@pytest.mark.parametrize(
    ("state", "expected"),
    [(PrintState.FINISHED, "finished"), (PrintState.FAILED, "failed"), (PrintState.PAUSED, "paused")],
)
def test_terminal_states_fire_once(state, expected):
    crossed = detect_milestones(status(layer=99), status(state, layer=100))
    assert expected in [name for name, _ in crossed]
    assert detect_milestones(status(state, layer=100), status(state, layer=100)) == []


def test_a_new_alert_fires_but_a_standing_one_does_not():
    alert = DeviceAlert(code="0300-8000", message="filament runout")
    crossed = detect_milestones(status(layer=10), status(layer=11, alerts=(alert,)))
    assert [name for name, _ in crossed] == ["alert"]
    assert "filament runout" in crossed[0][1]
    assert detect_milestones(status(layer=11, alerts=(alert,)), status(layer=12, alerts=(alert,))) == []


def test_the_first_observation_only_reports_the_start():
    """With nothing to compare against, a crossing cannot be claimed."""
    crossed = detect_milestones(None, status(layer=40))
    assert [name for name, _ in crossed] == ["start"]


# -- the watcher ------------------------------------------------------------
@pytest.fixture
def monitor(tmp_path):
    store = Store(tmp_path / "mhs.sqlite3")
    state = {"status": status(PrintState.IDLE, layer=None)}
    frames = {"count": 0}

    async def status_fn():
        return state["status"]

    async def snapshot_fn():
        frames["count"] += 1
        return b"\xff\xd8jpeg"

    watcher = PrintMonitor(
        store=store,
        device_id="mock",
        status_fn=status_fn,
        snapshot_fn=snapshot_fn,
        capture_path_fn=lambda label: tmp_path / f"{label}-{frames['count']}.jpg",
        run_id=store.start_run("mock", job_name="benchy"),
        poll_interval=0.01,
    )
    yield watcher, state, frames
    store.close()


async def test_a_tick_records_a_milestone_with_its_frame(monitor):
    watcher, state, frames = monitor
    await watcher.tick()  # idle: establishes the baseline
    state["status"] = status(PrintState.RUNNING, layer=0)
    recorded = await watcher.tick()

    assert [r["milestone"] for r in recorded] == ["start"]
    assert frames["count"] == 1
    saved = watcher.store.list_observations(run_id=watcher.run_id)
    assert saved[0]["kind"] == "stage:start"
    assert saved[0]["image_path"] and saved[0]["caption"] is None


async def test_each_milestone_is_recorded_once_however_often_it_polls(monitor):
    watcher, state, _ = monitor
    state["status"] = status(PrintState.RUNNING, layer=1)
    await watcher.tick()
    state["status"] = status(PrintState.RUNNING, layer=2)
    assert [r["milestone"] for r in await watcher.tick()] == ["first_layer"]
    for layer in (2, 2, 3):  # the printer keeps reporting; we do not re-record
        state["status"] = status(PrintState.RUNNING, layer=layer)
        assert await watcher.tick() == []
    assert watcher.captured == ["first_layer", "start"]


async def test_two_different_faults_are_both_captured(monitor):
    watcher, state, _ = monitor
    first = DeviceAlert(code="0300-8000", message="runout")
    second = DeviceAlert(code="0700-2000", message="bed temperature")
    state["status"] = status(layer=10)
    await watcher.tick()
    state["status"] = status(layer=11, alerts=(first,))
    assert len(await watcher.tick()) == 1
    state["status"] = status(layer=12, alerts=(first, second))
    recorded = await watcher.tick()
    assert [r["milestone"] for r in recorded] == ["alert"]
    assert "bed temperature" in recorded[0]["detail"]


async def test_only_the_requested_stages_are_recorded(monitor):
    watcher, state, frames = monitor
    watcher.watch = ("first_layer",)
    state["status"] = status(PrintState.RUNNING, layer=1)
    assert await watcher.tick() == []  # the start is crossed, but not watched
    state["status"] = status(PrintState.RUNNING, layer=2)
    assert [r["milestone"] for r in await watcher.tick()] == ["first_layer"]
    assert frames["count"] == 1


async def test_a_camera_failure_still_records_the_milestone(monitor):
    """A dark camera must not cost us the record that the stage happened."""
    from mhs.errors import ConnectionFailed

    watcher, state, _ = monitor

    async def broken():
        raise ConnectionFailed("LAN Mode Liveview is off")

    watcher.snapshot_fn = broken
    state["status"] = status(PrintState.RUNNING, layer=0)
    recorded = await watcher.tick()
    assert recorded[0]["milestone"] == "start" and recorded[0]["image_path"] is None


async def test_start_and_stop_run_the_loop(monitor):
    import asyncio

    watcher, state, _ = monitor
    state["status"] = status(PrintState.RUNNING, layer=2)
    await watcher.start()
    assert watcher.running
    for _ in range(50):
        await asyncio.sleep(0.01)
        if watcher.captured:
            break
    await watcher.stop()
    assert not watcher.running
    assert "start" in watcher.captured


# -- the report -------------------------------------------------------------
def make_run(**overrides) -> dict:
    now = time.time()
    run = {
        "id": 7,
        "device_id": "mock",
        "job_name": "benchy",
        "remote_path": "cache/benchy.3mf",
        "outcome": "success",
        "quality_score": 8,
        "notes": None,
        "settings": {"estimated_time_minutes": 60},
        "started_at": now - 3600,
        "ended_at": now,
        "started_at_iso": "2026-09-19T08:00:00+00:00",
        "ended_at_iso": "2026-09-19T09:00:00+00:00",
    }
    run.update(overrides)
    return run


def observation(kind: str, at: float, **extra) -> dict:
    row = {"id": 1, "kind": kind, "created_at": at, "layer": 1, "text": "detail",
           "caption": None, "image_path": None}
    row.update(extra)
    return row


def test_report_lays_the_run_out_in_order():
    now = time.time()
    report = build_report(make_run(), [
        observation("stage:first_layer", now - 10, id=2, image_path="/frames/b.jpg"),
        observation("stage:start", now - 20, id=1, image_path="/frames/a.jpg", caption="plate clear"),
    ])
    assert [row["milestone"] for row in report["timeline"]] == ["start", "first_layer"]
    assert report["images"] == ["/frames/a.jpg", "/frames/b.jpg"]
    assert report["duration_minutes"] == 60


def test_report_names_the_frames_still_waiting_for_a_caption():
    report = build_report(make_run(), [
        observation("stage:start", 1.0, id=1, image_path="/frames/a.jpg", caption="plate clear"),
        observation("stage:first_layer", 2.0, id=2, image_path="/frames/b.jpg"),
    ])
    assert [row["observation_id"] for row in report["uncaptioned_images"]] == [2]
    assert "1 awaiting a caption" in report["summary"]


def test_report_compares_the_time_against_the_slicer_estimate():
    report = build_report(make_run(), [])
    assert report["time_vs_estimate"]["drift_percent"] == 0.0
    assert report["time_vs_estimate"]["note"] == "close to the slicer's estimate"


def test_report_calls_out_a_print_that_ran_long():
    run = make_run(settings={"estimated_time_minutes": 30})
    drift = build_report(run, [])["time_vs_estimate"]
    assert drift["drift_percent"] == 100.0
    assert drift["note"] == "over the estimate by 100%"


def test_report_reads_the_estimate_out_of_a_nested_slice_record():
    run = make_run(settings={"slice": {"estimated_time_minutes": 60}})
    assert build_report(run, [])["predicted_minutes"] == 60


def test_report_says_which_key_frames_were_never_taken():
    report = build_report(make_run(), [observation("stage:start", 1.0)])
    assert report["missing_milestones"] == ["first_layer", "finished"]
    assert "no frame for: first_layer, finished" in report["summary"]


def test_report_survives_a_run_that_never_ended():
    report = build_report(make_run(ended_at=None, outcome="in_progress"), [])
    assert report["duration_minutes"] is None
    assert "time_vs_estimate" not in report


def test_stage_name_strips_the_prefix_and_leaves_other_kinds_alone():
    assert stage_name("stage:first_layer") == "first_layer"
    assert stage_name("snapshot") == "snapshot"


# -- through the app and the MCP surface ------------------------------------
@pytest.fixture
def server(settings):
    srv = create_server(settings, run_scheduler=False)
    yield srv
    srv.mhs_app.store.close()


async def call(server, name, **arguments) -> dict:
    return tool_body(await server.call_tool(name, arguments))


async def test_watch_print_reports_what_it_is_watching(server):
    body = await call(server, "watch_print", poll_seconds=60)
    assert body["ok"] is True and body["camera"] is True
    assert body["printer"] == "mock" and body["poll_seconds"] == 60
    assert await call(server, "watch_status") == {"ok": True, "watching": [
        {"printer": "mock", "run_id": None, "running": True,
         "poll_seconds": 60, "captured": []}
    ]}
    assert (await call(server, "stop_watching_print"))["watching"] is False


async def test_watch_print_rejects_an_unknown_stage(server):
    body = await call(server, "watch_print", stages=["first_layer", "cooldown"])
    assert body["ok"] is False and "cooldown" in body["message"]


async def test_watch_print_rejects_an_absurd_poll_interval(server):
    assert (await call(server, "watch_print", poll_seconds=0.1))["ok"] is False


async def test_a_second_watch_replaces_the_first(server):
    await call(server, "watch_print")
    before = server.mhs_app.monitors["mock"]
    await call(server, "watch_print")
    assert server.mhs_app.monitors["mock"] is not before
    assert not before.running
    await call(server, "stop_watching_print")


async def test_stop_watching_when_nothing_is_watched_is_not_an_error(server):
    body = await call(server, "stop_watching_print")
    assert body["ok"] is True and body["watching"] is False


async def test_list_print_stages_explains_why_each_frame_matters(server):
    body = await call(server, "list_print_stages")
    names = {s["name"] for s in body["stages"]}
    assert {"start", "first_layer", "finished", "failed"} <= names
    assert all(s["why"] for s in body["stages"])


async def test_the_whole_loop_from_a_print_to_its_report(server):
    app: MHSApp = server.mhs_app
    printer = await app.printer()
    run_id = app.store.start_run("mock", job_name="benchy",
                                 settings={"estimated_time_minutes": 1})
    monitor = await app.watch_print(run_id=run_id, poll_interval=3600)
    await printer.start_print("cache/benchy.3mf", PrintOptions())

    # Drive the mock's clock rather than waiting a real minute for it.
    base = printer.now()
    for offset in (0.0, 1.0, printer.print_duration + 1):
        printer.now = lambda offset=offset: base + offset
        await monitor.tick()
    await app.stop_watching("mock")
    assert monitor.captured == ["finished", "first_layer", "half", "quarter",
                                "start", "three_quarters"]

    listed = await call(server, "review_print_stages", run_id=run_id)
    assert listed["ok"] is True and listed["uncaptioned"]
    first_layer = next(s for s in listed["stages"] if s["milestone"] == "first_layer")

    captioned = await call(server, "caption_print_stage",
                           observation_id=first_layer["observation_id"],
                           caption="first layer down flat, no lifted corners")
    assert captioned["observation"]["caption"] == "first layer down flat, no lifted corners"

    app.store.finish_run(run_id, "success", quality_score=9)
    report = (await call(server, "print_report", run_id=run_id))["report"]
    assert report["outcome"] == "success" and report["quality_score"] == 9
    captions = [row["caption"] for row in report["timeline"] if row["caption"]]
    assert captions == ["first layer down flat, no lifted corners"]
    assert "first_layer" not in report["missing_milestones"]


async def test_caption_rejects_an_empty_string(server):
    assert (await call(server, "caption_print_stage", observation_id=1, caption="  "))["ok"] is False


async def test_caption_and_report_say_so_when_the_id_is_unknown(server):
    assert (await call(server, "caption_print_stage", observation_id=999,
                       caption="x"))["error"] == "NotFound"
    assert (await call(server, "print_report", run_id=999))["error"] == "NotFound"


async def test_watching_a_vacuum_is_refused(vacuum_settings):
    srv = create_server(vacuum_settings, run_scheduler=False)
    try:
        body = tool_body(await srv.call_tool("watch_print", {}))
        assert body["ok"] is False and "not a 3D printer" in body["message"]
    finally:
        srv.mhs_app.store.close()
