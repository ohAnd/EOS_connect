"""
PvYieldStore - hourly PV yield history for the PV autoscaler.

Stores one row per recorded hour (measured counter delta plus the forecast that had
been predicted for that hour) in the shared SQLite database, and serves the rolling
window the autoscaler derives its scale factors from.
"""
import logging
import math
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("__main__")

# Columns selected by every read, in the tuple order the callers unpack.
_COLUMNS = (
    "id, timestamp, date, hour, timeframe_id, real_counter_kwh, real_delta_kwh, "
    "forecast_kwh, created_at, local_date, local_hour, local_offset_minutes, origin"
)

# The shape every plan returns, so callers can read the same keys whether or not there
# was anything to plan.
_EMPTY_PLAN = {
    "total": 0,
    "rows": [],
    "valid": 0,
    "skipped": 0,
    "invalid": [],
    "outside_retention": 0,
    "newest_local_date": None,
    "oldest_local_date": None,
    "age_days": None,
    "seed_recommended": False,
    "shift_days": 0,
    "dropped_old": 0,
    "collisions": 0,
}

# Provenance of a row, stored in the `origin` column. NULL means measured on this
# system - the only kind of row that existed before backup restore was added.
ORIGIN_MEASURED = None
ORIGIN_IMPORTED = "imported"   # restored at its original timestamp
ORIGIN_SEEDED = "seeded"       # restored with its timestamp shifted into the window

# Timestamps are written as UTC ISO-8601 ("2026-08-22T08:00:00+00:00"), which does not
# compare lexicographically against SQLite's datetime() output ("2026-08-22 08:00:00"):
# the 'T' sorts above the space, so a plain `timestamp < datetime('now', ?)` never
# matches rows on the boundary date. Compare the leading "YYYY-MM-DDTHH:MM:SS" of both
# sides instead, which is well-ordered and offset-free.
_TS_PREFIX = "substr(timestamp, 1, 19)"
_CUTOFF = "strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)"


# Rows arrive from a user-supplied backup file, so every field is treated as untrusted.
# Only this many rejection reasons are kept for the response - the counts stay exact.
_MAX_INVALID_DETAILS = 20


def _timeframe_for_hour(hour: int) -> int:
    """
    Map a local hour to its timeframe 1-4.

    Deliberately duplicates ``interfaces.pv_autoscaler.timeframe_for_hour`` rather than
    importing it: at runtime the app starts as ``python eos_connect.py`` with ``src/``
    as the root, so ``persistence`` and ``interfaces`` are sibling top-level packages
    and a relative import across them raises ImportError. Two lines beat that hazard,
    and ``timeframe_id`` is a column of this table anyway.
    """
    return (hour // 6) + 1


def _zone(tz_name: Optional[str]):
    """Resolve a timezone name, falling back to UTC when it is missing or unknown."""
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        logger.warning("[PV-STORE] Unknown time zone %r - falling back to UTC", tz_name)
        return timezone.utc


def _parse_timestamp(value) -> Optional[datetime]:
    """Parse an ISO timestamp to an aware UTC datetime, or None when unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_kwh(value) -> tuple[bool, Optional[float]]:
    """
    Coerce an energy field. Returns ``(ok, value)``.

    Missing is fine - the column is nullable and COALESCE keeps whatever is stored.
    Negative or non-finite is not: it would enter the measured/forecast ratio and
    corrupt a scale factor for the whole retention window.
    """
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False, None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False, None
    if not math.isfinite(number) or number < 0:
        return False, None
    return True, number


def _parse_int(value, low: int, high: int) -> Optional[int]:
    """Coerce an integer field and bounds-check it, or None when unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _parse_date(value) -> Optional[str]:
    """Validate a YYYY-MM-DD date string, or None when unusable."""
    if not isinstance(value, str):
        return None
    try:
        return date_cls.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _coerce_rows(rows: list) -> tuple[list, list, int]:
    """
    Validate every exported row, keeping the good ones and counting the rest.

    Returns ``(prepared, invalid, skipped)``, where *invalid* carries a capped sample of
    rejection reasons while *skipped* stays an exact count.
    """
    prepared = []
    invalid = []
    skipped = 0
    for index, raw in enumerate(rows):
        row, error = _coerce_row(raw)
        if row is None:
            skipped += 1
            if len(invalid) < _MAX_INVALID_DETAILS:
                invalid.append({"index": index, "error": error})
            continue
        prepared.append(row)
    return prepared, invalid, skipped


def _shift_row(row: dict, shift_days: int, zone) -> dict:
    """
    Move one row forward by whole days, keeping its local wall-clock hour.

    Shifting the UTC instant instead would move the local hour by an hour across a DST
    boundary, and every consumer buckets by local date and hour. Rebuilding the local
    time and converting back also yields the correct ``local_offset_minutes`` for the
    new date, which is what distinguishes the two 02:00 hours of an autumn transition.
    """
    new_date = date_cls.fromisoformat(row["local_date"]) + timedelta(days=shift_days)
    local = datetime(
        new_date.year, new_date.month, new_date.day, row["local_hour"], tzinfo=zone
    )
    offset = local.utcoffset() or timedelta()
    return {
        **row,
        "timestamp": local.astimezone(timezone.utc).isoformat(),
        "date": new_date.isoformat(),
        "local_date": new_date.isoformat(),
        "local_offset_minutes": int(offset.total_seconds() // 60),
    }


def _coerce_row(raw) -> tuple[Optional[dict], str]:
    """
    Validate and normalise one exported row. Returns ``(row, error)``.

    ``real_counter_kwh`` is dropped on purpose - see ``PvYieldStore.plan_import``.
    """
    if not isinstance(raw, dict):
        return None, "row is not an object"

    utc_ts = _parse_timestamp(raw.get("timestamp"))
    if utc_ts is None:
        return None, "missing or unparseable timestamp"

    offset_minutes = _parse_int(raw.get("local_offset_minutes"), -1440, 1440)

    # The stored local date/hour are what every consumer buckets by, so prefer them and
    # only reconstruct from the timestamp when the row predates those columns.
    local_hour = _parse_int(raw.get("local_hour"), 0, 23)
    if local_hour is None:
        local_hour = _parse_int(raw.get("hour"), 0, 23)
    local_date = _parse_date(raw.get("local_date")) or _parse_date(raw.get("date"))
    if local_hour is None or local_date is None:
        derived = utc_ts + timedelta(minutes=offset_minutes or 0)
        if local_hour is None:
            local_hour = derived.hour
        if local_date is None:
            local_date = derived.date().isoformat()

    timeframe_id = _parse_int(raw.get("timeframe_id"), 1, 4)
    if timeframe_id is None:
        timeframe_id = _timeframe_for_hour(local_hour)

    real_ok, real_delta_kwh = _parse_kwh(raw.get("real_delta_kwh"))
    if not real_ok:
        return None, "real_delta_kwh is negative, non-finite or not a number"
    forecast_ok, forecast_kwh = _parse_kwh(raw.get("forecast_kwh"))
    if not forecast_ok:
        return None, "forecast_kwh is negative, non-finite or not a number"

    return {
        "timestamp": utc_ts.isoformat(),
        "date": local_date,
        "hour": local_hour,
        "timeframe_id": timeframe_id,
        "real_delta_kwh": real_delta_kwh,
        "forecast_kwh": forecast_kwh,
        "local_date": local_date,
        "local_hour": local_hour,
        "local_offset_minutes": offset_minutes,
    }, ""


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
            # NULL on every pre-existing row, which is correct: they were all measured
            # here. Only restored rows carry a value.
            ("origin", "TEXT"),
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
        origin: Optional[str] = ORIGIN_MEASURED,
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
            origin: Provenance marker for a new row - None for measured, "imported" or
                "seeded" for a restored one. Deliberately not part of the merge below:
                an hour measured here stays measured even if a backup later completes it.
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
                     local_offset_minutes, origin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    origin,
                ),
            )

    # ------------------------------------------------------------------
    # Backup restore
    # ------------------------------------------------------------------

    def plan_import(
        self,
        rows: Any,
        retention_days: int = 7,
        tz_name: Optional[str] = None,
        mode: str = "as_is",
    ) -> Dict[str, Any]:
        """
        Work out what restoring *rows* would do, without writing anything.

        Drives the dry-run preview in the restore dialog and is reused by
        ``import_rows`` so the preview and the write can never disagree.

        ``real_counter_kwh`` is never restored. It is an absolute meter reading, and the
        autoscaler seeds its in-memory baseline from the newest stored row on startup;
        a restored reading would make the first live collection record the difference
        between two unrelated meters as one hour of yield, pinning that timeframe to
        ``max_scale_factor`` for the entire retention window. Left NULL, the autoscaler
        simply re-establishes its baseline on the first collection. Scale factors are
        computed from ``real_delta_kwh`` and ``forecast_kwh`` only, so nothing is lost.

        Args:
            rows: The ``pv_yield_history`` list from a backup file. Anything else yields
                an empty plan rather than an error.
            retention_days: The window the autoscaler currently keeps.
            tz_name: Configured local timezone, used to place "today" and to rebuild
                local times when seeding.
            mode: ``"as_is"`` restores rows at their original timestamps. ``"seed"``
                shifts them forward so the newest lands on yesterday - see
                ``_plan_seed``.

        Returns:
            A plan with ``rows`` ready for ``insert_hourly_record``, plus the counts the
            preview shows: ``total``, ``valid``, ``skipped``, ``invalid``,
            ``outside_retention``, ``newest_local_date``, ``oldest_local_date``,
            ``age_days``, ``seed_recommended``, ``shift_days``, ``dropped_old`` and
            ``collisions``.
        """
        if not isinstance(rows, list) or not rows:
            return dict(_EMPTY_PLAN)

        prepared, invalid, skipped = _coerce_rows(rows)
        if not prepared:
            return {
                **_EMPTY_PLAN,
                "total": len(rows),
                "skipped": skipped,
                "invalid": invalid,
            }

        zone = _zone(tz_name)
        today_local = datetime.now(zone).date()
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        local_dates = sorted(row["local_date"] for row in prepared)
        newest = local_dates[-1]
        age_days = (today_local - date_cls.fromisoformat(newest)).days

        plan = {
            "total": len(rows),
            "rows": prepared,
            "valid": len(prepared),
            "skipped": skipped,
            "invalid": invalid,
            "outside_retention": sum(
                1 for row in prepared if _parse_timestamp(row["timestamp"]) < cutoff
            ),
            "newest_local_date": newest,
            "oldest_local_date": local_dates[0],
            "age_days": age_days,
            # Past the retention window the stored history and the backup cannot
            # overlap, which is what makes shifting the rows forward safe.
            "seed_recommended": age_days > retention_days,
            "shift_days": 0,
            "dropped_old": 0,
            "collisions": 0,
        }
        if mode == "seed":
            plan.update(self._plan_seed(plan, today_local, cutoff, zone))
        return plan

    def _plan_seed(
        self, plan: Dict[str, Any], today_local: date_cls, cutoff: datetime, zone
    ) -> Dict[str, Any]:
        """
        Shift a plan's rows forward so the newest one lands on yesterday.

        Restoring history that predates the retention window is otherwise futile: the
        rows are deleted at the next hourly collection and the user waits days for
        usable scale factors to be re-learnt. Shifting the block forward keeps each
        hour's measured/forecast ratio - which is what the factors are made of - while
        placing it where the autoscaler still reads it.

        Whole days only, so hour-of-day and therefore ``timeframe_id`` are preserved
        exactly. Rows that still land outside the window after the shift are dropped
        rather than written for immediate purging, and a row that would land on a
        timestamp already present is skipped: the stored row is either a real
        measurement or an earlier restore, and neither should be overwritten by a
        synthetic one.
        """
        shift_days = (today_local - timedelta(days=1)) - date_cls.fromisoformat(
            plan["newest_local_date"]
        )
        shift_days = shift_days.days
        if shift_days <= 0:
            return {}

        occupied = {
            row[0]
            for row in self._store.query("SELECT timestamp FROM pv_yield_history")
        }
        shifted = []
        dropped_old = 0
        collisions = 0
        for row in plan["rows"]:
            moved = _shift_row(row, shift_days, zone)
            if _parse_timestamp(moved["timestamp"]) < cutoff:
                dropped_old += 1
                continue
            # Also catches two source rows collapsing onto one local hour, which the
            # duplicated 02:00 of an autumn DST transition produces.
            if moved["timestamp"] in occupied:
                collisions += 1
                continue
            occupied.add(moved["timestamp"])
            shifted.append(moved)

        return {
            "rows": shifted,
            "valid": len(shifted),
            "shift_days": shift_days,
            "dropped_old": dropped_old,
            "collisions": collisions,
            "outside_retention": 0,
        }

    def import_rows(
        self,
        rows: Any,
        retention_days: int = 7,
        tz_name: Optional[str] = None,
        mode: str = "as_is",
    ) -> Dict[str, Any]:
        """
        Restore exported rows.

        Writes through ``insert_hourly_record``, which upserts on the UTC timestamp, so
        re-importing the same file is idempotent.

        In ``as_is`` mode rows keep their timestamps. Any already outside the retention
        window are still written and reported: they will be purged at the next hourly
        collection, and the count tells the user to raise ``retention_days`` and try
        again rather than leaving them to vanish silently.

        In ``seed`` mode the block is shifted forward into the window and marked
        ``seeded``, so a restore made long after the backup still yields usable scale
        factors from startup instead of days of neutral 1.0.

        Returns the plan counts plus ``imported``.
        """
        plan = self.plan_import(
            rows, retention_days=retention_days, tz_name=tz_name, mode=mode
        )
        # A shift of zero means seeding had nothing to do, so the rows are unmoved and
        # should not claim to be synthetic.
        seeded = mode == "seed" and plan["shift_days"] > 0
        origin = ORIGIN_SEEDED if seeded else ORIGIN_IMPORTED

        imported = 0
        for row in plan["rows"]:
            self.insert_hourly_record(real_counter_kwh=None, origin=origin, **row)
            imported += 1

        if imported:
            logger.info(
                "[PV-STORE] Restored %d yield row(s) as %s (shift=%dd), "
                "%d skipped, %d outside retention, %d dropped, %d collision(s)",
                imported,
                origin,
                plan["shift_days"],
                plan["skipped"],
                plan["outside_retention"],
                plan["dropped_old"],
                plan["collisions"],
            )
        return {
            **{k: v for k, v in plan.items() if k != "rows"},
            "imported": imported,
            "origin": origin if imported else None,
        }

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
            "origin": row[12],
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

    def get_all_history(self) -> List[dict]:
        """Return every stored hour, oldest first, ignoring any retention window.

        Used by backup export, which must capture whatever is on disk rather than only
        the days the autoscaler currently scores.
        """
        rows = self._store.query(
            f"SELECT {_COLUMNS} FROM pv_yield_history ORDER BY timestamp ASC"
        )
        return [self._as_dict(row) for row in rows]

    def purge_old_records(self, days: int = 7) -> int:
        """Delete records older than `days` days and return how many were removed."""
        cur = self._store.execute(
            f"DELETE FROM pv_yield_history WHERE {_TS_PREFIX} < {_CUTOFF}",
            (f"-{days} days",),
        )
        return cur.rowcount if hasattr(cur, "rowcount") else 0
