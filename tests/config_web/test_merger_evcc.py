"""
EVCC as data source: what the merger injects and what it must not touch.

Selecting EVCC as the central data source has to fill the load section's virtual
entities (so a switch away from Home Assistant does not leave an empty sensor
behind), borrow the EVCC URL from the evcc section, and leave every field the
user typed themselves alone.
"""

# pylint: disable=protected-access
from unittest.mock import patch

import pytest

from src.config_web.merger import (
    _apply_data_source_inheritance,
    build_merged_config,
)
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore


@pytest.fixture(autouse=True)
def _no_evcc_call():
    """No test here reaches a real EVCC.

    The resolution asks EVCC for its battery parameters, and the merge now runs
    that on every pass rather than once - an unmocked test would hang on a
    proxy or, worse, pick up whatever answers at that address.
    """
    with patch(
        "src.interfaces.evcc_parameters.fetch_evcc_parameters", return_value=None
    ):
        yield


EVCC_SETTINGS = {
    "data_source.type": "evcc",
    "data_source.url": "http://homeassistant:8123",  # stale HA URL, unused
    "data_source.access_token": "ha-token",
    "data_source.ssl_ignore": False,
    "evcc.url": "http://evcc:7070",
}


def _run(settings, load=None, battery=None):
    """Apply the inheritance and return ``(merged_result, resolved_values)``.

    The resolution derives; it never writes. *settings* is copied anyway so a
    test that shares a fixture dict with the next case cannot be the thing that
    proves it - two tests assert on the untouched copy directly.
    """
    settings = dict(settings)
    result = {
        "evcc": {"url": settings.get("evcc.url", "")},
        "load": (
            load
            if load is not None
            else {"source": "default", "url": "", "access_token": ""}
        ),
        "battery": (
            battery
            if battery is not None
            else {"source": "default", "url": "", "access_token": ""}
        ),
    }
    resolved = _apply_data_source_inheritance(result, settings)
    return result, resolved


def test_evcc_url_comes_from_the_evcc_section():
    result, _resolved = _run(EVCC_SETTINGS)
    assert result["load"]["source"] == "evcc"
    assert result["load"]["url"] == "http://evcc:7070"
    assert result["battery"]["source"] == "evcc"
    assert result["battery"]["url"] == "http://evcc:7070"
    # No token is forwarded: EVCC needs none, and a stale HA token sent as a
    # bearer would be harmless noise that hides a wrong connection.
    assert result["load"]["access_token"] == ""


def test_placeholder_evcc_url_leaves_the_sections_empty():
    """The schema placeholder is not an EVCC instance - a half-configured setup
    must not look connected."""
    result, _resolved = _run(
        {**EVCC_SETTINGS, "evcc.url": "http://yourEVCCserver:7070"}
    )
    assert result["load"]["url"] == ""
    assert result["load"]["source"] == "evcc"


def test_virtual_entities_are_prefilled():
    result, _resolved = _run(EVCC_SETTINGS)
    assert result["load"]["load_sensor"] == "group:home"
    # No consumer title configured - the additional load stays empty, not guessed.
    assert result["load"].get("additional_load_1_sensor", "") == ""


def test_the_wallbox_field_stays_empty_on_evcc():
    """``group:home`` is EVCC's residual: the loadpoints are already out of it.

    Naming the wallbox here would subtract the charging energy a second time,
    from a base load that never carried it. The field is not offered on an EVCC
    data source, and the merger leaves nothing in it to act on.
    """
    result, _resolved = _run(EVCC_SETTINGS)
    assert result["load"]["car_charge_load_sensor"] == ""


def test_consumer_title_fills_the_additional_load():
    result, _resolved = _run(
        {
            **EVCC_SETTINGS,
            "data_source.evcc_consumer_title": "HeatPump",
        }
    )
    assert result["load"]["load_sensor"] == "group:home"
    assert result["load"]["additional_load_1_sensor"] == "group:consumer:HeatPump"


def test_user_typed_entities_are_never_overwritten():
    result, _resolved = _run(
        EVCC_SETTINGS,
        load={
            "source": "default",
            "url": "",
            "access_token": "",
            "load_sensor": "group:grid",
            "additional_load_1_sensor": "group:consumer:Sauna",
        },
    )
    assert result["load"]["load_sensor"] == "group:grid"
    assert result["load"]["additional_load_1_sensor"] == "group:consumer:Sauna"


def test_expert_homeassistant_override_is_kept():
    """A load section kept on Home Assistant while the source is EVCC elsewhere."""
    result, _resolved = _run(
        EVCC_SETTINGS,
        load={
            "source": "homeassistant",
            "url": "http://ha.local:8123",
            "access_token": "tok",
            "load_sensor": "sensor.house",
        },
    )
    assert result["load"]["source"] == "homeassistant"
    assert result["load"]["url"] == "http://ha.local:8123"
    assert result["load"]["access_token"] == "tok"
    # No virtual entities injected for a section that did not switch.


def test_stale_ha_sensor_names_are_prefilled_on_a_switch():
    """Switching an existing HA setup to EVCC must not leave HA entity ids in place.

    An HA entity id fails the virtual-entity grammar, and a failed grammar check
    means the built-in default profile - silently, on a setup that looks
    configured. The prefill only claims names that are unmistakably Home
    Assistant's ("sensor." prefix); anything else typed by a user stays.
    """
    result, _resolved = _run(
        EVCC_SETTINGS,
        load={
            "source": "default",
            "url": "",
            "access_token": "",
            "load_sensor": "sensor.eos_hausverbrauch_gesamt_w",
            "car_charge_load_sensor": "sensor.evcc_charger_charge_power",
            "additional_load_1_sensor": "sensor.other",
        },
    )
    assert result["load"]["load_sensor"] == "group:home"
    # The wallbox name goes, it does not move: see the residual above.
    assert result["load"]["car_charge_load_sensor"] == ""
    # Without a consumer title configured, the additional load is dropped instead
    # of pointing somewhere wrong.
    assert result["load"]["additional_load_1_sensor"] == ""


def test_already_virtual_entities_are_kept():
    """An EVCC setup restored from a backup keeps its own virtual entities."""
    result, _resolved = _run(
        EVCC_SETTINGS,
        load={
            "source": "default",
            "url": "",
            "access_token": "",
            "load_sensor": "group:home",
            "additional_load_1_sensor": "group:consumer:Carport",
        },
    )
    assert result["load"]["load_sensor"] == "group:home"
    assert result["load"]["additional_load_1_sensor"] == "group:consumer:Carport"


def test_battery_stale_names_move_to_evcc_equivalents():
    """SOC and the price-calc sensors switch to EVCC's own readings.

    These names come from a Home Assistant setup where EVCC values were mirrored
    into HA entities; on an EVCC data source the same quantities are read from
    EVCC directly, and leaving the HA names in place would strand the SOC at 5%
    and silence the price calculation.
    """
    result, _resolved = _run(
        EVCC_SETTINGS,
        battery={
            "source": "default",
            "url": "",
            "access_token": "",
            "soc_sensor": "sensor.evcc_battery_soc",
            "battery_power_sensor": "sensor.evcc_battery_power",
            "pv_power_sensor": "sensor.evcc_pv_power",
            "grid_power_sensor": "sensor.evcc_grid_power",
            "load_power_sensor": "sensor.eos_hausverbrauch_gesamt_w",
            "price_sensor": "sensor.aktueller_strompreis_cent",
        },
    )
    assert result["battery"]["soc_sensor"] == "battery.soc"
    assert result["battery"]["battery_power_sensor"] == "group:battery"
    assert result["battery"]["pv_power_sensor"] == "group:pv"
    assert result["battery"]["grid_power_sensor"] == "group:grid"
    assert result["battery"]["load_power_sensor"] == "group:home"
    assert result["battery"]["price_sensor"] == "tariff:grid"


def test_battery_existing_virtual_entities_are_kept():
    result, _resolved = _run(
        EVCC_SETTINGS,
        battery={
            "source": "default",
            "soc_sensor": "battery.soc",
            "price_sensor": "tariff:grid",
            "battery_power_sensor": "group:battery",
        },
    )
    assert result["battery"]["soc_sensor"] == "battery.soc"
    assert result["battery"]["price_sensor"] == "tariff:grid"
    assert result["battery"]["battery_power_sensor"] == "group:battery"


def test_battery_untouched_when_source_not_evcc():
    result, _resolved = _run(
        {
            "data_source.type": "homeassistant",
            "data_source.url": "http://ha:8123",
            "data_source.access_token": "",
            "data_source.ssl_ignore": False,
            "evcc.url": "http://evcc:7070",
        },
        battery={"source": "default", "soc_sensor": "sensor.battery_soc"},
    )
    assert result["battery"]["soc_sensor"] == "sensor.battery_soc"


def test_a_shipped_default_url_is_not_an_evcc_address():
    """The default left from the previous connection does not win.

    ``http://homeassistant:8123`` in the Data Source field is what the schema
    ships, not something the user typed at an EVCC instance - so the evcc
    section's own address stands in.
    """
    settings = {**EVCC_SETTINGS, "data_source.url": "http://homeassistant:8123"}
    result, resolved = _run(settings)
    assert result["load"]["url"] == "http://evcc:7070"
    assert result["evcc"]["url"] == "http://evcc:7070"
    assert resolved["data_source.url"] == "http://evcc:7070"


def test_a_typed_url_wins_and_reaches_the_evcc_section():
    """What the user typed into the Data Source field is the address.

    Both keys follow it, so the roles reading the evcc section - inverter
    controller, price and PV source - talk to the same instance.
    """
    settings = {
        "data_source.type": "evcc",
        "data_source.url": "http://my-evcc.local:7070",
        "data_source.access_token": "",
        "data_source.ssl_ignore": False,
        "evcc.url": "http://yourEVCCserver:7070",
    }
    result, resolved = _run(settings)
    assert result["load"]["url"] == "http://my-evcc.local:7070"
    assert result["evcc"]["url"] == "http://my-evcc.local:7070"
    assert resolved["data_source.url"] == "http://my-evcc.local:7070"


def test_a_changed_url_is_not_reverted_to_the_stored_one():
    """Moving EVCC to another host has to stick.

    The earlier version mirrored both keys into the store, so the next merge
    overwrote the freshly typed address with the old one - and the page holding
    the other copy is hidden while EVCC is the data source, which left no way
    to change the address at all.
    """
    settings = {
        "data_source.type": "evcc",
        "data_source.url": "http://new-host:7070",  # just typed
        "data_source.access_token": "",
        "data_source.ssl_ignore": False,
        "evcc.url": "http://old-host:7070",  # what the store still holds
    }
    before = dict(settings)
    result, resolved = _run(settings)
    assert result["load"]["url"] == "http://new-host:7070"
    assert resolved["data_source.url"] == "http://new-host:7070"
    # And the resolution kept its hands off the settings it was given.
    assert settings == before


def test_nothing_is_written_back_into_the_settings():
    """The resolution derives; it never edits the store it reads."""
    settings = {**EVCC_SETTINGS}
    before = dict(settings)
    _run(settings)
    assert settings == before


def test_capacity_comes_from_the_evcc_battery_meter():
    """Capacity is a physical fact the meter knows better than a typed number.

    Efficiency and charge-power limits are NOT exposed by EVCC and stay exactly
    as configured.
    """
    battery = {
        "source": "default",
        "url": "http://evcc:7070",
        "soc_sensor": "sensor.battery_soc",
        "capacity_wh": 11059,
        "charge_efficiency": 0.88,
        "max_charge_power_w": 5000,
    }
    with patch(
        "src.interfaces.evcc_parameters.fetch_evcc_parameters",
        return_value={"capacity_wh": 15000},
    ):
        result, resolved = _run(EVCC_SETTINGS, battery=battery)
    assert result["battery"]["capacity_wh"] == 15000
    assert resolved["battery.capacity_wh"] == 15000
    assert result["battery"]["charge_efficiency"] == 0.88
    assert result["battery"]["max_charge_power_w"] == 5000


def test_an_unreachable_evcc_leaves_the_configured_capacity():
    """No answer is not a reason to overwrite what the user configured."""
    result, _resolved = _run(
        EVCC_SETTINGS,
        battery={
            "source": "default",
            "url": "http://evcc:7070",
            "soc_sensor": "sensor.battery_soc",
            "capacity_wh": 11059,
        },
    )
    assert result["battery"]["capacity_wh"] == 11059


def test_other_types_do_not_get_evcc_treatment():
    result, _resolved = _run(
        {
            "data_source.type": "homeassistant",
            "data_source.url": "http://ha.local:8123",
            "data_source.access_token": "tok",
            "data_source.ssl_ignore": False,
            "evcc.url": "http://evcc:7070",
        }
    )
    assert result["load"]["url"] == "http://ha.local:8123"
    assert result["load"]["access_token"] == "tok"
    assert "load_sensor" not in result["load"]


def test_supplementary_connection_reaches_the_battery_section():
    """The temp sensor's second connection lands in the battery config.

    The battery interface reads the temperature through it when EVCC is the
    source and the sensor name is still a Home Assistant entity - without the
    injection it would have to guess the host EVCC knows nothing of.
    """
    settings = {
        **EVCC_SETTINGS,
        "data_source.supplementary_type": "homeassistant",
        "data_source.supplementary_url": "http://ha.local:8123",
        "data_source.supplementary_access_token": "supp-tok",
    }
    result, _settings = _run(settings)
    battery = result["battery"]
    assert battery["supplementary_type"] == "homeassistant"
    assert battery["supplementary_url"] == "http://ha.local:8123"
    assert battery["supplementary_access_token"] == "supp-tok"
    # A non-remote supplementary choice means "no second connection": the keys
    # are blanked rather than left over from a previous save.
    settings["data_source.supplementary_type"] = "default"
    result, _settings = _run(settings)
    assert result["battery"]["supplementary_type"] == ""
    assert result["battery"]["supplementary_url"] == ""


def test_temp_sensor_stays_readable_through_supplementary_without_a_rewrite():
    """sensor.* temperature names are NOT switched to EVCC virtual entities.

    Switching them would lie - EVCC has no temperature. They keep their name and
    the supplementary connection carries them.
    """
    result, _settings = _run(
        {
            **EVCC_SETTINGS,
            "data_source.supplementary_type": "openhab",
            "data_source.supplementary_url": "http://oh.local:8080",
        },
        battery={
            "source": "default",
            "soc_sensor": "sensor.battery_soc",
            "sensor_battery_temperature": "sensor.battery_temp",
        },
    )
    assert result["battery"]["sensor_battery_temperature"] == "sensor.battery_temp"


def test_the_merge_leaves_the_store_untouched(tmp_path):
    """End to end: a full merge on an EVCC setup writes nothing.

    The resolution used to mirror its results into the store so the settings UI
    would see them. That pinned derived values - a capacity from before the
    battery was extended, an address the next merge overwrote - and put them
    into ``/api/config/export``, which is also the backup payload. They reach
    the UI through ``resolved_out`` instead.
    """
    store = ConfigStore(str(tmp_path / "merge.db"))
    store.open()
    try:
        store.set_batch(
            {
                "data_source.type": "evcc",
                "data_source.url": "http://evcc.local:7070",
                "load.load_sensor": "sensor.stale_from_home_assistant",
            }
        )
        before = store.get_all()

        resolved = {}
        merged = build_merged_config({}, store, ConfigSchema(), resolved_out=resolved)

        assert store.get_all() == before, "the merge is read-only on the store"
        # ...while the resolution still reached the merged config and the UI.
        assert merged["load"]["load_sensor"] == "group:home"
        assert resolved["load.load_sensor"] == "group:home"
        assert resolved["data_source.url"] == "http://evcc.local:7070"
    finally:
        store.close()


def test_the_soc_window_comes_from_evcc_when_the_battery_reports_it():
    """What EVCC reads off the battery beats a hand-typed range."""
    with patch(
        "src.interfaces.evcc_parameters.fetch_evcc_parameters",
        return_value={"min_soc_percentage": 12, "max_soc_percentage": 96},
    ):
        result, resolved = _run(
            EVCC_SETTINGS,
            battery={
                "source": "default",
                "url": "http://evcc:7070",
                "min_soc_percentage": 5,
                "max_soc_percentage": 100,
                "charge_efficiency": 0.88,
            },
        )
    assert result["battery"]["min_soc_percentage"] == 12
    assert result["battery"]["max_soc_percentage"] == 96
    assert resolved["battery.min_soc_percentage"] == 12
    # Still not EVCC's to know:
    assert result["battery"]["charge_efficiency"] == 0.88


def test_a_battery_without_soc_limits_keeps_the_configured_range():
    """EVCC's SoC-limit capability is optional, so the field must stay usable.

    It is also left out of the resolution, which is what keeps the settings page
    rendering it as an input the user can set rather than a read-only row.
    """
    with patch(
        "src.interfaces.evcc_parameters.fetch_evcc_parameters",
        return_value={"capacity_wh": 15000},
    ):
        result, resolved = _run(
            EVCC_SETTINGS,
            battery={
                "source": "default",
                "url": "http://evcc:7070",
                "min_soc_percentage": 5,
                "max_soc_percentage": 100,
            },
        )
    assert result["battery"]["min_soc_percentage"] == 5
    assert result["battery"]["max_soc_percentage"] == 100
    assert "battery.min_soc_percentage" not in resolved
    assert "battery.max_soc_percentage" not in resolved
