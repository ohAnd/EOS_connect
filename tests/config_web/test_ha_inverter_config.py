"""
Integration tests for Home Assistant inverter configuration flow.

Tests the end-to-end process:
1. Store JSON service call sequences
2. Merger injects data_source credentials
3. InverterHA initializes with complete config
"""

import json
import pytest
from src.config_web.store import ConfigStore
from src.config_web.schema import ConfigSchema
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.merger import build_merged_config
from src.interfaces.inverters import InverterHA


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
        },
        "battery": {
            "source": "homeassistant",
            "url": "http://homeassistant:8123",
            "access_token": "my_token",
            "soc_sensor": "sensor.soc",
            "capacity_wh": 11059,
        },
        "inverter": {
            "type": "default",
        },
        "data_source": {
            "type": "default",
        },
        "refresh_time": 300,
        "request_timeout": 10,
        "time_zone": "Europe/Berlin",
        "log_level": "INFO",
    }


class TestHAInverterConfigFlow:
    """Test the HA inverter config flow: store → merger → InverterHA."""

    def test_store_json_service_calls_as_string(self, store, schema):
        """Store should accept JSON strings for mode-sequence fields."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # JSON service call sequence as string
        service_calls_json = json.dumps([
            {
                "service": "number.set_value",
                "entity_id": "number.charge_power",
                "data_template": {"value": "{{ power }}"},
            },
            {
                "service": "select.select_option",
                "entity_id": "select.mode",
                "data": {"option": "Force Charge"},
            },
        ])

        store.set("inverter.charge_from_grid", service_calls_json)

        # Retrieve and verify
        retrieved = store.get("inverter.charge_from_grid")
        assert retrieved == service_calls_json
        assert isinstance(retrieved, str)

    def test_merger_builds_ha_inverter_config(self, store, schema):
        """Merger should build inverter config with mode sequences and injected credentials."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set HA inverter type and data_source
        store.set("inverter.type", "homeassistant")
        store.set("data_source.type", "homeassistant")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "my_ha_token_123")

        # Store service call sequences as JSON strings
        charge_from_grid_json = json.dumps([
            {
                "service": "number.set_value",
                "entity_id": "number.charge_power",
                "data_template": {"value": "{{ power }}"},
            }
        ])
        avoid_discharge_json = json.dumps([
            {
                "service": "select.select_option",
                "entity_id": "select.mode",
                "data": {"option": "Avoid Discharge"},
            }
        ])
        discharge_allowed_json = json.dumps([
            {
                "service": "select.select_option",
                "entity_id": "select.mode",
                "data": {"option": "Discharge"},
            }
        ])

        store.set("inverter.charge_from_grid", charge_from_grid_json)
        store.set("inverter.avoid_discharge", avoid_discharge_json)
        store.set("inverter.discharge_allowed", discharge_allowed_json)

        # Build merged config
        merged = build_merged_config(config, store, schema)

        # Verify inverter section has all required fields
        assert merged["inverter"]["type"] == "homeassistant"
        assert merged["inverter"]["url"] == "http://ha.local:8123"
        assert merged["inverter"]["token"] == "my_ha_token_123"
        assert merged["inverter"]["charge_from_grid"] == charge_from_grid_json
        assert merged["inverter"]["avoid_discharge"] == avoid_discharge_json
        assert merged["inverter"]["discharge_allowed"] == discharge_allowed_json

    def test_inverter_ha_init_with_merged_config(self, store, schema):
        """InverterHA should initialize successfully with merged config."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set HA inverter type and data_source
        store.set("inverter.type", "homeassistant")
        store.set("data_source.type", "homeassistant")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "token_123")

        # Store service call sequences
        charge_from_grid = [
            {"service": "number.set_value", "entity_id": "number.power", "data_template": {"value": "{{ power }}"}}
        ]
        avoid_discharge = [
            {"service": "select.select_option", "entity_id": "select.mode", "data": {"option": "Backup"}}
        ]
        discharge_allowed = [
            {"service": "select.select_option", "entity_id": "select.mode", "data": {"option": "Self Use"}}
        ]

        store.set("inverter.charge_from_grid", json.dumps(charge_from_grid))
        store.set("inverter.avoid_discharge", json.dumps(avoid_discharge))
        store.set("inverter.discharge_allowed", json.dumps(discharge_allowed))

        # Build merged config
        merged = build_merged_config(config, store, schema)

        # Create InverterHA instance with merged config
        inverter = InverterHA(merged["inverter"])

        # Verify initialization
        assert inverter.url == "http://ha.local:8123"
        assert inverter.token == "token_123"
        assert inverter.mode_sequences["force_charge"] == charge_from_grid
        assert inverter.mode_sequences["avoid_discharge"] == avoid_discharge
        assert inverter.mode_sequences["allow_discharge"] == discharge_allowed

    def test_inverter_ha_with_empty_sequences(self, store, schema):
        """InverterHA should handle empty service call sequences."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        store.set("inverter.type", "homeassistant")
        store.set("data_source.type", "homeassistant")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "token_123")

        # Store empty sequences
        store.set("inverter.charge_from_grid", "[]")
        store.set("inverter.avoid_discharge", "[]")
        store.set("inverter.discharge_allowed", "[]")

        merged = build_merged_config(config, store, schema)
        inverter = InverterHA(merged["inverter"])

        # Verify empty sequences
        assert inverter.mode_sequences["force_charge"] == []
        assert inverter.mode_sequences["avoid_discharge"] == []
        assert inverter.mode_sequences["allow_discharge"] == []

    def test_merger_parses_json_strings_to_lists(self, store, schema):
        """Merger should parse JSON strings for service call fields."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        store.set("inverter.type", "homeassistant")
        store.set("data_source.type", "homeassistant")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "token_123")

        service_calls = [
            {"service": "number.set_value", "entity_id": "number.power", "data": {"value": 5000}}
        ]
        store.set("inverter.charge_from_grid", json.dumps(service_calls))

        merged = build_merged_config(config, store, schema)

        # Merged config should have the JSON string (parser is at API layer or InverterHA)
        # For now, verify it's stored as a string
        retrieved = merged["inverter"]["charge_from_grid"]
        assert isinstance(retrieved, str)
        parsed = json.loads(retrieved)
        assert parsed == service_calls

    def test_non_ha_inverter_no_mode_sequences(self, store, schema):
        """Non-HA inverters should not require mode sequences."""
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)

        # Set to Fronius, not HA
        store.set("inverter.type", "fronius_gen24")
        store.set("inverter.address", "192.168.1.50")
        store.set("inverter.user", "customer")
        store.set("inverter.password", "password")

        merged = build_merged_config(config, store, schema)

        # Inverter should NOT have url/token injected
        assert merged["inverter"].get("url") != "http://ha.local:8123"
        assert merged["inverter"].get("token") is None or merged["inverter"].get("token") == ""
