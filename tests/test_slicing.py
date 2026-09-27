"""Slicing: intent resolution, argv construction, and a real slice where possible.

The argv and parsing tests always run - they are the parts that break silently.
The tests that invoke a slicer are skipped when no binary is installed, so CI
stays green without one, but they run for real when it is there.
"""

from __future__ import annotations

import shutil
import zipfile

import pytest

from mhs.errors import CommandRejected, ConfigError
from mhs.slicing import INTENTS, SliceSettings, settings_for_intent
from mhs.slicing.slicer import (
    FLAG_MAP,
    Slicer,
    SlicerFailed,
    SlicerFlavour,
    _minutes,
    _read_gcode_text,
    find_slicer,
    flavour_of,
)
from mhs.specs import A1_MINI, spec_for

HAS_SLICER = shutil.which("prusa-slicer") or shutil.which("orca-slicer")
needs_slicer = pytest.mark.skipif(not HAS_SLICER, reason="no slicer installed")


# -- intents ---------------------------------------------------------------
def test_intents_are_ordered_from_fast_to_fine():
    """Layer height is the main time lever; the ladder must be monotonic."""
    heights = [
        settings_for_intent(name, A1_MINI)[0].layer_height_mm
        for name in ("draft", "speed", "balanced", "quality", "fine")
    ]
    assert heights == sorted(heights, reverse=True)


def test_layer_height_is_derived_from_the_nozzle_not_hard_coded():
    fine_default, _ = settings_for_intent("fine", A1_MINI)
    fine_big, _ = settings_for_intent("fine", A1_MINI, nozzle_mm=0.8)
    assert fine_big.layer_height_mm > fine_default.layer_height_mm


def test_layer_height_is_clamped_into_the_printer_range():
    low, high = A1_MINI.layer_height_range_mm
    for name in INTENTS:
        settings, _ = settings_for_intent(name, A1_MINI)
        assert low <= settings.layer_height_mm <= high
        # A layer taller than the nozzle is rejected by every slicer.
        assert settings.layer_height_mm <= A1_MINI.default_nozzle_mm


def test_a_small_nozzle_cannot_produce_an_impossible_layer():
    settings, _ = settings_for_intent("draft", A1_MINI, nozzle_mm=0.2)
    assert settings.layer_height_mm <= 0.2


def test_strong_raises_walls_and_infill_over_balanced():
    strong, _ = settings_for_intent("strong", A1_MINI)
    balanced, _ = settings_for_intent("balanced", A1_MINI)
    assert strong.wall_loops > balanced.wall_loops
    assert strong.infill_percent > balanced.infill_percent


def test_every_intent_explains_its_trade_off():
    for intent in INTENTS.values():
        assert intent.summary and intent.trade


def test_overrides_beat_the_intent():
    settings, _ = settings_for_intent(
        "draft", A1_MINI, overrides=SliceSettings(layer_height_mm=0.1, wall_loops=5)
    )
    assert settings.layer_height_mm == 0.1
    assert settings.wall_loops == 5
    assert settings.infill_percent == INTENTS["draft"].infill_percent  # untouched


def test_unknown_intent_lists_the_real_ones():
    with pytest.raises(CommandRejected) as excinfo:
        settings_for_intent("beautiful", A1_MINI)
    assert "quality" in excinfo.value.hint


@pytest.mark.parametrize(
    "override",
    [
        SliceSettings(layer_height_mm=0.9),      # beyond the printer range
        SliceSettings(layer_height_mm=0.5),      # taller than the nozzle
        SliceSettings(infill_percent=140),
        SliceSettings(wall_loops=0),
        SliceSettings(perimeter_speed_mm_s=9000),
    ],
)
def test_impossible_overrides_are_refused_before_the_slicer_runs(override):
    with pytest.raises(CommandRejected):
        settings_for_intent("balanced", A1_MINI, overrides=override)


# -- argv ------------------------------------------------------------------
@pytest.fixture
def prusa(tmp_path) -> Slicer:
    return Slicer(binary=tmp_path / "prusa-slicer", flavour=SlicerFlavour.PRUSA)


@pytest.fixture
def orca(tmp_path) -> Slicer:
    return Slicer(binary=tmp_path / "orca-slicer", flavour=SlicerFlavour.ORCA)


def test_prusa_argv(prusa, tmp_path):
    argv = prusa.build_args(
        tmp_path / "m.stl", tmp_path / "out.gcode",
        SliceSettings(layer_height_mm=0.2, infill_percent=15, wall_loops=3, supports=False),
    )
    assert "--export-gcode" in argv
    assert argv[argv.index("--layer-height") + 1] == "0.2"
    assert argv[argv.index("--fill-density") + 1] == "15%"      # PrusaSlicer wants a percent
    assert argv[argv.index("--perimeters") + 1] == "3"
    assert "--support-material=0" in argv                        # booleans are =0/=1
    assert argv[-1].endswith("m.stl")                            # model comes last


def test_orca_argv_uses_its_own_names(orca, tmp_path):
    argv = orca.build_args(
        tmp_path / "m.stl", tmp_path / "out.gcode.3mf",
        SliceSettings(layer_height_mm=0.2, infill_percent=15, wall_loops=3, supports=True),
    )
    assert argv[argv.index("--slice") + 1] == "1"
    assert "--sparse-infill-density" in argv                     # not --fill-density
    assert "--wall-loops" in argv                                # not --perimeters
    assert "--enable-support=1" in argv
    assert "--export-3mf" in argv


def test_the_two_flavours_disagree_about_names_which_is_the_point():
    prusa_keys = set(FLAG_MAP[SlicerFlavour.PRUSA].values())
    orca_keys = set(FLAG_MAP[SlicerFlavour.ORCA].values())
    assert prusa_keys != orca_keys
    # But both must cover every neutral setting, or an override silently vanishes.
    neutral = set(SliceSettings().to_dict(skip_none=False))
    for flavour, mapping in FLAG_MAP.items():
        assert set(mapping) == neutral, f"{flavour.value} is missing a setting"


def test_bambu_shares_orca_names():
    assert FLAG_MAP[SlicerFlavour.BAMBU] == FLAG_MAP[SlicerFlavour.ORCA]


def test_profiles_are_loaded_with_the_right_flag(tmp_path):
    profile = tmp_path / "process.json"
    prusa = Slicer(tmp_path / "prusa-slicer", SlicerFlavour.PRUSA, profiles=(profile,))
    orca = Slicer(tmp_path / "orca-slicer", SlicerFlavour.ORCA, profiles=(profile,))
    assert "--load" in prusa.build_args(tmp_path / "m.stl", tmp_path / "o.gcode", SliceSettings())
    assert "--load-settings" in orca.build_args(tmp_path / "m.stl", tmp_path / "o.3mf", SliceSettings())


def test_output_suffix_matches_the_flavour(prusa, orca, tmp_path):
    model = tmp_path / "part.stl"
    assert prusa.default_output(model, tmp_path, "draft").name == "part-draft.gcode"
    assert orca.default_output(model, tmp_path, "draft").name == "part-draft.gcode.3mf"


# -- discovery --------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [("orca-slicer", SlicerFlavour.ORCA), ("OrcaSlicer", SlicerFlavour.ORCA),
     ("bambu-studio", SlicerFlavour.BAMBU), ("prusa-slicer", SlicerFlavour.PRUSA)],
)
def test_flavour_from_binary_name(name, expected):
    assert flavour_of(name) is expected


def test_an_unrecognisable_binary_is_an_error():
    with pytest.raises(ConfigError, match="cannot tell which slicer"):
        flavour_of("slicerator9000")


def test_a_missing_explicit_binary_says_so():
    with pytest.raises(ConfigError, match="no slicer at"):
        find_slicer("/nonexistent/slicer")


# -- parsing ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [("17m 51s", 18), ("1h 17m 51s", 78), ("2h", 120), ("45s", 1), ("1d 2h", 1560), (None, None)],
)
def test_time_estimates_are_parsed_to_minutes(text, expected):
    assert _minutes(text) == expected


def test_gcode_is_read_from_inside_a_3mf(tmp_path):
    archive = tmp_path / "out.gcode.3mf"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", "; layer_height = 0.16\n")
    assert "layer_height = 0.16" in _read_gcode_text(archive)


# -- the real thing ---------------------------------------------------------
@pytest.fixture
def cube(tmp_path):
    from mhs.design.mesh import save_stl, unit_cube

    return save_stl(unit_cube(15), tmp_path / "cube.stl")


@needs_slicer
def test_a_real_slice_reports_what_it_actually_applied(cube, tmp_path):
    slicer = Slicer.discover()
    settings, _ = settings_for_intent("balanced", A1_MINI)
    result = slicer.slice(cube, tmp_path / slicer.default_output(cube, tmp_path).name, settings)

    assert result.output_path.is_file()
    assert result.estimated_time_minutes and result.estimated_time_minutes > 0
    assert result.filament_cm3 and result.filament_cm3 > 0
    # The point of reading back: what it applied, not what we asked for.
    assert float(result.applied["layer_height"]) == settings.layer_height_mm


@needs_slicer
def test_intents_really_do_trade_time_for_detail(cube, tmp_path):
    slicer = Slicer.discover()
    times = {}
    for intent in ("draft", "quality"):
        settings, _ = settings_for_intent(intent, A1_MINI)
        result = slicer.slice(cube, tmp_path / f"{intent}.gcode", settings)
        times[intent] = result.estimated_time_minutes
    assert times["quality"] > times["draft"], times


@needs_slicer
def test_a_setting_the_slicer_does_not_know_fails_loudly(cube, tmp_path):
    """The safety property behind the flavour mapping: wrong key, no output."""
    slicer = Slicer.discover()
    slicer.extra_args = ("--definitely-not-a-setting", "1")
    with pytest.raises(SlicerFailed) as excinfo:
        slicer.slice(cube, tmp_path / "x.gcode", SliceSettings(layer_height_mm=0.2))
    assert "Unknown option" in excinfo.value.message
    assert not (tmp_path / "x.gcode").exists()


@needs_slicer
def test_a_missing_model_is_rejected_before_running_anything(tmp_path):
    with pytest.raises(CommandRejected, match="does not exist"):
        Slicer.discover().slice(tmp_path / "ghost.stl", tmp_path / "o.gcode", SliceSettings())


def test_unmappable_setting_is_refused(tmp_path):
    slicer = Slicer(tmp_path / "prusa-slicer", SlicerFlavour.PRUSA)
    del FLAG_MAP[SlicerFlavour.PRUSA]["brim_width_mm"]
    try:
        with pytest.raises(CommandRejected, match="no mapping for"):
            slicer.build_args(tmp_path / "m.stl", tmp_path / "o.gcode",
                              SliceSettings(brim_width_mm=5))
    finally:
        FLAG_MAP[SlicerFlavour.PRUSA]["brim_width_mm"] = "--brim-width"


def test_spec_lookup_feeds_the_intents():
    assert spec_for("A1 mini").default_nozzle_mm == 0.4
