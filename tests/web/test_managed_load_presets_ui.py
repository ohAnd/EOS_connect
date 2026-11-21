"""
A new or retyped managed load starts from its own type's values.

Every field has one schema default, and for the heated stores it is the pool's. The
form used to seed a new entry from those alone and send all of them on save - so a
sauna was stored aiming at 28 C and a house would have been stored with a pool's
volume, surface and heat loss. The per-type presets now ride with the schema and the
form applies them; the values a type derives for itself are left empty.
"""

import pytest

import tests.web.wizard_driver as wz  # noqa: F401  (keeps the shared skip behaviour)


@pytest.fixture(name="config_page")
def config_page_fixture(page, server):
    page.evaluate("() => showConfigurationMenu()")
    page.wait_for_selector(".config-field")
    page.evaluate("() => configurationManager._selectSection('managed_loads')")
    return page


def _add_and_retype(page, type_name):
    return page.evaluate(
        """(typeName) => {
            const cm = configurationManager;
            cm._addListEntry('managed_loads');
            const idx = cm._getListEntries('managed_loads').length - 1;
            cm._onFieldChange(`managed_loads.${idx}.type`, typeName);
            const out = {};
            for (const [k, v] of Object.entries(cm.values)) {
                if (k.startsWith(`managed_loads.${idx}.`)) {
                    out[k.split('.').pop()] = v;
                }
            }
            return out;
        }""",
        type_name,
    )


def test_the_presets_arrive_with_the_schema(config_page):
    presets = config_page.evaluate("() => configurationManager.managedLoadPresets")
    assert presets["sauna"]["defaults"]["target_temp"] == 90.0
    assert "volume_m3" in presets["space_heating"]["derived_keys"]


def test_a_new_entry_starts_from_the_default_types_values(config_page):
    values = config_page.evaluate("""() => {
            const cm = configurationManager;
            cm._addListEntry('managed_loads');
            const idx = cm._getListEntries('managed_loads').length - 1;
            return cm.values[`managed_loads.${idx}.target_temp`];
        }""")
    assert values == 28.0


def test_a_sauna_no_longer_aims_at_a_pools_temperature(config_page):
    values = _add_and_retype(config_page, "sauna")
    assert values["target_temp"] == 90.0
    assert values["window_start"] is None


def test_a_house_leaves_its_physics_to_be_derived(config_page):
    values = _add_and_retype(config_page, "space_heating")
    assert values["target_temp"] == 21.5
    assert values["planning_mode"] == "temperature"
    assert values["volume_m3"] is None
    assert values["surface_m2"] is None
    assert values["heat_loss_w_per_m2_k"] is None


def test_the_form_shows_the_new_types_values(config_page):
    _add_and_retype(config_page, "space_heating")
    shown = config_page.evaluate("""() => {
            const idx = configurationManager._getListEntries('managed_loads').length - 1;
            const value = (name) => {
                const el = document.querySelector(
                    `input[data-key="managed_loads.${idx}.${name}"]`);
                return el ? el.value : null;
            };
            return {target: value("target_temp"), volume: value("volume_m3")};
        }""")
    assert shown["target"] == "21.5"
    # Derived, so shown empty - never as the text "null".
    assert shown["volume"] == ""
