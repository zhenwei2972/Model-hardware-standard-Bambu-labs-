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


def test_unknown_printer_is_a_clean_error(cli_env, capsys):
    assert run(["--printer", "ghost", "status"]) == 1
    assert "unknown printer" in capsys.readouterr().err


def test_parser_exposes_every_command():
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert {"status", "print", "schedule", "doctor", "serve", "scheduler"} <= set(actions[0].choices)
