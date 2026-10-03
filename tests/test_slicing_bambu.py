"""The Bambu Studio path: each test is a failure that reached a real A1 mini."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from mhs.errors import ConfigError
from mhs.slicing import bambu
from mhs.slicing.parameters import SliceSettings
from mhs.slicing.slicer import Slicer, SlicerFlavour, find_slicer

MACHINE = "Bambu Lab A1 mini 0.4 nozzle"


@pytest.fixture
def profiles(tmp_path) -> Path:
    """A miniature profiles/BBL tree with the same shape as the real one."""
    root = tmp_path / "BBL"
    for kind in ("machine", "process", "filament"):
        (root / kind).mkdir(parents=True)

    def put(kind, filename, data):
        (root / kind / filename).write_text(json.dumps(data))

    put("machine", "fdm_common.json", {"name": "fdm_common", "printable_area": ["0x0", "180x180"],
                                      "machine_start_gcode": ";generic 2023 macro"})
    put("machine", f"{MACHINE}.json", {"name": MACHINE, "inherits": "fdm_common"})
    put("machine", f"{MACHINE} template machine_start_gcode.json",
        {"machine_start_gcode": ";===== machine: A1 mini ====="})
    put("process", "proc_common.json", {"name": "proc_common", "layer_height": "0.2",
                                       "sparse_infill_density": "15%", "outer_wall_speed": ["200"]})
    put("process", "0.20mm Standard @BBL A1M.json",
        {"name": "0.20mm Standard @BBL A1M", "inherits": "proc_common"})
    put("filament", "Generic PLA @BBL A1M.json", {"name": "Generic PLA @BBL A1M",
                                                 "nozzle_temperature": ["220"]})
    return root


def sliced(tmp_path, *, model_id="N1", bed_type="Textured PEI Plate", bed_c=65,
           start=";===== machine: A1 mini =====", layer="0.2", name="part.gcode.3mf") -> Path:
    gcode = "\n".join([
        f"; curr_bed_type = {bed_type}",
        "; filament_type = PLA",
        f"; layer_height = {layer}",
        f"; machine_start_gcode = {start}",
        f"M190 S{bed_c};set bed temp",
        "G1 X10 Y10",
    ])
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", gcode)
        archive.writestr("Metadata/slice_info.config",
                         f'<config><plate><metadata key="printer_model_id" value="{model_id}"/></plate></config>')
    return path


def test_a1_mini_maps_to_its_profiles_and_firmware_id():
    target = bambu.machine_for("A1 mini")
    assert target.machine == MACHINE and target.model_id == "N1"
    assert bambu.machine_for("Bambu Lab A1 mini").model_id == "N1"


def test_an_unmapped_printer_says_how_to_add_it():
    with pytest.raises(ConfigError) as err:
        bambu.machine_for("X9 Ultra")
    assert "MACHINES" in err.value.hint


def test_profiles_are_flattened_because_the_cli_ignores_inherits(profiles):
    index = bambu.index_profiles(profiles)
    proc = bambu.flatten(index, "process", "0.20mm Standard @BBL A1M")
    assert proc["layer_height"] == "0.2" and "inherits" not in proc


def test_the_machines_own_gcode_replaces_the_generic_macro(profiles, tmp_path):
    machine_json, _, _ = bambu.write_profiles(tmp_path, profiles, bambu.machine_for("A1 mini"),
                                              "Textured PEI Plate", SliceSettings())
    machine = json.loads(machine_json.read_text())
    assert "A1 mini" in machine["machine_start_gcode"]
    assert machine["curr_bed_type"] == "Textured PEI Plate"


def test_settings_land_in_the_process_profile_in_its_own_format(profiles, tmp_path):
    settings = SliceSettings(layer_height_mm=0.12, infill_percent=20, perimeter_speed_mm_s=150)
    _, process_json, _ = bambu.write_profiles(tmp_path, profiles, bambu.machine_for("A1 mini"),
                                              "Textured PEI Plate", settings)
    proc = json.loads(process_json.read_text())
    assert proc["layer_height"] == "0.12"
    assert proc["sparse_infill_density"] == "20%"
    assert proc["outer_wall_speed"] == ["150"]          # stays a list, like the profile had it
    assert proc["curr_bed_type"] == "Textured PEI Plate"


def test_model_id_is_written_into_a_blank_slice(tmp_path):
    path = sliced(tmp_path, model_id="")
    bambu.set_model_id(path, "N1")
    with zipfile.ZipFile(path) as archive:
        assert 'value="N1"' in archive.read("Metadata/slice_info.config").decode()


def test_a_correct_slice_passes(tmp_path):
    assert bambu.verify(sliced(tmp_path), model_id="N1", plate="Textured PEI Plate",
                        machine_name=MACHINE, layer_height=0.2) == []


@pytest.mark.parametrize("kwargs, complaint", [
    ({"model_id": ""}, "printer_model_id"),                        # firmware refused: 0500_C010
    ({"bed_type": "Cool Plate", "bed_c": 35}, "too cold for PLA"),  # part came loose
    ({"start": ";generic 2023 macro"}, "generic macro"),            # cutter fault: 0300_800B
    ({"layer": "0.2"}, "layers"),                                   # profile ignored
])
def test_each_failure_that_reached_the_printer_is_caught(tmp_path, kwargs, complaint):
    layer = 0.08 if "layer" in kwargs else 0.2
    problems = bambu.verify(sliced(tmp_path, **kwargs), model_id="N1", plate="Textured PEI Plate",
                            machine_name=MACHINE, layer_height=layer)
    assert any(complaint in p for p in problems), problems


def test_bambu_studio_is_routed_through_the_profile_path(monkeypatch, tmp_path):
    model = tmp_path / "cube.stl"
    model.write_text("solid cube\nendsolid cube\n")
    calls = {}

    def fake_slice(binary, source, target, settings, *, printer_model, plate, timeout_s):
        calls.update(printer_model=printer_model, plate=plate)
        sliced(tmp_path, name=target.name)
        return ["BambuStudio", "--load-settings", "machine.json;process.json"]

    monkeypatch.setattr(bambu, "slice_bambu", fake_slice)
    slicer = Slicer(binary=Path("/x/BambuStudio"), flavour=SlicerFlavour.BAMBU)
    result = slicer.slice(model, tmp_path / "part.gcode.3mf", SliceSettings(layer_height_mm=0.2),
                          printer_model="A1 mini", plate_type="Textured PEI Plate")
    assert calls == {"printer_model": "A1 mini", "plate": "Textured PEI Plate"}
    assert "--load-settings" in result.command


def test_a_stock_macos_install_is_found_without_the_path(monkeypatch, tmp_path):
    binary = tmp_path / "BambuStudio"
    binary.write_text("")
    monkeypatch.setattr("mhs.slicing.slicer.shutil.which", lambda name: None)
    monkeypatch.setattr("mhs.slicing.slicer.APP_BUNDLES",
                        {SlicerFlavour.BAMBU: (str(binary),)})
    path, flavour = find_slicer(env={})
    assert path == binary and flavour is SlicerFlavour.BAMBU
