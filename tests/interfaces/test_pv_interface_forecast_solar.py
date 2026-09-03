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


def test_coordinates_are_never_logged(monkeypatch, caplog):
    """
    The coordinates are the user's home address to within metres, and the bug reporter
    offers to paste recent log lines into a public GitHub issue. The request must carry
    them; the log must not.
    """
    pv = _make_pv()
    urls = _capture_urls(monkeypatch, _MockResponse(payload=_success_payload()))

    with caplog.at_level(logging.DEBUG, logger="__main__"):
        pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert "50.0/8.0" in urls[0]
    assert "50.0" not in caplog.text
    assert "8.0" not in caplog.text
    assert "estimate/<lat>/<lon>/" in caplog.text
    # The parameters that actually help diagnose a bad request still survive.
    assert "/30/0/5.0?horizon=" in caplog.text
    assert "roof0" in caplog.text


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
    assert "50.0" not in caplog.text
    assert "8.0" not in caplog.text


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


# --------------------------------------------------------------- period bucketing


def _make_pv_at(time_frame_base, api_key=""):
    """A PvInterface pinned to a given slot resolution."""
    config_source = {"source": "forecast_solar"}
    if api_key:
        config_source["api_key"] = api_key
    return PvInterface(
        config_source, [_installation()], time_frame_base, {}, timezone="UTC"
    )


# Shape taken from a live public-tier response (estimate/50.0/8.0/30/0/5): a sunrise
# entry of 0, hourly grid points, and a partial sunset sliver.  Every value is the
# energy of the period *ending* at its timestamp.
_HOURLY_DAY = {
    "2026-09-03 06:46:08": 0,
    "2026-09-03 07:00:00": 15,
    "2026-09-03 08:00:00": 240,
    "2026-09-03 09:00:00": 474,
    "2026-09-03 10:00:00": 703,
    "2026-09-03 11:00:00": 889,
    "2026-09-03 12:00:00": 1022,
    "2026-09-03 13:00:00": 1101,
    "2026-09-03 14:00:00": 1115,
    "2026-09-03 15:00:00": 1062,
    "2026-09-03 16:00:00": 944,
    "2026-09-03 17:00:00": 770,
    "2026-09-03 18:00:00": 556,
    "2026-09-03 19:00:00": 324,
    "2026-09-03 20:00:00": 137,
    "2026-09-03 20:08:35": 5,
}

# The reporter's own 30-minute data from issue #295, one plane, verbatim.
_HALF_HOURLY_DAY = {
    "2026-09-02 06:32:06": 0,
    "2026-09-02 07:00:00": 29,
    "2026-09-02 07:30:00": 85,
    "2026-09-02 08:00:00": 128,
    "2026-09-02 08:30:00": 164,
    "2026-09-02 09:00:00": 195,
    "2026-09-02 09:30:00": 223,
    "2026-09-02 10:00:00": 249,
    "2026-09-02 10:30:00": 274,
    "2026-09-02 11:00:00": 299,
    "2026-09-02 11:30:00": 318,
    "2026-09-02 12:00:00": 332,
    "2026-09-02 12:30:00": 345,
    "2026-09-02 13:00:00": 357,
    "2026-09-02 13:30:00": 372,
    "2026-09-02 14:00:00": 409,
    "2026-09-02 14:30:00": 468,
    "2026-09-02 15:00:00": 494,
    "2026-09-02 15:30:00": 433,
    "2026-09-02 16:00:00": 341,
    "2026-09-02 16:30:00": 289,
    "2026-09-02 17:00:00": 258,
    "2026-09-02 17:30:00": 226,
    "2026-09-02 18:00:00": 188,
    "2026-09-02 18:30:00": 146,
    "2026-09-02 19:00:00": 102,
    "2026-09-02 19:30:00": 58,
    "2026-09-02 19:53:05": 14,
}


def _quarter_hourly_day():
    """A 15-minute grid, the resolution a Professional Plus account returns."""
    payload = {"2026-09-03 06:40:00": 0}
    start = datetime(2026, 9, 3, 7, 0, 0)
    for i in range(1, 41):  # 07:00 .. 17:00
        payload[(start + timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S")] = (
            10 * i
        )
    payload["2026-09-03 07:00:00"] = 7  # 06:40-07:00 sliver
    return payload


def test_hourly_value_lands_in_the_hour_it_was_produced_in():
    """
    Forecast.Solar labels a period with its *end*, so the value keyed 08:00 is the
    energy of 07:00-08:00 and belongs in slot 7.  Reading it as slot 8 put the whole
    curve an hour late.
    """
    slots = _make_pv_at(3600)._forecast_solar_periods_to_slots(_HOURLY_DAY)

    assert slots[7] == 240  # keyed 08:00
    assert slots[8] == 474  # keyed 09:00
    assert slots[6] == 15  # the 06:46-07:00 sunrise sliver
    assert slots[19] == 137  # keyed 20:00
    assert slots[20] == 5  # the 20:00-20:08 sunset sliver


def test_half_hourly_periods_are_summed_not_sampled():
    """
    Issue #295: with an API key the source resolution is 30 min, and an exact hourly
    key lookup kept only the HH:00 half.  Hour 7 is 85 + 128, not 128.
    """
    slots = _make_pv_at(3600)._forecast_solar_periods_to_slots(_HALF_HOURLY_DAY)

    assert slots[7] == 85 + 128 == 213
    assert slots[8] == 164 + 195 == 359
    assert slots[14] == 468 + 494 == 962
    assert slots[19] == 58 + 14 == 72  # last half hour plus the sunset sliver
    assert slots[6] == 29  # 06:32-07:00, on no hour boundary


def test_quarter_hourly_periods_are_summed():
    """A 15-minute account must contribute all four sub-periods of each hour."""
    payload = _quarter_hourly_day()
    slots = _make_pv_at(3600)._forecast_solar_periods_to_slots(payload)

    assert slots[7] == sum(
        payload[f"2026-09-03 0{h}:{m}:00"]
        for h, m in ((7, "15"), (7, "30"), (7, "45"), (8, "00"))
    )
    assert slots[6] == 7  # 06:40-07:00 sliver


@pytest.mark.parametrize(
    "payload", [_HOURLY_DAY, _HALF_HOURLY_DAY], ids=["hourly", "half-hourly"]
)
def test_no_energy_is_lost(payload):
    """
    Every Wh the API reports has to end up in some slot - including the sunrise and
    sunset slivers, which land on no grid boundary and used to be dropped.
    """
    slots = _make_pv_at(3600)._forecast_solar_periods_to_slots(payload)

    assert sum(slots) == pytest.approx(sum(payload.values()), abs=0.5)


def test_the_overnight_gap_stays_empty():
    """
    The pair (yesterday's sunset, today's sunrise) spans the whole night.  Spreading
    it would invent production in the dark; a sunrise entry is always 0, so nothing
    may be attributed to the night slots.
    """
    payload = dict(_HOURLY_DAY)
    payload["2026-09-04 06:47:38"] = 0
    payload["2026-09-04 07:00:00"] = 13
    payload["2026-09-04 08:00:00"] = 244

    slots = _make_pv_at(3600)._forecast_solar_periods_to_slots(payload)

    assert slots[21:30] == [0.0] * 9  # 21:00 through 05:00 the next morning
    assert slots[30] == 13  # day two's 06:47-07:00 sliver
    assert slots[31] == 244  # day two's 07:00-08:00


def test_a_non_zero_period_after_a_long_gap_is_clamped_to_one_hour():
    """
    Defensive: if the API ever reports energy after a multi-hour gap, credit it to the
    hour before the timestamp rather than smearing it over hours it cannot belong to.
    """
    payload = {
        "2026-09-03 06:00:00": 0,
        "2026-09-03 12:00:00": 600,
    }

    slots = _make_pv_at(3600)._forecast_solar_periods_to_slots(payload)

    assert slots[11] == 600
    assert sum(slots) == 600


def test_15min_base_keeps_the_intra_hour_shape():
    """
    At a 900 s base the two halves of an hour must stay distinguishable.  The old
    path parsed hourly and divided by four, which flattened them.
    """
    slots = _make_pv_at(900)._forecast_solar_periods_to_slots(_HALF_HOURLY_DAY, 900, 192)

    assert len(slots) == 192
    # Hour 7 is slots 28-31: 85 Wh over 07:00-07:30, then 128 Wh over 07:30-08:00.
    assert slots[28:32] == [42.5, 42.5, 64.0, 64.0]
    assert sum(slots[28:32]) == 213
    assert sum(slots) == pytest.approx(sum(_HALF_HOURLY_DAY.values()), abs=0.5)


def test_a_period_straddling_a_slot_boundary_is_split_proportionally():
    """
    The sunrise sliver 06:32:06-07:00 crosses a 15-minute boundary.  Splitting it by
    overlap keeps the total exact instead of dropping or double-counting it.
    """
    slots = _make_pv_at(900)._forecast_solar_periods_to_slots(_HALF_HOURLY_DAY, 900, 192)

    # 06:32:06-06:45 is 774 s of a 1674 s period; 06:45-07:00 is the other 900 s.
    assert slots[26] == pytest.approx(29 * 774 / 1674, abs=0.05)
    assert slots[27] == pytest.approx(29 * 900 / 1674, abs=0.05)
    assert slots[26] + slots[27] == pytest.approx(29, abs=0.1)


@pytest.mark.parametrize(
    "time_frame_base,expected", [(3600, 48), (900, 192)], ids=["hourly", "15min"]
)
def test_the_slot_count_is_fixed_whatever_the_payload_covers(
    monkeypatch, time_frame_base, expected
):
    """
    A single-day payload still yields a full array, so a short response can never
    hand a downstream consumer fewer slots than it expects.
    """
    pv = _make_pv_at(time_frame_base)
    _capture_urls(
        monkeypatch, _MockResponse(payload={"result": {"watt_hours_period": _HOURLY_DAY}})
    )

    result = pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert len(result) == expected
    assert pv.pv_forcast_request_error["error"] is None


def test_the_interface_returns_bucketed_values_end_to_end(monkeypatch):
    """The wiring, not just the helper: a 30-min payload must arrive summed."""
    pv = _make_pv_at(3600, api_key="KEY")
    _capture_urls(
        monkeypatch,
        _MockResponse(payload={"result": {"watt_hours_period": _HALF_HOURLY_DAY}}),
    )

    result = pv._PvInterface__get_pv_forecast_forecast_solar_api(pv.config[0])

    assert result[7] == 213
    assert sum(result) == pytest.approx(sum(_HALF_HOURLY_DAY.values()), abs=0.5)


@pytest.mark.parametrize(
    "payload,expected",
    [
        (_HOURLY_DAY, 3600),
        (_HALF_HOURLY_DAY, 1800),
    ],
    ids=["hourly", "half-hourly"],
)
def test_source_resolution_is_detected_for_the_log(payload, expected):
    """
    The debug line has to name the resolution so a wrong curve is diagnosable from a
    log excerpt - the slivers and the overnight gap must not skew it.
    """
    pv = _make_pv_at(3600)

    assert pv._forecast_solar_source_resolution_s(payload) == expected
