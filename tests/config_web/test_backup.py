"""
Tests for the whole-install backup/restore endpoints.

The config-scoped ``/api/config`` pair is covered in ``test_api.py``; these pin the
behaviour that is specific to a full backup — the file's shape, dataset selection,
the dry-run preview, and graceful degradation when there is no yield store.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from src.config_web.api import config_bp, init_api
from src.config_web.backup import backup_bp, init_backup
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
from src.persistence import PvYieldStore

from .test_api import _FakeModule, _sample_config


def _make_client(tmp_path, with_pv_store):
    app = Flask(__name__)
    app.config["TESTING"] = True

    schema = ConfigSchema()
    store = ConfigStore(str(tmp_path / "backup.db"))
    store.open()
    migrate_yaml_to_store(_sample_config(), store, schema)

    module = _FakeModule(_sample_config(), store, schema)
    if with_pv_store:
        pv_store = PvYieldStore(store)
        pv_store.ensure_schema()
        module.pv_yield_store = pv_store

    init_api(store, schema, module)
    init_backup(store, schema, module)
    app.register_blueprint(config_bp)
    app.register_blueprint(backup_bp)

    client = app.test_client()
    client.store = store
    client.module = module
    return client, store


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    """A backup API with a working yield store."""
    client, store = _make_client(tmp_path, with_pv_store=True)
    try:
        yield client
    finally:
        store.close()


@pytest.fixture(name="storeless_client")
def storeless_client_fixture(tmp_path):
    """A backup API whose yield store failed to initialise."""
    client, store = _make_client(tmp_path, with_pv_store=False)
    try:
        yield client
    finally:
        store.close()


def _seed_hours(client, days_ago=1, hours=(8, 12)):
    """Record measured hours so there is history to back up."""
    base = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for hour in hours:
        when = base.replace(hour=hour, minute=0, second=0, microsecond=0)
        client.module.pv_yield_store.insert_hourly_record(
            timestamp=when.isoformat(),
            date=when.strftime("%Y-%m-%d"),
            hour=hour,
            timeframe_id=(hour // 6) + 1,
            real_counter_kwh=1042.5,
            real_delta_kwh=0.83,
            forecast_kwh=0.91,
            local_date=when.strftime("%Y-%m-%d"),
            local_hour=hour,
            local_offset_minutes=0,
        )


def _post(client, payload, query=""):
    return client.post(
        f"/api/backup/import{query}",
        data=json.dumps(payload),
        content_type="application/json",
    )


class TestBackupInfo:
    """What the panel shows before anything is downloaded."""

    def test_info_counts_both_datasets(self, client):
        _seed_hours(client)
        data = client.get("/api/backup/info").get_json()

        assert data["settings"]["count"] > 50
        assert data["pv_yield_history"]["count"] == 2
        assert data["pv_yield_history"]["available"] is True
        assert data["retention_days"] == 7

    def test_info_warns_the_file_holds_secrets(self, client):
        """A backup is never masked, so the UI has to say so."""
        assert client.get("/api/backup/info").get_json()["contains_secrets"] is True

    def test_info_degrades_without_a_yield_store(self, storeless_client):
        data = storeless_client.get("/api/backup/info").get_json()

        assert data["pv_yield_history"]["available"] is False
        assert data["pv_yield_history"]["count"] == 0
        assert data["settings"]["count"] > 50


class TestBackupExport:
    """The file's shape is a compatibility contract."""

    def test_export_is_flat_with_metadata(self, client):
        data = client.get("/api/backup/export").get_json()

        assert data["_format"] == "eos-connect-backup"
        assert data["_version"] == 1
        assert data["_exported_at"]
        # Settings sit at the top level, not inside an envelope — an older build
        # resolves top-level keys against its schema and would import nothing from a
        # nested file while reporting success.
        assert data["battery.capacity_wh"] == 10000
        assert "settings" not in data

    def test_export_carries_yield_history(self, client):
        _seed_hours(client)
        rows = client.get("/api/backup/export").get_json()["pv_yield_history"]

        assert len(rows) == 2
        assert rows[0]["real_delta_kwh"] == 0.83

    def test_export_omits_local_only_row_fields(self, client):
        _seed_hours(client)
        row = client.get("/api/backup/export").get_json()["pv_yield_history"][0]

        assert "id" not in row
        assert "created_at" not in row
        assert "real_counter_kwh" not in row

    def test_export_degrades_to_empty_history(self, storeless_client):
        data = storeless_client.get("/api/backup/export").get_json()

        assert data["pv_yield_history"] == []
        assert data["battery.capacity_wh"] == 10000

    def test_export_honours_dataset_selection(self, client):
        _seed_hours(client)

        only_history = client.get("/api/backup/export?include=pv_yield_history").get_json()
        assert "battery.capacity_wh" not in only_history
        assert len(only_history["pv_yield_history"]) == 2

        only_settings = client.get("/api/backup/export?include=settings").get_json()
        assert only_settings["battery.capacity_wh"] == 10000
        assert "pv_yield_history" not in only_settings

    def test_export_matches_the_config_endpoint_for_settings(self, client):
        """Both files must carry exactly the same settings."""
        backup = client.get("/api/backup/export").get_json()
        config = client.get("/api/config/export").get_json()

        assert {k: v for k, v in backup.items() if k in config} == config


class TestBackupRestore:
    """Restoring, and previewing a restore."""

    def test_round_trip_restores_history(self, client):
        _seed_hours(client)
        backup = client.get("/api/backup/export").get_json()
        client.store.execute("DELETE FROM pv_yield_history")
        assert client.module.pv_yield_store.get_all_history() == []

        result = _post(client, backup).get_json()

        assert result["pv_yield_history"]["imported"] == 2
        assert len(client.module.pv_yield_store.get_all_history()) == 2

    def test_restore_is_idempotent(self, client):
        _seed_hours(client)
        backup = client.get("/api/backup/export").get_json()
        client.store.execute("DELETE FROM pv_yield_history")

        _post(client, backup)
        _post(client, backup)

        assert len(client.module.pv_yield_store.get_all_history()) == 2

    def test_restore_defaults_to_replace(self, client):
        """The stored config must end up matching the file exactly."""
        backup = client.get("/api/backup/export").get_json()
        client.store.set("battery.capacity_wh", 99999)

        result = _post(client, backup).get_json()

        assert result["mode"] == "replace"
        assert client.store.get("battery.capacity_wh") == 10000

    def test_restore_removes_settings_absent_from_the_file(self, client):
        backup = client.get("/api/backup/export").get_json()
        del backup["eos.port"]

        result = _post(client, backup).get_json()

        assert "eos.port" in result["settings"]["removed"]
        assert not client.store.has_key("eos.port")

    def test_merge_mode_keeps_them(self, client):
        backup = client.get("/api/backup/export").get_json()
        del backup["eos.port"]

        result = _post(client, backup, "?mode=merge").get_json()

        assert result["settings"]["removed"] == []
        assert client.store.has_key("eos.port")

    def test_dry_run_writes_nothing(self, client):
        _seed_hours(client)
        backup = client.get("/api/backup/export").get_json()
        client.store.execute("DELETE FROM pv_yield_history")
        client.store.set("battery.capacity_wh", 99999)

        result = _post(client, backup, "?dry_run=1").get_json()

        assert result["dry_run"] is True
        assert result["pv_yield_history"]["valid"] == 2
        assert "imported" not in result["pv_yield_history"]
        # Nothing changed on disk.
        assert client.module.pv_yield_store.get_all_history() == []
        assert client.store.get("battery.capacity_wh") == 99999

    def test_dry_run_names_the_settings_it_would_remove(self, client):
        """The whole point of the preview: no surprises after confirming."""
        backup = client.get("/api/backup/export").get_json()
        del backup["eos.port"]

        result = _post(client, backup, "?dry_run=1").get_json()

        assert "eos.port" in result["settings"]["removed"]
        assert client.store.has_key("eos.port")

    def test_restore_reports_the_file_metadata_back(self, client):
        backup = client.get("/api/backup/export").get_json()
        result = _post(client, backup, "?dry_run=1").get_json()

        assert result["format"] == "eos-connect-backup"
        assert result["version"] == 1
        assert result["exported_at"] == backup["_exported_at"]

    def test_restore_honours_dataset_selection(self, client):
        _seed_hours(client)
        backup = client.get("/api/backup/export").get_json()
        client.store.execute("DELETE FROM pv_yield_history")
        client.store.set("battery.capacity_wh", 99999)

        _post(client, backup, "?include=pv_yield_history")

        assert len(client.module.pv_yield_store.get_all_history()) == 2
        assert client.store.get("battery.capacity_wh") == 99999

    def test_restore_degrades_without_a_yield_store(self, storeless_client):
        payload = {
            "_format": "eos-connect-backup",
            "battery.capacity_wh": 12000,
            "pv_yield_history": [{"timestamp": "2026-08-19T06:00:00+00:00"}],
        }
        result = _post(storeless_client, payload).get_json()

        assert result["pv_yield_history"]["available"] is False
        assert result["pv_yield_history"]["present_in_file"] == 1
        assert storeless_client.store.get("battery.capacity_wh") == 12000

    def test_restore_rejects_a_non_object_body(self, client):
        resp = client.post(
            "/api/backup/import", data=json.dumps([1, 2]), content_type="application/json"
        )
        assert resp.status_code == 400

    def test_legacy_config_file_still_restores(self, client):
        """A flat export from before this feature carries no marker at all."""
        legacy = client.get("/api/config/export").get_json()
        legacy["battery.capacity_wh"] = 13500
        client.store.set("battery.capacity_wh", 1)

        result = _post(client, legacy).get_json()

        assert result["format"] is None
        assert client.store.get("battery.capacity_wh") == 13500

    def test_seed_mode_shifts_a_stale_backup_into_the_window(self, client):
        _seed_hours(client, days_ago=25)
        backup = client.get("/api/backup/export").get_json()
        client.store.execute("DELETE FROM pv_yield_history")

        result = _post(client, backup, "?history_mode=seed").get_json()["pv_yield_history"]

        assert result["shift_days"] == 24
        assert result["imported"] == 2
        stored = client.module.pv_yield_store.get_all_history()
        assert {row["origin"] for row in stored} == {"seeded"}

    def test_preview_flags_a_stale_backup_for_seeding(self, client):
        _seed_hours(client, days_ago=25)
        backup = client.get("/api/backup/export").get_json()

        preview = _post(client, backup, "?dry_run=1").get_json()["pv_yield_history"]

        assert preview["age_days"] == 25
        assert preview["seed_recommended"] is True
        assert preview["retention_days"] == 7

    def test_preview_does_not_flag_a_fresh_backup(self, client):
        _seed_hours(client, days_ago=1)
        backup = client.get("/api/backup/export").get_json()

        preview = _post(client, backup, "?dry_run=1").get_json()["pv_yield_history"]

        assert preview["seed_recommended"] is False
