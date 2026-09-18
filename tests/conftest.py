from __future__ import annotations

import json

import pytest

from mhs.config import PrinterConfig, Settings
from mhs.drivers.mock import MockPrinter


@pytest.fixture
def mock_config() -> PrinterConfig:
    return PrinterConfig(printer_id="mock", driver="mock", model="Mock A1 mini")


@pytest.fixture
def settings(tmp_path, mock_config) -> Settings:
    return Settings(
        printers={"mock": mock_config},
        default_printer="mock",
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
