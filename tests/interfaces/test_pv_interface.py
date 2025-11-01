# pylint: disable=protected-access
"""
Unit tests for the PvInterface class and related functionality.

This module contains pytest-based tests for error handling, configuration validation,
forecast aggregation, and API fallback logic in the PvInterface implementation.
"""

import threading
import requests
import pytest
from src.interfaces.pv_interface import PvInterface


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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
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
    pv = PvInterface({}, [], {}, timezone="UTC")
    result = pv._PvInterface__get_default_pv_forcast(100)
    assert len(result) == 48
    assert all(isinstance(x, float) or isinstance(x, int) for x in result)


def test_default_temperature_forecast_length_and_values():
    """
    Test that the default temperature forecast returns 48 values of 15.0.
    """
    pv = PvInterface({}, [], {}, timezone="UTC")
    result = pv._PvInterface__get_default_temperature_forecast()
    assert len(result) == 48
    assert all(x == 15.0 for x in result)


def test_check_config_missing_parameters():
    """
    Test that missing required config parameters cause SystemExit.
    """
    config = [{"lat": 50, "lon": 8}]  # missing required parameters
    with pytest.raises(SystemExit):
        PvInterface({}, config, {}, timezone="UTC")


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
    pv = PvInterface({}, config, {}, timezone="UTC")
    # Monkeypatch get_pv_forecast to return fixed arrays
    pv.get_pv_forecast = lambda entry, tgt_duration=24: [entry["power"]] * tgt_duration
    result = pv.get_summarized_pv_forecast(24)
    assert result == [300] * 24


def test_api_error_triggers_fallback(monkeypatch):
    """
    Test that an API error triggers fallback to default PV forecast.
    """
    pv = PvInterface({}, [], {}, timezone="UTC")
    pv._retry_request = lambda req, err, **kwargs: err("api_error", Exception("fail"))
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
    assert result == [0] * 24
    assert pv.pv_forcast_request_error["error"] in (None, "api_error")


def test_get_current_pv_forecast_returns_array():
    """
    Test that get_current_pv_forecast returns the correct array.
    """
    pv = PvInterface({}, [], {}, timezone="UTC")
    pv.pv_forcast_array = [1, 2, 3]
    assert pv.get_current_pv_forecast() == [1, 2, 3]


def test_get_current_temp_forecast_returns_array():
    """
    Test that get_current_temp_forecast returns the correct array.
    """
    pv = PvInterface({}, [], {}, timezone="UTC")
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

    pv = PvInterface(config_source, config, {}, timezone="UTC")
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
