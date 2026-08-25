"""
Tests for the timeseries endpoint and the save-time pre-flight.

Two properties matter here: the documented POST /api/config/test-timeseries endpoint
exists and reports what a source yields, and saving an unrelated setting does not fire
a network probe (which would make every save depend on a reachable endpoint).
"""

import json
from unittest.mock import patch

import pytest
from flask import Flask

from src.config_web.api import config_bp, init_api
from src.config_web.merger import build_merged_config
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore

from .test_api import _FakeModule, _sample_config

EVCC_RATES = {
    "rates": [
        {"start": "2026-08-23T00:00:00+02:00", "value": 0.3811},
        {"start": "2026-08-23T00:15:00+02:00", "value": 0.3727},
    ]
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    """Flask client whose price source is already set to timeseries."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "test.db"))
    store.open()

    config = _sample_config()
    migrate_yaml_to_store(config, store, schema)
    store.set("price.source", "timeseries")
    store.set("price.data_url", "http://evcc.local/api/tariff/grid")
    store.set("price.data_path", "rates")
    store.set("price.value_unit", "EUR/kWh")

    module = _FakeModule(config, store, schema)
    init_api(store, schema, module)
    app.register_blueprint(config_bp)

    with app.test_client() as c:
        c.store = store
        yield c

    store.close()


def post_test_timeseries(client, body=None, payload=EVCC_RATES, status_code=200):
    with patch(
        "src.config_web.timeseries_probe.requests.get",
        return_value=FakeResponse(payload, status_code),
    ) as mock_get:
        resp = client.post(
            "/api/config/test-timeseries",
            data=json.dumps(body or {}),
            content_type="application/json",
        )
    return resp, mock_get


class TestTestTimeseriesEndpoint:
    """The endpoint the docs have described all along."""

    def test_endpoint_exists_and_reports_a_healthy_source(self, client):
        resp, _ = post_test_timeseries(client)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["entry_count"] == 2
        assert data["resolution_seconds"] == 900

    def test_reports_converted_values_in_display_units(self, client):
        resp, _ = post_test_timeseries(client)

        first = resp.get_json()["slots"][0]
        assert first["unit"] == "ct/kWh"
        assert first["value"] == pytest.approx(38.11, abs=0.01)

    def test_unsaved_values_from_the_form_are_honoured(self, client):
        """The UI tests what the user typed, before it is saved."""
        resp, mock_get = post_test_timeseries(
            client, {"price.data_url": "http://other.local/api/tariff/grid"}
        )

        assert resp.status_code == 200
        assert mock_get.call_args[0][0] == "http://other.local/api/tariff/grid"

    def test_unsaved_unit_change_is_honoured_and_warns(self, client):
        resp, _ = post_test_timeseries(client, {"price.value_unit": "EUR/Wh"})

        data = resp.get_json()
        assert data["ok"] is True
        assert any("implausible" in w for w in data["warnings"])

    def test_failure_is_reported_as_a_result_not_a_server_error(self, client):
        resp, _ = post_test_timeseries(client, payload={}, status_code=404)

        assert resp.status_code == 200
        assert resp.get_json()["ok"] is False

    def test_pv_domain_supported(self, client):
        payload = {"attributes": {"data": [
            {"start": "2026-08-23T00:00:00+02:00", "value": 4000.0},
            {"start": "2026-08-23T00:15:00+02:00", "value": 4000.0},
        ]}}
        resp, _ = post_test_timeseries(
            client,
            {
                "domain": "pv",
                "pv_forecast_source.source": "timeseries",
                "pv_forecast_source.data_url": "http://ha.local/api/states/sensor.pv",
                "pv_forecast_source.data_path": "attributes.data",
                "pv_forecast_source.value_unit": "W",
            },
            payload=payload,
        )

        data = resp.get_json()
        assert data["ok"] is True
        assert data["slots"][0]["unit"] == "Wh"

    def test_unknown_domain_rejected(self, client):
        resp = client.post(
            "/api/config/test-timeseries",
            data=json.dumps({"domain": "weather"}),
            content_type="application/json",
        )

        assert resp.status_code == 400


class TestPreflightScope:
    """A save must not depend on an endpoint it has no reason to contact."""

    def test_unrelated_setting_change_does_not_probe(self, client):
        with patch("src.config_web.timeseries_probe.requests.get") as mock_get:
            resp = client.put(
                "/api/config/",
                data=json.dumps({"battery.min_soc_percentage": 12}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        mock_get.assert_not_called()

    def test_timeseries_field_change_does_probe(self, client):
        with patch(
            "src.config_web.timeseries_probe.requests.get",
            return_value=FakeResponse(EVCC_RATES),
        ) as mock_get:
            resp = client.put(
                "/api/config/",
                data=json.dumps({"price.data_path": "rates"}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        mock_get.assert_called_once()

    def test_broken_source_blocks_the_save_against_the_right_field(self, client):
        with patch(
            "src.config_web.timeseries_probe.requests.get",
            return_value=FakeResponse({"rates": [{"start_time": "x", "price_per_kwh": 1}]}),
        ):
            resp = client.put(
                "/api/config/",
                data=json.dumps({"price.data_path": "rates"}),
                content_type="application/json",
            )

        assert resp.status_code == 422
        errors = resp.get_json()["errors"]
        assert errors[0]["key"] == "price.data_path"
        assert "'start_time'" in errors[0]["error"]

    def test_a_unit_warning_does_not_block_the_save(self, client):
        with patch(
            "src.config_web.timeseries_probe.requests.get",
            return_value=FakeResponse(EVCC_RATES),
        ):
            resp = client.put(
                "/api/config/",
                data=json.dumps({"price.value_unit": "EUR/Wh"}),
                content_type="application/json",
            )

        assert resp.status_code == 200


class TestCentralModeResolution:
    """
    The probe must resolve the connection exactly as the running interface will.

    The merged config is not a safe source for data_url/data_token: the merger
    overwrites them with values derived from the central data source, so a request
    that switches central mode off would otherwise be probed against the stale
    derived URL.
    """

    def _central_client(self, tmp_path):
        app = Flask(__name__)
        app.config["TESTING"] = True
        schema = ConfigSchema()
        store = ConfigStore(str(tmp_path / "central.db"))
        store.open()
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)
        store.set("price.source", "timeseries")
        store.set("price.use_ha_central_data_source", True)
        store.set("price.ha_sensor_name", "sensor.grid_prices")
        store.set("price.data_path", "attributes.data")
        store.set("price.data_url", "http://direct.local/prices")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "tok")
        module = _FakeModule(config, store, schema)
        init_api(store, schema, module)
        app.register_blueprint(config_bp)
        return app, store

    def test_central_mode_probes_the_derived_sensor_url(self, tmp_path):
        app, store = self._central_client(tmp_path)
        payload = {"attributes": {"data": EVCC_RATES["rates"]}}

        with app.test_client() as client:
            with patch(
                "src.config_web.timeseries_probe.requests.get",
                return_value=FakeResponse(payload),
            ) as mock_get:
                client.post(
                    "/api/config/test-timeseries",
                    data=json.dumps({}),
                    content_type="application/json",
                )

        assert mock_get.call_args[0][0] == "http://ha.local:8123/api/states/sensor.grid_prices"
        store.close()

    def test_switching_central_off_probes_the_users_own_url(self, tmp_path):
        app, store = self._central_client(tmp_path)

        with app.test_client() as client:
            with patch(
                "src.config_web.timeseries_probe.requests.get",
                return_value=FakeResponse({"rates": EVCC_RATES["rates"]}),
            ) as mock_get:
                client.post(
                    "/api/config/test-timeseries",
                    data=json.dumps(
                        {
                            "price.use_ha_central_data_source": False,
                            "price.data_path": "rates",
                        }
                    ),
                    content_type="application/json",
                )

        assert mock_get.call_args[0][0] == "http://direct.local/prices"
        store.close()

    def test_central_url_is_built_the_same_way_the_merger_builds_it(self, tmp_path):
        """
        Parity check against merger._apply_central_ha_data_source.

        If the two ever diverge the probe would validate a URL the interface never
        fetches, which is worse than having no probe at all.
        """
        app, store = self._central_client(tmp_path)
        schema = ConfigSchema()
        config = _sample_config()
        merged = build_merged_config(config, store, schema)
        expected = merged["price"]["data_url"]

        with app.test_client() as client:
            with patch(
                "src.config_web.timeseries_probe.requests.get",
                return_value=FakeResponse({"attributes": {"data": EVCC_RATES["rates"]}}),
            ) as mock_get:
                client.post(
                    "/api/config/test-timeseries",
                    data=json.dumps({}),
                    content_type="application/json",
                )

        assert mock_get.call_args[0][0] == expected
        store.close()


class TestPreflightTriggerScope:
    """The shared data source may only trigger a domain that opted into it."""

    def test_data_source_edit_does_not_probe_a_non_central_domain(self, client):
        with patch("src.config_web.timeseries_probe.requests.get") as mock_get:
            resp = client.put(
                "/api/config/",
                data=json.dumps({"data_source.url": "http://ha.local:8123"}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        mock_get.assert_not_called()

    def test_data_source_edit_does_probe_a_central_domain(self, client):
        client.store.set("price.use_ha_central_data_source", True)
        client.store.set("price.data_path", "attributes.data")

        with patch(
            "src.config_web.timeseries_probe.requests.get",
            return_value=FakeResponse({"attributes": {"data": EVCC_RATES["rates"]}}),
        ) as mock_get:
            resp = client.put(
                "/api/config/",
                data=json.dumps({"data_source.url": "http://ha.local:8123"}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        mock_get.assert_called_once()


class TestPreflightRobustness:
    """Pre-flight guards against misconfiguration — not against a source being down."""

    def test_unreachable_endpoint_does_not_block_the_save(self, client):
        import requests as _requests

        with patch(
            "src.config_web.timeseries_probe.requests.get",
            side_effect=_requests.exceptions.ConnectionError("down"),
        ):
            resp = client.put(
                "/api/config/",
                data=json.dumps({"price.data_path": "rates"}),
                content_type="application/json",
            )

        assert resp.status_code == 200

    def test_switching_source_on_does_not_block_on_the_placeholder_url(self, client):
        import requests as _requests

        client.store.set("price.source", "default")
        with patch(
            "src.config_web.timeseries_probe.requests.get",
            side_effect=_requests.exceptions.Timeout(),
        ):
            resp = client.put(
                "/api/config/",
                data=json.dumps({"price.source": "timeseries"}),
                content_type="application/json",
            )

        assert resp.status_code == 200

    def test_a_bad_format_still_blocks(self, client):
        with patch(
            "src.config_web.timeseries_probe.requests.get",
            return_value=FakeResponse({"rates": [{"nope": 1}]}),
        ):
            resp = client.put(
                "/api/config/",
                data=json.dumps({"price.data_path": "rates"}),
                content_type="application/json",
            )

        assert resp.status_code == 422

    def test_string_false_is_not_treated_as_central_mode(self, client):
        """
        Pre-flight runs before _coerce_value, so the raw body may carry "false".

        Truthiness-testing it would send the probe down the central-HA branch and
        reject a perfectly valid direct-URL save.
        """
        with patch(
            "src.config_web.timeseries_probe.requests.get",
            return_value=FakeResponse(EVCC_RATES),
        ) as mock_get:
            resp = client.put(
                "/api/config/",
                data=json.dumps(
                    {
                        "price.use_ha_central_data_source": "false",
                        "price.data_path": "rates",
                    }
                ),
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert mock_get.call_args[0][0] == "http://evcc.local/api/tariff/grid"


class TestEffectiveTimeFrame:
    """
    The probe must judge resolution against the slot length actually used.

    eos_connect forces 900 back to 3600 unless an EVopt backend is selected, so taking
    eos.time_frame verbatim would reject hourly sources on systems that run hourly.
    """

    HOURLY_PAYLOAD = {
        "rates": [
            {"start": "2026-08-23T00:00:00+02:00", "value": 0.30},
            {"start": "2026-08-23T01:00:00+02:00", "value": 0.31},
        ]
    }

    def test_hourly_source_accepted_when_900_is_not_actually_honoured(self, client):
        client.store.set("eos.time_frame", 900)
        client.store.set("eos.source", "eos_server")

        resp, _ = post_test_timeseries(client, payload=self.HOURLY_PAYLOAD)

        assert resp.get_json()["ok"] is True

    def test_hourly_source_rejected_when_900_really_applies(self, client):
        client.store.set("eos.time_frame", 900)
        client.store.set("eos.source", "local_evopt")

        resp, _ = post_test_timeseries(client, payload=self.HOURLY_PAYLOAD)

        data = resp.get_json()
        assert data["ok"] is False
        assert "15-minute slots" in data["error"]


class TestConnectionFieldResolution:
    """
    The probe must resolve data_url/data_token from what the user set, never from the
    merger's central-HA derivation.
    """

    def _client_with_central_only(self, tmp_path):
        app = Flask(__name__)
        app.config["TESTING"] = True
        schema = ConfigSchema()
        store = ConfigStore(str(tmp_path / "leak.db"))
        store.open()
        config = _sample_config()
        migrate_yaml_to_store(config, store, schema)
        store.set("price.source", "timeseries")
        store.set("price.use_ha_central_data_source", True)
        store.set("price.ha_sensor_name", "sensor.grid_prices")
        store.set("data_source.url", "http://ha.local:8123")
        store.set("data_source.access_token", "ha-token")
        # price.data_url deliberately never written: only the schema default exists.
        module = _FakeModule(config, store, schema)
        init_api(store, schema, module)
        app.register_blueprint(config_bp)
        return app, store

    def test_turning_central_off_does_not_reuse_the_derived_url(self, tmp_path):
        app, store = self._client_with_central_only(tmp_path)

        with app.test_client() as client:
            with patch(
                "src.config_web.timeseries_probe.requests.get",
                return_value=FakeResponse({"attributes": {"data": EVCC_RATES["rates"]}}),
            ) as mock_get:
                client.post(
                    "/api/config/test-timeseries",
                    data=json.dumps({"price.use_ha_central_data_source": False}),
                    content_type="application/json",
                )

        # Must not be built from data_source.url; falling back to the schema default
        # for price.data_url is correct here.
        probed_url = mock_get.call_args[0][0]
        assert "ha.local:8123" not in probed_url
        store.close()

    def test_turning_central_off_does_not_reuse_the_ha_token(self, tmp_path):
        app, store = self._client_with_central_only(tmp_path)

        with app.test_client() as client:
            with patch(
                "src.config_web.timeseries_probe.requests.get",
                return_value=FakeResponse({"attributes": {"data": EVCC_RATES["rates"]}}),
            ) as mock_get:
                client.post(
                    "/api/config/test-timeseries",
                    data=json.dumps({"price.use_ha_central_data_source": False}),
                    content_type="application/json",
                )

        headers = mock_get.call_args[1]["headers"]
        assert "ha-token" not in headers.get("Authorization", "")
        store.close()

    def test_empty_sensor_name_is_not_silently_defaulted(self, tmp_path):
        """
        The merger passes an empty entity through, producing '/api/states/'.

        Substituting a default here would test a URL the interface never fetches.
        """
        app, store = self._client_with_central_only(tmp_path)
        store.set("price.ha_sensor_name", "")

        with app.test_client() as client:
            with patch(
                "src.config_web.timeseries_probe.requests.get",
                return_value=FakeResponse({}, 404),
            ) as mock_get:
                client.post(
                    "/api/config/test-timeseries",
                    data=json.dumps({}),
                    content_type="application/json",
                )

        assert mock_get.call_args[0][0] == "http://ha.local:8123/api/states/"
        store.close()


class TestSharedDataSourceEditsAreNotBlocked:
    """Rotating the HA token must not depend on every domain's sensor being healthy."""

    def test_broken_central_domain_does_not_block_a_token_rotation(self, client):
        client.store.set("price.use_ha_central_data_source", True)
        client.store.set("price.ha_sensor_name", "sensor.deleted")
        client.store.set("price.data_path", "attributes.data")

        with patch(
            "src.config_web.timeseries_probe.requests.get",
            return_value=FakeResponse({}, 404),
        ):
            resp = client.put(
                "/api/config/",
                data=json.dumps({"data_source.access_token": "new-token"}),
                content_type="application/json",
            )

        assert resp.status_code == 200

    def test_editing_the_domains_own_field_still_blocks(self, client):
        client.store.set("price.use_ha_central_data_source", True)
        client.store.set("price.data_path", "attributes.data")

        with patch(
            "src.config_web.timeseries_probe.requests.get",
            return_value=FakeResponse({}, 404),
        ):
            resp = client.put(
                "/api/config/",
                data=json.dumps({"price.ha_sensor_name": "sensor.deleted"}),
                content_type="application/json",
            )

        assert resp.status_code == 422
