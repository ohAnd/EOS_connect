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
    Connection,
    connection_for,
    connections_from_section,
    evcc_entity_parts,
    ha_entity_connection,
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


class TestWhichConnectionAnswers:
    """
    One rule, because it had been written four times and the copies disagreed.

    The load interface routed by history reference alone, the entity probe also
    knew about live state keys, the merger required no URL behind a
    supplementary type while the probe did, and the managed-load readers did not
    route at all - they asked EVCC for the dishwasher and read back an empty
    string forever.
    """

    EVCC = Connection("evcc", "http://evcc.local:7070", "", False)
    HA = Connection("homeassistant", "http://ha.local:8123", "tok", False)

    def test_a_single_source_answers_everything(self):
        """Nothing to decide when there is one connection."""
        for name in ("sensor.washer", "group:home", "battery.soc", ""):
            assert connection_for(name, self.HA, None) is self.HA

    def test_evcc_keeps_what_it_meters(self):
        for name in ("group:home", "group:consumer:Sauna", "tariff:grid"):
            assert connection_for(name, self.EVCC, self.HA) is self.EVCC

    def test_evcc_keeps_its_own_live_state(self):
        """``battery.soc`` is dotted and ``pvEnergy`` is not - both are EVCC's."""
        for name in ("battery.soc", "pvEnergy"):
            assert connection_for(name, self.EVCC, self.HA) is self.EVCC

    def test_a_home_assistant_entity_goes_to_the_second_connection(self):
        assert connection_for("sensor.washer", self.EVCC, self.HA) is self.HA

    def test_nothing_answers_when_there_is_no_second_connection(self):
        """None, not the EVCC connection - a request there could only 404."""
        assert connection_for("sensor.washer", self.EVCC, None) is None

    def test_a_history_fetch_does_not_claim_live_keys(self):
        """
        A state key has no history series behind it, so the history endpoint
        cannot answer it however EVCC-ish the name looks.
        """
        assert (
            connection_for(
                "battery.soc", self.EVCC, self.HA, evcc_serves=is_evcc_reference
            )
            is self.HA
        )
        assert (
            connection_for(
                "group:home", self.EVCC, self.HA, evcc_serves=is_evcc_reference
            )
            is self.EVCC
        )

    def test_central_mode_never_resolves_to_evcc(self):
        """
        Central mode reads /api/states, which EVCC does not have - so the
        protocol decides, not the entity name.
        """
        assert ha_entity_connection(self.EVCC, self.HA) is self.HA
        assert ha_entity_connection(self.HA, None) is self.HA


class TestReadingConnectionsOffASection:
    """The merged ``load`` and ``battery`` sections carry both connections."""

    SECTION = {
        "source": "evcc",
        "url": "http://evcc.local:7070",
        "access_token": "",
        "ssl_ignore": False,
        "supplementary_type": "homeassistant",
        "supplementary_url": "http://ha.local:8123",
        "supplementary_access_token": "tok",
        "supplementary_ssl_ignore": True,
    }

    def test_both_connections_are_read(self):
        primary, supplementary = connections_from_section(self.SECTION)
        assert primary == Connection("evcc", "http://evcc.local:7070", "", False)
        assert supplementary == Connection(
            "homeassistant", "http://ha.local:8123", "tok", True
        )

    def test_a_supplementary_type_without_a_url_is_absent(self):
        """
        It cannot answer anything. Reporting "configure one" beats issuing a
        request against an empty host and reporting whatever that does.
        """
        section = dict(self.SECTION, supplementary_url="")
        assert connections_from_section(section)[1] is None

    def test_an_unknown_supplementary_type_is_absent(self):
        section = dict(self.SECTION, supplementary_type="evcc")
        assert connections_from_section(section)[1] is None

    def test_an_empty_section_does_not_raise(self):
        """A section can be missing entirely before the first save."""
        primary, supplementary = connections_from_section({})
        assert primary == Connection("", "", "", False)
        assert supplementary is None
        assert connections_from_section(None)[1] is None
