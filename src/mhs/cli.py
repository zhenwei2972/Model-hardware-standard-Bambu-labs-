"""Command line front-end.

Same capabilities as the MCP server, minus the model: useful for verifying a
printer works before wiring Claude to it, and for running the scheduler as a
standalone daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import socket
import sys
import time
from pathlib import Path

from . import __version__
from .app import MHSApp
from .config import load_settings
from .design import mesh as meshlib
from .design import vision
from .design.printability import analyze as analyze_print
from .design.printability import scale_advice
from .design.render import render_to_png
from .errors import MHSError
from .models import PrintOptions
from .scheduler import parse_when
from .specs import spec_for
from .standard.descriptor import descriptor_markdown
from .store import iso


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


async def _printer(app, args):
    """Resolve a printer, refusing another kind of device with a clear message.

    Without this a printer command run against a vacuum crashes on a missing
    field instead of saying what is wrong.
    """
    from .printer import Printer

    device = await app.device(args.printer)
    if not isinstance(device, Printer):
        raise MHSError(
            f"{device.device_id} is a {device.device_kind.replace('_', ' ')}, not a printer",
            hint="Use the vacuum commands (mhs vacuum, rooms, clean, goto, map), "
                 "or name a printer with --printer.",
        )
    return device


async def _with_app(args, coro_factory) -> int:
    settings = load_settings(args.config)
    app = MHSApp(settings)
    await app.startup(run_scheduler=False)
    try:
        return await coro_factory(app)
    except MHSError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint:  {exc.hint}", file=sys.stderr)
        return 1
    finally:
        await app.shutdown()


# -- commands --------------------------------------------------------------
async def cmd_status(app, args) -> int:
    printer = await _printer(app, args)
    status = await printer.status()
    if args.json:
        _print_json(status.to_dict())
    else:
        print(status.summary())
        for alert in status.alerts:
            print(f"  ! {alert.code} ({alert.severity}) {alert.message or ''} {alert.url or ''}")
    return 0


async def cmd_watch(app, args) -> int:
    printer = await _printer(app, args)
    try:
        while True:
            status = await printer.status()
            print(f"\r{time.strftime('%H:%M:%S')}  {status.summary():<110}", end="", flush=True)
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


async def cmd_files(app, args) -> int:
    printer = await _printer(app, args)
    files = await printer.list_files(args.directory)
    if args.json:
        _print_json([f.to_dict() for f in files])
    else:
        for entry in files:
            size = f"{entry.size_bytes/1_048_576:.1f} MB" if entry.size_bytes else "-"
            print(f"{'d' if entry.is_dir else '-'} {size:>10}  {entry.path}")
    return 0


async def cmd_upload(app, args) -> int:
    printer = await _printer(app, args)
    entry = await printer.upload_file(args.local_path, args.name)
    print(f"uploaded {entry.path} ({entry.size_bytes or 0} bytes)")
    return 0


async def cmd_print(app, args) -> int:
    printer = await _printer(app, args)
    if not args.yes:
        answer = input(f"Start {args.file} on {printer.device_id}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled")
            return 1
    options = PrintOptions(
        plate=args.plate,
        use_ams=args.use_ams,
        ams_mapping=args.ams_slots,
        timelapse=args.timelapse,
        job_name=args.name,
    )
    result = await printer.start_print(args.file, options)
    run_id = app.store.start_run(
        printer.device_id, job_name=options.job_name or args.file, remote_path=args.file,
        settings=options.to_dict(),
    )
    _print_json({"run_id": run_id, **result})
    return 0 if result.get("acknowledged", True) else 2


async def cmd_simple(app, args) -> int:
    printer = await _printer(app, args)
    action = {
        "pause": printer.pause_print,
        "resume": printer.resume_print,
        "stop": printer.stop_print,
    }[args.command]
    if args.command == "stop" and not args.yes:
        answer = input(f"Abort the print on {printer.device_id}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled")
            return 1
    _print_json(await action())
    return 0


async def cmd_light(app, args) -> int:
    printer = await _printer(app, args)
    _print_json(await printer.set_light(args.state == "on"))
    return 0


async def cmd_snapshot(app, args) -> int:
    printer = await _printer(app, args)
    if args.count == 1:
        frame = await printer.snapshot()
        out = Path(args.output) if args.output else app.capture_path(printer.device_id)
        out.write_bytes(frame)
        print(out)
        return 0
    for index in range(args.count):
        if index:
            await asyncio.sleep(args.interval)
        frame = await printer.snapshot()
        out = app.capture_path(printer.device_id, f"series-{index:03d}")
        out.write_bytes(frame)
        print(out)
    return 0


async def cmd_schedule(app, args) -> int:
    printer = await _printer(app, args)
    start_at = parse_when(args.when)
    job = app.store.add_job(
        printer.device_id,
        args.file,
        start_at,
        options=PrintOptions(plate=args.plate, use_ams=args.use_ams, job_name=args.name).to_dict(),
        window_minutes=args.window,
        note=args.note,
    )
    print(f"{job.id}: {args.file} at {iso(job.start_at)} (window {job.window_minutes} min)")
    print("Run `mhs scheduler` (or keep the MCP server running) so the job can fire.")
    return 0


async def cmd_jobs(app, args) -> int:
    jobs = app.store.list_jobs(device_id=args.printer, status=args.status)
    if args.json:
        _print_json([j.to_dict() for j in jobs])
    else:
        for job in jobs:
            print(f"{job.id}  {job.status:<9} {iso(job.start_at)}  {job.device_id}  {job.remote_path}"
                  + (f"  ({job.last_error})" if job.last_error else ""))
    return 0


async def cmd_cancel(app, args) -> int:
    job = app.store.cancel_job(args.job_id)
    if job is None:
        print(f"no job {args.job_id}", file=sys.stderr)
        return 1
    print(f"{job.id}: {job.status}")
    return 0


async def cmd_history(app, args) -> int:
    runs = app.store.list_runs(device_id=args.printer, limit=args.limit)
    if args.json:
        _print_json(runs)
    else:
        for run in runs:
            score = f"{run['quality_score']}/10" if run["quality_score"] else "-"
            print(f"#{run['id']:<4} {run['started_at_iso']}  {run['outcome']:<11} {score:>5}  "
                  f"{run['job_name'] or run['remote_path'] or ''}")
    return 0


async def cmd_scheduler(app, args) -> int:
    await app.scheduler.start()
    print(f"scheduler running (poll {app.scheduler.poll_interval:.0f}s). Ctrl-C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        return 0


async def cmd_doctor(app, args) -> int:
    config = app.settings.get(args.printer)
    print(f"printer '{config.device_id}' driver={config.driver} model={config.model}")
    ok = True
    if config.driver == "bambu":
        print(f"  host {config.host}  serial {config.serial[:4]}***  tls={config.tls_mode}")
        ports = (("mqtt", config.mqtt_port), ("ftps", config.ftps_port), ("camera", config.camera_port))
        for label, port in ports:
            reachable = _probe(config.host, port)
            ok &= reachable
            extra = "" if reachable else "  <- closed: LAN Only Mode / Developer Mode off, or firewalled"
            print(f"  tcp/{port:<5} {label:<7} {'open' if reachable else 'closed'}{extra}")
    # Connecting can fail on its own, and a diagnostic that stops at the first
    # failure is the least useful kind: report it and keep going, so one run
    # shows every transport's state rather than only the first broken one.
    try:
        device = await _printer(app, args)
    except MHSError as exc:
        ok = False
        print(f"  {'connect':<10} FAIL  {exc.message}")
        if exc.hint:
            print(f"             hint: {exc.hint}")
        _doctor_verdict(ok, unreachable=True)
        return 1

    for label, probe in (
        ("telemetry", device.status),
        ("files", device.list_files),
        ("camera", device.snapshot),
    ):
        try:
            result = await probe()
            detail = result.summary() if hasattr(result, "summary") else f"{len(result)} items/bytes"
            print(f"  {label:<10} ok    {detail}")
        except MHSError as exc:
            ok = False
            print(f"  {label:<10} FAIL  {exc.message}")
            if exc.hint:
                print(f"             hint: {exc.hint}")
        except Exception as exc:  # a driver bug should not hide the other checks
            ok = False
            print(f"  {label:<10} ERROR {type(exc).__name__}: {exc}")
    _doctor_verdict(ok)
    return 0 if ok else 1


def _doctor_verdict(ok: bool, unreachable: bool = False) -> None:
    if ok:
        print("all good")
        return
    print("some checks failed - see hints above")
    if unreachable:
        print(
            "\nNothing answered. In order of likelihood:\n"
            "  1. This machine is not on the same network as the printer. Check with:\n"
            "       ping <printer ip>\n"
            "  2. The network has client isolation on (common on guest and cafe Wi-Fi),\n"
            "     which blocks device-to-device traffic no matter what the printer says.\n"
            "  3. The printer is asleep or the IP has changed - re-check Settings > WLAN."
        )


# -- model-side commands ---------------------------------------------------
async def cmd_inspect(app, args) -> int:
    model = meshlib.load_mesh(args.path)
    spec = spec_for(app.settings.get(args.printer).model if app.settings.devices else None)
    report = analyze_print(model, spec, nozzle_mm=args.nozzle, layer_height_mm=args.layer_height)
    if args.json:
        _print_json({"model": model.summary(), "printability": report.to_dict()})
        return 0 if report.printable else 2

    dims = model.dimensions
    print(f"{Path(args.path).name}: {dims[0]:.2f} x {dims[1]:.2f} x {dims[2]:.2f} mm, "
          f"{model.volume_mm3 / 1000:.2f} cm3, {model.triangle_count:,} triangles"
          f"{'' if model.is_watertight() else ', NOT watertight'}")
    print(report.verdict())
    for finding in report.findings:
        marker = {"blocker": "!!", "warning": " !", "note": "  "}[finding.severity]
        print(f" {marker} {finding.message}")
        if finding.suggestion:
            print(f"      -> {finding.suggestion}")
    return 0 if report.printable else 2


async def cmd_preview(app, args) -> int:
    model = meshlib.load_mesh(args.path)
    target = Path(args.output) if args.output else (
        app.settings.capture_dir / "models" / f"{Path(args.path).stem}-preview.png"
    )
    render_to_png(model, target, views=tuple(args.views), size=args.size,
                  title=args.title or Path(args.path).name)
    print(target)
    return 0


async def cmd_scale(app, args) -> int:
    model = meshlib.load_mesh(args.path)
    spec = spec_for(app.settings.get(args.printer).model if app.settings.devices else None)
    advice = scale_advice(model, spec, args.target_mm, args.axis)
    print(f"{advice['factor']:.4f}x -> "
          f"{advice['dimensions_after_mm'][0]:.2f} x {advice['dimensions_after_mm'][1]:.2f} x "
          f"{advice['dimensions_after_mm'][2]:.2f} mm, ~{advice['estimated_mass_g_after']:.1f} g")
    print(advice["note"])
    if not args.apply:
        print("(dry run - pass --apply to write the scaled STL)")
        return 0
    scaled, _factor = model.scale_to_dimension(args.target_mm, args.axis)
    target = Path(args.output) if args.output else (
        app.settings.capture_dir / "models" / f"{Path(args.path).stem}-{args.target_mm:g}mm.stl"
    )
    print(meshlib.save_stl(scaled, target))
    return 0


# -- slicing commands ------------------------------------------------------
async def cmd_slice(app, args) -> int:
    from .slicing import INTENTS, SliceSettings, settings_for_intent

    spec = spec_for(app.settings.get(args.printer).model if app.settings.devices else None)
    if args.list_intents:
        for name, intent in INTENTS.items():
            resolved, _ = settings_for_intent(name, spec)
            print(f"{name:9} {intent.summary}")
            print(f"          {resolved.to_dict()}")
            if intent.trade:
                print(f"          trade: {intent.trade}")
        return 0

    overrides = SliceSettings(
        layer_height_mm=args.layer_height,
        infill_percent=args.infill,
        wall_loops=args.walls,
        supports=args.supports,
    )
    slicer = app.slicer()
    suffix = ".gcode" if slicer.flavour.value == "prusaslicer" else ".gcode.3mf"

    names = args.compare or [args.intent]
    rows = []
    for name in names:
        settings, meta = settings_for_intent(name, spec, overrides=overrides)
        target = Path(args.output) if (args.output and len(names) == 1) else app.slice_path(
            f"{Path(args.path).stem}-{name}", suffix
        )
        result = slicer.slice(args.path, target, settings)
        rows.append((name, meta, result))
        print(f"{name:9} {result.summary()}")
        print(f"          -> {result.output_path}")

    if len(rows) > 1:
        timed = [(n, r) for n, _m, r in rows if r.estimated_time_minutes]
        if len(timed) > 1:
            fast = min(timed, key=lambda t: t[1].estimated_time_minutes)
            slow = max(timed, key=lambda t: t[1].estimated_time_minutes)
            ratio = slow[1].estimated_time_minutes / fast[1].estimated_time_minutes
            print(f"\n{slow[0]} is {ratio:.1f}x the time of {fast[0]}.")
    return 0


# -- vacuum commands -------------------------------------------------------
async def _vacuum(app, args):
    from .vacuum import Vacuum

    device = await app.device(args.printer)
    if not isinstance(device, Vacuum):
        raise MHSError(f"{device.device_id} is not a robot vacuum")
    return device


async def cmd_vacuum(app, args) -> int:
    device = await _vacuum(app, args)
    status = await device.status()
    if args.json:
        _print_json(status.to_dict())
    else:
        print(status.summary())
    return 0


async def cmd_rooms(app, args) -> int:
    device = await _vacuum(app, args)
    rooms = await device.list_rooms()
    if args.json:
        _print_json([r.to_dict() for r in rooms])
    else:
        for room in rooms:
            centre = f"  centre ({room.center.x_mm:.0f}, {room.center.y_mm:.0f}) mm" if room.center else ""
            area = f"  {room.area_m2} m2" if room.area_m2 else ""
            print(f"{room.segment_id:>4}  {room.label:<18}{area}{centre}")
    return 0


async def cmd_map(app, args) -> int:
    device = await _vacuum(app, args)
    snapshot = await device.map_snapshot()
    target = Path(args.output) if args.output else (
        app.capture_path(device.device_id, "map").with_suffix(".png")
    )
    target.write_bytes(snapshot.image_png)
    print(f"{target}  ({snapshot.width}x{snapshot.height})")
    for room in snapshot.rooms:
        if room.center:
            print(f"  {room.label:<18} ({room.center.x_mm:.0f}, {room.center.y_mm:.0f}) mm")
    return 0


async def cmd_clean(app, args) -> int:
    from .models import CleanOptions

    device = await _vacuum(app, args)
    options = CleanOptions(repeat=args.repeat, fan_power=args.suction, water_level=args.water)
    where = ", ".join(args.rooms) if args.rooms else "the whole mapped area"
    if not args.yes:
        answer = input(f"Clean {where} with {device.device_id}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled")
            return 1
    if args.rooms:
        rooms = await device.list_rooms()
        segments = []
        for wanted in args.rooms:
            text = str(wanted).strip().lower()
            if text.isdigit():
                segments.append(int(text))
                continue
            match = next((r for r in rooms if r.name and text in r.name.lower()), None)
            if match is None:
                print(f"no mapped room matching {wanted!r}", file=sys.stderr)
                return 1
            segments.append(match.segment_id)
        _print_json(await device.clean_rooms(segments, options))
    else:
        _print_json(await device.start_clean(options))
    return 0


async def cmd_goto(app, args) -> int:
    device = await _vacuum(app, args)
    if args.x is not None and args.y is not None:
        target, label = (args.x, args.y), "those coordinates"
    elif args.location:
        saved = app.store.get_location(device.device_id, args.location)
        if saved:
            target, label = (saved["x_mm"], saved["y_mm"]), f"saved location {args.location!r}"
        else:
            rooms = await device.list_rooms()
            text = args.location.strip().lower()
            match = next((r for r in rooms if r.name and text in r.name.lower() and r.center), None)
            if match is None:
                print(f"nothing called {args.location!r} is saved or mapped", file=sys.stderr)
                return 1
            target, label = (match.center.x_mm, match.center.y_mm), f"the middle of {match.label}"
    else:
        print("give a location name, or --x and --y", file=sys.stderr)
        return 1

    if not args.yes:
        answer = input(f"Send {device.device_id} to {label} "
                       f"({target[0]:.0f}, {target[1]:.0f}) mm? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled")
            return 1
    _print_json(await device.go_to(*target))
    return 0


async def cmd_dock(app, args) -> int:
    device = await _vacuum(app, args)
    _print_json(await device.return_to_dock())
    return 0


async def cmd_locations(app, args) -> int:
    device = await _vacuum(app, args)
    if args.save:
        name, x_mm, y_mm = args.save
        saved = app.store.save_location(device.device_id, name, float(x_mm), float(y_mm))
        print(f"saved {saved['name']!r} at ({saved['x_mm']:.0f}, {saved['y_mm']:.0f}) mm")
        return 0
    if args.forget:
        ok = app.store.delete_location(device.device_id, args.forget)
        print(f"{'forgot' if ok else 'no such location:'} {args.forget}")
        return 0 if ok else 1
    for entry in app.store.list_locations(device.device_id):
        note = f"  - {entry['note']}" if entry["note"] else ""
        print(f"{entry['name']:<20} ({entry['x_mm']:.0f}, {entry['y_mm']:.0f}) mm{note}")
    return 0


def cmd_roborock_login(args) -> int:
    """Exchange an account password (or an emailed code) for a stored token."""
    import asyncio as _asyncio
    import getpass

    from .config import load_settings
    from .drivers.roborock import client as rr

    settings = load_settings(args.config)
    email = args.email or input("Roborock account email: ").strip()
    target = Path(args.output).expanduser() if args.output else (
        settings.state_dir / rr.DEFAULT_CREDENTIALS_NAME
    )

    async def run() -> int:
        if args.code:
            await rr.request_email_code(email, args.base_url)
            print(f"A sign-in code has been emailed to {email}.")
            user_data = await rr.login_with_code(email, input("Code: ").strip(), args.base_url)
        else:
            # Read the password without echoing it, and never keep it: only the
            # token it is exchanged for is written to disk.
            user_data = await rr.login(email, getpass.getpass("Password: "), args.base_url)
        path = rr.save_credentials(target, email, user_data, args.base_url)
        print(f"token saved to {path} (mode 600)")
        print("Add this to config.toml:\n")
        print(f'[devices.saros]\ndriver = "roborock"\nmodel = "Saros 10"\nemail = "{email}"')
        return 0

    try:
        return _asyncio.run(run())
    except MHSError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint:  {exc.hint}", file=sys.stderr)
        return 1


# -- MHS standard commands -------------------------------------------------
async def cmd_describe(app, args) -> int:
    printer = await _printer(app, args)
    descriptor = await printer.describe()
    if args.json:
        _print_json(descriptor)
    else:
        print(descriptor_markdown(descriptor))
    if args.output:
        Path(args.output).write_text(
            json.dumps(descriptor, indent=2) if args.json else descriptor_markdown(descriptor)
        )
    return 0


async def cmd_channels(app, args) -> int:
    printer = await _printer(app, args)
    for channel in printer.channel_table().to_list():
        limit = channel["limit"]
        bounds = ""
        if limit and limit["allowed"]:
            bounds = " [" + "|".join(limit["allowed"]) + "]"
        elif limit and (limit["minimum"] is not None or limit["maximum"] is not None):
            bounds = f" [{limit['minimum']}..{limit['maximum']}]"
        print(f"{channel['name']:<22} {channel['access']:<11} {channel['unit'] or '':<8}{bounds}")
    return 0


async def cmd_read(app, args) -> int:
    printer = await _printer(app, args)
    value = await printer.read(args.channel)
    _print_json(value if not isinstance(value, bytes) else {"bytes": len(value)})
    return 0


async def cmd_write(app, args) -> int:
    printer = await _printer(app, args)
    # Numbers arrive as strings on the command line; channels that want text
    # (job.control, light.chamber) keep it.
    value: object = args.value
    with contextlib.suppress(ValueError):
        value = float(args.value)
    _print_json(await printer.write(args.channel, value, confirm=args.yes))
    return 0


# -- camera measurement commands -------------------------------------------
async def cmd_grid(app, args) -> int:
    printer = await _printer(app, args)
    frame = await printer.snapshot()
    raw = app.capture_path(printer.device_id, "grid-source")
    raw.write_bytes(frame)
    app.store.add_observation(printer.device_id, kind="snapshot", image_path=str(raw))
    stored = app.store.get_calibration(printer.device_id)
    calibration = vision.ScaleCalibration.from_dict(stored) if stored else None
    target = Path(args.output) if args.output else (
        app.capture_path(printer.device_id, "grid").with_suffix(".png")
    )
    target.write_bytes(vision.annotate_grid(frame, args.step, calibration))
    print(target)
    return 0


async def cmd_calibrate(app, args) -> int:
    printer = await _printer(app, args)
    calibration = vision.calibrate(args.reference, args.pixels, device_id=printer.device_id)
    app.store.save_calibration(printer.device_id, calibration.to_dict())
    print(f"{calibration.mm_per_pixel:.5f} mm/px via {calibration.reference_name} "
          f"({calibration.reference_mm} mm over {args.pixels:g} px)")
    print(vision.ACCURACY_CAVEAT)
    return 0


async def cmd_measure(app, args) -> int:
    printer = await _printer(app, args)
    source = args.frame or app.store.latest_snapshot(printer.device_id)
    if not source or not Path(source).is_file():
        print("no camera frame yet - run `mhs grid` first", file=sys.stderr)
        return 1
    stored = app.store.get_calibration(printer.device_id)
    calibration = vision.ScaleCalibration.from_dict(stored) if stored else None
    annotated, pixels, millimetres = vision.annotate_measurement(
        Path(source).read_bytes(), (args.x1, args.y1), (args.x2, args.y2), calibration
    )
    out = app.capture_path(printer.device_id, "measure").with_suffix(".png")
    out.write_bytes(annotated)
    if millimetres is None:
        print(f"{pixels:.1f} px (uncalibrated - run `mhs calibrate` first) -> {out}")
        return 0
    print(f"{pixels:.1f} px = {millimetres:.2f} mm -> {out}")
    if args.target:
        _print_json(vision.compare_to_target(millimetres, args.target))
    return 0


def _probe(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cmd_serve(args) -> int:
    from .server import create_server

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    create_server(load_settings(args.config)).run("stdio")
    return 0


# -- argument parsing ------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mhs", description="Control 3D printers from the shell")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--printer", help="printer id (default: the only one configured)")
    parser.add_argument("--version", action="version", version=f"mhs {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, needs_app=True):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func, needs_app=needs_app)
        return p

    p = add("status", cmd_status, "show what the printer is doing")
    p.add_argument("--json", action="store_true")

    p = add("watch", cmd_watch, "poll status until interrupted")
    p.add_argument("--interval", type=float, default=5.0)

    p = add("files", cmd_files, "list files on the printer")
    p.add_argument("--directory", default="")
    p.add_argument("--json", action="store_true")

    p = add("upload", cmd_upload, "upload a sliced .3mf/.gcode")
    p.add_argument("local_path")
    p.add_argument("--name", help="remote filename")

    p = add("print", cmd_print, "start a print from a file on the printer")
    p.add_argument("file")
    p.add_argument("--plate", type=int, default=1)
    p.add_argument("--use-ams", action="store_true")
    p.add_argument("--ams-slots", type=int, nargs="*", help="AMS tray per colour, 0-3")
    p.add_argument("--timelapse", action="store_true")
    p.add_argument("--name")
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

    for name, help_text in (("pause", "pause the print"), ("resume", "resume the print"),
                            ("stop", "abort the print")):
        p = add(name, cmd_simple, help_text)
        p.add_argument("-y", "--yes", action="store_true")

    p = add("light", cmd_light, "turn the printer light on/off")
    p.add_argument("state", choices=["on", "off"])

    p = add("snapshot", cmd_snapshot, "capture camera frame(s) to disk")
    p.add_argument("-o", "--output")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--interval", type=float, default=20.0)

    p = add("schedule", cmd_schedule, "queue a print for later")
    p.add_argument("file")
    p.add_argument("when", help="ISO-8601 time or relative offset like '+2h'")
    p.add_argument("--window", type=int, default=60, help="minutes the job may wait for an idle printer")
    p.add_argument("--plate", type=int, default=1)
    p.add_argument("--use-ams", action="store_true")
    p.add_argument("--name")
    p.add_argument("--note")

    p = add("jobs", cmd_jobs, "list scheduled prints")
    p.add_argument("--status")
    p.add_argument("--json", action="store_true")

    p = add("cancel", cmd_cancel, "cancel a scheduled print")
    p.add_argument("job_id")

    p = add("history", cmd_history, "past prints and their recorded outcomes")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")

    p = add("inspect", cmd_inspect, "measure a mesh and check it against the printer")
    p.add_argument("path")
    p.add_argument("--nozzle", type=float)
    p.add_argument("--layer-height", type=float)
    p.add_argument("--json", action="store_true")

    p = add("preview", cmd_preview, "render a mesh to a PNG contact sheet")
    p.add_argument("path")
    p.add_argument("-o", "--output")
    p.add_argument("--views", nargs="+", default=["iso", "front", "right", "top"])
    p.add_argument("--size", type=int, default=420)
    p.add_argument("--title")

    p = add("scale", cmd_scale, "resize a mesh to a target dimension")
    p.add_argument("path")
    p.add_argument("target_mm", type=float)
    p.add_argument("--axis", default="max", choices=["x", "y", "z", "max", "min"])
    p.add_argument("--apply", action="store_true", help="write the scaled STL")
    p.add_argument("-o", "--output")

    p = add("slice", cmd_slice, "slice a mesh with an intent (draft/quality/strong/...)")
    p.add_argument("path", nargs="?", help="the .stl/.obj/.3mf to slice")
    p.add_argument("--intent", default="balanced")
    p.add_argument("--compare", nargs="+", metavar="INTENT",
                   help="slice several ways and compare the time")
    p.add_argument("--list-intents", action="store_true")
    p.add_argument("--layer-height", type=float)
    p.add_argument("--infill", type=float, metavar="PERCENT")
    p.add_argument("--walls", type=int)
    p.add_argument("--supports", action=argparse.BooleanOptionalAction)
    p.add_argument("-o", "--output")

    p = add("describe", cmd_describe, "print the MHS device descriptor")
    p.add_argument("--json", action="store_true")
    p.add_argument("-o", "--output")

    add("channels", cmd_channels, "list the device's read/write channels")

    p = add("read", cmd_read, "read one channel")
    p.add_argument("channel")

    p = add("write", cmd_write, "write one channel")
    p.add_argument("channel")
    p.add_argument("value")
    p.add_argument("-y", "--yes", action="store_true", help="confirm a physical action")

    p = add("grid", cmd_grid, "capture a frame with a pixel grid overlaid")
    p.add_argument("--step", type=int, default=80)
    p.add_argument("-o", "--output")

    p = add("calibrate", cmd_calibrate, "set mm-per-pixel from a known object")
    p.add_argument("reference", help="e.g. sgd_1, usd_quarter, or a size in mm")
    p.add_argument("pixels", type=float)

    p = add("measure", cmd_measure, "measure between two pixel coordinates")
    p.add_argument("x1", type=float)
    p.add_argument("y1", type=float)
    p.add_argument("x2", type=float)
    p.add_argument("y2", type=float)
    p.add_argument("--target", type=float, help="intended size in mm, to compare against")
    p.add_argument("--frame", help="measure a specific saved frame")

    p = add("vacuum", cmd_vacuum, "show what the robot vacuum is doing")
    p.add_argument("--json", action="store_true")

    p = add("rooms", cmd_rooms, "list the rooms the vacuum has mapped")
    p.add_argument("--json", action="store_true")

    p = add("map", cmd_map, "save the vacuum's map with a millimetre grid")
    p.add_argument("-o", "--output")

    p = add("clean", cmd_clean, "start a cleaning run")
    p.add_argument("rooms", nargs="*", help="room names or segment ids; empty means everywhere")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--suction")
    p.add_argument("--water")
    p.add_argument("-y", "--yes", action="store_true")

    p = add("goto", cmd_goto, "send the vacuum to a point and stop there")
    p.add_argument("location", nargs="?", help="a saved location or room name")
    p.add_argument("--x", type=float)
    p.add_argument("--y", type=float)
    p.add_argument("-y", "--yes", action="store_true")

    add("dock", cmd_dock, "send the vacuum back to its dock")

    p = add("locations", cmd_locations, "list, save or forget named points")
    p.add_argument("--save", nargs=3, metavar=("NAME", "X_MM", "Y_MM"))
    p.add_argument("--forget", metavar="NAME")

    p = add("roborock-login", cmd_roborock_login, "sign in to Roborock and store a token",
            needs_app=False)
    p.add_argument("--email")
    p.add_argument("--code", action="store_true", help="sign in with an emailed code")
    p.add_argument("--base-url")
    p.add_argument("-o", "--output", help="where to write the token")

    add("scheduler", cmd_scheduler, "run the scheduler in the foreground")
    add("doctor", cmd_doctor, "check every transport and explain what is broken")
    add("serve", cmd_serve, "run the MCP server over stdio", needs_app=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.needs_app:
        return args.func(args)
    return asyncio.run(_with_app(args, lambda app: args.func(app, args)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
