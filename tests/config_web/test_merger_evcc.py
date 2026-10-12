"""
EVCC as data source: what the merger injects and what it must not touch.

Selecting EVCC as the central data source has to fill the load section's virtual
entities (so a switch away from Home Assistant does not leave an empty sensor
behind), borrow the EVCC URL from the evcc section, and leave every field the
user typed themselves alone.
"""

# pylint: disable=protected-access
from src.config_web.merger import _apply_data_source_inheritance

EVCC_SETTINGS = {
    "data_source.type": "evcc",
    "data_source.url": "http://homeassistant:8123",  # stale HA URL, unused
    "data_source.access_token": "ha-token",
    "data_source.ssl_ignore": False,
    "evcc.url": "http://evcc:7070",
}


def _run(settings, load=None, battery=None):
    result = {
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
    _apply_data_source_inheritance(result, settings)
    return result


def test_evcc_url_comes_from_the_evcc_section():
    result = _run(EVCC_SETTINGS)
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
    result = _run({**EVCC_SETTINGS, "evcc.url": "http://yourEVCCserver:7070"})
    assert result["load"]["url"] == ""
    assert result["load"]["source"] == "evcc"


def test_virtual_entities_are_prefilled():
    result = _run({**EVCC_SETTINGS, "data_source.evcc_loadpoint_title": "Garage"})
    assert result["load"]["load_sensor"] == "group:home"
    assert result["load"]["car_charge_load_sensor"] == "group:loadpoint:Garage"
    # No consumer title configured - the additional load stays empty, not guessed.
    assert result["load"].get("additional_load_1_sensor", "") == ""


def test_consumer_title_fills_the_additional_load():
    result = _run(
        {
            **EVCC_SETTINGS,
            "data_source.evcc_loadpoint_title": "",
            "data_source.evcc_consumer_title": "HeatPump",
        }
    )
    assert result["load"]["load_sensor"] == "group:home"
    assert result["load"]["car_charge_load_sensor"] == "group:loadpoint"
    assert result["load"]["additional_load_1_sensor"] == "group:consumer:HeatPump"


def test_user_typed_entities_are_never_overwritten():
    result = _run(
        {**EVCC_SETTINGS, "data_source.evcc_loadpoint_title": "Garage"},
        load={
            "source": "default",
            "url": "",
            "access_token": "",
            "load_sensor": "group:grid",
            "car_charge_load_sensor": "group:loadpoint:Carport",
            "additional_load_1_sensor": "group:consumer:Sauna",
        },
    )
    assert result["load"]["load_sensor"] == "group:grid"
    assert result["load"]["car_charge_load_sensor"] == "group:loadpoint:Carport"
    assert result["load"]["additional_load_1_sensor"] == "group:consumer:Sauna"


def test_expert_homeassistant_override_is_kept():
    """A load section kept on Home Assistant while the source is EVCC elsewhere."""
    result = _run(
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
    result = _run(
        {**EVCC_SETTINGS, "data_source.evcc_loadpoint_title": "Garage"},
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
    assert result["load"]["car_charge_load_sensor"] == "group:loadpoint:Garage"
    # Without a consumer title configured, the additional load is dropped instead
    # of pointing somewhere wrong.
    assert result["load"]["additional_load_1_sensor"] == ""


def test_already_virtual_entities_are_kept():
    """An EVCC setup restored from a backup keeps its own virtual entities."""
    result = _run(
        EVCC_SETTINGS,
        load={
            "source": "default",
            "url": "",
            "access_token": "",
            "load_sensor": "group:home",
            "car_charge_load_sensor": "group:loadpoint:Carport",
        },
    )
    assert result["load"]["load_sensor"] == "group:home"
    assert result["load"]["car_charge_load_sensor"] == "group:loadpoint:Carport"


def test_battery_stale_names_move_to_evcc_equivalents():
    """SOC and the price-calc sensors switch to EVCC's own readings.

    These names come from a Home Assistant setup where EVCC values were mirrored
    into HA entities; on an EVCC data source the same quantities are read from
    EVCC directly, and leaving the HA names in place would strand the SOC at 5%
    and silence the price calculation.
    """
    result = _run(
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
    result = _run(
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
    result = _run(
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


def test_other_types_do_not_get_evcc_treatment():
    result = _run(
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
