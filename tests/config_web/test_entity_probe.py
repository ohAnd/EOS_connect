"""
Does the sensor test tell the user something true?

A sensor name is free text and a typo is invisible at runtime — Home Assistant answers
an unknown entity on the history endpoint with 200 and an empty list, so the load
profile silently becomes the built-in default. The probe exists so the user can tell
the two apart before saving, which is only worth anything if its verdicts are right.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from flask import Flask

from src.config_web.api import config_bp, init_api
from src.config_web.entity_probe import probe_entity
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
from tests.config_web.test_api import _FakeModule, _sample_config


def _http_error(status):
    response = MagicMock()
    response.status_code = status
    return requests.exceptions.HTTPError(f"{status}", response=response)


class TestProbeEntity:
    """The verdicts, without the HTTP layer."""

    OK = dict(source="homeassistant", url="http://ha.local:8123", access_token="tok")

    def test_a_readable_entity_reports_its_value(self):
        with patch("src.config_web.entity_probe.fetch_remote_state", return_value="431.2"):
            result = probe_entity(sensor="sensor.house_power", **self.OK)

        assert result["ok"] is True
        assert result["value"] == "431.2"

    def test_a_404_names_the_entity_and_the_expected_shape(self):
        with patch("src.config_web.entity_probe.fetch_remote_state",
                   side_effect=_http_error(404)):
            result = probe_entity(sensor="Load_Power", **self.OK)

        assert result["ok"] is False
        assert "not found" in result["error"]
        assert "Load_Power" in result["error"]
        assert "sensor.house_power" in result["error"]

    @pytest.mark.parametrize("status", [401, 403])
    def test_an_auth_failure_points_at_the_token(self, status):
        with patch("src.config_web.entity_probe.fetch_remote_state",
                   side_effect=_http_error(status)):
            result = probe_entity(sensor="sensor.house_power", **self.OK)

        assert result["ok"] is False
        assert "access token" in result["error"]

    def test_an_unreachable_host_is_a_finding_not_a_crash(self):
        with patch("src.config_web.entity_probe.fetch_remote_state",
                   side_effect=requests.exceptions.ConnectionError("no route")):
            result = probe_entity(sensor="sensor.house_power", **self.OK)

        assert result["ok"] is False
        assert "Could not reach" in result["error"]

    @pytest.mark.parametrize("state", ["unknown", "unavailable"])
    def test_an_entity_that_exists_but_reports_nothing_useful_fails(self, state):
        """Existing is not the same as usable, and the difference matters here."""
        with patch("src.config_web.entity_probe.fetch_remote_state", return_value=state):
            result = probe_entity(sensor="sensor.house_power", **self.OK)

        assert result["ok"] is False
        assert state in result["error"]

    def test_it_refuses_before_reaching_the_network(self):
        """Nothing to test yet is worth saying plainly, not as a connection error."""
        with patch("src.config_web.entity_probe.fetch_remote_state") as fetch:
            assert probe_entity("default", "sensor.x", "http://ha")["ok"] is False
            assert probe_entity("homeassistant", "", "http://ha")["ok"] is False
            assert probe_entity("homeassistant", "sensor.x", "")["ok"] is False
            assert not fetch.called


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    app = Flask(__name__)
    app.config["TESTING"] = True

    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "probe.db"))
    store.open()
    module = _FakeModule(_sample_config(), store, schema)
    init_api(store, schema, module)
    app.register_blueprint(config_bp)

    with app.test_client() as c:
        c.store = store
        yield c

    store.close()


class TestTestEntityEndpoint:
    """The route around it."""

    def test_it_probes_the_value_in_the_form_not_the_stored_one(self, client):
        """Testing before saving is the whole point."""
        with patch("src.config_web.api.probe_entity",
                   return_value={"ok": True, "error": "", "value": "12"}) as probe:
            resp = client.post(
                "/api/config/test-entity",
                data=json.dumps({
                    "key": "load.load_sensor",
                    "load.load_sensor": "sensor.typed_but_unsaved",
                    "data_source.type": "homeassistant",
                    "data_source.url": "http://ha.local:8123",
                }),
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert probe.call_args.args[1] == "sensor.typed_but_unsaved"

    def test_a_failing_probe_is_still_a_200(self, client):
        """An entity that does not answer is a finding to show, not a server error."""
        with patch("src.config_web.api.probe_entity",
                   return_value={"ok": False, "error": "not found"}):
            resp = client.post(
                "/api/config/test-entity",
                data=json.dumps({"key": "battery.soc_sensor"}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert resp.get_json()["ok"] is False

    @pytest.mark.parametrize("body", [{}, {"key": "not.a.field"}])
    def test_it_rejects_a_key_that_names_nothing(self, client, body):
        resp = client.post(
            "/api/config/test-entity",
            data=json.dumps(body),
            content_type="application/json",
        )

        assert resp.status_code == 400


class TestItProbesTheConnectionTheSectionActuallyUses:
    """
    Load and battery always inherit the central data source, but pv_autoscaling can
    opt out and carry its own host. Probing the wrong one would report a working
    entity as missing — or, worse, a missing one as fine.
    """

    CENTRAL = {
        "data_source.type": "homeassistant",
        "data_source.url": "http://central.local:8123",
        "data_source.access_token": "central-token",
    }
    OWN = {
        "pv_autoscaling.src": "openhab",
        "pv_autoscaling.url": "http://its-own.local:8080",
        "pv_autoscaling.access_token": "its-own-token",
    }

    def _probe_args(self, client, body):
        with patch("src.config_web.api.probe_entity",
                   return_value={"ok": True, "error": "", "value": "1"}) as probe:
            client.post(
                "/api/config/test-entity",
                data=json.dumps(body),
                content_type="application/json",
            )
        return probe.call_args

    @pytest.mark.parametrize("key", ["load.load_sensor", "battery.soc_sensor"])
    def test_load_and_battery_use_the_central_data_source(self, client, key):
        args = self._probe_args(client, {"key": key, **self.CENTRAL})

        assert args.args[0] == "homeassistant"
        assert args.args[2] == "http://central.local:8123"
        assert args.kwargs["access_token"] == "central-token"

    def test_pv_autoscaling_uses_its_own_when_it_opts_out(self, client):
        args = self._probe_args(client, {
            "key": "pv_autoscaling.sensor_entity_id",
            "pv_autoscaling.use_ha_central_data_source": False,
            **self.CENTRAL,
            **self.OWN,
        })

        assert args.args[0] == "openhab"
        assert args.args[2] == "http://its-own.local:8080"
        assert args.kwargs["access_token"] == "its-own-token"

    def test_pv_autoscaling_uses_the_central_one_when_it_opts_in(self, client):
        args = self._probe_args(client, {
            "key": "pv_autoscaling.sensor_entity_id",
            "pv_autoscaling.use_ha_central_data_source": True,
            **self.CENTRAL,
            **self.OWN,
        })

        assert args.args[0] == "homeassistant"
        assert args.args[2] == "http://central.local:8123"
