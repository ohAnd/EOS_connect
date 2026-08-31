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
        """ConfigManager should NOT sys.exit when config.yaml is missing.
        
        On fresh install, config dict contains only bootstrap keys (3 total).
        All other settings are managed via SQLite and web UI.
        """
        monkeypatch.delenv("HASSIO", raising=False)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)
        # This should NOT raise SystemExit
        cm = self._make_cm_no_yaml(tmp_path)
        assert cm.config is not None
        # Fresh install: only 3 bootstrap keys
        assert "eos_connect_web_port" in cm.config
        assert "time_zone" in cm.config
        assert "log_level" in cm.config
        # All other settings (load, battery, etc.) are in SQLite, not config dict


# -----------------------------------------------------------------------
# Unreadable / non-file config.yaml
# -----------------------------------------------------------------------

class TestUnusableConfigYaml:
    """A bootstrap file that cannot be read must never stop startup.

    docker-compose bind-mounted ./src/config.yaml, which is gitignored, so a clean
    clone made Docker create a *directory* at that path. os.path.exists() said True
    and the open() that followed raised IsADirectoryError at import time.
    """

    def _make_cm(self, tmp_path):
        from src.config import ConfigManager
        return ConfigManager(str(tmp_path))

    def _clear_env(self, monkeypatch):
        for var in ("HASSIO", "HASSIO_TOKEN", "EOS_WEB_PORT", "EOS_TIMEZONE",
                    "EOS_LOG_LEVEL"):
            monkeypatch.delenv(var, raising=False)

    def test_directory_at_config_path_does_not_crash(self, monkeypatch, tmp_path):
        """A directory named config.yaml falls back to defaults instead of raising."""
        self._clear_env(monkeypatch)
        (tmp_path / "config.yaml").mkdir()
        cm = self._make_cm(tmp_path)
        assert cm.config["eos_connect_web_port"] == 8081
        assert cm.config["time_zone"] == "Europe/Berlin"
        assert cm.config["log_level"] == "info"

    def test_directory_at_config_path_is_not_overwritten(self, monkeypatch, tmp_path):
        """The create-if-missing branch must not try to write over the directory."""
        self._clear_env(monkeypatch)
        cfg_dir = tmp_path / "config.yaml"
        cfg_dir.mkdir()
        self._make_cm(tmp_path)
        assert cfg_dir.is_dir(), "config.yaml must still be the untouched directory"

    def test_malformed_yaml_falls_back_to_defaults(self, monkeypatch, tmp_path):
        """Unparseable YAML is a warning, not a crash."""
        self._clear_env(monkeypatch)
        (tmp_path / "config.yaml").write_text(
            "eos_connect_web_port: [1, 2\ntime_zone: ]]]\n", encoding="utf-8"
        )
        cm = self._make_cm(tmp_path)
        assert cm.config["time_zone"] == "Europe/Berlin"

    def test_empty_yaml_falls_back_to_defaults(self, monkeypatch, tmp_path):
        """An empty file loads as None — that must not blow up config.update()."""
        self._clear_env(monkeypatch)
        (tmp_path / "config.yaml").write_text("", encoding="utf-8")
        cm = self._make_cm(tmp_path)
        assert cm.config["eos_connect_web_port"] == 8081

    def test_valid_yaml_still_wins(self, monkeypatch, tmp_path):
        """The guard must not break the normal path."""
        self._clear_env(monkeypatch)
        (tmp_path / "config.yaml").write_text(
            "eos_connect_web_port: 9099\ntime_zone: UTC\n", encoding="utf-8"
        )
        cm = self._make_cm(tmp_path)
        assert cm.config["eos_connect_web_port"] == 9099
        assert cm.config["time_zone"] == "UTC"

    def test_unwritable_dir_does_not_crash(self, monkeypatch, tmp_path):
        """A read-only mount cannot be written to; that must be survivable."""
        self._clear_env(monkeypatch)
        target = tmp_path / "ro"
        target.mkdir()
        os.chmod(target, 0o500)
        try:
            cm = self._make_cm(target)
            assert cm.config["eos_connect_web_port"] == 8081
        finally:
            os.chmod(target, 0o700)


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
# EOS_DATA_PATH
# -----------------------------------------------------------------------

class TestEnvDataPath:
    """EOS_DATA_PATH moves the SQLite database off the default ./data.

    Docker users who cannot bind-mount /app/data need a way to point the
    database at a path they already persist (see #287).
    """

    def _make_cm(self, tmp_path, yaml_extra=None):
        """ConfigManager with a config.yaml, optionally carrying extra keys."""
        from ruamel.yaml import YAML
        data = {"time_zone": "UTC"}
        if yaml_extra:
            data.update(yaml_extra)
        yaml = YAML()
        yaml.dump(data, (tmp_path / "config.yaml").open("w"))
        from src.config import ConfigManager
        return ConfigManager(str(tmp_path))

    def _not_ha(self, monkeypatch):
        monkeypatch.delenv("HASSIO", raising=False)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)

    def test_env_data_path_sets_data_dir(self, monkeypatch, tmp_path):
        """The variable should reach data_dir, not just the config dict."""
        self._not_ha(monkeypatch)
        monkeypatch.setenv("EOS_DATA_PATH", "/mnt/eos_data")
        cm = self._make_cm(tmp_path)
        assert cm.config["data_path"] == "/mnt/eos_data"
        if not os.path.exists("/data/options.json"):
            assert cm.data_dir == "/mnt/eos_data"

    def test_env_data_path_is_not_int_coerced(self, monkeypatch, tmp_path):
        """Only the port key is coerced; a numeric-looking path stays a string."""
        self._not_ha(monkeypatch)
        monkeypatch.setenv("EOS_DATA_PATH", "/mnt/12345")
        cm = self._make_cm(tmp_path)
        assert cm.config["data_path"] == "/mnt/12345"
        assert isinstance(cm.config["data_path"], str)

    def test_env_data_path_is_stripped(self, monkeypatch, tmp_path):
        """Stray whitespace from a compose file must not become part of the path."""
        self._not_ha(monkeypatch)
        monkeypatch.setenv("EOS_DATA_PATH", "  /mnt/eos_data  ")
        cm = self._make_cm(tmp_path)
        assert cm.config["data_path"] == "/mnt/eos_data"

    def test_env_data_path_empty_is_ignored(self, monkeypatch, tmp_path):
        """The Dockerfile ships ENV EOS_DATA_PATH="" — it must override nothing."""
        self._not_ha(monkeypatch)
        monkeypatch.setenv("EOS_DATA_PATH", "")
        cm = self._make_cm(tmp_path)
        assert "data_path" not in cm.load_env_bootstrap()
        if not os.path.exists("/data/options.json"):
            assert cm.data_dir == os.path.join(str(tmp_path), "data")

    def test_env_data_path_whitespace_only_is_ignored(self, monkeypatch, tmp_path):
        """Whitespace stripped to nothing must not set an empty data_path."""
        self._not_ha(monkeypatch)
        monkeypatch.setenv("EOS_DATA_PATH", "   ")
        cm = self._make_cm(tmp_path)
        assert "data_path" not in cm.load_env_bootstrap()

    def test_env_data_path_overrides_yaml(self, monkeypatch, tmp_path):
        """Environment beats config.yaml, as documented for every bootstrap key."""
        self._not_ha(monkeypatch)
        monkeypatch.setenv("EOS_DATA_PATH", "/mnt/from_env")
        cm = self._make_cm(tmp_path, {"data_path": "/mnt/from_yaml"})
        if not os.path.exists("/data/options.json"):
            assert cm.data_dir == "/mnt/from_env"

    def test_yaml_data_path_used_when_env_absent(self, monkeypatch, tmp_path):
        """The pre-existing config.yaml route must keep working."""
        self._not_ha(monkeypatch)
        monkeypatch.delenv("EOS_DATA_PATH", raising=False)
        cm = self._make_cm(tmp_path, {"data_path": "/mnt/from_yaml"})
        if not os.path.exists("/data/options.json"):
            assert cm.data_dir == "/mnt/from_yaml"

    def test_ha_addon_ignores_env_data_path(self, monkeypatch, tmp_path):
        """Supervisor owns /data; nothing may redirect the add-on away from it."""
        monkeypatch.setenv("HASSIO", "1")
        monkeypatch.setenv("EOS_DATA_PATH", "/mnt/eos_data")
        cm = self._make_cm(tmp_path)
        assert cm.data_dir == "/data"

    def test_env_data_path_not_written_to_fresh_yaml(self, monkeypatch, tmp_path):
        """A generated config.yaml must not bake in an env-supplied data_path.

        Otherwise the path outlives the variable: drop EOS_DATA_PATH and the app
        keeps writing to a directory that is no longer mounted.
        """
        self._not_ha(monkeypatch)
        monkeypatch.setenv("EOS_DATA_PATH", "/mnt/eos_data")
        from src.config import ConfigManager
        cm = ConfigManager(str(tmp_path))  # no config.yaml -> write_config() runs
        written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "data_path" not in written
        # still honoured for this process
        if not os.path.exists("/data/options.json"):
            assert cm.data_dir == "/mnt/eos_data"

    def test_yaml_data_path_survives_a_rewrite(self, monkeypatch, tmp_path):
        """A hand-authored data_path is a real setting and must be preserved."""
        self._not_ha(monkeypatch)
        monkeypatch.delenv("EOS_DATA_PATH", raising=False)
        cm = self._make_cm(tmp_path, {"data_path": "/mnt/from_yaml"})
        cm.write_config()
        written = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "/mnt/from_yaml" in written

    def test_relative_env_data_path_still_applies(self, monkeypatch, tmp_path):
        """A relative path is warned about but honoured — not silently dropped."""
        self._not_ha(monkeypatch)
        monkeypatch.setenv("EOS_DATA_PATH", "relative/data")
        cm = self._make_cm(tmp_path)
        assert cm.config["data_path"] == "relative/data"


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
