# pylint: disable=protected-access
"""
Falling back to cached forecasts while a provider is unreachable.

The regression these guard against: with several installations, each failing one
was handed the *summed* cache by the error handler, the loop added those up and
stored the result as the new cache - fourfold growth every 15-minute cycle with
four installations. After a five-hour DNS outage the PV forecast stood at 2.5e15 W
and every optimizer run came back infeasible. The temperature curve, meanwhile,
dropped to the 15 degC placeholder while still being reported as a real forecast.
"""

from datetime import date, datetime, timedelta

import pytest

from src.interfaces.pv_autoscaler import PvAutoscaler
from src.interfaces.pv_interface import PvInterface

HOURLY = 3600
TODAY = date(2026, 11, 26)


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    """Avoid starting the real background update thread during tests."""

    class DummyThread:
        """Stub that accepts the real constructor call and never runs anything."""

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            """Do nothing - the loop is invoked directly where a test needs it."""

    monkeypatch.setattr("threading.Thread", DummyThread)


def _entry(name):
    return {
        "name": name,
        "lat": 50.0,
        "lon": 8.0,
        "azimuth": 0,
        "tilt": 30,
        "power": 1000,
        "powerInverter": 1000,
        "inverterEfficiency": 0.9,
    }


def _pv(names=("a", "b", "c", "d"), source="openmeteo", **kwargs):
    pv = PvInterface(
        {"source": source},
        [_entry(n) for n in names],
        HOURLY,
        {},
        temperature_forecast_enabled=False,
        timezone="Europe/Berlin",
        **kwargs,
    )
    pv._local_today = lambda: TODAY
    return pv


def _provider(pv, curves, failing):
    """
    Stand-in for the per-installation fetch.

    ``curves`` maps installation name to the curve a success returns; a name in
    ``failing`` fails the way the real providers do - through the shared error
    handler, which hands back the summed cache.
    """

    def fetch(config_entry):
        name = config_entry["name"]
        if name in failing:
            return pv._handle_interface_error(
                "connection_error", f"{name} unreachable", config_entry, "openmeteo"
            )
        pv.pv_forcast_request_error["error"] = None
        return list(curves[name])

    pv._PvInterface__get_pv_forecast = fetch


def _curve(value, length=48):
    return [float(value)] * length


# ── several installations ─────────────────────────────────────────────────────


def test_failed_cycles_do_not_compound_the_forecast():
    pv = _pv()
    curves = {n: _curve(100) for n in "abcd"}
    _provider(pv, curves, failing=())
    assert pv.get_summarized_pv_forecast(scale=False) == _curve(400)

    _provider(pv, curves, failing=set("abcd"))
    for _ in range(20):  # five hours of 15-minute cycles
        assert pv.get_summarized_pv_forecast(scale=False) == _curve(400)

    assert pv.last_successful_pv_forecast == _curve(400)
    assert pv.pv_forcast_request_error["error"] == "connection_error"


def test_one_failing_installation_adds_only_its_own_curve():
    pv = _pv()
    _provider(pv, {"a": _curve(100), "b": _curve(200), "c": _curve(300), "d": _curve(400)}, ())
    pv.get_summarized_pv_forecast(scale=False)

    fresh = {"a": _curve(10), "b": _curve(20), "c": _curve(30), "d": _curve(40)}
    _provider(pv, fresh, failing={"b"})
    # 10 + 200 (b's own last curve) + 30 + 40
    assert pv.get_summarized_pv_forecast(scale=False) == _curve(280)


def test_a_failed_cycle_keeps_its_error_even_when_a_later_installation_succeeds():
    pv = _pv()
    curves = {n: _curve(100) for n in "abcd"}
    _provider(pv, curves, ())
    pv.get_summarized_pv_forecast(scale=False)

    _provider(pv, curves, failing={"a"})
    pv.get_summarized_pv_forecast(scale=False)

    assert pv.pv_forcast_request_error["error"] == "connection_error"
    assert "a unreachable" in pv.pv_forcast_request_error["message"]


def test_a_failed_cycle_neither_overwrites_the_cache_nor_resets_the_count():
    pv = _pv()
    _provider(pv, {n: _curve(100) for n in "abcd"}, ())
    pv.get_summarized_pv_forecast(scale=False)
    assert pv.consecutive_failures == 0

    _provider(pv, {n: _curve(1) for n in "abcd"}, failing={"d"})
    pv.get_summarized_pv_forecast(scale=False)
    pv.get_summarized_pv_forecast(scale=False)

    assert pv.last_successful_pv_forecast == _curve(400)
    assert pv.consecutive_failures == 2


def test_a_full_success_after_an_outage_replaces_the_cache_and_resets_the_count():
    pv = _pv()
    _provider(pv, {n: _curve(100) for n in "abcd"}, ())
    pv.get_summarized_pv_forecast(scale=False)
    _provider(pv, {n: _curve(100) for n in "abcd"}, failing=set("abcd"))
    pv.get_summarized_pv_forecast(scale=False)

    _provider(pv, {n: _curve(50) for n in "abcd"}, ())
    assert pv.get_summarized_pv_forecast(scale=False) == _curve(200)
    assert pv.last_successful_pv_forecast == _curve(200)
    assert pv.consecutive_failures == 0
    assert pv.pv_forcast_request_error["error"] is None


def test_an_installation_without_any_good_curve_is_left_out():
    pv = _pv()
    _provider(pv, {"a": _curve(10), "b": _curve(20), "c": _curve(30), "d": _curve(40)}, {"c"})
    # c never succeeded: it is left out instead of truncating the whole sum to nothing
    assert pv.get_summarized_pv_forecast(scale=False) == _curve(70)


def test_a_single_installation_falls_back_to_the_summed_cache():
    pv = _pv(names=("only",))
    pv.last_successful_pv_forecast = _curve(7)
    _provider(pv, {}, failing={"only"})
    assert pv.get_summarized_pv_forecast(scale=False) == _curve(7)


# ── the cache across midnight ─────────────────────────────────────────────────


def _two_days(today_value, tomorrow_value, slots=24):
    return [float(today_value)] * slots + [float(tomorrow_value)] * slots


def test_reanchor_keeps_a_curve_from_today():
    pv = _pv()
    assert pv._reanchor_cached_series(_two_days(1, 2), TODAY) == _two_days(1, 2)


def test_reanchor_shifts_yesterdays_curve_and_repeats_its_second_day():
    pv = _pv()
    yesterday = TODAY - timedelta(days=1)
    assert pv._reanchor_cached_series(_two_days(1, 2), yesterday) == _two_days(2, 2)


def test_reanchor_drops_a_curve_older_than_a_day():
    pv = _pv()
    assert pv._reanchor_cached_series(_two_days(1, 2), TODAY - timedelta(days=2)) == []


def test_reanchor_works_at_quarter_hour_resolution():
    pv = _pv()
    pv.time_frame_base = 900
    curve = [1.0] * 96 + [2.0] * 96
    assert pv._reanchor_cached_series(curve, TODAY - timedelta(days=1)) == [2.0] * 192


def test_a_failing_installation_uses_its_curve_from_yesterday_shifted():
    pv = _pv()
    pv._local_today = lambda: TODAY - timedelta(days=1)
    _provider(pv, {n: _two_days(1, 2) for n in "abcd"}, ())
    pv.get_summarized_pv_forecast(scale=False)

    pv._local_today = lambda: TODAY
    _provider(pv, {n: _two_days(1, 2) for n in "abcd"}, failing=set("abcd"))
    assert pv.get_summarized_pv_forecast(scale=False) == _two_days(8, 8)


def test_the_loop_uses_the_default_when_the_cache_is_too_old(monkeypatch):
    pv = _pv()
    pv._local_today = lambda: TODAY - timedelta(days=3)
    _provider(pv, {n: _curve(100) for n in "abcd"}, ())
    pv.get_summarized_pv_forecast(scale=False)
    pv.pv_forcast_array = _curve(400)
    pv.pv_forcast_array_raw = _curve(400)

    pv._local_today = lambda: TODAY
    _provider(pv, {n: _curve(100) for n in "abcd"}, failing=set("abcd"))
    monkeypatch.setattr(pv, "apply_autoscaling", list)
    _run_one_loop(pv)

    default = pv._PvInterface__get_default_pv_forcast(1000)
    assert pv.pv_forcast_array == default
    assert pv.pv_forcast_array_raw == default


def test_a_single_source_failure_is_served_reanchored(monkeypatch):
    pv = PvInterface(
        {"source": "timeseries", "data_url": "http://x"},
        [_entry("roof")],
        HOURLY,
        {},
        temperature_forecast_enabled=False,
        timezone="Europe/Berlin",
    )
    pv.configuration_valid = True
    pv._local_today = lambda: TODAY
    pv.last_successful_pv_forecast = _two_days(1, 2)
    pv._last_successful_pv_forecast_date = TODAY - timedelta(days=1)

    def failing_fetch():
        pv._handle_interface_error("request_failed", "down", "timeseries", "timeseries")
        return pv.last_successful_pv_forecast

    pv._PvInterface__get_pv_forecast_timeseries = failing_fetch
    assert pv.get_summarized_pv_forecast(scale=False) == _two_days(2, 2)
    # the stale copy is not stored back as if it were a fresh fetch
    assert pv._last_successful_pv_forecast_date == TODAY - timedelta(days=1)


# ── temperature ───────────────────────────────────────────────────────────────


def _run_one_loop(pv):
    calls = {"n": 0}

    def is_set_once():
        calls["n"] += 1
        return calls["n"] > 1

    pv._stop_event.is_set = is_set_once
    pv._stop_event.wait = lambda timeout=None: True
    pv._PvInterface__update_pv_state_loop()


def _temperature_pv(monkeypatch, fetch_result):
    pv = PvInterface(
        {"source": "openmeteo"},
        [_entry("roof")],
        HOURLY,
        {},
        temperature_forecast_enabled=True,
        timezone="Europe/Berlin",
    )
    pv._local_today = lambda: TODAY
    monkeypatch.setattr(pv, "get_summarized_pv_forecast", lambda scale=False: [100.0])
    monkeypatch.setattr(pv, "apply_autoscaling", list)

    def fetch(_config):
        if fetch_result:
            return list(fetch_result)
        pv.consecutive_temp_failures += 1
        return []

    pv._PvInterface__fetch_temperature = fetch
    return pv


def test_a_failed_temperature_fetch_keeps_the_last_real_curve(monkeypatch):
    pv = _temperature_pv(monkeypatch, fetch_result=None)
    pv.last_successful_temp_forecast = [1.0] * 48
    pv._last_successful_temp_forecast_date = TODAY

    _run_one_loop(pv)

    assert pv.temp_forecast_array == [1.0] * 48
    assert pv.has_real_temperature_forecast() is True


def test_a_kept_temperature_curve_from_yesterday_is_shifted(monkeypatch):
    pv = _temperature_pv(monkeypatch, fetch_result=None)
    pv.last_successful_temp_forecast = _two_days(5, -2)
    pv._last_successful_temp_forecast_date = TODAY - timedelta(days=1)

    _run_one_loop(pv)

    assert pv.temp_forecast_array == _two_days(-2, -2)


def test_past_the_failure_limit_the_placeholder_is_not_reported_as_real(monkeypatch):
    pv = _temperature_pv(monkeypatch, fetch_result=None)
    pv.last_successful_temp_forecast = [1.0] * 48
    pv._last_successful_temp_forecast_date = TODAY
    pv.consecutive_temp_failures = pv.max_failures

    _run_one_loop(pv)

    assert pv.temp_forecast_array == [15.0] * 48
    assert pv.has_real_temperature_forecast() is False


def test_a_successful_fetch_clears_the_placeholder_flag(monkeypatch):
    pv = _temperature_pv(monkeypatch, fetch_result=[3.0] * 48)
    pv.last_successful_temp_forecast = [3.0] * 48
    pv._temp_forecast_is_placeholder = True

    _run_one_loop(pv)

    assert pv.temp_forecast_array == [3.0] * 48
    assert pv.has_real_temperature_forecast() is True


# ── the autoscaler's history ──────────────────────────────────────────────────


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def get_latest_record(self):
        return self.rows[-1] if self.rows else None

    def purge_old_records(self, retention_days):
        """Keep everything."""

    def get_history_last_n_days(self, _n):
        return list(self.rows)


def _day(date_str, forecast_by_hour=None):
    rows = []
    for hour in range(24):
        producing = 8 <= hour < 16
        forecast = 6.0 if producing else 0.0
        if forecast_by_hour and hour in forecast_by_hour:
            forecast = forecast_by_hour[hour]
        rows.append(
            {
                "timestamp": f"{date_str}T{hour:02d}:00:00",
                "date": date_str,
                "hour": hour,
                "real_counter_kwh": None,
                "real_delta_kwh": 4.8 if producing else 0.0,
                "forecast_kwh": forecast,
            }
        )
    return rows


def _scaler(rows, config=None):
    scaler = PvAutoscaler(
        {
            "enabled": True,
            "retention_days": 14,
            "min_data_hours_required": 1,
            "min_scale_factor": 0.2,
            "max_scale_factor": 2.5,
        },
        _Store(rows),
        auto_start=False,
    )
    if config is not None:

        class _Iface:
            pass

        iface = _Iface()
        iface.config = config
        scaler.set_pv_interface(iface)
    return scaler


def test_a_corrupted_forecast_hour_does_not_drag_its_timeframe_down():
    rows = _day("2026-11-24") + _day("2026-11-25", forecast_by_hour={13: 2.7e9})
    factors = _scaler(rows).compute_timeframe_scaling_factors()
    # Without the guard the 12-16 factor is pinned to the 0.2 floor.
    assert factors[3] == 0.8
    assert factors[2] == 0.8


def test_the_corrupted_timeframe_is_missing_from_that_days_history():
    rows = _day("2026-11-24") + _day("2026-11-25", forecast_by_hour={13: 2.7e9})
    _, forecast, _, _, _ = _scaler(rows)._aggregate_by_day_and_timeframe(
        rows, lambda d: True
    )
    assert 3 not in forecast["2026-11-25"]
    assert forecast["2026-11-25"][2] == 24.0
    assert forecast["2026-11-24"][3] == 24.0


def test_the_plausibility_bound_follows_the_installed_power():
    big = [{"power": 20000}, {"power": 20000}]
    scaler = _scaler([], config=big)
    assert scaler._implausible_forecast_kwh() == 400.0
    assert _scaler([], config=[{"power": 1000}])._implausible_forecast_kwh() == 50.0
    assert _scaler([])._implausible_forecast_kwh() == 50.0


def test_an_optimistic_but_possible_forecast_still_counts():
    rows = _day("2026-11-24", forecast_by_hour={12: 40.0})
    _, forecast, _, _, _ = _scaler(rows, config=[{"power": 10000}])._aggregate_by_day_and_timeframe(
        rows, lambda d: True
    )
    assert forecast["2026-11-24"][3] == 58.0
