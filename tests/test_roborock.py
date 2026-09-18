"""The Roborock driver's pure parts: command shapes and status normalisation.

The transports belong to `python-roborock`; what is tested here is what this
driver adds on top, which is where a mistake would be silent - a malformed
segment list makes the robot sit still rather than return an error.
"""

from __future__ import annotations

import pytest

from mhs.drivers.roborock import commands
from mhs.drivers.roborock.state import map_state, to_status
from mhs.errors import CommandRejected
from mhs.models import VacuumState


# -- commands --------------------------------------------------------------
def test_goto_sends_integer_millimetres():
    assert commands.goto(24200.4, 26200.6) == ("app_goto_target", [24200, 26201])


@pytest.mark.parametrize("point", [(-10, 25500), (25500, 99999), (60000, 60000)])
def test_goto_rejects_coordinates_outside_the_protocol_range(point):
    with pytest.raises(CommandRejected, match="outside the map"):
        commands.goto(*point)


def test_goto_hint_explains_the_offset_frame():
    with pytest.raises(CommandRejected) as excinfo:
        commands.goto(-1, 0)
    assert "25500" in excinfo.value.hint


def test_segment_clean_uses_the_current_firmware_shape():
    command, params = commands.segment_clean([16, 17], repeat=2)
    assert command == "app_segment_clean"
    assert params == [{"segments": [16, 17], "repeat": 2}]


def test_segment_clean_validates_its_input():
    with pytest.raises(CommandRejected, match="no rooms given"):
        commands.segment_clean([])
    with pytest.raises(CommandRejected, match="must be a number"):
        commands.segment_clean(["kitchen"])
    with pytest.raises(CommandRejected, match="at most 32"):
        commands.segment_clean(list(range(40)))


@pytest.mark.parametrize("repeat", [0, 4, "lots"])
def test_repeat_is_bounded(repeat):
    with pytest.raises(CommandRejected):
        commands.segment_clean([16], repeat=repeat)


def test_zone_clean_normalises_corner_order():
    """A rectangle given top-right-first must still clean, not be ignored."""
    _command, params = commands.zone_clean([(26000, 26000, 24000, 24000)])
    assert params == [[24000, 24000, 26000, 26000, 1]]


def test_zone_clean_rejects_a_sliver():
    with pytest.raises(CommandRejected, match="at least 200 mm"):
        commands.zone_clean([(25000, 25000, 25050, 26000)])


def test_zone_clean_limits_and_shapes():
    with pytest.raises(CommandRejected, match="no zone given"):
        commands.zone_clean([])
    with pytest.raises(CommandRejected, match="at most 5"):
        commands.zone_clean([(24000, 24000, 26000, 26000)] * 6)
    with pytest.raises(CommandRejected, match="x1, y1, x2, y2"):
        commands.zone_clean([(1, 2, 3)])


def test_simple_commands_take_no_parameters():
    assert commands.simple(commands.DOCK) == ("app_charge", [])
    assert commands.simple(commands.PAUSE) == ("app_pause", [])


def test_resume_reuses_the_start_verb():
    # Not a typo: the firmware resumes a paused job with app_start.
    assert commands.RESUME == commands.START == "app_start"


# -- status normalisation --------------------------------------------------
class FakeStatusTrait:
    """Stands in for the library's status trait, which is attribute-shaped."""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("Cleaning", VacuumState.CLEANING),
        ("segment cleaning", VacuumState.CLEANING),
        ("Returning home", VacuumState.RETURNING),
        ("Charging", VacuumState.CHARGING),
        ("Paused", VacuumState.PAUSED),
        ("Washing the mop", VacuumState.DOCKED),
        ("Error", VacuumState.ERROR),
        ("something new in firmware", VacuumState.UNKNOWN),
        (None, VacuumState.UNKNOWN),
    ],
)
def test_state_mapping(native, expected):
    assert map_state(native) is expected


def test_status_normalisation():
    trait = FakeStatusTrait(
        state_name="Cleaning", battery=64, fan_power_name="balanced",
        water_box_mode_name="medium", clean_area=12_400_000, clean_time=1080,
        error_code=0, error_code_name="none", water_box_attached=True,
    )
    status = to_status("saros", trait)
    assert status.state is VacuumState.CLEANING
    assert status.battery_percent == 64
    assert status.fan_power == "balanced" and status.water_level == "medium"
    assert status.cleaned_area_m2 == pytest.approx(12.4)  # mm2 converted
    assert status.cleaning_time_minutes == 18  # seconds converted
    assert status.error is None
    assert status.mop_attached is True


def test_a_real_fault_becomes_an_alert():
    trait = FakeStatusTrait(state_name="Error", error_code=7, error_code_name="Wheel stuck")
    status = to_status("saros", trait)
    assert status.state is VacuumState.ERROR
    assert status.error.code == "ROBOROCK_7"
    assert status.error.message == "Wheel stuck"


def test_a_healthy_robot_has_no_alert():
    for code, name in ((0, "none"), (None, None), ("0", "None")):
        status = to_status("saros", FakeStatusTrait(state_name="Idle", error_code=code,
                                                    error_code_name=name))
        assert status.error is None


def test_full_battery_on_the_dock_reads_as_docked_not_charging():
    trait = FakeStatusTrait(state_name="Charging", battery=100)
    assert to_status("saros", trait).state is VacuumState.DOCKED
    partial = FakeStatusTrait(state_name="Charging", battery=80)
    assert to_status("saros", partial).state is VacuumState.CHARGING


def test_offline_overrides_whatever_was_last_reported():
    trait = FakeStatusTrait(state_name="Cleaning", battery=50)
    status = to_status("saros", trait, online=False)
    assert status.state is VacuumState.OFFLINE
    assert "offline" in status.summary()


def test_missing_fields_do_not_crash_the_mapping():
    status = to_status("saros", FakeStatusTrait())
    assert status.state is VacuumState.UNKNOWN
    assert status.battery_percent is None
    assert status.to_dict()["device_id"] == "saros"
