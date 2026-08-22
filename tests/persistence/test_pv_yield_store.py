from datetime import datetime, timedelta, timezone

import pytest

from src.persistence import PvYieldStore
from src.config_web.store import ConfigStore


@pytest.fixture(name="pv_store")
def pv_store_fixture(tmp_path):
    """A migrated PvYieldStore on a throwaway database."""
    store = ConfigStore(str(tmp_path / "config.db"))
    store.open()
    pv_store = PvYieldStore(store)
    pv_store.ensure_schema()
    try:
        yield pv_store
    finally:
        store.close()


def _record(pv_store, local_dt, **overrides):
    """Insert one hour, defaulting every field from the given local datetime."""
    fields = {
        "timestamp": local_dt.astimezone(timezone.utc).isoformat(),
        "date": local_dt.strftime("%Y-%m-%d"),
        "hour": local_dt.hour,
        "timeframe_id": (local_dt.hour // 6) + 1,
        "real_counter_kwh": 100.0,
        "real_delta_kwh": 1.0,
        "forecast_kwh": 1.2,
        "local_date": local_dt.strftime("%Y-%m-%d"),
        "local_hour": local_dt.hour,
        "local_offset_minutes": int((local_dt.utcoffset() or timedelta()).total_seconds() / 60),
    }
    fields.update(overrides)
    pv_store.insert_hourly_record(**fields)
    return fields


def test_migrates_legacy_forecast_wh_to_kwh(tmp_path):
    store = ConfigStore(str(tmp_path / "config.db"))
    store.open()
    try:
        store.execute(
            """
            CREATE TABLE pv_yield_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                timeframe_id INTEGER NOT NULL,
                real_counter_kwh REAL,
                real_delta_kwh REAL,
                forecast_wh REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        store.execute(
            """
            INSERT INTO pv_yield_history
                (timestamp, date, hour, timeframe_id, forecast_wh, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("2026-08-15T16:00:00+00:00", "2026-08-15", 18, 4, 8837.1, "now"),
        )

        pv_store = PvYieldStore(store)
        pv_store.ensure_schema()

        columns = {
            row[1]
            for row in store.query("PRAGMA table_info(pv_yield_history)")
        }
        assert "forecast_wh" not in columns
        assert "forecast_kwh" in columns
        row = pv_store.get_latest_record()
        assert row["forecast_kwh"] == 8.8371
    finally:
        store.close()


def test_backfills_local_columns_for_pre_migration_rows(tmp_path):
    """
    Rows written before the local_* columns existed must not read back as NULL.

    Every consumer groups history by local_date; a NULL there produces a phantom
    "no date" day that the UI renders as Jan 1 1970.
    """
    store = ConfigStore(str(tmp_path / "config.db"))
    store.open()
    try:
        store.execute(
            """
            CREATE TABLE pv_yield_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                timeframe_id INTEGER NOT NULL,
                real_counter_kwh REAL,
                real_delta_kwh REAL,
                forecast_kwh REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        store.execute(
            "INSERT INTO pv_yield_history "
            "(timestamp, date, hour, timeframe_id, real_delta_kwh, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-08-15T16:00:00+00:00", "2026-08-15", 18, 4, 3.5, "now"),
        )

        pv_store = PvYieldStore(store)
        pv_store.ensure_schema()

        row = pv_store.get_latest_record()
        assert row["local_date"] == "2026-08-15"
        assert row["local_hour"] == 18
    finally:
        store.close()


def test_ensure_schema_is_idempotent(pv_store):
    """Re-running migration on an already-current database must be a no-op."""
    _record(pv_store, datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc))
    pv_store.ensure_schema()
    pv_store.ensure_schema()
    assert len(pv_store.get_history_last_n_days(3650)) == 1


def test_same_hour_is_updated_not_duplicated(pv_store):
    """A reconstructed hour gains its forecast when the real value arrives."""
    when = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    _record(pv_store, when, real_delta_kwh=2.0, forecast_kwh=None)
    _record(pv_store, when, real_delta_kwh=2.0, forecast_kwh=3.0)

    rows = pv_store.get_history_last_n_days(3650)
    assert len(rows) == 1
    assert rows[0]["forecast_kwh"] == pytest.approx(3.0)


def test_null_does_not_overwrite_a_stored_value(pv_store):
    """COALESCE semantics: a later NULL must not erase real data."""
    when = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    _record(pv_store, when, forecast_kwh=3.0)
    _record(pv_store, when, forecast_kwh=None)

    assert pv_store.get_latest_record()["forecast_kwh"] == pytest.approx(3.0)


def test_repeated_local_hour_with_different_offsets_is_kept(pv_store):
    """
    Both 02:00 hours of a DST fall-back are distinct records.

    They share (local_date, local_hour) and differ only in UTC offset, so keying the
    upsert on the local hour would discard one hour of measured yield.
    """
    first = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)   # 02:00 CEST
    second = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)  # 02:00 CET
    _record(pv_store, first, date="2026-10-25", hour=2, local_date="2026-10-25",
            local_hour=2, local_offset_minutes=120, real_delta_kwh=5.0)
    _record(pv_store, second, date="2026-10-25", hour=2, local_date="2026-10-25",
            local_hour=2, local_offset_minutes=60, real_delta_kwh=7.0)

    rows = pv_store.get_history_last_n_days(3650)
    assert len(rows) == 2
    assert sorted(r["real_delta_kwh"] for r in rows) == pytest.approx([5.0, 7.0])
    assert {r["local_offset_minutes"] for r in rows} == {60, 120}


def test_retention_window_compares_timestamps_correctly(pv_store):
    """
    Retention must respect the day boundary it advertises.

    Timestamps are stored as ISO-8601 with a 'T' separator; comparing them directly
    against SQLite's space-separated datetime() output sorts every boundary-date row
    as newer than the cutoff, so nothing on that day is ever purged.
    """
    now = datetime.now(timezone.utc)
    _record(pv_store, now - timedelta(hours=1), real_delta_kwh=1.0)
    _record(pv_store, now - timedelta(days=2), real_delta_kwh=2.0)
    # Just past the cutoff, so it falls on the cutoff's own calendar date. This is the
    # case a naive string comparison gets wrong: 'T' sorts above the space in SQLite's
    # datetime() output, making the row look newer than the cutoff.
    _record(pv_store, now - timedelta(days=3, hours=1), real_delta_kwh=3.0)
    _record(pv_store, now - timedelta(days=10), real_delta_kwh=4.0)

    assert sorted(
        r["real_delta_kwh"] for r in pv_store.get_history_last_n_days(3)
    ) == pytest.approx([1.0, 2.0])

    removed = pv_store.purge_old_records(3)
    assert removed == 2
    remaining = pv_store.get_history_last_n_days(3650)
    assert sorted(r["real_delta_kwh"] for r in remaining) == pytest.approx([1.0, 2.0])


def test_duplicate_timestamps_are_repaired_on_migration(tmp_path):
    """A database that already holds duplicates is de-duplicated, keeping the newest."""
    store = ConfigStore(str(tmp_path / "config.db"))
    store.open()
    try:
        pv_store = PvYieldStore(store)
        pv_store.ensure_schema()
        # Bypass the upsert to plant a duplicate the way an older build could.
        store.execute("DROP INDEX IF EXISTS idx_pv_yield_ts")
        for delta in (1.0, 9.0):
            store.execute(
                "INSERT INTO pv_yield_history "
                "(timestamp, date, hour, timeframe_id, real_delta_kwh, created_at, "
                " local_date, local_hour) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-08-20T10:00:00+00:00", "2026-08-20", 10, 2, delta, "now",
                 "2026-08-20", 10),
            )
        assert len(pv_store.get_history_last_n_days(3650)) == 2

        pv_store.ensure_schema()

        rows = pv_store.get_history_last_n_days(3650)
        assert len(rows) == 1
        assert rows[0]["real_delta_kwh"] == pytest.approx(9.0)
    finally:
        store.close()
