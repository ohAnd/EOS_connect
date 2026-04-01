"""
ConfigWebModule — Facade for the web-based configuration system.

This is the SINGLE entry point that ``eos_connect.py`` interacts with.
It owns the schema, store, migration, merger, and API blueprint.

Usage in eos_connect.py (~10 lines total)::

    from config_web import ConfigWebModule

    config_web = ConfigWebModule(config_manager, app)
    config_web.start()
    config = config_web.get_config()   # same shape as config_manager.config
    # ... pass config to interfaces as before ...
    config_web.stop()                  # in shutdown
"""

import logging
import os

from .schema import ConfigSchema
from .store import ConfigStore
from .migration import migrate_yaml_to_store
from .merger import build_merged_config
from .api import config_bp, init_api

logger = logging.getLogger("__main__")


class ConfigWebModule:
    """
    Facade for the entire web-based configuration system.

    Args:
        config_manager: The existing ConfigManager instance.
        flask_app: The Flask app to register the API blueprint on.
        data_dir: Optional override for the data directory. If None, uses
                  ``config_manager.data_dir``.
    """

    def __init__(self, config_manager, flask_app, data_dir: str = None):
        self._config_manager = config_manager
        self._flask_app = flask_app
        self._data_dir = data_dir or config_manager.data_dir

        self._schema = ConfigSchema()
        self._store = None
        self._merged_config = None
        self._hot_reload_callbacks = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Initialize the config system: open DB, run migration if needed,
        build merged config, register API blueprint.
        """
        db_path = os.path.join(self._data_dir, "eos_connect.db")
        self._store = ConfigStore(db_path)
        self._store.open()

        # Migrate config.yaml to SQLite on first run
        migrate_yaml_to_store(
            self._config_manager.config,
            self._store,
            self._schema,
        )

        # Build the merged config dict
        self.rebuild_config()

        # Wire up and register the API blueprint
        init_api(self._store, self._schema, self)
        self._flask_app.register_blueprint(config_bp)

        logger.info(
            "[ConfigWeb] Started — %d fields in schema, DB at %s",
            len(self._schema.all_fields()),
            db_path,
        )

    def stop(self):
        """Close the database cleanly."""
        if self._store:
            self._store.close()
            logger.info("[ConfigWeb] Stopped")

    # ------------------------------------------------------------------
    # Config access
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        """
        Return the merged config dict — same shape as ``config_manager.config``.

        Interfaces can be passed this dict directly with zero changes.
        """
        if self._merged_config is None:
            self.rebuild_config()
        return self._merged_config

    def rebuild_config(self):
        """Rebuild the merged config from store + bootstrap."""
        self._merged_config = build_merged_config(
            self._config_manager.config,
            self._store,
            self._schema,
        )

    # ------------------------------------------------------------------
    # Hot-reload (Phase 4 — registration point)
    # ------------------------------------------------------------------

    def register_hot_reload_callback(self, callback):
        """
        Register a callback for config changes: ``callback(key, old_val, new_val)``.

        Used by HotReloadAdapter in Phase 4.
        """
        self._hot_reload_callbacks.append(callback)
        if self._store:
            self._store.register_change_callback(callback)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def blueprint(self):
        """The Flask Blueprint for config API routes."""
        return config_bp

    @property
    def schema(self) -> ConfigSchema:
        """The config schema registry."""
        return self._schema

    @property
    def store(self) -> ConfigStore:
        """The SQLite config store."""
        return self._store
