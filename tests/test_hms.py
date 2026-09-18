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
