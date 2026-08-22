"""
DST transition tests for PvAutoscaler.

These drive the collector across real transitions by injecting local time through the
`_local_now` seam, and they use a store fake that reproduces the production upsert
(keyed on the UTC timestamp) so a de-duplication bug shows up here rather than only in
the database.

Scenarios (Europe/Berlin):
- Spring forward: 2026-03-29, 02:00 -> 03:00, a 23-hour day, UTC+1 -> UTC+2
- Fall back:      2026-10-25, 03:00 -> 02:00, a 25-hour day, UTC+2 -> UTC+1
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytz

from src.interfaces.pv_autoscaler import PvAutoscaler

BERLIN = pytz.timezone("Europe/Berlin")


class InMemoryPvYieldStore:
    """Mirror of PvYieldStore's upsert semantics: one row per UTC timestamp."""

    def __init__(self):
        self.rows = []

    def get_latest_record(self):
        if not self.rows:
            return None
        return max(self.rows, key=lambda r: r["timestamp"])

    def insert_hourly_record(self, timestamp, date, hour, timeframe_id, real_counter_kwh,
                             real_delta_kwh, forecast_kwh, local_date=None,
                             local_hour=None, local_offset_minutes=None):
        new = {
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
        }
        for row in self.rows:
            if row["timestamp"] == timestamp:
                # COALESCE semantics: a NULL never overwrites a stored value.
                for key, value in new.items():
                    if value is not None:
                        row[key] = value
                return
        self.rows.append(new)

    def purge_old_records(self, days: int = 7):
        return 0

    def get_history_last_n_days(self, n: int = 7):
        return list(self.rows)


class FakePvInterface:
    def __init__(self, array, time_frame_base=3600):
        self._array = array
        self.time_frame_base = time_frame_base

    def get_current_pv_forecast(self, scale=True):
        return self._array


def make_autoscaler(store, now_local, counter, forecast=None, **cfg_overrides):
    """Build an autoscaler pinned to `now_local` with a stubbed counter reading."""
    cfg = {"enabled": True, "retention_days": 7, "min_data_hours_required": 1}
    cfg.update(cfg_overrides)
    autoscaler = PvAutoscaler(cfg, store, timezone="Europe/Berlin", auto_start=False)
    autoscaler.set_pv_interface(FakePvInterface(forecast if forecast is not None else [0.0] * 48))
    autoscaler._local_now = lambda: now_local
    setattr(
        autoscaler,
        "_PvAutoscaler__fetch_remote_state",
        lambda source, sensor: str(counter),
    )
    return autoscaler


def seed(store, local_dt, counter, delta=None, forecast=None):
    """Insert one already-recorded hour at `local_dt`."""
    utc = local_dt.astimezone(timezone.utc)
    store.insert_hourly_record(
        timestamp=utc.isoformat(),
        date=local_dt.strftime("%Y-%m-%d"),
        hour=local_dt.hour,
        timeframe_id=(local_dt.hour // 6) + 1,
        real_counter_kwh=counter,
        real_delta_kwh=delta,
        forecast_kwh=forecast,
        local_date=local_dt.strftime("%Y-%m-%d"),
        local_hour=local_dt.hour,
        local_offset_minutes=int(local_dt.utcoffset().total_seconds() / 60),
    )


def test_spring_forward_skips_the_nonexistent_hour():
    """
    Collecting at 03:00 CEST records the hour that actually preceded it.

    Local 02:00 does not exist on this day, so stepping back one hour from 03:00 CEST
    must land on 01:00 CET (UTC+1) - not on a wall-clock "02:00" that never happened.
    """
    store = InMemoryPvYieldStore()
    seed(store, BERLIN.localize(datetime(2026, 3, 29, 0, 0)), counter=100.0, delta=0.0)

    now = BERLIN.localize(datetime(2026, 3, 29, 3, 0))
    autoscaler = make_autoscaler(store, now, counter=104.0)
    autoscaler._previous_counter_kwh = 100.0

    assert autoscaler.collect_if_needed() is True

    recorded = store.rows[-1]
    # 03:00 CEST is 01:00 UTC; one hour earlier is 00:00 UTC = 01:00 CET local.
    assert recorded["local_hour"] == 1
    assert recorded["local_offset_minutes"] == 60
    assert recorded["local_date"] == "2026-03-29"
    assert recorded["real_counter_kwh"] == pytest.approx(104.0)
    assert all(0 <= r["local_hour"] <= 23 for r in store.rows)


def test_fall_back_keeps_both_occurrences_of_the_repeated_hour():
    """
    The 25-hour day has two local 02:00 hours; both must survive as separate rows.

    They differ only in UTC offset (+2 then +1), so a store keyed on
    (local_date, local_hour) would collapse them and silently lose an hour of yield.
    """
    store = InMemoryPvYieldStore()

    # First 02:00 (CEST, UTC+2): collected at 03:00 CEST, which is 01:00 UTC.
    first_boundary = BERLIN.normalize(
        BERLIN.localize(datetime(2026, 10, 25, 2, 0), is_dst=True) + timedelta(hours=1)
    )
    autoscaler = make_autoscaler(store, first_boundary, counter=105.0)
    autoscaler._previous_counter_kwh = 100.0
    assert autoscaler.collect_if_needed() is True

    # Second 02:00 (CET, UTC+1): collected an hour later, at 03:00 CET = 02:00 UTC.
    second_boundary = BERLIN.normalize(first_boundary + timedelta(hours=1))
    autoscaler2 = make_autoscaler(store, second_boundary, counter=112.0)
    autoscaler2._previous_counter_kwh = 105.0
    assert autoscaler2.collect_if_needed() is True

    two_am = [r for r in store.rows if r["local_hour"] == 2]
    assert len(two_am) == 2, f"expected both 02:00 hours, got {two_am}"
    assert {r["local_offset_minutes"] for r in two_am} == {120, 60}
    # Neither hour's measured yield was absorbed into the other.
    assert sorted(r["real_delta_kwh"] for r in two_am) == pytest.approx([5.0, 7.0])
    assert len({r["timestamp"] for r in two_am}) == 2


def test_scale_factors_computed_across_a_dst_transition():
    """Aggregation stays correct when the window spans a transition."""
    store = InMemoryPvYieldStore()
    start = BERLIN.localize(datetime(2026, 3, 26, 12, 0))

    # Four days straddling the 2026-03-29 spring-forward, midday (timeframe 3).
    for day in range(4):
        for hour in range(4):
            local = BERLIN.normalize(start + timedelta(days=day, hours=hour))
            seed(store, local, counter=100.0 + day, delta=4.0, forecast=5.0)

    now = BERLIN.localize(datetime(2026, 3, 30, 8, 0))
    autoscaler = make_autoscaler(store, now, counter=200.0)

    factors = autoscaler.compute_timeframe_scaling_factors()

    assert set(factors) == {1, 2, 3, 4}
    # Timeframe 3 (12:00-17:59) measured 4.0 against a 5.0 forecast every day.
    assert factors[3] == pytest.approx(0.8)
    # Timeframes with no paired data stay neutral.
    assert factors[1] == 1.0 and factors[2] == 1.0


def test_missed_hours_are_capped_rather_than_reconstructed_forever():
    """
    A months-old last record must not spawn thousands of synthetic rows.

    Without a cap, one reading after a long outage back-fills every intervening hour
    with an evenly-divided delta that carries no real information.
    """
    store = InMemoryPvYieldStore()
    seed(store, BERLIN.localize(datetime(2026, 1, 1, 0, 0)), counter=100.0, delta=0.0)

    now = BERLIN.localize(datetime(2026, 6, 1, 12, 0))
    autoscaler = make_autoscaler(store, now, counter=900.0)
    autoscaler._previous_counter_kwh = 100.0

    assert autoscaler.collect_if_needed() is True
    # One seeded row plus a single row for the previous hour.
    assert len(store.rows) == 2
