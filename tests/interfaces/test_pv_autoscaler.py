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


def _day_rows(date_str, tf2_actual, tf2_forecast):
    """One day of rows with production only in timeframe 2 (hours 6-11)."""
    rows = []
    for hour in range(24):
        in_tf2 = 6 <= hour < 12
        rows.append(
            {
                "timestamp": f"{date_str}T{hour:02d}:00:00+00:00",
                "date": date_str,
                "hour": hour,
                "timeframe_id": (hour // 6) + 1,
                "real_counter_kwh": None,
                "real_delta_kwh": (tf2_actual / 6.0) if in_tf2 else 0.0,
                "forecast_kwh": (
                    (tf2_forecast / 6.0) if in_tf2 and tf2_forecast is not None else
                    (None if in_tf2 else 0.0)
                ),
                "local_date": date_str,
                "local_hour": hour,
                "local_offset_minutes": 0,
            }
        )
    return rows


def _autoscaler(store, today="2026-08-20", **overrides):
    cfg = {
        "enabled": True,
        "retention_days": 7,
        "min_data_hours_required": 1,
        "min_scale_factor": 0.2,
        "max_scale_factor": 2.5,
    }
    cfg.update(overrides)
    autoscaler = PvAutoscaler(cfg, store, auto_start=False)
    autoscaler._today_local_iso = lambda: today
    return autoscaler


@pytest.mark.parametrize(
    "actual, forecast, expected",
    [
        (12.0, 6.0, 2.0),     # measured double the forecast
        (3.0, 6.0, 0.5),      # measured half the forecast
        (60.0, 6.0, 2.5),     # ratio 10.0 clamped to max_scale_factor
        (0.6, 6.0, 0.2),      # ratio 0.1 clamped to min_scale_factor
    ],
)
def test_scale_factor_is_clamped_to_configured_bounds(actual, forecast, expected):
    """The ratio is applied as measured, but never outside min/max_scale_factor."""
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-19", actual, forecast)
    autoscaler = _autoscaler(store)

    assert autoscaler.compute_timeframe_scaling_factors()[2] == pytest.approx(expected)


def test_inverted_scale_bounds_are_swapped():
    """min > max would otherwise pin every factor to a single value."""
    store = InMemoryPvYieldStore()
    autoscaler = _autoscaler(store, min_scale_factor=2.0, max_scale_factor=0.5)
    assert autoscaler.min_scale_factor == 0.5
    assert autoscaler.max_scale_factor == 2.0


def test_days_without_forecast_do_not_inflate_the_factor():
    """
    A day whose forecast was never recorded must be skipped, not counted as zero.

    Gap reconstruction stores real_delta_kwh with forecast_kwh NULL. Adding that yield
    to the numerator while contributing nothing to the denominator would raise the
    multiplier precisely when collection has been failing.
    """
    store = InMemoryPvYieldStore()
    # Three well-measured days: forecast was accurate, so the true ratio is 1.0.
    for day in ("2026-08-17", "2026-08-18", "2026-08-19"):
        store.rows.extend(_day_rows(day, 6.0, 6.0))
    # Two reconstructed days: yield known, forecast unknown.
    for day in ("2026-08-15", "2026-08-16"):
        store.rows.extend(_day_rows(day, 6.0, None))

    autoscaler = _autoscaler(store)

    assert autoscaler.compute_timeframe_scaling_factors()[2] == pytest.approx(1.0)


def test_today_is_collected_but_excluded_from_the_factor():
    """Today is still in progress, so scoring it would bias toward the morning."""
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-19", 6.0, 6.0) + _day_rows("2026-08-20", 1.0, 6.0)
    autoscaler = _autoscaler(store, today="2026-08-20")

    # Only the 2026-08-19 pair (6.0/6.0) counts.
    assert autoscaler.compute_timeframe_scaling_factors()[2] == pytest.approx(1.0)
    partial = autoscaler.get_todays_partial_data()
    assert partial["date"] == "2026-08-20"
    assert partial["hours_collected"] == 24


def test_near_zero_forecast_stays_neutral():
    """A night timeframe must not amplify noise through a near-zero denominator."""
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-19", 6.0, 6.0)
    autoscaler = _autoscaler(store)

    factors = autoscaler.compute_timeframe_scaling_factors()
    # Timeframes 1, 3 and 4 recorded 0.0 actual against 0.0 forecast.
    assert factors[1] == 1.0 and factors[3] == 1.0 and factors[4] == 1.0


def test_factors_are_seeded_from_the_database_at_construction():
    """
    A restart must not run unscaled until the next hour boundary.

    The factors are only recomputed after a successful hourly insert, so without
    seeding at construction a restart with a full history still sends EOS an
    uncorrected forecast for up to an hour.
    """
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-19", 3.0, 6.0)

    # _today_local_iso cannot be patched before __init__ runs, so pick a "today" far
    # from the seeded date by using a date the rows do not contain.
    autoscaler = PvAutoscaler(
        {"enabled": True, "retention_days": 7, "min_data_hours_required": 1},
        store,
        auto_start=False,
    )

    assert autoscaler.get_scale_factors()[2] == pytest.approx(0.5)


def test_apply_scaling_maps_slots_to_timeframes_at_15min_resolution():
    """Slot 0 is local midnight, so a 15-minute array has four slots per hour."""
    store = InMemoryPvYieldStore()
    autoscaler = _autoscaler(store)
    autoscaler._scale_factors = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0}

    scaled = autoscaler.apply_scaling([10.0] * 96, time_frame_base=900)

    assert scaled[0] == 10.0        # 00:00 -> timeframe 1
    assert scaled[24] == 20.0       # 06:00 -> timeframe 2
    assert scaled[48] == 30.0       # 12:00 -> timeframe 3
    assert scaled[72] == 40.0       # 18:00 -> timeframe 4


def test_update_config_recomputes_and_toggles_collection():
    """Config changes must take effect immediately, not at the next hourly insert."""
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-19", 6.0, 6.0)
    autoscaler = _autoscaler(store, min_scale_factor=0.2, max_scale_factor=2.5)
    autoscaler.compute_timeframe_scaling_factors()

    # Forcing max below the computed 1.0 must re-clamp the cached factor right away.
    autoscaler.update_config(max_scale_factor=0.5)
    assert autoscaler.get_scale_factors()[2] == pytest.approx(0.5)

    # Toggling `enabled` must start and stop the collection thread.
    assert autoscaler.is_running() is False
    autoscaler.update_config(enabled=True)
    assert autoscaler.is_running() is True
    autoscaler.update_config(enabled=False)
    assert autoscaler.is_running() is False


# ---------------------------------------------------------------------------
# Aggregated history — the payload behind /api/pv_autoscaling/status
# ---------------------------------------------------------------------------

def test_aggregated_history_excludes_today_and_sorts_newest_first():
    store = InMemoryPvYieldStore()
    store.rows = (
        _day_rows("2026-08-18", 5.0, 6.0)
        + _day_rows("2026-08-19", 6.0, 6.0)
        + _day_rows("2026-08-20", 1.0, 6.0)
    )
    autoscaler = _autoscaler(store, today="2026-08-20")

    history = autoscaler.get_aggregated_history()

    assert [d["date"] for d in history["days"]] == ["2026-08-19", "2026-08-18"]
    assert history["days"][0]["hours_collected"] == 24
    assert history["days"][0]["total_actual_kwh"] == pytest.approx(6.0)


def test_aggregated_history_falls_back_to_legacy_date_columns():
    """
    Rows predating the local_* columns must still land on their real day.

    Grouping on a NULL local_date produced a "no date" bucket that the overlay
    rendered as Jan 1 1970.
    """
    store = InMemoryPvYieldStore()
    rows = _day_rows("2026-08-19", 6.0, 6.0)
    for row in rows:
        row["local_date"] = None
        row["local_hour"] = None
    store.rows = rows
    autoscaler = _autoscaler(store, today="2026-08-20")

    history = autoscaler.get_aggregated_history()

    assert [d["date"] for d in history["days"]] == ["2026-08-19"]
    assert all(d["date"] for d in history["days"])


def test_aggregated_history_summary_counts_only_paired_days():
    """The summary must reflect the same days the scale factors were derived from."""
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-18", 6.0, 6.0) + _day_rows("2026-08-19", 6.0, None)
    autoscaler = _autoscaler(store, today="2026-08-20")

    summary = autoscaler.get_aggregated_history()["summary_by_timeframe"]

    # Only 2026-08-18 has both an actual and a forecast for timeframe 2.
    assert summary["2"]["days"] == 1
    assert summary["2"]["actual_kwh"] == pytest.approx(6.0)


def test_status_reports_liveness_and_configuration():
    """The status payload carries what the UI needs to distinguish healthy from stalled."""
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-19", 6.0, 6.0)
    autoscaler = _autoscaler(store, min_data_hours_required=12, retention_days=5)

    status = autoscaler.get_status()

    assert status["enabled"] is True
    assert status["running"] is False
    assert status["min_data_hours_required"] == 12
    assert status["retention_days"] == 5
    assert status["last_error"] is None
    assert set(status["scale_factors"]) == {"1", "2", "3", "4"}


def test_empty_store_degrades_to_empty_history():
    store = InMemoryPvYieldStore()
    autoscaler = _autoscaler(store)

    assert autoscaler.get_aggregated_history() == {"days": [], "summary_by_timeframe": {}}
    assert autoscaler.get_todays_partial_data() == {}


# ---------------------------------------------------------------------------
# Provenance — restored days must not read as locally measured
# ---------------------------------------------------------------------------

def test_aggregated_history_labels_days_measured_by_default():
    """Rows with no origin came from this system's own meter."""
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-19", 6.0, 6.0)
    autoscaler = _autoscaler(store, today="2026-08-20")

    assert autoscaler.get_aggregated_history()["days"][0]["origin"] == "measured"


def test_aggregated_history_labels_a_seeded_day():
    """A day made entirely of restored rows is not a measurement."""
    store = InMemoryPvYieldStore()
    rows = _day_rows("2026-08-19", 6.0, 6.0)
    for row in rows:
        row["origin"] = "seeded"
    store.rows = rows
    autoscaler = _autoscaler(store, today="2026-08-20")

    assert autoscaler.get_aggregated_history()["days"][0]["origin"] == "seeded"


def test_a_restored_day_topped_up_by_real_collection_reads_as_measured():
    """One genuinely measured hour is enough to stop calling the day seeded."""
    store = InMemoryPvYieldStore()
    rows = _day_rows("2026-08-19", 6.0, 6.0)
    for row in rows:
        row["origin"] = "seeded"
    rows[8]["origin"] = None
    store.rows = rows
    autoscaler = _autoscaler(store, today="2026-08-20")

    assert autoscaler.get_aggregated_history()["days"][0]["origin"] == "measured"


def test_status_counts_restored_hours():
    """The panel needs to say how much of the window was not measured here."""
    store = InMemoryPvYieldStore()
    measured = _day_rows("2026-08-19", 6.0, 6.0)
    restored = _day_rows("2026-08-18", 6.0, 6.0)
    for row in restored:
        row["origin"] = "imported"
    store.rows = measured + restored
    autoscaler = _autoscaler(store, today="2026-08-20")

    status = autoscaler.get_status()

    assert status["total_hours_recorded"] == 48
    assert status["restored_hours"] == 24


def test_status_reports_no_restored_hours_for_a_normal_install():
    store = InMemoryPvYieldStore()
    store.rows = _day_rows("2026-08-19", 6.0, 6.0)
    autoscaler = _autoscaler(store, today="2026-08-20")

    assert autoscaler.get_status()["restored_hours"] == 0
