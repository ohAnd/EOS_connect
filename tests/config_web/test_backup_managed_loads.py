"""
Does a backup carry what the managed loads have learned?

It did not. Settings and yield history travelled; the calibration did not - so a
restore brought back a pool heat pump that knew its volume and its rated power and
nothing about the water. That is the slowest thing on the install to rebuild: a loss
coefficient needs weeks of overnight cooling and a spread of weather before it means
anything, and the cover factor needs the cover seen both on and off. Losing it to a
reinstall costs a season.

Samples and fitted coefficients travel. Decisions do not - a decision is what *we*
did and feeds nothing, where a sample is what the *world* did and is the only thing a
calibrator can be rebuilt from.
"""

from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from src.config_web.api import config_bp, init_api
from src.config_web.backup import backup_bp, init_backup
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
from src.persistence import ManagedLoadStore

from .test_api import _FakeModule, _sample_config


def _client(tmp_path, with_store=True):
    app = Flask(__name__)
    app.config["TESTING"] = True
    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "backup.db"))
    store.open()
    module = _FakeModule(_sample_config(), store, schema)

    managed = None
    if with_store:
        managed = ManagedLoadStore(store)
        managed.ensure_schema()
        module.managed_load_store = managed

    init_api(store, schema, module)
    init_backup(store, schema, module)
    app.register_blueprint(config_bp, url_prefix="/api/config")
    app.register_blueprint(backup_bp, url_prefix="/api/backup")
    return app.test_client(), store, managed


def _teach(managed, load_id="pool", samples=5):
    """A little recorded history and a fitted estimate."""
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for index in range(samples):
        managed.record_sample(load_id, {
            "timestamp": start + timedelta(hours=index),
            "medium_c": 24.0 + index * 0.1,
            "ambient_c": 15.0,
            "power_w": 1600.0,
        })
    managed.save_model_state(load_id, {"loss_coefficient": 25.0, "cop_nominal": 4.6})


def test_the_export_carries_samples_and_coefficients(tmp_path):
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed)
        payload = client.get("/api/backup/export").get_json()
        learning = payload["managed_load_learning"]
        assert len(learning["samples"]) == 5
        assert len(learning["model"]) == 1
        assert "managed_load_learning" in payload["_datasets"]
    finally:
        store.close()


def test_a_restore_puts_the_education_back(tmp_path):
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed)
        backup = client.get("/api/backup/export").get_json()

        managed.forget("pool")
        assert managed.sample_count("pool") == 0
        assert managed.load_model_state("pool") is None

        result = client.post("/api/backup/import", json=backup).get_json()
        assert result["managed_load_learning"]["samples"] == 5
        assert managed.sample_count("pool") == 5
        assert managed.load_model_state("pool")["loss_coefficient"] == 25.0
    finally:
        store.close()


def test_every_load_that_learns_travels_not_only_the_pool(tmp_path):
    """
    The pool is the example, not the scope. A sauna, a hot-water tank and a buffer
    tank run the same thermal model and the same calibrator, and a sample is recorded
    for anything with a temperature sensor - the type never enters into it. Nothing
    here filters by load, so all of them travel or none do.
    """
    client, store, managed = _client(tmp_path)
    try:
        for load_id in ("pool", "sauna", "hot_water_tank", "buffer_tank"):
            _teach(managed, load_id=load_id, samples=3)

        learning = client.get("/api/backup/export").get_json()["managed_load_learning"]
        assert {row["load_id"] for row in learning["samples"]} == {
            "pool", "sauna", "hot_water_tank", "buffer_tank"
        }
        assert len(learning["model"]) == 4

        for load_id in ("pool", "sauna", "hot_water_tank", "buffer_tank"):
            managed.forget(load_id)

        result = client.post(
            "/api/backup/import", json={"managed_load_learning": learning}
        ).get_json()["managed_load_learning"]
        assert result["loads"] == [
            "buffer_tank", "hot_water_tank", "pool", "sauna"
        ]
        for load_id in ("pool", "sauna", "hot_water_tank", "buffer_tank"):
            assert managed.sample_count(load_id) == 3
            assert managed.load_model_state(load_id)["cop_nominal"] == 4.6
    finally:
        store.close()


def test_the_cover_habit_travels_with_the_coefficients(tmp_path):
    """
    A thermal model persists its calibrator *and* what it has seen of the cover - one
    blob under one key - so both sides of the education ride in the same row.
    """
    client, store, managed = _client(tmp_path)
    try:
        managed.save_model_state("sauna", {
            "loss_coefficient": 25.0,
            "cover_habit": {"covered_hours": [9, 10], "hours_known": 2},
        })
        backup = client.get("/api/backup/export").get_json()
        managed.forget("sauna")

        client.post("/api/backup/import", json=backup)
        restored = managed.load_model_state("sauna")
        assert restored["cover_habit"]["covered_hours"] == [9, 10]
    finally:
        store.close()


def test_a_dry_run_writes_nothing(tmp_path):
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed)
        backup = client.get("/api/backup/export").get_json()
        managed.forget("pool")

        result = client.post("/api/backup/import?dry_run=1", json=backup).get_json()
        assert result["managed_load_learning"]["samples"] == 5
        assert managed.sample_count("pool") == 0, "a preview must not restore"
    finally:
        store.close()


def test_replacing_does_not_fit_two_histories_of_one_appliance(tmp_path):
    """
    The default. Merging a backup into a store that has recorded its own observations
    since would leave the calibrator fitting two overlapping records of the same pool.
    """
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed, samples=5)
        backup = client.get("/api/backup/export").get_json()
        _teach(managed, samples=3)                    # it kept learning meanwhile
        assert managed.sample_count("pool") == 8

        client.post("/api/backup/import", json=backup)
        assert managed.sample_count("pool") == 5
    finally:
        store.close()


def test_merge_keeps_what_was_recorded_since(tmp_path):
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed, samples=5)
        backup = client.get("/api/backup/export").get_json()
        _teach(managed, samples=3)

        client.post("/api/backup/import?mode=merge", json=backup)
        assert managed.sample_count("pool") == 13
    finally:
        store.close()


def test_decisions_do_not_travel(tmp_path):
    """They are what we did, not what the world did, and feed no calibration."""
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed)
        managed.record_decision("pool", datetime(2026, 9, 1, tzinfo=timezone.utc), {
            "origin": "optimizer", "released": True, "reason": "planned cheap slot",
        })
        payload = client.get("/api/backup/export").get_json()
        assert "decisions" not in payload["managed_load_learning"]
        assert "managed_load_decisions" not in payload
    finally:
        store.close()


def test_a_host_with_no_store_degrades_rather_than_failing(tmp_path):
    client, store, _ = _client(tmp_path, with_store=False)
    try:
        info = client.get("/api/backup/info").get_json()
        assert info["managed_load_learning"]["available"] is False

        payload = client.get("/api/backup/export").get_json()
        assert payload["managed_load_learning"] == {"samples": [], "model": []}

        result = client.post("/api/backup/import", json=payload).get_json()
        assert result["managed_load_learning"]["available"] is False
    finally:
        store.close()


def test_a_malformed_row_does_not_cost_the_whole_backup(tmp_path):
    """A file from an older build, or hand-edited, restores what it can."""
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed)
        backup = client.get("/api/backup/export").get_json()
        backup["managed_load_learning"]["samples"].append({"load_id": "pool"})
        backup["managed_load_learning"]["samples"].append("not a row")
        managed.forget("pool")

        result = client.post("/api/backup/import", json=backup).get_json()
        assert result["managed_load_learning"]["samples"] == 5
        assert managed.sample_count("pool") == 5
    finally:
        store.close()


def test_info_counts_what_is_there(tmp_path):
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed)
        info = client.get("/api/backup/info").get_json()
        assert info["managed_load_learning"]["available"] is True
        assert info["managed_load_learning"]["count"] == 5
        assert "managed_load_learning" in info["datasets"]
    finally:
        store.close()


def test_it_can_be_deselected(tmp_path):
    client, store, managed = _client(tmp_path)
    try:
        _teach(managed)
        payload = client.get("/api/backup/export?include=settings").get_json()
        assert "managed_load_learning" not in payload
    finally:
        store.close()
