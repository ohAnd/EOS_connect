# pylint: disable=redefined-outer-name
"""
Unit tests for the merged config builder.

Note: redefined-outer-name is disabled because pytest fixtures with the same
names are a standard pattern for parameterized and scoped test fixtures.
"""

import pytest
from src.config_web.store import ConfigStore
from src.config_web.schema import ConfigSchema
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.merger import build_merged_config


@pytest.fixture
def schema():
    """Fresh schema instance."""
    return ConfigSchema()


@pytest.fixture
def store(tmp_path):
    """Empty ConfigStore in a temp directory."""
    s = ConfigStore(str(tmp_path / "test.db"))
    s.open()
    yield s
    s.close()


def _sample_config():
    """Minimal config dict matching the real config.yaml structure."""
    return {
        "load": {
            "source": "homeassistant",
            "url": "http://homeassistant:8123",
            "access_token": "my_token",
            "load_sensor": "sensor.power",
            "car_charge_load_sensor": "sensor.wallbox",
            "additional_load_1_sensor": "",
            "additional_load_1_runtime": 0,
            "additional_load_1_consumption": 0,
        },
        "eos": {
            "source": "eos_server",
            "server": "192.168.1.100",
            "port": 8503,
            "timeout": 180,
            "time_frame": 3600,
            "dyn_override_discharge_allowed_pv_greater_load": False,
            "pv_battery_charge_control_enabled": False,
        },
        "price": {
            "source": "tibber",
            "token": "tok",
            "fixed_price_adder_ct": 0,
            "relative_price_multiplier": 0,
            "feed_in_price": 0.08,
            "negative_price_switch": False,
            "energyforecast_enabled": False,
            "energyforecast_token": "demo",
            "energyforecast_market_zone": "DE-LU",
        },
        "battery": {
            "source": "homeassistant",
            "url": "http://homeassistant:8123",
            "access_token": "my_token",
            "soc_sensor": "sensor.soc",
            "capacity_wh": 10000,
            "charge_efficiency": 0.88,
            "discharge_efficiency": 0.88,
            "max_charge_power_w": 5000,
            "min_soc_percentage": 5,
            "max_soc_percentage": 100,
            "charging_curve_enabled": True,
            "sensor_battery_temperature": "",
            "price_ct_kwh_accu": 0.0,
            "price_euro_per_wh_sensor": "",
            "price_calculation_enabled": False,
            "price_update_interval": 900,
            "price_history_lookback_hours": 96,
            "battery_power_sensor": "",
            "pv_power_sensor": "",
            "grid_power_sensor": "",
            "load_power_sensor": "",
            "price_sensor": "",
            "charging_threshold_w": 50.0,
            "grid_charge_threshold_w": 100.0,
            "battery_price_include_feedin": False,
        },
        "pv_forecast_source": {"source": "akkudoktor", "api_key": ""},
        "pv_forecast": [{"name": "Roof", "lat": 48.0, "lon": 9.0,
                         "azimuth": 90, "tilt": 30, "power": 4600,
                         "powerInverter": 5000, "inverterEfficiency": 0.9,
                         "horizon": "10,20", "resource_id": ""}],
        "inverter": {"type": "default", "address": "192.168.1.12",
                     "user": "customer", "password": "abc",
                     "max_grid_charge_rate": 5000, "max_pv_charge_rate": 5000},
        "evcc": {"url": "http://yourEVCCserver:7070"},
        "mqtt": {"enabled": False, "broker": "ha", "port": 1883,
                 "user": "u", "password": "p", "tls": False,
                 "ha_mqtt_auto_discovery": True,
                 "ha_mqtt_auto_discovery_prefix": "homeassistant"},
        "refresh_time": 3,
        "time_zone": "Europe/Berlin",
        "eos_connect_web_port": 8081,
        "log_level": "info",
        "request_timeout": 10,
    }


class TestMergedConfigBuilder:
    """Tests for build_merged_config."""

    def test_merged_has_all_sections(self, store, schema):
        """Merged config should have all expected top-level keys."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)
        merged = build_merged_config(config, store, schema)

        for section in ("load", "eos", "price", "battery", "pv_forecast_source",
                        "inverter", "evcc", "mqtt"):
            assert section in merged, f"Missing section: {section}"

    def test_merged_has_top_level_keys(self, store, schema):
        """System-level keys should be present at top level."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)
        merged = build_merged_config(config, store, schema)

        assert "refresh_time" in merged
        assert "time_zone" in merged
        assert "eos_connect_web_port" in merged
        assert "log_level" in merged

    def test_bootstrap_keys_from_config_yaml(self, store, schema):
        """Bootstrap keys should come from config.yaml, not SQLite."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # These are not in the store (bootstrap only)
        merged = build_merged_config(config, store, schema)
        assert merged["time_zone"] == "Europe/Berlin"
        assert merged["eos_connect_web_port"] == 8081

    def test_values_from_store(self, store, schema):
        """Non-bootstrap values should come from the store."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Change a value in the store
        store.set("price.feed_in_price", 0.12)
        merged = build_merged_config(config, store, schema)
        assert merged["price"]["feed_in_price"] == 0.12

    def test_pv_forecast_is_list(self, store, schema):
        """pv_forecast should be a list in the merged config."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)
        merged = build_merged_config(config, store, schema)

        assert isinstance(merged["pv_forecast"], list)
        assert len(merged["pv_forecast"]) == 1
        assert merged["pv_forecast"][0]["name"] == "Roof"

    def test_pv_forecast_from_indexed_keys(self, store, schema):
        """Indexed pv_forecast keys should rebuild a list entry."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        store.set("pv_forecast.0.name", "Roof")
        store.set("pv_forecast.0.lat", 48.0)
        store.set("pv_forecast.0.lon", 9.0)
        store.set("pv_forecast.0.azimuth", 90)
        store.set("pv_forecast.0.tilt", 30)
        store.set("pv_forecast.0.power", 4600)
        store.set("pv_forecast.0.powerInverter", 5000)
        store.set("pv_forecast.0.inverterEfficiency", 0.9)
        store.set("pv_forecast.0.horizon", "10")

        merged = build_merged_config(config, store, schema)
        assert len(merged["pv_forecast"]) == 1
        assert merged["pv_forecast"][0]["name"] == "Roof"
        assert merged["pv_forecast"][0]["lat"] == 48.0

    def test_pv_forecast_from_unindexed_template_keys_synthesizes_entry(self, store, schema):
        """Unindexed pv_forecast template keys should synthesize one installation entry."""
        config = _sample_config()
        config["pv_forecast"] = []

        store.set("pv_forecast.name", "TemplateRoof")
        store.set("pv_forecast.lat", 48.0)
        store.set("pv_forecast.lon", 9.0)
        store.set("pv_forecast.azimuth", 90)
        store.set("pv_forecast.tilt", 30)
        store.set("pv_forecast.power", 4600)

        merged = build_merged_config(config, store, schema)
        assert len(merged["pv_forecast"]) == 1
        assert merged["pv_forecast"][0]["name"] == "TemplateRoof"
        assert merged["pv_forecast"][0]["powerInverter"] == 5000
        assert merged["pv_forecast"][0]["inverterEfficiency"] == 0.9

    def test_pv_forecast_indexed_takes_precedence_over_template_keys(self, store, schema):
        """Indexed pv_forecast keys should win over template keys when both exist."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        store.set("pv_forecast.name", "TemplateRoof")
        store.set("pv_forecast.0.name", "IndexedRoof")
        store.set("pv_forecast.0.lat", 48.0)
        store.set("pv_forecast.0.lon", 9.0)
        store.set("pv_forecast.0.azimuth", 90)
        store.set("pv_forecast.0.tilt", 30)
        store.set("pv_forecast.0.power", 4600)
        store.set("pv_forecast.0.powerInverter", 5000)
        store.set("pv_forecast.0.inverterEfficiency", 0.9)
        store.set("pv_forecast.0.horizon", "10")

        merged = build_merged_config(config, store, schema)
        assert len(merged["pv_forecast"]) == 1
        assert merged["pv_forecast"][0]["name"] == "IndexedRoof"

    def test_data_source_inherits_to_load(self, store, schema):
        """If load.source is 'default', data_source should populate it."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set load.source to default in store, data_source stays
        store.set("load.source", "default")
        merged = build_merged_config(config, store, schema)

        # load should inherit from data_source
        assert merged["load"]["source"] == "homeassistant"
        assert merged["load"]["url"] == "http://homeassistant:8123"

    def test_expert_override_preserved(self, store, schema):
        """If load has explicit non-default source, it should NOT be overridden."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # load.source already 'homeassistant' (non-default)
        merged = build_merged_config(config, store, schema)
        assert merged["load"]["source"] == "homeassistant"

    def test_data_source_not_in_output(self, store, schema):
        """data_source is internal — should NOT appear in merged output."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)
        merged = build_merged_config(config, store, schema)
        assert "data_source" not in merged

    def test_inverter_ha_gets_data_source_credentials(self, store, schema):
        """When inverter.type='homeassistant', inject data_source.url and token."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set inverter type to homeassistant and set data_source
        store.set("inverter.type", "homeassistant")
        store.set("data_source.type", "homeassistant")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "test_token_123")

        merged = build_merged_config(config, store, schema)

        # Verify inverter has injected url and token
        assert merged["inverter"]["url"] == "http://ha.local:8123"
        assert merged["inverter"]["token"] == "test_token_123"

    def test_inverter_non_ha_no_injection(self, store, schema):
        """When inverter.type!='homeassistant', don't inject credentials."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set inverter type to something else
        store.set("inverter.type", "fronius_gen24")
        store.set("data_source.type", "homeassistant")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "test_token_123")

        merged = build_merged_config(config, store, schema)

        # Inverter should NOT have injected url/token
        assert merged["inverter"].get("url") != "http://ha.local:8123"
        assert merged["inverter"].get("token") != "test_token_123"

    def test_inverter_ha_with_empty_data_source(self, store, schema):
        """When inverter.type='homeassistant' but data_source is empty, inject empty values."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        store.set("inverter.type", "homeassistant")
        store.set("data_source.type", "homeassistant")
        # Explicitly clear data_source.url and token by setting to empty strings
        store.set("data_source.url", "")
        store.set("data_source.access_token", "")

        merged = build_merged_config(config, store, schema)

        # Verify injected empty values
        assert not merged["inverter"]["url"]
        assert not merged["inverter"]["token"]

    def test_ssl_ignore_propagates_to_load(self, store, schema):
        """ssl_ignore from data_source should propagate to load section."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set ssl_ignore in data_source
        store.set("data_source.ssl_ignore", True)
        merged = build_merged_config(config, store, schema)

        # load section should have ssl_ignore=True
        assert merged["load"]["ssl_ignore"] is True

    def test_ssl_ignore_propagates_to_battery(self, store, schema):
        """ssl_ignore from data_source should propagate to battery section."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set ssl_ignore in data_source
        store.set("data_source.ssl_ignore", True)
        merged = build_merged_config(config, store, schema)

        # battery section should have ssl_ignore=True
        assert merged["battery"]["ssl_ignore"] is True

    def test_ssl_ignore_propagates_to_inverter_ha(self, store, schema):
        """ssl_ignore from data_source should propagate to inverter when type='homeassistant'."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set inverter type to homeassistant
        store.set("inverter.type", "homeassistant")
        store.set("data_source.type", "homeassistant")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "test_token")
        store.set("data_source.ssl_ignore", True)

        merged = build_merged_config(config, store, schema)

        # inverter should have ssl_ignore=True
        assert merged["inverter"]["ssl_ignore"] is True

    def test_ssl_ignore_false_by_default(self, store, schema):
        """ssl_ignore should default to False when not set."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Don't set ssl_ignore — should default to False
        merged = build_merged_config(config, store, schema)

        # All sections should have ssl_ignore=False
        assert merged["load"]["ssl_ignore"] is False
        assert merged["battery"]["ssl_ignore"] is False
        # inverter might not have ssl_ignore if type is not homeassistant
        if merged["inverter"].get("type") == "homeassistant":
            assert merged["inverter"]["ssl_ignore"] is False

    def test_central_ha_price_timeseries(self, store, schema):
        """When price.use_ha_central_data_source is true, construct URL from central HA."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Enable central HA for price
        store.set("price.source", "timeseries")
        store.set("price.use_ha_central_data_source", True)
        store.set("price.ha_sensor_name", "sensor.electricity_prices")
        store.set("data_source.url", "http://homeassistant.local:8123")
        store.set("data_source.access_token", "ha_token_123")

        merged = build_merged_config(config, store, schema)

        # Verify URL and token are constructed from central HA
        expected_url = (
            "http://homeassistant.local:8123/api/states/sensor.electricity_prices"
        )
        assert merged["price"]["data_url"] == expected_url
        assert merged["price"]["data_token"] == "ha_token_123"

    def test_central_ha_pv_timeseries(self, store, schema):
        """When pv_forecast_source.use_ha_central_data_source is true,
        construct URL from central HA."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Enable central HA for PV
        store.set("pv_forecast_source.source", "timeseries")
        store.set("pv_forecast_source.use_ha_central_data_source", True)
        store.set("pv_forecast_source.ha_sensor_name", "sensor.pv_forecast_data")
        store.set("data_source.url", "http://homeassistant.local:8123")
        store.set("data_source.access_token", "ha_token_456")

        merged = build_merged_config(config, store, schema)

        # Verify URL and token are constructed from central HA
        expected_pv_url = (
            "http://homeassistant.local:8123/api/states/sensor.pv_forecast_data"
        )
        assert merged["pv_forecast_source"]["data_url"] == expected_pv_url
        assert merged["pv_forecast_source"]["data_token"] == "ha_token_456"

    def test_manual_url_used_when_central_ha_disabled(self, store, schema):
        """When use_ha_central_data_source is false, use manual URL and token."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set manual URL and token
        store.set("price.source", "timeseries")
        store.set("price.use_ha_central_data_source", False)
        store.set("price.data_url", "https://custom-api.example.com/prices")
        store.set("price.data_token", "custom_token_xyz")
        store.set("data_source.url", "http://homeassistant.local:8123")
        store.set("data_source.access_token", "ha_token_should_not_be_used")

        merged = build_merged_config(config, store, schema)

        # Verify manual URL and token are used, not central HA ones
        assert merged["price"]["data_url"] == "https://custom-api.example.com/prices"
        assert merged["price"]["data_token"] == "custom_token_xyz"
