"""
An empty ``pv_forecast`` list is only a problem for location-based sources.

Five of the nine PV sources carry their own configuration — a resource id, a URL, an
EVCC instance, or nothing at all — and work fine with no installations defined.  A
fresh install has exactly that: ``pv_forecast`` is ``[]`` until the user configures
one.  Before this, a single unconditional guard rejected the empty list for *every*
source, so a fresh evcc or solcast install started degraded with its forecast zeroed,
and a switch to one of those sources from the web UI was rolled back rather than
applied — leaving the user no way to fix it from the UI at all.
"""

import logging

import pytest

from src.config_web.schema import LOCATION_BASED_PV_SOURCES as SCHEMA_SOURCES
from src.interfaces.pv_interface import (
    LOCATION_BASED_PV_SOURCES as INTERFACE_SOURCES,
    PvInterface,
)

ALL_SOURCES = [
    "akkudoktor",
    "openmeteo",
    "openmeteo_local",
    "forecast_solar",
    "evcc",
    "solcast",
    "victron",
    "timeseries",
    "default",
]


@pytest.fixture(autouse=True)
def no_update_thread(monkeypatch):
    """
    Keep the background fetcher out of it.

    What is under test is the validation ``__init__`` runs before anything is fetched.
    A *valid* configuration would otherwise start the update thread and make a real
    request to the forecast provider, so these tests would depend on the network to
    assert something that never touches it.
    """
    monkeypatch.setattr(
        PvInterface, "_PvInterface__start_update_service", lambda self: None
    )


def _installation():
    """
    One installation, with the fields the merger would have filled in.

    ``_build_pv_forecast`` backfills every schema default onto a stored entry, so a
    real installation always arrives complete even though the wizard only asks for
    six of the fields.
    """
    return {
        "name": "roof",
        "lat": 47.5,
        "lon": 8.5,
        "azimuth": 0,
        "tilt": 30,
        "power": 4600,
        "powerInverter": 5000,
        "inverterEfficiency": 0.9,
        "horizon": "",
    }


def _pv(source, entries):
    """A PvInterface for *source* with *entries* installations, no threads started."""
    return PvInterface(
        config_source={
            "source": source,
            "api_key": "an-api-key",
            "resource_id": "a-resource-id",
            "data_url": "http://sensor.local/pv",
            "data_path": "attributes.data",
            "value_unit": "W",
            "use_real_data_correction": True,
        },
        config=entries,
        time_frame_base=3600,
        config_special={"url": "http://evcc.local:7070"},
        temperature_forecast_enabled=False,
        timezone="Europe/Berlin",
    )


def test_the_two_source_lists_agree():
    """
    ``interfaces`` and ``config_web`` are sibling top-level packages at runtime, so
    ``pv_interface`` cannot import the schema's copy.  This is what keeps the duplicate
    honest — if one list gains a source, the other has to as well.
    """
    assert sorted(INTERFACE_SOURCES) == sorted(SCHEMA_SOURCES)


@pytest.mark.parametrize(
    "source", [s for s in ALL_SOURCES if s not in SCHEMA_SOURCES]
)
def test_sources_that_need_no_installations_start_valid(source):
    """A fresh install of a self-configuring source is not degraded."""
    pv = _pv(source, [])

    assert pv.configuration_valid is True
    assert pv.configuration_state == "valid"


@pytest.mark.parametrize("source", SCHEMA_SOURCES)
def test_location_based_sources_still_need_an_installation(source):
    """The check that matters is kept: no coordinates, no forecast."""
    pv = _pv(source, [])

    assert pv.configuration_valid is False
    assert pv.configuration_state == "incomplete"


@pytest.mark.parametrize("source", SCHEMA_SOURCES)
def test_location_based_sources_are_valid_once_configured(source):
    """And it clears as soon as there is something to work from."""
    pv = _pv(source, [_installation()])

    assert pv.configuration_valid is True


def test_switching_to_a_self_configuring_source_is_not_rolled_back():
    """
    The reason this mattered most: ``reload_config`` validates with ``strict=True`` and
    restores the previous configuration if validation raises.  A user on akkudoktor with
    no installations who switched to evcc used to have the switch refused, with the
    advice pointing back at the UI they were already in.
    """
    pv = _pv("akkudoktor", [])

    pv.reload_config(
        {"source": "evcc", "use_real_data_correction": True},
        [],
        {"url": "http://evcc.local:7070"},
        False,
        "Europe/Berlin",
    )

    assert pv.config_source["source"] == "evcc"
    assert pv.configuration_valid is True


def test_the_advice_names_a_section_that_exists(caplog):
    """
    The old message sent users to "Settings > PV Forecast".  There is no such section —
    ``SECTION_META`` defines "PV Source" and "PV Installations" — so the one actionable
    sentence in the error was a dead end.
    """
    with caplog.at_level(logging.WARNING, logger="__main__"):
        _pv("akkudoktor", [])

    advice = " ".join(r.message for r in caplog.records)
    assert "PV Forecast" not in advice
