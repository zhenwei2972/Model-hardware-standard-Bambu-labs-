"""The CLI is how a human verifies the printer before Claude touches it."""

from __future__ import annotations

import json

import pytest

from mhs import cli


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MHS_MOCK", "1")
    monkeypatch.setenv("MHS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MHS_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.chdir(tmp_path)


def run(argv) -> int:
    return cli.main(argv)


def test_status_prints_a_summary(cli_env, capsys):
    assert run(["status"]) == 0
    assert "mock: idle" in capsys.readouterr().out


def test_status_json(cli_env, capsys):
    assert run(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "idle"


def test_files_listing(cli_env, capsys):
    assert run(["files"]) == 0
    assert "cache/benchy.3mf" in capsys.readouterr().out


def test_print_requires_confirmation(cli_env, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert run(["print", "cache/benchy.3mf"]) == 1
    assert "cancelled" in capsys.readouterr().out


def test_print_with_yes_starts_and_journals(cli_env, capsys):
    assert run(["print", "cache/benchy.3mf", "-y"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == 1
    assert run(["history"]) == 0
    assert "cache/benchy.3mf" in capsys.readouterr().out


def test_schedule_and_cancel(cli_env, capsys):
    assert run(["schedule", "cache/benchy.3mf", "+3h"]) == 0
    out = capsys.readouterr().out
    job_id = out.split(":")[0]
    assert run(["jobs", "--json"]) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert jobs[0]["id"] == job_id and jobs[0]["status"] == "pending"
    assert run(["cancel", job_id]) == 0
    assert "cancelled" in capsys.readouterr().out


def test_cancel_unknown_job(cli_env, capsys):
    assert run(["cancel", "job_nope"]) == 1


def test_snapshot_writes_a_file(cli_env, capsys, tmp_path):
    target = tmp_path / "frame.jpg"
    assert run(["snapshot", "-o", str(target)]) == 0
    assert target.read_bytes().startswith(b"\xff\xd8")


def test_doctor_reports_all_transports(cli_env, capsys):
    assert run(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "telemetry" in out and "camera" in out and "all good" in out


def test_unknown_device_is_a_clean_error(cli_env, capsys):
    assert run(["--printer", "ghost", "status"]) == 1
    assert "unknown device" in capsys.readouterr().err


def test_parser_exposes_every_command():
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert {"status", "print", "schedule", "doctor", "serve", "scheduler"} <= set(actions[0].choices)


# -- design and standard commands -------------------------------------------
@pytest.fixture
def ball(tmp_path):
    from mhs.design.mesh import save_stl, uv_sphere

    return str(save_stl(uv_sphere(25, 32, 24), tmp_path / "ball.stl"))


def test_inspect_prints_dimensions_and_findings(cli_env, capsys, ball):
    assert run(["inspect", ball]) == 0
    out = capsys.readouterr().out
    assert "50.00 x 50.00 x 50.00 mm" in out
    assert "resolves about 0.4 mm in XY" in out


def test_inspect_exits_nonzero_when_unprintable(cli_env, capsys, tmp_path):
    from mhs.design.mesh import save_stl, unit_cube

    path = str(save_stl(unit_cube(300), tmp_path / "huge.stl"))
    assert run(["inspect", path]) == 2
    assert "build volume" in capsys.readouterr().out


def test_preview_writes_a_png(cli_env, capsys, ball, tmp_path):
    target = tmp_path / "preview.png"
    assert run(["preview", ball, "-o", str(target), "--views", "iso", "--size", "160"]) == 0
    assert target.read_bytes().startswith(b"\x89PNG")


def test_scale_dry_run_then_apply(cli_env, capsys, ball, tmp_path):
    assert run(["scale", ball, "24.65"]) == 0
    assert "dry run" in capsys.readouterr().out

    target = tmp_path / "small.stl"
    assert run(["scale", ball, "24.65", "--apply", "-o", str(target)]) == 0
    from mhs.design.mesh import load_mesh

    assert load_mesh(target).dimensions.max() == pytest.approx(24.65, rel=1e-3)


def test_describe_prints_the_reference_sheet(cli_env, capsys):
    assert run(["describe"]) == 0
    out = capsys.readouterr().out
    assert "## Channels" in out and "nozzle.temperature" in out


def test_channels_read_and_write(cli_env, capsys):
    assert run(["channels"]) == 0
    assert "nozzle.temperature" in capsys.readouterr().out
    assert run(["read", "printer.state"]) == 0
    assert "idle" in capsys.readouterr().out
    assert run(["write", "nozzle.temperature", "205"]) == 0


def test_write_refuses_an_out_of_range_value(cli_env, capsys):
    assert run(["write", "nozzle.temperature", "400"]) == 1
    assert "exceeds the safe maximum" in capsys.readouterr().err


def test_write_requires_confirmation_for_physical_actions(cli_env, capsys):
    assert run(["write", "job.control", "pause"]) == 1
    assert "requires confirmation" in capsys.readouterr().err


def test_grid_calibrate_measure_flow(cli_env, capsys):
    assert run(["grid"]) == 0
    assert capsys.readouterr().out.strip().endswith(".png")

    assert run(["measure", "0", "0", "100", "0"]) == 0
    assert "uncalibrated" in capsys.readouterr().out

    assert run(["calibrate", "sgd_1", "100"]) == 0
    assert "0.24650 mm/px" in capsys.readouterr().out

    assert run(["measure", "0", "0", "200", "0", "--target", "49.3"]) == 0
    out = capsys.readouterr().out
    assert "49.30 mm" in out and '"error_percent": 0.0' in out


def test_measure_without_a_frame_fails_cleanly(cli_env, capsys):
    assert run(["measure", "0", "0", "10", "0"]) == 1
    assert "mhs grid" in capsys.readouterr().err


# -- vacuum commands ---------------------------------------------------------
@pytest.fixture
def vacuum_cli(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\ndefault_device = "saros"\n\n'
        '[devices.saros]\ndriver = "mock_vacuum"\nmodel = "Saros 10"\n'
    )
    monkeypatch.setenv("MHS_CONFIG", str(config))
    monkeypatch.setenv("MHS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("MHS_MOCK", raising=False)
    monkeypatch.chdir(tmp_path)


def test_vacuum_status(vacuum_cli, capsys):
    assert run(["vacuum"]) == 0
    assert "saros: docked" in capsys.readouterr().out


def test_rooms_lists_names_and_ids(vacuum_cli, capsys):
    assert run(["rooms"]) == 0
    out = capsys.readouterr().out
    assert "Kitchen" in out and "16" in out


def test_clean_a_room_by_name(vacuum_cli, capsys):
    assert run(["clean", "kitchen", "-y"]) == 0
    out = capsys.readouterr().out
    assert '"segments"' in out and "16" in out


def test_clean_rejects_an_unmapped_room(vacuum_cli, capsys):
    assert run(["clean", "bathroom", "-y"]) == 1
    assert "no mapped room" in capsys.readouterr().err


def test_goto_saved_location_round_trip(vacuum_cli, capsys):
    """The saved point survives between invocations, even though the mock robot does not."""
    assert run(["locations", "--save", "dog bowl", "24200", "26200"]) == 0
    capsys.readouterr()
    assert run(["goto", "dog bowl", "-y"]) == 0
    out = capsys.readouterr().out
    assert '"x_mm": 24200' in out and '"y_mm": 26200' in out


def test_goto_a_room_centre(vacuum_cli, capsys):
    assert run(["goto", "bedroom", "-y"]) == 0
    assert '"command": "go_to"' in capsys.readouterr().out


def test_goto_needs_a_target(vacuum_cli, capsys):
    assert run(["goto"]) == 1
    assert "location name" in capsys.readouterr().err


def test_goto_unknown_place(vacuum_cli, capsys):
    assert run(["goto", "atlantis", "-y"]) == 1
    assert "nothing called" in capsys.readouterr().err


def test_map_writes_a_png_and_lists_room_centres(vacuum_cli, capsys, tmp_path):
    target = tmp_path / "map.png"
    assert run(["map", "-o", str(target)]) == 0
    assert target.read_bytes().startswith(b"\x89PNG")
    assert "Kitchen" in capsys.readouterr().out


def test_dock(vacuum_cli, capsys):
    assert run(["dock"]) == 0
    assert '"command": "dock"' in capsys.readouterr().out


def test_locations_forget(vacuum_cli, capsys):
    run(["locations", "--save", "corner", "24000", "24000"])
    capsys.readouterr()
    assert run(["locations", "--forget", "corner"]) == 0
    assert run(["locations", "--forget", "corner"]) == 1


def test_printer_commands_refuse_a_vacuum(vacuum_cli, capsys):
    assert run(["status"]) == 1
    err = capsys.readouterr().err
    assert "not a printer" in err and "vacuum commands" in err


# -- doctor ------------------------------------------------------------------
def test_doctor_reports_every_transport_when_one_fails(cli_env, capsys, monkeypatch):
    """A diagnostic that stops at the first failure is the least useful kind."""
    from mhs.drivers.mock import MockPrinter
    from mhs.errors import ConnectionFailed

    async def broken_camera(self):
        raise ConnectionFailed("camera refused", hint="turn on LAN Mode Liveview")

    monkeypatch.setattr(MockPrinter, "snapshot", broken_camera)
    assert run(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "telemetry  ok" in out      # kept going
    assert "files      ok" in out      # kept going
    assert "camera     FAIL  camera refused" in out
    assert "turn on LAN Mode Liveview" in out


def test_doctor_explains_an_unreachable_device(cli_env, capsys, monkeypatch):
    from mhs.drivers.mock import MockPrinter
    from mhs.errors import ConnectionFailed

    async def refuse(self):
        raise ConnectionFailed("cannot reach 192.0.2.1:8883 - timed out")

    monkeypatch.setattr(MockPrinter, "connect", refuse)
    assert run(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "connect    FAIL" in out
    assert "client isolation" in out   # names the cause people actually hit
    assert "ping" in out


def test_doctor_survives_a_driver_bug(cli_env, capsys, monkeypatch):
    """An unexpected exception in one probe must not hide the others."""
    from mhs.drivers.mock import MockPrinter

    async def explode(self, directory=""):
        raise RuntimeError("driver bug")

    monkeypatch.setattr(MockPrinter, "list_files", explode)
    assert run(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "files      ERROR RuntimeError: driver bug" in out
    assert "camera     ok" in out       # the check after the crash still ran
