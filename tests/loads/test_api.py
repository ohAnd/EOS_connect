"""The HTTP surface: pushing a forecast, reading status, overriding a release."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from flask import Flask

from src.loads import api as loads_api
from src.loads.manager import ManagedLoadManager, ManagedLoadSources
from src.loads.presets import TYPE_EXTERNAL_CONTINGENT, TYPE_EXTERNAL_PROFILE, TYPE_POOL_HEATPUMP

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 6, 1, 6, 30, tzinfo=BERLIN)

ENTRIES = [
    {"id": "heating", "type": TYPE_EXTERNAL_PROFILE},
    {"id": "budget", "type": TYPE_EXTERNAL_CONTINGENT, "rated_power_w": 2000},
    {
        "id": "pool", "type": TYPE_POOL_HEATPUMP,
        "temp_sensor": "sensor.pool", "power_sensor": "sensor.pool_power",
        "window_start": None, "window_end": None,
        "season_start": None, "season_end": None, "min_ambient_temp_c": None,
    },
]


@pytest.fixture(name="client")
def client_fixture():
    manager = ManagedLoadManager(
        ENTRIES,
        time_frame_base=3600,
        time_zone=BERLIN,
        sources=ManagedLoadSources(read_sensor=lambda name: {"sensor.pool": 24.0}.get(name)),
        clock=lambda: NOW,
    )
    loads_api.init_api(manager)

    app = Flask(__name__)
    app.register_blueprint(loads_api.loads_bp)
    app.config["TESTING"] = True
    with app.test_client() as client:
        client.manager = manager
        yield client
    loads_api.init_api(None)


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


# --- listing ---------------------------------------------------------------------------

def test_listing_reports_every_configured_load(client):
    body = client.get("/api/managed_loads").get_json()
    assert [load["id"] for load in body["loads"]] == ["budget", "heating", "pool"]
    assert body["enabled"] is True


def test_a_single_load_can_be_fetched(client):
    body = client.get("/api/managed_loads/pool").get_json()
    assert body["id"] == "pool"
    assert body["type"] == TYPE_POOL_HEATPUMP


def test_an_unknown_load_is_a_404(client):
    response = client.get("/api/managed_loads/nope")
    assert response.status_code == 404
    assert "nope" in response.get_json()["error"]


# --- pushing ---------------------------------------------------------------------------

def test_pushing_a_profile_echoes_the_normalised_series(client):
    """Getting `start` wrong is invisible from the sending end - so echo the result."""
    response = _post(client, "/api/managed_loads/heating/push", {"values": [1000.0] * 24})
    body = response.get_json()

    assert response.status_code == 200
    assert body["accepted"] is True
    assert body["kind"] == "profile"
    assert body["slots"] == 48
    assert body["source_resolution_s"] == 3600
    assert body["values"][:24] == [1000.0] * 24
    assert body["values"][24:] == [0.0] * 24


def test_a_pushed_profile_reaches_the_load_forecast(client):
    _post(client, "/api/managed_loads/heating/push", {"value_wh": 500})
    client.manager.run_cycle()
    assert client.manager.apply([100.0] * 48)[0] == pytest.approx(600.0)


def test_a_ninetysix_slot_push_is_accepted(client):
    """j4rvisstant's form, against an optimizer running on hourly slots."""
    body = _post(client, "/api/managed_loads/heating/push",
                 {"values": [250.0] * 96}).get_json()
    assert body["source_resolution_s"] == 900
    assert body["values"][:24] == [1000.0] * 24


def test_a_bare_number_is_accepted(client):
    body = _post(client, "/api/managed_loads/heating/push", 750).get_json()
    assert body["total_wh"] == pytest.approx(750 * 48)


def test_pushing_a_contingent(client):
    body = _post(client, "/api/managed_loads/budget/push",
                 {"total_wh": 8000, "deadline_hours": 12}).get_json()
    assert body["kind"] == "contingent"
    assert body["total_wh"] == 8000


def test_a_malformed_payload_is_a_400_with_a_usable_message(client):
    response = _post(client, "/api/managed_loads/heating/push", {"values": [1.0] * 7})
    assert response.status_code == 400
    assert "cannot infer" in response.get_json()["error"]


def test_broken_json_is_reported_as_broken_json(client):
    response = client.post(
        "/api/managed_loads/heating/push", data="{not json",
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "not valid JSON" in response.get_json()["error"]


def test_pushing_to_a_self_computing_load_is_refused(client):
    response = _post(client, "/api/managed_loads/pool/push", {"value_wh": 100})
    assert response.status_code == 400
    assert "computes its own demand" in response.get_json()["error"]


def test_pushing_to_an_unknown_load_is_refused(client):
    assert _post(client, "/api/managed_loads/nope/push", {"value_wh": 1}).status_code == 400


def test_a_push_can_be_cleared(client):
    _post(client, "/api/managed_loads/heating/push", {"value_wh": 500})
    client.manager.run_cycle()

    before = client.manager.apply([100.0] * 48)

    body = client.delete("/api/managed_loads/heating/push").get_json()
    assert body["cleared"] is True

    ids = [c["id"] for c in client.manager.registry.snapshot()]
    assert "heating" not in ids
    # The pool in the same fixture keeps contributing; only the pushed profile goes.
    after = client.manager.apply([100.0] * 48)
    assert sum(before) - sum(after) == pytest.approx(500 * 48)


def test_clearing_an_unknown_load_is_a_404(client):
    assert client.delete("/api/managed_loads/nope/push").status_code == 404


# --- overrides -------------------------------------------------------------------------

def test_an_override_forces_a_release(client):
    body = _post(client, "/api/managed_loads/pool/override",
                 {"mode": "release", "minutes": 30}).get_json()
    assert body["override"]["override"] == "release"

    client.manager.run_cycle()
    assert client.manager.instance("pool").last_release["released"] is True


def test_an_override_can_block(client):
    _post(client, "/api/managed_loads/pool/override", {"mode": "block", "minutes": 30})
    client.manager.run_cycle()
    assert client.manager.instance("pool").last_release["released"] is False


def test_an_override_can_be_cleared(client):
    _post(client, "/api/managed_loads/pool/override", {"mode": "block", "minutes": 30})
    body = _post(client, "/api/managed_loads/pool/override", {"mode": "clear"}).get_json()
    assert body["override"]["override"] is None


@pytest.mark.parametrize("payload,fragment", [
    ({"mode": "maybe"}, "must be 'release'"),
    ({"mode": "release", "minutes": "soon"}, "whole number"),
    ({"mode": "release", "minutes": 0}, "between 1 and 1440"),
    ({"mode": "release", "minutes": 99999}, "between 1 and 1440"),
])
def test_a_bad_override_is_refused(client, payload, fragment):
    response = _post(client, "/api/managed_loads/pool/override", payload)
    assert response.status_code == 400
    assert fragment in response.get_json()["error"]


def test_overriding_a_load_without_a_release_signal_is_refused(client):
    response = _post(client, "/api/managed_loads/heating/override", {"mode": "release"})
    assert response.status_code == 400
    assert "no release signal" in response.get_json()["error"]


def test_overriding_an_unknown_load_is_a_404(client):
    assert _post(client, "/api/managed_loads/nope/override",
                 {"mode": "release"}).status_code == 404


# --- not configured ---------------------------------------------------------------------

def test_every_route_reports_cleanly_when_nothing_is_configured():
    loads_api.init_api(None)
    app = Flask(__name__)
    app.register_blueprint(loads_api.loads_bp)
    with app.test_client() as client:
        for response in (
            client.get("/api/managed_loads"),
            client.get("/api/managed_loads/pool"),
            _post(client, "/api/managed_loads/pool/push", {"value_wh": 1}),
            client.delete("/api/managed_loads/pool/push"),
            _post(client, "/api/managed_loads/pool/override", {"mode": "release"}),
        ):
            assert response.status_code == 404
            assert "not configured" in response.get_json()["error"]
