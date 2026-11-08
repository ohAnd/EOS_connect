"""
The Open-Meteo temperature provider.

It exists because a forecast that is only as available as one host is not available
enough for something a heat pump plans against: akkudoktor's /forecast relays its
upstream provider's rate limit and refuses everyone at once when it triggers.
"""

import logging
import types

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


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    """
    The delay between attempts is real seconds, and the failure tests walk the whole
    ladder. Patched on the shared ``time`` module because that is where the retry loop
    in http_fetch reads it from.
    """
    monkeypatch.setattr("src.interfaces.http_fetch.time.sleep", lambda _s: None)


def _patch_get(monkeypatch, fake_get):
    """
    Swap the module's ``requests`` for one whose ``get`` is ``fake_get``.

    The module reference, not requests.get -- ``requests`` is one shared module
    object and patching its ``get`` reaches every other caller in the process.
    """
    monkeypatch.setattr(
        tf, "requests",
        types.SimpleNamespace(get=fake_get, exceptions=requests.exceptions),
    )


@pytest.fixture(name="captured")
def captured_fixture(monkeypatch):
    """Capture the request instead of making it."""
    seen = {"timeouts": []}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["timeout"] = timeout
        seen["timeouts"].append(timeout)
        return seen.get("response", _Response(_hourly([10.0] * 48)))

    _patch_get(monkeypatch, fake_get)
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


class TestTheRetryLadder:
    """
    Same treatment as the other public forecast APIs (#358): a provider that is only
    slow gets a second attempt with more time, rather than the same one twice.
    """

    def test_a_healthy_answer_costs_one_request(self, captured):
        tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)

        assert captured["timeouts"] == [5]

    def test_the_second_attempt_gets_more_time(self, captured):
        captured["response"] = _Response({}, status=503)

        with pytest.raises(tf.TemperatureForecastError):
            tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)

        assert captured["timeouts"] == [5, 30]
        assert captured["timeouts"] == list(tf.TEMPERATURE_TIMEOUTS)

    def test_a_slow_provider_is_waited_out_instead_of_asked_twice(self, monkeypatch):
        """The failure from #358, on the path most installations actually use."""
        attempts = []

        def slow_then_fine(_url, timeout=None, **_kwargs):
            attempts.append(timeout)
            if timeout < 30:
                raise requests.exceptions.Timeout()
            return _Response(_hourly([7.5] * 48))

        _patch_get(monkeypatch, slow_then_fine)

        values = tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)

        assert attempts == [5, 30]
        assert values[0] == 7.5

    def test_the_timeout_message_names_what_was_tried(self, monkeypatch):
        def always_times_out(*_args, **_kwargs):
            raise requests.exceptions.Timeout()

        _patch_get(monkeypatch, always_times_out)

        with pytest.raises(tf.TemperatureForecastError) as excinfo:
            tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)

        assert "did not answer within 5/30 s" in str(excinfo.value)

    @pytest.mark.parametrize("payload", [{}, _hourly([1.0, "warm", 3.0]), _hourly([500.0] * 48)])
    def test_a_series_that_arrived_is_judged_once(self, captured, payload):
        """
        A series that is missing, non-numeric or implausible says the same thing
        however often it is asked for - only the request is worth repeating.
        """
        captured["response"] = _Response(payload)

        with pytest.raises(tf.TemperatureForecastError):
            tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)

        assert captured["timeouts"] == [5]

    def test_a_body_that_is_not_json_at_all_is_retried(self, captured):
        """
        The other side of the line above: a body that never decoded is usually a
        proxy's HTML error page, and that is as transient as a timeout.
        """
        captured["response"] = _Response(ValueError("not json"))

        with pytest.raises(tf.TemperatureForecastError):
            tf.fetch_openmeteo_temperature(48.8, 8.9, hours=48)

        assert captured["timeouts"] == [5, 30]


def test_the_schema_offers_exactly_the_providers_that_exist():
    """The two lists are separate because the schema imports nothing from interfaces."""
    from src.config_web.schema import TEMPERATURE_SOURCES

    assert sorted(TEMPERATURE_SOURCES) == sorted(tf.TEMPERATURE_PROVIDERS)
    assert tf.DEFAULT_TEMPERATURE_PROVIDER in TEMPERATURE_SOURCES


def test_the_log_does_not_carry_the_users_address(captured, caplog):
    """
    Debug logs get pasted into bug reports, and precise coordinates are a home
    address. The line is there to say how many values came back, not where from.
    """
    with caplog.at_level(logging.DEBUG, logger="__main__"):
        tf.fetch_openmeteo_temperature(52.516275, 13.377704, hours=48)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "52.51" not in logged and "13.37" not in logged
    assert "48 hourly values" in logged
    # The request itself of course still carries them.
    assert captured["params"]["latitude"] == 52.516275
