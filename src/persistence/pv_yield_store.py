"""
PvYieldStore - hourly PV yield history for the PV autoscaler.

Stores one row per recorded hour (measured counter delta plus the forecast that had
been predicted for that hour) in the shared SQLite database, and serves the rolling
window the autoscaler derives its scale factors from.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger("__main__")

# Columns selected by every read, in the tuple order the callers unpack.
_COLUMNS = (
    "id, timestamp, date, hour, timeframe_id, real_counter_kwh, real_delta_kwh, "
    "forecast_kwh, created_at, local_date, local_hour, local_offset_minutes"
)

# Timestamps are written as UTC ISO-8601 ("2026-08-22T08:00:00+00:00"), which does not
# compare lexicographically against SQLite's datetime() output ("2026-08-22 08:00:00"):
# the 'T' sorts above the space, so a plain `timestamp < datetime('now', ?)` never
# matches rows on the boundary date. Compare the leading "YYYY-MM-DDTHH:MM:SS" of both
# sides instead, which is well-ordered and offset-free.
_TS_PREFIX = "substr(timestamp, 1, 19)"
_CUTOFF = "strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)"


class PvYieldStore:
    """Read/write access to the `pv_yield_history` table."""

    def __init__(self, store):
        self._store = store

    def _columns(self) -> set:
        return {row[1] for row in self._store.query("PRAGMA table_info(pv_yield_history)")}

    def ensure_schema(self) -> None:
        """Create the table and indexes, and migrate databases from earlier versions."""
        self._store.execute(
            """
            CREATE TABLE IF NOT EXISTS pv_yield_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                timeframe_id INTEGER NOT NULL,
                real_counter_kwh REAL,
                real_delta_kwh REAL,
                forecast_kwh REAL,
                created_at TEXT NOT NULL,
                local_date TEXT,
                local_hour INTEGER,
                local_offset_minutes INTEGER
            )
            """
        )

        columns = self._columns()

        # Migrate the old Wh column once. New records use kWh consistently.
        if "forecast_wh" in columns and "forecast_kwh" not in columns:
            self._store.execute(
                "ALTER TABLE pv_yield_history RENAME COLUMN forecast_wh TO forecast_kwh"
            )
            self._store.execute(
                "UPDATE pv_yield_history SET forecast_kwh = forecast_kwh / 1000.0 "
                "WHERE forecast_kwh IS NOT NULL"
            )
            columns = self._columns()
        elif "forecast_kwh" not in columns:
            self._store.execute("ALTER TABLE pv_yield_history ADD COLUMN forecast_kwh REAL")
            columns = self._columns()

        # Add the timezone-aware columns when upgrading. Checked against PRAGMA rather
        # than attempted-and-swallowed, so a real failure (locked database) surfaces.
        for name, coltype in (
            ("local_date", "TEXT"),
            ("local_hour", "INTEGER"),
            ("local_offset_minutes", "INTEGER"),
        ):
            if name not in columns:
                self._store.execute(
                    f"ALTER TABLE pv_yield_history ADD COLUMN {name} {coltype}"
                )

        # Back-fill rows written before those columns existed. Without this they read as
        # NULL and every consumer that groups by local_date drops them into a phantom
        # "no date" bucket. The legacy date/hour columns were always written with local
        # values despite their name, so they are the correct source.
        self._store.execute(
            "UPDATE pv_yield_history SET local_date = date WHERE local_date IS NULL"
        )
        self._store.execute(
            "UPDATE pv_yield_history SET local_hour = hour WHERE local_hour IS NULL"
        )

        # indices
        self._store.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_yield_date ON pv_yield_history(date)"
        )
        self._store.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_yield_local_date ON pv_yield_history(local_date)"
        )
        self._ensure_timestamp_unique()

    def _ensure_timestamp_unique(self) -> None:
        """
        Enforce one row per recorded hour at the schema level.

        `insert_hourly_record` de-duplicates with a SELECT-then-UPDATE, which is not
        atomic; this index is the backstop that keeps a duplicate from becoming
        permanent and unrepairable.
        """
        duplicates = self._store.query(
            "SELECT COUNT(*) FROM (SELECT timestamp FROM pv_yield_history "
            "GROUP BY timestamp HAVING COUNT(*) > 1)"
        )
        if duplicates and duplicates[0][0]:
            logger.warning(
                "[PV-STORE] Removing %d duplicated pv_yield_history timestamp(s)",
                duplicates[0][0],
            )
            # Keep the newest row for each timestamp - it carries the most complete data.
            self._store.execute(
                "DELETE FROM pv_yield_history WHERE id NOT IN "
                "(SELECT MAX(id) FROM pv_yield_history GROUP BY timestamp)"
            )
        self._store.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pv_yield_ts "
            "ON pv_yield_history(timestamp)"
        )

    def insert_hourly_record(
        self,
        timestamp: str,
        date: str,
        hour: int,
        timeframe_id: int,
        real_counter_kwh: Optional[float],
        real_delta_kwh: Optional[float],
        forecast_kwh: Optional[float],
        local_date: Optional[str] = None,
        local_hour: Optional[int] = None,
        local_offset_minutes: Optional[int] = None,
    ) -> None:
        """
        Insert an hourly PV yield record, or complete the one already stored for it.

        Rows are keyed by the UTC `timestamp`, which identifies the recorded hour
        uniquely. Keying on the local hour instead would merge the two 02:00 hours of
        the autumn DST transition and silently discard one hour of measured yield.

        An existing row is updated rather than replaced, so an hour first written by gap
        reconstruction (forecast unknown) gains its forecast when the real value arrives.

        Args:
            timestamp: UTC ISO datetime of the period start; the de-duplication key.
            date: Local date of the period (YYYY-MM-DD). Legacy name, local value.
            hour: Local hour 0-23 of the period. Legacy name, local value.
            timeframe_id: Timeframe 1-4 the hour falls into.
            real_counter_kwh: Meter counter reading in kWh.
            real_delta_kwh: Yield during this hour in kWh.
            forecast_kwh: Forecast for this hour in kWh, or None when unknown.
            local_date: Timezone-aware local date (YYYY-MM-DD).
            local_hour: Timezone-aware local hour 0-23.
            local_offset_minutes: UTC offset in minutes, distinguishing DST occurrences.
        """
        now = datetime.now(timezone.utc).isoformat()

        existing = self._store.query(
            "SELECT id FROM pv_yield_history WHERE timestamp = ? LIMIT 1",
            (timestamp,),
        )

        if existing:
            # Merge into the existing row. COALESCE keeps a previously stored value when
            # the new one is NULL, so reconstruction never erases real data.
            self._store.execute(
                """
                UPDATE pv_yield_history
                SET
                    date = COALESCE(?, date),
                    hour = COALESCE(?, hour),
                    timeframe_id = COALESCE(?, timeframe_id),
                    real_counter_kwh = COALESCE(?, real_counter_kwh),
                    real_delta_kwh = COALESCE(?, real_delta_kwh),
                    forecast_kwh = COALESCE(?, forecast_kwh),
                    local_date = COALESCE(?, local_date),
                    local_hour = COALESCE(?, local_hour),
                    local_offset_minutes = COALESCE(?, local_offset_minutes)
                WHERE id = ?
                """,
                (
                    date,
                    hour,
                    timeframe_id,
                    real_counter_kwh,
                    real_delta_kwh,
                    forecast_kwh,
                    local_date,
                    local_hour,
                    local_offset_minutes,
                    existing[0][0],
                ),
            )
        else:
            self._store.execute(
                """
                INSERT INTO pv_yield_history
                    (timestamp, date, hour, timeframe_id, real_counter_kwh,
                     real_delta_kwh, forecast_kwh, created_at, local_date, local_hour,
                     local_offset_minutes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    date,
                    hour,
                    timeframe_id,
                    real_counter_kwh,
                    real_delta_kwh,
                    forecast_kwh,
                    now,
                    local_date,
                    local_hour,
                    local_offset_minutes,
                ),
            )

    @staticmethod
    def _as_dict(row) -> dict:
        return {
            "id": row[0],
            "timestamp": row[1],
            "date": row[2],
            "hour": row[3],
            "timeframe_id": row[4],
            "real_counter_kwh": row[5],
            "real_delta_kwh": row[6],
            "forecast_kwh": row[7],
            "created_at": row[8],
            "local_date": row[9],
            "local_hour": row[10],
            "local_offset_minutes": row[11],
        }

    def get_latest_record(self) -> Optional[dict]:
        """Return the most recently recorded hour, or None when the table is empty."""
        rows = self._store.query(
            f"SELECT {_COLUMNS} FROM pv_yield_history ORDER BY timestamp DESC LIMIT 1"
        )
        return self._as_dict(rows[0]) if rows else None

    def get_history_last_n_days(self, days: int = 7) -> List[dict]:
        """Return every recorded hour within the last `days` days, oldest first."""
        rows = self._store.query(
            f"SELECT {_COLUMNS} FROM pv_yield_history "
            f"WHERE {_TS_PREFIX} >= {_CUTOFF} "
            "ORDER BY timestamp ASC",
            (f"-{days} days",),
        )
        return [self._as_dict(row) for row in rows]

    def purge_old_records(self, days: int = 7) -> int:
        """Delete records older than `days` days and return how many were removed."""
        cur = self._store.execute(
            f"DELETE FROM pv_yield_history WHERE {_TS_PREFIX} < {_CUTOFF}",
            (f"-{days} days",),
        )
        return cur.rowcount if hasattr(cur, "rowcount") else 0
