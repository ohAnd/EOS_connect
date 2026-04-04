"""
Unit tests for the merged config builder.
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
            "price_euro_per_wh_accu": 0.0,
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
