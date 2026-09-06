"""
The Open-Meteo temperature provider.

It exists because a forecast that is only as available as one host is not available
enough for something a heat pump plans against: akkudoktor's /forecast relays its
upstream provider's rate limit and refuses everyone at once when it triggers.
"""

import pytest
import requests

from src.interfaces import temperature_forecast as tf


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


def _hourly(values):
    return {"hourly": {"temperature_2m": list(values)}}


@pytest.fixture(name="captured")
def captured_fixture(monkeypatch):
    """Capture the request instead of making it."""
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["timeout"] = timeout
        return seen.get("response", _Response(_hourly([10.0] * 48)))

    monkeypatch.setattr(tf.requests, "get", fake_get)
    return seen


def test_it_asks_for_whole_local_days(captured):
    """
    Slot 0 is local midnight everywhere else in EOS Connect. Open-Meteo cuts its days on
    the timezone it is given, so asking with one means no realignment is needed.
    """
    tf.fetch_openmeteo_temperature(48.8, 8.9, timezone="Europe/Berlin", hours=48)

    assert captured["params"]["timezone"] == "Europe/Berlin"
    assert captured["params"]["forecast_days"] == 2
    assert captured["params"]["hourly"] == "temperature_2m"
    assert captured["params"]["latitude"] == 48.8


def test_a_partial_day_still_asks_for_a_whole_one(captured):
    tf.fetch_openmeteo_temperature(48.8, 8.9, hours=30)
    assert captured["params"]["forecast_days"] == 2


def test_it_returns_exactly_the_hours_asked_for(captured):
    captured["response"] = _Response(_hourly([float(h) for h in range(48)]))
    values = tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)
    assert len(values) == 48
    assert values[0] == 0.0
    assert values[-1] == 47.0


def test_a_short_series_is_padded_rather_than_rejected(captured):
    """A trimmed horizon is not an error, but the array has a fixed length downstream."""
    captured["response"] = _Response(_hourly([5.0] * 40))
    values = tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)
    assert len(values) == 48
    assert values[-1] == 5.0


def test_a_long_series_is_trimmed(captured):
    captured["response"] = _Response(_hourly([5.0] * 72))
    assert len(tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)) == 48


@pytest.mark.parametrize("payload,fragment", [
    ({}, "no temperature series"),
    ({"hourly": {}}, "no temperature series"),
    ({"hourly": {"temperature_2m": []}}, "no temperature series"),
    (_hourly([1.0, "warm", 3.0]), "non-numeric"),
])
def test_a_malformed_answer_is_an_error_not_a_curve(captured, payload, fragment):
    captured["response"] = _Response(payload)
    with pytest.raises(tf.TemperatureForecastError) as excinfo:
        tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("bad", [[500.0] * 48, [-200.0] * 48])
def test_implausible_values_are_refused(captured, bad):
    """A PV figure leaking into the temperature array looks like a number and ruins everything."""
    captured["response"] = _Response(_hourly(bad))
    with pytest.raises(tf.TemperatureForecastError) as excinfo:
        tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)
    assert "plausible range" in str(excinfo.value)


def test_an_http_error_names_the_provider(captured):
    captured["response"] = _Response({}, status=503)
    with pytest.raises(tf.TemperatureForecastError) as excinfo:
        tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)
    assert "Open-Meteo is unreachable" in str(excinfo.value)


def test_a_non_json_answer_is_reported_as_such(captured):
    captured["response"] = _Response(ValueError("not json"))
    with pytest.raises(tf.TemperatureForecastError) as excinfo:
        tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)
    assert "not JSON" in str(excinfo.value)


def test_the_schema_offers_exactly_the_providers_that_exist():
    """The two lists are separate because the schema imports nothing from interfaces."""
    from src.config_web.schema import TEMPERATURE_SOURCES

    assert sorted(TEMPERATURE_SOURCES) == sorted(tf.TEMPERATURE_PROVIDERS)
    assert tf.DEFAULT_TEMPERATURE_PROVIDER in TEMPERATURE_SOURCES
