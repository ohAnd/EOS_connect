"""
``POST /api/config/restart`` — the button from issue #336.

Two properties carry the feature. The response has to be sent *before* the
teardown starts, because the teardown closes the socket it would travel over;
and a second press while one restart is already running has to be refused
rather than tearing the application down twice.
"""

# pylint: disable=redefined-outer-name

from unittest.mock import patch

import pytest
from flask import Flask

from src.config_web.api import config_bp, init_api
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
from src.restart import RestartManager

from tests.config_web.test_api import _FakeModule, _sample_config


@pytest.fixture
def manager():
    """A fresh manager in place of the module singleton, which is process-wide."""
    fresh = RestartManager(exec_argv=["/usr/bin/python3", "/app/eos_connect.py"])
    with patch("src.config_web.api.RESTART", fresh):
        yield fresh


@pytest.fixture
def client(tmp_path, manager):  # pylint: disable=unused-argument
    app = Flask(__name__)
    app.config["TESTING"] = True

    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "restart.db"))
    store.open()
    config = _sample_config()
    migrate_yaml_to_store(config, store, schema)
    init_api(store, schema, _FakeModule(config, store, schema))
    app.register_blueprint(config_bp)

    with app.test_client() as test_client:
        yield test_client
    store.close()


def test_a_restart_is_accepted(client, manager):
    response = client.post("/api/config/restart")
    assert response.status_code == 202
    assert response.get_json()["status"] == "restarting"
    assert manager.requested is True


def test_the_response_points_at_the_status_to_poll(client):
    """
    The page has to tell "coming back" from "never went away". /api/boot/status
    only exists while a start is in progress, so its return is the signal.
    """
    assert client.post("/api/config/restart").get_json()["poll"] == "/api/boot/status"


def test_the_answer_comes_before_the_teardown(client, manager):
    """
    The teardown closes the listening socket. Tearing down inside the request
    would mean the browser never learns the restart was accepted — it would see
    a dropped connection and could not tell that from a crash.
    """
    client.post("/api/config/restart")
    # Requesting only records and wakes the main thread; nothing is stopped here.
    assert manager.requested is True
    assert manager.details["source"] == "web interface"


def test_a_second_press_is_refused(client):
    assert client.post("/api/config/restart").status_code == 202
    second = client.post("/api/config/restart")
    assert second.status_code == 409
    assert second.get_json()["status"] == "already_restarting"


def test_a_reason_can_be_given_and_is_recorded(client, manager):
    client.post("/api/config/restart", json={"reason": "applied MQTT settings"})
    assert manager.details["reason"] == "applied MQTT settings"


def test_a_missing_body_is_fine(client, manager):
    """The banner's button sends no body."""
    assert client.post("/api/config/restart").status_code == 202
    assert manager.details["reason"]


def test_an_absurd_reason_is_truncated(client, manager):
    """It ends up in the log; it is not a place to paste a novel."""
    client.post("/api/config/restart", json={"reason": "x" * 5000})
    assert len(manager.details["reason"]) == 200


def test_a_non_string_reason_does_not_break_the_endpoint(client, manager):
    client.post("/api/config/restart", json={"reason": {"nested": True}})
    assert manager.requested is True
    assert isinstance(manager.details["reason"], str)


def test_get_is_not_allowed(client):
    """Restarting must not be reachable by following a link or a prefetch."""
    assert client.get("/api/config/restart").status_code == 405
