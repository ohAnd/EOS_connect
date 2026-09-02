# pylint: disable=protected-access
"""
Tests for akkudoktor's relayed upstream errors — issue #289.

``api.akkudoktor.net`` proxies a weather service and does not pass its status through:
every upstream fault arrives as an HTTP 500 whose *body* names the real code, e.g.
``Request failed with status code 429``.  The status line alone is therefore useless —
it is identical for a transient quota exhaustion and for a request the upstream rejects
outright — and the two need opposite responses: wait, or report.
"""

import logging
from datetime import datetime, timedelta

import pytest
import requests

from src.interfaces.pv_interface import (
    AKKUDOKTOR_RATE_LIMIT_HOLD_S,
    PvInterface,
    _akkudoktor_upstream_status,
    _describe_akkudoktor_error,
)

TIME_FRAME_BASE_HOURLY = 3600

ENTRY = {
    "name": "roof",
    "lat": 47.716,
    "lon": 9.39,
    "azimuth": 0.1,
    "tilt": 30.0,
    "power": 4000.0,
    "powerInverter": 4000.0,
    "inverterEfficiency": 0.95,
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
            """Do nothing - the fetch is invoked directly where a test needs it."""

    monkeypatch.setattr("threading.Thread", DummyThread)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries must not put real seconds into the suite."""
    monkeypatch.setattr("src.interfaces.pv_interface.time.sleep", lambda *_: None)


class _Relayed:
    """An akkudoktor response that wraps an upstream failure in a 500."""

    def __init__(self, upstream_status, status_code=500):
        self.status_code = status_code
        self.text = f"Request failed with status code {upstream_status}"

    def raise_for_status(self):
        """Mimic requests: an HTTPError that still carries the response."""
        error = requests.exceptions.HTTPError(
            f"{self.status_code} Server Error: Internal Server Error for url: "
            "https://api.akkudoktor.net/forecast?lat=47.716&lon=9.39"
        )
        error.response = self
        raise error

    def json(self):
        """Never reached - the status check fires first."""
        return {}


def _pv(**kwargs):
    """A PvInterface with the temperature forecast on, no threads started."""
    return PvInterface(
        {},
        [dict(ENTRY)],
        TIME_FRAME_BASE_HOURLY,
        {},
        temperature_forecast_enabled=True,
        timezone="Europe/Berlin",
        **kwargs,
    )


def _fetch(pv, monkeypatch, response, tgt_value="temperature"):
    """Run one akkudoktor fetch against *response*, counting the HTTP calls."""
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    monkeypatch.setattr("src.interfaces.pv_interface.requests.get", fake_get)
    result = pv._PvInterface__get_pv_forecast_akkudoktor_api(
        tgt_value=tgt_value, pv_config_entry=pv.config[0]
    )
    return result, len(calls)


def test_upstream_status_is_read_out_of_the_body():
    """The one fact that identifies the fault is parsed, not guessed."""
    assert _akkudoktor_upstream_status("Request failed with status code 429") == 429
    assert _akkudoktor_upstream_status("Request failed with status code 400") == 400
    assert _akkudoktor_upstream_status("<html>bad gateway</html>") is None
    assert _akkudoktor_upstream_status("") is None


def test_error_message_carries_the_upstream_body():
    """
    ``raise_for_status()`` renders as "500 Server Error" plus the URL for every fault
    the proxy relays, so the body has to reach the log or the report starts at zero.
    """
    error = requests.exceptions.HTTPError("500 Server Error: ... for url: https://x")
    error.response = _Relayed(429)

    message = _describe_akkudoktor_error("temperature", error)

    assert "Request failed with status code 429" in message
    assert "rate limiting akkudoktor.net" in message
    assert "the request itself is fine" in message


def test_relayed_429_arms_a_hold_and_costs_one_request(monkeypatch):
    """
    A quota window outlasts the retry policy by orders of magnitude, so the retries
    only spend requests against a quota that is already exhausted.
    """
    pv = _pv()

    result, calls = _fetch(pv, monkeypatch, _Relayed(429))

    assert calls == 1  # not TEMP_MAX_RETRIES
    assert result == []
    assert pv._akkudoktor_hold_until is not None
    assert pv.temp_forecast_request_error["error"] == "rate_limit"


def test_relayed_429_is_not_counted_as_a_failure(monkeypatch):
    """
    Waiting out a quota is not a failed fetch.  Counting it as one would walk the
    counter toward ``max_failures`` and discard the very cache the hold protects.
    """
    pv = _pv()
    pv.last_successful_temp_forecast = [12.0] * 48

    result, _ = _fetch(pv, monkeypatch, _Relayed(429))

    assert result == [12.0] * 48
    assert pv.consecutive_temp_failures == 0


def test_hold_suppresses_further_requests_for_both_targets(monkeypatch):
    """The quota belongs to the provider, so PV and temperature hold together."""
    pv = _pv()
    _fetch(pv, monkeypatch, _Relayed(429))

    _, temp_calls = _fetch(pv, monkeypatch, _Relayed(429))
    _, power_calls = _fetch(pv, monkeypatch, _Relayed(429), tgt_value="power")

    assert temp_calls == 0
    assert power_calls == 0


def test_hold_expires_and_the_request_is_retried(monkeypatch):
    """The hold is a pause, not a circuit breaker - recovery must not need a restart."""
    pv = _pv()
    pv._akkudoktor_hold_until = datetime.now() - timedelta(seconds=1)

    _, calls = _fetch(pv, monkeypatch, _Relayed(429))

    assert calls == 1


def test_relayed_400_is_a_normal_failure(monkeypatch):
    """
    An upstream 400 is permanent - a bad parameter, not a busy provider.  Holding off
    would only hide it, so it takes the ordinary retry-and-count path.
    """
    pv = _pv()

    result, calls = _fetch(pv, monkeypatch, _Relayed(400))

    assert calls > 1  # retried, unlike the 429
    assert result == []
    assert pv._akkudoktor_hold_until is None
    assert pv.consecutive_temp_failures == 1


def test_cold_start_failure_never_fabricates_a_temperature_curve(
    monkeypatch, caplog
):
    """
    Regression for the report in issue #289: a first fetch that fails with no cache
    yet used to be padded to 48 zeros, logged as "fetched successfully", and stored
    as the last *successful* forecast - 0 degC is wrong by ~20 K in summer, yet sits
    well inside the +-60 degC plausibility guard that is supposed to catch this.
    """
    pv = _pv()
    assert pv.last_successful_temp_forecast == []

    with caplog.at_level(logging.DEBUG, logger="__main__"):
        result, _ = _fetch(pv, monkeypatch, _Relayed(429))

    assert result == []
    assert pv.last_successful_temp_forecast == []
    assert pv._last_temp_fetch is None
    assert "fetched successfully" not in caplog.text


def test_hold_message_names_the_provider_not_the_configuration(monkeypatch):
    """
    The served error is what the user reads in the UI.  A quota problem at the
    provider must not send anyone hunting through their own settings.
    """
    pv = _pv()
    _fetch(pv, monkeypatch, _Relayed(429))

    message = pv.temp_forecast_request_error["message"]

    assert "rate limited by the weather provider" in message
    assert "Nothing is wrong with the configuration" in message
    assert str(AKKUDOKTOR_RATE_LIMIT_HOLD_S) in message
