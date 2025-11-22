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
    Test that missing required config parameters cause SystemExit.
    """
    config = [{"lat": 50, "lon": 8}]  # missing required parameters
    with pytest.raises(SystemExit):
        PvInterface({}, config, time_frame_base, {}, timezone="UTC")


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


def test_api_error_triggers_fallback(monkeypatch):
    """
    Test that an API error triggers fallback to default PV forecast.
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
    assert result == [0] * 48
    assert pv.pv_forcast_request_error["error"] in (None, "api_error")


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
        "resource_id": "dummy_resource",
    }
    config_source = {"source": "solcast", "api_key": "dummy_key"}

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
