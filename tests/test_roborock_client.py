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
