from src.config_web.pv_yield_store import PvYieldStore
from src.config_web.store import ConfigStore


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