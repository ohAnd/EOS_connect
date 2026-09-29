"""Presets: every type resolves to a working model with complete, plausible defaults."""

import pytest

from src.loads.instance import ManagedLoad
from src.loads.models.external import ExternalPushModel
from src.loads.models.thermal import ThermalStorageModel
from src.loads.presets import (
    CONTINGENT_TYPES,
    EXTERNAL_TYPES,
    PRESETS,
    THERMAL_TYPES,
    TYPES,
    apply_defaults,
    build_model,
    fallback_ambient_c,
    preset_for,
    uses_outdoor_ambient,
)
from src.loads.models.thermal_physics import COP_MAX, COP_MIN


@pytest.mark.parametrize("type_name", TYPES)
def test_every_type_builds_a_model(type_name):
    model = build_model({"id": "x", "type": type_name})
    assert model is not None


@pytest.mark.parametrize("type_name", THERMAL_TYPES)
def test_thermal_types_share_one_implementation(type_name):
    """Four appliances, one model - that is what makes adding a fifth a table entry."""
    assert PRESETS[type_name]["model"] is ThermalStorageModel


@pytest.mark.parametrize("type_name", EXTERNAL_TYPES)
def test_external_types_share_the_push_model(type_name):
    assert PRESETS[type_name]["model"] is ExternalPushModel


def test_an_unknown_type_builds_nothing_rather_than_raising():
    """A typo in one entry must not stop the house being optimized."""
    assert build_model({"id": "x", "type": "teleporter"}) is None
    assert preset_for("teleporter") is None


@pytest.mark.parametrize("type_name", THERMAL_TYPES)
def test_thermal_defaults_are_complete_and_plausible(type_name):
    entry = apply_defaults({"id": "x", "type": type_name})
    for key in ("volume_m3", "surface_m2", "heat_loss_w_per_m2_k", "rated_power_w",
                "cop_nominal", "target_temp", "deadband_k"):
        assert key in entry, f"{type_name} is missing {key}"

    assert entry["volume_m3"] > 0
    assert entry["surface_m2"] > 0
    assert entry["rated_power_w"] > 0
    assert COP_MIN <= entry["cop_nominal"] <= COP_MAX
    assert entry["target_temp"] > 0
    assert entry["deadband_k"] > 0


def test_a_resistive_appliance_may_have_a_cop_of_one():
    """A sauna is a resistive heater; a COP floor of 1.5 would under-forecast it by a third."""
    assert apply_defaults({"type": "sauna"})["cop_nominal"] == 1.0


def test_only_a_pool_reads_the_outdoor_forecast():
    assert uses_outdoor_ambient("pool_heatpump") is True
    assert uses_outdoor_ambient("hot_water_tank") is False
    assert uses_outdoor_ambient("teleporter") is False


def test_every_type_has_a_fallback_ambient_temperature():
    for type_name in TYPES:
        assert 0 < fallback_ambient_c(type_name) < 40


def test_user_values_are_never_overwritten_by_a_preset():
    entry = apply_defaults({"id": "x", "type": "pool_heatpump", "target_temp": 32.0})
    assert entry["target_temp"] == 32.0


def test_an_explicit_none_survives_the_defaults():
    """``window_start: None`` means "no window", not "use the preset's 08:00"."""
    entry = apply_defaults({"id": "x", "type": "pool_heatpump", "window_start": None})
    assert entry["window_start"] is None


def test_a_pool_defaults_to_daylight_hours_and_the_swimming_season():
    entry = apply_defaults({"id": "x", "type": "pool_heatpump"})
    assert entry["window_start"] == 8
    assert entry["window_end"] == 20
    assert entry["season_start"] == "04-15"
    assert entry["frost_protection_temp_c"] == 4.0


@pytest.mark.parametrize("type_name", CONTINGENT_TYPES)
def test_contingent_types_get_a_release_gate(type_name):
    load = ManagedLoad({"id": "x", "type": type_name})
    assert load.gate is not None


def test_a_profile_type_has_no_release_gate():
    """Nothing to gate: the caller has already decided when the energy is drawn."""
    assert ManagedLoad({"id": "x", "type": "external_profile"}).gate is None
