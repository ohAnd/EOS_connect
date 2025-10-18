import pytest
from src.interfaces.pv_interface import PvInterface


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    class DummyThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr("threading.Thread", DummyThread)


def test_handle_interface_error_updates_state_and_returns_empty(monkeypatch):
    """
    Test that _handle_interface_error updates the error state and returns an empty list.
    """
    monkeypatch.setattr(PvInterface, "_PvInterface__update_pv_state_loop", lambda self: None)
    pv = PvInterface({}, [], {}, timezone="UTC")
    error_type = "test_error"
    message = "Test error message"
    config_entry = {"name": "test"}
    source = "test_source"

    result = pv._handle_interface_error(error_type, message, config_entry, source)

    assert result == []
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
        call_count["count"] += 1
        return "success"

    def error_handler(error_type, exception):
        return "error"

    result = pv._retry_request(request_func, error_handler, max_retries=3)
    assert result == "success"
    assert call_count["count"] == 1


def test_retry_request_failure():
    """
    Test that _retry_request retries the correct number of times and calls error_handler after max retries.
    """
    pv = PvInterface({}, [], {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        call_count["count"] += 1
        raise ValueError("fail")

    def error_handler(error_type, exception):
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
        call_count["count"] += 1
        if call_count["count"] < 2:
            raise ValueError("fail")
        return "success"

    def error_handler(error_type, exception):
        return "error"

    result = pv._retry_request(request_func, error_handler, max_retries=3, delay=0)
    assert result == "success"
    assert call_count["count"] == 2


def test_retry_request_handles_timeout():
    """
    Test that _retry_request handles timeout exceptions.
    """
    import requests
    pv = PvInterface({}, [], {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        call_count["count"] += 1
        raise requests.exceptions.Timeout("timeout")

    def error_handler(error_type, exception):
        assert error_type == "timeout"
        return "timeout_error"

    result = pv._retry_request(request_func, error_handler, max_retries=2, delay=0)
    assert result == "timeout_error"
    assert call_count["count"] == 2


def test_retry_request_handles_request_exception():
    """
    Test that _retry_request handles generic request exceptions.
    """
    import requests
    pv = PvInterface({}, [], {}, timezone="UTC")
    call_count = {"count": 0}

    def request_func():
        call_count["count"] += 1
        raise requests.exceptions.RequestException("request failed")

    def error_handler(error_type, exception):
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
        call_count["count"] += 1
        raise ValueError("json error")

    def error_handler(error_type, exception):
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
        call_count["count"] += 1
        raise KeyError("parsing error")

    def error_handler(error_type, exception):
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
    assert result1 == []
    assert result2 == []
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
    assert result == []
    assert pv.pv_forcast_request_error["error"] == "error"
    assert pv.pv_forcast_request_error["message"] == "msg"
    assert pv.pv_forcast_request_error["config_entry"] == {}
    assert pv.pv_forcast_request_error["source"] == "src"
    assert pv.pv_forcast_request_error["timestamp"] is not None


def test_default_pv_forecast_length_and_values():
    pv = PvInterface({}, [], {}, timezone="UTC")
    result = pv._PvInterface__get_default_pv_forcast(100)
    assert len(result) == 48
    assert all(isinstance(x, float) or isinstance(x, int) for x in result)


def test_default_temperature_forecast_length_and_values():
    pv = PvInterface({}, [], {}, timezone="UTC")
    result = pv._PvInterface__get_default_temperature_forecast()
    assert len(result) == 48
    assert all(x == 15.0 for x in result)


def test_check_config_missing_parameters():
    config = [{"lat": 50, "lon": 8}]  # missing required parameters
    with pytest.raises(SystemExit):
        PvInterface({}, config, {}, timezone="UTC")


def test_summarized_pv_forecast_aggregation():
    config = [
        {"name": "A", "lat": 50, "lon": 8, "azimuth": 180, "tilt": 30, "power": 100, "powerInverter": 100, "inverterEfficiency": 1.0},
        {"name": "B", "lat": 51, "lon": 9, "azimuth": 180, "tilt": 30, "power": 200, "powerInverter": 200, "inverterEfficiency": 1.0}
    ]
    pv = PvInterface({}, config, {}, timezone="UTC")
    # Monkeypatch get_pv_forecast to return fixed arrays
    pv.get_pv_forecast = lambda entry, tgt_duration=24: [entry["power"]] * tgt_duration
    result = pv.get_summarized_pv_forecast(24)
    assert result == [300] * 24


def test_api_error_triggers_fallback(monkeypatch):
    pv = PvInterface({}, [], {}, timezone="UTC")
    pv._retry_request = lambda req, err, **kwargs: err("api_error", Exception("fail"))
    result = pv._PvInterface__get_pv_forecast_akkudoktor_api(
        pv_config_entry={
            "lat": 50,
            "lon": 8,
            "azimuth": 180,
            "tilt": 30,
            "power": 100,
            "horizon": "0",
            "powerInverter": 800,
            "inverterEfficiency": 0.95,
            "horizon": "0"
        }
    )
    assert result == [0] * 24
    assert pv.pv_forcast_request_error["error"] in (None, "api_error")


def test_get_current_pv_forecast_returns_array():
    pv = PvInterface({}, [], {}, timezone="UTC")
    pv.pv_forcast_array = [1, 2, 3]
    assert pv.get_current_pv_forecast() == [1, 2, 3]


def test_get_current_temp_forecast_returns_array():
    pv = PvInterface({}, [], {}, timezone="UTC")
    pv.temp_forecast_array = [15, 16, 17]
    assert pv.get_current_temp_forecast() == [15, 16, 17]
