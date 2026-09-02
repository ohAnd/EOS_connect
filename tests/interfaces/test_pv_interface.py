# pylint: disable=protected-access
"""
Unit tests for the PvInterface class and related functionality.

This module contains pytest-based tests for error handling, configuration validation,
forecast aggregation, and API fallback logic in the PvInterface implementation.
"""

import threading
import datetime as real_datetime
import requests
import pytest
from src.interfaces.pv_interface import PvInterface

time_frame_base = 3600  # Example time frame base, adjust as needed


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    """
    Fixture to patch threading.Thread to avoid starting real threads during tests.
    """

    class DummyThread:
        """
        A dummy thread class used for testing purposes.
        This class provides stub implementations of thread methods
        without performing any actual threading operations.
        """

        def __init__(self, *args, **kwargs):
            """
            DummyThread constructor.
            """
            # pass

        def start(self):
            """
            Dummy start method.
            """
            # pass

    monkeypatch.setattr("threading.Thread", DummyThread)


def test_handle_interface_error_updates_state_and_returns_empty(monkeypatch):
    """
    Test that _handle_interface_error updates the error state and returns an empty list.
    """
    monkeypatch.setattr(
        PvInterface, "_PvInterface__update_pv_state_loop", lambda self: None
    )
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    error_type = "test_error"
    message = "Test error message"
    config_entry = {"name": "test"}
    source = "test_source"

    result = pv._handle_interface_error(error_type, message, config_entry, source)

    assert not result
    assert pv.pv_forcast_request_error["error"] == error_type
    assert pv.pv_forcast_request_error["message"] == message
    assert pv.pv_forcast_request_error["config_entry"] == config_entry
    assert pv.pv_forcast_request_error["source"] == source
    assert pv.pv_forcast_request_error["timestamp"] is not None


def test_retry_request_success():
    """
    Test that _retry_request returns the result on the first successful attempt.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        """
        Dummy request function for success.
        """
        call_count["count"] += 1
        return "success"

    def error_handler(_error_type, _exception):
        """
        Dummy error handler.
        """
        return "error"

    result = pv._retry_request(request_func, error_handler, max_retries=3)
    assert result == "success"
    assert call_count["count"] == 1


def test_retry_request_failure():
    """
    Test that _retry_request retries the correct number of times and calls
    error_handler after max retries.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        """
        Dummy request function for failure.
        """
        call_count["count"] += 1
        raise ValueError("fail")

    def error_handler(_error_type, _exception):
        """
        Dummy error handler.
        """
        return "error"

    result = pv._retry_request(request_func, error_handler, max_retries=3, delay=0)
    assert result == "error"
    assert call_count["count"] == 3


def test_retry_request_partial_success():
    """
    Test that _retry_request returns the result if a later attempt succeeds.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        """
        Dummy request function for partial success.
        """
        call_count["count"] += 1
        if call_count["count"] < 2:
            raise ValueError("fail")
        return "success"

    def error_handler(_error_type, _exception):
        """
        Dummy error handler.
        """
        return "error"

    result = pv._retry_request(request_func, error_handler, max_retries=3, delay=0)
    assert result == "success"
    assert call_count["count"] == 2


def test_retry_request_handles_timeout():
    """
    Test that _retry_request handles timeout exceptions.
    """

    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        """
        Dummy request function for timeout.
        """
        call_count["count"] += 1
        raise requests.exceptions.Timeout("timeout")

    def error_handler(error_type, _exception):
        """
        Dummy error handler for timeout.
        """
        assert error_type == "timeout"
        return "timeout_error"

    result = pv._retry_request(request_func, error_handler, max_retries=2, delay=0)
    assert result == "timeout_error"
    assert call_count["count"] == 2


def test_retry_request_handles_request_exception():
    """
    Test that _retry_request handles generic request exceptions.
    """

    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        """
        Dummy request function for request exception.
        """
        call_count["count"] += 1
        raise requests.exceptions.RequestException("request failed")

    def error_handler(error_type, _exception):
        """
        Dummy error handler for request exception.
        """
        assert error_type == "request_failed"
        return "request_error"

    result = pv._retry_request(request_func, error_handler, max_retries=2, delay=0)
    assert result == "request_error"
    assert call_count["count"] == 2


def test_retry_request_handles_json_errors():
    """
    Test that _retry_request handles ValueError and TypeError as JSON errors.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        """
        Dummy request function for JSON error.
        """
        call_count["count"] += 1
        raise ValueError("json error")

    def error_handler(error_type, _exception):
        """
        Dummy error handler for JSON error.
        """
        assert error_type == "invalid_json"
        return "json_error"

    result = pv._retry_request(request_func, error_handler, max_retries=2, delay=0)
    assert result == "json_error"
    assert call_count["count"] == 2


def test_retry_request_handles_parsing_errors():
    """
    Test that _retry_request handles KeyError and AttributeError as parsing errors.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        """
        Dummy request function for parsing error.
        """
        call_count["count"] += 1
        raise KeyError("parsing error")

    def error_handler(error_type, _exception):
        """
        Dummy error handler for parsing error.
        """
        assert error_type == "parsing_error"
        return "parsing_error"

    result = pv._retry_request(request_func, error_handler, max_retries=2, delay=0)
    assert result == "parsing_error"
    assert call_count["count"] == 2


def test_handle_interface_error_multiple_calls():
    """
    Test that multiple calls to _handle_interface_error update the error state each time.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    result1 = pv._handle_interface_error("error1", "msg1", {"a": 1}, "src1")
    result2 = pv._handle_interface_error("error2", "msg2", {"b": 2}, "src2")
    assert not result1
    assert not result2
    assert pv.pv_forcast_request_error["error"] == "error2"
    assert pv.pv_forcast_request_error["message"] == "msg2"
    assert pv.pv_forcast_request_error["config_entry"] == {"b": 2}
    assert pv.pv_forcast_request_error["source"] == "src2"
    assert pv.pv_forcast_request_error["timestamp"] is not None


def test_handle_interface_error_with_empty_config():
    """
    Test that _handle_interface_error works with empty config_entry.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    result = pv._handle_interface_error("error", "msg", {}, "src")
    assert not result
    assert pv.pv_forcast_request_error["error"] == "error"
    assert pv.pv_forcast_request_error["message"] == "msg"
    assert not pv.pv_forcast_request_error["config_entry"]
    assert pv.pv_forcast_request_error["source"] == "src"
    assert pv.pv_forcast_request_error["timestamp"] is not None


def test_handle_interface_error_temperature_target_ignores_pv_cache():
    """
    Regression test for issue #276: a failed temperature request must not
    fall back to the PV power cache, even when it holds unrelated Watt
    values and the temperature cache is still empty.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv.last_successful_pv_forecast = [2090.0, 1500.0, 800.0]
    pv.last_successful_temp_forecast = []

    result = pv._handle_interface_error(
        "timeout", "temp failed", {}, "akkudoktor", target="temperature"
    )

    assert result == []
    assert pv.consecutive_temp_failures == 1
    assert pv.consecutive_failures == 0


def test_handle_interface_error_temperature_target_uses_own_cache():
    """
    Test that a failed temperature request falls back to its own cache when
    available, not the PV power cache.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv.last_successful_pv_forecast = [2090.0, 1500.0, 800.0]
    pv.last_successful_temp_forecast = [15.0, 16.0, 17.0]

    result = pv._handle_interface_error(
        "timeout", "temp failed", {}, "akkudoktor", target="temperature"
    )

    assert result == [15.0, 16.0, 17.0]


def test_consecutive_temp_failures_independent_of_pv_success():
    """
    Temperature failures must accumulate toward max_failures even while PV
    power keeps succeeding and resetting its own counter every cycle -
    reproduces the 0/1 oscillation from issue #276 that kept the temperature
    failure count from ever reaching the threshold.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    for _ in range(30):
        # PV power fetch succeeds and resets only the power counter.
        pv.last_successful_pv_forecast = [100.0]
        pv.consecutive_failures = 0
        # Temperature fetch fails every cycle.
        pv._handle_interface_error(
            "timeout", "temp failed", {}, "akkudoktor", target="temperature"
        )

    assert pv.consecutive_temp_failures == 30
    assert pv.consecutive_temp_failures >= pv.max_failures
    assert pv.consecutive_failures == 0


def test_default_pv_forecast_length_and_values():
    """
    Test that the default PV forecast returns 48 values of type int or float.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    result = pv._PvInterface__get_default_pv_forcast(100)
    assert len(result) == 48
    assert all(isinstance(x, float) or isinstance(x, int) for x in result)


def test_default_temperature_forecast_length_and_values():
    """
    Test that the default temperature forecast returns 48 values of 15.0.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    result = pv._PvInterface__get_default_temperature_forecast()
    assert len(result) == 48
    assert all(x == 15.0 for x in result)


def test_check_config_missing_parameters():
    """
    Test that missing required config parameters result in graceful degradation.
    The interface should initialize successfully but with configuration_valid=False.
    This allows users to fix the config via the web UI instead of crashing.
    """
    config = [{"lat": 50, "lon": 8}]  # missing required parameters
    pv = PvInterface({}, config, time_frame_base, {}, timezone="UTC")
    
    # Should not crash, but should start in degraded mode
    assert pv.configuration_valid is False
    assert pv.configuration_state == "incomplete"


def test_pv_forecast_config_must_be_list():
    """
    Test that pv_forecast as a dict (wrong YAML format) results in graceful degradation.
    This catches user config errors where they forget the '-' in YAML.

    WRONG: pv_forecast:
             name: "test"
             lat: 50

    CORRECT: pv_forecast:
               - name: "test"
                 lat: 50
    
    The interface should not crash, but should start in degraded mode so user can fix via web UI.
    """
    # Config is a dict instead of a list - simulates wrong YAML format
    config = {"name": "test", "lat": 50, "lon": 8}

    # Should not crash with SystemExit, but should degrade gracefully
    pv = PvInterface({}, config, time_frame_base, {}, timezone="UTC")
    
    # Should be in degraded mode
    assert pv.configuration_valid is False
    assert pv.configuration_state == "incomplete"


def test_summarized_pv_forecast_aggregation():
    """
    Test that get_summarized_pv_forecast correctly aggregates multiple config entries.
    """
    config = [
        {
            "name": "A",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 100,
            "powerInverter": 100,
            "inverterEfficiency": 1.0,
        },
        {
            "name": "B",
            "lat": 51,
            "lon": 9,
            "azimuth": 180,
            "tilt": 30,
            "power": 200,
            "powerInverter": 200,
            "inverterEfficiency": 1.0,
        },
    ]
    pv = PvInterface({}, config, time_frame_base, {}, timezone="UTC")
    # Monkeypatch __get_pv_forecast to return fixed arrays
    pv._PvInterface__get_pv_forecast = (
        lambda entry, tgt_duration=24: [entry["power"]] * tgt_duration
    )
    result = pv.get_summarized_pv_forecast()
    assert result == [300] * 24


def test_summarized_pv_forecast_scale_false_returns_unscaled_values():
    """Explicit scale=False should bypass autoscaler output while default behavior stays scaled."""
    config = [
        {"name": "A", "lat": 50, "lon": 8, "azimuth": 180, "tilt": 30, "power": 100, "powerInverter": 100, "inverterEfficiency": 1.0},
        {"name": "B", "lat": 51, "lon": 9, "azimuth": 180, "tilt": 30, "power": 200, "powerInverter": 200, "inverterEfficiency": 1.0},
    ]
    pv = PvInterface({}, config, time_frame_base, {}, timezone="UTC")
    pv._PvInterface__get_pv_forecast = (
        lambda entry, tgt_duration=24: [entry["power"]] * tgt_duration
    )

    class DummyAutoscaler:
        enabled = True

        def apply_scaling(self, values, time_frame_base):
            return [v * 10 for v in values]

    pv.set_autoscaler(DummyAutoscaler())

    assert pv.get_summarized_pv_forecast(scale=False) == [300] * 24
    assert pv.get_summarized_pv_forecast() == [3000] * 24


def test_api_error_triggers_fallback(monkeypatch):
    """
    Test that an API error with no cache yields nothing to serve.

    The empty list is what makes the update loop reach its own default: a
    zero-filled array of full length is truthy, so it used to be picked up by the
    "cached forecast available" branch and served as a flat 0 W day.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv._retry_request = lambda req, err, *args, **kwargs: err(
        "api_error", Exception("fail")
    )
    result = pv._PvInterface__get_pv_forecast_akkudoktor_api(
        pv_config_entry={
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 100,
            "powerInverter": 800,
            "inverterEfficiency": 0.95,
            "horizon": "0",
        }
    )
    assert result == []
    assert pv.pv_forcast_request_error["error"] in (None, "api_error")


def test_temperature_api_error_never_returns_pv_watts(monkeypatch):
    """
    Regression test for issue #276: a total temperature-fetch failure must
    never return data derived from the cached PV power (Watts) array, even
    though both forecasts are fetched through this same shared method.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv.last_successful_pv_forecast = [2090.0, 1500.0, 800.0]
    pv._retry_request = lambda req, err, *args, **kwargs: err(
        "timeout", Exception("fail")
    )

    result = pv._PvInterface__get_pv_forecast_akkudoktor_api(
        tgt_value="temperature",
        pv_config_entry={
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 100,
            "powerInverter": 800,
            "inverterEfficiency": 0.95,
            "horizon": "0",
        },
    )

    # No temperature cache exists yet, so there is nothing to serve.  Padding the
    # empty result to full length instead produced 48 h of 0 degC - not PV Watts,
    # but just as fabricated, and inside the +-60 degC plausibility guard.
    assert result == []
    assert 2090.0 not in result


def test_temperature_api_error_falls_back_to_temp_cache(monkeypatch):
    """
    Once a temperature forecast has succeeded at least once, a later
    failure must reuse the temperature cache, not the PV power cache.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv.last_successful_pv_forecast = [2090.0, 1500.0, 800.0]
    pv.last_successful_temp_forecast = [15.0, 16.0, 17.0]
    pv._retry_request = lambda req, err, *args, **kwargs: err(
        "timeout", Exception("fail")
    )

    result = pv._PvInterface__get_pv_forecast_akkudoktor_api(
        tgt_value="temperature",
        pv_config_entry={
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 100,
            "powerInverter": 800,
            "inverterEfficiency": 0.95,
            "horizon": "0",
        },
    )

    assert result == [15.0, 16.0, 17.0]


def _run_one_update_loop_iteration(pv):
    """
    Helper to run exactly one iteration of the background update loop.
    threading.Thread is stubbed by the autouse patch_thread fixture, so the
    loop was never actually started - it is safe to invoke directly here.
    """
    calls = {"n": 0}

    def is_set_once():
        calls["n"] += 1
        return calls["n"] > 1

    pv._stop_event.is_set = is_set_once
    pv._PvInterface__update_pv_state_loop()


def test_update_loop_temperature_failure_never_uses_pv_cache(monkeypatch):
    """
    End-to-end regression test for issue #276: with PV power succeeding and
    caching Watt values, and the temperature fetch exhausting its own retries
    with no temperature cache yet, the update loop must fall back to the
    default 15C forecast - never to the cached PV power array.
    """
    config = [{"name": "roof", "lat": 50, "lon": 8, "power": 5000}]
    pv = PvInterface(
        {}, config, time_frame_base, {}, temperature_forecast_enabled=True,
        timezone="UTC",
    )
    pv.last_successful_pv_forecast = [2090.0, 1500.0, 800.0]
    monkeypatch.setattr(pv, "get_summarized_pv_forecast", lambda scale=False: [100.0])
    monkeypatch.setattr(pv, "apply_autoscaling", lambda values: values)
    monkeypatch.setattr(
        pv,
        "_PvInterface__get_pv_forecast_akkudoktor_api",
        lambda tgt_value, pv_config_entry: [],
    )

    _run_one_update_loop_iteration(pv)

    assert pv.temp_forecast_array == pv._PvInterface__get_default_temperature_forecast()
    assert pv.temp_forecast_array != pv.last_successful_pv_forecast


def test_update_loop_rejects_implausible_temperature_values(monkeypatch):
    """
    Defense-in-depth: even if a mislabeled PV-Watts array slipped past the
    target-aware cache fix, physically implausible values must still be
    rejected in favor of the default 15C forecast.
    """
    config = [{"name": "roof", "lat": 50, "lon": 8, "power": 5000}]
    pv = PvInterface(
        {}, config, time_frame_base, {}, temperature_forecast_enabled=True,
        timezone="UTC",
    )
    monkeypatch.setattr(pv, "get_summarized_pv_forecast", lambda scale=False: [100.0])
    monkeypatch.setattr(pv, "apply_autoscaling", lambda values: values)
    monkeypatch.setattr(
        pv,
        "_PvInterface__get_pv_forecast_akkudoktor_api",
        lambda tgt_value, pv_config_entry: [2090.0, 1500.0, 800.0],
    )

    _run_one_update_loop_iteration(pv)

    assert pv.temp_forecast_array == pv._PvInterface__get_default_temperature_forecast()


def test_get_current_pv_forecast_returns_array():
    """
    Test that get_current_pv_forecast returns the correct array.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv.pv_forcast_array = [1, 2, 3]
    assert pv.get_current_pv_forecast() == [1, 2, 3]


def test_get_current_temp_forecast_returns_array():
    """
    Test that get_current_temp_forecast returns the correct array.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv.temp_forecast_array = [15, 16, 17]
    assert pv.get_current_temp_forecast() == [15, 16, 17]


@pytest.mark.parametrize(
    "source",
    ["akkudoktor", "openmeteo", "forecast_solar", "solcast", "evcc", "default"],
)
def test_horizon_config_handling(source):
    """
    Test that horizon is set to default for openmeteo_local and forecast_solar,
    and not enforced for other sources.
    """
    config_entry = {
        "name": "test",
        "lat": 50,
        "lon": 8,
        "azimuth": 180,
        "tilt": 30,
        "power": 100,
        "powerInverter": 100,
        "inverterEfficiency": 1.0,
        # horizon intentionally omitted
    }
    config = [config_entry.copy()]
    config_source = {"source": source}
    if source == "solcast":
        config_source["api_key"] = "dummy"
        config[0]["resource_id"] = "dummy"
    # Patch thread to avoid starting background thread

    class DummyThread:
        """
        A dummy thread class used for testing purposes.
        This class mimics the interface of a thread but does not perform any
        actual threading operations.
        Useful for unit tests where thread behavior needs to be simulated
        without real concurrency.
        Methods
        -------
        start():
            Dummy method to simulate starting a thread.
        """

        def __init__(self, *args, **kwargs):
            """
            DummyThread constructor.
            """
            # pass

        def start(self):
            """
            Dummy start method.
            """
            # pass

    threading.Thread = DummyThread

    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")
    entry = pv.config[0]
    if source == "openmeteo_local":
        assert "horizon" in entry
        assert isinstance(entry["horizon"], list)
        assert entry["horizon"] == [0] * 36
    elif source == "forecast_solar":
        assert "horizon" in entry
        assert isinstance(entry["horizon"], list)
        assert entry["horizon"] == [0] * 24
    else:
        assert (
            "horizon" not in entry
            or entry["horizon"] == [0] * 36
            or entry["horizon"] == ""
            or entry["horizon"] is None
        )


class FixedDatetime(real_datetime.datetime):
    """
    A subclass of `real_datetime.datetime` that overrides the `now()` class method
    to return a fixed datetime value. Useful for testing scenarios where a
    predictable datetime is required.

    Attributes:
        None

    Methods:
        now(cls, tz=None): Returns a fixed datetime (2025-11-02 08:30:00) with optional timezone.
    """

    @classmethod
    def now(cls, tz=None):
        # Return a fixed datetime, e.g., midnight UTC
        return cls(2025, 11, 2, 8, 30, 0, tzinfo=tz)


def test_solcast_data_adaption(monkeypatch):
    """
    Test that Solcast API data is correctly transformed into a 48-hour forecast in Wh per hour.
    Uses a mock response based on test.json (data embedded directly).
    Also checks that the transformation from kWh/30min to Wh/hour is correct.
    """
    # Minimal mock Solcast response (first 48 periods, simplified for brevity)
    solcast_data = {
        "forecasts": [
            {
                "pv_estimate": 0.0734,
                "period_end": "2025-11-02T08:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.1706,
                "period_end": "2025-11-02T09:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.4624,
                "period_end": "2025-11-02T09:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.4254,
                "period_end": "2025-11-02T10:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.4511,
                "period_end": "2025-11-02T10:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.4142,
                "period_end": "2025-11-02T11:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.3381,
                "period_end": "2025-11-02T11:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.3353,
                "period_end": "2025-11-02T12:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.3267,
                "period_end": "2025-11-02T12:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.3267,
                "period_end": "2025-11-02T13:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.2797,
                "period_end": "2025-11-02T13:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.2072,
                "period_end": "2025-11-02T14:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.1313,
                "period_end": "2025-11-02T14:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.0638,
                "period_end": "2025-11-02T15:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.0303,
                "period_end": "2025-11-02T15:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.0096,
                "period_end": "2025-11-02T16:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T16:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T17:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T17:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T18:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T18:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T19:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T19:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T20:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T20:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T21:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T21:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T22:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T22:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T23:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-02T23:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T00:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T00:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T01:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T01:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T02:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T02:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T03:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T03:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T04:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T04:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T05:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0,
                "period_end": "2025-11-03T05:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.0051,
                "period_end": "2025-11-03T06:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.0417,
                "period_end": "2025-11-03T07:00:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.0802,
                "period_end": "2025-11-03T07:30:00.0000000Z",
                "period": "PT30M",
            },
            {
                "pv_estimate": 0.2342,
                "period_end": "2025-11-03T08:00:00.0000000Z",
                "period": "PT30M",
            },
        ]
    }
    # Pad to 48 entries if needed
    while len(solcast_data["forecasts"]) < 48:
        solcast_data["forecasts"].append(
            {"pv_estimate": 0, "period_end": "", "period": "PT30M"}
        )

    config_entry = {
        "name": "solcast_test",
        "lat": 50,
        "lon": 8,
        "inverterEfficiency": 1.0,  # Test expects no efficiency loss
    }
    config_source = {"source": "solcast", "api_key": "dummy_key", "resource_id": "dummy_resource"}

    pv = PvInterface(config_source, [config_entry], time_frame_base, {}, timezone="UTC")

    # Monkeypatch the _retry_request to return our mock data
    def mock_retry_request(request_func, error_handler, **kwargs):
        return solcast_data

    pv._retry_request = mock_retry_request

    # Monkeypatch requests.get to avoid real HTTP calls
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: None)

    # Monkeypatch datetime in pv_interface to fixed time (08:30)
    monkeypatch.setattr("src.interfaces.pv_interface.datetime", FixedDatetime)

    # Call the Solcast API handler
    forecast = pv._PvInterface__get_pv_forecast_solcast_api(
        config_entry, tgt_duration=48
    )

    print("forecast: " + str(forecast))

    # The forecast should be a list of 48 floats (Wh per hour)
    assert isinstance(forecast, list)
    assert len(forecast) == 48
    assert all(isinstance(x, float) or isinstance(x, int) for x in forecast)
    # Check that the sum is positive (since there is some production in the mock)
    assert sum(forecast) > 0

    # Since padding is from midnight, first 8 hours (0-7) should be zero
    for i in range(8):
        assert forecast[i] == 0, f"Expected zero padding at hour {i}, got {forecast[i]}"

    # Check the transformation for the first few nonzero hours (starting at hour 8)
    for hour in range(8, min(8 + 6, 24)):
        solcast_idx = (hour - 8) * 2
        expected_wh = (
            solcast_data["forecasts"][solcast_idx]["pv_estimate"]
            + solcast_data["forecasts"][solcast_idx + 1]["pv_estimate"]
        ) * 500
        # Round both values to one decimal place before comparison
        assert round(forecast[hour], 1) == round(expected_wh, 1), (
            f"Solcast data transformation error at hour {hour}: "
            f"expected {expected_wh} Wh, got {forecast[hour]} Wh. "
            f"Input values: {solcast_data['forecasts'][solcast_idx]['pv_estimate']} kWh + "
            f"{solcast_data['forecasts'][solcast_idx + 1]['pv_estimate']} kWh"
        )


# ============================================================================
# Victron VRM API Integration Tests
# ============================================================================


def test_victron_config_validation_missing_vrm_id():
    """
    Test that Victron provider requires resource_id in pv_forecast_source.
    With Issue #259 fix, missing VRM should result in graceful degradation,
    not a crash. This allows users to fix the config via the web UI.
    """
    config_source = {"source": "victron", "api_key": "test_token"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]
    # Should not crash with SystemExit
    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")
    
    # Should be in degraded mode
    assert pv.configuration_valid is False
    assert pv.configuration_state == "incomplete"


def test_victron_config_validation_missing_api_key():
    """
    Test that Victron provider requires api_key in pv_forecast_source.
    With Issue #259 fix, missing API key should result in graceful degradation,
    not a crash. This allows users to fix the config via the web UI.
    """
    config_source = {"source": "victron", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]
    # Should not crash with SystemExit
    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")
    
    # Should be in degraded mode
    assert pv.configuration_valid is False
    assert pv.configuration_state == "incomplete"


def test_victron_resource_id_as_integer(monkeypatch):
    """
    Test that resource_id given as a plain integer (unquoted in YAML) is handled
    gracefully without crashing on .strip().
    This simulates the YAML parse result when a user writes: resource_id: 12345678
    instead of: resource_id: "12345678"
    """
    config_source = {"source": "victron", "api_key": "test_token", "resource_id": 12345678}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    # PvInterface init must not crash with AttributeError on .strip()
    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

    mock_response = {
        "records": {
            "solar_yield_forecast": [
                [1730505600000 + (3600000 * i), 100.0] for i in range(48)
            ]
        }
    }

    def mock_get(*args, **kwargs):
        class MockResponse:
            def json(self):
                return mock_response

            def raise_for_status(self):
                pass

        return MockResponse()

    monkeypatch.setattr("requests.get", mock_get)

    forecast = pv._PvInterface__get_pv_forecast_victron_api(config[0], hours=48)

    assert isinstance(forecast, list)
    assert len(forecast) == 48
    assert all(isinstance(v, (int, float)) for v in forecast)


def test_victron_resource_id_as_string(monkeypatch):
    """
    Test that resource_id given as a quoted string (the documented correct format)
    works identically.
    This simulates: resource_id: "12345678"
    """
    config_source = {"source": "victron", "api_key": "test_token", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

    mock_response = {
        "records": {
            "solar_yield_forecast": [
                [1730505600000 + (3600000 * i), 100.0] for i in range(48)
            ]
        }
    }

    def mock_get(*args, **kwargs):
        class MockResponse:
            def json(self):
                return mock_response

            def raise_for_status(self):
                pass

        return MockResponse()

    monkeypatch.setattr("requests.get", mock_get)

    forecast = pv._PvInterface__get_pv_forecast_victron_api(config[0], hours=48)

    assert isinstance(forecast, list)
    assert len(forecast) == 48
    assert all(isinstance(v, (int, float)) for v in forecast)


def test_victron_successful_forecast_retrieval(monkeypatch):
    """
    Test successful Victron VRM API forecast retrieval.
    Verifies the method returns a valid forecast array.
    """
    config_source = {"source": "victron", "api_key": "test", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

    # Mock Victron API response with sufficient forecast data
    mock_response = {
        "records": {
            "solar_yield_forecast": [
                [1730505600000, 50.0],
                [1730509200000, 100.0],
                [1730512800000, 150.0],
            ]
            + [[1730505600000 + (3600000 * i), 100.0] for i in range(3, 48)]
        }
    }

    def mock_get(*args, **kwargs):
        class MockResponse:
            def json(self):
                return mock_response

            def raise_for_status(self):
                pass

        return MockResponse()

    monkeypatch.setattr("requests.get", mock_get)

    forecast = pv._PvInterface__get_pv_forecast_victron_api(config[0], hours=48)

    # Assert we got a valid list
    assert isinstance(forecast, list)
    assert len(forecast) == 48
    assert all(isinstance(x, (int, float)) for x in forecast)
    # Error state should be clear on success
    assert pv.pv_forcast_request_error["error"] is None


def test_victron_15min_time_frame_conversion(monkeypatch):
    """
    Test that Victron forecast is correctly converted to 15-min intervals.
    48 hourly values should become 192 15-min values (each hourly value / 4).
    """
    config_source = {"source": "victron", "api_key": "test", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    time_frame_900 = 900
    pv = PvInterface(config_source, config, time_frame_900, {}, timezone="UTC")

    mock_response = {
        "records": {
            "solar_yield_forecast": [
                [1730505600000 + (3600000 * i), 400.0] for i in range(48)
            ]
        }
    }

    def mock_get(*args, **kwargs):
        class MockResponse:
            def json(self):
                return mock_response

            def raise_for_status(self):
                pass

        return MockResponse()

    monkeypatch.setattr("requests.get", mock_get)

    forecast = pv._PvInterface__get_pv_forecast_victron_api(config[0], hours=48)

    # Should have 192 values (48 * 4)
    assert len(forecast) == 192
    # Each value should be numeric and reasonable (400 / 4 = 100 with rounding)
    assert all(isinstance(x, (int, float)) for x in forecast)
    # Most values should be around 100 (400 / 4)
    non_zero_values = [x for x in forecast if x > 0]
    if non_zero_values:
        avg_value = sum(non_zero_values) / len(non_zero_values)
        assert 90 <= avg_value <= 110  # Should average around 100


def test_victron_api_timeout_error_handling(monkeypatch):
    """
    Test that Victron provider handles API timeout errors gracefully.
    Should return empty list and set error state.
    """
    config_source = {"source": "victron", "api_key": "test", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

    def mock_timeout(*args, **kwargs):
        raise requests.exceptions.Timeout("Connection timeout")

    monkeypatch.setattr("requests.get", mock_timeout)

    forecast = pv._PvInterface__get_pv_forecast_victron_api(config[0], hours=48)

    # Should return empty list on error
    assert forecast == []
    # Error state should be set to "no_valid_data" (after retries exhausted)
    # or contain "timeout" in the message
    assert pv.pv_forcast_request_error["error"] in ("timeout", "no_valid_data")
    assert (
        "Victron VRM API error" in pv.pv_forcast_request_error["message"]
        or "No valid solar forecast data" in pv.pv_forcast_request_error["message"]
    )


def test_victron_api_request_error_handling(monkeypatch):
    """
    Test that Victron provider handles generic request errors gracefully.
    """
    config_source = {"source": "victron", "api_key": "test", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

    def mock_request_error(*args, **kwargs):
        raise requests.exceptions.RequestException("Connection failed")

    monkeypatch.setattr("requests.get", mock_request_error)

    forecast = pv._PvInterface__get_pv_forecast_victron_api(config[0], hours=48)

    assert forecast == []
    # Error state should indicate failure
    assert pv.pv_forcast_request_error["error"] in ("request_failed", "no_valid_data")
    # Message should reference either the API error or the data validation failure
    assert (
        "victron" in pv.pv_forcast_request_error["message"].lower()
        or "forecast" in pv.pv_forcast_request_error["message"].lower()
    )


def test_victron_invalid_response_structure(monkeypatch):
    """
    Test that Victron provider handles malformed API responses.
    Missing 'records' key should trigger error handling.
    """
    config_source = {"source": "victron", "api_key": "test", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

    # Missing nested structure - API will return empty forecast list
    mock_response = {"status": "error"}

    def mock_get(*args, **kwargs):
        class MockResponse:
            def json(self):
                return mock_response

            def raise_for_status(self):
                pass

        return MockResponse()

    monkeypatch.setattr("requests.get", mock_get)

    forecast = pv._PvInterface__get_pv_forecast_victron_api(config[0], hours=48)

    # Empty solar forecast triggers error
    assert forecast == []
    assert pv.pv_forcast_request_error["error"] == "no_valid_data"


def test_victron_malformed_forecast_points(monkeypatch):
    """
    Test that Victron provider handles malformed forecast points in response.
    Invalid points should be skipped gracefully.
    """
    config_source = {"source": "victron", "api_key": "test", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

    # Build forecast points with mix of valid and invalid entries
    forecast_points = [
        [1730505600000, 100.0],  # Valid
        [1730509200000],  # Invalid: missing value
        [1730512800000, 200.0],  # Valid
        None,  # Invalid: not a list
        1730516400000,  # Invalid: not a list/tuple
        [1730519200000, 300.0],  # Valid
    ]
    # Add remaining valid points
    for i in range(6, 48):
        forecast_points.append([1730505600000 + (3600000 * i), 50.0])

    mock_response = {"records": {"solar_yield_forecast": forecast_points}}

    def mock_get(*args, **kwargs):
        class MockResponse:
            def json(self):
                return mock_response

            def raise_for_status(self):
                pass

        return MockResponse()

    monkeypatch.setattr("requests.get", mock_get)

    forecast = pv._PvInterface__get_pv_forecast_victron_api(config[0], hours=48)

    # Should process valid points and skip invalid ones
    assert isinstance(forecast, list)
    assert len(forecast) == 48
    # All values should be numeric (floats or ints)
    assert all(isinstance(x, (int, float)) for x in forecast)


def test_victron_dispatch_routing(monkeypatch):
    """
    Test that __get_pv_forecast correctly routes to Victron provider.
    """

    class FixedDatetimeVictron(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 11, 2, 8, 0, 0, tzinfo=tz)

    monkeypatch.setattr("src.interfaces.pv_interface.datetime", FixedDatetimeVictron)

    config_source = {"source": "victron", "api_key": "test", "resource_id": "12345678"}
    config = [
        {
            "name": "test",
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 5000,
            "powerInverter": 5000,
            "inverterEfficiency": 0.95,
        }
    ]

    pv = PvInterface(config_source, config, time_frame_base, {}, timezone="UTC")

    mock_response = {
        "records": {
            "solar_yield_forecast": [
                [1730534400000 + (3600000 * i), 200.0] for i in range(48)
            ]
        }
    }

    def mock_get(*args, **kwargs):
        class MockResponse:
            def json(self):
                return mock_response

            def raise_for_status(self):
                pass

        return MockResponse()

    monkeypatch.setattr("requests.get", mock_get)

    # Call the dispatch method
    forecast = pv._PvInterface__get_pv_forecast(config[0])

    # Should return valid Victron forecast via dispatch
    assert isinstance(forecast, list)
    assert len(forecast) == 48
    assert all(isinstance(x, (int, float)) for x in forecast)

# ---------------------------------------------------------------------------
# evcc tests
# ---------------------------------------------------------------------------

def test_evcc_compact_unix_timestamp_forecast_is_supported(monkeypatch):
    """Support EVCC #32391 compact [unix_timestamp, power] entries."""
    config_entry = {"name": "evcc_test", "lat": 50, "lon": 8, "power": 100}
    config_source = {"source": "evcc", "use_real_data_correction": False}
    base = real_datetime.datetime.now(real_datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    compact_timeseries = [
        [int((base + real_datetime.timedelta(minutes=15 * i)).timestamp()), 40.0]
        for i in range(192)
    ]

    def mock_retry_request(request_func, error_handler, **kwargs):
        return compact_timeseries

    monkeypatch.setattr(PvInterface, "_retry_request", staticmethod(mock_retry_request))
    pv = PvInterface(config_source, [config_entry], 900, {"url": "http://dummy-evcc"}, timezone="UTC")

    result = pv._PvInterface__get_pv_forecast_evcc_api(config_entry, hours=48)

    assert len(result) == 192
    assert result == [10.0] * 192


def test_evcc_scaling_enabled_applies_scale_factor(monkeypatch):
    """Central PV summary applies autoscaling by default when enabled."""
    config_entry = {"name": "evcc_test", "lat": 50, "lon": 8, "power": 100}
    config_source = {"source": "evcc"}
    pv = PvInterface(config_source, [config_entry], 3600, {"url": "http://dummy-evcc"}, timezone="UTC")
    pv._PvInterface__get_pv_forecast = lambda entry, tgt_duration=48: [10.0] * 48

    class DummyAutoscaler:
        enabled = True
        _scale_factors = {1: 1.5}

        def apply_scaling(self, values, time_frame_base):
            return [value * 1.5 for value in values]

    pv.set_autoscaler(DummyAutoscaler())

    result = pv.get_summarized_pv_forecast()

    assert isinstance(result, list)
    assert len(result) == 48
    assert all(abs(value - 15.0) < 1e-6 for value in result)


def test_evcc_scaling_disabled_uses_1(monkeypatch):
    """Central PV summary leaves raw values when autoscaler is disabled."""
    config_entry = {"name": "evcc_test", "lat": 50, "lon": 8, "power": 100}
    config_source = {"source": "evcc"}
    pv = PvInterface(config_source, [config_entry], 3600, {"url": "http://dummy-evcc"}, timezone="UTC")
    pv._PvInterface__get_pv_forecast = lambda entry, tgt_duration=48: [10.0] * 48

    class DummyAutoscaler:
        enabled = False
        _scale_factors = {1: 1.5}

        def apply_scaling(self, values, time_frame_base):
            return [value * 1.5 for value in values]

    pv.set_autoscaler(DummyAutoscaler())

    result = pv.get_summarized_pv_forecast()

    assert isinstance(result, list)
    assert len(result) == 48
    assert all(abs(value - 10.0) < 1e-6 for value in result)


def _evcc_interface(use_real_data_correction=None):
    """Build a PvInterface configured for the EVCC source."""
    config_entry = {"name": "evcc_test", "lat": 50, "lon": 8, "power": 100}
    config_source = {"source": "evcc"}
    if use_real_data_correction is not None:
        config_source["use_real_data_correction"] = use_real_data_correction
    return PvInterface(
        config_source, [config_entry], 3600, {"url": "http://dummy-evcc"}, timezone="UTC"
    )


@pytest.mark.parametrize(
    "published_scale, expected",
    [
        (0.8, 0.8),            # normal correction is applied as published
        (1.0, 1.0),
        (0.05, 0.5),           # below 0.1 EVCC has too little data: floor at 0.5
        (0.0, 1.0),            # non-positive is meaningless: fall back to neutral
        (-1.0, 1.0),
        ("unknown", 1.0),      # EVCC omitted the field
        (None, 1.0),
    ],
)
def test_evcc_scale_factor_resolution(published_scale, expected):
    """The published EVCC correction factor is validated before it is applied."""
    pv = _evcc_interface()
    assert pv._resolve_evcc_scale_factor(published_scale) == pytest.approx(expected)


def test_evcc_scale_factor_ignored_when_correction_disabled():
    """use_real_data_correction=False opts out of EVCC's own correction entirely."""
    pv = _evcc_interface(use_real_data_correction=False)
    assert pv._resolve_evcc_scale_factor(0.8) == pytest.approx(1.0)
    assert pv._resolve_evcc_scale_factor(0.05) == pytest.approx(1.0)


def test_evcc_forecast_applies_published_scale(monkeypatch):
    """EVCC's learned scale is applied to the forecast it publishes."""
    config_entry = {"name": "evcc_test", "lat": 50, "lon": 8, "power": 100}
    base = real_datetime.datetime.now(real_datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    timeseries = [
        [int((base + real_datetime.timedelta(minutes=15 * i)).timestamp()), 40.0]
        for i in range(192)
    ]

    monkeypatch.setattr(
        PvInterface,
        "_retry_request",
        staticmethod(lambda request_func, error_handler, **kwargs: (timeseries, 0.5)),
    )
    pv = _evcc_interface()

    result = pv._PvInterface__get_pv_forecast_evcc_api(config_entry, hours=48)

    # 40 W over four 15-minute slots is 40 Wh per hour, halved by the 0.5 scale.
    assert len(result) == 48
    assert result == [20.0] * 48


def test_evcc_forecast_unscaled_when_correction_disabled(monkeypatch):
    """With correction disabled the raw EVCC values pass through untouched."""
    config_entry = {"name": "evcc_test", "lat": 50, "lon": 8, "power": 100}
    base = real_datetime.datetime.now(real_datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    timeseries = [
        [int((base + real_datetime.timedelta(minutes=15 * i)).timestamp()), 40.0]
        for i in range(192)
    ]

    monkeypatch.setattr(
        PvInterface,
        "_retry_request",
        staticmethod(lambda request_func, error_handler, **kwargs: (timeseries, 0.5)),
    )
    pv = _evcc_interface(use_real_data_correction=False)

    result = pv._PvInterface__get_pv_forecast_evcc_api(config_entry, hours=48)

    assert result == [40.0] * 48

# ---------------------------------------------------------------------------
# Open-Meteo DST normalisation tests
# ---------------------------------------------------------------------------
# These tests verify the DST guard added to two OpenMeteo methods:
#   * __get_pv_forecast_openmeteo_api  (source: openmeteo_local)
#   * __get_pv_forecast_openmeteo_lib  (source: openmeteo)
#
# Three scenarios per method:
#   1. Normal day  – API / lib returns exactly 48 hourly elements
#   2. Spring-forward – returns 47 elements (extra hour trimmed away)
#   3. Fall-back       – returns 49 elements (extra hour added)
#
# In all cases the output must contain exactly 48 elements.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402 – used only by the openmeteo_lib tests
import pytz  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402


def _openmeteo_api_entry():
    """
    Return a minimal pv_config_entry suitable for __get_pv_forecast_openmeteo_api.

    Returns:
        dict: PV config entry with required keys.
    """
    return {
        "lat": 50.0,
        "lon": 8.0,
        "name": "test_openmeteo_api",
        "tilt": 30,
        "azimuth": 180,
        "power": 200,
        "inverterEfficiency": 0.85,
        "horizon": [0] * 36,
    }


def _make_api_response(n_elements):
    """
    Build a fake Open-Meteo hourly response with *n_elements* per array.

    Args:
        n_elements: Number of hourly entries to include.

    Returns:
        dict: Fake hourly weather response dict.
    """
    return {
        "hourly": {
            "shortwave_radiation": [500.0] * n_elements,
            "cloudcover": [20.0] * n_elements,
            "time": [
                f"2026-03-29T{str(h % 24).zfill(2)}:00:00" for h in range(n_elements)
            ],
        }
    }


def _mock_retry_for_api(fake_data):
    """
    Build a ``_retry_request`` replacement that returns *fake_data* on the
    second call (JSON parse).  The first call (HTTP request) returns a mock
    response whose ``.json()`` method returns the same *fake_data*.

    Args:
        fake_data: Dict to return when the JSON-parse step is executed.

    Returns:
        Callable: Mock ``_retry_request`` implementation.
    """
    mock_response = MagicMock()
    mock_response.json.return_value = fake_data
    call_count = [0]

    def _retry(func, err, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return mock_response  # HTTP request step
        return func()  # JSON parse step – calls response.json()

    return _retry


class TestOpenMeteoApiDSTNormalisation:
    """
    Tests for __get_pv_forecast_openmeteo_api DST normalisation.

    The method fetches hourly radiation/cloudcover data and builds a
    pv_forecast list.  On a spring-forward day the API may return only
    47 hourly entries; on a fall-back day it may return 49.  The
    normalisation guard must ensure the output always contains exactly
    48 elements.
    """

    _ENTRY = _openmeteo_api_entry()

    def _run(self, n_api_elements):
        """
        Run __get_pv_forecast_openmeteo_api with *n_api_elements* hourly
        API entries and return the forecast list.

        Solar-position and AOI methods are replaced with trivial lambdas so
        the test runs quickly without any real astronomical computation.

        Args:
            n_api_elements: Number of hourly entries to supply in the fake
                API response (47, 48, or 49).

        Returns:
            list: Hourly PV forecast (should always be length 48).
        """
        pv = PvInterface({}, [], time_frame_base, {}, timezone="Europe/Berlin")
        fake_data = _make_api_response(n_api_elements)
        pv._retry_request = _mock_retry_for_api(fake_data)
        pv._solar_position = lambda times, lat, lon: [
            {"apparent_zenith": 45.0, "azimuth": 180.0} for _ in times
        ]
        pv._angle_of_incidence = (
            lambda surface_tilt=0, surface_azimuth=0, solar_zenith=0, solar_azimuth=0: 45.0
        )
        return pv._PvInterface__get_pv_forecast_openmeteo_api(self._ENTRY)

    def test_normal_day_48_elements_unchanged(self):
        """
        Normal day: API returns 48 hourly entries → output must have 48 elements.
        """
        result = self._run(48)
        assert len(result) == 48, f"Normal day: expected 48 elements, got {len(result)}"

    def test_spring_forward_47_elements_padded_to_48(self):
        """
        Spring-forward day: API returns only 47 hourly entries (23-hour day).
        The DST guard must pad the output to exactly 48 elements using the
        last available value.
        """
        result = self._run(47)
        assert (
            len(result) == 48
        ), f"Spring-forward: expected 48 elements after padding, got {len(result)}"
        # The 48th element must equal the 47th (last-value padding)
        assert (
            result[47] == result[46]
        ), "Spring-forward: padded element must repeat the last forecast value"

    def test_fall_back_49_elements_trimmed_to_48(self):
        """
        Fall-back day: API returns 49 hourly entries (25-hour day, or edge case
        where more data than expected is provided).  The DST guard must trim
        the output to exactly 48 elements.
        """
        result = self._run(49)
        assert (
            len(result) == 48
        ), f"Fall-back: expected 48 elements after trimming, got {len(result)}"


class TestOpenMeteoLibDSTNormalisation:
    """
    Tests for __get_pv_forecast_openmeteo_lib DST normalisation.

    The lib-based method computes ``hours_until_tomorrow_midnight`` using
    wall-clock arithmetic which yields 47 on a spring-forward day and 49
    on a fall-back day.  The normalisation guard must always return 48 hourly
    elements.

    ``OpenMeteoSolarForecast`` and ``datetime.now`` are mocked so the test is
    purely unit-level with no network traffic or real astronomical computation.
    """

    _ENTRY = {
        "lat": 50.0,
        "lon": 8.0,
        "name": "test_openmeteo_lib",
        "tilt": 30,
        "azimuth": 0,
        "power": 1000,
        "inverterEfficiency": 0.9,
    }

    def _run(self, fake_now_berlin):
        """
        Run __get_pv_forecast_openmeteo_lib with ``datetime.now`` pinned to
        *fake_now_berlin* and all OpenMeteoSolarForecast internals mocked.

        The OpenMeteo lib usage pattern is:
            async with OpenMeteoSolarForecast(...) as forecast:
                estimate = await forecast.estimate()

        Args:
            fake_now_berlin: A Berlin-timezone-aware datetime representing the
                simulated current time.

        Returns:
            list: Hourly PV forecast (should always be length 48).
        """
        pv = PvInterface({}, [], time_frame_base, {}, timezone="Europe/Berlin")

        # Build the estimate mock: timezone + power_production_at_time
        mock_estimate = MagicMock()
        mock_estimate.timezone = fake_now_berlin.tzinfo
        mock_estimate.power_production_at_time.return_value = 60.0

        # The context manager yields a `forecast` object; calling
        # `await forecast.estimate()` returns the estimate above.
        mock_forecast = MagicMock()
        mock_forecast.estimate = AsyncMock(return_value=mock_estimate)

        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_forecast
        mock_cm.__aexit__.return_value = None

        class _PinnedDatetime(real_datetime.datetime):
            """Datetime subclass with now() pinned to *fake_now_berlin*."""

            _fake = fake_now_berlin

            @classmethod
            def now(cls, tz=None):
                """Return the pinned datetime, optionally converted to *tz*."""
                return cls._fake.astimezone(tz) if tz is not None else cls._fake

        with patch(
            "src.interfaces.pv_interface.OpenMeteoSolarForecast", return_value=mock_cm
        ):
            with patch("src.interfaces.pv_interface.datetime", _PinnedDatetime):
                return pv._PvInterface__get_pv_forecast_openmeteo_lib(self._ENTRY)

    def test_normal_day_returns_48_elements(self):
        """
        Normal day (March 1, 2026, midnight CET): the lib produces 48 hourly
        entries for the 2-day window → output must have exactly 48 elements.
        """
        berlin = pytz.timezone("Europe/Berlin")
        fake_now = berlin.localize(real_datetime.datetime(2026, 3, 1, 0, 0, 0))
        result = self._run(fake_now)
        assert len(result) == 48, f"Normal day: expected 48 elements, got {len(result)}"

    def test_spring_forward_returns_48_elements(self):
        """
        Spring-forward day (March 29, 2026, midnight CET): the lib produces
        only 47 hourly entries because today has 23 wall-clock hours.  The
        normalisation guard must pad the output to 48 elements.
        """
        berlin = pytz.timezone("Europe/Berlin")
        # 00:00 CET – before the spring-forward at 02:00
        fake_now = berlin.localize(real_datetime.datetime(2026, 3, 29, 0, 0, 0))
        result = self._run(fake_now)
        assert (
            len(result) == 48
        ), f"Spring-forward: expected 48 elements after padding, got {len(result)}"

    def test_fall_back_returns_48_elements(self):
        """
        Fall-back day (October 25, 2026, midnight CEST): the lib produces
        49 hourly entries because today has 25 wall-clock hours.  The
        normalisation guard must trim the output to 48 elements.
        """
        berlin = pytz.timezone("Europe/Berlin")
        # 00:00 CEST – before the fall-back at 03:00
        fake_now = berlin.localize(real_datetime.datetime(2026, 10, 25, 0, 0, 0))
        result = self._run(fake_now)
        assert (
            len(result) == 48
        ), f"Fall-back: expected 48 elements after trimming, got {len(result)}"


def test_get_current_pv_forecast_returns_a_copy():
    """
    The accessor must not hand out the cached arrays themselves.

    The EOS request builder discounts the in-progress slot to the fraction of it that
    is still ahead (src/eos_connect.py, get_ems_data). While this returned the cached
    list, that discount accumulated into the cache and shrank the current slot again on
    every optimizer run, so the panel reported a phantom autoscaler correction for a
    single slot and EOS was handed a forecast that decayed between runs.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv.pv_forcast_array = [100.0, 200.0, 300.0]
    pv.pv_forcast_array_raw = [110.0, 210.0, 310.0]

    scaled = pv.get_current_pv_forecast()
    raw = pv.get_current_pv_forecast(scale=False)

    assert scaled is not pv.pv_forcast_array
    assert raw is not pv.pv_forcast_array_raw

    # Exactly what get_ems_data does to the partial slot.
    scaled[0] *= 0.25
    raw[0] *= 0.25

    assert pv.pv_forcast_array == [100.0, 200.0, 300.0]
    assert pv.pv_forcast_array_raw == [110.0, 210.0, 310.0]


def test_partial_slot_discount_does_not_accumulate_across_runs():
    """
    Repeating the partial-slot discount must not compound into the cached forecast.

    Reproduces the observed decay of one slot (718.8 -> 557.5 -> 291.7 -> 79.4 Wh)
    while every autoscaler factor was still 1.0.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    pv.pv_forcast_array = [718.8] * 4

    for remaining_fraction in (0.7756, 0.5233, 0.2722):
        series = pv.get_current_pv_forecast()
        series[1] *= remaining_fraction
        # Each run sees the undecayed forecast, discounted only for its own slot.
        assert series[1] == pytest.approx(718.8 * remaining_fraction)

    assert pv.pv_forcast_array == [718.8] * 4


@pytest.mark.parametrize("autoscaler", [None, "disabled"])
def test_scaled_and_raw_forecasts_are_never_the_same_object(autoscaler):
    """
    apply_autoscaling must copy even when it applies nothing.

    Returning its input let the update loop store one list as both pv_forcast_array and
    pv_forcast_array_raw whenever autoscaling was off, so a single in-place edit would
    corrupt the raw array too - the one the autoscaler records as forecast_kwh and
    trains the correction on.
    """
    pv = PvInterface({}, [], time_frame_base, {}, timezone="UTC")
    if autoscaler == "disabled":
        class _Disabled:
            enabled = False
        pv.set_autoscaler(_Disabled())

    raw = [718.8] * 4
    scaled = pv.apply_autoscaling(raw)

    assert scaled == raw
    assert scaled is not raw

    scaled[1] *= 0.5
    assert raw == [718.8] * 4


# ----------------------------------------------------------------------
# Day totals for the dashboard header
# ----------------------------------------------------------------------


def _pv_with_forecast(scaled, raw=None, tfb=3600):
    """A PvInterface holding a fixed forecast, with no provider or thread behind it."""
    pv = PvInterface({}, [], tfb, {}, timezone="UTC")
    pv.pv_forcast_array = list(scaled)
    pv.pv_forcast_array_raw = list(raw if raw is not None else scaled)
    return pv


def test_day_totals_split_the_horizon_at_local_midnight():
    """Slot 0 is local midnight today, the alignment apply_scaling also relies on."""
    pv = _pv_with_forecast([100.0] * 24 + [50.0] * 24)

    assert pv.get_forecast_day_totals() == {"today_wh": 2400.0, "tomorrow_wh": 1200.0}


def test_day_totals_follow_the_configured_resolution():
    """A 15-minute install publishes 96 slots per day, not 24."""
    pv = _pv_with_forecast([100.0] * 96 + [50.0] * 96, tfb=900)

    assert pv.get_forecast_day_totals() == {"today_wh": 9600.0, "tomorrow_wh": 4800.0}


def test_day_totals_report_the_scaled_array_by_default():
    """
    The header must show what the optimizer and the autoscaling overlay show.

    Summing the raw array here would put the header back out of step with the overlay,
    which is the disagreement this method exists to remove.
    """
    pv = _pv_with_forecast([80.0] * 48, raw=[100.0] * 48)

    assert pv.get_forecast_day_totals()["today_wh"] == 1920.0
    assert pv.get_forecast_day_totals(scale=False)["today_wh"] == 2400.0


def test_day_totals_report_none_for_a_day_with_no_slots():
    """A short forecast must not publish 0.0, which reads as "no sun tomorrow"."""
    pv = _pv_with_forecast([100.0] * 24)

    assert pv.get_forecast_day_totals() == {"today_wh": 2400.0, "tomorrow_wh": None}


def test_day_totals_survive_an_empty_forecast():
    """A fresh install has no forecast yet; the endpoint must still answer."""
    pv = _pv_with_forecast([])

    assert pv.get_forecast_day_totals() == {"today_wh": None, "tomorrow_wh": None}


def test_day_totals_report_none_for_an_unusable_slot():
    """One bad slot must not be summed as zero, silently understating the day."""
    pv = _pv_with_forecast([100.0] * 23 + ["not a number"])

    assert pv.get_forecast_day_totals()["today_wh"] is None
