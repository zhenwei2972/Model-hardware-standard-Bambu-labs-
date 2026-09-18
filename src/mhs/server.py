"""The MCP server: every printer capability, exposed as a tool.

Design notes that matter when reading the tool list below:

* Tools return plain dicts, and failures come back as ``{"ok": false, "error":
  ...}`` with a hint rather than as protocol errors, so the model can recover
  (wrong access code, Developer Mode off, printer busy) instead of giving up.
* Anything that moves the machine takes ``confirm=True``. That makes an
  accidental print or an aborted job a deliberate two-step action.
* ``MHS_READ_ONLY=1`` turns the whole server into a monitoring-only surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .app import MHSApp
from .config import Settings, load_settings
from .device import Printer
from .errors import CommandRejected, MHSError, NotSupported
from .models import Capability, PrintOptions
from .scheduler import parse_when

log = logging.getLogger(__name__)

INSTRUCTIONS = """
Control and observe 3D printers (Bambu Lab A1 mini / A1 / P1 / X1 over the LAN).

Typical flows:
* Check on a print: `get_status`, then `capture_snapshot` to actually look at it.
* Print something already sliced: `upload_and_print(local_path=..., confirm=True)`.
* Print later: `schedule_print(file=..., when="2026-09-19T06:30")` - the job only
  fires while the printer is idle, and expires if the window passes.
* Tune settings over time: `log_print_result` after each print, then
  `list_print_history` to compare what worked.

Constraints worth knowing before you act:
* Files must be sliced (.3mf/.gcode) already; this server does not slice.
* Physical actions need `confirm=True`.
* If commands come back `acknowledged: false`, the printer is almost certainly
  in cloud mode: LAN Only Mode + Developer Mode must be enabled on its screen.
""".strip()


def _ok(**payload: Any) -> dict:
    return {"ok": True, **payload}


def _fail(exc: MHSError) -> dict:
    log.info("tool error: %s", exc.message)
    return {"ok": False, **exc.to_dict()}


def create_server(settings: Settings | None = None, *, run_scheduler: bool = True) -> MCPServer:
    """Build the MCP server. Kept a factory so tests can inject settings."""
    app = MHSApp(settings or load_settings())

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[MHSApp]:
        await app.startup(run_scheduler=run_scheduler)
        try:
            yield app
        finally:
            await app.shutdown()

    server = MCPServer(
        name="mhs-printer",
        title="Model Hardware Standard - 3D printers",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )
    server.mhs_app = app  # type: ignore[attr-defined]  # convenience for tests/CLI

    async def _printer(printer_id: str | None) -> Printer:
        return await app.printer(printer_id)

    # ---------------------------------------------------------------- status
    @server.tool()
    async def list_printers() -> dict:
        """List configured printers, their driver, model and capabilities."""
        out = []
        ids = app.pool.ids()
        default = app.settings.default_printer or (ids[0] if len(ids) == 1 else None)
        for pid in ids:
            config = app.settings.printers[pid]
            entry: dict[str, Any] = {
                "printer_id": pid,
                "driver": config.driver,
                "model": config.model,
                "host": config.host or None,
                "is_default": pid == default,
            }
            try:
                printer = await _printer(pid)
                entry["capabilities"] = [c.value for c in printer.capabilities]
                entry["connected"] = True
                entry["firmware"] = (await printer.info()).firmware
            except MHSError as exc:
                entry["connected"] = False
                entry["error"] = exc.message
            out.append(entry)
        return _ok(printers=out, read_only=app.settings.read_only)

    @server.tool()
    async def get_status(printer: str | None = None) -> dict:
        """Current state of a printer: job, progress, layer, temperatures, filament, alerts.

        Call this before any action that depends on the machine being free.
        """
        try:
            device = await _printer(printer)
            status = await device.status()
        except MHSError as exc:
            return _fail(exc)
        return _ok(summary=status.summary(), status=status.to_dict())

    @server.tool()
    async def get_printer_info(printer: str | None = None) -> dict:
        """Static facts about a printer: model, serial, firmware, build volume."""
        try:
            device = await _printer(printer)
            return _ok(info=(await device.info()).to_dict())
        except MHSError as exc:
            return _fail(exc)

    # ----------------------------------------------------------------- files
    @server.tool()
    async def list_files(printer: str | None = None, directory: str = "") -> dict:
        """List sliced files on the printer's storage (defaults to the upload directory)."""
        try:
            device = await _printer(printer)
            device.require(Capability.FILE_LIST)
            files = await device.list_files(directory)
        except MHSError as exc:
            return _fail(exc)
        return _ok(directory=directory or app.settings.get(printer).upload_dir,
                   files=[f.to_dict() for f in files])

    @server.tool()
    async def upload_file(
        local_path: str, printer: str | None = None, remote_name: str | None = None
    ) -> dict:
        """Upload a sliced .3mf/.gcode from this machine to the printer's storage.

        Does not start anything; pair with `start_print`, or use `upload_and_print`.
        """
        try:
            app.require_writable("upload a file")
            device = await _printer(printer)
            device.require(Capability.FILE_UPLOAD)
            entry = await device.upload_file(local_path, remote_name)
        except MHSError as exc:
            return _fail(exc)
        return _ok(file=entry.to_dict())

    # ------------------------------------------------------------- printing
    def _options(
        plate: int,
        use_ams: bool,
        ams_slots: list[int] | None,
        bed_leveling: bool,
        flow_calibration: bool,
        timelapse: bool,
        job_name: str | None,
    ) -> PrintOptions:
        return PrintOptions(
            plate=plate,
            use_ams=use_ams,
            ams_mapping=ams_slots,
            bed_leveling=bed_leveling,
            flow_calibration=flow_calibration,
            timelapse=timelapse,
            job_name=job_name,
        )

    @server.tool()
    async def start_print(
        file: str,
        confirm: bool = False,
        printer: str | None = None,
        plate: int = 1,
        use_ams: bool = False,
        ams_slots: list[int] | None = None,
        bed_leveling: bool = True,
        flow_calibration: bool = True,
        timelapse: bool = False,
        job_name: str | None = None,
    ) -> dict:
        """Start a print from a file **already on the printer** (see `list_files`).

        `file` is the printer-side path, e.g. "cache/benchy.3mf". `ams_slots` maps
        the file's colours to AMS trays (0-3) in order. Requires `confirm=True`.
        """
        if not confirm:
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": f"Would start {file} on {printer or 'the default printer'}.",
                "hint": "Call again with confirm=true once the bed is clear.",
            }
        try:
            app.require_writable("start a print")
            device = await _printer(printer)
            device.require(Capability.START_PRINT)
            options = _options(plate, use_ams, ams_slots, bed_leveling, flow_calibration, timelapse, job_name)
            result = await device.start_print(file, options)
            run_id = app.store.start_run(
                device.printer_id,
                job_name=options.job_name or Path(file).stem,
                remote_path=file,
                settings=options.to_dict(),
            )
        except MHSError as exc:
            return _fail(exc)
        return _ok(result=result, run_id=run_id,
                   note="Record the outcome later with log_print_result(run_id=...).")

    @server.tool()
    async def upload_and_print(
        local_path: str,
        confirm: bool = False,
        printer: str | None = None,
        plate: int = 1,
        use_ams: bool = False,
        ams_slots: list[int] | None = None,
        bed_leveling: bool = True,
        flow_calibration: bool = True,
        timelapse: bool = False,
        job_name: str | None = None,
    ) -> dict:
        """Upload a sliced file and immediately print it. Requires `confirm=True`."""
        if not confirm:
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": f"Would upload {local_path} and print it.",
                "hint": "Call again with confirm=true once the bed is clear.",
            }
        try:
            app.require_writable("start a print")
            device = await _printer(printer)
            device.require(Capability.FILE_UPLOAD)
            entry = await device.upload_file(local_path)
        except MHSError as exc:
            return _fail(exc)
        started = await start_print(
            file=entry.path,
            confirm=True,
            printer=printer,
            plate=plate,
            use_ams=use_ams,
            ams_slots=ams_slots,
            bed_leveling=bed_leveling,
            flow_calibration=flow_calibration,
            timelapse=timelapse,
            job_name=job_name or entry.name,
        )
        started["uploaded"] = entry.to_dict()
        return started

    @server.tool()
    async def pause_print(printer: str | None = None) -> dict:
        """Pause the running print."""
        try:
            app.require_writable("pause the print")
            device = await _printer(printer)
            return _ok(result=await device.pause_print())
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def resume_print(printer: str | None = None) -> dict:
        """Resume a paused print."""
        try:
            app.require_writable("resume the print")
            device = await _printer(printer)
            return _ok(result=await device.resume_print())
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def stop_print(confirm: bool = False, printer: str | None = None) -> dict:
        """Abort the running print. Irreversible - the job cannot be resumed."""
        if not confirm:
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": "Stopping a print cannot be undone and wastes the partial object.",
                "hint": "Call again with confirm=true.",
            }
        try:
            app.require_writable("stop the print")
            device = await _printer(printer)
            return _ok(result=await device.stop_print())
        except MHSError as exc:
            return _fail(exc)

    # ------------------------------------------------------------- controls
    @server.tool()
    async def set_temperature(
        nozzle: float | None = None, bed: float | None = None, printer: str | None = None
    ) -> dict:
        """Set nozzle and/or bed target temperature in Celsius (0 turns a heater off)."""
        try:
            app.require_writable("change temperatures")
            device = await _printer(printer)
            device.require(Capability.TEMPERATURE_CONTROL)
            return _ok(result=await device.set_temperature(nozzle, bed))
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def set_light(on: bool, printer: str | None = None, node: str = "chamber_light") -> dict:
        """Turn the printer's LED on or off (useful before a snapshot)."""
        try:
            app.require_writable("change the light")
            device = await _printer(printer)
            device.require(Capability.LIGHT)
            return _ok(result=await device.set_light(on, node))
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def set_print_speed(level: int, printer: str | None = None) -> dict:
        """Set speed preset: 1 silent, 2 standard, 3 sport, 4 ludicrous."""
        try:
            app.require_writable("change print speed")
            device = await _printer(printer)
            device.require(Capability.SPEED)
            return _ok(result=await device.set_speed(level))
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def send_gcode(gcode: str, confirm: bool = False, printer: str | None = None) -> dict:
        """Send raw gcode. Restricted to a safe list unless allow_raw_gcode is set."""
        if not confirm:
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": f"Would send: {gcode}",
                "hint": "Call again with confirm=true.",
            }
        try:
            app.require_writable("send gcode")
            device = await _printer(printer)
            device.require(Capability.RAW_GCODE)
            if not app.settings.allow_raw_gcode:
                from .drivers.bambu.commands import assert_gcode_allowed

                assert_gcode_allowed(gcode)
            return _ok(result=await device.send_gcode(gcode))
        except MHSError as exc:
            return _fail(exc)

    # --------------------------------------------------------------- camera
    @server.tool()
    async def capture_snapshot(printer: str | None = None, save: bool = True, label: str = "frame") -> Image:
        """Grab one frame from the printer camera and return it as an image.

        Use it to actually look at the print: first-layer adhesion, stringing,
        spaghetti, whether the plate is clear before starting a job.
        """
        device = await _printer(printer)
        device.require(Capability.CAMERA_SNAPSHOT)
        frame = await device.snapshot()
        if save:
            path = app.capture_path(device.printer_id, label)
            path.write_bytes(frame)
            app.store.add_observation(device.printer_id, kind="snapshot", image_path=str(path))
        return Image(data=frame, format="jpeg")

    @server.tool()
    async def capture_frames(
        count: int = 5,
        interval_seconds: float = 20.0,
        printer: str | None = None,
        label: str = "series",
    ) -> dict:
        """Capture several frames over time and save them to disk.

        Returns the file paths; read them back with `read_capture` when you want
        to compare how the print evolved (layer shifts, warping, a failure that
        started 20 minutes ago).
        """
        if count < 1 or count > 60:
            return _fail(CommandRejected("count must be between 1 and 60"))
        if interval_seconds < 1 or count * interval_seconds > 3600:
            return _fail(CommandRejected("keep count x interval under one hour, interval >= 1s"))
        try:
            device = await _printer(printer)
            device.require(Capability.CAMERA_SNAPSHOT)
        except MHSError as exc:
            return _fail(exc)

        paths: list[str] = []
        for index in range(count):
            if index:
                await asyncio.sleep(interval_seconds)
            try:
                frame = await device.snapshot()
            except MHSError as exc:
                return _fail(exc) if not paths else _ok(paths=paths, partial=True, error=exc.message)
            status = await device.status()
            path = app.capture_path(device.printer_id, f"{label}-{index:03d}")
            path.write_bytes(frame)
            app.store.add_observation(
                device.printer_id, kind="snapshot", layer=status.current_layer, image_path=str(path)
            )
            paths.append(str(path))
        return _ok(paths=paths, count=len(paths))

    @server.tool()
    async def read_capture(path: str) -> Image:
        """Return a previously captured frame as an image."""
        file = Path(path).expanduser()
        captures = app.settings.capture_dir.resolve()
        if not file.resolve().is_relative_to(captures):
            raise ToolError(f"{path} is outside the capture directory {captures}")
        if not file.is_file():
            raise ToolError(f"no capture at {path}")
        return Image(data=file.read_bytes(), format="jpeg")

    # ------------------------------------------------------------ scheduling
    @server.tool()
    async def schedule_print(
        file: str,
        when: str,
        printer: str | None = None,
        window_minutes: int = 60,
        note: str | None = None,
        plate: int = 1,
        use_ams: bool = False,
        ams_slots: list[int] | None = None,
        bed_leveling: bool = True,
        flow_calibration: bool = True,
        timelapse: bool = False,
        job_name: str | None = None,
    ) -> dict:
        """Queue a print to start later.

        `when` accepts ISO-8601 ("2026-09-19T06:30", local time if no offset) or a
        relative offset ("in 90m", "+2h"). The job fires only while the printer is
        idle and alert-free; if it is still busy `window_minutes` after the target
        time the job is marked missed rather than starting hours late.
        The file must already be on the printer - upload it first.
        """
        try:
            app.require_writable("schedule a print")
            start_at = parse_when(when)
            device = await _printer(printer)
            known = {f.path for f in await device.list_files()}
            if file.strip("/") not in known and known:
                return _fail(
                    CommandRejected(
                        f"{file} is not on {device.printer_id}",
                        hint=f"Upload it first. Present: {', '.join(sorted(known)[:8]) or 'nothing'}",
                    )
                )
            options = _options(plate, use_ams, ams_slots, bed_leveling, flow_calibration, timelapse, job_name)
            job = app.store.add_job(
                device.printer_id,
                file,
                start_at,
                options=options.to_dict(),
                window_minutes=window_minutes,
                note=note,
            )
        except MHSError as exc:
            return _fail(exc)
        return _ok(job=job.to_dict(), starts_in_seconds=int(start_at - time.time()))

    @server.tool()
    async def list_scheduled_jobs(printer: str | None = None, status: str | None = None) -> dict:
        """List scheduled prints.

        Status is one of: pending, started, done, failed, cancelled, missed.
        """
        jobs = app.store.list_jobs(printer_id=printer, status=status)
        return _ok(jobs=[j.to_dict() for j in jobs])

    @server.tool()
    async def cancel_scheduled_job(job_id: str) -> dict:
        """Cancel a pending scheduled print."""
        job = app.store.cancel_job(job_id)
        if job is None:
            return {"ok": False, "error": "NotFound", "message": f"no job {job_id}"}
        return _ok(job=job.to_dict())

    # ------------------------------------------------------- print journal
    @server.tool()
    async def log_print_result(
        run_id: int,
        outcome: str,
        quality_score: int | None = None,
        notes: str | None = None,
    ) -> dict:
        """Record how a print turned out: outcome (success/failed/aborted/partial),
        an optional 1-10 quality score and free-text notes.

        This is the memory that makes settings iteration possible - `start_print`
        returns the `run_id` to use here.
        """
        if outcome not in {"success", "failed", "aborted", "partial", "in_progress"}:
            return _fail(CommandRejected("outcome must be success, failed, aborted, partial or in_progress"))
        if quality_score is not None and not 1 <= quality_score <= 10:
            return _fail(CommandRejected("quality_score must be 1-10"))
        run = app.store.finish_run(run_id, outcome, quality_score=quality_score, notes=notes)
        if run is None:
            return {"ok": False, "error": "NotFound", "message": f"no run {run_id}"}
        return _ok(run=run)

    @server.tool()
    async def record_observation(
        text: str,
        printer: str | None = None,
        run_id: int | None = None,
        layer: int | None = None,
        kind: str = "note",
    ) -> dict:
        """Attach a note to a run (e.g. "layer 40: slight stringing on the tower")."""
        device_id = (await _printer(printer)).printer_id if printer is None else printer
        obs_id = app.store.add_observation(device_id, kind=kind, run_id=run_id, layer=layer, text=text)
        return _ok(observation_id=obs_id)

    @server.tool()
    async def list_print_history(printer: str | None = None, limit: int = 20) -> dict:
        """Past prints with their settings, outcome, score and notes.

        Use it before changing settings: compare what actually printed well.
        """
        return _ok(runs=app.store.list_runs(printer_id=printer, limit=limit))

    @server.tool()
    async def get_print_run(run_id: int) -> dict:
        """One run in full, including its observations and snapshot paths."""
        run = app.store.get_run(run_id)
        if run is None:
            return {"ok": False, "error": "NotFound", "message": f"no run {run_id}"}
        return _ok(run=run)

    # ------------------------------------------------------------ diagnosis
    @server.tool()
    async def check_connection(printer: str | None = None) -> dict:
        """Probe every transport (MQTT telemetry, file transfer, camera) and report
        what works. Run this first when something behaves oddly."""
        report: dict[str, Any] = {"printer": printer or app.settings.default_printer}
        try:
            device = await _printer(printer)
        except MHSError as exc:
            return _fail(exc)
        report["printer"] = device.printer_id

        try:
            status = await device.status()
            report["telemetry"] = {"ok": status.online, "state": status.state.value,
                                   "summary": status.summary()}
        except MHSError as exc:
            report["telemetry"] = {"ok": False, **exc.to_dict()}
        try:
            files = await device.list_files()
            report["files"] = {"ok": True, "count": len(files)}
        except (MHSError, NotSupported) as exc:
            report["files"] = {"ok": False, **exc.to_dict()}
        try:
            frame = await device.snapshot()
            report["camera"] = {"ok": True, "bytes": len(frame)}
        except (MHSError, NotSupported) as exc:
            report["camera"] = {"ok": False, **exc.to_dict()}

        failures = [k for k, v in report.items() if isinstance(v, dict) and not v.get("ok")]
        report["all_ok"] = not failures
        if failures:
            report["hint"] = (
                "Enable LAN Only Mode + Developer Mode (and LAN Mode Liveview for the camera) "
                "on the printer screen, and confirm the Access Code and serial in your config."
            )
        return _ok(**report)

    # ------------------------------------------------------------- resources
    @server.resource("mhs://printers", mime_type="application/json")
    async def printers_resource() -> dict:
        """Configured printers."""
        return await list_printers()

    @server.resource("mhs://printer/{printer_id}/status", mime_type="application/json")
    async def status_resource(printer_id: str) -> dict:
        """Live status of one printer."""
        return await get_status(printer_id)

    @server.resource("mhs://printer/{printer_id}/history", mime_type="application/json")
    async def history_resource(printer_id: str) -> dict:
        """Recent prints on one printer."""
        return await list_print_history(printer_id)

    # --------------------------------------------------------------- prompts
    @server.prompt()
    def diagnose_print(printer: str | None = None) -> str:
        """Walk through diagnosing a print that looks wrong."""
        return (
            f"Check printer {printer or 'the default printer'}: call get_status, then "
            "capture_snapshot and look at the image. Compare what you see against the "
            "reported layer and temperatures, check list_print_history for how the same "
            "file printed before, and recommend one concrete change (or pausing/stopping "
            "the job). Be explicit about what you can and cannot see in the frame."
        )

    @server.prompt()
    def tune_settings(goal: str = "better surface finish") -> str:
        """Plan a settings experiment across prints."""
        return (
            f"Goal: {goal}. Use list_print_history to see which settings were already "
            "tried and how they scored, propose exactly one change for the next print, "
            "explain the expected effect, and after printing record the result with "
            "log_print_result including a 1-10 score."
        )

    return server


def main() -> None:
    """Console entry point: run the server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    create_server().run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
