"""
Error messages served over HTTP must not carry exception text.

Every message asserted here reaches an unauthenticated endpoint:

- the optimizer backends' ``{"error": ...}`` dict becomes ``optimized_response``, which
  ``OptimizationScheduler`` stores and ``GET /json/optimize_response.json`` returns
  verbatim;
- the autoscaler's recorded failure is returned as ``last_error`` by
  ``GET /api/pv_autoscaling/status``;
- the entity probe's ``{"error": ...}`` is the body of ``POST
  /api/config/test-entity`` and is rendered straight into the config panel.

A ``requests`` exception names the resolved host, port and any proxy in the chain; the
CBC message names absolute binary paths and OSError text; the local optimizer's broad
handler can carry arbitrary internals. All of that belongs in the log only — CodeQL
reports it as *information exposure through an exception*, and it is unreadable to the
person who has to act on it anyway.

Grouped in one file rather than scattered across the backend test modules: the concern
is shared, and `test_optimization_backend_eos.py` is already at pylint's module-size
limit.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytz
import requests

from src.interfaces.optimization_backends.optimization_backend_eos import EOSBackend
from src.interfaces.optimization_backends.optimization_backend_evopt import EVOptBackend
from src.interfaces.optimization_backends.optimization_backend_local_evopt import (
    LocalEVOptBackend,
)
from src.interfaces.optimization_backends.local_evopt.optimizer import (
    CbcSolverUnavailableError,
)
from src.config_web.entity_probe import probe_entity
from src.interfaces.pv_autoscaler import PvAutoscaler

# Distinctive stand-ins for what a real exception would carry.
LEAKY_HOST = "internal-secret.lan"
NETWORK_LEAK = (
    f"HTTPSConnectionPool(host='{LEAKY_HOST}', port=8123): Max retries exceeded with "
    "url: /api/states/sensor.pv_total_yield (Caused by ProxyError('no proxy'))"
)


@pytest.fixture(name="berlin_tz")
def fixture_berlin_tz():
    """Timezone the backends are constructed with."""
    return pytz.timezone("Europe/Berlin")


# ----------------------------------------------------------------------
# Remote optimizer backends
# ----------------------------------------------------------------------


@patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.post")
def test_eos_backend_does_not_echo_request_exception(mock_post, berlin_tz):
    """The EOS backend reports the failure without the exception's text."""
    mock_post.side_effect = requests.exceptions.RequestException(NETWORK_LEAK)
    backend = EOSBackend("http://localhost:8503", 3600, berlin_tz)

    result, _ = backend.optimize({"some": "request"})

    assert LEAKY_HOST not in result["error"]
    assert "HTTPSConnectionPool" not in result["error"]
    assert "ProxyError" not in result["error"]
    assert "EOS server" in result["error"]
    assert "log" in result["error"]


@patch("src.interfaces.optimization_backends.optimization_backend_evopt.requests.post")
def test_evopt_backend_does_not_echo_request_exception(mock_post, berlin_tz):
    """Same for the EVopt backend."""
    mock_post.side_effect = requests.exceptions.RequestException(NETWORK_LEAK)
    backend = EVOptBackend("http://localhost:8502", 3600, berlin_tz)

    result, _ = backend.optimize({"some": "request"})

    assert LEAKY_HOST not in result["error"]
    assert "HTTPSConnectionPool" not in result["error"]
    assert "EVopt server" in result["error"]
    assert "log" in result["error"]


# ----------------------------------------------------------------------
# Built-in optimizer
# ----------------------------------------------------------------------


def test_local_optimizer_does_not_echo_an_unexpected_exception(berlin_tz):
    """The broad handler must not hand arbitrary internals to the caller."""
    backend = LocalEVOptBackend(3600, berlin_tz)

    with patch.object(
        backend,
        "_build_optimizer",
        side_effect=RuntimeError("KeyError at /app/secret/path.py line 42"),
    ):
        result, _ = backend.optimize({"some": "request"}, timeout=60)

    assert "/app/secret/path.py" not in result["error"]
    assert "KeyError" not in result["error"]
    assert result["error"] == "Local optimizer failed - see the log for details"


def test_local_optimizer_does_not_echo_the_cbc_probe_detail(berlin_tz):
    """
    The CBC rejection detail names binary paths; the response gets the actionable half.

    Keeping it out of the response is the point — the log still carries every candidate
    that was probed and why each was rejected.
    """
    backend = LocalEVOptBackend(3600, berlin_tz)

    with patch.object(
        backend,
        "_build_optimizer",
        side_effect=CbcSolverUnavailableError(
            "No runnable CBC solver found.\n"
            "  - bundled CBC -> /usr/lib/python3/pulp/solverdir/cbc: could not be "
            "started (OSError 8)"
        ),
    ):
        result, _ = backend.optimize({"some": "request"}, timeout=60)

    assert "/usr/lib/python3" not in result["error"]
    assert "OSError" not in result["error"]
    assert "CBC solver" in result["error"]
    assert "Settings" in result["error"]


# ----------------------------------------------------------------------
# PV autoscaler status
# ----------------------------------------------------------------------


def test_autoscaler_status_does_not_echo_the_fetch_exception():
    """`last_error` names the sensor and stops there."""

    class _Store:  # pylint: disable=too-few-public-methods
        """Enough of PvYieldStore for a collection attempt."""

        @staticmethod
        def get_latest_record():
            """No history yet."""
            return None

        @staticmethod
        def get_history_last_n_days(_days):
            """No history yet."""
            return []

    autoscaler = PvAutoscaler(
        {"enabled": True, "retention_days": 7, "min_data_hours_required": 1},
        _Store(),
        auto_start=False,
    )
    autoscaler.sensor_entity_id = "sensor.pv_total_yield"
    # collect_if_needed() bails out before the fetch unless a full day of forecast slots
    # is available, so stand in a minimal PV interface.
    autoscaler._pv_interface = SimpleNamespace(  # pylint: disable=protected-access
        time_frame_base=3600,
        get_current_pv_forecast=lambda scale=True: [0.0] * 48,
    )

    with patch.object(
        PvAutoscaler,
        "_PvAutoscaler__fetch_remote_state",
        side_effect=requests.exceptions.RequestException(NETWORK_LEAK),
    ):
        autoscaler.collect_if_needed()

    last_error = autoscaler.get_status()["last_error"]
    assert last_error, "the failure should have been recorded"
    assert LEAKY_HOST not in last_error
    assert "HTTPSConnectionPool" not in last_error
    assert "ProxyError" not in last_error
    assert "sensor.pv_total_yield" in last_error


# ----------------------------------------------------------------------
# Entity probe (POST /api/config/test-entity)
# ----------------------------------------------------------------------


PROBE_ARGS = dict(
    source="homeassistant", url="http://ha.local:8123", access_token="tok"
)


def _probe_error(exc):
    """The message the probe would serve for *exc*."""
    with patch(
        "src.config_web.entity_probe.fetch_remote_state", side_effect=exc
    ):
        result = probe_entity(sensor="sensor.house_power", **PROBE_ARGS)
    assert result["ok"] is False
    return result["error"]


def test_entity_probe_does_not_echo_a_transport_exception():
    """
    The probe exists to tell the user their entity name is wrong. A resolved host,
    port and proxy chain neither helps them nor belongs in a served response.
    """
    error = _probe_error(requests.exceptions.ConnectionError(NETWORK_LEAK))

    assert LEAKY_HOST not in error
    assert "HTTPSConnectionPool" not in error
    assert "ProxyError" not in error
    assert "Could not reach the data source" in error


def test_entity_probe_does_not_echo_an_unhandled_http_error():
    """
    404/401/403 get their own curated advice; anything else must still not fall
    through to the exception's text.
    """
    response = SimpleNamespace(status_code=500)
    error = _probe_error(
        requests.exceptions.HTTPError(NETWORK_LEAK, response=response)
    )

    assert LEAKY_HOST not in error
    assert "HTTPSConnectionPool" not in error
    assert "HTTP 500" in error


def test_entity_probe_does_not_echo_a_value_error():
    error = _probe_error(ValueError(NETWORK_LEAK))

    assert LEAKY_HOST not in error
    assert "HTTPSConnectionPool" not in error


def test_the_curated_404_still_names_the_entity():
    """
    Suppressing exception text must not cost the user the one detail they need: the
    name they typed, and what a valid one looks like.
    """
    response = SimpleNamespace(status_code=404)
    error = _probe_error(
        requests.exceptions.HTTPError(NETWORK_LEAK, response=response)
    )

    assert "sensor.house_power" in error
    assert "not found" in error
    assert LEAKY_HOST not in error


# ---------------------------------------------------------------------------
# Credentials in the log
#
# The messages above are about exception text reaching an endpoint. This one is
# worse in kind: a working credential. ``GET /logs`` serves the in-memory buffer
# with no authentication, and the whole point of that endpoint is that people
# copy from it into bug reports — which is exactly how a live Home Assistant
# long-lived token reached a GitHub issue.
# ---------------------------------------------------------------------------

#: Shaped like the real thing — three dot-separated base64 segments — so a test
#: that looked only for the word "token" could not pass by accident.
FAKE_HA_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJ0ZXN0IiwiaWF0IjoxNzMyMzE1OTc4fQ."
    "s3cr3t-signature-that-must-never-be-logged"
)


def _load_interface_log(caplog, token):
    """Every record LoadInterface emits while being constructed with *token*."""
    from src.interfaces.load_interface import (  # noqa: PLC0415
        LoadInterface,
    )

    config = {
        "source": "homeassistant",
        "url": "http://ha.local:8123",
        "access_token": token,
        "load_sensor": "sensor.house_power",
        "car_charge_load_sensor": "",
        "additional_load_1_sensor": "",
    }
    with caplog.at_level(logging.DEBUG, logger="__main__"):
        LoadInterface(config, 3600, pytz.timezone("Europe/Berlin"))
    return "\n".join(record.getMessage() for record in caplog.records)


def test_the_access_token_is_never_written_to_the_log(caplog):
    """
    The line this guards used to log the token in full at DEBUG. It reached the
    unauthenticated /logs endpoint and from there into pasted bug reports.
    """
    log = _load_interface_log(caplog, FAKE_HA_TOKEN)

    assert FAKE_HA_TOKEN not in log
    # Not even a recognisable piece of it: a JWT's payload segment alone is
    # enough to identify the install, and the signature is the secret part.
    assert "s3cr3t-signature-that-must-never-be-logged" not in log
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in log


def test_the_log_still_says_whether_a_token_arrived(caplog):
    """
    Redacting must not cost the diagnosis it existed for. A token truncated on
    paste is a real failure mode, so the length has to survive.
    """
    log = _load_interface_log(caplog, FAKE_HA_TOKEN)

    assert "Access token" in log
    assert f"{len(FAKE_HA_TOKEN)} characters" in log


def test_a_missing_token_is_reported_as_missing(caplog):
    log = _load_interface_log(caplog, "")

    assert "not set" in log
    assert "0 characters" not in log


# ---------------------------------------------------------------------------
# Credentials in a URL an exception quotes back
#
# Forecast.Solar carries the API key as the first path segment. pv_interface
# builds a masked `loggable_url` for exactly that reason — and then the generic
# error branch stringified the requests exception, which quotes the URL it
# tried and puts the key straight back. logger.error also fills the alert
# buffer, and the bug report copies the last 50 alerts into a public issue.
# ---------------------------------------------------------------------------

FORECAST_SOLAR_KEY = "fs-Abc123SecretKeyXyz"


def _forecast_solar_log(caplog, exception, api_key=FORECAST_SOLAR_KEY):
    """
    Drive the real request path with *exception* raised by requests.get, and
    return everything it logged.

    Deliberately not a reimplementation of the masking: the bug was that the
    careful ``loggable_url`` a few lines up was undone by the generic error
    branch, and only the real path can show whether that still holds.
    """
    from src.interfaces.pv_interface import PvInterface  # noqa: PLC0415

    with patch.object(
        PvInterface, "_PvInterface__update_pv_state_loop", lambda self: None
    ):
        pv = PvInterface(
            {"source": "forecast_solar", "api_key": api_key},
            [],
            3600,
            {},
            timezone="UTC",
        )

    entry = {
        "name": "roof", "lat": 47.5, "lon": 8.5,
        "azimuth": 90, "tilt": 30, "power": 4600,
    }

    # The real path retries three times with a one-second pause. Nothing here is
    # about the retry schedule, and paying for it would add eight seconds to a
    # fifteen-second lane.
    with caplog.at_level(logging.DEBUG, logger="__main__"):
        with patch("src.interfaces.http_fetch.time.sleep"):
            with patch(
                "src.interfaces.pv_interface.requests.get", side_effect=exception
            ):
                pv._PvInterface__get_pv_forecast_forecast_solar_api(entry)

    return "\n".join(record.getMessage() for record in caplog.records)


def test_a_connection_error_does_not_carry_the_forecast_solar_key(caplog):
    """
    The key is the first path segment, and a ConnectionError quotes the path:
    "Max retries exceeded with url: /KEY/estimate/...".
    """
    log = _forecast_solar_log(
        caplog,
        requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.forecast.solar', port=443): "
            f"Max retries exceeded with url: /{FORECAST_SOLAR_KEY}/estimate/47.5/8.5"
        ),
    )

    assert FORECAST_SOLAR_KEY not in log
    # The diagnosis has to survive the masking.
    assert "api.forecast.solar" in log


def test_an_http_error_does_not_carry_the_forecast_solar_key(caplog):
    """``raise_for_status()`` stringifies to "... for url: <the whole URL>"."""
    url = f"https://api.forecast.solar/{FORECAST_SOLAR_KEY}/estimate/47.5/8.5/30/90/4.6"
    log = _forecast_solar_log(
        caplog,
        requests.exceptions.HTTPError(
            f"500 Server Error: Internal Server Error for url: {url}"
        ),
    )

    assert FORECAST_SOLAR_KEY not in log
    assert "500" in log


def test_the_request_url_itself_is_never_logged(caplog):
    """The debug line that announces the fetch uses the masked URL."""
    log = _forecast_solar_log(
        caplog, requests.exceptions.ConnectionError("connection refused")
    )

    assert FORECAST_SOLAR_KEY not in log
    assert "api.forecast.solar/***/" in log


def test_a_keyless_install_still_reports_its_error(caplog):
    """Forecast.Solar also answers keyless; nothing to mask, nothing to break."""
    log = _forecast_solar_log(
        caplog,
        requests.exceptions.ConnectionError("connection refused"),
        api_key="",
    )

    assert "connection refused" in log
    assert "***" not in log
