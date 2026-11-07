"""
Live reads of EVCC series references through the shared state reader.

The history endpoint always served a loadpoint series; its live presence in the
state document is a list of objects matched by title, which a dotted path can
never reach - so a managed load pointed at one read as 0 W every cycle while
its history probe answered correctly. Pins that both shapes answer.
"""

from unittest.mock import MagicMock, patch

from src.interfaces.state_source import (
    fetch_remote_state,
    fetch_remote_state_details,
)


class FakeEvcc:
    """requests.get for the one endpoint this source talks to."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, **kwargs):
        # pylint: disable=unused-argument
        self.calls.append(url)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = self.payload
        return response


LIVE_DOCUMENT = {
    "homePower": 549.7,
    "pvPower": 439.7,
    "grid": {"power": -6.5},
    "battery": {"power": 196.0, "soc": 45.5},
    "loadpoints": [
        {"title": "Wärmepumpe", "chargePower": 79.4},
        {"title": "Charger", "chargePower": 0},
    ],
    "consumers": [{"title": "Klima", "power": 2.1}],
}


def test_loadpoint_series_reads_its_live_power():
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        state = fetch_remote_state(
            "evcc", "group:loadpoint:Wärmepumpe", "http://evcc:7070"
        )
    assert state == "79.4"


def test_title_is_matched_case_insensitively():
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        state = fetch_remote_state("evcc", "group:loadpoint:wärmepumpe", "http://x")
    assert state == "79.4"


def test_untitled_loadpoint_group_has_no_single_answer():
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "group:loadpoint", "http://x") == ""


def test_unknown_title_answers_empty_not_a_neighbour():
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "group:loadpoint:Nope", "http://x") == ""


def test_consumer_series_reads_its_live_power():
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "group:consumer:Klima", "http://x") == "2.1"


def test_home_and_pv_groups_read_their_state_keys():
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "group:home", "http://x") == "549.7"
        assert fetch_remote_state("evcc", "group:pv", "http://x") == "439.7"


def test_grid_and_battery_groups_read_their_power():
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "group:grid", "http://x") == "-6.5"
        assert fetch_remote_state("evcc", "group:battery", "http://x") == "196.0"


def test_dotted_key_still_answers_beside_the_series():
    """The reader's original contract keeps working - both shapes in one call."""
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        assert fetch_remote_state("evcc", "battery.soc", "http://x") == "45.5"


def test_details_carry_the_series_value_too():
    fake = FakeEvcc(LIVE_DOCUMENT)
    with patch("src.interfaces.state_source.requests.get", fake):
        details = fetch_remote_state_details(
            "evcc", "group:loadpoint:Wärmepumpe", "http://x"
        )
    assert details["state"] == "79.4"
    assert details["unit"] == ""