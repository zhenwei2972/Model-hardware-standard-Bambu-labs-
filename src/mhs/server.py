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
from .design import mesh as meshlib
from .design import vision
from .design.printability import analyze as analyze_print
from .design.printability import scale_advice
from .design.render import render_to_png
from .errors import CommandRejected, MHSError, NotSupported
from .models import Capability, CleanOptions, PrintOptions
from .printer import Printer
from .scheduler import parse_when
from .slicing import INTENTS, SliceSettings, settings_for_intent
from .specs import spec_for
from .standard.descriptor import descriptor_markdown
from .vacuum import Vacuum

log = logging.getLogger(__name__)

INSTRUCTIONS = """
Control and observe physical devices: Bambu Lab 3D printers (A1 mini / A1 / P1 /
X1, over the LAN) and Roborock robot vacuums (Saros 10 / 10R / Z70, via the
Roborock account). Every device also exposes the MHS read/write channels -
`list_channels`, `read_channel`, `write_channel` - whose limits are enforced in
the driver.

Typical flows:
* See what is connected: `list_devices`, then `describe_device` for one of them.
* Check on a print: `get_status`, then `capture_snapshot` to actually look at it.
* Send the robot somewhere: `get_map` (read coordinates off the grid), then
  `go_to(x_mm=..., y_mm=..., confirm=True)`, or `go_to(location="kitchen")`.
* Clean a room: `list_rooms`, then `start_cleaning(rooms=["kitchen"], confirm=True)`.
* Design loop: `analyze_model` -> `preview_model` (look at it) -> `scale_model` ->
  `slice_model` -> print -> `camera_grid` + `camera_measure` to check the result.
* Weigh a trade-off in real numbers: `compare_slice_intents` slices the same
  model at several settings and reports each one's time and filament.
* Size something against a real object: put a coin on the plate, `camera_grid`,
  read the coin's span in pixels off the grid, `camera_calibrate`, then
  `camera_measure` anything else in that frame.
* Print something already sliced: `upload_and_print(local_path=..., confirm=True)`.
* Print later: `schedule_print(file=..., when="2026-09-19T06:30")` - the job only
  fires while the printer is idle, and expires if the window passes.
* Tune settings over time: `log_print_result` after each print, then
  `list_print_history` to compare what worked.

Constraints worth knowing before you act:
* `slice_model` drives an installed OrcaSlicer/Bambu Studio/PrusaSlicer. Without
  one on the PATH every other tool still works; only slicing needs it.
* Camera measurements are estimates from an off-axis camera: good to a few
  percent with a reference object in the same plane, not caliper-grade.
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

    async def _printer(device_id: str | None) -> Printer:
        device = await app.device(device_id)
        if not isinstance(device, Printer):
            raise NotSupported(
                f"{device.device_id} is a {device.device_kind.replace('_', ' ')}, "
                "not a 3D printer",
                hint="Use the vacuum tools for it, or name a printer with the "
                     "`printer` argument.",
            )
        return device

    async def _vacuum(device_id: str | None) -> Vacuum:
        device = await app.device(device_id)
        if not isinstance(device, Vacuum):
            raise NotSupported(
                f"{device.device_id} is a {device.device_kind.replace('_', ' ')}, "
                "not a robot vacuum",
                hint="Use the printer tools for it, or name a vacuum with the "
                     "`vacuum` argument.",
            )
        return device

    # ---------------------------------------------------------------- status
    @server.tool()
    async def list_devices() -> dict:
        """List configured devices - printers and vacuums - with their capabilities."""
        out = []
        ids = app.pool.ids()
        default = app.settings.default_device or (ids[0] if len(ids) == 1 else None)
        for pid in ids:
            config = app.settings.devices[pid]
            entry: dict[str, Any] = {
                "device_id": pid,
                "driver": config.driver,
                "model": config.model,
                "host": config.host or None,
                "is_default": pid == default,
            }
            try:
                device = await app.device(pid)
                entry["kind"] = device.device_kind
                entry["capabilities"] = [c.value for c in device.capabilities]
                entry["connected"] = True
                entry["firmware"] = (await device.info()).firmware
            except MHSError as exc:
                entry["connected"] = False
                entry["error"] = exc.message
            out.append(entry)
        return _ok(devices=out, read_only=app.settings.read_only)

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
                device.device_id,
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
            path = app.capture_path(device.device_id, label)
            path.write_bytes(frame)
            app.store.add_observation(device.device_id, kind="snapshot", image_path=str(path))
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
            path = app.capture_path(device.device_id, f"{label}-{index:03d}")
            path.write_bytes(frame)
            app.store.add_observation(
                device.device_id, kind="snapshot", layer=status.current_layer, image_path=str(path)
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
                        f"{file} is not on {device.device_id}",
                        hint=f"Upload it first. Present: {', '.join(sorted(known)[:8]) or 'nothing'}",
                    )
                )
            options = _options(plate, use_ams, ams_slots, bed_leveling, flow_calibration, timelapse, job_name)
            job = app.store.add_job(
                device.device_id,
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
        jobs = app.store.list_jobs(device_id=printer, status=status)
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
        device_id = (await _printer(printer)).device_id if printer is None else printer
        obs_id = app.store.add_observation(device_id, kind=kind, run_id=run_id, layer=layer, text=text)
        return _ok(observation_id=obs_id)

    @server.tool()
    async def list_print_history(printer: str | None = None, limit: int = 20) -> dict:
        """Past prints with their settings, outcome, score and notes.

        Use it before changing settings: compare what actually printed well.
        """
        return _ok(runs=app.store.list_runs(device_id=printer, limit=limit))

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
        report: dict[str, Any] = {"printer": printer or app.settings.default_device}
        try:
            device = await _printer(printer)
        except MHSError as exc:
            return _fail(exc)
        report["printer"] = device.device_id

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

    # ----------------------------------------------------------- the model
    def _model_path(path: str) -> Path:
        file = Path(path).expanduser()
        if not file.is_file():
            raise CommandRejected(f"{path} does not exist")
        if file.suffix.lower() not in meshlib.SUPPORTED_SUFFIXES:
            raise CommandRejected(
                f"{file.suffix} is not a mesh format",
                hint=f"Supported: {', '.join(meshlib.SUPPORTED_SUFFIXES)}",
            )
        return file

    @server.tool()
    async def analyze_model(
        path: str,
        printer: str | None = None,
        nozzle_mm: float | None = None,
        layer_height_mm: float | None = None,
        material: str = "PLA",
    ) -> dict:
        """Measure a mesh and judge it against what the printer can actually resolve.

        Returns its dimensions and volume, plus findings: does it fit, which
        detail is finer than the extrusion width, thin walls, overhangs needing
        support, bed contact, layer count, estimated filament. Use it before
        slicing, and again after `scale_model` - shrinking a model is exactly
        when detail stops being printable.
        """
        try:
            model = meshlib.load_mesh(_model_path(path))
            spec = spec_for(app.settings.get(printer).model if app.settings.devices else None)
            report = analyze_print(
                model, spec, nozzle_mm=nozzle_mm, layer_height_mm=layer_height_mm, material=material
            )
        except MHSError as exc:
            return _fail(exc)
        return _ok(model=model.summary(), printability=report.to_dict(), verdict=report.verdict())

    @server.tool()
    async def preview_model(
        path: str,
        views: list[str] | None = None,
        title: str | None = None,
        size: int = 420,
    ) -> Image:
        """Render a mesh so you can look at it before printing.

        Orthographic views at a shared scale with a millimetre scale bar; faces
        that overhang more than 45 degrees are tinted red. Views: iso, front,
        back, left, right, top, bottom. Overhangs are only visible from below,
        so pass ["bottom", "iso"] when checking supports.
        """
        file = _model_path(path)
        model = meshlib.load_mesh(file)
        chosen = tuple(views) if views else ("iso", "front", "right", "top")
        try:
            data = render_to_png(
                model,
                path=app.settings.capture_dir / "models" / f"{file.stem}-preview.png",
                views=chosen,
                size=max(160, min(size, 900)),
                title=title or f"{file.name}  -  {model.dimensions[0]:.1f} x "
                               f"{model.dimensions[1]:.1f} x {model.dimensions[2]:.1f} mm",
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return Image(data=data, format="png")

    @server.tool()
    async def scale_model(
        path: str,
        target_mm: float,
        axis: str = "max",
        apply: bool = False,
        output_path: str | None = None,
        printer: str | None = None,
    ) -> dict:
        """Resize a mesh so one dimension becomes `target_mm`, keeping proportions.

        `axis` is x, y, z, max (longest side - the usual "make it this big") or
        min. Defaults to a dry run reporting the before/after detail trade-off;
        pass apply=true to write a scaled STL ready for slicing.

        To match a real object, measure it first with `camera_measure`, or look
        up its size in the mhs://reference-objects resource.
        """
        try:
            file = _model_path(path)
            model = meshlib.load_mesh(file)
            spec = spec_for(app.settings.get(printer).model if app.settings.devices else None)
            advice = scale_advice(model, spec, target_mm, axis)
            if not apply:
                advice["applied"] = False
                advice["hint"] = "Call again with apply=true to write the scaled file."
                return _ok(**advice)

            scaled, _factor = model.scale_to_dimension(target_mm, axis)
            target = Path(output_path).expanduser() if output_path else (
                app.settings.capture_dir / "models" / f"{file.stem}-{target_mm:g}mm.stl"
            )
            written = meshlib.save_stl(scaled, target)
            report = analyze_print(scaled, spec)
        except MHSError as exc:
            return _fail(exc)
        advice["applied"] = True
        return _ok(**advice, output=str(written), model=scaled.summary(),
                   printability=report.to_dict(), verdict=report.verdict())

    # ------------------------------------------------- measuring the result
    def _calibration(device_id: str) -> vision.ScaleCalibration | None:
        stored = app.store.get_calibration(device_id)
        return vision.ScaleCalibration.from_dict(stored) if stored else None

    @server.tool()
    async def camera_grid(printer: str | None = None, grid_px: int = 80) -> Image:
        """Capture a frame and overlay a labelled pixel grid.

        Read coordinates off this image - the grid is what makes "the coin spans
        x=410 to x=498" accurate rather than a guess. The frame is saved, and
        `camera_calibrate` and `camera_measure` then work against this same one.
        """
        device = await _printer(printer)
        device.require(Capability.CAMERA_SNAPSHOT)
        frame = await device.snapshot()
        raw = app.capture_path(device.device_id, "grid-source")
        raw.write_bytes(frame)
        app.store.add_observation(device.device_id, kind="snapshot", image_path=str(raw))
        annotated = vision.annotate_grid(frame, grid_px, _calibration(device.device_id))
        app.capture_path(device.device_id, "grid").with_suffix(".png").write_bytes(annotated)
        return Image(data=annotated, format="png")

    @server.tool()
    async def camera_calibrate(
        reference: str,
        pixel_length: float,
        printer: str | None = None,
        note: str | None = None,
    ) -> dict:
        """Set the camera's millimetres-per-pixel from a known object in the frame.

        `reference` is a name from mhs://reference-objects (e.g. "sgd_1",
        "usd_quarter") or a plain size in mm; `pixel_length` is how many pixels
        that object spans, read off `camera_grid`. Put the object flat on the
        plate, near whatever you want to measure - the calibration is only valid
        in that plane.
        """
        try:
            device = await _printer(printer)
            calibration = vision.calibrate(
                reference, pixel_length, device_id=device.device_id, note=note
            )
            app.store.save_calibration(device.device_id, calibration.to_dict())
        except MHSError as exc:
            return _fail(exc)
        return _ok(
            calibration=calibration.to_dict(),
            example=f"1 mm is now {1 / calibration.mm_per_pixel:.1f} px; the 180 mm plate would "
                    f"span {180 / calibration.mm_per_pixel:.0f} px.",
            caveat=vision.ACCURACY_CAVEAT,
        )

    @server.tool()
    async def camera_measure(
        point_a: list[float],
        point_b: list[float],
        printer: str | None = None,
        target_mm: float | None = None,
        frame_path: str | None = None,
        label: str | None = None,
    ) -> dict:
        """Measure between two pixel coordinates on the latest camera frame.

        Pass the endpoints as [x, y] pairs read off `camera_grid`. With a
        calibration set, returns millimetres and draws the span back onto the
        frame so the reading can be checked. Give `target_mm` to compare a
        printed part against its intended size.
        """
        try:
            device = await _printer(printer)
            source = frame_path or app.store.latest_snapshot(device.device_id)
            if not source or not Path(source).is_file():
                return _fail(CommandRejected(
                    "no camera frame to measure",
                    hint="Call camera_grid first; measurements must use the frame the "
                         "coordinates were read from.",
                ))
            if len(point_a) != 2 or len(point_b) != 2:
                return _fail(CommandRejected("points must be [x, y] pixel pairs"))
            calibration = _calibration(device.device_id)
            annotated, pixels, millimetres = vision.annotate_measurement(
                Path(source).read_bytes(), tuple(point_a), tuple(point_b), calibration, label
            )
            out = app.capture_path(device.device_id, "measure").with_suffix(".png")
            out.write_bytes(annotated)
        except MHSError as exc:
            return _fail(exc)

        result: dict[str, Any] = {
            "pixels": round(pixels, 1),
            "millimetres": round(millimetres, 2) if millimetres is not None else None,
            "frame": str(source),
            "annotated_image": str(out),
            "caveat": vision.ACCURACY_CAVEAT,
        }
        if millimetres is None:
            result["hint"] = "Uncalibrated: call camera_calibrate with a known object first."
        elif target_mm:
            result["comparison"] = vision.compare_to_target(millimetres, target_mm)
        return _ok(**result)

    @server.tool()
    async def read_annotated_image(path: str) -> Image:
        """Return a saved grid or measurement image so you can look at it again."""
        file = Path(path).expanduser()
        captures = app.settings.capture_dir.resolve()
        if not file.resolve().is_relative_to(captures):
            raise ToolError(f"{path} is outside the capture directory {captures}")
        if not file.is_file():
            raise ToolError(f"no image at {path}")
        return Image(data=file.read_bytes(), format=file.suffix.lstrip(".").lower() or "png")

    # ------------------------------------------------- MHS standard surface
    @server.tool()
    async def describe_device(device: str | None = None, as_markdown: bool = True) -> dict:
        """The device's MHS descriptor: what it is, what it exposes, what it refuses.

        Read this before operating an unfamiliar machine - it lists every
        channel with its units and enforced limits, the resolution the hardware
        can actually achieve, and what the device cannot do.
        """
        try:
            target = await app.device(device)
            descriptor = await target.describe()
        except MHSError as exc:
            return _fail(exc)
        payload = {"descriptor": descriptor}
        if as_markdown:
            payload["reference"] = descriptor_markdown(descriptor)
        return _ok(**payload)

    @server.tool()
    async def list_channels(device: str | None = None) -> dict:
        """List the device's read/write channels with their units and limits."""
        try:
            target = await app.device(device)
            return _ok(channels=target.channel_table().to_list())
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def read_channel(channel: str, device: str | None = None) -> dict:
        """Read one channel by name (the MHS read primitive), e.g. "nozzle.temperature"."""
        try:
            target = await app.device(device)
            value = await target.read(channel)
        except MHSError as exc:
            return _fail(exc)
        entry = target.channel_table().get(channel)
        if entry.value_type == "binary":
            return _ok(channel=channel, value_type="binary", bytes=len(value),
                       hint="Use capture_snapshot or get_map to see the image itself.")
        return _ok(channel=channel, value=value, unit=entry.unit)

    @server.tool()
    async def write_channel(
        channel: str, value: Any, confirm: bool = False, device: str | None = None
    ) -> dict:
        """Write one channel by name (the MHS write primitive).

        The value is range-checked against the channel's declared limits in the
        driver before it reaches the hardware; channels that move or heat the
        machine also require confirm=true.
        """
        try:
            app.require_writable(f"write {channel}")
            target = await app.device(device)
            result = await target.write(channel, value, confirm=confirm)
        except MHSError as exc:
            return _fail(exc)
        return _ok(channel=channel, value=value, result=result)

    # ------------------------------------------------------------- slicing
    def _slice_overrides(
        layer_height_mm: float | None,
        infill_percent: float | None,
        wall_loops: int | None,
        supports: bool | None,
        brim_width_mm: float | None,
        perimeter_speed_mm_s: float | None,
        infill_speed_mm_s: float | None,
    ) -> SliceSettings:
        return SliceSettings(
            layer_height_mm=layer_height_mm,
            infill_percent=infill_percent,
            wall_loops=wall_loops,
            supports=supports,
            brim_width_mm=brim_width_mm,
            perimeter_speed_mm_s=perimeter_speed_mm_s,
            infill_speed_mm_s=infill_speed_mm_s,
        )

    @server.tool()
    async def list_slice_intents(printer: str | None = None) -> dict:
        """The named slicing trade-offs, and what each resolves to on this printer.

        Layer heights are derived from the machine's own nozzle and layer-height
        limits, so the same intent means something different on a 0.2 mm nozzle
        than on a 0.4 mm one.
        """
        spec = spec_for(app.settings.get(printer).model if app.settings.devices else None)
        out = []
        for name, intent in INTENTS.items():
            settings, _ = settings_for_intent(name, spec)
            out.append({**intent.to_dict(), "resolves_to": settings.to_dict()})
        return _ok(printer=spec.model, intents=out,
                   note="Any individual setting can still be overridden alongside the intent.")

    @server.tool()
    async def slice_model(
        path: str,
        intent: str = "balanced",
        printer: str | None = None,
        output_path: str | None = None,
        layer_height_mm: float | None = None,
        infill_percent: float | None = None,
        wall_loops: int | None = None,
        supports: bool | None = None,
        brim_width_mm: float | None = None,
        perimeter_speed_mm_s: float | None = None,
        infill_speed_mm_s: float | None = None,
    ) -> dict:
        """Slice a mesh into a file the printer can run.

        `intent` picks the trade-off - draft, speed, balanced, quality, fine or
        strong (see `list_slice_intents`) - and any named setting overrides it.
        Returns the slicer's own estimate of time and filament, and what it
        actually applied, which is how a wrong profile becomes visible before
        anything is printed.
        """
        try:
            file = _model_path(path)
            spec = spec_for(app.settings.get(printer).model if app.settings.devices else None)
            overrides = _slice_overrides(
                layer_height_mm, infill_percent, wall_loops, supports, brim_width_mm,
                perimeter_speed_mm_s, infill_speed_mm_s,
            )
            settings, chosen = settings_for_intent(intent, spec, overrides=overrides)
            slicer = app.slicer()
            target = (
                Path(output_path).expanduser() if output_path
                else app.slice_path(
                    file.stem + f"-{chosen.name}",
                    ".gcode" if slicer.flavour.value == "prusaslicer" else ".gcode.3mf",
                )
            )
            result = await asyncio.to_thread(slicer.slice, file, target, settings)
        except MHSError as exc:
            return _fail(exc)
        return _ok(
            summary=result.summary(),
            intent=chosen.to_dict(),
            slice=result.to_dict(),
            next_step="upload_and_print(local_path=...) to print it, "
                      "or compare_slice_intents to weigh the alternatives.",
        )

    @server.tool()
    async def compare_slice_intents(
        path: str,
        intents: list[str] | None = None,
        printer: str | None = None,
    ) -> dict:
        """Slice the same model several ways and report the trade-off in real numbers.

        This is how to answer "is quality worth it here?" - the slicer's own
        time and filament estimates for each intent, side by side, rather than
        a guess. Slicing is a file operation; nothing is sent to the printer.
        """
        chosen = intents or ["draft", "balanced", "quality"]
        try:
            file = _model_path(path)
            spec = spec_for(app.settings.get(printer).model if app.settings.devices else None)
            slicer = app.slicer()
        except MHSError as exc:
            return _fail(exc)

        rows, failures = [], []
        for name in chosen:
            try:
                settings, meta = settings_for_intent(name, spec)
                target = app.slice_path(
                    f"{file.stem}-{name}",
                    ".gcode" if slicer.flavour.value == "prusaslicer" else ".gcode.3mf",
                )
                result = await asyncio.to_thread(slicer.slice, file, target, settings)
            except MHSError as exc:
                failures.append({"intent": name, "error": exc.message})
                continue
            rows.append({
                "intent": name,
                "summary": meta.summary,
                "trade": meta.trade,
                "minutes": result.estimated_time_minutes,
                "filament_cm3": result.filament_cm3,
                "layer_height_mm": settings.layer_height_mm,
                "infill_percent": settings.infill_percent,
                "wall_loops": settings.wall_loops,
                "output_path": str(result.output_path),
            })
        if not rows:
            return _fail(CommandRejected(
                "every intent failed to slice",
                hint="; ".join(f["error"] for f in failures) or None,
            ))

        timed = [r for r in rows if r["minutes"]]
        verdict = None
        if len(timed) > 1:
            fastest = min(timed, key=lambda r: r["minutes"])
            slowest = max(timed, key=lambda r: r["minutes"])
            verdict = (
                f"{slowest['intent']} takes {slowest['minutes']} min against "
                f"{fastest['intent']}'s {fastest['minutes']} min - "
                f"{slowest['minutes'] / fastest['minutes']:.1f}x the time for "
                f"{slowest['layer_height_mm']:g} mm layers instead of "
                f"{fastest['layer_height_mm']:g} mm."
            )
        return _ok(model=file.name, comparison=rows, failed=failures, verdict=verdict)

    @server.tool()
    async def slice_and_print(
        path: str,
        confirm: bool = False,
        intent: str = "balanced",
        printer: str | None = None,
        layer_height_mm: float | None = None,
        infill_percent: float | None = None,
        wall_loops: int | None = None,
        supports: bool | None = None,
    ) -> dict:
        """Slice a mesh, upload it and start printing, in one step.

        The whole chain from a .stl to a running print. Requires `confirm=True`;
        the slice happens first either way, so a dry run still tells you what it
        would cost in time and filament.
        """
        sliced = await slice_model(
            path=path, intent=intent, printer=printer, layer_height_mm=layer_height_mm,
            infill_percent=infill_percent, wall_loops=wall_loops, supports=supports,
        )
        if not sliced.get("ok"):
            return sliced
        estimate = sliced["slice"]
        if not confirm:
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": f"Would print {Path(path).name} at intent '{intent}': "
                           f"{estimate.get('estimated_time') or 'unknown time'}, "
                           f"{estimate.get('filament_cm3') or '?'} cm3 of filament.",
                "hint": "Call again with confirm=true once the plate is clear.",
                "slice": estimate,
            }
        started = await upload_and_print(
            local_path=estimate["output_path"], confirm=True, printer=printer,
            job_name=f"{Path(path).stem} ({intent})",
        )
        started["slice"] = estimate
        return started

    # ------------------------------------------------------------- vacuums
    async def _resolve_rooms(device: Vacuum, wanted: list) -> list[int]:
        """Turn a mixed list of room names and segment ids into segment ids.

        People say "the kitchen"; the robot only knows segment 16. Matching is
        case-insensitive and accepts a partial name, because "kitchen" should
        find "Kitchen" and "Main Kitchen".
        """
        rooms = await device.list_rooms()
        by_id = {room.segment_id: room for room in rooms}
        resolved: list[int] = []
        for entry in wanted:
            if isinstance(entry, int) or str(entry).strip().isdigit():
                segment = int(entry)
                if segment not in by_id:
                    raise CommandRejected(
                        f"no mapped room with segment id {segment}",
                        hint="Rooms: " + _room_list(rooms),
                    )
                resolved.append(segment)
                continue
            text = str(entry).strip().lower()
            matches = [r for r in rooms if r.name and r.name.strip().lower() == text]
            if not matches:
                matches = [r for r in rooms if r.name and text in r.name.strip().lower()]
            if not matches:
                raise CommandRejected(
                    f"no mapped room called {entry!r}", hint="Rooms: " + _room_list(rooms)
                )
            if len(matches) > 1:
                raise CommandRejected(
                    f"{entry!r} matches several rooms: "
                    + ", ".join(m.label for m in matches),
                    hint="Name it exactly, or pass the segment id.",
                )
            resolved.append(matches[0].segment_id)
        return resolved

    def _room_list(rooms) -> str:
        return ", ".join(f"{r.label} (id {r.segment_id})" for r in rooms) or "none mapped yet"

    @server.tool()
    async def vacuum_status(vacuum: str | None = None) -> dict:
        """What the robot is doing: state, battery, suction, position, errors."""
        try:
            device = await _vacuum(vacuum)
            status = await device.status()
        except MHSError as exc:
            return _fail(exc)
        return _ok(summary=status.summary(), status=status.to_dict())

    @server.tool()
    async def list_rooms(vacuum: str | None = None) -> dict:
        """The rooms the robot has mapped, with the segment ids used to clean them.

        Names come from the Roborock app; a room the app has not named shows as
        its segment id only.
        """
        try:
            device = await _vacuum(vacuum)
            rooms = await device.list_rooms()
        except MHSError as exc:
            return _fail(exc)
        return _ok(rooms=[r.to_dict() for r in rooms], count=len(rooms))

    @server.tool()
    async def get_map(vacuum: str | None = None, save: bool = True) -> Image:
        """The current map, with a millimetre grid, room names, robot and dock.

        Read coordinates straight off the grid and pass them to `go_to` or
        `clean_zone` - the labels are the robot's own frame, not pixels.
        """
        device = await _vacuum(vacuum)
        device.require(Capability.MAP)
        snapshot = await device.map_snapshot()
        if save:
            path = app.capture_path(device.device_id, "map").with_suffix(".png")
            path.write_bytes(snapshot.image_png)
            app.store.add_observation(device.device_id, kind="map", image_path=str(path))
        return Image(data=snapshot.image_png, format="png")

    @server.tool()
    async def start_cleaning(
        confirm: bool = False,
        vacuum: str | None = None,
        rooms: list[str] | None = None,
        repeat: int = 1,
        suction: str | None = None,
        water: str | None = None,
    ) -> dict:
        """Start cleaning - the whole map, or just the rooms named in `rooms`.

        `rooms` accepts names ("kitchen") or segment ids; `list_rooms` shows
        both. Requires `confirm=True`.
        """
        if not confirm:
            where = f"the {', '.join(rooms)}" if rooms else "the whole mapped area"
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": f"Would start cleaning {where}.",
                "hint": "Call again with confirm=true.",
            }
        try:
            app.require_writable("start cleaning")
            device = await _vacuum(vacuum)
            options = CleanOptions(repeat=repeat, fan_power=suction, water_level=water)
            if rooms:
                device.require(Capability.ROOM_CLEAN)
                segments = await _resolve_rooms(device, rooms)
                result = await device.clean_rooms(segments, options)
                result["rooms"] = segments
            else:
                result = await device.start_clean(options)
            run_id = app.store.start_run(
                device.device_id,
                job_name=", ".join(rooms) if rooms else "whole map",
                settings=options.to_dict(),
            )
        except MHSError as exc:
            return _fail(exc)
        return _ok(result=result, run_id=run_id)

    @server.tool()
    async def go_to(
        confirm: bool = False,
        vacuum: str | None = None,
        location: str | None = None,
        x_mm: float | None = None,
        y_mm: float | None = None,
    ) -> dict:
        """Send the robot to a point and have it stop there - it does not clean.

        Give either a saved `location` name (see `save_location`), a room name,
        or `x_mm`/`y_mm` read off `get_map`. Requires `confirm=True`.
        """
        try:
            device = await _vacuum(vacuum)
            target, label = await _resolve_point(device, location, x_mm, y_mm)
        except MHSError as exc:
            return _fail(exc)

        if not confirm:
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": f"Would drive to {label} at ({target[0]:.0f}, {target[1]:.0f}) mm.",
                "hint": "Call again with confirm=true.",
            }
        try:
            app.require_writable("move the robot")
            device.require(Capability.GO_TO)
            result = await device.go_to(*target)
        except MHSError as exc:
            return _fail(exc)
        return _ok(result=result, target={"x_mm": target[0], "y_mm": target[1], "label": label})

    async def _resolve_point(
        device: Vacuum, location: str | None, x_mm: float | None, y_mm: float | None
    ) -> tuple[tuple[float, float], str]:
        """A saved location, a room centre, or explicit coordinates - in that order."""
        if x_mm is not None and y_mm is not None:
            return (float(x_mm), float(y_mm)), "those coordinates"
        if not location:
            raise CommandRejected(
                "give a location name or both x_mm and y_mm",
                hint="Call get_map to read coordinates, or save_location to name a spot.",
            )
        saved = app.store.get_location(device.device_id, location)
        if saved:
            return (saved["x_mm"], saved["y_mm"]), f"saved location {location!r}"
        rooms = await device.list_rooms()
        text = location.strip().lower()
        for room in rooms:
            if room.name and text in room.name.strip().lower() and room.center:
                return (room.center.x_mm, room.center.y_mm), f"the middle of {room.label}"
        known = [entry["name"] for entry in app.store.list_locations(device.device_id)]
        raise CommandRejected(
            f"nothing called {location!r} is saved or mapped",
            hint=f"Saved: {', '.join(known) or 'none'}. Rooms: {_room_list(rooms)}",
        )

    @server.tool()
    async def clean_zone(
        x1_mm: float, y1_mm: float, x2_mm: float, y2_mm: float,
        confirm: bool = False, vacuum: str | None = None, repeat: int = 1,
    ) -> dict:
        """Clean one rectangle, given as two opposite corners in map millimetres.

        Use it for a spill: read the corners off `get_map`. Requires `confirm=True`.
        """
        if not confirm:
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": f"Would clean the rectangle ({x1_mm:.0f}, {y1_mm:.0f}) to "
                           f"({x2_mm:.0f}, {y2_mm:.0f}) mm.",
                "hint": "Call again with confirm=true.",
            }
        try:
            app.require_writable("start cleaning")
            device = await _vacuum(vacuum)
            device.require(Capability.ZONE_CLEAN)
            result = await device.clean_zone(
                [(x1_mm, y1_mm, x2_mm, y2_mm)], CleanOptions(repeat=repeat)
            )
        except MHSError as exc:
            return _fail(exc)
        return _ok(result=result)

    @server.tool()
    async def pause_cleaning(vacuum: str | None = None) -> dict:
        """Pause the run. `resume_cleaning` picks it up where it stopped."""
        try:
            app.require_writable("pause the robot")
            return _ok(result=await (await _vacuum(vacuum)).pause())
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def resume_cleaning(vacuum: str | None = None) -> dict:
        """Resume a paused run."""
        try:
            app.require_writable("resume the robot")
            return _ok(result=await (await _vacuum(vacuum)).resume())
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def stop_cleaning(confirm: bool = False, vacuum: str | None = None) -> dict:
        """Stop the run and stay put. The job cannot be resumed afterwards."""
        if not confirm:
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "message": "Stopping ends the run; it cannot be resumed.",
                "hint": "Call again with confirm=true, or use pause_cleaning instead.",
            }
        try:
            app.require_writable("stop the robot")
            return _ok(result=await (await _vacuum(vacuum)).stop())
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def return_to_dock(vacuum: str | None = None) -> dict:
        """Send the robot home to charge."""
        try:
            app.require_writable("send the robot to its dock")
            device = await _vacuum(vacuum)
            device.require(Capability.RETURN_TO_DOCK)
            return _ok(result=await device.return_to_dock())
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def find_vacuum(vacuum: str | None = None) -> dict:
        """Make the robot announce itself, for when it is stuck under something."""
        try:
            app.require_writable("make the robot speak")
            device = await _vacuum(vacuum)
            device.require(Capability.LOCATE)
            return _ok(result=await device.locate())
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def set_suction(level: str, vacuum: str | None = None) -> dict:
        """Set the suction preset. `vacuum_status` and the descriptor list the options."""
        try:
            app.require_writable("change suction")
            device = await _vacuum(vacuum)
            return _ok(result=await device.set_fan_power(level),
                       options=list(device.fan_power_options))
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def set_water_flow(level: str, vacuum: str | None = None) -> dict:
        """Set the mop's water flow."""
        try:
            app.require_writable("change water flow")
            device = await _vacuum(vacuum)
            return _ok(result=await device.set_water_level(level),
                       options=list(device.water_level_options))
        except MHSError as exc:
            return _fail(exc)

    @server.tool()
    async def save_location(
        name: str, x_mm: float, y_mm: float, vacuum: str | None = None, note: str | None = None
    ) -> dict:
        """Remember a point by name, so it can be reused with `go_to`.

        Coordinates come from `get_map`. Useful for the spots a map has no name
        for: under the dining table, the cat's water bowl, the back door.
        """
        try:
            device = await _vacuum(vacuum)
            saved = app.store.save_location(device.device_id, name, x_mm, y_mm, note)
        except (MHSError, ValueError) as exc:
            return _fail(exc) if isinstance(exc, MHSError) else _fail(CommandRejected(str(exc)))
        return _ok(location=saved)

    @server.tool()
    async def list_locations(vacuum: str | None = None) -> dict:
        """Saved points for this robot."""
        try:
            device = await _vacuum(vacuum)
        except MHSError as exc:
            return _fail(exc)
        return _ok(locations=app.store.list_locations(device.device_id))

    @server.tool()
    async def delete_location(name: str, vacuum: str | None = None) -> dict:
        """Forget a saved point."""
        try:
            device = await _vacuum(vacuum)
        except MHSError as exc:
            return _fail(exc)
        if not app.store.delete_location(device.device_id, name):
            return {"ok": False, "error": "NotFound", "message": f"no saved location {name!r}"}
        return _ok(deleted=name)

    # ------------------------------------------------------------- resources
    @server.resource("mhs://devices", mime_type="application/json")
    async def devices_resource() -> dict:
        """Every configured device."""
        return await list_devices()

    @server.resource("mhs://printers", mime_type="application/json")
    async def printers_resource() -> dict:
        """Previous spelling of mhs://devices."""
        return await list_devices()

    @server.resource("mhs://device/{device_id}/status", mime_type="application/json")
    async def device_status_resource(device_id: str) -> dict:
        """Live status of one device, whatever kind it is."""
        target = await app.device(device_id)
        status = await target.status()
        return _ok(summary=status.summary(), status=status.to_dict())

    @server.resource("mhs://printer/{device_id}/status", mime_type="application/json")
    async def status_resource(device_id: str) -> dict:
        """Live status of one printer."""
        return await get_status(device_id)

    @server.resource("mhs://reference-objects", mime_type="application/json")
    async def reference_objects_resource() -> dict:
        """Known objects and their real sizes, for calibrating the camera."""
        return {
            "objects": [
                {"name": name, "size_mm": size, "label": label}
                for name, (size, label) in sorted(vision.REFERENCE_OBJECTS.items())
            ],
            "usage": "Put one flat on the plate, call camera_grid, read its span in pixels, "
                     "then camera_calibrate(reference=<name>, pixel_length=<px>).",
        }

    @server.resource("mhs://device/{device_id}/descriptor", mime_type="application/json")
    async def descriptor_resource(device_id: str) -> dict:
        """MHS device descriptor for one device."""
        return await describe_device(device_id, as_markdown=False)

    @server.resource("mhs://printer/{device_id}/descriptor", mime_type="application/json")
    async def printer_descriptor_resource(device_id: str) -> dict:
        """Previous spelling of mhs://device/{id}/descriptor."""
        return await describe_device(device_id, as_markdown=False)

    @server.resource("mhs://printer/{device_id}/history", mime_type="application/json")
    async def history_resource(device_id: str) -> dict:
        """Recent prints on one printer."""
        return await list_print_history(device_id)

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

    @server.prompt()
    def design_iteration(goal: str = "a part sized to match a reference object") -> str:
        """Run the full design-print-measure loop."""
        return (
            f"Goal: {goal}.\n"
            "1. analyze_model and preview_model - look at the render and say what you see.\n"
            "2. Check the printability findings: which detail is below the printer's "
            "resolution at this size?\n"
            "3. If it needs resizing, camera_grid + camera_calibrate against a known object, "
            "measure the reference, then scale_model (dry run first, then apply).\n"
            "4. Re-run analyze_model on the scaled file and say what the scaling cost in detail.\n"
            "5. After slicing and printing, camera_grid + camera_measure the result against the "
            "target, then log_print_result with a score and what to change next time.\n"
            "Be explicit about measurement uncertainty, and never start a print without "
            "confirming the plate is clear."
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
