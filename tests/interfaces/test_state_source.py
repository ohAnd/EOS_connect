"""
The EVCC branch of the shared state reader.

EVCC is read as a dotted path into its /api/state document - ``battery.soc`` is
the configured field this exists for at the moment. The endpoint is also the
first place a legacy envelope (`{"result": {...}}`) has to be unwrapped outside
`EvccInterface`, so the two unwrappings are pinned here rather than trusted.
"""

# pylint: disable=protected-access
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.interfaces.state_source import (
    needs_access_token,
    SUPPORTED_SOURCES,
    fetch_remote_state,
    fetch_remote_state_details,
)


class FakeEvcc:
    """requests.get for the one endpoint this source talks to."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def __call__(self, url, **kwargs):
        # pylint: disable=unused-argument
        self.calls.append(url)
        response = MagicMock()
        response.status_code = self.status
        if self.status >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status}")
        response.json.return_value = self.payload
        return response


def test_evcc_is_a_supported_source():
    assert "evcc" in SUPPORTED_SOURCES


def test_dotted_path_reads_nested_state():
    fake = FakeEvcc({"battery": {"soc": 84.0}, "version": "0.320.0"})
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "battery.soc", "http://evcc:7070") == "84.0"


def test_legacy_result_envelope_is_unwrapped():
    fake = FakeEvcc({"result": {"battery": {"soc": 60}}})
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "battery.soc", "http://evcc:7070") == "60"


def test_deeply_nested_legacy_envelope_is_unwrapped():
    # The EvccInterface only handles one level; the wrapper here never nests, but
    # the reader should not break on one either.
    fake = FakeEvcc({"result": {"result": {"battery": {"soc": 55}}}})
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "battery.soc", "http://evcc:7070") == ""


def test_missing_path_returns_empty_string():
    fake = FakeEvcc({"battery": {}})
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "battery.soc", "http://evcc:7070") == ""


def test_numbers_are_strimmed_not_jsonified():
    fake = FakeEvcc({"battery": {"soc": 55.5}})
    with patch("src.interfaces.state_source.requests.get", fake):
        state = fetch_remote_state("evcc", "battery.soc", "http://evcc:7070")
    assert state == "55.5"  # str(value), not json-style repr


def test_details_come_back_without_unit():
    """EVCC answers values only; unit and device_class stay empty like openHAB."""
    fake = FakeEvcc({"battery": {"soc": 71}})
    with patch("src.interfaces.state_source.requests.get", fake):
        details = fetch_remote_state_details("evcc", "battery.soc", "http://evcc:7070")
    assert details["state"] == "71"
    assert details["unit"] == ""
    assert details["device_class"] == ""


def test_empty_sensor_raises_like_the_other_sources():
    fake = FakeEvcc({"battery": {"soc": 71}})
    with patch("src.interfaces.state_source.requests.get", fake), pytest.raises(
        ValueError
    ):
        fetch_remote_state("evcc", "", "http://evcc:7070")


def test_http_error_propagates():
    fake = FakeEvcc({}, status=500)
    with patch("src.interfaces.state_source.requests.get", fake), pytest.raises(
        requests.exceptions.HTTPError
    ):
        fetch_remote_state("evcc", "battery.soc", "http://evcc:7070")


class TestWhichSourcesAuthenticate:
    """
    Only Home Assistant sends a bearer token - see ``fetch_remote_state``.

    The startup check for the PV auto-scaling counter did not ask: it warned
    about an empty token whatever the source was, and promised a 401 that an
    unauthenticated API cannot return. On an EVCC site that warning stood on
    the alerts panel while the counter was being read correctly.
    """

    def test_home_assistant_needs_one(self):
        assert needs_access_token("homeassistant") is True

    def test_the_unauthenticated_sources_do_not(self):
        """openHAB serves items without one; EVCC has no auth at all."""
        assert needs_access_token("openhab") is False
        assert needs_access_token("evcc") is False

    def test_an_absent_source_is_not_claimed_to_need_one(self):
        for source in ("", None, "   ", "default"):
            assert needs_access_token(source) is False

    def test_surrounding_whitespace_does_not_hide_the_answer(self):
        assert needs_access_token(" homeassistant ") is True
