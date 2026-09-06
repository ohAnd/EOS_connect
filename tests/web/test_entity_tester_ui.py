"""
Is the sensor connection test actually placed like a connection test?

The first cut put the Test button inside the field's input column. Every input in the
config panel is ``width: 100%``, so the button wrapped underneath and rendered as a
stray control floating under the box — not as part of the field, and nothing like the
timeseries "Test connection" panel that had already established the pattern.

Geometry is the only honest way to check that, so these read the laid-out boxes rather
than the markup.
"""

import json

import pytest

import tests.web.wizard_driver as wz  # noqa: F401  (keeps the shared skip behaviour)


SENSOR_KEYS = [
    "load.load_sensor",
    "load.car_charge_load_sensor",
    "battery.soc_sensor",
    "battery.sensor_battery_temperature",
]


def _css_key(key):
    return key.replace(".", "-")


def _open_config(page):
    """Open the configuration overlay and wait for it to render its fields."""
    page.evaluate("() => showConfigurationMenu()")
    page.wait_for_selector(".config-field")


def _select_section(page, section):
    page.evaluate(f"() => configurationManager._selectSection('{section}')")
    page.wait_for_selector(f'[data-key^="{section}."]')


def _box(page, selector):
    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) { return null; }
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        }""",
        selector,
    )


def _label_style(page, testerSelector):
    """Font size and colour of a tester row's label, as actually computed."""
    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel + ' .config-field-label');
            if (!el) { return null; }
            const s = getComputedStyle(el);
            return {fontSize: s.fontSize, color: s.color};
        }""",
        testerSelector,
    )


@pytest.fixture(name="config_page")
def config_page_fixture(page):
    _open_config(page)
    return page


@pytest.mark.parametrize("key", SENSOR_KEYS)
def test_every_sensor_field_gets_a_tester_row(config_page, key):
    """All of them, not just the two the first pass happened to cover."""
    section = key.split(".")[0]
    _select_section(config_page, section)

    assert config_page.query_selector(f'[data-entity-tester="{key}"]') is not None


@pytest.mark.parametrize("key", SENSOR_KEYS)
def test_the_tester_sits_below_its_field_and_not_inside_it(config_page, key):
    """
    The row belongs to the field above it, and is a sibling of it — not a control
    crammed into the input column, which is what made it float.
    """
    section = key.split(".")[0]
    _select_section(config_page, section)

    field = _box(config_page, f"#cfg-field-{_css_key(key)}")
    tester = _box(config_page, f'[data-entity-tester="{key}"]')

    assert field is not None and tester is not None
    assert tester["y"] >= field["y"] + field["h"] - 1, "the tester must follow its field"
    nested = config_page.evaluate(
        """(k) => {
            const f = document.getElementById('cfg-field-' + k.replace(/\\./g, '-'));
            return f ? f.querySelector('[data-entity-tester]') !== null : true;
        }""",
        key,
    )
    assert nested is False, "the tester is a sibling row, not a child of the field"


@pytest.mark.parametrize("key", SENSOR_KEYS)
def test_the_button_lines_up_with_the_inputs_above_it(config_page, key):
    """
    The point of the labelled row: the button starts on the same left edge as every
    input in the panel, instead of hanging under one at an arbitrary offset.
    """
    section = key.split(".")[0]
    _select_section(config_page, section)

    field_input = _box(config_page, f"#cfg-field-{_css_key(key)} .config-field-input")
    button = _box(config_page, f"#cfg-entity-test-{_css_key(key)}")

    assert field_input is not None and button is not None
    assert abs(button["x"] - field_input["x"]) <= 1, (
        f"button at x={button['x']}, inputs at x={field_input['x']}"
    )


def test_it_is_laid_out_like_the_timeseries_connection_test(config_page):
    """
    The established pattern, and the one the user pointed at: a labelled row whose
    button sits in the input column. Both testers must agree on that geometry.
    """
    config_page.evaluate(
        """() => {
            configurationManager.values['price.source'] = 'timeseries';
            configurationManager._selectSection('price');
        }"""
    )
    config_page.wait_for_selector("[data-timeseries-tester]")
    ts_label = _box(config_page, "[data-timeseries-tester] .config-field-label")
    ts_button = _box(config_page, "[data-timeseries-tester] .config-btn")
    ts_label_style = _label_style(config_page, "[data-timeseries-tester]")

    _select_section(config_page, "battery")
    en_label = _box(config_page, '[data-entity-tester] .config-field-label')
    en_button = _box(config_page, "[data-entity-tester] .config-btn")
    en_label_style = _label_style(config_page, "[data-entity-tester]")

    assert ts_label is not None and en_label is not None
    # Same label column width, and the button in the same column after it.
    assert abs(ts_label["w"] - en_label["w"]) <= 1
    assert abs(ts_button["x"] - en_button["x"]) <= 1
    # And the same weight: a dimmer, smaller label would read as a different kind of
    # thing, which is exactly what "one standard" rules out.
    assert ts_label_style == en_label_style


def test_the_tester_hides_with_its_field(config_page):
    """A Test button for a field that does not apply is a control that cannot act."""
    _select_section(config_page, "battery")

    config_page.evaluate(
        """() => {
            configurationManager.values['data_source.type'] = 'default';
            configurationManager._updateDependencies('data_source.type');
        }"""
    )

    hidden = config_page.evaluate(
        """() => document.querySelector('[data-entity-tester="battery.soc_sensor"]')
                    .classList.contains('hidden')"""
    )
    assert hidden is True


def test_a_failing_test_reports_it_in_place(config_page):
    """The verdict lands in the row's own result area, styled like the other tester."""
    _select_section(config_page, "battery")
    config_page.route(
        "**/api/config/test-entity",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": False, "error": "Entity not found (404)."}),
        ),
    )

    config_page.click("#cfg-entity-test-battery-soc_sensor")
    config_page.wait_for_selector("#cfg-entity-result-battery-soc_sensor.error")

    text = config_page.inner_text("#cfg-entity-result-battery-soc_sensor")
    assert "not found" in text


def test_a_group_of_only_sensors_still_collapses(config_page):
    """
    The tester rows carry .config-field too, so _updateGroupVisibility counts them.
    They must not hold a group open after every real field in it has gone.
    """
    _select_section(config_page, "load")

    config_page.evaluate(
        """() => {
            configurationManager.values['data_source.type'] = 'default';
            configurationManager._updateDependencies('data_source.type');
        }"""
    )

    hidden = config_page.evaluate(
        """() => {
            const g = [...document.querySelectorAll('.config-group[data-group]')]
                .find(e => e.getAttribute('data-group') === 'Sensors');
            return g ? g.classList.contains('hidden') : null;
        }"""
    )
    assert hidden is True


# ── Managed loads ───────────────────────────────────────────────────────────────
#
# Managed loads are the first list section whose entries are heterogeneous: which
# fields apply depends on the type picked in that card, so their dependencies are
# resolved *within* the entry. The tester row did not get that scope passed to it, so
# every sensor field with a type dependency rendered its Test button permanently
# hidden — visible field, no way to test it. Only the power sensor, which has no type
# dependency, escaped, which is exactly how it was reported.

MANAGED_SENSOR_KEYS = [
    "managed_loads.0.temp_sensor",
    "managed_loads.0.target_temp_sensor",
    "managed_loads.0.ambient_temp_sensor",
    "managed_loads.0.power_sensor",
    "managed_loads.0.cover_sensor",
]


def _seed_entry(page, entry_type, index=0):
    """Put one managed load into the form and render its section at expert level."""
    page.evaluate(
        """([type, index]) => {
            const cm = configurationManager;
            cm._setLevel('expert');
            for (const k of Object.keys(cm.values)) {
                if (/^managed_loads\\.\\d+\\./.test(k)) { delete cm.values[k]; }
            }
            cm.values[`managed_loads.${index}.id`] = 'pool';
            cm.values[`managed_loads.${index}.type`] = type;
            cm._selectSection('managed_loads');
        }""",
        [entry_type, index],
    )
    page.wait_for_selector('[data-entry-section="managed_loads"]')


def _is_visible(page, selector):
    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            return el ? el.offsetParent !== null : false;
        }""",
        selector,
    )


@pytest.mark.parametrize("key", MANAGED_SENSOR_KEYS)
def test_a_pool_sensor_field_gets_a_visible_tester(config_page, key):
    """Rendered *and* visible — it was rendered all along, just always hidden."""
    _seed_entry(config_page, "pool_heatpump")

    assert config_page.query_selector(f'[data-entity-tester="{key}"]') is not None
    assert _is_visible(config_page, f'[data-entity-tester="{key}"]'), (
        f"{key} has a Test button the user cannot see"
    )


@pytest.mark.parametrize("key", MANAGED_SENSOR_KEYS)
def test_a_visible_field_always_has_a_visible_tester(config_page, key):
    """
    The invariant behind the bug: a field and its connection test travel together.
    Either both apply to this entry or neither does.
    """
    _seed_entry(config_page, "pool_heatpump")

    field_visible = _is_visible(config_page, f"#cfg-field-{_css_key(key)}")
    tester_visible = _is_visible(config_page, f'[data-entity-tester="{key}"]')
    assert field_visible == tester_visible, (
        f"{key}: field visible={field_visible}, tester visible={tester_visible}"
    )


def test_a_pushed_profile_hides_the_thermal_fields_and_their_testers(config_page):
    """The other half: an entry that does not use a sensor must not offer to test it."""
    _seed_entry(config_page, "external_profile")

    assert not _is_visible(config_page, '[data-entity-tester="managed_loads.0.temp_sensor"]')
    assert not _is_visible(config_page, "#cfg-field-managed_loads-0-temp_sensor")
    # A pushed profile still has a power sensor, so the base load can be corrected.
    assert _is_visible(config_page, '[data-entity-tester="managed_loads.0.power_sensor"]')


def test_two_entries_of_different_types_are_judged_independently(config_page):
    """The reason the dependency has to be entry-relative at all."""
    config_page.evaluate(
        """() => {
            const cm = configurationManager;
            cm._setLevel('expert');
            cm.values['managed_loads.0.id'] = 'pool';
            cm.values['managed_loads.0.type'] = 'pool_heatpump';
            cm.values['managed_loads.1.id'] = 'heating';
            cm.values['managed_loads.1.type'] = 'external_profile';
            cm._selectSection('managed_loads');
        }"""
    )
    config_page.wait_for_selector('[data-entry-idx="1"]')

    assert _is_visible(config_page, '[data-entity-tester="managed_loads.0.temp_sensor"]')
    assert not _is_visible(config_page, '[data-entity-tester="managed_loads.1.temp_sensor"]')


def test_only_a_pool_offers_a_cover_sensor(config_page):
    _seed_entry(config_page, "hot_water_tank")
    assert not _is_visible(config_page, '[data-entity-tester="managed_loads.0.cover_sensor"]')

    _seed_entry(config_page, "pool_heatpump")
    assert _is_visible(config_page, '[data-entity-tester="managed_loads.0.cover_sensor"]')
