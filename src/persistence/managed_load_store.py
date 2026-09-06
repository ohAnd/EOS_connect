"""
Recorded samples and learned coefficients for managed loads.

Two things have to survive a restart for a managed load to be useful:

- the **samples** it learns from. A calibrator that starts from an empty history relearns
  the same pool every time the container is updated, and on a weekly update cadence it
  never settles.
- the **estimate itself**, so a fresh start begins from what was learned rather than from
  the user's original guess while it rebuilds the sample window.

Both live in the shared SQLite database next to the PV yield history, and this module is
deliberately outside `config_web` for the same reason `PvYieldStore` is: the interface and
`loads` layers must be able to use it without pulling in Flask.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("__main__")

_SAMPLES_TABLE = """
    CREATE TABLE IF NOT EXISTS managed_load_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        load_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        payload TEXT NOT NULL
    )
"""

# The payload is JSON rather than typed columns on purpose: the model owns its sample
# shape, and a space-heating model added later should not need a schema migration to
# record a flow temperature. The columns that exist are the ones this store queries on.
_MODEL_TABLE = """
    CREATE TABLE IF NOT EXISTS managed_load_model (
        load_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (load_id, key)
    )
"""

_SAMPLE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_managed_load_samples_lookup "
    "ON managed_load_samples (load_id, timestamp)"
)

# How long recorded samples are kept. Two weeks is enough for the loss coefficient to
# settle and for the COP fit to see a range of ambient temperatures, and short enough
# that the table stays small on a Raspberry Pi.
DEFAULT_RETENTION_DAYS = 14

# One row per instance per cycle at the default five-minute cadence is ~4000 rows a
# fortnight. This bound only exists to stop a misconfigured cycle time filling the disk.
MAX_SAMPLES_PER_LOAD = 20000

_STATE_KEY = "calibration"


def _iso(moment):
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _parse(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class ManagedLoadStore:
    """Sample history and persisted model state, sharing the config database."""

    def __init__(self, store):
        self._store = store

    def ensure_schema(self):
        """Create the tables. Safe to call on every start."""
        self._store.execute(_SAMPLES_TABLE)
        self._store.execute(_MODEL_TABLE)
        self._store.execute(_SAMPLE_INDEX)

    # -- samples ----------------------------------------------------------------------

    def record_sample(self, load_id, sample):
        """
        Append one observation.

        The timestamp is stored as a column so the retention sweep and the window query
        do not have to parse JSON; everything else goes in the payload untouched.
        """
        moment = sample.get("timestamp")
        if not isinstance(moment, datetime):
            return False

        payload = {
            key: value for key, value in sample.items() if key != "timestamp"
        }
        self._store.execute(
            "INSERT INTO managed_load_samples (load_id, timestamp, payload) "
            "VALUES (?, ?, ?)",
            (str(load_id), _iso(moment), json.dumps(payload)),
        )
        return True

    def load_samples(self, load_id, days=DEFAULT_RETENTION_DAYS):
        """
        Recorded samples for one load, oldest first, ready to feed a calibrator.

        Rows whose payload cannot be read are skipped rather than fatal: a sample written
        by an older version is not worth losing the other 4000 over.
        """
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=max(1, int(days))))
        rows = self._store.query(
            "SELECT timestamp, payload FROM managed_load_samples "
            "WHERE load_id = ? AND timestamp >= ? ORDER BY timestamp ASC LIMIT ?",
            (str(load_id), cutoff, MAX_SAMPLES_PER_LOAD),
        )

        samples = []
        for timestamp, payload in rows:
            moment = _parse(timestamp)
            if moment is None:
                continue
            try:
                data = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            data["timestamp"] = moment
            samples.append(data)
        return samples

    def purge_old_samples(self, days=DEFAULT_RETENTION_DAYS):
        """Drop everything outside the retention window. Returns rows removed."""
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=max(1, int(days))))
        cursor = self._store.execute(
            "DELETE FROM managed_load_samples WHERE timestamp < ?", (cutoff,)
        )
        removed = cursor.rowcount if cursor is not None else 0
        if removed > 0:
            logger.debug("[LOADS-STORE] purged %d sample(s) older than %d days",
                         removed, days)
        return removed

    def forget(self, load_id):
        """Remove everything for one load - used when an instance is deleted."""
        self._store.execute(
            "DELETE FROM managed_load_samples WHERE load_id = ?", (str(load_id),)
        )
        self._store.execute(
            "DELETE FROM managed_load_model WHERE load_id = ?", (str(load_id),)
        )

    def sample_count(self, load_id):
        """How many samples are stored for one load, retention window aside."""
        rows = self._store.query(
            "SELECT COUNT(*) FROM managed_load_samples WHERE load_id = ?",
            (str(load_id),),
        )
        return rows[0][0] if rows else 0

    # -- model state -------------------------------------------------------------------

    def save_model_state(self, load_id, state):
        """Persist a calibrator's estimate. Silently ignores anything unserialisable."""
        if not isinstance(state, dict):
            return False
        try:
            encoded = json.dumps(state)
        except (TypeError, ValueError):
            logger.debug("[LOADS-STORE] model state for '%s' is not serialisable", load_id)
            return False

        self._store.execute(
            "INSERT INTO managed_load_model (load_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(load_id, key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (str(load_id), _STATE_KEY, encoded, _iso(datetime.now(timezone.utc))),
        )
        return True

    def load_model_state(self, load_id):
        """The persisted estimate for one load, or None."""
        rows = self._store.query(
            "SELECT value FROM managed_load_model WHERE load_id = ? AND key = ?",
            (str(load_id), _STATE_KEY),
        )
        if not rows:
            return None
        try:
            state = json.loads(rows[0][0])
        except (TypeError, ValueError):
            return None
        return state if isinstance(state, dict) else None
