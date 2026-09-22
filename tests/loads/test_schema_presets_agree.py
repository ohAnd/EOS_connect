"""
The config schema and the runtime presets describe the same appliances.

`schema.py` cannot import `loads.presets` - it has to stay importable without the
runtime package layout - so the type lists exist in both places. This is the test that
makes that duplication safe: add a preset without adding it to the schema and the UI
silently refuses to offer it; add it to the schema only and picking it produces a load
that builds no model.
"""

import pytest

from src.config_web.schema import (
    MANAGED_LOAD_CONTINGENT_TYPES,
    MANAGED_LOAD_COVER_TYPES,
    MANAGED_LOAD_EXTERNAL_TYPES,
    MANAGED_LOAD_STRATEGIES,
    MANAGED_LOAD_THERMAL_TYPES,
    MANAGED_LOAD_TYPES,
    ConfigSchema,
)
from src.loads import presets
from src.loads.planner import STRATEGIES


def test_the_type_lists_match():
    assert sorted(MANAGED_LOAD_TYPES) == sorted(presets.TYPES)


def test_the_thermal_type_lists_match():
    assert sorted(MANAGED_LOAD_THERMAL_TYPES) == sorted(presets.THERMAL_TYPES)


def test_the_contingent_type_lists_match():
    assert sorted(MANAGED_LOAD_CONTINGENT_TYPES) == sorted(presets.CONTINGENT_TYPES)


def test_the_external_type_lists_match():
    assert sorted(MANAGED_LOAD_EXTERNAL_TYPES) == sorted(presets.EXTERNAL_TYPES)


def test_the_cover_type_lists_match():
    assert sorted(MANAGED_LOAD_COVER_TYPES) == sorted(presets.COVER_TYPES)


def test_the_strategy_lists_match():
    assert sorted(MANAGED_LOAD_STRATEGIES) == sorted(STRATEGIES)


def test_the_type_field_offers_exactly_the_known_types():
    field = ConfigSchema().get("managed_loads.type")
    assert field.validation["choices"] == MANAGED_LOAD_TYPES


SHARED_WITH_POOL_PRESET = (
    "target_temp", "deadband_k", "volume_m3", "surface_m2",
    "heat_loss_w_per_m2_k", "rated_power_w", "cop_nominal", "cop_air_coeff",
    "cover_loss_factor", "min_runtime_minutes", "max_runtime_hours_per_day",
    "window_start", "window_end", "min_ambient_temp_c",
    "frost_protection_temp_c", "season_start", "season_end", "strategy",
)


@pytest.mark.parametrize("field_name", SHARED_WITH_POOL_PRESET)
def test_schema_defaults_mirror_the_pool_preset(field_name):
    """
    The schema's defaults are what the settings UI shows before anything is saved, and
    the presets are what the runtime applies. They only agree by being checked: a user
    who reads 28 C in the form and gets 24 C in the pool has been lied to.
    """
    schema_default = ConfigSchema().get(f"managed_loads.{field_name}").default
    preset_default = presets.PRESETS[presets.TYPE_POOL_HEATPUMP]["defaults"][field_name]
    assert schema_default == pytest.approx(preset_default) if isinstance(
        preset_default, (int, float)
    ) else schema_default == preset_default


@pytest.mark.parametrize("type_name", MANAGED_LOAD_TYPES)
def test_every_offered_type_builds_a_model(type_name):
    """Whatever the UI lets a user pick has to produce something that runs."""
    assert presets.build_model({"id": "x", "type": type_name}) is not None


def test_every_thermal_field_is_hidden_for_non_thermal_types():
    """A pushed profile has no volume; showing the field would only invite nonsense."""
    schema = ConfigSchema()
    for name in ("volume_m3", "surface_m2", "cop_nominal", "temp_sensor"):
        field = schema.get(f"managed_loads.{name}")
        assert field.depends_on == {"type": MANAGED_LOAD_THERMAL_TYPES}


def test_relative_dependencies_are_used_only_inside_the_list_section():
    """
    A dotted key is absolute; a bare key is resolved within the entry. Only the list
    sections may use the relative form, because only they have an entry to resolve in.
    """
    for field in ConfigSchema().all_fields():
        for dep_key in (field.depends_on or {}):
            if "." not in dep_key:
                assert field.section == "managed_loads", (
                    f"{field.key} uses a relative dependency outside a list section"
                )
