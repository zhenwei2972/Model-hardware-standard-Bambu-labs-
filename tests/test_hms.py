from mhs.drivers.bambu.hms import (
    alert_from_print_error,
    format_hms_code,
    format_print_error,
    severity_of,
)


def test_hms_code_formatting():
    assert format_hms_code(0x0300_0100, 0x0003_0001) == "0300_0100_0003_0001"


def test_severity_mapping():
    assert severity_of(0x0001_0001) == "fatal"
    assert severity_of(0x0003_0001) == "common"
    assert severity_of(0x0099_0001) == "unknown"


def test_print_error_formatting():
    assert format_print_error(0x0300_4000) == "0300_4000"
    assert alert_from_print_error(0) is None
    alert = alert_from_print_error(0x0300_4000)
    assert alert.code == "PRINT_ERROR_0300_4000"
    assert alert.url.endswith("0300_4000")


def test_the_codes_from_the_bumper_run_are_decoded():
    """Both alerts from 4 Oct reached get_status with message=null."""
    from mhs.drivers.bambu.hms import alert_from_hms

    force = alert_from_hms({"attr": 0x0300_1800, "code": 0x0001_0003})
    assert force.code == "HMS_0300_1800_0001_0003"
    assert force.severity == "fatal"
    assert "force sensor" in force.message and "bed levelling" in force.message
    assert "power-cycle" in force.message

    cancelled = alert_from_print_error(0x0300_400C)
    assert cancelled.message == "Print cancelled"


def test_ams_runouts_decode_for_every_unit_and_slot():
    from mhs.drivers.bambu.hms import describe_hms

    assert describe_hms("0700_2000_0002_0001") == "AMS A slot 1 has run out of filament"
    assert describe_hms("0703_2300_0002_0001") == "AMS D slot 4 has run out of filament"


def test_unlisted_codes_still_say_where_they_come_from():
    from mhs.drivers.bambu.hms import describe_hms, describe_print_error

    assert "motion controller" in describe_hms("0300_9900_0002_0001")
    assert "not in the bundled table" in describe_print_error("0500_9999")
    assert "not in the bundled table" in describe_hms("ABCD_0000_0002_0001")


def test_table_keys_are_well_formed():
    import re

    from mhs.drivers.bambu.hms import HMS_MESSAGES, PRINT_ERROR_MESSAGES

    assert all(re.fullmatch(r"[0-9A-F]{4}(_[0-9A-F]{4}){3}", k) for k in HMS_MESSAGES)
    assert all(re.fullmatch(r"[0-9A-F]{4}_[0-9A-F]{4}", k) for k in PRINT_ERROR_MESSAGES)
    assert all(v.strip() for v in [*HMS_MESSAGES.values(), *PRINT_ERROR_MESSAGES.values()])


def test_a1_series_external_spool_codes_are_read_through_their_07ff_twin():
    from mhs.drivers.bambu.hms import describe_hms, describe_print_error
    assert describe_print_error("12FF_C006").startswith("Feed filament into the PTFE tube")
    assert "meaning inferred from the matching 07FF code" in describe_print_error("12FF_8007")
    assert "external spool" in describe_hms("12FF_2000_0002_0002").lower()


def test_an_unlisted_code_still_says_so():
    from mhs.drivers.bambu.hms import describe_print_error
    assert describe_print_error("0300_8015").startswith("Code not in the bundled table")
