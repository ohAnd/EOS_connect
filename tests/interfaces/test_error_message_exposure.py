"""
Error messages served over HTTP must not carry exception text.

Every message asserted here reaches an unauthenticated endpoint:

- the optimizer backends' ``{"error": ...}`` dict becomes ``optimized_response``, which
  ``OptimizationScheduler`` stores and ``GET /json/optimize_response.json`` returns
  verbatim;
- the autoscaler's recorded failure is returned as ``last_error`` by
  ``GET /api/pv_autoscaling/status``.

A ``requests`` exception names the resolved host, port and any proxy in the chain; the
CBC message names absolute binary paths and OSError text; the local optimizer's broad
handler can carry arbitrary internals. All of that belongs in the log only — CodeQL
reports it as *information exposure through an exception*, and it is unreadable to the
person who has to act on it anyway.

Grouped in one file rather than scattered across the backend test modules: the concern
is shared, and `test_optimization_backend_eos.py` is already at pylint's module-size
limit.
"""

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
