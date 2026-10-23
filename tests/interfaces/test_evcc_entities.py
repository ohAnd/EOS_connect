"""
The two questions about an EVCC name, and why they are not the same question.

Four modules used to answer them with four slightly different private helpers:
the load interface before fetching, the merger before calling a stored name
stale, the API before picking a connection to probe through, and the probe
itself. Two validated the group name, one checked only the prefix, one knew
additionally about ``sensor.``. That drift is what these pin down.
"""

import pytest

from src.interfaces.evcc_entities import (
    evcc_entity_parts,
    is_evcc_history_entity,
    is_evcc_reference,
    is_evcc_state_key,
)


@pytest.mark.parametrize(
    "name",
    ["group:home", "group:loadpoint:Carport", "tariff:grid", "group:nosuch"],
)
def test_anything_evcc_shaped_is_a_reference(name):
    assert is_evcc_reference(name)


@pytest.mark.parametrize("name", ["sensor.house_power", "Load_Power", "", None, 7])
def test_nothing_else_is(name):
    assert not is_evcc_reference(name)


def test_a_typo_in_the_group_is_still_a_reference_but_not_fetchable():
    """The whole reason for two predicates.

    The merger asks "is this EVCC's name?" to decide whether a stored value is a
    leftover Home Assistant id. Answering no for a mistyped group would have it
    replace the user's typo with a default - hiding the mistake instead of
    letting the fetch report it.
    """
    assert is_evcc_reference("group:nosuch")
    assert not is_evcc_history_entity("group:nosuch")
    assert evcc_entity_parts("group:nosuch") is None


@pytest.mark.parametrize(
    "name,expected",
    [
        ("group:home", ("home", None)),
        ("group:loadpoint:Carport", ("loadpoint", "Carport")),
        ("tariff:grid", ("tariff:grid", None)),
        ("tariff:feedin", ("tariff:feedin", None)),
    ],
)
def test_a_fetchable_name_splits_into_its_parts(name, expected):
    assert is_evcc_history_entity(name)
    assert evcc_entity_parts(name) == expected


@pytest.mark.parametrize(
    "name", ["tariff:co2", "home", "group:", "group:home:a:b", "group"]
)
def test_malformed_names_are_not_fetchable(name):
    assert not is_evcc_history_entity(name)


@pytest.mark.parametrize("name", ["battery.soc", "pvEnergy", "batterySoc"])
def test_a_state_key_may_or_may_not_be_dotted(name):
    """``pvEnergy`` has no dot and is still a key in the same document.

    Requiring one sent the PV auto-scaling counter to the supplementary Home
    Assistant connection, where it does not exist.
    """
    assert is_evcc_state_key(name)


@pytest.mark.parametrize("name", ["sensor.house_power", "group:home", "", None])
def test_entity_ids_and_references_are_not_state_keys(name):
    assert not is_evcc_state_key(name)
