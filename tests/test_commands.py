"""The MQTT wire format is the part that is expensive to debug on real hardware."""

from __future__ import annotations

import pytest

from mhs.drivers.bambu import commands
from mhs.errors import CommandRejected


def test_project_file_uses_sdcard_url_and_zero_cloud_ids():
    payload = commands.project_file("cache/bracket.3mf", plate=2, job_name="bracket", sequence_id=7)
    body = payload["print"]
    assert body["command"] == "project_file"
    assert body["sequence_id"] == "7"
    # An ftp:/// url is acknowledged but never resolved: the firmware reports the
    # failed read as HMS 0500_C010 while the file sits intact on the card.
    assert body["url"] == "file:///sdcard/cache/bracket.3mf"
    assert body["param"] == "Metadata/plate_2.gcode"
    assert body["subtask_name"] == "bracket"
    assert {body["project_id"], body["profile_id"], body["task_id"], body["subtask_id"]} == {"0"}
    assert body["use_ams"] is False
    assert body["ams_mapping"] == ""


def test_project_file_derives_job_name_from_filename():
    body = commands.project_file("/cache/some part v2.3mf")["print"]
    assert body["subtask_name"] == "some part v2"
    assert body["url"] == "file:///sdcard/cache/some part v2.3mf"


def test_project_file_rejects_unsliced_input():
    with pytest.raises(CommandRejected):
        commands.project_file("model.stl")


def test_clean_print_error_clears_the_stuck_flag():
    body = commands.clean_print_error(sequence_id=4)["print"]
    assert body["command"] == "clean_print_error"
    assert body["sequence_id"] == "4"
    assert body["subtask_id"] == "0"


def test_ams_mapping_is_right_aligned():
    assert commands.build_ams_mapping([2]) == [-1, -1, -1, -1, 2]
    assert commands.build_ams_mapping([0, 3]) == [-1, -1, -1, 0, 3]
    assert commands.build_ams_mapping([]) == [-1, -1, -1, -1, -1]


@pytest.mark.parametrize("slots", [[4], [-1], [0, 1, 2, 3, 0]])
def test_ams_mapping_rejects_bad_slots(slots):
    with pytest.raises(CommandRejected):
        commands.build_ams_mapping(slots)


def test_project_file_with_ams_embeds_mapping():
    body = commands.project_file("cache/x.3mf", use_ams=True, ams_mapping=[1, 0])["print"]
    assert body["use_ams"] is True
    assert body["ams_mapping"] == [-1, -1, -1, 1, 0]


def test_speed_levels():
    assert commands.set_speed(3)["print"]["param"] == "3"
    with pytest.raises(CommandRejected):
        commands.set_speed(5)


def test_temperature_becomes_gcode():
    body = commands.set_temperature(nozzle=220, bed=60)["print"]
    assert body["command"] == "gcode_line"
    assert body["param"] == "M104 S220\nM140 S60\n"


@pytest.mark.parametrize("kwargs", [{"nozzle": 400}, {"bed": 200}, {}])
def test_temperature_limits(kwargs):
    with pytest.raises(CommandRejected):
        commands.set_temperature(**kwargs)


def test_gcode_allow_list():
    commands.assert_gcode_allowed("M104 S200\n; a comment\nG28")
    with pytest.raises(CommandRejected):
        commands.assert_gcode_allowed("M997")  # firmware update


def test_led_payload_always_carries_timing_fields():
    body = commands.led_control(True)["system"]
    assert body["led_mode"] == "on"
    assert {"led_on_time", "led_off_time", "loop_times", "interval_time"} <= body.keys()
    with pytest.raises(CommandRejected):
        commands.led_control(True, node="floodlight")


def test_calibration_bitmask():
    assert commands.calibration(bed_level=True, vibration=True)["print"]["option"] == 0b110
    assert commands.calibration(bed_level=True, vibration=False)["print"]["option"] == 0b010
    with pytest.raises(CommandRejected):
        commands.calibration(bed_level=False, vibration=False)


def test_pause_resume_stop_shape():
    for builder in (commands.pause, commands.resume, commands.stop):
        body = builder(4)["print"]
        assert body["param"] == ""
        assert body["sequence_id"] == "4"
