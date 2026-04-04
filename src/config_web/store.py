"""
SQLite Config Store — Persistent key/value storage for configuration settings.

Uses SQLite with WAL mode for concurrent reads. All values are stored as
JSON-encoded strings. Thread-safe via threading.Lock.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("__main__")

# Current database schema version — bump when table structure changes
_SCHEMA_VERSION = 1


class ConfigStore:
    """
    SQLite-backed key/value store for configuration settings.

    Args:
        db_path: Absolute path to the SQLite database file.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._change_callbacks: list = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self):
        """Open (or create) the database and ensure tables exist."""
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_tables()
        logger.info("[ConfigStore] Opened database: %s", self._db_path)

    def close(self):
        """Flush and close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("[ConfigStore] Closed database")

    def _init_tables(self):
        """Create tables if they don't exist and run any migrations."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """CREATE TABLE IF NOT EXISTS schema_version (
                       version INTEGER NOT NULL
                   )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                       key TEXT PRIMARY KEY,
                       value TEXT NOT NULL,
                       updated_at TEXT NOT NULL
                   )"""
            )
            # Seed schema version if empty
            row = cur.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieve a config value by key.

        Returns the deserialized value, or *default* if the key doesn't exist.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def set(self, key: str, value: Any) -> None:  # noqa: A003
        """
        Store a config value. Fires registered change callbacks if the value changed.
        """
        old_value = self.get(key)
        now = datetime.now(timezone.utc).isoformat()
        json_value = json.dumps(value)

        with self._lock:
            self._conn.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                  updated_at = excluded.updated_at""",
                (key, json_value, now),
            )
            self._conn.commit()

        if old_value != value:
            self._fire_callbacks(key, old_value, value)

    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if something was deleted."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._conn.commit()
            return cur.rowcount > 0

    def get_all(self) -> dict[str, Any]:
        """Return all stored settings as {key: value}."""
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {k: json.loads(v) for k, v in rows}

    def has_key(self, key: str) -> bool:
        """Check if a key exists in the store."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row is not None

    def is_empty(self) -> bool:
        """Check if the settings table has no entries."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM settings").fetchone()
        return row[0] == 0

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def import_dict(self, data: dict[str, Any]) -> int:
        """
        Bulk import flat key/value pairs. Returns count of keys imported.
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with self._lock:
            for key, value in data.items():
                json_value = json.dumps(value)
                self._conn.execute(
                    """INSERT INTO settings (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                      updated_at = excluded.updated_at""",
                    (key, json_value, now),
                )
                count += 1
            self._conn.commit()
        logger.info("[ConfigStore] Imported %d settings", count)
        return count

    def set_batch(self, items: dict[str, Any]) -> int:
        """
        Atomically store multiple key/value pairs in a single transaction.

        Unlike repeated ``set()`` calls, this commits once at the end so the
        operation is all-or-nothing.  Change callbacks are NOT fired (intended
        for migration/import, not live editing).

        Args:
            items: Flat dict of key/value pairs to store.

        Returns:
            Number of keys written.
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with self._lock:
            for key, value in items.items():
                json_value = json.dumps(value)
                self._conn.execute(
                    """INSERT INTO settings (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                      updated_at = excluded.updated_at""",
                    (key, json_value, now),
                )
                count += 1
            self._conn.commit()
        return count

    def export_dict(self) -> dict[str, Any]:
        """Alias for get_all() — export all settings as flat dict."""
        return self.get_all()

    # ------------------------------------------------------------------
    # Change notification
    # ------------------------------------------------------------------

    def register_change_callback(self, callback):
        """
        Register a callback: ``callback(key, old_value, new_value)``.

        Called whenever ``set()`` changes a value.
        """
        self._change_callbacks.append(callback)

    def _fire_callbacks(self, key: str, old_value: Any, new_value: Any):
        """Notify all registered callbacks of a value change."""
        for cb in self._change_callbacks:
            try:
                cb(key, old_value, new_value)
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "[ConfigStore] Error in change callback for key '%s'", key
                )
