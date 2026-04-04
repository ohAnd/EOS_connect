"""
ConfigWebModule — Facade for the web-based configuration system.

This is the SINGLE entry point that ``eos_connect.py`` interacts with.
It owns the schema, store, migration, merger, and API blueprint.

Two-phase startup (preferred)::

    from config_web import ConfigWebModule

    # Phase 1 — DB only.  Call immediately after ConfigManager so that
    # config_manager.config is already correct when interfaces are constructed.
    config_web = ConfigWebModule(config_manager)
    config_web.start_db()
    # ... construct all interfaces using config_manager.config as usual ...

    # Phase 2 — register Flask API (needs app to exist first).
    app = Flask(__name__)
    config_web.start_api(app)
    config_web.stop()                  # in shutdown

Legacy single-phase (kept for compatibility)::

    config_web = ConfigWebModule(config_manager, app)
    config_web.start()
"""

import logging
import os

from .schema import ConfigSchema
from .store import ConfigStore
from .migration import migrate_yaml_to_store, migrate_ha_options_to_store
from .merger import build_merged_config
from .api import config_bp, init_api

logger = logging.getLogger("__main__")


def _deep_update(dst: dict, src: dict) -> None:
    """Recursively update *dst* in-place with values from *src*."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v


class ConfigWebModule:
    """
    Facade for the entire web-based configuration system.

    Args:
        config_manager: The existing ConfigManager instance.
        flask_app: Optional Flask app.  May be omitted when using the
                   two-phase ``start_db()`` / ``start_api(app)`` pattern and
                   supplied later via ``start_api()``.
        data_dir: Optional override for the data directory. If None, uses
                  ``config_manager.data_dir``.
    """

    def __init__(self, config_manager, flask_app=None, data_dir: str = None):
        self._config_manager = config_manager
        self._flask_app = flask_app  # may be set later via start_api()
        self._data_dir = data_dir or config_manager.data_dir

        self._schema = ConfigSchema()
        self._store = None
        self._merged_config = None
        self._hot_reload_callbacks = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_db(self):
        """
        Phase 1 — open the SQLite store, run migration, build merged config,
        and deep-update ``config_manager.config`` in-place.

        Call this immediately after ``ConfigManager`` is created, before any
        interfaces are constructed.  After this returns, ``config_manager.config``
        holds the authoritative DB values so every interface automatically
        receives the correct settings at construction time — no post-init
        sync is required.
        """
        db_path = os.path.join(self._data_dir, "eos_connect.db")
        self._store = ConfigStore(db_path)
        self._store.open()

        # In HA addon mode, try migrating legacy options.json first
        if self._config_manager.is_ha_addon:
            migrate_ha_options_to_store(self._store, self._schema)

        # Migrate config.yaml to SQLite on first run
        migrate_yaml_to_store(
            self._config_manager.config,
            self._store,
            self._schema,
        )

        # Build the merged config dict
        self.rebuild_config()

        # Clear restart-pending flags — server just (re)started
        self._store.set("_restart_pending", [])

        # Deep-update config_manager.config so interfaces constructed after
        # this call naturally receive DB-stored values.
        _deep_update(self._config_manager.config, self._merged_config)

        logger.info(
            "[ConfigWeb] DB ready — %d fields in schema, DB at %s",
            len(self._schema.all_fields()),
            db_path,
        )

    def start_api(self, flask_app):
        """
        Phase 2 — register the Flask REST API blueprint.

        Call this after the Flask ``app`` instance has been created.

        Args:
            flask_app: The Flask application instance.
        """
        self._flask_app = flask_app
        init_api(self._store, self._schema, self)
        self._flask_app.register_blueprint(config_bp)
        logger.info("[ConfigWeb] API registered on Flask app")

    def start(self):
        """
        Legacy single-phase start — requires ``flask_app`` passed to ``__init__``.

        Kept for backward compatibility.  Prefer the two-phase
        ``start_db()`` / ``start_api(app)`` pattern instead.
        """
        if self._flask_app is None:
            raise RuntimeError(
                "ConfigWebModule.start() requires flask_app to be passed to __init__(). "
                "Use start_db() + start_api(app) instead."
            )
        self.start_db()
        self.start_api(self._flask_app)

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
