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
        self.notified = []

    def get_config(self):
        """Return current merged config."""
        return build_merged_config(self._config, self._store, self._schema)

    def rebuild_config(self):
        """No-op for tests."""

    def notify_config_changed(self, key, old_value, new_value):
        """Record hot-reload notifications so tests can assert they were fired."""
        self.notified.append((key, old_value, new_value))


@pytest.fixture(name="client")
def client_fixture(tmp_path):
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
        # Handed to tests that need to inspect what the API did to the store or
        # which hot-reload notifications it fired.
        c.store = store
        c.module = module
        yield c

    store.close()


class TestConfigAPI:
    """Tests for the config REST API."""

    def test_get_schema(self, client):
        """GET /api/config/schema should return schema JSON with fields and sections."""
        resp = client.get("/api/config/schema")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert "fields" in data
        assert "sections" in data
        assert len(data["fields"]) > 50
        assert isinstance(data["sections"], dict)
        assert "data_source" in data["sections"]

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
        """PUT /api/config/ should update values and report hot_reloaded."""
        resp = client.put(
            "/api/config/",
            data=json.dumps({"price.feed_in_price": 0.12}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "price.feed_in_price" in data["updated"]
        # feed_in_price has hot_reload=True and no restart_required label
        assert "price.feed_in_price" in data["hot_reloaded"]
        assert "price.feed_in_price" not in data.get("restart_required", [])

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

    def test_import_config(self, client):
        """POST /api/config/import should import flat key/value pairs."""
        payload = {"battery.capacity_wh": 20000, "eos.port": 9999}
        resp = client.post(
            "/api/config/import",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported"] == 2

        # Verify values were actually stored
        resp2 = client.get("/api/config/export")
        exported = resp2.get_json()
        assert exported["battery.capacity_wh"] == 20000
        assert exported["eos.port"] == 9999

    def test_import_invalid_body(self, client):
        """POST /api/config/import with non-dict body should return 400."""
        resp = client.post(
            "/api/config/import",
            data=json.dumps([1, 2, 3]),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_password_masking_in_get_config(self, client):
        """GET /api/config/ should mask password fields."""
        # Set a password value first
        client.put(
            "/api/config/",
            data=json.dumps({"price.token": "my_secret_token"}),
            content_type="application/json",
        )
        resp = client.get("/api/config/")
        data = resp.get_json()
        assert data["price"]["token"] == "********"

    def test_password_masking_in_section(self, client):
        """GET /api/config/section/inverter should mask password fields."""
        client.put(
            "/api/config/",
            data=json.dumps({"inverter.password": "secret123"}),
            content_type="application/json",
        )
        resp = client.get("/api/config/section/inverter")
        data = resp.get_json()
        assert data["password"] == "********"

    def test_empty_password_not_masked(self, client):
        """Empty password should show as empty, not asterisks."""
        client.put(
            "/api/config/",
            data=json.dumps({"inverter.password": ""}),
            content_type="application/json",
        )
        resp = client.get("/api/config/section/inverter")
        data = resp.get_json()
        assert data["password"] == ""

    def test_restart_required_endpoint(self, client):
        """GET /api/config/restart-required should return fields list."""
        resp = client.get("/api/config/restart-required")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "fields" in data
        assert isinstance(data["fields"], list)

    def test_update_restart_required_field(self, client):
        """PUT with restart_required field should report it in response."""
        resp = client.put(
            "/api/config/",
            data=json.dumps({"mqtt.broker": "new-broker"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "mqtt.broker" in data["restart_required"]
        assert "mqtt.broker" not in data["hot_reloaded"]

    def test_update_multiple_values(self, client):
        """PUT with multiple values should update all."""
        resp = client.put(
            "/api/config/",
            data=json.dumps({
                "battery.capacity_wh": 15000,
                "eos.port": 7050,
                "price.feed_in_price": 0.10,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["updated"]) == 3

    def test_wizard_status(self, client):
        """GET /api/config/wizard-status should report wizard as completed after migration."""
        resp = client.get("/api/config/wizard-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["completed"] is True
        assert data["pending"] is False

    def test_timeseries_sensor_name_change_triggers_preflight(self, client, monkeypatch):
        """
        Test that changing sensor name on existing timeseries config triggers pre-flight validation.
        
        This validates the fix for the issue where changing sensor_name on an already-configured
        timeseries didn't trigger the pre-flight test, allowing invalid sensor names to be saved.
        """
        import requests

        # Mock successful response for valid sensor
        def mock_get_valid(*args, **kwargs):
            class MockResponse:
                status_code = 200
                def json(self):
                    return {
                        'state': 'available',
                        'attributes': {
                            'data': [
                                {'start': '2024-01-01T00:00:00', 'end': '2024-01-01T01:00:00', 'value': 0.25},
                                {'start': '2024-01-01T01:00:00', 'end': '2024-01-01T02:00:00', 'value': 0.30},
                            ]
                        }
                    }
                def raise_for_status(self):
                    pass
            return MockResponse()

        # Mock response for invalid sensor (404)
        def mock_get_invalid(*args, **kwargs):
            exc = requests.exceptions.HTTPError()
            exc.response = type('obj', (object,), {
                'status_code': 404,
                'reason': 'Not Found'
            })()
            raise exc

        # Step 1: Set up working timeseries config with valid sensor
        monkeypatch.setattr('requests.get', mock_get_valid)
        resp1 = client.put(
            "/api/config/",
            data=json.dumps({
                "price.source": "timeseries",
                "price.use_ha_central_data_source": True,
                "price.ha_sensor_name": "sensor.valid_prices",
                "data_source.url": "http://ha:8123",
                "data_source.access_token": "test_token",
            }),
            content_type="application/json",
        )
        assert resp1.status_code == 200, f"Setup failed: {resp1.get_json()}"

        # Step 2: Try to change sensor name to invalid one (should trigger pre-flight and fail)
        monkeypatch.setattr('requests.get', mock_get_invalid)
        resp2 = client.put(
            "/api/config/",
            data=json.dumps({
                "price.ha_sensor_name": "sensor.invalid_prices",
            }),
            content_type="application/json",
        )
        
        # Should get 422 error because pre-flight test failed
        assert resp2.status_code == 422, f"Expected 422, got {resp2.status_code}: {resp2.get_json()}"
        data = resp2.get_json()
        assert "errors" in data
        assert any("invalid_prices" in str(e.get("error", "")) for e in data["errors"]), \
            f"Error message should mention the invalid sensor name. Got: {data['errors']}"

        # Step 3: Verify the invalid config was NOT saved by changing back to valid sensor
        monkeypatch.setattr('requests.get', mock_get_valid)
        resp3 = client.put(
            "/api/config/",
            data=json.dumps({
                "price.ha_sensor_name": "sensor.valid_prices",
            }),
            content_type="application/json",
        )
        assert resp3.status_code == 200, f"Reverting to valid sensor failed: {resp3.get_json()}"
        
        # Get current config to verify valid sensor name is still set
        resp4 = client.get("/api/config/")
        current = resp4.get_json()
        assert current["price"]["ha_sensor_name"] == "sensor.valid_prices", \
            "Config should revert to valid sensor name"


def test_price_fixed_24h_array_validation_count(client):
    """Test that fixed_24h_array must contain exactly 24 values."""
    # Test: Too few values (12) should fail
    resp = client.put(
        "/api/config/",
        data=json.dumps({
            "price.source": "fixed_24h",
            "price.fixed_24h_array": "10,11,12,13,14,15,16,17,18,19,20,21",  # 12 values
        }),
        content_type="application/json",
    )
    assert resp.status_code == 422, "Should reject array with fewer than 24 values"
    errors = resp.get_json()["errors"]
    assert any("24" in e.get("error", "") for e in errors), \
        "Error should mention exactly 24 values"


def test_price_fixed_24h_array_validation_too_many(client):
    """Test that fixed_24h_array rejects more than 24 values."""
    # Test: Too many values (30) should fail
    resp = client.put(
        "/api/config/",
        data=json.dumps({
            "price.source": "fixed_24h",
            "price.fixed_24h_array": ",".join(str(10 + i * 0.5) for i in range(30)),  # 30 values
        }),
        content_type="application/json",
    )
    assert resp.status_code == 422, "Should reject array with more than 24 values"
    errors = resp.get_json()["errors"]
    assert any("24" in e.get("error", "") for e in errors), \
        "Error should mention exactly 24 values"


def test_price_fixed_24h_array_validation_exact(client):
    """Test that fixed_24h_array accepts exactly 24 values."""
    # Exactly 24 values should succeed
    valid_array = ",".join(str(10 + i * 0.5) for i in range(24))
    resp = client.put(
        "/api/config/",
        data=json.dumps({
            "price.source": "fixed_24h",
            "price.fixed_24h_array": valid_array,
        }),
        content_type="application/json",
    )
    assert resp.status_code == 200, f"Should accept 24 values: {resp.get_json()}"
    assert resp.get_json().get("success") is True, "Save should succeed"


def test_price_fixed_24h_array_non_numeric(client):
    """Test that fixed_24h_array must contain only numeric values."""
    # Non-numeric value should fail
    resp = client.put(
        "/api/config/",
        data=json.dumps({
            "price.source": "fixed_24h",
            "price.fixed_24h_array": "10,11,abc,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33",
        }),
        content_type="application/json",
    )
    assert resp.status_code == 422, "Should reject non-numeric values"
    errors = resp.get_json()["errors"]
    assert any("numeric" in e.get("error", "").lower() for e in errors), \
        "Error should mention numeric values"


def test_price_fixed_24h_array_required_when_source_fixed(client):
    """Test that fixed_24h_array is required when source is fixed_24h."""
    # Empty array should fail when source is fixed_24h
    resp = client.put(
        "/api/config/",
        data=json.dumps({
            "price.source": "fixed_24h",
            "price.fixed_24h_array": "",
        }),
        content_type="application/json",
    )
    assert resp.status_code == 422, "Should reject empty array when source is fixed_24h"
    errors = resp.get_json()["errors"]
    assert any("required" in e.get("error", "").lower() for e in errors), \
        "Error should indicate field is required"


def test_price_fixed_24h_array_not_required_other_source(client):
    """Test that fixed_24h_array is not validated when source is not fixed_24h."""
    # Should allow any value when source is not fixed_24h
    resp = client.put(
        "/api/config/",
        data=json.dumps({
            "price.source": "default",
            "price.fixed_24h_array": "10,20,30",  # Only 3 values, but not used
        }),
        content_type="application/json",
    )
    assert resp.status_code == 200, "Should accept array when source is not fixed_24h"


def test_price_fixed_24h_array_whitespace_handling(client):
    """Test that fixed_24h_array handles whitespace correctly."""
    # Whitespace in values should be trimmed
    resp = client.put(
        "/api/config/",
        data=json.dumps({
            "price.source": "fixed_24h",
            "price.fixed_24h_array": "10.5 , 11.2 , 12.3 , 13.1 , 14.2 , 15.3 , 16.1 , 17.2 , 18.3 , 19.1 , 20.2 , 21.3 , 22.1 , 23.2 , 24.3 , 25.1 , 26.2 , 27.3 , 28.1 , 29.2 , 30.3 , 31.1 , 32.2 , 33.3",  # 24 values with spaces
        }),
        content_type="application/json",
    )
    assert resp.status_code == 200, "Should accept 24 values with whitespace"


def test_price_fixed_24h_array_zero_and_negative(client):
    """Test that fixed_24h_array accepts zero and negative values."""
    # Zero and negative values should be valid (they affect grid discharge prices)
    valid_array = ",".join(["-5.2" if i % 3 == 0 else "0" if i % 3 == 1 else str(10 + i * 0.5) for i in range(24)])
    resp = client.put(
        "/api/config/",
        data=json.dumps({
            "price.source": "fixed_24h",
            "price.fixed_24h_array": valid_array,
        }),
        content_type="application/json",
    )
    assert resp.status_code == 200, "Should accept zero and negative numeric values"


@pytest.fixture(name="fresh_client")
def fresh_client_fixture(tmp_path):
    """Create a Flask test client with NO migration (fresh install)."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "fresh.db"))
    store.open()

    config = _sample_config()
    config["pv_forecast"] = []
    module = _FakeModule(config, store, schema)
    init_api(store, schema, module)
    app.register_blueprint(config_bp)

    with app.test_client() as c:
        yield c

    store.close()


class TestWizardAPI:
    """Tests for the setup wizard endpoints."""

    def test_wizard_pending_on_fresh_install(self, fresh_client):
        """Fresh install (no migration, no wizard) should be pending."""
        resp = fresh_client.get("/api/config/wizard-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pending"] is True
        assert data["completed"] is False
        assert data["migrated"] is False

    def test_wizard_not_pending_after_migration(self, client):
        """Migrated install should not be pending."""
        resp = client.get("/api/config/wizard-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pending"] is False
        assert data["migrated"] is True

    def test_save_location_based_pv_installation_succeeds(self, fresh_client):
        """Saving a location-based PV source with an indexed pv_forecast entry should succeed."""
        payload = {
            "pv_forecast_source.source": "openmeteo",
            "pv_forecast.0.name": "Roof",
            "pv_forecast.0.lat": 48.0,
            "pv_forecast.0.lon": 9.0,
            "pv_forecast.0.azimuth": 90,
            "pv_forecast.0.tilt": 30,
            "pv_forecast.0.power": 4600,
            "pv_forecast.0.powerInverter": 5000,
            "pv_forecast.0.inverterEfficiency": 0.9,
            "pv_forecast.0.horizon": "10",
        }

        resp = fresh_client.put(
            "/api/config/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "pv_forecast.0.name" in data["updated"]

        resp2 = fresh_client.get("/api/config/")
        merged = resp2.get_json()
        assert isinstance(merged["pv_forecast"], list)
        assert len(merged["pv_forecast"]) == 1
        assert merged["pv_forecast"][0]["name"] == "Roof"

    def test_save_location_based_source_without_installations_blocks(self, fresh_client):
        """Saving a location-based source without PV installations should return unmet dependencies."""
        resp = fresh_client.put(
            "/api/config/",
            data=json.dumps({"pv_forecast_source.source": "openmeteo"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is False
        assert data["unmet_dependencies"]
        assert any(dep["field"] == "pv_forecast" for dep in data["unmet_dependencies"])

    def test_save_pv_installation_after_location_source_succeeds(self, fresh_client):
        """After saving a location-based source, adding a pv_forecast entry later should succeed."""
        resp1 = fresh_client.put(
            "/api/config/",
            data=json.dumps({"pv_forecast_source.source": "openmeteo"}),
            content_type="application/json",
        )
        assert resp1.status_code == 200
        assert resp1.get_json()["success"] is False

        resp2 = fresh_client.put(
            "/api/config/",
            data=json.dumps({
                "pv_forecast.0.name": "Roof",
                "pv_forecast.0.lat": 48.0,
                "pv_forecast.0.lon": 9.0,
                "pv_forecast.0.azimuth": 90,
                "pv_forecast.0.tilt": 30,
                "pv_forecast.0.power": 4600,
                "pv_forecast.0.powerInverter": 5000,
                "pv_forecast.0.inverterEfficiency": 0.9,
                "pv_forecast.0.horizon": "10",
            }),
            content_type="application/json",
        )
        assert resp2.status_code == 200
        assert resp2.get_json()["success"] is True

        resp3 = fresh_client.get("/api/config/")
        merged = resp3.get_json()
        assert len(merged["pv_forecast"]) == 1
        assert merged["pv_forecast"][0]["name"] == "Roof"

    def test_wizard_complete_marks_done(self, fresh_client):
        """POST /api/config/wizard-complete should mark wizard as completed."""
        resp = fresh_client.post("/api/config/wizard-complete")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["completed"] is True

        # Verify status is now completed and not pending
        resp2 = fresh_client.get("/api/config/wizard-status")
        data2 = resp2.get_json()
        assert data2["pending"] is False
        assert data2["completed"] is True

    def test_wizard_complete_idempotent(self, fresh_client):
        """Calling wizard-complete twice should succeed both times."""
        fresh_client.post("/api/config/wizard-complete")
        resp = fresh_client.post("/api/config/wizard-complete")
        assert resp.status_code == 200
        assert resp.get_json()["completed"] is True


class TestImportAppliesChanges:
    """
    Import must behave like a save, not a raw database write.

    ``ConfigStore.import_dict`` wrote straight to SQL, so an imported config never
    reached the running interfaces and never raised the restart banner.  These pin the
    behaviour that replaced it.
    """

    def test_import_fires_hot_reload_notifications(self, client):
        """Imported values must be replayed to the hot-reload callbacks."""
        client.module.notified.clear()
        resp = client.post(
            "/api/config/import",
            data=json.dumps({"price.feed_in_price": 0.42}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        notified = dict((k, v) for k, _o, v in client.module.notified)
        assert notified.get("price.feed_in_price") == 0.42

    def test_import_skips_notification_for_unchanged_values(self, client):
        """A key already holding the imported value must not trigger a reload."""
        client.post(
            "/api/config/import",
            data=json.dumps({"price.feed_in_price": 0.42}),
            content_type="application/json",
        )
        client.module.notified.clear()
        client.post(
            "/api/config/import",
            data=json.dumps({"price.feed_in_price": 0.42}),
            content_type="application/json",
        )
        assert [k for k, _o, _n in client.module.notified] == []

    def test_import_records_restart_pending(self, client):
        """A restart_required field must raise the banner that survives a reload."""
        resp = client.post(
            "/api/config/import",
            data=json.dumps({"eos_connect_web_port": 8099}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert "eos_connect_web_port" in resp.get_json()["restart_required"]

        resp2 = client.get("/api/config/restart-required")
        assert "eos_connect_web_port" in resp2.get_json()["fields"]

    def test_import_reports_hot_reloaded_fields(self, client):
        """Hot-reloadable fields are reported separately from restart-required ones."""
        resp = client.post(
            "/api/config/import",
            data=json.dumps({"price.feed_in_price": 0.11}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert "price.feed_in_price" in data["hot_reloaded"]
        assert data["restart_required"] == []

    def test_import_skips_invalid_values_without_aborting(self, client):
        """An out-of-range value is reported and skipped; the rest still imports."""
        resp = client.post(
            "/api/config/import",
            data=json.dumps(
                {"battery.min_soc_percentage": 9999, "battery.capacity_wh": 15000}
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported"] == 1
        assert [e["key"] for e in data["invalid"]] == ["battery.min_soc_percentage"]

        exported = client.get("/api/config/export").get_json()
        assert exported["battery.capacity_wh"] == 15000
        assert exported["battery.min_soc_percentage"] != 9999

    def test_import_reports_a_value_that_cannot_be_coerced(self, client):
        """
        Validation and coercion are separate gates, and both must be survivable.

        A field with no validation rules skips the range checks entirely, so a value of
        the wrong shape only fails later when it is coerced to the column's type. That
        must be reported like any other rejected value, not raise a 500.
        """
        resp = client.post(
            "/api/config/import",
            data=json.dumps(
                {"price.feed_in_price": "not a number", "battery.capacity_wh": 14000}
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported"] == 1
        assert [e["key"] for e in data["invalid"]] == ["price.feed_in_price"]
        assert "Invalid type" in data["invalid"][0]["error"]

    def test_import_does_not_count_metadata_as_skipped(self, client):
        """Backup metadata is not a setting and not an unknown key either."""
        resp = client.post(
            "/api/config/import",
            data=json.dumps(
                {
                    "_format": "eos-connect-backup",
                    "_version": 1,
                    "battery.capacity_wh": 12345,
                }
            ),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["imported"] == 1
        assert data["skipped"] == 0

    def test_import_still_counts_unknown_keys(self, client):
        """Genuinely unrecognised keys remain reported as skipped."""
        resp = client.post(
            "/api/config/import",
            data=json.dumps({"there.is.no.such.key": 1, "battery.capacity_wh": 11000}),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["imported"] == 1
        assert data["skipped"] == 1

    def test_config_import_reports_but_ignores_yield_history(self, client):
        """A full backup dropped here restores settings and names what it left alone."""
        resp = client.post(
            "/api/config/import",
            data=json.dumps(
                {
                    "_format": "eos-connect-backup",
                    "battery.capacity_wh": 17000,
                    "pv_yield_history": [{"timestamp": "2026-08-19T06:00:00+00:00"}],
                }
            ),
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["imported"] == 1
        assert data["skipped"] == 0
        assert data["ignored_datasets"] == ["pv_yield_history"]

    def test_config_import_is_merge_only(self, client):
        """The config-scoped endpoint never removes settings absent from the file."""
        resp = client.post(
            "/api/config/import",
            data=json.dumps({"battery.capacity_wh": 16000}),
            content_type="application/json",
        )
        assert resp.get_json()["removed"] == []
        exported = client.get("/api/config/export").get_json()
        assert exported["eos.port"] == 8503


class TestApplySettingsReplaceMode:
    """Replace mode is the exact inverse of export — no more, no less."""

    def _apply(self, client, data, mode):
        from src.config_web.api import apply_settings

        with client.application.test_request_context():
            return apply_settings(data, mode=mode)

    def test_replace_removes_stale_keys(self, client):
        """Settings absent from the file are removed, not left behind."""
        exported = client.get("/api/config/export").get_json()
        assert "eos.port" in exported

        payload = {k: v for k, v in exported.items() if k != "eos.port"}
        result = self._apply(client, payload, "replace")

        assert "eos.port" in result["removed"]
        assert "eos.port" not in client.get("/api/config/export").get_json()

    def test_replace_preserves_internal_keys(self, client):
        """Wiping _wizard_completed would pop the setup wizard after every restore."""
        client.post("/api/config/wizard-complete")
        assert client.get("/api/config/wizard-status").get_json()["completed"] is True

        self._apply(client, {"battery.capacity_wh": 20000}, "replace")

        assert client.get("/api/config/wizard-status").get_json()["completed"] is True

    def test_replace_leaves_unschemad_keys_alone(self, client):
        """The raw pv_forecast list is a merger fallback, not a stale setting."""
        client.store.set("pv_forecast", [{"name": "legacy"}])
        self._apply(client, {"battery.capacity_wh": 20000}, "replace")
        assert client.store.has_key("pv_forecast")

    def test_replace_drops_surplus_pv_installations(self, client):
        """Restoring a one-installation backup must remove the second installation."""
        client.store.set("pv_forecast.1.name", "second")
        exported = client.get("/api/config/export").get_json()
        assert "pv_forecast.1.name" in exported

        payload = {k: v for k, v in exported.items() if not k.startswith("pv_forecast.1.")}
        result = self._apply(client, payload, "replace")

        assert "pv_forecast.1.name" in result["removed"]

    def test_replace_notifies_removed_keys_with_default(self, client):
        """A removed key reverts to its schema default; interfaces must be told."""
        exported = client.get("/api/config/export").get_json()
        payload = {k: v for k, v in exported.items() if k != "price.feed_in_price"}

        client.module.notified.clear()
        self._apply(client, payload, "replace")

        notified = dict((k, v) for k, _o, v in client.module.notified)
        assert "price.feed_in_price" in notified
        assert notified["price.feed_in_price"] == 0.0

    def test_replace_round_trip_is_stable(self, client):
        """Exporting and replacing with the same file must change nothing."""
        exported = client.get("/api/config/export").get_json()
        result = self._apply(client, exported, "replace")

        assert result["removed"] == []
        assert result["invalid"] == []
        assert client.get("/api/config/export").get_json() == exported
