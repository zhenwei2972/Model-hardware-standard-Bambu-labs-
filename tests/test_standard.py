"""The MHS layer: read/write primitives, driver-side limits, and the descriptor.

The property that matters here is that a limit cannot be argued past. It is
checked in the driver before the hardware is touched, so no prompt, tool
argument or clever phrasing gets around it.
"""

from __future__ import annotations

import pytest

from mhs.standard.channels import Access, Channel, ChannelTable, SafetyLimit, SafetyViolation
from mhs.standard.descriptor import descriptor_markdown


# -- limits ----------------------------------------------------------------
def test_numeric_bounds():
    limit = SafetyLimit(minimum=0, maximum=300, rationale="hotend rating")
    limit.check("nozzle.temperature", 220)
    with pytest.raises(SafetyViolation, match="exceeds the safe maximum"):
        limit.check("nozzle.temperature", 301)
    with pytest.raises(SafetyViolation, match="below the safe minimum"):
        limit.check("nozzle.temperature", -1)


def test_the_rationale_travels_with_the_rejection():
    limit = SafetyLimit(maximum=80, rationale="the plate is rated to 80 C")
    with pytest.raises(SafetyViolation) as excinfo:
        limit.check("bed.temperature", 120)
    assert excinfo.value.hint == "the plate is rated to 80 C"


def test_enumerated_values():
    limit = SafetyLimit(allowed=("pause", "resume", "stop"))
    limit.check("job.control", "pause")
    with pytest.raises(SafetyViolation, match="not allowed"):
        limit.check("job.control", "explode")


def test_non_numeric_value_for_a_numeric_channel():
    with pytest.raises(SafetyViolation, match="expected a number"):
        SafetyLimit(maximum=10).check("speed", "fast")


def test_access_flags():
    assert Access.READ.readable and not Access.READ.writable
    assert Access.WRITE.writable and not Access.WRITE.readable
    assert Access.READ_WRITE.readable and Access.READ_WRITE.writable


def test_a_readable_channel_needs_a_reader():
    with pytest.raises(ValueError, match="no reader"):
        Channel("x", Access.READ, "no reader supplied")


def test_channel_table_suggests_near_misses():
    table = ChannelTable()
    table.add(Channel("nozzle.temperature", Access.READ, "d", reader=lambda: None))
    with pytest.raises(SafetyViolation) as excinfo:
        table.get("temperature")
    assert "nozzle.temperature" in excinfo.value.hint


# -- primitives against a real driver --------------------------------------
async def test_standard_channels_are_present(printer):
    names = printer.channel_table().names()
    assert {"printer.state", "nozzle.temperature", "bed.temperature", "job.control",
            "job.file", "job.progress"} <= set(names)


async def test_read_returns_live_values(printer):
    assert await printer.read("printer.state") == "idle"
    assert await printer.read("nozzle.temperature") == 25.0
    status = await printer.read("printer.status")
    assert status["printer_id"] == "mock"


async def test_reading_a_write_only_channel_fails(printer):
    with pytest.raises(SafetyViolation, match="write-only"):
        await printer.read("job.control")


async def test_writing_a_read_only_channel_fails(printer):
    with pytest.raises(SafetyViolation, match="read-only"):
        await printer.write("job.progress", 50)


async def test_limits_are_enforced_before_the_hardware_is_touched(printer):
    with pytest.raises(SafetyViolation, match="exceeds the safe maximum 300"):
        await printer.write("nozzle.temperature", 350)
    # Nothing was sent: the driver refused before dispatching.
    assert not any(name == "set_temperature" for name, _ in printer.commands)

    await printer.write("nozzle.temperature", 215)
    assert ("set_temperature", {"nozzle": 215.0, "bed": None}) in printer.commands


async def test_bed_limit_comes_from_the_printer_spec(printer):
    # The A1 mini plate is rated to 80 C, unlike the 100 C of the larger machines.
    await printer.write("bed.temperature", 60)
    with pytest.raises(SafetyViolation, match="exceeds the safe maximum 80"):
        await printer.write("bed.temperature", 100)


async def test_physical_actions_need_confirmation(printer):
    with pytest.raises(SafetyViolation, match="requires confirmation"):
        await printer.write("job.control", "stop")
    await printer.start_print("cache/benchy.3mf")
    await printer.write("job.control", "pause", confirm=True)
    assert (await printer.read("printer.state")) == "paused"


async def test_job_control_rejects_unknown_actions(printer):
    with pytest.raises(SafetyViolation, match="not allowed"):
        await printer.write("job.control", "reverse", confirm=True)


async def test_unknown_channel_is_a_clear_error(printer):
    with pytest.raises(SafetyViolation, match="unknown channel"):
        await printer.read("laser.power")


async def test_writing_a_file_channel_starts_a_print(printer):
    await printer.write("job.file", "cache/benchy.3mf", confirm=True)
    assert (await printer.read("printer.state")) == "running"


# -- descriptor ------------------------------------------------------------
async def test_descriptor_describes_device_channels_limits_and_refusals(printer):
    descriptor = await printer.describe()
    assert descriptor["device"]["kind"] == "fdm_3d_printer"
    assert descriptor["device"]["model"] == "Mock A1 mini"
    # The spec lookup still resolves it to the real A1 mini hardware profile.
    assert descriptor["physical"]["build_volume_mm"] == [180.0, 180.0, 180.0]
    assert descriptor["resolution"]["min_xy_feature_mm"] == 0.4
    assert descriptor["safety"]["enforced_in"] == "driver"
    assert descriptor["safety"]["limits"]["nozzle.temperature"]["maximum"] == 300.0
    assert any("cloud mode" in item or "Slice models" in item for item in descriptor["cannot"])
    assert any("burn" in tag for tag in descriptor["tags"])


async def test_descriptor_channels_match_the_live_table(printer):
    descriptor = await printer.describe()
    assert [c["name"] for c in descriptor["channels"]] == printer.channel_table().names()


async def test_descriptor_markdown_is_readable(printer):
    text = descriptor_markdown(await printer.describe())
    assert text.startswith("# Mock A1 mini (mock)")
    for heading in ("## What this device is", "## What it can print", "## Channels",
                    "## Safety", "## What it cannot do"):
        assert heading in text
    assert "`nozzle.temperature`" in text
    assert "needs confirmation" in text
