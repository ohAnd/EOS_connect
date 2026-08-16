import pytest
from datetime import datetime, timedelta

from src.interfaces.pv_autoscaler import PvAutoscaler


class InMemoryPvYieldStore:
    def __init__(self):
        # store list of dict rows
        self.rows = []

    def get_latest_record(self):
        return self.rows[-1] if self.rows else None

    def insert_hourly_record(self, timestamp, date, hour, timeframe_id, real_counter_kwh, real_delta_kwh, forecast_kwh):
        self.rows.append({
            "timestamp": timestamp,
            "date": date,
            "hour": hour,
            "timeframe_id": timeframe_id,
            "real_counter_kwh": real_counter_kwh,
            "real_delta_kwh": real_delta_kwh,
            "forecast_kwh": forecast_kwh,
        })

    def purge_old_records(self, retention_days):
        # naive: keep all for test
        pass

    def get_history_last_n_days(self, n):
        return list(self.rows)


def make_rows_for_days(start_date: datetime, days: int):
    rows = []
    for d in range(days):
        day = start_date + timedelta(days=d)
        date_str = day.strftime("%Y-%m-%d")
        # timeframe 1: hours 0-5 -> no PV (0)
        for h in range(0, 24):
            tf = (h // 6) + 1
            # simulate PV: only TF 2 and 3 have production
            if tf in (2, 3):
                # let's set real_delta_kwh = 4.8 and forecast_kwh = 6 kWh
                rows.append({
                    "timestamp": f"{date_str}T{h:02d}:00:00",
                    "date": date_str,
                    "hour": h,
                    "timeframe_id": tf,
                    "real_counter_kwh": None,
                    "real_delta_kwh": 4.8,
                    "forecast_kwh": 6.0,
                })
            else:
                rows.append({
                    "timestamp": f"{date_str}T{h:02d}:00:00",
                    "date": date_str,
                    "hour": h,
                    "timeframe_id": tf,
                    "real_counter_kwh": None,
                    "real_delta_kwh": 0.0,
                    "forecast_kwh": 0,
                })
    return rows


def test_compute_timeframe_scaling_factors_and_apply_scaling():
    store = InMemoryPvYieldStore()
    # create 3 days matching user's JSON
    base = datetime(2026, 8, 6)
    store.rows = make_rows_for_days(base, 3)

    cfg = {
        "enabled": True,
        "retention_days": 7,
        "min_data_hours_required": 1,
        "min_scale_factor": 0.2,
        "max_scale_factor": 2.5,
    }

    autoscaler = PvAutoscaler(cfg, store, auto_start=False)

    factors = autoscaler.compute_timeframe_scaling_factors()
    # Expect TF1 and TF4 neutral, TF2 and TF3 scale = avg_actual/avg_forecast = 4.8/(6) = 0.8
    assert factors[1] == 1.0
    assert factors[4] == 1.0
    assert round(factors[2], 3) == 0.8
    assert round(factors[3], 3) == 0.8

    # Apply scaling to a simple forecast array of 24 hourly values (kWh)
    forecast = [0.0] * 24
    # Set some values for TF2 hours (6-11) and TF3 hours (12-17)
    for h in range(6, 12):
        forecast[h] = 6.0
    for h in range(12, 18):
        forecast[h] = 6.0

    scaled = autoscaler.apply_scaling(forecast, time_frame_base=3600)
    # TF2 hours should be scaled to 6 * 0.8 = 4.8 -> rounded to 4.8 (one decimal)
    assert scaled[6] == 4.8
    assert scaled[11] == 4.8
    # TF3 hours likewise
    assert scaled[12] == 4.8
    assert scaled[17] == 4.8

    # Status reflects enabled and total hours recorded
    status = autoscaler.get_status()
    assert status["enabled"] is True
    assert status["total_hours_recorded"] == len(store.rows)


def test_compute_with_insufficient_data_uses_neutral():
    store = InMemoryPvYieldStore()
    # empty store
    cfg = {"enabled": True, "retention_days": 7, "min_data_hours_required": 100}
    autoscaler = PvAutoscaler(cfg, store, auto_start=False)
    factors = autoscaler.compute_timeframe_scaling_factors()
    assert factors == {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}
