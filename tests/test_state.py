"""A1/P1 telemetry arrives as deltas; the merged view is what everything reads."""

from __future__ import annotations

import time

from mhs.drivers.bambu.state import BambuState, deep_merge
from mhs.models import PrintState

FULL_REPORT = {
    "print": {
        "command": "push_status",
        "gcode_state": "RUNNING",
        "subtask_name": "bracket_v3",
        "mc_percent": 41,
        "layer_num": 82,
        "total_layer_num": 200,
        "mc_remaining_time": 37,
        "stg_cur": 0,
        "nozzle_temper": 219.8,
        "nozzle_target_temper": 220.0,
        "bed_temper": 59.5,
        "bed_target_temper": 60.0,
        "spd_lvl": 2,
        "spd_mag": 100,
        "cooling_fan_speed": "15",
        "wifi_signal": "-45dBm",
        "sdcard": True,
        "print_error": 0,
        "hms": [],
        "lights_report": [{"node": "chamber_light", "mode": "on"}],
        "ams": {
            "tray_now": "1",
            "ams": [
                {
                    "id": "0",
                    "tray": [
                        {"id": "0"},
                        {
                            "id": "1",
                            "tray_type": "PLA",
                            "tray_color": "FF8800FF",
                            "remain": 73,
                            "nozzle_temp_min": "190",
                            "nozzle_temp_max": "240",
                        },
                    ],
                }
            ],
        },
        "vt_tray": {"id": "254", "tray_type": ""},
    }
}


def fresh_state() -> BambuState:
    state = BambuState("a1mini")
    state.connected = True
    state.apply(FULL_REPORT)
    return state


def test_deep_merge_keeps_untouched_keys_and_replaces_lists():
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "items": [1, 2, 3]}
    deep_merge(base, {"nested": {"y": 9}, "items": [7]})
    assert base == {"a": 1, "nested": {"x": 1, "y": 9}, "items": [7]}


def test_delta_report_does_not_erase_earlier_fields():
    state = fresh_state()
    state.apply({"print": {"mc_percent": 55, "layer_num": 110}})
    status = state.to_status()
    assert status.progress_percent == 55
    assert status.current_layer == 110
    assert status.job_name == "bracket_v3"  # not resent in the delta
    assert status.total_layers == 200


def test_normalised_status_fields():
    status = fresh_state().to_status()
    assert status.state is PrintState.RUNNING
    assert status.online is True
    assert status.stage == "printing"
    assert status.nozzle.current == 219.8 and status.nozzle.target == 220.0
    assert status.speed_level == "standard"
    assert status.lights == {"chamber_light": "on"}
    assert status.fans["part_cooling"] == 15
    assert "41%" in status.summary() and "layer 82/200" in status.summary()


def test_filament_slots_include_external_spool_and_active_flag():
    slots = fresh_state().to_status().filament
    labels = [s.slot for s in slots]
    assert labels == ["AMS1-1", "AMS1-2", "external"]
    assert slots[0].empty is True
    assert slots[1].material == "PLA" and slots[1].active is True and slots[1].remaining_percent == 73
    assert slots[2].empty is True


def test_hms_and_print_error_become_alerts():
    state = fresh_state()
    state.apply({"print": {"hms": [{"attr": 50331904, "code": 131073}], "print_error": 50364416}})
    alerts = state.to_status().alerts
    codes = [a.code for a in alerts]
    assert codes[0].startswith("HMS_0300_0100")
    assert alerts[0].severity == "serious"  # code 0x0002_0001
    assert any(c.startswith("PRINT_ERROR_") for c in codes)


def test_stale_telemetry_is_reported_offline():
    state = fresh_state()
    state.last_message_at = time.time() - 600
    status = state.to_status(stale_after=90)
    assert status.online is False
    assert status.state is PrintState.OFFLINE


def test_stage_is_suppressed_when_not_printing():
    state = fresh_state()
    state.apply({"print": {"gcode_state": "FINISH", "stg_cur": 0}})
    status = state.to_status()
    assert status.state is PrintState.FINISHED
    assert status.stage is None
    assert status.state.accepts_new_job is True


def test_firmware_version_from_info_report():
    state = fresh_state()
    state.apply({"info": {"module": [{"name": "ota", "sw_ver": "01.06.00.00"}]}})
    assert state.firmware == "01.06.00.00"
