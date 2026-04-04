"""
Unit tests for the config.yaml to SQLite migration.
"""

import pytest
from unittest.mock import patch, MagicMock
from src.config_web.store import ConfigStore
from src.config_web.schema import ConfigSchema
from src.config_web.migration import (
    migrate_yaml_to_store,
    _flatten_config,
    _has_user_configured_values,
    _coerce_migrated_value,
    _create_data_source,
    _create_data_source_batch,
)


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


def _default_config():
    """Return a config dict that mimics ConfigManager defaults (fresh install)."""
    return {
        "load": {
            "source": "default",
            "url": "http://homeassistant:8123",
            "access_token": "abc123",
            "load_sensor": "Load_Power",
        },
        "eos": {"source": "default", "server": "192.168.100.100", "port": 8503},
        "price": {"source": "default", "token": "tibberBearerToken"},
        "battery": {
            "source": "default",
            "url": "http://homeassistant:8123",
            "soc_sensor": "battery_SOC",
        },
    }


class TestHasUserConfiguredValues:
    """Tests for _has_user_configured_values()."""

    def test_all_defaults_returns_false(self):
        """Config with only default placeholder values → fresh install."""
        assert _has_user_configured_values(_default_config()) is False

    def test_load_source_homeassistant(self):
        """Real load source value detected as user-configured."""
        cfg = _default_config()
        cfg["load"]["source"] = "homeassistant"
        assert _has_user_configured_values(cfg) is True

    def test_battery_source_openhab(self):
        """Real battery source detected as user-configured."""
        cfg = _default_config()
        cfg["battery"]["source"] = "openhab"
        assert _has_user_configured_values(cfg) is True

    def test_price_source_tibber(self):
        """Real price source detected as user-configured."""
        cfg = _default_config()
        cfg["price"]["source"] = "tibber"
        assert _has_user_configured_values(cfg) is True

    def test_eos_source_eos_server(self):
        """Real EOS source detected as user-configured."""
        cfg = _default_config()
        cfg["eos"]["source"] = "eos_server"
        assert _has_user_configured_values(cfg) is True

    def test_eos_source_evopt(self):
        """EVopt source detected as user-configured."""
        cfg = _default_config()
        cfg["eos"]["source"] = "evopt"
        assert _has_user_configured_values(cfg) is True

    def test_real_load_sensor(self):
        """Non-placeholder load sensor detected as user-configured."""
        cfg = _default_config()
        cfg["load"]["load_sensor"] = "sensor.power_consumption"
        assert _has_user_configured_values(cfg) is True

    def test_real_soc_sensor(self):
        """Non-placeholder SOC sensor detected as user-configured."""
        cfg = _default_config()
        cfg["battery"]["soc_sensor"] = "sensor.battery_status"
        assert _has_user_configured_values(cfg) is True

    def test_empty_string_source_is_default(self):
        """Empty string sources are treated as unconfigured."""
        cfg = _default_config()
        cfg["load"]["source"] = ""
        cfg["eos"]["source"] = ""
        cfg["price"]["source"] = ""
        cfg["battery"]["source"] = ""
        assert _has_user_configured_values(cfg) is False

    def test_none_source_is_default(self):
        """None sources are treated as unconfigured."""
        cfg = _default_config()
        cfg["load"]["source"] = None
        cfg["eos"]["source"] = None
        cfg["price"]["source"] = None
        cfg["battery"]["source"] = None
        assert _has_user_configured_values(cfg) is False

    def test_empty_config_dict(self):
        """Completely empty config is a fresh install."""
        assert _has_user_configured_values({}) is False

    def test_missing_sections(self):
        """Missing sections treated as defaults."""
        assert _has_user_configured_values({"refresh_time": 3}) is False

    def test_placeholder_sensors_remain_default(self):
        """Placeholder sensor names (Load_Power, battery_SOC) are not real config."""
        cfg = _default_config()
        cfg["load"]["load_sensor"] = "Load_Power"
        cfg["battery"]["soc_sensor"] = "battery_SOC"
        assert _has_user_configured_values(cfg) is False


class TestCoerceMigratedValue:
    """Tests for _coerce_migrated_value()."""

    @pytest.fixture
    def schema(self):
        """Fresh schema instance."""
        return ConfigSchema()

    def test_bool_enabled_string(self, schema):
        """Legacy 'enabled' string coerced to True for bool fields."""
        assert _coerce_migrated_value(schema, "mqtt.enabled", "enabled") is True

    def test_bool_disabled_string(self, schema):
        """Legacy 'disabled' string coerced to False for bool fields."""
        assert _coerce_migrated_value(schema, "mqtt.enabled", "disabled") is False

    def test_bool_true_string(self, schema):
        """String 'true' coerced to True for bool fields."""
        assert _coerce_migrated_value(schema, "mqtt.enabled", "true") is True

    def test_bool_yes_string(self, schema):
        """String 'yes' coerced to True for bool fields."""
        assert _coerce_migrated_value(schema, "mqtt.enabled", "yes") is True

    def test_bool_false_string(self, schema):
        """String 'false' coerced to False for bool fields."""
        assert _coerce_migrated_value(schema, "mqtt.enabled", "false") is False

    def test_bool_native_passthrough(self, schema):
        """Native bool values pass through unchanged."""
        assert _coerce_migrated_value(schema, "mqtt.enabled", True) is True
        assert _coerce_migrated_value(schema, "mqtt.enabled", False) is False

    def test_int_from_string(self, schema):
        """String coerced to int for int fields."""
        result = _coerce_migrated_value(schema, "battery.capacity_wh", "15000")
        assert result == 15000
        assert isinstance(result, int)

    def test_int_native_passthrough(self, schema):
        """Native int values pass through unchanged."""
        assert _coerce_migrated_value(schema, "battery.capacity_wh", 15000) == 15000

    def test_int_invalid_string(self, schema):
        """Non-numeric string for int field returns original value."""
        assert _coerce_migrated_value(schema, "battery.capacity_wh", "abc") == "abc"

    def test_float_from_string(self, schema):
        """String coerced to float for float fields."""
        result = _coerce_migrated_value(schema, "price.feed_in_price", "0.08")
        assert result == 0.08
        assert isinstance(result, float)

    def test_float_native_passthrough(self, schema):
        """Native float values pass through unchanged."""
        assert _coerce_migrated_value(schema, "price.feed_in_price", 0.08) == 0.08

    def test_float_invalid_string(self, schema):
        """Non-numeric string for float field returns original value."""
        assert _coerce_migrated_value(schema, "price.feed_in_price", "abc") == "abc"

    def test_invalid_choice_falls_back_to_default(self, schema):
        """Value not in schema choices replaced with schema default."""
        result = _coerce_migrated_value(schema, "eos.source", "invalid_backend")
        eos_default = schema.get("eos.source").default
        assert result == eos_default

    def test_default_not_in_choices(self, schema):
        """ConfigManager's 'default' placeholder falls back to schema default."""
        result = _coerce_migrated_value(schema, "eos.source", "default")
        eos_default = schema.get("eos.source").default
        assert result == eos_default

    def test_valid_choice_passes_through(self, schema):
        """Valid choice value passes through unchanged."""
        assert _coerce_migrated_value(schema, "eos.source", "eos_server") == "eos_server"
        assert _coerce_migrated_value(schema, "eos.source", "evopt") == "evopt"

    def test_unknown_key_returns_value(self, schema):
        """Unknown schema key returns value unchanged."""
        assert _coerce_migrated_value(schema, "nonexistent.key", "whatever") == "whatever"

    def test_string_field_no_coercion(self, schema):
        """String fields are not coerced (except choices validation)."""
        assert _coerce_migrated_value(schema, "load.load_sensor", "sensor.power") == "sensor.power"


class TestCreateDataSource:
    """Tests for _create_data_source()."""

    @pytest.fixture
    def store(self, tmp_path):
        """Empty ConfigStore in a temp directory."""
        s = ConfigStore(str(tmp_path / "test.db"))
        s.open()
        yield s
        s.close()

    def test_from_load_homeassistant(self, store):
        """data_source created from load section when source is homeassistant."""
        config = {
            "load": {
                "source": "homeassistant",
                "url": "http://ha:8123",
                "access_token": "token123",
            },
            "battery": {"source": "default"},
        }
        _create_data_source(config, store)

        assert store.get("data_source.type") == "homeassistant"
        assert store.get("data_source.url") == "http://ha:8123"
        assert store.get("data_source.access_token") == "token123"

    def test_from_load_openhab(self, store):
        """data_source created from load section when source is openhab."""
        config = {
            "load": {
                "source": "openhab",
                "url": "http://oh:8080",
                "access_token": "",
            },
            "battery": {"source": "default"},
        }
        _create_data_source(config, store)

        assert store.get("data_source.type") == "openhab"
        assert store.get("data_source.url") == "http://oh:8080"

    def test_fallback_to_battery_when_load_default(self, store):
        """data_source falls back to battery when load source is 'default'."""
        config = {
            "load": {"source": "default", "url": "", "access_token": ""},
            "battery": {
                "source": "openhab",
                "url": "http://openhab:8080",
                "access_token": "bat_token",
            },
        }
        _create_data_source(config, store)

        assert store.get("data_source.type") == "openhab"
        assert store.get("data_source.url") == "http://openhab:8080"
        assert store.get("data_source.access_token") == "bat_token"

    def test_both_default_stays_default(self, store):
        """data_source stays 'default' when both load and battery are default."""
        config = {
            "load": {"source": "default", "url": "", "access_token": ""},
            "battery": {"source": "default", "url": "", "access_token": ""},
        }
        _create_data_source(config, store)

        assert store.get("data_source.type") == "default"

    def test_missing_sections(self, store):
        """data_source handles missing load/battery sections gracefully."""
        _create_data_source({}, store)

        assert store.get("data_source.type") == "default"
        assert store.get("data_source.url") == ""
        assert store.get("data_source.access_token") == ""

    def test_load_url_preserved_when_battery_fallback(self, store):
        """Load URL used as fallback when battery provides source but no URL."""
        config = {
            "load": {
                "source": "default",
                "url": "http://ha:8123",
                "access_token": "load_token",
            },
            "battery": {"source": "homeassistant"},
        }
        _create_data_source(config, store)

        assert store.get("data_source.type") == "homeassistant"
        # Battery section has no url, so load's url is preserved as fallback
        assert store.get("data_source.url") == "http://ha:8123"


class TestMigrationWizardFlag:
    """Tests for wizard flag behavior during migration."""

    @pytest.fixture
    def schema(self):
        """Fresh schema instance."""
        return ConfigSchema()

    @pytest.fixture
    def store(self, tmp_path):
        """Empty ConfigStore in a temp directory."""
        s = ConfigStore(str(tmp_path / "test.db"))
        s.open()
        yield s
        s.close()

    def test_fresh_install_wizard_not_completed(self, store, schema):
        """Fresh install (all defaults) should NOT mark wizard completed."""
        config = _default_config()
        migrate_yaml_to_store(config, store, schema)

        assert store.get("_wizard_completed") is None
        assert store.get("_migrated_from_yaml") is None

    def test_real_config_wizard_completed(self, store, schema):
        """Real user config migration marks wizard completed."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        assert store.get("_wizard_completed") is True
        assert store.get("_migrated_from_yaml") is True

    def test_fresh_install_still_stores_values(self, store, schema):
        """Fresh install still migrates values to store (just no wizard flag)."""
        config = _default_config()
        migrate_yaml_to_store(config, store, schema)

        assert not store.is_empty()
        # Values are stored even though wizard is not marked complete
        assert store.get("eos.port") is not None


class TestStoreBatch:
    """Tests for the atomic set_batch method."""

    @pytest.fixture
    def store(self, tmp_path):
        """Empty ConfigStore in a temp directory."""
        s = ConfigStore(str(tmp_path / "test.db"))
        s.open()
        yield s
        s.close()

    def test_set_batch_writes_all_keys(self, store):
        """set_batch should write all keys atomically."""
        batch = {"a": 1, "b": "hello", "c": True, "d": [1, 2, 3]}
        count = store.set_batch(batch)

        assert count == 4
        assert store.get("a") == 1
        assert store.get("b") == "hello"
        assert store.get("c") is True
        assert store.get("d") == [1, 2, 3]

    def test_set_batch_empty_dict(self, store):
        """set_batch with empty dict should write nothing."""
        count = store.set_batch({})
        assert count == 0
        assert store.is_empty()

    def test_set_batch_does_not_fire_callbacks(self, store):
        """set_batch should not fire change callbacks (migration use case)."""
        callback = MagicMock()
        store.register_change_callback(callback)
        store.set_batch({"key1": "val1", "key2": "val2"})
        callback.assert_not_called()


class TestCreateDataSourceBatch:
    """Tests for _create_data_source_batch returning dict instead of writing."""

    def test_returns_dict_from_load(self):
        """Should return data_source keys from load section."""
        config = {
            "load": {
                "source": "homeassistant",
                "url": "http://ha:8123",
                "access_token": "tok123",
            }
        }
        result = _create_data_source_batch(config)

        assert result == {
            "data_source.type": "homeassistant",
            "data_source.url": "http://ha:8123",
            "data_source.access_token": "tok123",
        }

    def test_fallback_to_battery(self):
        """Should fall back to battery when load source is default."""
        config = {
            "load": {"source": "default", "url": "", "access_token": ""},
            "battery": {
                "source": "openhab",
                "url": "http://oh:8080",
                "access_token": "bat_tok",
            },
        }
        result = _create_data_source_batch(config)

        assert result["data_source.type"] == "openhab"
        assert result["data_source.url"] == "http://oh:8080"
        assert result["data_source.access_token"] == "bat_tok"


class TestAtomicMigration:
    """Tests verifying that migration uses atomic batch writes."""

    @pytest.fixture
    def schema(self):
        return ConfigSchema()

    @pytest.fixture
    def store(self, tmp_path):
        s = ConfigStore(str(tmp_path / "test.db"))
        s.open()
        yield s
        s.close()

    def test_migration_uses_set_batch(self, store, schema):
        """Migration should call set_batch, not individual set() calls."""
        config = _sample_config()
        with patch.object(store, "set_batch", wraps=store.set_batch) as mock_batch:
            migrate_yaml_to_store(config, store, schema)
            mock_batch.assert_called_once()

    def test_failed_batch_leaves_store_empty(self, store, schema):
        """If set_batch fails, store should remain empty (no partial data)."""
        config = _sample_config()
        with patch.object(store, "set_batch", side_effect=Exception("disk full")):
            result = migrate_yaml_to_store(config, store, schema)

        assert result is False
        assert store.is_empty()

    def test_migration_retryable_after_failure(self, store, schema):
        """After a failed migration, next restart should retry successfully."""
        config = _sample_config()

        # First attempt fails
        with patch.object(store, "set_batch", side_effect=Exception("disk full")):
            migrate_yaml_to_store(config, store, schema)

        assert store.is_empty()

        # Second attempt succeeds
        result = migrate_yaml_to_store(config, store, schema)
        assert result is True
        assert not store.is_empty()
        assert store.get("_wizard_completed") is True
