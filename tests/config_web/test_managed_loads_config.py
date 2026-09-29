"""The managed_loads list section: merging stored entries, and entry-relative deps."""

import json

import pytest
from flask import Flask

from src.config_web.api import (
    _dependency_met,
    _dependency_value,
    _entry_prefix,
    config_bp,
    init_api,
)
from src.config_web.merger import build_merged_config
from src.config_web.store import ConfigStore
from src.config_web.merger import _build_managed_loads
from src.config_web.schema import LIST_SECTIONS, ConfigSchema

SCHEMA = ConfigSchema()


class _Module:
    """Minimal stand-in for ConfigWebModule, mirroring the one in test_api.py."""

    def __init__(self, store, schema):
        self._store = store
        self._schema = schema

    def get_config(self):
        return build_merged_config({}, self._store, self._schema)

    def rebuild_config(self):
        """No-op."""

    def notify_config_changed(self, key, old_value, new_value):
        """No-op."""


@pytest.fixture(name="config_client")
def config_client_fixture(tmp_path):
    """A Flask client over the config blueprint, on a throwaway database."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    store = ConfigStore(str(tmp_path / "test.db"))
    store.open()
    init_api(store, SCHEMA, _Module(store, SCHEMA))
    app.register_blueprint(config_bp)

    with app.test_client() as client:
        def put_config(values):
            return client.put(
                "/api/config/", data=json.dumps(values),
                content_type="application/json",
            )

        client.put_config = put_config
        yield client

    store.close()


# --- merging -------------------------------------------------------------------------

def test_indexed_keys_become_a_list_of_entries():
    settings = {
        "managed_loads.0.id": "pool",
        "managed_loads.0.type": "pool_heatpump",
        "managed_loads.0.target_temp": 28.0,
        "managed_loads.1.id": "sauna",
        "managed_loads.1.type": "sauna",
    }
    entries = _build_managed_loads(settings, {}, {})
    assert [e["id"] for e in entries] == ["pool", "sauna"]
    assert entries[0]["target_temp"] == 28.0


def test_entries_are_ordered_by_index_not_by_dict_order():
    settings = {
        "managed_loads.2.id": "c", "managed_loads.2.type": "sauna",
        "managed_loads.0.id": "a", "managed_loads.0.type": "sauna",
        "managed_loads.10.id": "d", "managed_loads.10.type": "sauna",
        "managed_loads.1.id": "b", "managed_loads.1.type": "sauna",
    }
    assert [e["id"] for e in _build_managed_loads(settings, {}, {})] == ["a", "b", "c", "d"]


def test_unset_keys_are_left_absent_for_the_presets_to_fill():
    """
    Filling them here as well would give a sauna a pool's swimming season - the
    defaults that matter depend on the type, and only ``loads.presets`` knows that.
    """
    settings = {"managed_loads.0.id": "sauna", "managed_loads.0.type": "sauna"}
    entry = _build_managed_loads(settings, {}, {})[0]
    assert "season_start" not in entry
    assert "volume_m3" not in entry


@pytest.mark.parametrize("settings", [
    {"managed_loads.0.type": "sauna"},                       # no id
    {"managed_loads.0.id": "  ", "managed_loads.0.type": "sauna"},  # blank id
    {"managed_loads.0.id": "pool"},                          # no type
])
def test_unusable_entries_are_dropped_at_merge_time(settings, caplog):
    """An entry with no id has no MQTT topic; one with no type has no model."""
    with caplog.at_level("WARNING"):
        assert _build_managed_loads(settings, {}, {}) == []
    assert any("ignoring it" in r.getMessage() for r in caplog.records)


def test_ids_are_stripped():
    settings = {"managed_loads.0.id": " pool ", "managed_loads.0.type": "pool_heatpump"}
    assert _build_managed_loads(settings, {}, {})[0]["id"] == "pool"


def test_a_restored_backup_list_is_accepted():
    settings = {"managed_loads": [{"id": "pool", "type": "pool_heatpump"}, "junk"]}
    entries = _build_managed_loads(settings, {}, {})
    assert entries == [{"id": "pool", "type": "pool_heatpump"}]


def test_nothing_configured_yields_an_empty_list():
    assert _build_managed_loads({}, {}, {}) == []
    assert _build_managed_loads({}, {}, {"managed_loads": []}) == []


def test_managed_loads_is_a_list_section():
    assert "managed_loads" in LIST_SECTIONS
    assert SCHEMA.defaults_dict()["managed_loads"] == []


# --- entry-relative dependencies -------------------------------------------------------

def test_entry_prefix_recognises_an_indexed_key():
    assert _entry_prefix("managed_loads.3.temp_sensor") == ("managed_loads", 3)
    assert _entry_prefix("load.load_sensor") is None
    assert _entry_prefix("") is None


def test_a_relative_dependency_reads_the_same_entry_from_the_request():
    data = {"managed_loads.1.type": "sauna", "managed_loads.0.type": "external_profile"}
    value = _dependency_value("type", "managed_loads.1.volume_m3", data, {})
    assert value == "sauna"


def test_a_relative_dependency_falls_back_to_the_stored_entry():
    config = {"managed_loads": [{"id": "pool", "type": "pool_heatpump"}]}
    value = _dependency_value("type", "managed_loads.0.volume_m3", {}, config)
    assert value == "pool_heatpump"


def test_a_relative_dependency_outside_a_list_entry_is_unknown():
    assert _dependency_value("type", "load.load_sensor", {}, {}) is None


def test_an_absolute_dependency_still_resolves_from_the_nested_config():
    config = {"data_source": {"type": "homeassistant"}}
    assert _dependency_value("data_source.type", "load.load_sensor", {}, config) == "homeassistant"


def test_a_thermal_field_does_not_apply_to_a_pushed_profile():
    """The failure this prevents: an empty volume rejecting the whole save."""
    field = SCHEMA.get("managed_loads.volume_m3")
    data = {"managed_loads.0.type": "external_profile"}
    assert _dependency_met(field, data, {}, "managed_loads.0.volume_m3") is False


def test_a_thermal_field_applies_to_a_pool():
    field = SCHEMA.get("managed_loads.volume_m3")
    data = {"managed_loads.0.type": "pool_heatpump"}
    assert _dependency_met(field, data, {}, "managed_loads.0.volume_m3") is True


def test_two_entries_of_different_types_are_judged_independently():
    """The reason the dependency has to be entry-relative at all."""
    field = SCHEMA.get("managed_loads.cover_sensor")
    data = {
        "managed_loads.0.type": "pool_heatpump",
        "managed_loads.1.type": "hot_water_tank",
    }
    assert _dependency_met(field, data, {}, "managed_loads.0.cover_sensor") is True
    assert _dependency_met(field, data, {}, "managed_loads.1.cover_sensor") is False


# --- reading a list section over HTTP ---------------------------------------------------

def test_reading_a_list_section_returns_its_entries(config_client):
    """
    A list section holds one dict per entry, not a flat group of keys.

    Masking it as a dict raised a 500, which ``pv_forecast`` had done since it was
    added - nothing reads this route for it, because the settings UI works from
    ``GET /api/config``.
    """
    response = config_client.get("/api/config/section/managed_loads")
    assert response.status_code == 200
    assert response.get_json() == []


def test_reading_the_pv_forecast_section_no_longer_errors(config_client):
    response = config_client.get("/api/config/section/pv_forecast")
    assert response.status_code == 200
    assert isinstance(response.get_json(), list)


def test_a_configured_list_section_reads_back(config_client):
    config_client.put_config({
        "managed_loads.0.id": "pool",
        "managed_loads.0.type": "pool_heatpump",
        "managed_loads.0.target_temp": 27.5,
    })
    entries = config_client.get("/api/config/section/managed_loads").get_json()
    assert [e["id"] for e in entries] == ["pool"]
    assert entries[0]["target_temp"] == 27.5
