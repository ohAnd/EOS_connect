"""
What EOS Connect takes from an EVCC site's own battery readings.

Capacity and the SoC window are physical facts EVCC already knows, so they are
read rather than typed. The window is the part with a rule: EOS Connect models
one battery, EVCC reports per device, and the two have to be reconciled without
ever driving an individual battery past its own limit.

The source matters as much as the numbers. EVCC carries site-level
``bufferSoc``/``prioritySoc`` that look like SoC bounds and are not - they say
when surplus stops being stored and what stays in reserve for the household.
Reading those as the physical range once left a site with priority 70 configured
as "min SOC 70%", locking the optimizer out of three quarters of its battery.
The real limits come off each battery in ``battery.devices[]``.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.interfaces import evcc_parameters
from src.interfaces.evcc_parameters import fetch_evcc_parameters

URL = "http://evcc.local:7070"


@pytest.fixture(autouse=True)
def _empty_cache():
    """Answers are cached per URL; every test starts from cold."""
    evcc_parameters.clear_cache()
    yield
    evcc_parameters.clear_cache()


def _answers(state):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = state
    response.raise_for_status.return_value = None
    return patch("src.interfaces.evcc_parameters.requests.get", return_value=response)


def test_capacity_comes_back_in_wh():
    """EVCC reports kWh; the optimizer works in Wh."""
    with _answers({"battery": {"capacity": 11.5, "soc": 62.0}}):
        parameters = fetch_evcc_parameters(URL)
    assert parameters["capacity_wh"] == 11500
    assert parameters["soc_percentage"] == 62.0


def test_a_single_battery_hands_over_its_own_limits():
    with _answers(
        {
            "battery": {
                "capacity": 10.0,
                "devices": [{"minSoc": 10, "maxSoc": 95}],
            }
        }
    ):
        parameters = fetch_evcc_parameters(URL)
    assert parameters["min_soc_percentage"] == 10
    assert parameters["max_soc_percentage"] == 95


def test_two_batteries_are_reconciled_to_the_tighter_window():
    """The intersection, so neither battery is driven past its own limit.

    Averaging - by capacity or otherwise - would widen the window beyond what
    the tighter battery allows, which is the one case that must not happen.
    """
    with _answers(
        {
            "battery": {
                "devices": [
                    {"minSoc": 5, "maxSoc": 100},
                    {"minSoc": 15, "maxSoc": 90},
                ]
            }
        }
    ):
        parameters = fetch_evcc_parameters(URL)
    assert parameters["min_soc_percentage"] == 15
    assert parameters["max_soc_percentage"] == 90


def test_devices_without_limits_contribute_nothing():
    """Not every battery integration implements EVCC's SoC-limit capability."""
    with _answers({"battery": {"capacity": 10.0, "devices": [{"soc": 55}]}}):
        parameters = fetch_evcc_parameters(URL)
    assert "min_soc_percentage" not in parameters
    assert "max_soc_percentage" not in parameters
    assert parameters["capacity_wh"] == 10000


def test_a_window_that_does_not_overlap_is_refused():
    """Two batteries with incompatible limits leave the configured values.

    An empty range is worse than a hand-typed one: the optimizer would have
    nothing to work in at all.
    """
    with _answers(
        {
            "battery": {
                "devices": [
                    {"minSoc": 80, "maxSoc": 100},
                    {"minSoc": 5, "maxSoc": 50},
                ]
            }
        }
    ):
        parameters = fetch_evcc_parameters(URL)
    assert parameters is None or "min_soc_percentage" not in parameters


def test_the_site_level_buffer_and_priority_soc_are_never_read():
    """They are charging behaviour, not the battery's range - the old bug."""
    with _answers({"battery": {"capacity": 10.0}, "bufferSoc": 20, "prioritySoc": 70}):
        parameters = fetch_evcc_parameters(URL)
    assert "min_soc_percentage" not in parameters
    assert "max_soc_percentage" not in parameters


def test_an_unreachable_site_answers_none():
    with patch(
        "src.interfaces.evcc_parameters.requests.get",
        side_effect=requests.exceptions.ConnectionError("down"),
    ):
        assert fetch_evcc_parameters(URL) is None


def test_the_answer_is_cached_including_the_failure():
    """The merge asks on every settings save; one save must not cost a round trip.

    Failures are cached too - an unreachable instance should cost one timeout,
    not one per save.
    """
    with patch(
        "src.interfaces.evcc_parameters.requests.get",
        side_effect=requests.exceptions.ConnectionError("down"),
    ) as getter:
        assert fetch_evcc_parameters(URL) is None
        assert fetch_evcc_parameters(URL) is None
        assert getter.call_count == 1


def test_the_placeholder_address_is_not_asked():
    with patch("src.interfaces.evcc_parameters.requests.get") as getter:
        assert fetch_evcc_parameters("http://yourEVCCserver:7070") is None
    getter.assert_not_called()
