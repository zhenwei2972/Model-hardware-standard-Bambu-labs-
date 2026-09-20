"""The checks that would have caught the print we lost to a 35 C bed."""

from __future__ import annotations

import io
import zipfile

from PIL import Image

from mhs.design.vision import frame_is_dark, mean_luminance
from mhs.preflight import check, inspect, preflight

COOL_PLATE_HEADER = """\
; HEADER_BLOCK_START
; filament_type = PLA
; curr_bed_type = Cool Plate
; cool_plate_temp_initial_layer = 35
; hot_plate_temp_initial_layer = 60
; textured_plate_temp_initial_layer = 65
; nozzle_temperature_initial_layer = 220
M106 S0
M190 S35 ; set bed temperature and wait for it to be reached
G29 ;Home
M109 S220
G1 X10 Y10 F3000
"""

TEXTURED_HEADER = COOL_PLATE_HEADER.replace("Cool Plate", "Textured PEI Plate").replace(
    "M190 S35", "M190 S65"
)


def _write_3mf(path, gcode: str) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", gcode)
    return str(path)


def test_inspect_reads_plate_type_and_the_bed_target(tmp_path):
    info = inspect(_write_3mf(tmp_path / "part.gcode.3mf", COOL_PLATE_HEADER))
    assert info.bed_type == "Cool Plate"
    assert info.bed_c == 35.0
    assert info.filaments == ["PLA"]
    assert info.nozzle_c == 220.0
    assert info.plate_temps["textured"] == 65.0


def test_pla_on_a_35c_bed_is_a_blocker(tmp_path):
    path = _write_3mf(tmp_path / "benchy.gcode.3mf", COOL_PLATE_HEADER)
    _, findings = preflight(path, plate_type="Textured PEI Plate")
    codes = {f.code: f.severity for f in findings}
    assert codes["bed_too_cold"] == "blocker"
    assert codes["plate_type_mismatch"] == "warning"


def test_matching_plate_and_temperature_passes(tmp_path):
    path = _write_3mf(tmp_path / "good.gcode.3mf", TEXTURED_HEADER)
    _, findings = preflight(path, plate_type="Textured PEI Plate")
    assert findings == []


def test_plain_gcode_is_read_too(tmp_path):
    path = tmp_path / "part.gcode"
    path.write_text(COOL_PLATE_HEADER)
    assert inspect(path).bed_c == 35.0


def test_unknown_filament_is_not_judged(tmp_path):
    gcode = COOL_PLATE_HEADER.replace("PLA", "SOMETHING-NEW")
    path = _write_3mf(tmp_path / "exotic.gcode.3mf", gcode)
    assert check(inspect(path)) == []


def test_missing_plate_type_only_checks_temperature(tmp_path):
    path = _write_3mf(tmp_path / "benchy.gcode.3mf", COOL_PLATE_HEADER)
    codes = {f.code for f in check(inspect(path))}
    assert codes == {"bed_too_cold"}


def _frame(level: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (level, level, level)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_an_unlit_chamber_reads_as_dark():
    assert frame_is_dark(_frame(12))
    assert mean_luminance(_frame(12)) < 40


def test_a_lit_chamber_does_not():
    assert not frame_is_dark(_frame(150))
