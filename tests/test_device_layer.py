"""The vendor-neutral layer: capability checks, waiting, driver registry."""

from __future__ import annotations

import pytest

from mhs.config import PrinterConfig
from mhs.device import Printer
from mhs.drivers import available, build
from mhs.errors import ConfigError, NotSupported, TimeoutExceeded
from mhs.models import Capability, PrintState


def test_registry_knows_the_builtin_drivers():
    assert {"bambu", "mock"} <= set(available())


def test_registry_rejects_unknown_drivers():
    with pytest.raises(ConfigError, match="unknown driver"):
        build(PrinterConfig(printer_id="x", driver="ender3"))


def test_capability_checks_explain_themselves(printer):
    assert printer.supports(Capability.CAMERA_SNAPSHOT)
    printer.require(Capability.CAMERA_SNAPSHOT)
    with pytest.raises(NotSupported, match="skip_objects"):
        printer.require(Capability.SKIP_OBJECTS)


async def test_unimplemented_optional_methods_raise_not_supported(mock_config):
    class Bare(Printer):
        driver_name = "bare"
        capabilities = ()

        async def connect(self): ...
        async def disconnect(self): ...
        async def info(self): ...
        async def status(self): ...
        async def start_print(self, remote_path, options=None): ...
        async def pause_print(self): ...
        async def resume_print(self): ...
        async def stop_print(self): ...

    bare = Bare("bare")
    for coro in (bare.snapshot(), bare.list_files(), bare.send_gcode("G28"), bare.set_light(True)):
        with pytest.raises(NotSupported):
            await coro


async def test_wait_for_state_returns_when_reached(printer):
    await printer.start_print("cache/benchy.3mf")
    status = await printer.wait_for_state({PrintState.RUNNING}, timeout=1, poll_interval=0.01)
    assert status.state is PrintState.RUNNING


async def test_wait_for_state_times_out_with_context(printer):
    with pytest.raises(TimeoutExceeded, match="idle"):
        await printer.wait_for_state({PrintState.FINISHED}, timeout=0.05, poll_interval=0.01)


async def test_async_context_manager_connects_and_disconnects(mock_config):
    from mhs.drivers.mock import MockPrinter

    device = MockPrinter(mock_config)
    async with device as connected:
        assert (await connected.status()).online is True
    assert (await device.status()).online is False


async def test_mock_print_completes_over_time(printer, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(printer, "now", lambda: clock[0])
    await printer.start_print("cache/benchy.3mf")
    clock[0] += printer.print_duration / 2
    mid = await printer.status()
    assert mid.state is PrintState.RUNNING and 45 <= mid.progress_percent <= 55
    clock[0] += printer.print_duration
    assert (await printer.status()).state is PrintState.FINISHED
