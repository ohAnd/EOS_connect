import pytest
from datetime import datetime, timedelta, timezone
import pytz

from src.interfaces.pv_autoscaler import PvAutoscaler


class InMemoryPvYieldStore:
    def __init__(self):
        self.rows = []

    def get_latest_record(self):
        return self.rows[-1] if self.rows else None

    def insert_hourly_record(self, timestamp, date, hour, timeframe_id, real_counter_kwh, real_delta_kwh, forecast_kwh, local_date=None, local_hour=None, local_offset_minutes=None):
        self.rows.append({
            "timestamp": timestamp,
            "date": date,
            "hour": hour,
            "timeframe_id": timeframe_id,
            "real_counter_kwh": real_counter_kwh,
            "real_delta_kwh": real_delta_kwh,
            "forecast_kwh": forecast_kwh,
            "local_date": local_date,
            "local_hour": local_hour,
            "local_offset_minutes": local_offset_minutes,
        })

    def purge_old_records(self, days: int = 7):
        pass

    def get_history_last_n_days(self, n: int = 7):
        return list(self.rows)


class FakePvInterface:
    def __init__(self, array, time_frame_base=3600):
        self._current_forecast = array
        self.time_frame_base = time_frame_base

    def get_current_pv_forecast(self):
        return self._current_forecast


def test_collect_if_needed_populates_forecast_kwh_and_recomputes():
    store = InMemoryPvYieldStore()
    
    # Use UTC timezone for consistent test behavior
    tz = pytz.UTC
    
    # seed a previous counter so autoscaler won't early-return
    # Use UTC time for seeding
    utc_now = datetime.now(timezone.utc)
    seed_ts = (utc_now - timedelta(hours=1)).isoformat()
    local_hour = utc_now.hour
    local_date = utc_now.strftime("%Y-%m-%d")
    store.rows.append({
        "timestamp": seed_ts,
        "date": local_date,
        "hour": (local_hour - 1) % 24,
        "timeframe_id": 2,
        "real_counter_kwh": 100.0,
        "real_delta_kwh": None,
        "forecast_kwh": None
    })

    cfg = {"enabled": True, "retention_days": 7, "min_data_hours_required": 1}
    # Pass UTC timezone so test behavior is consistent
    autoscaler = PvAutoscaler(cfg, store, timezone="UTC", auto_start=False)

    # Attach fake PV interface providing hourly forecast array (in Wh)
    # Each slot value represents Wh for that hour; the database stores kWh.
    # Note: We store data for the PREVIOUS hour (when delta was generated),
    # so set previous hour's forecast to 6000 Wh = 6.0 kWh
    utc_now_hour = datetime.now(timezone.utc).hour
    prev_hour = (utc_now_hour - 1) % 24
    arr = [0.0] * 48
    arr[prev_hour] = 6000.0  # 6000 Wh = 6.0 kWh for previous hour (when delta occurred)
    if prev_hour + 1 < len(arr):
        arr[prev_hour + 1] = 7000.0  # Must not be included in the previous hour
    fake = FakePvInterface(arr)
    autoscaler.set_pv_interface(fake)

    # Monkeypatch the private fetch to simulate a new cumulative counter (increase by 4.8 kWh)
    setattr(autoscaler, f"_PvAutoscaler__fetch_remote_state", lambda source, sensor: str(104.8))

    # Run collection
    ok = autoscaler.collect_if_needed()
    assert ok is True
    # Last inserted row should contain forecast_kwh = 6.0.
    last = store.rows[-1]
    assert last["forecast_kwh"] == pytest.approx(6.0)
    # Scale factors should have been computed (exists and is a dict)
    assert isinstance(getattr(autoscaler, "_scale_factors", None), dict)


def test_collect_if_needed_sums_four_15_minute_slots_to_kwh():
    store = InMemoryPvYieldStore()
    utc_now = datetime.now(timezone.utc)
    store.rows.append(
        {
            "timestamp": (utc_now - timedelta(hours=1)).isoformat(),
            "date": utc_now.strftime("%Y-%m-%d"),
            "hour": (utc_now.hour - 1) % 24,
            "timeframe_id": 2,
            "real_counter_kwh": 100.0,
            "real_delta_kwh": None,
            "forecast_kwh": None,
        }
    )
    autoscaler = PvAutoscaler(
        {"enabled": True, "retention_days": 7, "min_data_hours_required": 1},
        store,
        timezone="UTC",
        auto_start=False,
    )
    previous_hour = (datetime.now(timezone.utc).hour - 1) % 24
    arr = [0.0] * 192
    start = previous_hour * 4
    arr[start : start + 4] = [100.0, 200.0, 300.0, 400.0]
    fake = FakePvInterface(arr, time_frame_base=900)
    autoscaler.set_pv_interface(fake)
    setattr(autoscaler, "_PvAutoscaler__fetch_remote_state", lambda source, sensor: "101.0")

    assert autoscaler.collect_if_needed() is True
    assert store.rows[-1]["forecast_kwh"] == pytest.approx(1.0)


def test_collect_if_needed_uses_current_forecast_array():
    store = InMemoryPvYieldStore()
    utc_now = datetime.now(timezone.utc)
    previous_hour = (utc_now.hour - 1) % 24
    store.rows.append(
        {
            "timestamp": (utc_now - timedelta(hours=1)).isoformat(),
            "date": utc_now.strftime("%Y-%m-%d"),
            "hour": previous_hour,
            "timeframe_id": (previous_hour // 6) + 1,
            "real_counter_kwh": 100.0,
            "real_delta_kwh": None,
            "forecast_kwh": None,
        }
    )
    autoscaler = PvAutoscaler(
        {"enabled": True, "retention_days": 7, "min_data_hours_required": 1},
        store,
        timezone="UTC",
        auto_start=False,
    )
    current_array = [0.0] * 48
    current_array[previous_hour] = 4000.0
    fake = FakePvInterface(current_array, time_frame_base=3600)
    autoscaler.set_pv_interface(fake)
    setattr(autoscaler, "_PvAutoscaler__fetch_remote_state", lambda source, sensor: "101.0")

    assert autoscaler.collect_if_needed() is True
    assert store.rows[-1]["forecast_kwh"] == pytest.approx(4.0)


def test_collect_if_needed_distributes_over_missed_hours():
    store = InMemoryPvYieldStore()
    
    # Use UTC timezone for consistent test behavior
    tz = pytz.UTC
    
    # seed a previous counter 4 hours ago
    utc_now = datetime.now(timezone.utc)
    seed_ts = (utc_now - timedelta(hours=4)).isoformat()
    local_date = (utc_now - timedelta(hours=4)).strftime("%Y-%m-%d")
    local_hour = (utc_now - timedelta(hours=4)).hour
    store.rows.append({
        "timestamp": seed_ts,
        "date": local_date,
        "hour": local_hour,
        "timeframe_id": 1,
        "real_counter_kwh": 100.0,
        "real_delta_kwh": None,
        "forecast_kwh": None
    })

    cfg = {"enabled": True, "retention_days": 7, "min_data_hours_required": 1}
    # Pass UTC timezone so test behavior is consistent
    autoscaler = PvAutoscaler(cfg, store, timezone="UTC", auto_start=False)

    # A complete zero-valued snapshot represents unavailable historical
    # production without triggering the startup readiness guard.
    fake = FakePvInterface([0.0] * 48)
    autoscaler.set_pv_interface(fake)

    # Simulate new cumulative counter increased by 8.0 kWh
    setattr(autoscaler, f"_PvAutoscaler__fetch_remote_state", lambda source, sensor: str(108.0))

    ok = autoscaler.collect_if_needed()
    assert ok is True
    # One seed row + 4 distributed rows expected
    assert len(store.rows) == 5
    new_rows = store.rows[-4:]
    # Each distributed delta should be ~2.0 kWh
    for i, r in enumerate(new_rows):
        assert r["real_delta_kwh"] == pytest.approx(2.0), f"Row {i}: delta mismatch"
        # Missed hours should have forecast_kwh = None (we don't have snapshots from that time)
        assert r["forecast_kwh"] is None, f"Row {i}: missed hour should have forecast_kwh=None, got {r['forecast_kwh']}"
    # Final cumulative counter equals 108.0
    assert store.rows[-1]["real_counter_kwh"] == pytest.approx(108.0)
    # The current collection hour should also have no forecast because the
    # available snapshot contains no usable daytime forecast.


def test_collect_waits_for_forecast_at_startup():
    store = InMemoryPvYieldStore()
    utc_now = datetime.now(timezone.utc)
    store.rows.append(
        {
            "timestamp": (utc_now - timedelta(hours=1)).isoformat(),
            "date": utc_now.strftime("%Y-%m-%d"),
            "hour": (utc_now.hour - 1) % 24,
            "timeframe_id": 2,
            "real_counter_kwh": 100.0,
            "real_delta_kwh": None,
            "forecast_kwh": None,
        }
    )
    autoscaler = PvAutoscaler(
        {"enabled": True, "retention_days": 7, "min_data_hours_required": 1},
        store,
        timezone="UTC",
        auto_start=False,
    )
    fake = FakePvInterface([], time_frame_base=900)
    autoscaler.set_pv_interface(fake)
    setattr(autoscaler, "_PvAutoscaler__fetch_remote_state", lambda source, sensor: "101.0")

    assert autoscaler.collect_if_needed() is False
    assert len(store.rows) == 1

    previous_hour = (datetime.now(timezone.utc).hour - 1) % 24
    current_forecast = [0.0] * 192
    current_forecast[previous_hour * 4 : previous_hour * 4 + 4] = [100.0] * 4
    fake._current_forecast = current_forecast

    assert autoscaler.collect_if_needed() is True
    assert store.rows[-1]["forecast_kwh"] == pytest.approx(0.4)
