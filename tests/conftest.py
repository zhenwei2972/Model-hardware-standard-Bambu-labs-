from __future__ import annotations

import json

import pytest

from mhs.config import DeviceConfig, Settings
from mhs.drivers.mock import MockPrinter


@pytest.fixture
def mock_config() -> DeviceConfig:
    return DeviceConfig(device_id="mock", driver="mock", model="Mock A1 mini")


@pytest.fixture
def settings(tmp_path, mock_config) -> Settings:
    return Settings(
        devices={"mock": mock_config},
        default_device="mock",
        state_dir=tmp_path / "state",
    )


@pytest.fixture
async def printer(mock_config) -> MockPrinter:
    device = MockPrinter(mock_config)
    await device.connect()
    return device


def tool_body(result) -> dict:
    """Tools return JSON text content; decode it."""
    return json.loads(result.content[0].text)


@pytest.fixture
def vacuum_config() -> DeviceConfig:
    return DeviceConfig(device_id="saros", driver="mock_vacuum", model="Saros 10")


@pytest.fixture
def vacuum_settings(tmp_path, vacuum_config) -> Settings:
    return Settings(
        devices={"saros": vacuum_config},
        default_device="saros",
        state_dir=tmp_path / "vacuum-state",
    )


@pytest.fixture
async def robot(vacuum_config):
    from mhs.drivers.mock_vacuum import MockVacuum

    device = MockVacuum(vacuum_config)
    await device.connect()
    return device
