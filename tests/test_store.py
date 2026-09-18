from __future__ import annotations

import time

import pytest

from mhs.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "mhs.sqlite3")
    yield s
    s.close()


def test_job_lifecycle(store):
    job = store.add_job("a1", "cache/x.3mf", time.time() + 60, options={"plate": 2}, window_minutes=30)
    assert job.status == "pending"
    assert job.options == {"plate": 2}
    assert job.deadline == pytest.approx(job.start_at + 1800)

    assert store.due_jobs(now=job.start_at - 1) == []
    assert [j.id for j in store.due_jobs(now=job.start_at + 1)] == [job.id]

    store.update_job(job.id, bump_attempts=True, last_error="printer busy")
    reloaded = store.get_job(job.id)
    assert reloaded.attempts == 1 and reloaded.last_error == "printer busy"

    store.update_job(job.id, status="started", fired=True)
    assert store.get_job(job.id).fired_at is not None
    assert store.due_jobs(now=time.time() + 3600) == []


def test_cancel_only_affects_pending_jobs(store):
    job = store.add_job("a1", "cache/x.3mf", time.time())
    assert store.cancel_job(job.id).status == "cancelled"
    store.update_job(job.id, status="started")
    assert store.cancel_job(job.id).status == "started"
    assert store.cancel_job("job_missing") is None


def test_unknown_status_rejected(store):
    job = store.add_job("a1", "cache/x.3mf", time.time())
    with pytest.raises(ValueError):
        store.update_job(job.id, status="exploded")


def test_listing_filters(store):
    store.add_job("a1", "one.3mf", time.time())
    other = store.add_job("p1s", "two.3mf", time.time())
    store.update_job(other.id, status="done")
    assert [j.remote_path for j in store.list_jobs(printer_id="a1")] == ["one.3mf"]
    assert [j.remote_path for j in store.list_jobs(status="done")] == ["two.3mf"]


def test_print_journal_round_trip(store):
    run_id = store.start_run("a1", job_name="bracket", remote_path="cache/b.3mf",
                             settings={"flow_calibration": True})
    store.add_observation("a1", kind="snapshot", run_id=run_id, layer=12, image_path="/tmp/f.jpg")
    store.add_observation("a1", kind="note", run_id=run_id, text="slight stringing")
    store.finish_run(run_id, "partial", quality_score=6, notes="corners lifted")

    run = store.get_run(run_id)
    assert run["outcome"] == "partial" and run["quality_score"] == 6
    assert run["settings"] == {"flow_calibration": True}
    assert run["ended_at"] is not None and run["started_at_iso"]
    assert {o["kind"] for o in run["observations"]} == {"snapshot", "note"}

    assert [r["id"] for r in store.list_runs("a1")] == [run_id]
    assert store.list_runs("other") == []
    assert len(store.list_observations(run_id=run_id)) == 2


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "mhs.sqlite3"
    first = Store(path)
    job = first.add_job("a1", "cache/x.3mf", time.time())
    first.close()

    second = Store(path)
    assert second.get_job(job.id).remote_path == "cache/x.3mf"
    second.close()
