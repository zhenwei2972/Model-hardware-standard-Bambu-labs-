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
    assert [j.remote_path for j in store.list_jobs(device_id="a1")] == ["one.3mf"]
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


def test_record_run_settings_merges_without_losing_what_was_there(store):
    """The slice estimate arrives after the run row; both have to survive."""
    run_id = store.start_run("mock", job_name="benchy", settings={"plate": 1})
    store.record_run_settings(run_id, {"intent": "quality",
                                       "slice": {"estimated_time_minutes": 54}})
    settings = store.get_run(run_id)["settings"]
    assert settings == {"plate": 1, "intent": "quality",
                        "slice": {"estimated_time_minutes": 54}}
    assert store.record_run_settings(999, {"x": 1}) is None


def test_captioning_an_observation_leaves_the_rest_alone(store):
    obs = store.add_observation("mock", kind="stage:first_layer", layer=2, text="layer 1 done")
    assert store.get_observation(obs)["caption"] is None
    row = store.caption_observation(obs, "flat and well squished")
    assert row["caption"] == "flat and well squished"
    assert row["text"] == "layer 1 done" and row["layer"] == 2
    assert store.caption_observation(999, "nothing there") is None


def test_a_v01_database_gains_the_caption_column_without_losing_rows(tmp_path):
    """v0.1 had no captions and called the column printer_id; both are migrated."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE observations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, printer_id TEXT NOT NULL,"
        " kind TEXT NOT NULL, layer INTEGER, text TEXT, image_path TEXT,"
        " created_at REAL NOT NULL);"
        "INSERT INTO observations (printer_id, kind, text, created_at)"
        " VALUES ('a1mini', 'snapshot', 'from the old schema', 1.0);"
    )
    old.commit()
    old.close()

    store = Store(path)
    kept = store.list_observations()[0]
    assert kept["device_id"] == "a1mini" and kept["text"] == "from the old schema"
    assert kept["caption"] is None
    assert store.caption_observation(kept["id"], "read later")["caption"] == "read later"
    store.close()
