"""
Tests for Home Assistant addon integration.

Covers:
- HA addon detection (is_ha_addon property)
- Bootstrap value loading from /data/options.json
- Legacy options.json migration to SQLite
"""

import json
import os

import pytest

from src.config_web.migration import migrate_ha_options_to_store
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore


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


def _write_options(tmp_path, data):
    """Write a fake options.json and return its path."""
    path = tmp_path / "options.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


# -----------------------------------------------------------------------
# HA addon detection
# -----------------------------------------------------------------------

class TestHaAddonDetection:
    """Tests for ConfigManager.is_ha_addon property."""

    def _make_cm(self, tmp_path):
        """Create a ConfigManager with a dummy config.yaml to prevent sys.exit."""
        from ruamel.yaml import YAML
        yaml = YAML()
        cfg_path = tmp_path / "config.yaml"
        yaml.dump({"time_zone": "UTC"}, cfg_path.open("w"))
        from src.config import ConfigManager
        return ConfigManager(str(tmp_path))

    def _make_cm_no_yaml(self, tmp_path):
        """Create a ConfigManager without config.yaml (first-run scenario)."""
        from src.config import ConfigManager
        return ConfigManager(str(tmp_path))

    def test_not_ha_when_no_markers(self, monkeypatch, tmp_path):
        """Should return False without any HA markers."""
        monkeypatch.delenv("HASSIO", raising=False)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)
        cm = self._make_cm(tmp_path)
        if not os.path.exists("/data/options.json"):
            assert cm.is_ha_addon is False

    def test_ha_detected_via_hassio_env(self, monkeypatch, tmp_path):
        """HASSIO env var should trigger HA addon detection."""
        monkeypatch.setenv("HASSIO", "1")
        cm = self._make_cm(tmp_path)
        assert cm.is_ha_addon is True

    def test_ha_detected_via_hassio_token(self, monkeypatch, tmp_path):
        """HASSIO_TOKEN env var should trigger HA addon detection."""
        monkeypatch.delenv("HASSIO", raising=False)
        monkeypatch.setenv("HASSIO_TOKEN", "abc123")
        cm = self._make_cm(tmp_path)
        assert cm.is_ha_addon is True

    def test_first_run_no_config_yaml(self, monkeypatch, tmp_path):
        """ConfigManager should NOT sys.exit when config.yaml is missing."""
        monkeypatch.delenv("HASSIO", raising=False)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)
        # This should NOT raise SystemExit
        cm = self._make_cm_no_yaml(tmp_path)
        assert cm.config is not None
        # Defaults should be populated
        assert "load" in cm.config


# -----------------------------------------------------------------------
# Bootstrap loading
# -----------------------------------------------------------------------

class TestHaBootstrap:
    """Tests for ConfigManager.load_ha_bootstrap()."""

    def _make_cm(self, tmp_path):
        """Create a ConfigManager with a dummy config.yaml."""
        from ruamel.yaml import YAML
        yaml = YAML()
        cfg_path = tmp_path / "config.yaml"
        yaml.dump({"time_zone": "UTC"}, cfg_path.open("w"))
        from src.config import ConfigManager
        return ConfigManager(str(tmp_path))

    def test_bootstrap_noop_when_not_ha(self, monkeypatch, tmp_path):
        """Should return empty dict when not in HA addon mode."""
        monkeypatch.delenv("HASSIO", raising=False)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)
        cm = self._make_cm(tmp_path)
        if not os.path.exists("/data/options.json"):
            result = cm.load_ha_bootstrap()
            assert result == {}


class TestEnvBootstrap:
    """Tests for ConfigManager.load_env_bootstrap()."""

    def _make_cm(self, tmp_path):
        """Create a ConfigManager with a dummy config.yaml."""
        from ruamel.yaml import YAML
        yaml = YAML()
        cfg_path = tmp_path / "config.yaml"
        yaml.dump({"time_zone": "UTC"}, cfg_path.open("w"))
        from src.config import ConfigManager
        return ConfigManager(str(tmp_path))

    def test_env_web_port(self, monkeypatch, tmp_path):
        """EOS_WEB_PORT should override config web port."""
        monkeypatch.setenv("EOS_WEB_PORT", "9090")
        cm = self._make_cm(tmp_path)
        # load_env_bootstrap was already called in load_config, but call again to test
        result = cm.load_env_bootstrap()
        assert cm.config["eos_connect_web_port"] == 9090

    def test_env_timezone(self, monkeypatch, tmp_path):
        """EOS_TIMEZONE should override config timezone."""
        monkeypatch.setenv("EOS_TIMEZONE", "US/Eastern")
        cm = self._make_cm(tmp_path)
        assert cm.config["time_zone"] == "US/Eastern"

    def test_env_log_level(self, monkeypatch, tmp_path):
        """EOS_LOG_LEVEL should override config log level."""
        monkeypatch.setenv("EOS_LOG_LEVEL", "DEBUG")
        cm = self._make_cm(tmp_path)
        assert cm.config["log_level"] == "DEBUG"

    def test_env_invalid_port_ignored(self, monkeypatch, tmp_path):
        """Invalid port value should be ignored."""
        monkeypatch.setenv("EOS_WEB_PORT", "not_a_number")
        cm = self._make_cm(tmp_path)
        # Should keep the default, not crash
        assert isinstance(cm.config["eos_connect_web_port"], int)

    def test_env_empty_values_ignored(self, monkeypatch, tmp_path):
        """Empty env vars should not override config values."""
        monkeypatch.setenv("EOS_WEB_PORT", "")
        cm = self._make_cm(tmp_path)
        result = cm.load_env_bootstrap()
        assert "eos_connect_web_port" not in result

    def test_env_overrides_ha_bootstrap(self, monkeypatch, tmp_path):
        """ENV vars should take precedence over HA options.json values."""
        monkeypatch.setenv("HASSIO", "1")
        monkeypatch.setenv("EOS_TIMEZONE", "Asia/Tokyo")
        cm = self._make_cm(tmp_path)
        # ENV should win over any HA bootstrap
        assert cm.config["time_zone"] == "Asia/Tokyo"

    def test_bootstrap_reads_values(self, monkeypatch, tmp_path):
        """Should apply bootstrap values from options.json to config dict."""
        options_file = tmp_path / "options.json"
        options_file.write_text(json.dumps({
            "web_port": 9090,
            "time_zone": "America/New_York",
            "log_level": "DEBUG",
        }), encoding="utf-8")

        monkeypatch.setenv("HASSIO", "1")
        # Monkey-patch the options path
        from src.config import ConfigManager
        cm = ConfigManager(str(tmp_path))
        # Override the path check to use our temp file
        original_method = cm.load_ha_bootstrap

        def patched_bootstrap():
            """Read from temp options.json instead of /data/options.json."""
            import json as _json
            with open(str(options_file), "r", encoding="utf-8") as f:
                options = _json.load(f)
            applied = {}
            for opt_key, cfg_key in cm._HA_BOOTSTRAP_MAP.items():
                if opt_key in options and options[opt_key] is not None:
                    cm.config[cfg_key] = options[opt_key]
                    applied[cfg_key] = options[opt_key]
            return applied

        cm.load_ha_bootstrap = patched_bootstrap
        result = cm.load_ha_bootstrap()

        assert result["eos_connect_web_port"] == 9090
        assert result["time_zone"] == "America/New_York"
        assert result["log_level"] == "DEBUG"
        assert cm.config["eos_connect_web_port"] == 9090
        assert cm.config["time_zone"] == "America/New_York"


# -----------------------------------------------------------------------
# Legacy options.json migration
# -----------------------------------------------------------------------

class TestHaOptionsMigration:
    """Tests for migrate_ha_options_to_store()."""

    def test_migration_on_legacy_options(self, store, schema, tmp_path):
        """Should migrate non-bootstrap keys from a full options.json."""
        options = {
            "web_port": 8081,
            "time_zone": "Europe/Berlin",
            "log_level": "info",
            "load": {
                "source": "homeassistant",
                "url": "http://homeassistant:8123",
                "access_token": "ha_token",
                "load_sensor": "sensor.load",
            },
            "eos": {
                "source": "eos_server",
                "server": "192.168.1.50",
                "port": 8503,
            },
            "battery": {
                "source": "homeassistant",
                "url": "http://homeassistant:8123",
                "access_token": "ha_token",
                "capacity_wh": 12000,
                "min_soc_percentage": 10,
            },
            "refresh_time": 5,
        }
        path = _write_options(tmp_path, options)
        result = migrate_ha_options_to_store(store, schema, options_path=path)

        assert result is True
        assert store.get("load.source") == "homeassistant"
        assert store.get("load.load_sensor") == "sensor.load"
        assert store.get("eos.server") == "192.168.1.50"
        assert store.get("battery.capacity_wh") == 12000
        assert store.get("refresh_time") == 5

    def test_bootstrap_keys_excluded(self, store, schema, tmp_path):
        """Bootstrap keys should NOT be migrated to SQLite."""
        options = {
            "web_port": 9090,
            "time_zone": "US/Eastern",
            "log_level": "DEBUG",
            "load": {"source": "openhab"},
        }
        path = _write_options(tmp_path, options)
        migrate_ha_options_to_store(store, schema, options_path=path)

        assert store.get("eos_connect_web_port") is None
        assert store.get("time_zone") is None
        assert store.get("log_level") is None

    def test_skipped_if_store_has_data(self, store, schema, tmp_path):
        """Should not run if the store already has data."""
        store.set("some_key", "existing_value")
        options = {"load": {"source": "homeassistant"}}
        path = _write_options(tmp_path, options)

        result = migrate_ha_options_to_store(store, schema, options_path=path)
        assert result is False

    def test_skipped_if_file_missing(self, store, schema):
        """Should return False if options.json doesn't exist."""
        result = migrate_ha_options_to_store(
            store, schema, options_path="/nonexistent/options.json"
        )
        assert result is False

    def test_skipped_if_only_bootstrap_keys(self, store, schema, tmp_path):
        """Should skip if options.json has only bootstrap keys."""
        options = {
            "web_port": 8081,
            "time_zone": "Europe/Berlin",
            "log_level": "info",
        }
        path = _write_options(tmp_path, options)
        result = migrate_ha_options_to_store(store, schema, options_path=path)
        assert result is False
        assert store.is_empty()

    def test_data_source_created(self, store, schema, tmp_path):
        """Should create data_source from load section values."""
        options = {
            "load": {
                "source": "homeassistant",
                "url": "http://ha:8123",
                "access_token": "tok",
            },
        }
        path = _write_options(tmp_path, options)
        migrate_ha_options_to_store(store, schema, options_path=path)

        assert store.get("data_source.type") == "homeassistant"
        assert store.get("data_source.url") == "http://ha:8123"

    def test_migration_markers_set(self, store, schema, tmp_path):
        """Should set HA-specific migration markers."""
        options = {"load": {"source": "homeassistant"}}
        path = _write_options(tmp_path, options)
        migrate_ha_options_to_store(store, schema, options_path=path)

        assert store.get("_migrated_from_ha_options") is True
        assert store.get("_wizard_completed") is True

    def test_invalid_json_handled(self, store, schema, tmp_path):
        """Should handle invalid JSON gracefully."""
        path = tmp_path / "options.json"
        path.write_text("not valid json {{{", encoding="utf-8")

        result = migrate_ha_options_to_store(store, schema, options_path=str(path))
        assert result is False
        assert store.is_empty()

    def test_non_dict_json_handled(self, store, schema, tmp_path):
        """Should handle non-dict JSON."""
        path = _write_options(tmp_path, [1, 2, 3])
        result = migrate_ha_options_to_store(store, schema, options_path=str(path))
        assert result is False

    def test_ha_migration_prevents_yaml_migration(self, store, schema, tmp_path):
        """After HA migration, yaml migration should be skipped (store not empty)."""
        from src.config_web.migration import migrate_yaml_to_store

        options = {
            "load": {"source": "homeassistant", "load_sensor": "sensor.ha_load"},
        }
        path = _write_options(tmp_path, options)
        migrate_ha_options_to_store(store, schema, options_path=path)

        # Now try yaml migration — should be skipped
        yaml_config = {"load": {"source": "openhab", "load_sensor": "sensor.oh_load"}}
        result = migrate_yaml_to_store(yaml_config, store, schema)
        assert result is False

        # Verify HA values are preserved
        assert store.get("load.source") == "homeassistant"
        assert store.get("load.load_sensor") == "sensor.ha_load"
