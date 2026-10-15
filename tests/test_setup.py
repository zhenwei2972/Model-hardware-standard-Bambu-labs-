"""The setup gate: what a machine needs before its first print."""

from __future__ import annotations

from dataclasses import dataclass, field

from mhs.config import DeviceConfig
from mhs.setup import build_report, check_config, recommended_for


@dataclass
class FakeAlert:
    code: str
    severity: str


@dataclass
class FakeReading:
    current: float = 0.0


@dataclass
class FakeFilament:
    slot: str = "external"
    material: str = "PLA"
    empty: bool = False


@dataclass
class FakeState:
    value: str = "idle"


@dataclass
class FakeStatus:
    state: FakeState = field(default_factory=FakeState)
    online: bool = True
    alerts: list = field(default_factory=list)
    filament: list = field(default_factory=lambda: [FakeFilament()])
    nozzle: FakeReading = field(default_factory=FakeReading)
    bed: FakeReading = field(default_factory=FakeReading)


def a1mini(**kwargs) -> DeviceConfig:
    return DeviceConfig(device_id="a1mini", model="A1 mini", host="10.0.0.2",
                        serial="X", access_code="Y", **kwargs)


def test_a1_mini_recommendations_name_the_textured_plate():
    rec = recommended_for("A1 mini")
    assert rec.plate_type == "Textured PEI Plate"
    assert rec.bed_c == 65.0
    assert any("Cool Plate" in note for note in rec.notes)


def test_recommendations_match_loosely_like_specs_do():
    assert recommended_for("Bambu Lab A1 mini").plate_type == "Textured PEI Plate"
    assert recommended_for(None).material == "PLA"
    assert recommended_for("some other printer").bed_c == 60.0


def test_missing_plate_type_blocks_setup():
    findings = check_config("a1mini", a1mini(), read_only=False)
    blocker = next(f for f in findings if f.code == "plate_type_unset")
    assert blocker.severity == "blocker"
    assert "Textured PEI Plate" in blocker.suggestion


def test_configured_plate_type_passes():
    findings = check_config("a1mini", a1mini(plate_type="Textured PEI Plate"), read_only=False)
    assert not [f for f in findings if f.code == "plate_type_unset"]


def test_read_only_is_reported_but_does_not_block():
    findings = check_config("a1mini", a1mini(plate_type="Textured PEI Plate"), read_only=True)
    note = next(f for f in findings if f.code == "read_only")
    assert note.severity == "note"


def test_report_is_ready_when_configured_and_idle():
    report = build_report("a1mini", a1mini(plate_type="Textured PEI Plate"), FakeStatus(), False)
    assert report.ready
    assert report.observed["plate_type"] == "Textured PEI Plate"
    assert report.recommended.bed_c == 65.0


def test_a_serious_alert_blocks_setup():
    status = FakeStatus(alerts=[FakeAlert(code="PRINT_ERROR_0300_800B", severity="serious")])
    report = build_report("a1mini", a1mini(plate_type="Textured PEI Plate"), status, False)
    assert not report.ready
    assert any(f.code == "active_alert" for f in report.findings)


def test_empty_spool_is_a_warning_not_a_blocker():
    status = FakeStatus(filament=[FakeFilament(empty=True)])
    report = build_report("a1mini", a1mini(plate_type="Textured PEI Plate"), status, False)
    assert report.ready
    assert any(f.code == "no_filament" and f.severity == "warning" for f in report.findings)


def test_report_survives_an_unreachable_printer():
    report = build_report("a1mini", a1mini(plate_type="Textured PEI Plate"), None, False)
    assert report.ready
    assert report.observed["plate_type"] == "Textured PEI Plate"
