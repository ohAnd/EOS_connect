"""
This module provides the ConfigManager class for managing configuration settings
of the application. The configuration settings are stored in a 'config.yaml' file.
"""

import json
import os
import logging
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

logger = logging.getLogger("__main__")
logger.info("[Config] loading module ")


class ConfigManager:
    """
    Manages the configuration settings for the application.

    This class handles loading, updating, and saving configuration settings from a 'config.yaml'
    file. If the configuration file does not exist, it creates one with default values and
    prompts the user to restart the server.
    """

    def __init__(self, given_dir):
        self.current_dir = given_dir
        self.config_file = os.path.join(self.current_dir, "config.yaml")
        self.yaml = YAML()
        self.yaml.default_flow_style = False
        self.yaml.indent(mapping=2, sequence=4, offset=2)
        self.yaml.preserve_quotes = True
        self.default_config = self.create_default_config()
        self.config = self.default_config.copy()
        self.load_config()

    @property
    def data_dir(self) -> str:
        """Resolve the persistent data directory for SQLite DB and other data files.

        Resolution order:
        1. HA addon environment -> /data/
        2. ``data_path`` in config.yaml -> custom path
        3. Default -> ./data/ relative to application directory
        """
        if self.is_ha_addon:
            return "/data"

        custom = self.config.get("data_path")
        if custom:
            return str(custom)

        return os.path.join(self.current_dir, "data")

    @property
    def is_ha_addon(self) -> bool:
        """Return True when running inside a Home Assistant add-on."""
        return (
            os.environ.get("HASSIO") is not None
            or os.environ.get("HASSIO_TOKEN") is not None
            or os.path.exists("/data/options.json")
        )

    # HA bootstrap key mapping: options.json key -> config dict key
    _HA_BOOTSTRAP_MAP = {
        "web_port": "eos_connect_web_port",
        "eos_connect_web_port": "eos_connect_web_port",
        "time_zone": "time_zone",
        "log_level": "log_level",
    }

    # Environment variable bootstrap mapping: ENV name -> config dict key
    _ENV_BOOTSTRAP_MAP = {
        "EOS_WEB_PORT": "eos_connect_web_port",
        "EOS_TIMEZONE": "time_zone",
        "EOS_LOG_LEVEL": "log_level",
    }

    def load_ha_bootstrap(self) -> dict:
        """Read bootstrap values from HA addon ``/data/options.json``.

        Returns:
            Dict of bootstrap key/value pairs that were applied, empty if not
            running in HA or if options.json is missing/invalid.
        """
        options_path = "/data/options.json"
        if not self.is_ha_addon or not os.path.exists(options_path):
            return {}

        try:
            with open(options_path, "r", encoding="utf-8") as f:
                options = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[Config] Failed to read %s: %s", options_path, exc)
            return {}

        applied = {}
        for opt_key, cfg_key in self._HA_BOOTSTRAP_MAP.items():
            if opt_key in options and options[opt_key] is not None:
                self.config[cfg_key] = options[opt_key]
                applied[cfg_key] = options[opt_key]

        if applied:
            logger.info("[Config] Applied HA addon bootstrap values: %s", list(applied.keys()))
        return applied

    def load_env_bootstrap(self) -> dict:
        """Read bootstrap values from environment variables.

        Supports ``EOS_WEB_PORT``, ``EOS_TIMEZONE``, and ``EOS_LOG_LEVEL``.
        These take precedence over config.yaml and options.json values.

        Returns:
            Dict of bootstrap key/value pairs that were applied.
        """
        applied = {}
        for env_key, cfg_key in self._ENV_BOOTSTRAP_MAP.items():
            value = os.environ.get(env_key)
            if value:
                # Coerce port to int
                if cfg_key == "eos_connect_web_port":
                    try:
                        value = int(value)
                    except ValueError:
                        logger.warning("[Config] Invalid %s value: %s", env_key, value)
                        continue
                self.config[cfg_key] = value
                applied[cfg_key] = value

        if applied:
            logger.info("[Config] Applied env bootstrap values: %s", list(applied.keys()))
        return applied

    def create_default_config(self):
        """
        Creates the default bootstrap configuration with comments.

        Only contains the three bootstrap keys managed via config.yaml.
        All other settings are managed through the web UI and stored in SQLite.
        """
        config = CommentedMap(
            {
                "eos_connect_web_port": 8081,
                "time_zone": "Europe/Berlin",
                "log_level": "info",
            }
        )
        config.yaml_add_eol_comment(
            "Port for EOS Connect web server - default: 8081",
            "eos_connect_web_port",
        )
        config.yaml_add_eol_comment(
            "Time zone for the application - default: Europe/Berlin",
            "time_zone",
        )
        config.yaml_add_eol_comment(
            "Log level: debug, info, warning, error - default: info",
            "log_level",
        )
        return config

    def load_config(self):
        """
        Reads the configuration from 'config.yaml' file located in the current directory.
        If the file exists, it loads the configuration values.
        If the file does not exist, defaults are used and the setup wizard will
        guide the user through initial configuration.

        When running as an HA addon, bootstrap values from ``/data/options.json``
        override the corresponding config.yaml values. Environment variables
        (``EOS_WEB_PORT``, ``EOS_TIMEZONE``, ``EOS_LOG_LEVEL``) take highest
        precedence.
        """
        # isfile, not exists: docker-compose used to bind-mount ./src/config.yaml, which
        # is gitignored, so a clean clone made Docker create a *directory* at this path.
        # exists() said True and open() then raised IsADirectoryError at import time,
        # before logging was useful. Nothing in a bootstrap file is worth a hard crash —
        # all three keys have defaults and the database is authoritative anyway.
        if os.path.isfile(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded = self.yaml.load(f)
                if loaded:
                    self.config.update(loaded)
            except (OSError, YAMLError) as exc:
                logger.warning(
                    "[Config] Could not read %s (%s) - using defaults, "
                    "settings from the database still apply",
                    self.config_file,
                    exc,
                )
        else:
            if os.path.exists(self.config_file):
                logger.warning(
                    "[Config] %s exists but is not a file - ignoring it. If this is a "
                    "directory, a docker volume mount created it; remove the "
                    "config.yaml bind mount and use EOS_WEB_PORT / EOS_TIMEZONE / "
                    "EOS_LOG_LEVEL instead.",
                    self.config_file,
                )
            if self.is_ha_addon:
                logger.info(
                    "[Config] No config.yaml found (HA addon mode) - using defaults"
                )
            else:
                logger.info(
                    "[Config] No config.yaml found - using defaults, "
                    "setup wizard will guide initial configuration"
                )

        # In HA addon mode, bootstrap values from options.json override config.yaml
        self.load_ha_bootstrap()
        # Environment variables take highest precedence
        self.load_env_bootstrap()

        # If config.yaml doesn't exist, create it with defaults
        # (for fresh install only, not for HA addon mode).
        # exists(), not isfile(), on purpose: when something non-file occupies the path
        # there is nothing useful to write and open(..., "w") would raise.
        if not os.path.exists(self.config_file) and not self.is_ha_addon:
            logger.info(
                "[Config] Creating new config.yaml with bootstrap defaults at %s",
                self.config_file,
            )
            self.write_config()

    def write_config(self):
        """
        Writes the configuration to 'config.yaml' file located in the current directory.

        Never fatal: config.yaml holds bootstrap values only, and a read-only bind mount
        or an unwritable path must not stop the application from starting.
        """
        logger.info("[Config] writing config file")
        try:
            with open(self.config_file, "w", encoding="utf-8") as config_file_handle:
                self.yaml.dump(self.config, config_file_handle)
        except OSError as exc:
            logger.warning(
                "[Config] Could not write %s (%s) - continuing with the values "
                "already loaded",
                self.config_file,
                exc,
            )
