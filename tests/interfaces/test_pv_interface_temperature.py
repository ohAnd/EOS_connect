# pylint: disable=protected-access
"""
Tests for the outside-temperature forecast in ``PvInterface`` — issue #289.

The temperature curve is an optional extra input for EOS: it makes the model more
precise, but a held or defaulted curve is always a usable answer.  Everything here is
about that asymmetry — the temperature fetch must never be able to damage the PV
forecast, degrade the configuration, or shout about a failure the cache absorbed.
"""

import logging
from datetime import datetime

import pytest

from src.interfaces.pv_interface import (
    TEMP_MAX_RETRIES,
    TEMP_REFRESH_INTERVAL_S,
    TEMP_RETRY_DELAY_S,
    PvInterface,
)

TIME_FRAME_BASE_HOURLY = 3600

FULL_ENTRY = {
    "name": "roof",
    "lat": 50.0,
    "lon": 8.0,
    "azimuth": 90.0,
    "tilt": 10.0,
    "power": 2640,
    "powerInverter": 5000,
    "inverterEfficiency": 0.9,
    "horizon": [10, 20, 10, 15],
}


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    """Avoid starting the real background update thread during tests."""

    class DummyThread:
        """Stub that accepts the real constructor call and never runs anything."""

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            """Do nothing — the loop is invoked directly where a test needs it."""

    monkeypatch.setattr("threading.Thread", DummyThread)


def _pv(config=None, source=None, **kwargs):
    """A PvInterface with the temperature forecast on, no threads started."""
    return PvInterface(
        source if source is not None else {},
        config if config is not None else [dict(FULL_ENTRY)],
        TIME_FRAME_BASE_HOURLY,
        {},
        temperature_forecast_enabled=True,
        timezone="UTC",
        **kwargs,
    )


def _provider_day(key, values):
    """
    One day of hourly provider entries, in the shape the API returns them.

    Dated today: the fetch only keeps entries inside a window that starts at midnight
    today, so a fixed date would be filtered out and every assertion below would be made
    against the empty-array fallback instead of the parsing under test.
    """
    day = datetime.now().strftime("%Y-%m-%d")
    return [
        [
            {"datetime": f"{day}T{hour:02d}:00:00", key: value}
            for hour, value in enumerate(values)
        ]
    ]


def _run_one_update_loop_iteration(pv):
    """Run exactly one iteration of the background update loop."""
    calls = {"n": 0}

    def is_set_once():
        calls["n"] += 1
        return calls["n"] > 1

    pv._stop_event.is_set = is_set_once
    pv._PvInterface__update_pv_state_loop()


# ---------------------------------------------------------------------------
# The request itself: location only
# ---------------------------------------------------------------------------


def test_temperature_request_carries_only_the_location():
    """
    The temperature the provider returns depends on the location and nothing else, so
    the request must not carry the user's panel geometry — least of all the horizon
    list, which is a whole class of failure over a value it cannot influence.
    """
    pv = _pv()

    params = pv._PvInterface__create_temperature_request(dict(FULL_ENTRY))

    assert "horizont" not in params
    assert params["lat"] == 50.0
    assert params["lon"] == 8.0
    assert params["timezone"] == "UTC"
    # Nothing the installation says about its panels survives into the query.
    assert params["azimuth"] != FULL_ENTRY["azimuth"]
    assert params["tilt"] != FULL_ENTRY["tilt"]
    assert params["power"] != FULL_ENTRY["power"]


def test_temperature_request_is_identical_for_the_same_location():
    """Two different installations at one location produce one cacheable query."""
    pv = _pv()
    other = dict(FULL_ENTRY, azimuth=270.0, tilt=45.0, power=9000, horizon=[0] * 12)

    assert pv._PvInterface__create_temperature_request(
        dict(FULL_ENTRY)
    ) == pv._PvInterface__create_temperature_request(other)


def test_pv_request_still_carries_the_installation():
    """The PV path is untouched: it needs every parameter, horizon included."""
    pv = _pv()

    params = pv._PvInterface__create_forecast_request(dict(FULL_ENTRY))

    assert params["horizont"] == "10,20,10,15"
    assert params["azimuth"] == 90.0
    assert params["power"] == 2640


def test_temperature_fetch_uses_the_location_only_request(monkeypatch):
    """And the fetch actually picks that request builder for temperature."""
    pv = _pv()
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(params)
        raise RuntimeError("stop here — only the params are under test")

    monkeypatch.setattr("src.interfaces.pv_interface.requests.get", fake_get)
    monkeypatch.setattr(pv, "_retry_request", lambda req, err, *a, **k: req())

    with pytest.raises(RuntimeError):
        pv._PvInterface__get_pv_forecast_akkudoktor_api(
            tgt_value="temperature", pv_config_entry=dict(FULL_ENTRY)
        )

    assert "horizont" not in seen
    assert seen["lat"] == 50.0


# ---------------------------------------------------------------------------
# Error state kept out of the PV slot
# ---------------------------------------------------------------------------


def test_temperature_failure_leaves_the_pv_error_slot_clean():
    """
    A temperature failure used to be written into ``pv_forcast_request_error`` — the slot
    the update loop reads to decide whether the *PV* fetch failed.
    """
    pv = _pv()

    pv._handle_interface_error(
        "request_failed", "boom", {}, "akkudoktor", target="temperature"
    )

    assert pv.pv_forcast_request_error["error"] is None
    assert pv.temp_forecast_request_error["error"] == "request_failed"
    assert "boom" in pv.temp_forecast_request_error["message"]


def test_temperature_failure_does_not_make_a_good_pv_cycle_look_degraded(monkeypatch):
    """
    Regression for the noisiest symptom in #289.  With ``source: default`` nothing on the
    PV path ever clears the error slot, so one temperature failure made *every* later
    cycle log "Using cached PV forecast due to API error: … for temperature" while the PV
    forecast was in fact fine.
    """
    pv = _pv(config=[], source={"source": "default"})
    monkeypatch.setattr(
        pv,
        "_PvInterface__get_pv_forecast_akkudoktor_api",
        lambda tgt_value, pv_config_entry: [],
    )

    _run_one_update_loop_iteration(pv)
    assert pv.pv_forcast_request_error["error"] is None

    with pytest.MonkeyPatch.context():
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("__main__")
        logger.addHandler(handler)
        try:
            _run_one_update_loop_iteration(pv)
        finally:
            logger.removeHandler(handler)

    assert not [r for r in records if "Using cached PV forecast" in r.getMessage()]


def test_successful_temperature_fetch_does_not_clear_a_pv_error(monkeypatch):
    """
    The mirror image: a good temperature fetch must not wipe a real PV error, or the
    update loop would treat a failed PV cycle as a healthy one.
    """
    pv = _pv()
    pv.pv_forcast_request_error["error"] = "request_failed"
    monkeypatch.setattr(
        pv,
        "_retry_request",
        lambda req, err, *a, **k: _provider_day("temperature", [5.0] * 24),
    )

    pv._PvInterface__get_pv_forecast_akkudoktor_api(
        tgt_value="temperature", pv_config_entry=dict(FULL_ENTRY)
    )

    assert pv.pv_forcast_request_error["error"] == "request_failed"
    assert pv.temp_forecast_request_error["error"] is None


# ---------------------------------------------------------------------------
# No bogus 0 °C slot
# ---------------------------------------------------------------------------




def test_temperature_curve_is_padded_with_the_last_value(monkeypatch):
    """
    The provider's series starts one slot early, so the first entry is dropped and the
    end padded.  For Watts a padded 0 is night-time; for temperature it was a bogus cold
    hour at the end of every curve.
    """
    pv = _pv()
    monkeypatch.setattr(
        pv,
        "_retry_request",
        lambda req, err, *a, **k: _provider_day(
            "temperature", [float(10 + h) for h in range(24)]
        ),
    )

    result = pv._PvInterface__get_pv_forecast_akkudoktor_api(
        tgt_value="temperature", pv_config_entry=dict(FULL_ENTRY)
    )

    assert result[22] == 33.0  # last real reading, first entry having been dropped
    assert result[23] == 33.0  # padded with it, not with 0
    assert 0 not in result[:24]


def test_power_curve_is_still_padded_with_zero(monkeypatch):
    """Watts keep the old rule — a dropped slot there really is dark."""
    pv = _pv()
    day = _provider_day("power", [100.0] * 24)
    monkeypatch.setattr(pv, "_retry_request", lambda req, err, *a, **k: day)

    result = pv._PvInterface__get_pv_forecast_akkudoktor_api(
        tgt_value="power", pv_config_entry=dict(FULL_ENTRY)
    )

    assert result[23] == 0


# ---------------------------------------------------------------------------
# Refresh throttle and retry policy
# ---------------------------------------------------------------------------


def _counting_provider(pv, monkeypatch, values=None):
    """
    Wire *pv* so the update loop runs offline and count real provider fetches.

    The transport is stubbed rather than the whole fetch method: the freshness stamp is
    recorded where a genuine forecast is parsed, so stubbing above that would take the
    behaviour under test out of the picture.
    """
    calls = {"n": 0}
    day = _provider_day("temperature", values or [float(10 + h) for h in range(24)])

    def counting_retry(request_func, error_handler, *args, **kwargs):
        calls["n"] += 1
        return day

    monkeypatch.setattr(pv, "get_summarized_pv_forecast", lambda scale=False: [100.0])
    monkeypatch.setattr(pv, "apply_autoscaling", lambda values_: values_)
    monkeypatch.setattr(pv, "_retry_request", counting_retry)
    return calls


def test_temperature_is_not_refetched_within_the_refresh_interval(monkeypatch):
    """
    The curve is hourly at the source, so refetching it on every 15-minute PV cycle was
    four requests an hour for one new data point.
    """
    pv = _pv()
    calls = _counting_provider(pv, monkeypatch)

    _run_one_update_loop_iteration(pv)
    _run_one_update_loop_iteration(pv)

    assert calls["n"] == 1
    assert pv.temp_forecast_array == pv.last_successful_temp_forecast


def test_a_stale_temperature_forecast_is_refetched(monkeypatch):
    """The throttle saves requests; it must not stop the curve tracking reality."""
    pv = _pv()
    calls = _counting_provider(pv, monkeypatch)

    _run_one_update_loop_iteration(pv)
    pv._last_temp_fetch -= TEMP_REFRESH_INTERVAL_S + 1
    _run_one_update_loop_iteration(pv)

    assert calls["n"] == 2


def test_a_failing_provider_is_retried_on_every_cycle(monkeypatch):
    """
    The throttle only ever holds a *successful* forecast back, so an outage still gets a
    fresh attempt every cycle rather than one an hour.
    """
    pv = _pv()
    calls = {"n": 0}

    def failing_fetch(tgt_value, pv_config_entry):
        calls["n"] += 1
        return []

    monkeypatch.setattr(pv, "get_summarized_pv_forecast", lambda scale=False: [100.0])
    monkeypatch.setattr(pv, "apply_autoscaling", lambda values: values)
    monkeypatch.setattr(pv, "_PvInterface__get_pv_forecast_akkudoktor_api", failing_fetch)

    _run_one_update_loop_iteration(pv)
    _run_one_update_loop_iteration(pv)

    assert calls["n"] == 2


def test_temperature_uses_the_short_retry_policy(monkeypatch):
    """
    Five retries three seconds apart blocked the shared update loop for 13 s a cycle,
    for a value the cache already covers.  Power keeps its own policy.
    """
    seen = []

    pv = _pv()
    monkeypatch.setattr(
        pv,
        "_retry_request",
        lambda req, err, retries=3, delay=1: seen.append((retries, delay)) or [],
    )

    pv._PvInterface__get_pv_forecast_akkudoktor_api(
        tgt_value="temperature", pv_config_entry=dict(FULL_ENTRY)
    )
    pv._PvInterface__get_pv_forecast_akkudoktor_api(
        tgt_value="power", pv_config_entry=dict(FULL_ENTRY)
    )

    assert seen == [(TEMP_MAX_RETRIES, TEMP_RETRY_DELAY_S), (5, 3)]


# ---------------------------------------------------------------------------
# Log level while the cache covers the failure
# ---------------------------------------------------------------------------


def _levels_for_temperature_failure(pv):
    """Levels logged by one temperature failure, at the "__main__" logger."""
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("__main__")
    logger.addHandler(handler)
    try:
        pv._handle_interface_error(
            "request_failed", "500 Server Error", {}, "akkudoktor", target="temperature"
        )
    finally:
        logger.removeHandler(handler)
    return [r.levelno for r in records]


def test_a_cache_covered_temperature_failure_is_not_an_error():
    """
    bytedealer's report: hours of ERROR lines while the cache quietly served correct
    degrees.  Real, but not an incident — the optimizer got a usable curve throughout.
    """
    pv = _pv()
    pv.last_successful_temp_forecast = [15.0] * 48

    assert logging.ERROR not in _levels_for_temperature_failure(pv)
    assert logging.WARNING in _levels_for_temperature_failure(pv)


def test_an_uncovered_temperature_failure_still_escalates():
    """With no cache to fall back on, the optimizer loses real data — that is an error."""
    pv = _pv()

    assert logging.ERROR in _levels_for_temperature_failure(pv)


def test_an_exhausted_cache_still_escalates():
    """And so is a streak long enough that the held forecast has gone stale."""
    pv = _pv()
    pv.last_successful_temp_forecast = [15.0] * 48
    pv.consecutive_temp_failures = pv.max_failures

    assert logging.ERROR in _levels_for_temperature_failure(pv)


def test_a_pv_failure_is_still_an_error():
    """The power path is unchanged: a held Watt array is not a good answer."""
    pv = _pv()
    pv.last_successful_pv_forecast = [100.0] * 48
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("__main__")
    logger.addHandler(handler)
    try:
        pv._handle_interface_error("request_failed", "boom", {}, "akkudoktor")
    finally:
        logger.removeHandler(handler)

    assert logging.ERROR in [r.levelno for r in records]
