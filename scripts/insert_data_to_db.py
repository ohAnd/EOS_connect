#!/usr/bin/env python3
"""Seed PV yield history with manually supplied timeframe values.

This is a development/test helper only. Production collection is handled by
``src.interfaces.pv_autoscaler.PvAutoscaler``.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pytz

# Add project root to path so we can import from src
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.persistence import PvYieldStore
from src.config_web.store import ConfigStore

logging.basicConfig(level=logging.INFO, format="[PV-INSERT] %(message)s")
logger = logging.getLogger(__name__)

def delete_pv_yields_for_date(store, target_date: str):
    """Delete all PV yield records for a specific local date."""
    try:
        # Go through ConfigStore.execute so the write takes the store's lock, rather
        # than opening a second path to the same connection.
        cursor = store.execute(
            "DELETE FROM pv_yield_history WHERE local_date = ?",
            (target_date,),
        )
        deleted = cursor.rowcount if hasattr(cursor, "rowcount") else 0
        logger.info("Deleted %s record(s) for date %s", deleted, target_date)
    except Exception as exc:
        logger.error("Failed to delete records for date %s: %s", target_date, exc)

def insert_pv_yields(
    pv_yield_store,
    previous_counter_kwh: float,
    t1_r: float,
    t2_r: float,
    t3_r: float,
    t4_r: float,
    t1_f: float = None,
    t2_f: float = None,
    t3_f: float = None,
    t4_f: float = None,
    target_date: str = None,
    timezone_name: str = "UTC",
    hour_interval_minutes: int = 60,
):
    """Insert manually supplied real and forecast values in kWh."""
    try:
        tz = pytz.timezone(timezone_name)
        target_local = (
            datetime.strptime(target_date, "%Y-%m-%d").date()
            if target_date
            else datetime.now(tz).date() - timedelta(days=1)
        )
    except (TypeError, ValueError, pytz.UnknownTimeZoneError) as exc:
        logger.error("Invalid target date or timezone: %s", exc)
        return None

    step_hours = max(1, hour_interval_minutes // 60)
    rows = []

    # Calculate hourly values for each timeframe
    t1_hours = 6  # 0-6 hours
    t2_hours = 6  # 6-12 hours
    t3_hours = 6  # 12-18 hours
    t4_hours = 6  # 18-24 hours

    t1_r_hourly = t1_r / t1_hours if t1_hours > 0 else 0.0
    t1_f_hourly = t1_f / t1_hours if t1_hours > 0 and t1_f is not None else None

    t2_r_hourly = t2_r / t2_hours if t2_hours > 0 else 0.0
    t2_f_hourly = t2_f / t2_hours if t2_hours > 0 and t2_f is not None else None

    t3_r_hourly = t3_r / t3_hours if t3_hours > 0 else 0.0
    t3_f_hourly = t3_f / t3_hours if t3_hours > 0 and t3_f is not None else None

    t4_r_hourly = t4_r / t4_hours if t4_hours > 0 else 0.0
    t4_f_hourly = t4_f / t4_hours if t4_hours > 0 and t4_f is not None else None

    for hour in range(0, 24, step_hours):
        local_dt = tz.localize(
            datetime(target_local.year, target_local.month, target_local.day, hour)
        )

        if hour < 6:
            current_delta_kwh, forecast_kwh = t1_r_hourly, t1_f_hourly
        elif hour < 12:
            current_delta_kwh, forecast_kwh = t2_r_hourly, t2_f_hourly
        elif hour < 18:
            current_delta_kwh, forecast_kwh = t3_r_hourly, t3_f_hourly
        else:
            current_delta_kwh, forecast_kwh = t4_r_hourly, t4_f_hourly

        current_delta_kwh = float(current_delta_kwh or 0.0)
        forecast_kwh = float(forecast_kwh) if forecast_kwh is not None else None
        previous_counter_kwh += current_delta_kwh
        rows.append(
            {
                "timestamp": local_dt.astimezone(timezone.utc).isoformat(),
                "date": target_local.isoformat(),
                "hour": hour,
                "timeframe_id": (hour // 6) + 1,
                "real_counter_kwh": previous_counter_kwh,
                "real_delta_kwh": current_delta_kwh,
                "forecast_kwh": forecast_kwh,
                "local_date": target_local.isoformat(),
                "local_hour": hour,
                "local_offset_minutes": int(
                    local_dt.utcoffset().total_seconds() / 60
                ),
            }
        )

    try:
        for row in rows:
            pv_yield_store.insert_hourly_record(**row)
        logger.info("Inserted %d records into pv_yield_history.", len(rows))
        return len(rows)
    except Exception as exc:
        logger.error("Failed to insert PV yield records: %s", exc)
        return None

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to eos_connect.db")
    parser.add_argument("--date", dest="target_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--timezone", default="UTC", help="Timezone for the target date (default: UTC)")
    parser.add_argument("--delete", action="store_true", help="Delete all entries for the specified date")

    # Add command-line arguments for timeframe values (optional if --delete is specified)
    parser.add_argument("--t1_r", type=float, help="Real value for timeframe 1 (0-6 hours)")
    parser.add_argument("--t1_f", type=float, help="Forecast value for timeframe 1 (0-6 hours)")
    parser.add_argument("--t2_r", type=float, help="Real value for timeframe 2 (6-12 hours)")
    parser.add_argument("--t2_f", type=float, help="Forecast value for timeframe 2 (6-12 hours)")
    parser.add_argument("--t3_r", type=float, help="Real value for timeframe 3 (12-18 hours)")
    parser.add_argument("--t3_f", type=float, help="Forecast value for timeframe 3 (12-18 hours)")
    parser.add_argument("--t4_r", type=float, help="Real value for timeframe 4 (18-24 hours)")
    parser.add_argument("--t4_f", type=float, help="Forecast value for timeframe 4 (18-24 hours)")

    args = parser.parse_args()

    # Log the full path of the database file
    db_path = os.path.abspath(args.db)
    logger.info("Using database file: %s", db_path)

    store = ConfigStore(db_path)
    store.open()
    try:
        yield_store = PvYieldStore(store)
        yield_store.ensure_schema()

        # Delete existing records for the date if --delete is specified
        if args.delete:
            delete_pv_yields_for_date(store, args.target_date)
            return  # Exit after deletion

        # Validate required arguments for insertion
        if args.t1_r is None or args.t2_r is None or args.t3_r is None or args.t4_r is None:
            parser.error("The following arguments are required for insertion: --t1_r, --t2_r, --t3_r, --t4_r")

        # Insert the PV yields using the provided timeframe values
        inserted_count = insert_pv_yields(
            pv_yield_store=yield_store,
            previous_counter_kwh=0.0,  # Start with 0 or fetch the last value from the database
            t1_r=args.t1_r,
            t2_r=args.t2_r,
            t3_r=args.t3_r,
            t4_r=args.t4_r,
            t1_f=args.t1_f,
            t2_f=args.t2_f,
            t3_f=args.t3_f,
            t4_f=args.t4_f,
            target_date=args.target_date,
            timezone_name=args.timezone,
        )

        if inserted_count is not None:
            logger.info("Successfully inserted %d records.", inserted_count)
        else:
            logger.error("No records were inserted.")
    finally:
        store.close()

if __name__ == "__main__":
    main()