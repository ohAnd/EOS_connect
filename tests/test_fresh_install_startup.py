"""
Does the application boot healthy on what the setup wizard writes?

``tests/web`` proves the wizard collects the right answers and stores them.  This is
the step after: the stored keys go through the merger the way ``start_db()`` does, and
the result is handed to the parts of startup that judge a configuration.  It is the
layer that would have caught a fresh install coming up degraded — the browser harness
substitutes the application module, so it structurally cannot see this.

The personas mirror ``tests/web/test_wizard_ui.py``; the dicts here are what the wizard
was observed to store for each.
"""

import pytest

from src.config_web.merger import build_merged_config
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
from src.interfaces.inverters.inverter_factory import create_inverter
from src.interfaces.pv_interface import PvInterface

# config.yaml holds only these on a first run.
BOOTSTRAP = {
    "eos_connect_web_port": 8081,
    "time_zone": "Europe/Berlin",
    "log_level": "info",
}

# What the migration seeds before the user answers anything.
SEEDED = {
    "data_source.type": "default",
    "data_source.url": "",
    "data_source.access_token": "",
    "price.feed_in_source": "fixed",
    "price.feed_in_zone": "DK1",
    "price.feed_in_static_adder": 0.0,
    "price.feed_in_multiplier": 1.0,
}

INSTALLATION = {
    "pv_forecast.0.name": "myPvInstallation1",
    "pv_forecast.0.lat": 47.5,
    "pv_forecast.0.lon": 8.5,
    "pv_forecast.0.azimuth": 90.0,
    "pv_forecast.0.tilt": 30.0,
    "pv_forecast.0.power": 4600,
}

BASE_ANSWERS = {
    "eos.source": "local_evopt",
    "inverter.type": "default",
    "load.load_sensor": "Load_Power",
    "battery.soc_sensor": "battery_SOC",
    "battery.capacity_wh": 11059,
    "battery.min_soc_percentage": 5,
    "battery.max_soc_percentage": 100,
    "price.source": "default",
    "price.feed_in_price": 0.0,
}

PERSONAS = {
    # Everything left as it arrives: the PV step is preselected to the built-in
    # source, so a user who only clicks Next stores no installation at all.
    "defaults": {
        **BASE_ANSWERS,
        "evcc.url": "http://yourEVCCserver:7070",
        "pv_forecast_source.source": "default",
    },
    "home_assistant_fronius_tibber": {
        **BASE_ANSWERS,
        "data_source.type": "homeassistant",
        "data_source.url": "http://homeassistant.local:8123",
        "data_source.access_token": "ha-token",
        "inverter.type": "fronius_gen24",
        "inverter.address": "192.168.1.50",
        "inverter.user": "customer",
        "inverter.password": "secret",
        "price.source": "tibber",
        "price.token": "tibber-token",
        "pv_forecast_source.source": "akkudoktor",
        **INSTALLATION,
    },
    "evcc_everywhere": {
        **BASE_ANSWERS,
        "evcc.url": "http://evcc.local:7070",
        "inverter.type": "evcc",
        "price.source": "evcc",
        "pv_forecast_source.source": "evcc",
    },
    "openhab_fixed_price": {
        **BASE_ANSWERS,
        "data_source.type": "openhab",
        "data_source.url": "http://openhab.local:8080",
        "price.source": "fixed_24h",
        "pv_forecast_source.source": "openmeteo",
        **INSTALLATION,
    },
    "solcast": {
        **BASE_ANSWERS,
        "pv_forecast_source.source": "solcast",
        "pv_forecast_source.api_key": "an-api-key",
        "pv_forecast_source.resource_id": "site-1",
    },
    "victron": {
        **BASE_ANSWERS,
        "inverter.type": "victron",
        "inverter.address": "192.168.1.60",
        "pv_forecast_source.source": "openmeteo_local",
        **INSTALLATION,
    },
    "timeseries_from_home_assistant": {
        **BASE_ANSWERS,
        "data_source.type": "homeassistant",
        "data_source.url": "http://homeassistant.local:8123",
        "data_source.access_token": "ha-token",
        "price.source": "timeseries",
        "price.data_url": "http://homeassistant.local:8123/api/states/sensor.prices",
        "price.value_unit": "EUR/kWh",
        "pv_forecast_source.source": "default",
    },
}


@pytest.fixture(autouse=True)
def no_update_thread(monkeypatch):
    """
    Keep the forecast fetcher out of it.

    A valid configuration is exactly the case where the update thread starts and makes
    a real request, so without this the tests would need the network to assert
    something that never touches it.
    """
    monkeypatch.setattr(
        PvInterface, "_PvInterface__start_update_service", lambda self: None
    )


@pytest.fixture(name="merged")
def merged_fixture(tmp_path):
    """Build the merged config for a persona, the way ``start_db()`` does."""
    stores = []

    def _build(persona):
        schema = ConfigSchema()
        store = ConfigStore(str(tmp_path / f"{persona}.db"))
        store.open()
        stores.append(store)
        store.set_batch({**SEEDED, **PERSONAS[persona]})
        return build_merged_config(dict(BOOTSTRAP), store, schema)

    yield _build
    for store in stores:
        store.close()


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_the_pv_interface_is_not_degraded(merged, persona):
    """
    A fresh install that answered every question must not come up with its forecast
    zeroed. Five of the nine sources need no installations, and used to be rejected
    anyway by a check that ran before the source was looked at.
    """
    config = merged(persona)

    pv = PvInterface(
        config["pv_forecast_source"],
        config["pv_forecast"],
        config["eos"]["time_frame"],
        {"url": config["evcc"]["url"], "data_source": {}},
        config["eos"]["source"] == "eos_server",
        config["time_zone"],
    )

    assert pv.configuration_valid is True, pv.configuration_state


def test_a_click_through_install_gets_a_forecast(merged):
    """
    Not being degraded is not the same as producing something. The built-in source
    validated as *valid* while summarizing an empty installation list into an empty
    forecast, so the least-configured install — the most common one — handed the
    optimizer no solar at all and said nothing about it.
    """
    config = merged("defaults")

    pv = PvInterface(
        config["pv_forecast_source"],
        config["pv_forecast"],
        config["eos"]["time_frame"],
        {"url": config["evcc"]["url"], "data_source": {}},
        config["eos"]["source"] == "eos_server",
        config["time_zone"],
    )
    forecast = pv.get_summarized_pv_forecast()

    assert config["pv_forecast"] == []
    assert forecast
    assert max(forecast) > 0


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_the_inverter_is_the_one_that_was_chosen(merged, persona):
    """
    A Fronius missing its address silently becomes a NullInverter, so "it constructed"
    is not enough — the type that comes back has to be the type that was asked for.
    """
    config = merged(persona)
    chosen = PERSONAS[persona]["inverter.type"]

    inverter = create_inverter(config["inverter"])

    expected = {
        "default": "NullInverter",
        "evcc": "EvccInverter",
        "fronius_gen24": "FroniusV2",
        "victron": "VictronInverter",
    }[chosen]
    assert type(inverter).__name__ == expected


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_no_installation_is_invented(merged, persona):
    """
    The merger synthesizes an installation from unindexed ``pv_forecast.*`` keys. A
    source that uses none must end up with an empty list, not a solar array at the
    schema's default coordinates.
    """
    config = merged(persona)
    needs_one = PERSONAS[persona]["pv_forecast_source.source"] in (
        "akkudoktor", "openmeteo", "openmeteo_local", "forecast_solar",
    )

    if needs_one:
        assert len(config["pv_forecast"]) == 1
    else:
        assert config["pv_forecast"] == []


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_the_answers_survive_the_merge(merged, persona):
    """What the user chose is what the interfaces are handed."""
    config = merged(persona)
    answers = PERSONAS[persona]

    assert config["eos"]["source"] == answers["eos.source"]
    assert config["price"]["source"] == answers["price.source"]
    assert config["inverter"]["type"] == answers["inverter.type"]
    assert config["battery"]["capacity_wh"] == answers["battery.capacity_wh"]
    assert config["pv_forecast_source"]["source"] == answers["pv_forecast_source.source"]


def test_the_bootstrap_timezone_wins_over_the_database(merged):
    """
    ``time_zone`` lives in config.yaml. The wizard used to post it anyway; the merger
    ignores the stored copy, and this pins that — a stray row must never override the
    file the operator edits.
    """
    config = merged("defaults")

    assert config["time_zone"] == BOOTSTRAP["time_zone"]


@pytest.mark.parametrize("persona", ["home_assistant_fronius_tibber", "openhab_fixed_price"])
def test_the_data_source_reaches_the_sections_that_read_sensors(merged, persona):
    """
    Load and battery do not have their own connection any more — they inherit the one
    the Data Source step configures. If that inheritance breaks, both silently fall
    back to synthetic data and the dashboard looks plausible while being fiction.
    """
    config = merged(persona)
    answers = PERSONAS[persona]

    for section in ("load", "battery"):
        assert config[section]["source"] == answers["data_source.type"]
        assert config[section]["url"] == answers["data_source.url"]
