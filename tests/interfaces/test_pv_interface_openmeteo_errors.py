# pylint: disable=protected-access
"""
Tests for PV forecast failures in the background update loop - issue #354.

A failing OpenMeteo request must fall back to the cached forecast, and an unexpected
error must not end the update thread: the optimizer keeps planning with the last good
forecast only as long as the loop keeps running.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from open_meteo_solar_forecast.exceptions import (
    OpenMeteoSolarForecastConnectionError,
    OpenMeteoSolarForecastRatelimitError,
)

from src.interfaces.pv_interface import PvInterface

TIME_FRAME_BASE_HOURLY = 3600

ENTRY = {
    "name": "roof",
    "lat": 50.0,
    "lon": 8.0,
    "azimuth": 0,
    "tilt": 30,
    "power": 1000,
    "inverterEfficiency": 0.9,
}


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


def _pv(**kwargs):
    """A PvInterface with the temperature forecast off, no threads started."""
    return PvInterface(
        {"source": "openmeteo"},
        [dict(ENTRY)],
        TIME_FRAME_BASE_HOURLY,
        {},
        temperature_forecast_enabled=False,
        timezone="UTC",
        **kwargs,
    )


def _library_raising(exc):
    """Mock OpenMeteoSolarForecast whose estimate() raises *exc*."""
    mock_forecast = MagicMock()
    mock_forecast.estimate = AsyncMock(side_effect=exc)
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_forecast
    mock_cm.__aexit__.return_value = None
    return patch("src.interfaces.pv_interface.OpenMeteoSolarForecast", return_value=mock_cm)


@pytest.mark.parametrize(
    "exc",
    [
        OpenMeteoSolarForecastConnectionError("The API is unreachable"),
        OpenMeteoSolarForecastRatelimitError("rate limited"),
        TimeoutError("timed out"),
    ],
)
def test_openmeteo_lib_failure_returns_cached_forecast(exc):
    """A library or timeout error is absorbed by the cache fallback instead of raised."""
    pv = _pv()
    cached = [1.0] * 48
    pv.last_successful_pv_forecast = list(cached)

    with _library_raising(exc):
        result = pv._PvInterface__get_pv_forecast_openmeteo_lib(ENTRY)

    assert result == cached
    assert pv.pv_forcast_request_error["error"] == "connection_error"


def test_update_loop_survives_unexpected_error_and_keeps_previous_forecast():
    """An unexpected exception in one cycle is logged; the previous forecast is kept."""
    pv = _pv()
    pv.pv_forcast_array = [5.0] * 48
    pv.pv_forcast_array_raw = [4.0] * 48

    calls = {"n": 0}

    def is_set_once():
        calls["n"] += 1
        return calls["n"] > 1

    pv._stop_event.is_set = is_set_once
    pv._stop_event.wait = lambda timeout=None: True

    with patch.object(
        pv, "get_summarized_pv_forecast", side_effect=RuntimeError("boom")
    ):
        pv._PvInterface__update_pv_state_loop()

    assert pv.pv_forcast_array == [5.0] * 48
    assert pv.pv_forcast_array_raw == [4.0] * 48
