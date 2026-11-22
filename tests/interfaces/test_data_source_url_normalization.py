"""
A trailing slash on the data source URL must not reach the endpoints.

Issue #340: a base URL of ``http://host/`` was joined into
``http://host//api/history/period/...``. Home Assistant does not route a doubled
slash, so every history fetch came back 404 — while the config UI's Test button,
which reads through ``state_source`` and trims, reported the very same entity as
working. Two code paths, one configuration, opposite verdicts.

The fix normalizes where a connection is built rather than at each endpoint, so
these tests pin both: the normalization itself, and the absence of ``//`` in the
URL the interface actually requests.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.interfaces.evcc_entities import Connection, connections_from_section
from src.interfaces.load_interface import LoadInterface


@pytest.mark.parametrize(
    "configured",
    [
        "http://192.168.10.70/",
        "http://192.168.10.70//",
        "  http://192.168.10.70/  ",
        "http://192.168.10.70",
    ],
)
def test_connection_trims_the_base_url(configured):
    """However the URL is typed, the connection holds it without a tail."""
    assert Connection("homeassistant", configured, "token", False).url == (
        "http://192.168.10.70"
    )


def test_connection_survives_an_absent_url():
    """An unconfigured source is empty, not None — callers test it as a string."""
    assert Connection("homeassistant", None, "", False).url == ""


def test_connections_from_section_trims_both_connections():
    """The supplementary URL is a base URL too, and was joined the same way."""
    primary, supplementary = connections_from_section(
        {
            "source": "evcc",
            "url": "http://evcc.local/",
            "supplementary_type": "homeassistant",
            "supplementary_url": "http://192.168.10.70/",
            "supplementary_access_token": "token",
        }
    )
    assert primary.url == "http://evcc.local"
    assert supplementary.url == "http://192.168.10.70"


def test_load_interface_trims_its_own_base_url():
    """The EVCC endpoints build their paths from this field, not from a connection."""
    interface = LoadInterface(
        {"source": "evcc", "url": "http://evcc.local/", "load_sensor": "group:home"},
        3600,
    )
    assert interface.url == "http://evcc.local"


def test_history_request_has_no_doubled_slash():
    """The reported symptom: the URL that goes out against a trailing-slash base.

    Asserted on the request rather than on ``interface.url`` because that is
    where the 404 came from — the join, not the field.
    """
    interface = LoadInterface(
        {
            "source": "homeassistant",
            "url": "http://192.168.10.70/",
            "load_sensor": "sensor.goodwe_house_consumption",
            "access_token": "token",
            "max_retries": 1,
            "retry_backoff": 0,
        },
        3600,
    )

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    response = MagicMock()
    response.status_code = 200
    # Two samples, so the recorder-statistics fallback is never reached: it
    # would leave this test waiting out a 30 second timeout against a host that
    # does not exist.
    response.json.return_value = [
        [
            {"state": "500", "last_updated": start.isoformat(), "attributes": {}},
            {"state": "600", "last_updated": end.isoformat(), "attributes": {}},
        ]
    ]

    with patch("requests.get", return_value=response) as get:
        interface.fetch_historical_energy_data(
            "sensor.goodwe_house_consumption", start, end
        )

    assert get.call_args_list, "no request was made"
    requested = get.call_args_list[0].args[0]
    assert requested.startswith("http://192.168.10.70/api/history/period/")
    assert "//api/" not in requested
