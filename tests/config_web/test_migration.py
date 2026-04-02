"""
Unit tests for the config.yaml to SQLite migration.
"""

import pytest
from src.config_web.store import ConfigStore
from src.config_web.schema import ConfigSchema
from src.config_web.migration import migrate_yaml_to_store, _flatten_config


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
    """Return a minimal config dict matching the real config.yaml structure."""
    return {
        "load": {
            "source": "homeassistant",
            "url": "http://homeassistant:8123",
            "access_token": "my_token_123",
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
            "token": "my_tibber_token",
            "fixed_price_adder_ct": 0,
            "relative_price_multiplier": 0,
            "feed_in_price": 0.08,
            "negative_price_switch": False,
            "energyforecast_enabled": False,
            "energyforecast_token": "demo_token",
            "energyforecast_market_zone": "DE-LU",
        },
        "battery": {
            "source": "homeassistant",
            "url": "http://homeassistant:8123",
            "access_token": "my_token_123",
            "soc_sensor": "sensor.battery_soc",
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
        "pv_forecast_source": {
            "source": "akkudoktor",
            "api_key": "",
        },
        "pv_forecast": [
            {
                "name": "Roof",
                "lat": 48.0,
                "lon": 9.0,
                "azimuth": 90,
                "tilt": 30,
                "power": 4600,
                "powerInverter": 5000,
                "inverterEfficiency": 0.9,
                "horizon": "10,20",
                "resource_id": "",
            }
        ],
        "inverter": {
            "type": "default",
            "address": "192.168.1.12",
            "user": "customer",
            "password": "abc123",
            "max_grid_charge_rate": 5000,
            "max_pv_charge_rate": 5000,
        },
        "evcc": {"url": "http://yourEVCCserver:7070"},
        "mqtt": {
            "enabled": False,
            "broker": "homeassistant",
            "port": 1883,
            "user": "user",
            "password": "pass",
            "tls": False,
            "ha_mqtt_auto_discovery": True,
            "ha_mqtt_auto_discovery_prefix": "homeassistant",
        },
        "refresh_time": 3,
        "time_zone": "Europe/Berlin",
        "eos_connect_web_port": 8081,
        "log_level": "info",
        "request_timeout": 10,
    }


class TestMigration:
    """Tests for config.yaml to SQLite migration."""

    def test_migration_runs_on_empty_store(self, store, schema):
        """Migration should proceed when store is empty."""
        config = _sample_config()
        result = migrate_yaml_to_store(config, store, schema)
        assert result is True
        assert not store.is_empty()

    def test_migration_skipped_if_store_has_data(self, store, schema):
        """Migration should not run if store already has data."""
        store.set("some_key", "some_value")
        config = _sample_config()
        result = migrate_yaml_to_store(config, store, schema)
        assert result is False

    def test_data_source_created(self, store, schema):
        """Migration should create data_source from load section."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        assert store.get("data_source.type") == "homeassistant"
        assert store.get("data_source.url") == "http://homeassistant:8123"
        assert store.get("data_source.access_token") == "my_token_123"

    def test_bootstrap_keys_not_migrated(self, store, schema):
        """Bootstrap keys (web_port, timezone, etc.) should NOT be in the store."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        assert store.get("eos_connect_web_port") is None
        assert store.get("time_zone") is None
        assert store.get("log_level") is None

    def test_sections_migrated(self, store, schema):
        """Load, EOS, price etc. fields should be migrated."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        assert store.get("load.load_sensor") == "sensor.power"
        assert store.get("eos.server") == "192.168.1.100"
        assert store.get("price.source") == "tibber"
        assert store.get("battery.capacity_wh") == 10000

    def test_pv_forecast_list_migrated(self, store, schema):
        """The pv_forecast list should be stored as a single JSON list."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        pv = store.get("pv_forecast")
        assert isinstance(pv, list)
        assert len(pv) == 1
        assert pv[0]["name"] == "Roof"

    def test_wizard_marked_completed(self, store, schema):
        """Existing config migration should mark wizard as completed."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        assert store.get("_wizard_completed") is True
        assert store.get("_migrated_from_yaml") is True

    def test_data_source_from_battery_if_load_default(self, store, schema):
        """If load source is 'default', data_source should come from battery."""
        config = _sample_config()
        config["load"]["source"] = "default"
        config["battery"]["source"] = "openhab"
        config["battery"]["url"] = "http://openhab:8080"
        config["battery"]["access_token"] = "oh_token"

        migrate_yaml_to_store(config, store, schema)

        assert store.get("data_source.type") == "openhab"
        assert store.get("data_source.url") == "http://openhab:8080"


class TestFlattenConfig:
    """Tests for the _flatten_config() helper."""

    def test_flat_keys_unchanged(self):
        """Top-level scalar keys should pass through as-is."""
        result = _flatten_config({"refresh_time": 3, "time_zone": "UTC"})
        assert result == {"refresh_time": 3, "time_zone": "UTC"}

    def test_nested_dict_flattened(self):
        """Nested dicts should be flattened to dot-notation."""
        result = _flatten_config({"load": {"source": "ha", "url": "http://x"}})
        assert result == {"load.source": "ha", "load.url": "http://x"}

    def test_list_stored_as_is(self):
        """Lists should be stored as a single value, not flattened."""
        result = _flatten_config({"pv_forecast": [{"name": "A"}, {"name": "B"}]})
        assert result == {"pv_forecast": [{"name": "A"}, {"name": "B"}]}

    def test_none_values_preserved(self):
        """None values should be included in flatten output."""
        result = _flatten_config({"load": {"sensor": None}})
        assert "load.sensor" in result
        assert result["load.sensor"] is None

    def test_empty_dict_flattened(self):
        """Empty nested dict should produce no keys."""
        result = _flatten_config({"load": {}})
        assert result == {}

    def test_empty_string_preserved(self):
        """Empty strings should be preserved."""
        result = _flatten_config({"load": {"sensor": ""}})
        assert result["load.sensor"] == ""

    def test_bool_values_preserved(self):
        """Boolean values should keep their type."""
        result = _flatten_config({"mqtt": {"enabled": False, "tls": True}})
        assert result["mqtt.enabled"] is False
        assert result["mqtt.tls"] is True

    def test_mixed_types(self):
        """Mix of scalars, nested dicts, lists, and booleans."""
        config = {
            "refresh_time": 5,
            "load": {"source": "ha"},
            "pv_forecast": [{"name": "Roof"}],
            "mqtt": {"enabled": True},
        }
        result = _flatten_config(config)
        assert result["refresh_time"] == 5
        assert result["load.source"] == "ha"
        assert isinstance(result["pv_forecast"], list)
        assert result["mqtt.enabled"] is True
