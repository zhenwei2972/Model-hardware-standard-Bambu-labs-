"""Deferred prints: the unattended path, so the guard rails are what matter."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from mhs.errors import ConfigError
from mhs.models import PrintState
from mhs.scheduler import PrintScheduler, parse_when
from mhs.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


@pytest.fixture
def scheduler(store, printer) -> PrintScheduler:
    async def factory(_printer_id: str):
        return printer

    return PrintScheduler(store, factory, poll_interval=0.01)


# -- time parsing -----------------------------------------------------------
def test_parse_relative_offsets():
    now = 1_000_000.0
    assert parse_when("+2h", now=now) == now + 7200
    assert parse_when("in 90m", now=now) == now + 5400
    assert parse_when("3 days", now=now) == now + 3 * 86400


def test_parse_iso_with_and_without_timezone():
    assert parse_when("2026-09-19T06:30:00+00:00") == datetime.fromisoformat(
        "2026-09-19T06:30:00+00:00"
    ).timestamp()
    naive = parse_when("2026-09-19T06:30")
    assert naive == datetime(2026, 9, 19, 6, 30).astimezone().timestamp()


@pytest.mark.parametrize("value", ["", "tomorrow morning", "6pm-ish"])
def test_parse_rejects_nonsense(value):
    with pytest.raises(ConfigError):
        parse_when(value)


# -- firing rules -----------------------------------------------------------
async def test_due_job_starts_on_an_idle_printer(scheduler, store, printer):
    job = store.add_job("mock", "cache/benchy.3mf", time.time() - 1)
    [result] = await scheduler.tick()

    assert result["action"] == "started"
    assert store.get_job(job.id).status == "started"
    assert (await printer.status()).state is PrintState.RUNNING
    run = store.get_run(result["run_id"])
    assert run["settings"]["scheduled_job"] == job.id


async def test_future_job_is_left_alone(scheduler, store, printer):
    job = store.add_job("mock", "cache/benchy.3mf", time.time() + 3600)
    assert await scheduler.tick() == []
    assert store.get_job(job.id).status == "pending"
    assert (await printer.status()).state is PrintState.IDLE


async def test_busy_printer_causes_a_retry_not_a_failure(scheduler, store, printer):
    await printer.start_print("cache/benchy.3mf")
    job = store.add_job("mock", "cache/benchy.3mf", time.time() - 1)

    [result] = await scheduler.tick()
    assert result["action"] == "retry" and result["reason"] == "running"
    reloaded = store.get_job(job.id)
    assert reloaded.status == "pending" and reloaded.attempts == 1

    await printer.stop_print()
    [result] = await scheduler.tick()
    assert result["action"] == "started"


async def test_job_expires_instead_of_printing_hours_late(scheduler, store):
    job = store.add_job("mock", "cache/benchy.3mf", time.time() - 7200, window_minutes=30)
    [result] = await scheduler.tick()
    assert result["action"] == "missed"
    assert store.get_job(job.id).status == "missed"
    assert "window" in store.get_job(job.id).last_error


async def test_serious_alerts_block_the_job(scheduler, store, printer):
    from mhs.models import DeviceAlert

    printer.alerts = [DeviceAlert(code="HMS_0300_0100_0002_0001", severity="serious")]
    job = store.add_job("mock", "cache/benchy.3mf", time.time() - 1)
    [result] = await scheduler.tick()
    assert result["action"] == "retry"
    assert store.get_job(job.id).status == "pending"


async def test_offline_printer_retries(scheduler, store, printer):
    await printer.disconnect()
    store.add_job("mock", "cache/benchy.3mf", time.time() - 1)
    [result] = await scheduler.tick()
    assert result == {"job_id": result["job_id"], "action": "retry", "reason": "offline"}


async def test_start_failure_marks_the_job_failed(scheduler, store, printer):
    printer.fail_next = "start_print"
    job = store.add_job("mock", "cache/benchy.3mf", time.time() - 1)
    [result] = await scheduler.tick()
    assert result["action"] == "failed"
    assert store.get_job(job.id).status == "failed"


async def test_read_only_scheduler_never_prints(store, printer):
    async def factory(_pid):
        return printer

    scheduler = PrintScheduler(store, factory, read_only=True)
    store.add_job("mock", "cache/benchy.3mf", time.time() - 1)
    [result] = await scheduler.tick()
    assert result["reason"] == "read_only"
    assert (await printer.status()).state is PrintState.IDLE


async def test_cancelled_jobs_are_not_due(scheduler, store):
    job = store.add_job("mock", "cache/benchy.3mf", time.time() - 1)
    store.cancel_job(job.id)
    assert await scheduler.tick() == []


async def test_options_survive_the_round_trip(scheduler, store, printer):
    store.add_job(
        "mock",
        "cache/benchy.3mf",
        time.time() - 1,
        options={"plate": 3, "use_ams": True, "job_name": "night run"},
    )
    await scheduler.tick()
    name, payload = printer.commands[-1]
    assert name == "start_print"
    assert payload["plate"] == 3 and payload["use_ams"] is True and payload["job_name"] == "night run"


async def test_background_loop_starts_and_stops(scheduler, store, printer):
    store.add_job("mock", "cache/benchy.3mf", time.time() - 1)
    await scheduler.start()
    deadline = time.monotonic() + 2
    while store.list_jobs()[0].status == "pending" and time.monotonic() < deadline:
        import asyncio

        await asyncio.sleep(0.01)
    await scheduler.stop()
    assert store.list_jobs()[0].status == "started"


def test_deadline_uses_the_window(store):
    job = store.add_job("mock", "f.3mf", 1_000_000.0, window_minutes=45)
    assert job.deadline == 1_000_000.0 + 45 * 60
    assert timedelta(minutes=45).total_seconds() == 2700
