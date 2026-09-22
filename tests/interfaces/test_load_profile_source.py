"""
Reading a managed load's forecast out of a Home Assistant entity.

The manager's side of this is in `tests/loads/test_profile_pull.py`; what is tested here
is the half that talks to the network - how the URL is built, what the unit does to the
numbers, and whether a payload that does not match the format says so in a way the user
can act on.

The error messages matter as much as the parsing. This is the first thing a user wires
up, they cannot see it working from the sending end, and "could not be reached" against
"nothing found at 'attributes.data'" sends them to two completely different places.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import requests

from src.interfaces.load_profile_source import ProfileSourceError, fetch_profile

BERLIN = ZoneInfo("Europe/Berlin")

DATA_SOURCE = {"url": "http://ha.local:8123", "access_token": "tok"}


def _entry(**overrides):
    entry = {
        "id": "heating",
        "use_ha_central_data_source": True,
        "ha_sensor_name": "sensor.heat_pump_forecast",
        "data_path": "attributes.forecast",
        "value_unit": "W",
        "rated_power_w": 3000.0,
    }
    entry.update(overrides)
    return entry


def _payload(values, step_minutes=60, path="forecast"):
    """A Home Assistant state object with the array in an attribute."""
    entries = []
    for index, value in enumerate(values):
        minute = index * step_minutes
        start = datetime(2026, 1, 15, 0, 0, tzinfo=BERLIN).replace(
            hour=(minute // 60) % 24, minute=minute % 60
        )
        entries.append({"start": start.isoformat(), "value": value})
    return {"state": "unknown", "attributes": {path: entries}}


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _fetch(payload, entry=None, **kwargs):
    with patch("src.interfaces.load_profile_source.requests.get") as get:
        if isinstance(payload, Exception):
            get.side_effect = payload
        else:
            get.return_value = _Response(payload)
        result = fetch_profile(
            entry or _entry(), data_source=DATA_SOURCE, time_zone=BERLIN, **kwargs
        )
    return result, get


# --- the connection ------------------------------------------------------------------

def test_the_central_connection_builds_the_states_url():
    """Built the same way the price and PV sources build theirs."""
    _, get = _fetch(_payload([1000.0, 1000.0]))

    url = get.call_args[0][0]
    assert url == "http://ha.local:8123/api/states/sensor.heat_pump_forecast"
    assert get.call_args[1]["headers"]["Authorization"] == "Bearer tok"


def test_a_standalone_endpoint_is_used_verbatim():
    entry = _entry(
        use_ha_central_data_source=False,
        data_url="http://nas.local/forecast.json",
        data_token="other",
    )
    _, get = _fetch(_payload([1000.0, 1000.0]), entry)

    assert get.call_args[0][0] == "http://nas.local/forecast.json"
    assert get.call_args[1]["headers"]["Authorization"] == "Bearer other"


def test_an_endpoint_with_no_token_sends_no_authorization_header():
    entry = _entry(
        use_ha_central_data_source=False, data_url="http://nas.local/f.json"
    )
    _, get = _fetch(_payload([1000.0, 1000.0]), entry)

    assert "Authorization" not in get.call_args[1]["headers"]


def test_nothing_configured_says_what_to_configure():
    with pytest.raises(ProfileSourceError) as excinfo:
        fetch_profile(_entry(ha_sensor_name=""), data_source=DATA_SOURCE)
    assert "no entity configured" in str(excinfo.value)


# --- units ---------------------------------------------------------------------------

def test_watts_are_integrated_over_each_entry():
    """1500 W held for an hour is 1500 Wh."""
    result, _ = _fetch(_payload([1500.0, 1500.0]))

    assert [entry["value"] for entry in result["entries"]] == [1500.0, 1500.0]
    assert result["resolution_seconds"] == 3600


def test_watts_on_a_quarter_hourly_grid_give_a_quarter_of_the_energy():
    result, _ = _fetch(_payload([1200.0] * 4, step_minutes=15))

    assert [entry["value"] for entry in result["entries"]] == [300.0] * 4
    assert result["resolution_seconds"] == 900


def test_energy_units_pass_through_unscaled():
    result, _ = _fetch(_payload([250.0] * 4, step_minutes=15),
                       _entry(value_unit="Wh"))

    assert [entry["value"] for entry in result["entries"]] == [250.0] * 4


def test_kilowatts_are_scaled_and_integrated():
    result, _ = _fetch(_payload([1.5, 1.5]), _entry(value_unit="kW"))

    assert [entry["value"] for entry in result["entries"]] == [1500.0, 1500.0]


def test_a_half_hourly_grid_is_accepted_on_its_own_span():
    """
    Neither 15 nor 60 minutes, and a hand-built template sensor may well publish it.

    Price and PV refuse anything off their two resolutions. Refusing here would turn a
    working sensor into a support question for no gain, since the entries carry their
    own timestamps.
    """
    result, _ = _fetch(_payload([2000.0] * 4, step_minutes=30))

    assert [entry["value"] for entry in result["entries"]] == [1000.0] * 4
    assert result["resolution_seconds"] == 1800


def test_an_unknown_unit_is_refused_before_anything_is_fetched():
    with pytest.raises(ProfileSourceError) as excinfo:
        fetch_profile(_entry(value_unit="MJ"), data_source=DATA_SOURCE)
    assert "unknown unit 'MJ'" in str(excinfo.value)


def test_a_unit_mistake_is_warned_about_against_the_rated_power(caplog):
    """3 kW rated, but the values imply 12 kW - the classic W-read-as-Wh."""
    with caplog.at_level("WARNING", logger="__main__"):
        _fetch(_payload([3000.0] * 4, step_minutes=15), _entry(value_unit="Wh"))

    assert any("implausible load level" in r.getMessage() for r in caplog.records)


# --- what the payload has to look like -----------------------------------------------

def test_the_array_is_read_from_the_configured_path():
    result, _ = _fetch(_payload([100.0, 100.0], path="data"),
                       _entry(data_path="attributes.data"))

    assert len(result["entries"]) == 2


def test_a_wrong_path_names_the_path():
    with pytest.raises(ProfileSourceError) as excinfo:
        _fetch(_payload([100.0, 100.0]), _entry(data_path="attributes.nope"))
    assert "attributes.nope" in str(excinfo.value)


def test_a_path_pointing_at_something_that_is_not_a_list_says_so():
    with pytest.raises(ProfileSourceError) as excinfo:
        _fetch({"attributes": {"forecast": {"not": "a list"}}})
    assert "not an array" in str(excinfo.value)


def test_an_empty_array_is_not_an_error():
    """A template sensor mid-restart. The manager keeps what it had."""
    result, _ = _fetch({"attributes": {"forecast": []}})
    assert result is None


def test_a_single_entry_is_taken_to_cover_one_hour():
    """
    The normalizer's existing convention, inherited rather than special-cased.

    Worth knowing about: somebody publishing one entry probably means "all day", and
    what they get is one hour. The fix for that is to publish the hours, which is what
    the docs tell them to do - not a guess here that price and PV would not share.
    """
    result, _ = _fetch(_payload([1000.0]))

    assert result["resolution_seconds"] == 3600
    assert [entry["value"] for entry in result["entries"]] == [1000.0]


def test_a_malformed_entry_names_the_keys_it_actually_had():
    """The normalizer's contract, and the reason the format can afford to be strict."""
    with pytest.raises(ProfileSourceError) as excinfo:
        _fetch({"attributes": {"forecast": [
            {"time": "2026-01-15T00:00:00+01:00", "watts": 100},
            {"time": "2026-01-15T01:00:00+01:00", "watts": 100},
        ]}})
    message = str(excinfo.value)
    assert "time" in message and "watts" in message


# --- when the source is not there ----------------------------------------------------

def test_a_timeout_says_it_timed_out():
    with pytest.raises(ProfileSourceError) as excinfo:
        _fetch(requests.exceptions.Timeout())
    assert "did not answer" in str(excinfo.value)


def test_a_connection_failure_does_not_echo_the_exception():
    """
    A request exception can carry the URL, and a standalone URL can carry a token.

    So the message is written here rather than copied from something we did not author
    - the same rule the rest of the codebase's served errors follow.
    """
    with pytest.raises(ProfileSourceError) as excinfo:
        _fetch(requests.exceptions.ConnectionError("https://user:secret@nas.local"))
    message = str(excinfo.value)
    assert "could not be reached" in message
    assert "secret" not in message


def test_a_non_json_body_says_so():
    with pytest.raises(ProfileSourceError) as excinfo:
        _fetch(ValueError("not json"))
    assert "did not return JSON" in str(excinfo.value)


def test_an_http_error_is_reported_as_unreachable():
    with patch("src.interfaces.load_profile_source.requests.get") as get:
        get.return_value = _Response({}, status=404)
        with pytest.raises(ProfileSourceError) as excinfo:
            fetch_profile(_entry(), data_source=DATA_SOURCE, time_zone=BERLIN)
    assert "could not be reached" in str(excinfo.value)
