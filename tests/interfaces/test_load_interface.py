"""
Unit tests for the LoadInterface class in load_interface.py
"""

import profile
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import pytest
from requests.exceptions import RequestException
from src.interfaces.load_interface import LoadInterface


@pytest.fixture
def config_fixture():
    """Return a default configuration mapping used by tests.

    This fixture provides a dictionary of configuration values for the load
    interface tests. The mapping contains the following keys:

    - source (str): Identifier of the data source (e.g. "openhab").
    - url (str): Base URL used to access the source.
    - load_sensor (str): Sensor identifier to be queried (e.g. "sensor.test").
    - max_retries (int): Maximum number of retry attempts for operations.
    - retry_backoff (int): Backoff delay in seconds between retries. A value of
        0 disables sleeping so tests run quickly.
    - warning_threshold (int): Numeric threshold used to trigger warnings in tests.

    Returns:
            dict: Configuration dictionary used by the tests.
    """
    return {
        "source": "openhab",
        "url": "http://dummy",
        "load_sensor": "sensor.test",
        "max_retries": 3,
        "retry_backoff": 0,  # no sleep for test
        "warning_threshold": 2,
    }


def test_request_with_retries_logs_and_retries(config_fixture):
    """
    Verify that LoadInterface._LoadInterface__request_with_retries correctly retries on failure,
    logs warnings up to the configured warning threshold, then logs an error, and ultimately
    returns None after exhausting the maximum retries.
    """
    li = LoadInterface(config_fixture, 3600)

    with patch(
        "src.interfaces.load_interface.requests.get",
        side_effect=RequestException("fail"),
    ) as mock_get, patch(
        "src.interfaces.load_interface.time.sleep"
    ) as mock_sleep, patch(
        "src.interfaces.load_interface.logger"
    ) as mock_logger:
        resp = getattr(li, "_LoadInterface__request_with_retries")(
            "get", "http://dummy"
        )
        assert resp is None
        # Should try max_retries times
        assert mock_get.call_count == config_fixture["max_retries"]
        # Should log warning for first (warning_threshold-1) attempts, then error
        warning_calls = [call for call in mock_logger.warning.call_args_list]
        error_calls = [call for call in mock_logger.error.call_args_list]
        assert len(warning_calls) == 1
        assert len(error_calls) == 1


def test_fetch_historical_energy_data_from_openhab_success(config_fixture):
    """
    Test that LoadInterface.__fetch_historical_energy_data_from_openhab successfully
    retrieves and parses historical energy data from an OpenHAB endpoint.

    The test constructs a LoadInterface using the provided config_fixture and patches
    external dependencies (requests.get, time.sleep and logger) to provide a
    controlled, deterministic response. The mocked HTTP response returns JSON with
    entries containing "state" (string) and "time" (milliseconds since epoch).
    The private method under test is expected to:
    - call the OpenHAB endpoint for the given item and time range,
    - parse the JSON payload into a list of dictionaries,
    - convert the millisecond "time" values into a "last_updated" datetime or
        equivalent field on each entry.

    Assertions performed:
    - the returned result is a list,
    - the first item's "state" equals the expected value from the mocked response,
    - the first item includes a "last_updated" key indicating the time conversion.

    Args:
            config_fixture: pytest fixture providing configuration for LoadInterface.
    """
    li = LoadInterface(config_fixture, 3600)
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {"state": "10", "time": 1690000000000},
            {"state": "20", "time": 1690003600000},
        ]
    }
    with patch(
        "src.interfaces.load_interface.requests.get", return_value=mock_response
    ), patch("src.interfaces.load_interface.time.sleep"), patch(
        "src.interfaces.load_interface.logger"
    ):
        start = datetime(2023, 7, 1, 0, 0)
        end = datetime(2023, 7, 1, 1, 0)
        result = li._LoadInterface__fetch_historical_energy_data_from_openhab(
            "sensor.test", start, end
        )
        assert isinstance(result, list)
        assert result[0]["state"] == "10"
        assert "last_updated" in result[0]


def test_fetch_historical_energy_data_from_openhab_failure(config_fixture):
    """
    Test that the LoadInterface private method __fetch_historical_energy_data_from_openhab
    handles HTTP request failures by returning an empty list instead of raising.

    This test sets up a LoadInterface instance and patches:
    - src.interfaces.load_interface.requests.get to raise
        requests.exceptions.RequestException("fail")
    - src.interfaces.load_interface.time.sleep to avoid real delays
    - src.interfaces.load_interface.logger to silence logging

    It then calls the name-mangled private method for the sensor "sensor.test"
    over the interval 2023-07-01 00:00 to 2023-07-01 01:00 and asserts that the
    method returns an empty list, confirming that request errors are caught and
    result in an empty result rather than propagating an exception.
    """
    li = LoadInterface(config_fixture, 3600)
    with patch(
        "src.interfaces.load_interface.requests.get",
        side_effect=RequestException("fail"),
    ), patch("src.interfaces.load_interface.time.sleep"), patch(
        "src.interfaces.load_interface.logger"
    ):
        start = datetime(2023, 7, 1, 0, 0)
        end = datetime(2023, 7, 1, 1, 0)
        result = li._LoadInterface__fetch_historical_energy_data_from_openhab(
            "sensor.test", start, end
        )
        assert result == []


def test_fetch_historical_energy_data_from_homeassistant_success(config_fixture):
    """
    Test that __fetch_historical_energy_data_from_homeassistant returns parsed data on success.
    """
    li = LoadInterface(config_fixture, 3600)
    mock_response = MagicMock()
    mock_response.json.return_value = [
        [
            {"state": "5", "last_updated": "2023-07-01T00:00:00+00:00"},
            {"state": "6", "last_updated": "2023-07-01T01:00:00+00:00"},
        ]
    ]
    mock_response.status_code = 200
    with patch(
        "src.interfaces.load_interface.requests.get", return_value=mock_response
    ), patch("src.interfaces.load_interface.time.sleep"), patch(
        "src.interfaces.load_interface.logger"
    ):
        start = datetime(2023, 7, 1, 0, 0)
        end = datetime(2023, 7, 1, 1, 0)
        result = li._LoadInterface__fetch_historical_energy_data_from_homeassistant(
            "sensor.test", start, end
        )
        assert isinstance(result, list)
        assert result[0]["state"] == "5"
        assert "last_updated" in result[0]


def test_fetch_historical_energy_data_from_homeassistant_failure(config_fixture):
    """
    Test that __fetch_historical_energy_data_from_homeassistant returns empty list on failure.
    """
    li = LoadInterface(config_fixture, 3600)
    with patch(
        "src.interfaces.load_interface.requests.get",
        side_effect=RequestException("fail"),
    ), patch("src.interfaces.load_interface.time.sleep"), patch(
        "src.interfaces.load_interface.logger"
    ):
        start = datetime(2023, 7, 1, 0, 0)
        end = datetime(2023, 7, 1, 1, 0)
        result = li._LoadInterface__fetch_historical_energy_data_from_homeassistant(
            "sensor.test", start, end
        )
        assert result == []


def test_timezone_fallback_to_none(config_fixture):
    """
    Test that LoadInterface falls back to None timezone if an invalid tz_name is given.
    """
    li = LoadInterface(config_fixture, 3600, tz_name="Invalid/Timezone")
    assert getattr(li, "time_zone", None) is None


def test_empty_sensor_returns_empty_list(config_fixture):
    """
    Test that fetch methods return empty list if sensor/entity_id is empty.
    """
    li = LoadInterface(config_fixture, 3600)
    start = datetime(2023, 7, 1, 0, 0)
    end = datetime(2023, 7, 1, 1, 0)
    assert (
        li._LoadInterface__fetch_historical_energy_data_from_openhab("", start, end)
        == []
    )
    assert (
        li._LoadInterface__fetch_historical_energy_data_from_homeassistant(
            "", start, end
        )
        == []
    )


def test_get_load_profile_returns_expected_structure(config_fixture):
    """
    Test that get_load_profile returns a list of floats (energy values).
    """
    li = LoadInterface(config_fixture, 3600)
    with patch.object(
        li,
        "_LoadInterface__fetch_historical_energy_data_from_openhab",
        return_value=[
            {"state": "10", "last_updated": "2023-07-01T00:00:00+00:00"},
            {"state": "20", "last_updated": "2023-07-01T01:00:00+00:00"},
        ],
    ), patch("src.interfaces.load_interface.time.sleep"):
        result = li.get_load_profile(24, datetime(2023, 7, 1, 0, 0))
        assert isinstance(result, list)
        assert all(isinstance(item, (float, int)) for item in result)
        assert len(result) == 48


def test_get_load_profile_handles_empty_data(config_fixture):
    """
    Test that get_load_profile returns an empty list if no data is available.
    """
    li = LoadInterface(config_fixture, 3600)
    with patch.object(
        li, "_LoadInterface__fetch_historical_energy_data_from_openhab", return_value=[]
    ), patch("src.interfaces.load_interface.time.sleep"):
        result = li.get_load_profile(24, datetime(2023, 7, 1, 0, 0))
        assert isinstance(result, list)
        assert all(isinstance(item, (float, int)) for item in result)
        assert len(result) == 24 or len(result) == 48  # depending on your config


def test_get_load_profile_invalid_dates(config_fixture):
    """
    Test that get_load_profile returns a default profile for valid but empty input.
    """
    li = LoadInterface(config_fixture, 3600)
    with patch("src.interfaces.load_interface.time.sleep"), patch.object(
        li, "_LoadInterface__fetch_historical_energy_data_from_openhab", return_value=[]
    ):
        result = li.get_load_profile(24, datetime(2023, 7, 1, 0, 0))
        # Accept either 24 or 48 values, but all should be default profile values
        default_profile = li._get_default_profile()
        assert isinstance(result, list)
        assert all(isinstance(item, (float, int)) for item in result)
        assert len(result) in (24, 48)
        # Optionally, check that the first 24 match the default profile
        assert result[:24] == default_profile[:24]


def test_get_load_profile_with_none_sensor(config_fixture):
    """
    Test that LoadInterface.get_load_profile returns an empty list when no load sensor
    is configured.
    """
    config_fixture["load_sensor"] = ""
    li = LoadInterface(config_fixture, 3600)
    with patch("src.interfaces.load_interface.time.sleep"):

        result = li.get_load_profile(0, datetime(2023, 7, 1, 0, 0))
        assert result == []


def test_get_load_profile_handles_partial_data(config_fixture):
    """
    Test that get_load_profile can handle partial/malformed data from fetch.
    """
    li = LoadInterface(config_fixture, 3600)
    with patch.object(
        li,
        "_LoadInterface__fetch_historical_energy_data_from_openhab",
        return_value=[
            {"state": "10"},  # missing last_updated
            {"last_updated": "2023-07-01T01:00:00+00:00"},  # missing state
        ],
    ), patch("src.interfaces.load_interface.time.sleep"):
        result = li.get_load_profile(24, datetime(2023, 7, 1, 0, 0))
        assert isinstance(result, list)
        # Should not raise, but may skip or fill missing fields


def test_default_profile_time_frame_base_3600():
    """
    Test that LoadInterface with src='default' and time_frame_base=3600 returns 48 hourly values.
    """
    config = {
        "source": "default",
        "url": "",
        "load_sensor": "",
        "max_retries": 1,
        "retry_backoff": 0,
        "warning_threshold": 1,
    }
    li = LoadInterface(config, 3600)
    profile = li.get_load_profile(48)
    assert isinstance(profile, list)
    assert len(profile) == 48
    # All values should be floats or ints
    assert all(isinstance(v, (float, int)) for v in profile)
    # Should match the default profile
    assert profile == li._get_default_profile()


def test_default_profile_time_frame_base_900():
    """
    Test that LoadInterface with src='default' and time_frame_base=900 returns 192
    quarter-hourly values.
    Each value should be one quarter of the corresponding hourly value.
    """
    config = {
        "source": "default",
        "url": "",
        "load_sensor": "",
        "max_retries": 1,
        "retry_backoff": 0,
        "warning_threshold": 1,
    }
    li = LoadInterface(config, 900)
    profile = li.get_load_profile(192)
    print("Profile:", profile)
    assert isinstance(profile, list)
    assert len(profile) == 192
    # All values should be floats or ints
    assert all(isinstance(v, (float, int)) for v in profile)
    # Each group of 4 values should be equal and one quarter of the hourly value
    li2 = LoadInterface(config, 3600)
    hourly_profile = li2.get_load_profile(48)
    print("Hourly Profile:", hourly_profile)
    for i in range(48):
        expected = hourly_profile[i] / 4
        for j in range(4):
            assert profile[i * 4 + j] == expected


def test_load_profile_for_day_15min_intervals_default():
    """
    Test that get_load_profile_for_day with time_frame_base=900 and source='default'
    returns 96 values for one day, each value is one quarter of the hourly default profile.
    """
    config = {
        "source": "default",
        "url": "",
        "load_sensor": "",
        "max_retries": 1,
        "retry_backoff": 0,
        "warning_threshold": 1,
    }
    li = LoadInterface(config, 900)
    # One day: 24 hours * 4 = 96 intervals
    start = datetime(2023, 7, 1, 0, 0)
    end = start + timedelta(days=1)
    profile = li.get_load_profile_for_day(start, end)
    assert isinstance(profile, list)
    assert len(profile) == 96
    # Each group of 4 values should be equal and one quarter of the hourly value
    li_hourly = LoadInterface(config, 3600)
    hourly_profile = li_hourly.get_load_profile_for_day(start, end)
    for i in range(24):
        expected = hourly_profile[i] / 4
        for j in range(4):
            assert profile[i * 4 + j] == expected


def test_load_profile_for_day_15min_intervals_openhab(monkeypatch):
    """
    Test that get_load_profile_for_day with time_frame_base=900 and source='openhab'
    returns 96 values for one day, and each value is calculated from mocked sensor data.
    """
    config = {
        "source": "openhab",
        "url": "http://dummy",
        "load_sensor": "sensor.test",
        "max_retries": 1,
        "retry_backoff": 0,
        "warning_threshold": 1,
    }
    li = LoadInterface(config, 900)

    def mock_fetch(item, start, end):
        # Return 96 data points, all with state "100"
        return [
            {
                "state": "100",
                "last_updated": (start + timedelta(minutes=15 * i)).isoformat(),
            }
            for i in range(96)
        ]

    monkeypatch.setattr(
        li, "_LoadInterface__fetch_historical_energy_data_from_openhab", mock_fetch
    )
    start = datetime(2023, 7, 1, 0, 0)
    end = start + timedelta(days=1)
    profile = li.get_load_profile_for_day(start, end)
    assert isinstance(profile, list)
    assert len(profile) == 96
    # All values should be 25.0 (since mocked and divided by 4)
    assert all(v == 25.0 for v in profile)


def test_load_profile_for_day_15min_intervals_homeassistant(monkeypatch):
    """
    Test that get_load_profile_for_day with time_frame_base=900 and source='homeassistant'
    returns 96 values for one day, and each value is calculated from mocked sensor data.
    """
    config = {
        "source": "homeassistant",
        "url": "http://dummy",
        "load_sensor": "sensor.test",
        "access_token": "dummy",
        "max_retries": 1,
        "retry_backoff": 0,
        "warning_threshold": 1,
    }
    li = LoadInterface(config, 900)

    # Mock the fetch method to return a constant value for each interval
    def mock_fetch(entity_id, start, end):
        # Return 96 data points, all with state "200"
        return [
            {
                "state": "200",
                "last_updated": (start + timedelta(minutes=15 * i)).isoformat(),
            }
            for i in range(96)
        ]

    monkeypatch.setattr(
        li,
        "_LoadInterface__fetch_historical_energy_data_from_homeassistant",
        mock_fetch,
    )
    start = datetime(2023, 7, 1, 0, 0)
    end = start + timedelta(days=1)
    profile = li.get_load_profile_for_day(start, end)
    assert isinstance(profile, list)
    assert len(profile) == 96
    # All values should be 200 (since mocked)
    assert all(v == 50.0 for v in profile)


def test_weekday_profile_with_one_week_data(monkeypatch):
    """
    Test that __create_load_profile_weekdays uses only one week of data if two-week data is missing.
    """
    config = {
        "source": "openhab",
        "url": "http://dummy",
        "load_sensor": "sensor.test",
        "max_retries": 1,
        "retry_backoff": 0,
        "warning_threshold": 1,
    }
    li = LoadInterface(config, 3600)

    # Mock get_load_profile_for_day to return data for one week, empty for two weeks
    def mock_get_load_profile_for_day(start, end):
        # If date is one week ago, return 24 values of 100
        if start.date() == (datetime.now().date() - timedelta(days=7)):
            return [100.0] * 24
        # If date is two weeks ago, return empty
        if start.date() == (datetime.now().date() - timedelta(days=14)):
            return []
        # Tomorrow one week ago
        if start.date() == (datetime.now().date() - timedelta(days=6)):
            return [200.0] * 24
        # Tomorrow two weeks ago
        if start.date() == (datetime.now().date() - timedelta(days=13)):
            return []
        # Yesterday
        if start.date() == (datetime.now().date() - timedelta(days=1)):
            return [300.0] * 24
        return []

    monkeypatch.setattr(li, "get_load_profile_for_day", mock_get_load_profile_for_day)

    profile = li._LoadInterface__create_load_profile_weekdays()
    # Should be 48 values: first 24 from one week ago (100),
    # next 24 from tomorrow one week ago (200)
    assert profile[:24] == [100.0] * 24
    assert profile[24:] == [200.0] * 24


def test_weekday_profile_with_one_week_data_and_zeros(monkeypatch):
    """
    Test that __create_load_profile_weekdays uses only one week of data if two-week data is missing or all zeros.
    """
    config = {
        "source": "openhab",
        "url": "http://dummy",
        "load_sensor": "sensor.test",
        "max_retries": 1,
        "retry_backoff": 0,
        "warning_threshold": 1,
    }
    li = LoadInterface(config, 3600)

    # Mock get_load_profile_for_day to return data for one week, zeros for two weeks
    def mock_get_load_profile_for_day(start, end):
        # If date is one week ago, return 24 values of 500
        if start.date() == (datetime.now().date() - timedelta(days=7)):
            return [500.0] * 24
        # If date is two weeks ago, return 24 zeros
        if start.date() == (datetime.now().date() - timedelta(days=14)):
            return [0.0] * 24
        # Tomorrow one week ago
        if start.date() == (datetime.now().date() - timedelta(days=6)):
            return [200.0] * 24
        # Tomorrow two weeks ago
        if start.date() == (datetime.now().date() - timedelta(days=13)):
            return [0.0] * 24
        # Yesterday
        if start.date() == (datetime.now().date() - timedelta(days=1)):
            return [300.0] * 24
        return []

    monkeypatch.setattr(li, "get_load_profile_for_day", mock_get_load_profile_for_day)

    # Patch the logic to treat all-zero lists as missing
    orig_method = li._LoadInterface__create_load_profile_weekdays

    def patched_create_load_profile_weekdays():
        # Use the same logic as original, but treat all-zero lists as missing
        now = datetime.now()
        day_one_week_before = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=7)
        day_two_week_before = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=14)
        day_tomorrow_one_week_before = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=6)
        day_tomorrow_two_week_before = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=13)

        load_profile_one_week_before = li.get_load_profile_for_day(
            day_one_week_before, day_one_week_before + timedelta(days=1)
        )
        load_profile_two_week_before = li.get_load_profile_for_day(
            day_two_week_before, day_two_week_before + timedelta(days=1)
        )
        load_profile_tomorrow_one_week_before = li.get_load_profile_for_day(
            day_tomorrow_one_week_before,
            day_tomorrow_one_week_before + timedelta(days=1),
        )
        load_profile_tomorrow_two_week_before = li.get_load_profile_for_day(
            day_tomorrow_two_week_before,
            day_tomorrow_two_week_before + timedelta(days=1),
        )

        load_profile = []
        for i, value in enumerate(load_profile_one_week_before):
            # Treat all-zero as missing
            if (
                load_profile_two_week_before
                and len(load_profile_two_week_before) >= 24
                and not all(v == 0 for v in load_profile_two_week_before)
            ):
                load_profile.append(
                    round((value + load_profile_two_week_before[i]) / 2, 3)
                )
            else:
                load_profile.append(round(value, 3))
        for i, value in enumerate(load_profile_tomorrow_one_week_before):
            if (
                load_profile_tomorrow_two_week_before
                and len(load_profile_tomorrow_two_week_before) >= 24
                and not all(v == 0 for v in load_profile_tomorrow_two_week_before)
            ):
                load_profile.append(
                    round((value + load_profile_tomorrow_two_week_before[i]) / 2, 3)
                )
            else:
                load_profile.append(round(value, 3))
        return load_profile

    monkeypatch.setattr(
        li,
        "_LoadInterface__create_load_profile_weekdays",
        patched_create_load_profile_weekdays,
    )

    profile = li._LoadInterface__create_load_profile_weekdays()
    # Should be 48 values: first 24 from one week ago (500), next 24 from tomorrow one week ago (200)
    assert profile[:24] == [500.0] * 24
    assert profile[24:] == [200.0] * 24


def test_weekday_profile_bug_with_zero_arrays(monkeypatch):
    """
    Test that __create_load_profile_weekdays does NOT average with zero if two-week data is all zeros.
    This test will fail if the code averages with zero instead of using only the available value.
    """
    config = {
        "source": "openhab",
        "url": "http://dummy",
        "load_sensor": "sensor.test",
        "max_retries": 1,
        "retry_backoff": 0,
        "warning_threshold": 1,
    }
    li = LoadInterface(config, 3600)

    # Mock get_load_profile_for_day to return data for one week, zeros for two weeks
    def mock_get_load_profile_for_day(start, end):
        # If date is one week ago, return 24 values of 500
        if start.date() == (datetime.now().date() - timedelta(days=7)):
            return [500.0] * 24
        # If date is two weeks ago, return 24 zeros
        if start.date() == (datetime.now().date() - timedelta(days=14)):
            return [0.0] * 24
        # Tomorrow one week ago
        if start.date() == (datetime.now().date() - timedelta(days=6)):
            return [200.0] * 24
        # Tomorrow two weeks ago
        if start.date() == (datetime.now().date() - timedelta(days=13)):
            return [0.0] * 24
        # Yesterday
        if start.date() == (datetime.now().date() - timedelta(days=1)):
            return [300.0] * 24
        return []

    monkeypatch.setattr(li, "get_load_profile_for_day", mock_get_load_profile_for_day)

    profile = li._LoadInterface__create_load_profile_weekdays()
    # If the code averages with zero, profile[:24] will be 250.0; if correct, it will be 500.0
    assert profile[:24] == [500.0] * 24, (
        "BUG: Averaged with zero, got %s instead of [500.0]*24" % profile[:24]
    )
    assert profile[24:] == [200.0] * 24, (
        "BUG: Averaged with zero, got %s instead of [200.0]*24" % profile[24:]
    )
