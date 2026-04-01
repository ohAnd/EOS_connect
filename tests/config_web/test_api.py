"""
Unit tests for the REST API Blueprint.
"""

import json
import pytest
from flask import Flask
from src.config_web.store import ConfigStore
from src.config_web.schema import ConfigSchema
from src.config_web.api import config_bp, init_api
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.merger import build_merged_config


def _sample_config():
    """Minimal config dict."""
    return {
        "load": {
            "source": "homeassistant", "url": "http://ha:8123",
            "access_token": "tok", "load_sensor": "sensor.power",
            "car_charge_load_sensor": "", "additional_load_1_sensor": "",
            "additional_load_1_runtime": 0, "additional_load_1_consumption": 0,
        },
        "eos": {
            "source": "eos_server", "server": "192.168.1.1", "port": 8503,
            "timeout": 180, "time_frame": 3600,
            "dyn_override_discharge_allowed_pv_greater_load": False,
            "pv_battery_charge_control_enabled": False,
        },
        "price": {
            "source": "default", "token": "tok", "fixed_price_adder_ct": 0,
            "relative_price_multiplier": 0, "feed_in_price": 0.0,
            "negative_price_switch": False, "energyforecast_enabled": False,
            "energyforecast_token": "demo", "energyforecast_market_zone": "DE-LU",
        },
        "battery": {
            "source": "homeassistant", "url": "http://ha:8123", "access_token": "tok",
            "soc_sensor": "sensor.soc", "capacity_wh": 10000,
            "charge_efficiency": 0.88, "discharge_efficiency": 0.88,
            "max_charge_power_w": 5000, "min_soc_percentage": 5,
            "max_soc_percentage": 100, "charging_curve_enabled": True,
            "sensor_battery_temperature": "", "price_euro_per_wh_accu": 0.0,
            "price_euro_per_wh_sensor": "", "price_calculation_enabled": False,
            "price_update_interval": 900, "price_history_lookback_hours": 96,
            "battery_power_sensor": "", "pv_power_sensor": "", "grid_power_sensor": "",
            "load_power_sensor": "", "price_sensor": "",
            "charging_threshold_w": 50.0, "grid_charge_threshold_w": 100.0,
            "battery_price_include_feedin": False,
        },
        "pv_forecast_source": {"source": "akkudoktor", "api_key": ""},
        "pv_forecast": [{"name": "R", "lat": 48.0, "lon": 9.0, "azimuth": 90,
                         "tilt": 30, "power": 4600, "powerInverter": 5000,
                         "inverterEfficiency": 0.9, "horizon": "10", "resource_id": ""}],
        "inverter": {"type": "default", "address": "192.168.1.12",
                     "user": "c", "password": "p",
                     "max_grid_charge_rate": 5000, "max_pv_charge_rate": 5000},
        "evcc": {"url": "http://yourEVCCserver:7070"},
        "mqtt": {"enabled": False, "broker": "ha", "port": 1883,
                 "user": "u", "password": "p", "tls": False,
                 "ha_mqtt_auto_discovery": True,
                 "ha_mqtt_auto_discovery_prefix": "homeassistant"},
        "refresh_time": 3, "time_zone": "Europe/Berlin",
        "eos_connect_web_port": 8081, "log_level": "info", "request_timeout": 10,
    }


class _FakeModule:
    """Minimal stand-in for ConfigWebModule to satisfy API references."""

    def __init__(self, config, store, schema):
        self._config = config
        self._store = store
        self._schema = schema

    def get_config(self):
        """Return current merged config."""
        return build_merged_config(self._config, self._store, self._schema)

    def rebuild_config(self):
        """No-op for tests."""


@pytest.fixture
def client(tmp_path):
    """Create a Flask test client with the config blueprint."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "test.db"))
    store.open()

    config = _sample_config()
    migrate_yaml_to_store(config, store, schema)

    module = _FakeModule(config, store, schema)
    init_api(store, schema, module)
    app.register_blueprint(config_bp)

    with app.test_client() as c:
        yield c

    store.close()


class TestConfigAPI:
    """Tests for the config REST API."""

    def test_get_schema(self, client):
        """GET /api/config/schema should return schema JSON."""
        resp = client.get("/api/config/schema")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) > 50

    def test_get_config(self, client):
        """GET /api/config/ should return current config."""
        resp = client.get("/api/config/")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "load" in data
        assert "battery" in data

    def test_get_section(self, client):
        """GET /api/config/section/eos should return EOS config."""
        resp = client.get("/api/config/section/eos")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "server" in data

    def test_get_section_unknown(self, client):
        """GET /api/config/section/nonexistent should return 404."""
        resp = client.get("/api/config/section/nonexistent")
        assert resp.status_code == 404

    def test_update_config(self, client):
        """PUT /api/config/ should update values."""
        resp = client.put(
            "/api/config/",
            data=json.dumps({"price.feed_in_price": 0.12}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "price.feed_in_price" in data["updated"]

    def test_update_invalid_key(self, client):
        """PUT /api/config/ with unknown key should return 422."""
        resp = client.put(
            "/api/config/",
            data=json.dumps({"fake.key": "nope"}),
            content_type="application/json",
        )
        assert resp.status_code == 422

    def test_validate_valid(self, client):
        """POST /api/config/validate with valid data should return valid."""
        resp = client.post(
            "/api/config/validate",
            data=json.dumps({"battery.capacity_wh": 15000}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is True

    def test_validate_invalid_range(self, client):
        """POST /api/config/validate with out-of-range value should fail."""
        resp = client.post(
            "/api/config/validate",
            data=json.dumps({"battery.capacity_wh": -5}),
            content_type="application/json",
        )
        assert resp.status_code == 422
        data = resp.get_json()
        assert data["valid"] is False

    def test_export(self, client):
        """GET /api/config/export should return all store data."""
        resp = client.get("/api/config/export")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert len(data) > 0

    def test_wizard_status(self, client):
        """GET /api/config/wizard-status should report wizard as completed after migration."""
        resp = client.get("/api/config/wizard-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["completed"] is True
        assert data["pending"] is False
