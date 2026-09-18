from __future__ import annotations

import pytest

from mhs.config import load_settings
from mhs.errors import ConfigError

TOML = """
[server]
default_printer = "a1mini"
read_only = true

[printers.a1mini]
driver = "bambu"
model = "A1 mini"
host = "192.168.1.42"
serial = "01P00A000000000"
access_code = "12345678"
upload_dir = "cache"

[printers.p1s]
driver = "bambu"
model = "P1S"
host = "192.168.1.43"
serial = "01S00A000000000"
access_code = "87654321"
tls_mode = "ca_only"
"""


def write(tmp_path, text=TOML):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_loads_multiple_printers(tmp_path):
    settings = load_settings(write(tmp_path), env={})
    assert sorted(settings.printers) == ["a1mini", "p1s"]
    assert settings.read_only is True
    assert settings.get().printer_id == "a1mini"
    assert settings.get("p1s").tls_mode == "ca_only"


def test_unknown_printer_is_an_error(tmp_path):
    settings = load_settings(write(tmp_path), env={})
    with pytest.raises(ConfigError, match="unknown printer"):
        settings.get("nope")


def test_env_overrides_apply_on_top_of_the_file(tmp_path):
    settings = load_settings(write(tmp_path), env={"MHS_READ_ONLY": "0", "MHS_ALLOW_RAW_GCODE": "true"})
    assert settings.read_only is False
    assert settings.allow_raw_gcode is True


def test_env_only_configuration():
    settings = load_settings(
        None,
        env={
            "BAMBU_HOST": "10.0.0.7",
            "BAMBU_SERIAL": "01P00A000000000",
            "BAMBU_ACCESS_CODE": "abcd1234",
            "MHS_CONFIG": "/nonexistent.toml",
        },
    )
    config = settings.get()
    assert (config.host, config.driver, config.printer_id) == ("10.0.0.7", "bambu", "a1mini")


def test_missing_credentials_are_reported_with_a_hint(tmp_path):
    path = write(tmp_path, '[printers.a1]\ndriver = "bambu"\nhost = "1.2.3.4"\n')
    with pytest.raises(ConfigError) as excinfo:
        load_settings(path, env={})
    assert "serial" in str(excinfo.value) and "access_code" in str(excinfo.value)
    assert excinfo.value.hint


def test_bad_tls_mode_rejected(tmp_path):
    path = write(
        tmp_path,
        '[printers.a1]\nhost="1.2.3.4"\nserial="S"\naccess_code="C"\ntls_mode="whatever"\n',
    )
    with pytest.raises(ConfigError, match="tls_mode"):
        load_settings(path, env={})


def test_ambiguous_default_printer(tmp_path):
    path = write(tmp_path, TOML.replace('default_printer = "a1mini"', ""))
    settings = load_settings(path, env={})
    with pytest.raises(ConfigError, match="several printers"):
        settings.get()


def test_no_printers_configured():
    settings = load_settings(None, env={"MHS_CONFIG": "/nonexistent.toml"})
    with pytest.raises(ConfigError, match="no printers configured"):
        settings.get()
