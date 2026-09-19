"""Credential handling: the part that touches someone's account."""

from __future__ import annotations

import json
import os
import stat

import pytest

from mhs.config import DeviceConfig
from mhs.drivers.roborock.client import (
    RoborockAccount,
    load_credentials,
    save_credentials,
)
from mhs.errors import ConfigError


def test_token_is_written_owner_only(tmp_path):
    path = save_credentials(tmp_path / "rr.json", "a@b.c", {"token": "secret"}, None)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, readable by others"
    assert json.loads(path.read_text())["user_data"] == {"token": "secret"}


def test_a_loose_token_file_is_tightened_on_read(tmp_path):
    path = save_credentials(tmp_path / "rr.json", "a@b.c", {"token": "secret"})
    os.chmod(path, 0o644)
    load_credentials(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_missing_or_malformed_credentials_explain_the_fix(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_credentials(tmp_path / "absent.json")
    assert "roborock-login" in excinfo.value.hint

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_credentials(broken)

    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"email": "a@b.c"}')
    with pytest.raises(ConfigError, match="does not contain a Roborock token"):
        load_credentials(wrong)


def test_account_is_read_from_the_device_config(tmp_path):
    config = DeviceConfig(
        device_id="saros", driver="roborock", model="Saros 10",
        extra={"email": "me@example.com", "device_name": "Saros 10"},
    )
    account = RoborockAccount.from_config(config, tmp_path, env={})
    assert account.email == "me@example.com"
    assert account.device_name == "Saros 10"
    assert account.credentials_file == tmp_path / "roborock-credentials.json"


def test_environment_fills_in_what_the_config_omits(tmp_path):
    config = DeviceConfig(device_id="saros", driver="roborock")
    account = RoborockAccount.from_config(
        config, tmp_path,
        env={"ROBOROCK_EMAIL": "env@example.com", "ROBOROCK_DEVICE_UID": "abc123"},
    )
    assert account.email == "env@example.com" and account.device_uid == "abc123"


def test_a_missing_email_says_what_to_do(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        RoborockAccount.from_config(DeviceConfig("saros", driver="roborock"), tmp_path, env={})
    assert "roborock-login" in excinfo.value.hint


def test_the_password_is_never_part_of_what_is_stored(tmp_path):
    """Only the exchanged token is persisted - there is nowhere to put a password."""
    path = save_credentials(tmp_path / "rr.json", "a@b.c", {"token": "t"}, "https://example")
    stored = json.loads(path.read_text())
    assert set(stored) == {"email", "base_url", "user_data"}


# -- the login command -------------------------------------------------------
class _Args:
    """Stands in for the parsed argparse namespace."""

    def __init__(self, **fields):
        self.config = None
        self.email = "someone@example.com"
        self.code = False
        self.base_url = None
        self.output = None
        self.__dict__.update(fields)


@pytest.fixture
def stub_roborock(monkeypatch, tmp_path):
    """Replace the network calls, so the flow is exercised without an account."""
    import getpass

    from mhs import cli
    from mhs.drivers.roborock import client as rr

    calls: dict = {}

    async def fake_login(email, password, base_url=None):
        calls["password"] = {"email": email, "password": password, "base_url": base_url}
        return {"token": "FAKE"}

    async def fake_request_code(email, base_url=None):
        calls["code_requested"] = email

    async def fake_code_login(email, code, base_url=None):
        calls["code"] = {"email": email, "code": code}
        return {"token": "FAKE_FROM_CODE"}

    monkeypatch.setattr(rr, "login", fake_login)
    monkeypatch.setattr(rr, "request_email_code", fake_request_code)
    monkeypatch.setattr(rr, "login_with_code", fake_code_login)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "hunter2")
    monkeypatch.setenv("MHS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MHS_CONFIG", str(tmp_path / "absent.toml"))
    return cli, calls


def test_password_login_writes_a_token_and_nothing_else(stub_roborock, tmp_path, capsys):
    cli, calls = stub_roborock
    target = tmp_path / "token.json"
    assert cli.cmd_roborock_login(_Args(output=str(target))) == 0

    assert calls["password"]["password"] == "hunter2"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    stored = json.loads(target.read_text())
    assert stored["user_data"] == {"token": "FAKE"}
    # The password must not survive anywhere in the written file.
    assert "hunter2" not in target.read_text()

    out = capsys.readouterr().out
    assert "mode 600" in out
    assert 'driver = "roborock"' in out  # prints the config block to paste


def test_code_login_requests_then_exchanges(stub_roborock, tmp_path, monkeypatch, capsys):
    cli, calls = stub_roborock
    monkeypatch.setattr("builtins.input", lambda prompt="": "123456")
    target = tmp_path / "token.json"

    assert cli.cmd_roborock_login(_Args(code=True, output=str(target))) == 0
    assert calls["code_requested"] == "someone@example.com"
    assert calls["code"]["code"] == "123456"
    assert json.loads(target.read_text())["user_data"] == {"token": "FAKE_FROM_CODE"}
    assert "emailed to someone@example.com" in capsys.readouterr().out


def test_the_token_defaults_into_the_state_directory(stub_roborock, tmp_path):
    cli, _calls = stub_roborock
    assert cli.cmd_roborock_login(_Args()) == 0
    assert (tmp_path / "state" / "roborock-credentials.json").is_file()


def test_a_failed_login_reports_the_reason(stub_roborock, tmp_path, capsys):
    cli, _calls = stub_roborock
    from mhs.drivers.roborock import client as rr
    from mhs.errors import ConnectionFailed

    async def refuse(email, password, base_url=None):
        raise ConnectionFailed("Roborock login failed", hint="try --code instead")

    rr.login = refuse
    assert cli.cmd_roborock_login(_Args(output=str(tmp_path / "t.json"))) == 1
    err = capsys.readouterr().err
    assert "Roborock login failed" in err and "try --code" in err
