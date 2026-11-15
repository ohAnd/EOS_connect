"""
A refused request is reported once, and reported truthfully.

Issue #340: one misconfigured base URL produced about 2000 ``[LOAD-IF] Request
failed after 5 attempts ... 404`` lines in roughly three hours. Two separate
faults sat in that one line. The count was never true — a 4xx short-circuits
after a single attempt — and the same standing refusal was written out on every
pass, which buries every other finding in the log.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.interfaces.load_interface import LoadInterface

HA_CONFIG = {
    "source": "homeassistant",
    "url": "http://192.168.10.70",
    "load_sensor": "sensor.house",
    "access_token": "token",
    "max_retries": 5,
    "retry_backoff": 0,
}


def _refusing(status):
    """A requests stand-in that answers every call with *status*."""
    response = MagicMock()
    response.status_code = status
    response.raise_for_status.side_effect = requests.exceptions.HTTPError(
        f"{status} Client Error for url: ...", response=response
    )
    return MagicMock(return_value=response)


@contextmanager
def _refusing_source(status):
    """Both verbs refused, as a broken base URL refuses them.

    ``requests.request`` carries the recorder-statistics fallback. Letting it
    succeed while the history is refused would send the fetch on to the
    attributes endpoint and add a third refusal that has nothing to do with
    what these tests are about.
    """
    get = _refusing(status)
    with patch("requests.get", get), patch("requests.request", _refusing(status)):
        yield get


def _fetch(interface, entity="sensor.house"):
    end = datetime.now(timezone.utc)
    interface.fetch_historical_energy_data(entity, end - timedelta(hours=1), end)


def _refusals(log):
    """The ERROR calls the refusal reporter made, as (url, message) pairs."""
    return [
        (call.args[3], call.args[0])
        for call in log.error.call_args_list
        if "refused with HTTP" in call.args[0]
    ]


def test_a_4xx_is_not_retried():
    """The entity does not exist, or the path is not routed. Asking again cannot help."""
    interface = LoadInterface(dict(HA_CONFIG), 3600)
    with _refusing_source(404) as get:
        _fetch(interface)

    history_calls = [c for c in get.call_args_list if "/api/history/" in c.args[0]]
    assert len(history_calls) == 1


def test_the_message_does_not_claim_five_attempts(caplog):
    """It made one. Saying five sent the bug report looking for a flaky network."""
    interface = LoadInterface(dict(HA_CONFIG), 3600)
    with caplog.at_level(logging.DEBUG), _refusing_source(404):
        _fetch(interface)

    assert "after 5 attempts" not in caplog.text
    assert "refused with HTTP 404" in caplog.text


def test_a_404_names_the_base_url_as_the_suspect(caplog):
    """The symptom points nowhere on its own, so the message has to point somewhere."""
    interface = LoadInterface(dict(HA_CONFIG), 3600)
    with caplog.at_level(logging.DEBUG), _refusing_source(404):
        _fetch(interface)

    assert "Settings > Data Source" in caplog.text


def test_a_401_does_not_blame_the_base_url(caplog):
    """A rejected token is a different finding, and the hint would misdirect."""
    interface = LoadInterface(dict(HA_CONFIG), 3600)
    with caplog.at_level(logging.DEBUG), _refusing_source(401):
        _fetch(interface)

    assert "refused with HTTP 401" in caplog.text
    assert "Settings > Data Source" not in caplog.text


def test_the_same_refusal_is_reported_once():
    """Ten passes, one ERROR. The rest are the same finding, kept at DEBUG."""
    interface = LoadInterface(dict(HA_CONFIG), 3600)
    with _refusing_source(404), patch("src.interfaces.load_interface.logger") as log:
        for _ in range(10):
            _fetch(interface)

    assert len(_refusals(log)) == 1
    assert any(
        "still refused with HTTP" in call.args[0] for call in log.debug.call_args_list
    )


def test_a_different_entity_is_still_reported():
    """Deduplication must not swallow the second broken sensor."""
    interface = LoadInterface(dict(HA_CONFIG), 3600)
    with _refusing_source(404), patch("src.interfaces.load_interface.logger") as log:
        _fetch(interface, "sensor.goodwe_pv_power")
        _fetch(interface, "sensor.evcc_grid_power")

    assert len(_refusals(log)) == 2


def test_the_statistics_probe_reports_its_own_refusal():
    """It says "recorder.get_statistics is unavailable", which is the better words.

    The generic reporter staying quiet there is the point: the caller asked for
    the status because it handles it, and two lines for one refusal is the
    noise this change exists to remove.
    """
    interface = LoadInterface(dict(HA_CONFIG), 3600)
    with _refusing_source(404), patch("src.interfaces.load_interface.logger") as log:
        _fetch(interface)

    urls = [url for url, _ in _refusals(log)]
    assert not any("get_statistics" in url for url in urls)
    assert any(
        "recorder.get_statistics is unavailable" in call.args[0]
        for call in log.info.call_args_list
    )


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "http://host/api/history/period/2026-10-17T20:59:09+00:00",
            "host/api/history/period",
        ),
        ("http://host/api/states/sensor.x", "host/api/states/sensor.x"),
        (
            "http://host/api/services/recorder/get_statistics",
            "host/api/services/recorder",
        ),
    ],
)
def test_the_dedup_key_drops_the_variable_tail(url, expected):
    """A timestamp in the path would make every refusal look like a new one."""
    # pylint: disable=protected-access
    key = LoadInterface._LoadInterface__endpoint_key
    assert key(url) == expected
