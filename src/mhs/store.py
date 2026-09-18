"""Durable state: the scheduled-print queue and the print journal.

SQLite, because the two things that must survive a restart are small and
relational: jobs that have not fired yet, and the history an agent needs to tell
whether last week's settings actually printed better than today's.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id              TEXT PRIMARY KEY,
    printer_id      TEXT NOT NULL,
    remote_path     TEXT NOT NULL,
    options_json    TEXT NOT NULL DEFAULT '{}',
    start_at        REAL NOT NULL,
    window_minutes  INTEGER NOT NULL DEFAULT 60,
    status          TEXT NOT NULL DEFAULT 'pending',
    note            TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    fired_at        REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON scheduled_jobs (status, start_at);

CREATE TABLE IF NOT EXISTS print_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id    TEXT NOT NULL,
    job_name      TEXT,
    remote_path   TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    outcome       TEXT NOT NULL DEFAULT 'in_progress',
    quality_score INTEGER,
    notes         TEXT,
    started_at    REAL NOT NULL,
    ended_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_printer ON print_runs (printer_id, started_at);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES print_runs (id) ON DELETE CASCADE,
    printer_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    layer       INTEGER,
    text        TEXT,
    image_path  TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations (run_id, created_at);

CREATE TABLE IF NOT EXISTS calibrations (
    printer_id  TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""

JOB_STATUSES = ("pending", "started", "done", "failed", "cancelled", "missed")


@dataclass
class ScheduledJob:
    id: str
    printer_id: str
    remote_path: str
    options: dict
    start_at: float
    window_minutes: int
    status: str
    note: str | None
    attempts: int
    last_error: str | None
    fired_at: float | None
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ScheduledJob:
        return cls(
            id=row["id"],
            printer_id=row["printer_id"],
            remote_path=row["remote_path"],
            options=json.loads(row["options_json"] or "{}"),
            start_at=row["start_at"],
            window_minutes=row["window_minutes"],
            status=row["status"],
            note=row["note"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            fired_at=row["fired_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def deadline(self) -> float:
        return self.start_at + self.window_minutes * 60

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["start_at_iso"] = iso(self.start_at)
        data["deadline_iso"] = iso(self.deadline)
        return data


def iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    """Thread-safe (serialised) SQLite wrapper. All methods are blocking."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # -- scheduled jobs ----------------------------------------------------
    def add_job(
        self,
        printer_id: str,
        remote_path: str,
        start_at: float,
        *,
        options: dict | None = None,
        window_minutes: int = 60,
        note: str | None = None,
    ) -> ScheduledJob:
        now = time.time()
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        self._execute(
            "INSERT INTO scheduled_jobs (id, printer_id, remote_path, options_json, start_at, "
            "window_minutes, status, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                job_id,
                printer_id,
                remote_path,
                json.dumps(options or {}),
                start_at,
                window_minutes,
                note,
                now,
                now,
            ),
        )
        job = self.get_job(job_id)
        assert job is not None
        return job

    def get_job(self, job_id: str) -> ScheduledJob | None:
        rows = self._query("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,))
        return ScheduledJob.from_row(rows[0]) if rows else None

    def list_jobs(
        self, printer_id: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[ScheduledJob]:
        sql = "SELECT * FROM scheduled_jobs WHERE 1=1"
        params: list[Any] = []
        if printer_id:
            sql += " AND printer_id = ?"
            params.append(printer_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY start_at ASC LIMIT ?"
        params.append(limit)
        return [ScheduledJob.from_row(r) for r in self._query(sql, tuple(params))]

    def due_jobs(self, now: float | None = None) -> list[ScheduledJob]:
        now = time.time() if now is None else now
        rows = self._query(
            "SELECT * FROM scheduled_jobs WHERE status = 'pending' AND start_at <= ? ORDER BY start_at",
            (now,),
        )
        return [ScheduledJob.from_row(r) for r in rows]

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        last_error: str | None = None,
        bump_attempts: bool = False,
        fired: bool = False,
    ) -> ScheduledJob | None:
        if status is not None and status not in JOB_STATUSES:
            raise ValueError(f"unknown job status {status!r}")
        sets = ["updated_at = ?"]
        params: list[Any] = [time.time()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        if bump_attempts:
            sets.append("attempts = attempts + 1")
        if fired:
            sets.append("fired_at = ?")
            params.append(time.time())
        params.append(job_id)
        self._execute(f"UPDATE scheduled_jobs SET {', '.join(sets)} WHERE id = ?", tuple(params))
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> ScheduledJob | None:
        job = self.get_job(job_id)
        if job is None or job.status != "pending":
            return job
        return self.update_job(job_id, status="cancelled")

    # -- print journal -----------------------------------------------------
    def start_run(
        self,
        printer_id: str,
        *,
        job_name: str | None = None,
        remote_path: str | None = None,
        settings: dict | None = None,
    ) -> int:
        cur = self._execute(
            "INSERT INTO print_runs (printer_id, job_name, remote_path, settings_json, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (printer_id, job_name, remote_path, json.dumps(settings or {}), time.time()),
        )
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        outcome: str,
        *,
        quality_score: int | None = None,
        notes: str | None = None,
    ) -> dict | None:
        self._execute(
            "UPDATE print_runs SET outcome = ?, quality_score = ?, notes = ?, ended_at = ? WHERE id = ?",
            (outcome, quality_score, notes, time.time(), run_id),
        )
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> dict | None:
        rows = self._query("SELECT * FROM print_runs WHERE id = ?", (run_id,))
        if not rows:
            return None
        run = _run_to_dict(rows[0])
        run["observations"] = [
            dict(r) for r in self._query("SELECT * FROM observations WHERE run_id = ? ORDER BY id", (run_id,))
        ]
        return run

    def list_runs(self, printer_id: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM print_runs"
        params: list[Any] = []
        if printer_id:
            sql += " WHERE printer_id = ?"
            params.append(printer_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return [_run_to_dict(r) for r in self._query(sql, tuple(params))]

    def add_observation(
        self,
        printer_id: str,
        kind: str,
        *,
        run_id: int | None = None,
        layer: int | None = None,
        text: str | None = None,
        image_path: str | None = None,
    ) -> int:
        cur = self._execute(
            "INSERT INTO observations (run_id, printer_id, kind, layer, text, image_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, printer_id, kind, layer, text, image_path, time.time()),
        )
        return int(cur.lastrowid)

    def list_observations(self, run_id: int | None = None, printer_id: str | None = None, limit: int = 50):
        sql = "SELECT * FROM observations WHERE 1=1"
        params: list[Any] = []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if printer_id:
            sql += " AND printer_id = ?"
            params.append(printer_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._query(sql, tuple(params))]


    # -- camera scale calibration -----------------------------------------
    def save_calibration(self, printer_id: str, payload: dict) -> None:
        """Store the current mm-per-pixel for a printer's camera.

        One per printer: it is only valid for the camera's fixed position, so a
        newer reading always supersedes the old one.
        """
        self._execute(
            "INSERT INTO calibrations (printer_id, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(printer_id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (printer_id, json.dumps(payload), time.time()),
        )

    def get_calibration(self, printer_id: str) -> dict | None:
        rows = self._query("SELECT payload FROM calibrations WHERE printer_id = ?", (printer_id,))
        return json.loads(rows[0]["payload"]) if rows else None

    def clear_calibration(self, printer_id: str) -> None:
        self._execute("DELETE FROM calibrations WHERE printer_id = ?", (printer_id,))

    def latest_snapshot(self, printer_id: str) -> str | None:
        """Path of the most recent camera frame saved for this printer.

        Measurements must run against the frame the coordinates were read from,
        so this is what `camera_measure` defaults to.
        """
        rows = self._query(
            "SELECT image_path FROM observations WHERE printer_id = ? AND image_path IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (printer_id,),
        )
        return rows[0]["image_path"] if rows else None


def _run_to_dict(row: sqlite3.Row) -> dict:
    run = dict(row)
    run["settings"] = json.loads(run.pop("settings_json") or "{}")
    run["started_at_iso"] = iso(run["started_at"])
    run["ended_at_iso"] = iso(run["ended_at"])
    return run
