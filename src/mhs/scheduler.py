"""Deferred prints.

"Start this at 6am" is the request that makes an agent genuinely useful for a
printer, and it is also the one most likely to go wrong unattended. The rules
here are deliberately conservative:

* a job only fires while the printer is idle and free of serious alerts;
* if the printer is busy, the job keeps retrying until its window expires and is
  then marked ``missed`` - it never starts a print hours late;
* everything is journalled, so a restart resumes the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from .errors import ConfigError, MHSError
from .models import PrintOptions
from .printer import Printer
from .store import ScheduledJob, Store

log = logging.getLogger(__name__)

_RELATIVE = re.compile(r"^\s*(?:in\s+)?\+?(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hours|d|days)\s*$", re.I)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def parse_when(when: str, *, now: float | None = None) -> float:
    """Parse a schedule time into an epoch timestamp.

    Accepts ISO-8601 (``2026-09-19T06:30``, with or without offset; naive values
    are read in the host's local timezone) and relative forms (``in 90m``,
    ``+2h``, ``3 days``).
    """
    now = time.time() if now is None else now
    text = when.strip()
    if not text:
        raise ConfigError("empty schedule time")

    match = _RELATIVE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()[0]
        return now + amount * _UNIT_SECONDS[unit]

    normalised = text.replace("Z", "+00:00").replace(" ", "T", 1) if " " in text else text
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        raise ConfigError(
            f"could not read {when!r} as a time",
            hint="Use ISO-8601 (2026-09-19T06:30) or a relative offset (in 90m, +2h).",
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # interpret naive input as local time
    return parsed.timestamp()


def describe_delay(seconds: float) -> str:
    return str(timedelta(seconds=int(max(0, seconds))))


class PrintScheduler:
    """Polls the store and starts jobs whose time has come."""

    def __init__(
        self,
        store: Store,
        printer_factory: Callable[[str], Awaitable[Printer]],
        *,
        poll_interval: float = 15.0,
        read_only: bool = False,
        on_started: Callable[[ScheduledJob, dict], None] | None = None,
    ) -> None:
        self.store = store
        self.printer_factory = printer_factory
        self.poll_interval = poll_interval
        self.read_only = read_only
        self.on_started = on_started
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="mhs-scheduler")
            log.info("scheduler started (poll every %.0fs)", self.poll_interval)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover - the loop must outlive failures
                log.exception("scheduler tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)

    # -- work --------------------------------------------------------------
    async def tick(self, now: float | None = None) -> list[dict]:
        """Process every due job once. Returns what happened, for tests/logs."""
        now = time.time() if now is None else now
        results = []
        for job in self.store.due_jobs(now):
            results.append(await self._fire(job, now))
        return results

    async def _fire(self, job: ScheduledJob, now: float) -> dict:
        if now > job.deadline:
            self.store.update_job(
                job.id,
                status="missed",
                last_error=f"window of {job.window_minutes} min expired before the printer was free",
            )
            log.warning("job %s missed its window", job.id)
            return {"job_id": job.id, "action": "missed"}

        if self.read_only:
            self.store.update_job(job.id, bump_attempts=True, last_error="server is read-only")
            return {"job_id": job.id, "action": "skipped", "reason": "read_only"}

        try:
            printer = await self.printer_factory(job.device_id)
            status = await printer.status()
        except MHSError as exc:
            self.store.update_job(job.id, bump_attempts=True, last_error=exc.message)
            return {"job_id": job.id, "action": "retry", "reason": exc.message}

        if not status.online:
            self.store.update_job(job.id, bump_attempts=True, last_error="printer offline")
            return {"job_id": job.id, "action": "retry", "reason": "offline"}
        if not status.state.accepts_new_job:
            self.store.update_job(
                job.id, bump_attempts=True, last_error=f"printer is {status.state.value}"
            )
            return {"job_id": job.id, "action": "retry", "reason": status.state.value}
        blocking = [a for a in status.alerts if a.severity in {"fatal", "serious"}]
        if blocking:
            self.store.update_job(job.id, bump_attempts=True, last_error=f"alert {blocking[0].code}")
            return {"job_id": job.id, "action": "retry", "reason": blocking[0].code}

        options = PrintOptions(**job.options) if job.options else PrintOptions()
        try:
            result = await printer.start_print(job.remote_path, options)
        except MHSError as exc:
            self.store.update_job(job.id, status="failed", bump_attempts=True, last_error=exc.message)
            log.error("job %s failed to start: %s", job.id, exc.message)
            return {"job_id": job.id, "action": "failed", "reason": exc.message}

        if result.get("acknowledged") is False:
            # Command went out but the printer never answered: usually Developer
            # Mode being off. Keep retrying inside the window rather than lying.
            self.store.update_job(job.id, bump_attempts=True, last_error="no acknowledgement from printer")
            return {"job_id": job.id, "action": "retry", "reason": "unacknowledged"}

        run_id = self.store.start_run(
            job.device_id,
            job_name=options.job_name or job.remote_path,
            remote_path=job.remote_path,
            settings={"scheduled_job": job.id, **job.options},
        )
        self.store.update_job(job.id, status="started", bump_attempts=True, fired=True)
        log.info("job %s started print %s (run %s)", job.id, job.remote_path, run_id)
        if self.on_started:
            self.on_started(job, result)
        return {"job_id": job.id, "action": "started", "run_id": run_id, "result": result}
