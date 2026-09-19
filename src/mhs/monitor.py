"""Watching a print through its stages, and reporting on how it went.

A print is not one event, it is a sequence: the first layer goes down, a
quarter passes, it finishes or it fails. The moments worth a photograph are
few and predictable, and the first layer is worth more than all the rest put
together - most failures are visible there and nowhere else.

This watches the status stream, captures a frame when a milestone is crossed,
and writes each one into the print journal. It does not caption them: reading
a photograph is the model's job, and :func:`build_report` lays the run out so
that it can.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .errors import MHSError
from .models import PrinterStatus, PrintState
from .store import Store, iso

log = logging.getLogger(__name__)

#: How often to look. A print is slow; polling faster only costs the MCU.
DEFAULT_POLL_S = 20.0


@dataclass(frozen=True)
class Milestone:
    """A moment in a print worth recording."""

    name: str
    description: str
    #: Why a photograph here is worth having.
    why: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "why": self.why}


MILESTONES: dict[str, Milestone] = {
    "start": Milestone("start", "The job began.",
                       "Confirms the plate was clear and the right file started."),
    "first_layer": Milestone(
        "first_layer", "The first layer finished.",
        "The single most informative frame: adhesion, squish, warping and a shifted "
        "origin are all visible here, and nowhere else so cheaply.",
    ),
    "quarter": Milestone("quarter", "A quarter of the layers are down.",
                         "Early enough that stopping still saves most of the filament."),
    "half": Milestone("half", "Halfway.", "Catches layer shift and the start of stringing."),
    "three_quarters": Milestone("three_quarters", "Three quarters done.",
                                "Overhangs and top surfaces are usually underway."),
    "finished": Milestone("finished", "The print completed.",
                          "The result, before the plate is touched."),
    "failed": Milestone("failed", "The print failed or was stopped.",
                        "Whatever state it ended in, for diagnosis."),
    "paused": Milestone("paused", "The print paused.",
                        "Often a filament runout or a detected defect."),
    "alert": Milestone("alert", "The printer raised a fault.",
                       "Pairs the machine's own error code with what it looks like."),
}

#: Progress-based milestones, as a fraction of total layers.
_PROGRESS_POINTS = (("quarter", 0.25), ("half", 0.5), ("three_quarters", 0.75))


def detect_milestones(
    previous: PrinterStatus | None, current: PrinterStatus
) -> list[tuple[str, str]]:
    """Which milestones were crossed between two observations.

    Returns ``(milestone name, detail)`` pairs. Crossings are computed from the
    transition rather than the current value, so a milestone fires once even
    though the state that triggered it persists for many polls.
    """
    crossed: list[tuple[str, str]] = []
    was_active = previous is not None and previous.state.is_active

    if current.state.is_active and not was_active:
        crossed.append(("start", f"{current.job_name or 'job'} started"))

    # The first layer is done the moment the printer reports it is on layer 2.
    previous_layer = (previous.current_layer or 0) if previous else 0
    layer = current.current_layer or 0
    if previous is not None and previous_layer <= 1 < layer:
        crossed.append(("first_layer", f"layer 1 complete, now on layer {layer}"))

    total = current.total_layers or 0
    if previous is not None and total > 0:
        before = previous_layer / total
        after = layer / total
        for name, point in _PROGRESS_POINTS:
            if before < point <= after:
                crossed.append((name, f"layer {layer} of {total}"))

    if previous is not None and current.state is not previous.state:
        if current.state is PrintState.FINISHED:
            crossed.append(("finished", "the printer reports the job finished"))
        elif current.state is PrintState.FAILED:
            crossed.append(("failed", "the printer reports the job failed"))
        elif current.state is PrintState.PAUSED:
            crossed.append(("paused", f"paused at layer {layer}"))

    previous_codes = {a.code for a in previous.alerts} if previous else set()
    for alert in current.alerts:
        if alert.code not in previous_codes:
            crossed.append(("alert", f"{alert.code}: {alert.message or 'no description'}"))
    return crossed


@dataclass
class PrintMonitor:
    """Polls one printer and records a frame at each milestone."""

    store: Store
    device_id: str
    status_fn: Callable[[], Awaitable[PrinterStatus]]
    snapshot_fn: Callable[[], Awaitable[bytes]] | None
    capture_path_fn: Callable[[str], object]
    run_id: int | None = None
    poll_interval: float = DEFAULT_POLL_S
    #: Milestones to act on; the default is everything.
    watch: tuple[str, ...] = tuple(MILESTONES)
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _stopping: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _previous: PrinterStatus | None = field(default=None, init=False, repr=False)
    _seen: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def captured(self) -> list[str]:
        """Milestones already recorded, so none is photographed twice."""
        return sorted(self._seen)

    async def start(self) -> None:
        if not self.running:
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name=f"mhs-monitor-{self.device_id}")
            log.info("watching %s every %.0fs", self.device_id, self.poll_interval)

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
            except Exception:  # pragma: no cover - a watcher must outlive a bad poll
                log.exception("monitor tick failed for %s", self.device_id)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)

    async def tick(self) -> list[dict]:
        """Poll once, record anything crossed. Returns what it recorded."""
        status = await self.status_fn()
        crossed = detect_milestones(self._previous, status)
        self._previous = status

        recorded = []
        for name, detail in crossed:
            if name not in self.watch:
                continue
            # A milestone fires once per run; "alert" is keyed by its detail so
            # that a second, different fault is still captured.
            key = name if name != "alert" else f"alert:{detail}"
            if key in self._seen:
                continue
            self._seen.add(key)
            recorded.append(await self._record(name, detail, status))
        return recorded

    async def _record(self, name: str, detail: str, status: PrinterStatus) -> dict:
        image_path = None
        if self.snapshot_fn is not None:
            try:
                frame = await self.snapshot_fn()
                path = self.capture_path_fn(f"stage-{name}")
                path.write_bytes(frame)
                image_path = str(path)
            except MHSError as exc:
                log.info("no frame for %s on %s: %s", name, self.device_id, exc.message)

        observation_id = self.store.add_observation(
            self.device_id,
            kind=f"stage:{name}",
            run_id=self.run_id,
            layer=status.current_layer,
            text=detail,
            image_path=image_path,
        )
        log.info("%s: %s (%s)", self.device_id, name, detail)
        return {
            "observation_id": observation_id,
            "milestone": name,
            "detail": detail,
            "layer": status.current_layer,
            "progress_percent": status.progress_percent,
            "image_path": image_path,
            "at": iso(time.time()),
        }


def build_report(run: dict, milestones: list[dict]) -> dict:
    """Assemble a run into something worth reading.

    Pulls together what was asked for, what the slicer predicted, what actually
    happened and what was seen along the way - which is the material for
    deciding what to change next time.
    """
    settings = run.get("settings") or {}
    started, ended = run.get("started_at"), run.get("ended_at")
    actual_minutes = round((ended - started) / 60) if started and ended else None
    # A print that rounds to zero minutes is still a measured duration, so the
    # comparison below tests for None rather than for truth.
    predicted = settings.get("estimated_time_minutes") or settings.get("slice", {}).get(
        "estimated_time_minutes"
    )

    timeline = []
    for entry in milestones:
        timeline.append({
            "observation_id": entry.get("id"),
            "milestone": stage_name(str(entry.get("kind", ""))),
            "at": iso(entry.get("created_at")),
            "layer": entry.get("layer"),
            "detail": entry.get("text"),
            "image_path": entry.get("image_path"),
            "caption": entry.get("caption"),
        })
    timeline.sort(key=lambda row: row["at"] or "")

    seen = {row["milestone"] for row in timeline}
    uncaptioned = [
        {"observation_id": row["observation_id"], "milestone": row["milestone"],
         "image_path": row["image_path"]}
        for row in timeline
        if row["image_path"] and not row["caption"]
    ]
    report = {
        "run_id": run.get("id"),
        "device_id": run.get("device_id"),
        "job_name": run.get("job_name"),
        "file": run.get("remote_path"),
        "outcome": run.get("outcome"),
        "quality_score": run.get("quality_score"),
        "notes": run.get("notes"),
        "settings": settings,
        "started_at": run.get("started_at_iso"),
        "ended_at": run.get("ended_at_iso"),
        "duration_minutes": actual_minutes,
        "predicted_minutes": predicted,
        "timeline": timeline,
        "images": [row["image_path"] for row in timeline if row["image_path"]],
        "uncaptioned_images": uncaptioned,
        "missing_milestones": [m for m in ("start", "first_layer", "finished") if m not in seen],
    }
    if predicted and actual_minutes is not None:
        drift = (actual_minutes - predicted) / predicted * 100
        report["time_vs_estimate"] = {
            "predicted_minutes": predicted,
            "actual_minutes": actual_minutes,
            "drift_percent": round(drift, 1),
            "note": (
                "close to the slicer's estimate" if abs(drift) <= 15
                else f"{'over' if drift > 0 else 'under'} the estimate by {abs(drift):.0f}%"
            ),
        }
    report["summary"] = _summarise(report)
    return report


def _summarise(report: dict) -> str:
    bits = [f"run {report['run_id']}: {report.get('job_name') or 'unnamed'}"]
    outcome = report.get("outcome")
    if outcome and outcome != "in_progress":
        bits.append(outcome)
    if report.get("duration_minutes") is not None:
        bits.append(f"{report['duration_minutes']} min")
    drift = report.get("time_vs_estimate")
    if drift:
        bits.append(drift["note"])
    captured = len(report.get("images", []))
    if captured:
        bits.append(f"{captured} frame(s) captured")
    uncaptioned = len(report.get("uncaptioned_images", []))
    if uncaptioned:
        bits.append(f"{uncaptioned} awaiting a caption")
    if report.get("missing_milestones"):
        bits.append("no frame for: " + ", ".join(report["missing_milestones"]))
    return ", ".join(bits)


def stage_name(kind: str) -> str:
    """The milestone behind an observation kind (``stage:first_layer``)."""
    return kind.split(":", 1)[1] if kind.startswith("stage:") else kind
