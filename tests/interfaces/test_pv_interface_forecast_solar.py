# pylint: disable=protected-access
"""
Tests for the Forecast.Solar PV source: API key support and 429 back-off.

Forecast.Solar meters requests per zone - the caller's IP, or the API key once one is
configured - and hands out 12 per hour on the public tier.  These tests pin the two
behaviours that keep EOS Connect inside that budget: the key goes into the URL path (and
nowhere near the log), and a 429 parks further requests instead of retrying into a
permanent block.
"""

import logging
from datetime import datetime, timedelta

import pytest
import requests

from src.interfaces import pv_interface as pv_module
from src.interfaces.pv_interface import (
    FORECAST_SOLAR_DEFAULT_HOLD_S,
    FORECAST_SOLAR_MAX_HOLD_S,
    FORECAST_SOLAR_MIN_HOLD_S,
    PvInterface,
)

TIME_FRAME_BASE = 3600


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    """Keep the background update thread out of the tests."""

    class DummyThread:
        """Stand-in for threading.Thread that never runs anything."""

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            """No-op."""

        def is_alive(self):
            """Report dead so shutdown() never waits."""
            return False

        def join(self, timeout=None):
            """No-op."""

    monkeypatch.setattr("threading.Thread", DummyThread)


def _installation(name="roof", power=5000):
    return {
        "name": name,
        "lat": 50.0,
        "lon": 8.0,
        "azimuth": 0,
        "tilt": 30,
        "power": power,
        "horizon": [0] * 24,
    }


def _make_pv(api_key="", installations=1):
    config_source = {"source": "forecast_solar"}
    if api_key:
        config_source["api_key"] = api_key
    config = [_installation(f"roof{i}") for i in range(installations)]
    return PvInterface(config_source, config, TIME_FRAME_BASE, {}, timezone="UTC")


class _MockResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        """Return the canned payload."""
        return self._payload

    def raise_for_status(self):
        """Mimic requests' behaviour for non-2xx codes."""
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


def _success_payload():
    """A watt_hours_period block covering 48 hours from a fixed midnight."""
    start = datetime(2026, 3, 1, 0, 0, 0)
    return {
        "result": {
            "watt_hours_period": {
                (start + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"): 100 * i
                for i in range(48)
            }
        },
        "message": {"ratelimit": {"period": 3600, "limit": 12, "remaining": 11}},
    }


def _capture_urls(monkeypatch, response):
    """Patch requests.get to record every URL it is called with."""
    urls = []

    def mock_get(url, *_args, **_kwargs):
        urls.append(url)
        return response() if callable(response) else response

    monkeypatch.setattr("requests.get", mock_get)
    return urls


# --------------------------------------------------------------------------- API key


def test_url_has_no_key_segment_without_api_key(monkeypatch):
    """Without a key the public-tier URL must be unchanged."""
    pv = _make_pv()
    urls = _capture_urls(monkeypatch, _MockResponse(payload=_success_payload()))

    pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert len(urls) == 1
    assert urls[0].startswith("https://api.forecast.solar/estimate/50.0/8.0/30/0/5.0")


def test_api_key_is_the_first_path_segment(monkeypatch):
    """The key goes before 'estimate', per doc.forecast.solar/api:estimate."""
    pv = _make_pv(api_key="SECRETKEY123")
    urls = _capture_urls(monkeypatch, _MockResponse(payload=_success_payload()))

    pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert urls[0].startswith(
        "https://api.forecast.solar/SECRETKEY123/estimate/50.0/8.0/30/0/5.0"
    )


def test_api_key_is_read_from_the_source_config_not_the_installation(monkeypatch):
    """An api_key on the installation entry must be ignored - it lives on the source."""
    pv = _make_pv(api_key="FROMSOURCE")
    entry = dict(pv.config[0], api_key="FROMENTRY")
    urls = _capture_urls(monkeypatch, _MockResponse(payload=_success_payload()))

    pv._PvInterface__get_pv_forecast_forecast_solar_api(entry)

    assert "FROMSOURCE" in urls[0]
    assert "FROMENTRY" not in urls[0]


def test_api_key_is_never_logged(monkeypatch, caplog):
    """
    The key sits in the URL path, so the request carries it and the log must not.
    Both halves are asserted together: that divergence is the whole point.
    """
    pv = _make_pv(api_key="SECRETKEY123")
    urls = _capture_urls(monkeypatch, _MockResponse(payload=_success_payload()))

    with caplog.at_level(logging.DEBUG, logger="__main__"):
        pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert "SECRETKEY123" in urls[0]
    assert "SECRETKEY123" not in caplog.text
    assert "https://api.forecast.solar/***/estimate/" in caplog.text


def test_nothing_logged_at_all_carries_the_key(monkeypatch, caplog):
    """
    Belt and braces across every log level, including the error paths: no record
    emitted by a Forecast.Solar cycle may contain the credential.
    """
    pv = _make_pv(api_key="SECRETKEY123")
    _capture_urls(monkeypatch, _MockResponse(status_code=429))

    with caplog.at_level(logging.DEBUG, logger="__main__"):
        pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])
        pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert "SECRETKEY123" not in caplog.text
    assert "SECRETKEY123" not in str(pv.pv_forcast_request_error)


def test_blank_api_key_is_treated_as_absent(monkeypatch):
    """Whitespace in the field must not produce a bogus '  /estimate' path."""
    pv = _make_pv(api_key="   ")
    urls = _capture_urls(monkeypatch, _MockResponse(payload=_success_payload()))

    pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert urls[0].startswith("https://api.forecast.solar/estimate/")


# ------------------------------------------------------------------------ 429 handling


def test_429_arms_the_hold_and_makes_exactly_one_request(monkeypatch):
    """
    The bug behind "you can never get out of it": a 429 is a RequestException, so
    _retry_request used to fire three requests against an exhausted quota.
    """
    pv = _make_pv()
    urls = _capture_urls(monkeypatch, _MockResponse(status_code=429))

    result = pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert len(urls) == 1
    assert pv._forecast_solar_hold_until is not None
    assert pv.pv_forcast_request_error["error"] == "rate_limit"
    assert result == []


def test_hold_skips_the_request_entirely(monkeypatch):
    """While the hold stands, no HTTP call may be made at all."""
    pv = _make_pv()
    pv.last_successful_pv_forecast = [42] * 48
    pv._forecast_solar_hold_until = datetime.now() + timedelta(seconds=600)
    urls = _capture_urls(monkeypatch, _MockResponse(payload=_success_payload()))

    result = pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert not urls
    assert result == [42] * 48
    assert pv.pv_forcast_request_error["error"] == "rate_limit"


def test_hold_does_not_count_as_a_failure(monkeypatch):
    """
    Waiting out a quota is not a failed fetch.  Counting it would exhaust max_failures
    and throw away the cache the hold exists to protect.
    """
    pv = _make_pv()
    pv.last_successful_pv_forecast = [7] * 48
    _capture_urls(monkeypatch, _MockResponse(status_code=429))

    pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])
    before = pv.consecutive_failures
    for _ in range(5):
        pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert pv.consecutive_failures == before == 0


def test_requests_resume_once_the_hold_expires(monkeypatch):
    """An expired hold must clear itself and let the next cycle through."""
    pv = _make_pv()
    pv._forecast_solar_hold_until = datetime.now() - timedelta(seconds=1)
    urls = _capture_urls(monkeypatch, _MockResponse(payload=_success_payload()))

    result = pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert len(urls) == 1
    assert pv._forecast_solar_hold_until is None
    assert len(result) == 48


def test_reload_config_clears_the_hold():
    """
    Adding or removing the key moves us to a different quota zone, so a hold recorded
    against the old one no longer describes anything real.
    """
    pv = _make_pv()
    pv._forecast_solar_hold_until = datetime.now() + timedelta(seconds=3000)

    pv.reload_config(
        {"source": "forecast_solar", "api_key": "NEWKEY"},
        [_installation()],
        {},
        False,
        "UTC",
    )

    assert pv._forecast_solar_hold_until is None


# ------------------------------------------------------- retry-after parsing


@pytest.mark.parametrize(
    "headers,payload,expected",
    [
        ({"Retry-After": "900"}, {}, 900),
        ({"X-Ratelimit-Reset": "1200"}, {}, 1200),
        # Header wins over the body: it is the more specific answer.
        ({"Retry-After": "300"}, {"message": {"ratelimit": {"reset": 1800}}}, 300),
        ({}, {"message": {"ratelimit": {"reset": 1800}}}, 1800),
        # Nothing usable at all falls back to the public tier's own period.
        ({}, {}, FORECAST_SOLAR_DEFAULT_HOLD_S),
        ({}, {"message": {"ratelimit": {}}}, FORECAST_SOLAR_DEFAULT_HOLD_S),
        # Garbage must not raise out of the error path.
        ({"Retry-After": "soon"}, {"message": "not a dict"}, FORECAST_SOLAR_DEFAULT_HOLD_S),
        # Out-of-range values are clamped rather than trusted.
        ({"Retry-After": "5"}, {}, FORECAST_SOLAR_MIN_HOLD_S),
        ({"Retry-After": "999999"}, {}, FORECAST_SOLAR_MAX_HOLD_S),
    ],
)
def test_retry_after_parsing(headers, payload, expected):
    """Every documented signal is read, and anything unusable degrades safely."""
    pv = _make_pv()
    response = _MockResponse(status_code=429, payload=payload, headers=headers)

    hold = pv._PvInterface__forecast_solar_retry_after_seconds(response)

    assert hold == expected


def test_retry_at_timestamp_is_honoured():
    """The ISO 'retry-at' the API returns is converted to a hold in seconds."""
    pv = _make_pv()
    retry_at = (datetime.now() + timedelta(seconds=750)).isoformat()
    response = _MockResponse(
        status_code=429, payload={"message": {"ratelimit": {"retry-at": retry_at}}}
    )

    hold = pv._PvInterface__forecast_solar_retry_after_seconds(response)

    assert 700 <= hold <= 760


def test_a_body_that_cannot_be_parsed_still_yields_a_hold():
    """A 429 with an unreadable body must still stop us calling."""
    pv = _make_pv()

    class Broken(_MockResponse):
        """Response whose body cannot be decoded."""

        def json(self):
            raise ValueError("not json")

    hold = pv._PvInterface__forecast_solar_retry_after_seconds(Broken(status_code=429))

    assert hold == FORECAST_SOLAR_DEFAULT_HOLD_S


# ------------------------------------------------------------------- auth + interval


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_api_key_reports_an_actionable_error(monkeypatch, status):
    """A bad key must not surface as a bare HTTPError."""
    pv = _make_pv(api_key="WRONG")
    monkeypatch.setattr(pv_module.time, "sleep", lambda _s: None)
    _capture_urls(monkeypatch, _MockResponse(status_code=status))

    pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    message = str(pv.pv_forcast_request_error["message"])
    assert pv.pv_forcast_request_error["error"] == "request_failed"
    assert "rejected the API key" in message
    assert "WRONG" not in message


@pytest.mark.parametrize(
    "installations,expected_seconds", [(1, 900), (2, 1800), (3, 2700), (4, 3600)]
)
def test_update_interval_scales_with_the_installation_count(
    installations, expected_seconds
):
    """
    One request per plane per cycle against a 12/hour budget: scaling the interval
    keeps total traffic at four requests an hour however many planes are configured.
    """
    pv = _make_pv(installations=installations)

    assert pv.update_interval == expected_seconds


def test_other_sources_keep_the_flat_interval():
    """The scaling must not leak into providers that batch their planes."""
    config = [_installation(f"roof{i}") for i in range(3)]
    pv = PvInterface(
        {"source": "akkudoktor"}, config, TIME_FRAME_BASE, {}, timezone="UTC"
    )

    assert pv.update_interval == 15 * 60
