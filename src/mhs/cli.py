"""Command line front-end.

Same capabilities as the MCP server, minus the model: useful for verifying a
printer works before wiring Claude to it, and for running the scheduler as a
standalone daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import time
from pathlib import Path

from . import __version__
from .app import MHSApp
from .config import load_settings
from .errors import MHSError
from .models import PrintOptions
from .scheduler import parse_when
from .store import iso


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


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
    printer = await app.printer(args.printer)
    status = await printer.status()
    if args.json:
        _print_json(status.to_dict())
    else:
        print(status.summary())
        for alert in status.alerts:
            print(f"  ! {alert.code} ({alert.severity}) {alert.message or ''} {alert.url or ''}")
    return 0


async def cmd_watch(app, args) -> int:
    printer = await app.printer(args.printer)
    try:
        while True:
            status = await printer.status()
            print(f"\r{time.strftime('%H:%M:%S')}  {status.summary():<110}", end="", flush=True)
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


async def cmd_files(app, args) -> int:
    printer = await app.printer(args.printer)
    files = await printer.list_files(args.directory)
    if args.json:
        _print_json([f.to_dict() for f in files])
    else:
        for entry in files:
            size = f"{entry.size_bytes/1_048_576:.1f} MB" if entry.size_bytes else "-"
            print(f"{'d' if entry.is_dir else '-'} {size:>10}  {entry.path}")
    return 0


async def cmd_upload(app, args) -> int:
    printer = await app.printer(args.printer)
    entry = await printer.upload_file(args.local_path, args.name)
    print(f"uploaded {entry.path} ({entry.size_bytes or 0} bytes)")
    return 0


async def cmd_print(app, args) -> int:
    printer = await app.printer(args.printer)
    if not args.yes:
        answer = input(f"Start {args.file} on {printer.printer_id}? [y/N] ").strip().lower()
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
        printer.printer_id, job_name=options.job_name or args.file, remote_path=args.file,
        settings=options.to_dict(),
    )
    _print_json({"run_id": run_id, **result})
    return 0 if result.get("acknowledged", True) else 2


async def cmd_simple(app, args) -> int:
    printer = await app.printer(args.printer)
    action = {
        "pause": printer.pause_print,
        "resume": printer.resume_print,
        "stop": printer.stop_print,
    }[args.command]
    if args.command == "stop" and not args.yes:
        answer = input(f"Abort the print on {printer.printer_id}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled")
            return 1
    _print_json(await action())
    return 0


async def cmd_light(app, args) -> int:
    printer = await app.printer(args.printer)
    _print_json(await printer.set_light(args.state == "on"))
    return 0


async def cmd_snapshot(app, args) -> int:
    printer = await app.printer(args.printer)
    if args.count == 1:
        frame = await printer.snapshot()
        out = Path(args.output) if args.output else app.capture_path(printer.printer_id)
        out.write_bytes(frame)
        print(out)
        return 0
    for index in range(args.count):
        if index:
            await asyncio.sleep(args.interval)
        frame = await printer.snapshot()
        out = app.capture_path(printer.printer_id, f"series-{index:03d}")
        out.write_bytes(frame)
        print(out)
    return 0


async def cmd_schedule(app, args) -> int:
    printer = await app.printer(args.printer)
    start_at = parse_when(args.when)
    job = app.store.add_job(
        printer.printer_id,
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
    jobs = app.store.list_jobs(printer_id=args.printer, status=args.status)
    if args.json:
        _print_json([j.to_dict() for j in jobs])
    else:
        for job in jobs:
            print(f"{job.id}  {job.status:<9} {iso(job.start_at)}  {job.printer_id}  {job.remote_path}"
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
    runs = app.store.list_runs(printer_id=args.printer, limit=args.limit)
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
    print(f"printer '{config.printer_id}' driver={config.driver} model={config.model}")
    ok = True
    if config.driver == "bambu":
        print(f"  host {config.host}  serial {config.serial[:4]}***  tls={config.tls_mode}")
        ports = (("mqtt", config.mqtt_port), ("ftps", config.ftps_port), ("camera", config.camera_port))
        for label, port in ports:
            reachable = _probe(config.host, port)
            ok &= reachable
            extra = "" if reachable else "  <- closed: LAN Only Mode / Developer Mode off, or firewalled"
            print(f"  tcp/{port:<5} {label:<7} {'open' if reachable else 'closed'}{extra}")
    printer = await app.printer(args.printer)
    for label, probe in (
        ("telemetry", printer.status),
        ("files", printer.list_files),
        ("camera", printer.snapshot),
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
    print("all good" if ok else "some checks failed - see hints above")
    return 0 if ok else 1


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
