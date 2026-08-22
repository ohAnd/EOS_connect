import pytest
from datetime import datetime, timedelta, timezone

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
        self._current_forecast_raw = array
        self.time_frame_base = time_frame_base

    def get_current_pv_forecast(self, scale=True):
        if scale:
            return self._current_forecast
        return self._current_forecast_raw

# All collection logic derives local time from PvAutoscaler._local_now(). Pinning it
# keeps these tests deterministic: without it a run that crosses an hour boundary
# between two datetime.now() calls seeds one hour and collects another.
FIXED_NOW = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
PREV_HOUR = 9


def pin_clock(autoscaler, now=FIXED_NOW):
    """Freeze the autoscaler's notion of local time."""
    autoscaler._local_now = lambda: now
    return autoscaler


def _seed(store, hours_ago=1, counter=100.0, now=FIXED_NOW):
    """Record one already-collected hour `hours_ago` before the pinned clock."""
    when = now - timedelta(hours=hours_ago)
    store.rows.append({
        "timestamp": when.isoformat(),
        "date": when.strftime("%Y-%m-%d"),
        "hour": when.hour,
        "timeframe_id": (when.hour // 6) + 1,
        "real_counter_kwh": counter,
        "real_delta_kwh": None,
        "forecast_kwh": None,
        "local_date": when.strftime("%Y-%m-%d"),
        "local_hour": when.hour,
        "local_offset_minutes": 0,
    })


def _autoscaler(store, forecast, time_frame_base=3600, counter="101.0", now=FIXED_NOW):
    autoscaler = PvAutoscaler(
        {"enabled": True, "retention_days": 7, "min_data_hours_required": 1},
        store,
        timezone="UTC",
        auto_start=False,
    )
    pin_clock(autoscaler, now)
    fake = FakePvInterface(forecast, time_frame_base=time_frame_base)
    autoscaler.set_pv_interface(fake)
    setattr(autoscaler, "_PvAutoscaler__fetch_remote_state",
            lambda source, sensor: str(counter))
    return autoscaler, fake


def test_collect_if_needed_populates_forecast_kwh_and_recomputes():
    """Hourly Wh slots for the previous hour are stored as kWh."""
    store = InMemoryPvYieldStore()
    _seed(store)

    arr = [0.0] * 48
    arr[PREV_HOUR] = 6000.0        # 6 kWh for the hour we are recording
    arr[PREV_HOUR + 1] = 7000.0    # the next hour must not leak in
    autoscaler, _ = _autoscaler(store, arr, counter=104.8)

    assert autoscaler.collect_if_needed() is True
    assert store.rows[-1]["forecast_kwh"] == pytest.approx(6.0)
    assert store.rows[-1]["local_hour"] == PREV_HOUR
    assert isinstance(autoscaler.get_scale_factors(), dict)


def test_collect_if_needed_sums_four_15_minute_slots_to_kwh():
    """At 15-minute resolution the previous hour spans four slots."""
    store = InMemoryPvYieldStore()
    _seed(store)

    arr = [0.0] * 192
    arr[PREV_HOUR * 4: PREV_HOUR * 4 + 4] = [100.0, 200.0, 300.0, 400.0]
    autoscaler, _ = _autoscaler(store, arr, time_frame_base=900)

    assert autoscaler.collect_if_needed() is True
    assert store.rows[-1]["forecast_kwh"] == pytest.approx(1.0)


def test_collect_if_needed_uses_current_forecast_array():
    """The stored forecast is read from the live array, at the previous hour's index."""
    store = InMemoryPvYieldStore()
    _seed(store)

    arr = [0.0] * 48
    arr[PREV_HOUR] = 4000.0
    autoscaler, _ = _autoscaler(store, arr)

    assert autoscaler.collect_if_needed() is True
    assert store.rows[-1]["forecast_kwh"] == pytest.approx(4.0)


def test_collect_if_needed_distributes_over_missed_hours():
    """A gap is back-filled evenly, with no forecast claimed for the missed hours."""
    store = InMemoryPvYieldStore()
    _seed(store, hours_ago=4)

    autoscaler, _ = _autoscaler(store, [0.0] * 48, counter=108.0)

    assert autoscaler.collect_if_needed() is True
    # Seed plus three reconstructed hours: a 4h gap leaves 3 hours unrecorded.
    assert len(store.rows) == 4
    for row in store.rows[-3:]:
        assert row["real_delta_kwh"] == pytest.approx(8.0 / 3)
        # No historical forecast snapshot exists for a missed hour, and claiming one
        # would let the day into the ratio with a fabricated denominator.
        assert row["forecast_kwh"] is None
    assert store.rows[-1]["real_counter_kwh"] == pytest.approx(108.0)


def test_collect_waits_for_forecast_at_startup():
    """
    No row is written until the forecast array covers a full day.

    The readiness check runs before the counter fetch, so waiting costs no HTTP
    request and does not consume the hour's collection slot.
    """
    store = InMemoryPvYieldStore()
    _seed(store)

    autoscaler, fake = _autoscaler(store, [], time_frame_base=900)

    assert autoscaler.collect_if_needed() is False
    assert len(store.rows) == 1

    ready = [0.0] * 192
    ready[PREV_HOUR * 4: PREV_HOUR * 4 + 4] = [100.0] * 4
    fake._current_forecast_raw = ready
    fake._current_forecast = ready

    assert autoscaler.collect_if_needed() is True
    assert store.rows[-1]["forecast_kwh"] == pytest.approx(0.4)


def test_collect_accepts_the_24_hour_default_curve():
    """
    The built-in default curve is one day long, not the 48h providers publish.

    Requiring the full provider horizon made the feature a permanent no-op for
    `source: default` and during any provider fallback.
    """
    store = InMemoryPvYieldStore()
    _seed(store)

    arr = [0.0] * 24
    arr[PREV_HOUR] = 3000.0
    autoscaler, _ = _autoscaler(store, arr)

    assert autoscaler.collect_if_needed() is True
    assert store.rows[-1]["forecast_kwh"] == pytest.approx(3.0)


def test_fetch_failure_is_recorded_and_surfaced():
    """A broken sensor must be visible in the status, not only in the log."""
    store = InMemoryPvYieldStore()
    _seed(store)

    autoscaler, _ = _autoscaler(store, [0.0] * 48)
    autoscaler.sensor_entity_id = "sensor.typo"

    def boom(source, sensor):
        raise ValueError("not found")

    setattr(autoscaler, "_PvAutoscaler__fetch_remote_state", boom)

    assert autoscaler.collect_if_needed() is False
    status = autoscaler.get_status()
    assert status["consecutive_failures"] == 1
    assert "sensor.typo" in status["last_error"]
    assert status["last_error_timestamp"] is not None


def test_successful_collection_clears_a_previous_failure():
    store = InMemoryPvYieldStore()
    _seed(store)

    arr = [0.0] * 48
    arr[PREV_HOUR] = 1000.0
    autoscaler, _ = _autoscaler(store, arr)
    autoscaler._record_failure("earlier problem")

    assert autoscaler.collect_if_needed() is True
    status = autoscaler.get_status()
    assert status["last_error"] is None
    assert status["consecutive_failures"] == 0
