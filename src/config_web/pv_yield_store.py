"""
PvYieldStore — helper wrapper around ConfigStore for PV yield history.

Provides a dedicated schema and convenience methods to insert and
query hourly PV yield records used by the PV autoscaler feature.
"""
from datetime import datetime
from typing import List, Optional

from .store import ConfigStore


class PvYieldStore:
    def __init__(self, store: ConfigStore):
        self._store = store

    def ensure_schema(self) -> None:
        """Create pv_yield_history table and indexes if they don't exist.
        
        Includes migration for new timezone-aware columns (local_date, local_hour, local_offset_minutes).
        """
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
        # Migrate the old Wh column once. New records use kWh consistently.
        columns = {
            row[1]
            for row in self._store.query("PRAGMA table_info(pv_yield_history)")
        }
        if "forecast_wh" in columns and "forecast_kwh" not in columns:
            self._store.execute(
                "ALTER TABLE pv_yield_history RENAME COLUMN forecast_wh TO forecast_kwh"
            )
            self._store.execute(
                "UPDATE pv_yield_history SET forecast_kwh = forecast_kwh / 1000.0 "
                "WHERE forecast_kwh IS NOT NULL"
            )
        elif "forecast_kwh" not in columns:
            self._store.execute(
                "ALTER TABLE pv_yield_history ADD COLUMN forecast_kwh REAL"
            )

        # Add timezone columns if upgrading from older schema (idempotent)
        try:
            self._store.execute("ALTER TABLE pv_yield_history ADD COLUMN local_date TEXT")
        except Exception:
            pass  # Column already exists
        try:
            self._store.execute("ALTER TABLE pv_yield_history ADD COLUMN local_hour INTEGER")
        except Exception:
            pass  # Column already exists
        try:
            self._store.execute("ALTER TABLE pv_yield_history ADD COLUMN local_offset_minutes INTEGER")
        except Exception:
            pass  # Column already exists
        
        # indices
        self._store.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_yield_ts ON pv_yield_history(timestamp)"
        )
        self._store.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_yield_date ON pv_yield_history(date)"
        )
        self._store.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_yield_local_date ON pv_yield_history(local_date)"
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
        """Insert or update hourly PV yield record.
        
        Prevents duplicates by checking if a record exists for (local_date, local_hour).
        If it exists, UPDATE it; otherwise INSERT a new one.
        
        This handles the case where missed hours are first inserted with forecast_kwh=None,
        and later that same hour is collected with real forecast data.
        
        Args:
            timestamp: UTC ISO datetime string (for audit trail)
            date: Date string in UTC (legacy field)
            hour: Hour 0-23 in UTC (legacy field)
            timeframe_id: Timeframe 1-4 (based on hour)
            real_counter_kwh: Counter reading in kWh
            real_delta_kwh: Delta from previous counter in kWh
            forecast_kwh: Forecast value in kWh for this hour
            local_date: Date string in local timezone (YYYY-MM-DD)
            local_hour: Hour 0-23 in local timezone
            local_offset_minutes: UTC offset in minutes at time of collection (for DST tracking)
        """
        now = datetime.utcnow().isoformat()
        
        # Check if a record already exists for this (local_date, local_hour)
        existing = self._store.query(
            "SELECT id FROM pv_yield_history WHERE local_date = ? AND local_hour = ? LIMIT 1",
            (local_date, local_hour),
        )
        
        if existing:
            # UPDATE: Merge new data with existing record
            # Preserve the original id but update all other fields
            # Only update forecast_kwh if new value is not NULL (prefer real data)
            row_id = existing[0][0]
            self._store.execute(
                """
                UPDATE pv_yield_history 
                SET 
                    timestamp = COALESCE(?, timestamp),
                    date = COALESCE(?, date),
                    hour = COALESCE(?, hour),
                    timeframe_id = COALESCE(?, timeframe_id),
                    real_counter_kwh = COALESCE(?, real_counter_kwh),
                    real_delta_kwh = COALESCE(?, real_delta_kwh),
                    forecast_kwh = COALESCE(?, forecast_kwh),
                    local_offset_minutes = COALESCE(?, local_offset_minutes)
                WHERE id = ?
                """,
                (
                    timestamp,
                    date,
                    hour,
                    timeframe_id,
                    real_counter_kwh,
                    real_delta_kwh,
                    forecast_kwh,
                    local_offset_minutes,
                    row_id,
                ),
            )
        else:
            # INSERT: New record
            self._store.execute(
                """
                INSERT INTO pv_yield_history
                    (timestamp, date, hour, timeframe_id, real_counter_kwh, real_delta_kwh, forecast_kwh, created_at, local_date, local_hour, local_offset_minutes)
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

    def get_latest_record(self) -> Optional[dict]:
        rows = self._store.query(
            "SELECT id, timestamp, date, hour, timeframe_id, real_counter_kwh, real_delta_kwh, forecast_kwh, created_at, local_date, local_hour, local_offset_minutes "
            "FROM pv_yield_history ORDER BY timestamp DESC LIMIT 1"
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r[0],
            "timestamp": r[1],
            "date": r[2],
            "hour": r[3],
            "timeframe_id": r[4],
            "real_counter_kwh": r[5],
            "real_delta_kwh": r[6],
            "forecast_kwh": r[7],
            "created_at": r[8],
            "local_date": r[9],
            "local_hour": r[10],
            "local_offset_minutes": r[11],
        }

    def get_history_last_n_days(self, days: int = 7) -> List[dict]:
        rows = self._store.query(
            "SELECT id, timestamp, date, hour, timeframe_id, real_counter_kwh, real_delta_kwh, forecast_kwh, created_at, local_date, local_hour, local_offset_minutes "
            "FROM pv_yield_history "
            "WHERE timestamp >= datetime('now', ?) "
            "ORDER BY timestamp ASC",
            (f"-{days} days",),
        )
        result = []
        for r in rows:
            result.append(
                {
                    "id": r[0],
                    "timestamp": r[1],
                    "date": r[2],
                    "hour": r[3],
                    "timeframe_id": r[4],
                    "real_counter_kwh": r[5],
                    "real_delta_kwh": r[6],
                    "forecast_kwh": r[7],
                    "created_at": r[8],
                    "local_date": r[9],
                    "local_hour": r[10],
                    "local_offset_minutes": r[11],
                }
            )
        return result

    def purge_old_records(self, days: int = 7) -> int:
        cur = self._store.execute(
            "DELETE FROM pv_yield_history WHERE timestamp < datetime('now', ?)",
            (f"-{days} days",),
        )
        return cur.rowcount if hasattr(cur, "rowcount") else 0
