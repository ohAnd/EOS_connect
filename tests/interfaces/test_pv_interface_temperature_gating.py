# pylint: disable=protected-access
"""
Who gets an outside-temperature forecast, and who can turn it off — issue #289.

Two reporters saw requests to ``api.akkudoktor.net`` while running a local EOS server and
asked for them to be gated on the EOS URL.  That conflates two things: Akkudoktor is the
*forecast provider* for the temperature curve, which has nothing to do with where the
*optimizer* runs.  ``eos.source: eos_server`` is always a self-hosted EOS — there is no
public EOS API — so URL-gating would have silently disabled the forecast for everyone.
EOS wants the curve and models the house more precisely with it, so it stays on by
default and ``eos.temperature_forecast_enabled`` is the way out.

The rule lives in two places: ``interfaces.pv_interface.wants_temperature_forecast`` and
an inline copy in ``config_web.hot_reload``, which imports nothing cross-package by
design.  These tests are what keeps the copy honest.
"""

import pytest

from src.config_web.hot_reload import _wants_temperature_forecast as hot_reload_rule
from src.interfaces.pv_interface import wants_temperature_forecast as startup_rule

# (eos config, wanted) — the full truth table over source and switch.
CASES = [
    ({}, True),                                                    # eos_server default
    ({"source": "eos_server"}, True),
    ({"source": "eos_server", "temperature_forecast_enabled": True}, True),
    ({"source": "eos_server", "temperature_forecast_enabled": False}, False),
    ({"source": "evopt"}, False),
    ({"source": "evopt", "temperature_forecast_enabled": True}, False),
    ({"source": "local_evopt"}, False),
    ({"source": "local_evopt", "temperature_forecast_enabled": True}, False),
    # A bool that round-tripped through the config store as a string.
    ({"source": "eos_server", "temperature_forecast_enabled": "false"}, False),
    ({"source": "eos_server", "temperature_forecast_enabled": "true"}, True),
]


@pytest.mark.parametrize("eos_config,wanted", CASES)
def test_the_rule_holds(eos_config, wanted):
    """EVopt never asks for temperature; EOS does unless the user says otherwise."""
    assert startup_rule(dict(eos_config)) is wanted


@pytest.mark.parametrize("eos_config,wanted", CASES)
def test_the_two_copies_of_the_rule_agree(eos_config, wanted):
    """
    ``hot_reload`` decides this again on every live config change.  If the copies drifted,
    toggling an unrelated PV field would quietly flip the temperature forecast back on.
    """
    assert hot_reload_rule(dict(eos_config)) is wanted


def test_a_missing_eos_section_asks_for_nothing():
    """Defensive: no eos config at all is not a reason to start calling a provider."""
    assert startup_rule(None) is False
    assert hot_reload_rule(None) is False


def test_the_switch_is_declared_in_the_schema():
    """
    The switch is only usable if the web UI knows about it, and it belongs to the one
    backend that uses temperature.
    """
    from src.config_web.schema import ConfigSchema  # pylint: disable=import-outside-toplevel

    field = ConfigSchema().get("eos.temperature_forecast_enabled")

    assert field is not None

    assert field.field_type == "bool"
    assert field.default is True
    assert field.hot_reload is True
    assert field.depends_on == {"eos.source": "eos_server"}


# ── A second consumer: outdoor managed loads ────────────────────────────────────
#
# The rule above is about what the *optimizer* wants, and it left a pool heat pump
# reading a flat 15 degrees on the default backend — a weather-driven model that could
# not see the weather. An outdoor managed load now asks for the curve on its own
# account, which overrides the source check but not the explicit opt-out.

ALSO_NEEDED_CASES = [
    ({"source": "local_evopt"}, True),
    ({"source": "evopt"}, True),
    ({"source": "eos_server"}, True),
    ({}, True),
    # The opt-out is the documented way to stop the requests, and stays that.
    ({"source": "eos_server", "temperature_forecast_enabled": False}, False),
    ({"source": "local_evopt", "temperature_forecast_enabled": False}, False),
]


@pytest.mark.parametrize("eos_config,wanted", ALSO_NEEDED_CASES)
def test_an_outdoor_managed_load_asks_for_the_curve_itself(eos_config, wanted):
    assert startup_rule(eos_config, also_needed=True) is wanted


@pytest.mark.parametrize("eos_config,wanted", ALSO_NEEDED_CASES)
def test_the_inline_copy_agrees_about_that_too(eos_config, wanted):
    assert hot_reload_rule(eos_config, also_needed=True) is wanted


@pytest.mark.parametrize("eos_config,_wanted", CASES)
def test_not_needing_it_leaves_the_original_rule_alone(eos_config, _wanted):
    """The new argument defaults to off, so nothing changes for an install without one."""
    assert startup_rule(eos_config, also_needed=False) is startup_rule(eos_config)
    assert hot_reload_rule(eos_config, also_needed=False) is hot_reload_rule(eos_config)


def test_a_non_dict_config_still_answers_for_the_managed_load():
    assert startup_rule(None, also_needed=True) is True
    assert hot_reload_rule(None, also_needed=True) is True
    assert startup_rule(None) is False
    assert hot_reload_rule(None) is False


# ── Telling a fetched forecast from the placeholder ─────────────────────────────

def test_the_placeholder_curve_is_not_reported_as_a_forecast():
    """
    Both are 48 plausible numbers, and a caller cannot tell them apart. Managed loads
    preferred the 15 degree placeholder over a user's own outdoor sensor because of it.
    """
    from src.interfaces.pv_interface import PvInterface

    iface = PvInterface.__new__(PvInterface)
    iface.temperature_forecast_enabled = True
    iface.last_successful_temp_forecast = []
    assert iface.has_real_temperature_forecast() is False

    iface.last_successful_temp_forecast = [12.0] * 48
    assert iface.has_real_temperature_forecast() is True

    iface.temperature_forecast_enabled = False
    assert iface.has_real_temperature_forecast() is False


# ── Where the temperature request gets its coordinates ──────────────────────────

def _interface(pv_config, site_location=None):
    from src.interfaces.pv_interface import PvInterface

    iface = PvInterface.__new__(PvInterface)
    iface.config = pv_config
    iface.site_location = None
    if site_location is not None:
        from src.interfaces.pv_interface import _clean_site_location
        iface.site_location = _clean_site_location(site_location)
    return iface


def _entry(iface):
    # Name-mangled: the method is private and this is the only way in from outside.
    return iface._PvInterface__get_temperature_config_entry()


def test_a_pv_installation_supplies_the_coordinates_as_before():
    iface = _interface([{"name": "RoofA", "lat": 47.5, "lon": 8.5}])
    assert _entry(iface)["lat"] == 47.5


def test_the_site_location_covers_a_source_with_no_installations():
    """
    pv_forecast is empty for every source that is not location-based -- EVCC, Solcast,
    Victron, timeseries -- so a pool heat pump had no coordinates to ask with at all.
    """
    iface = _interface([], site_location=(52.52, 13.405))
    entry = _entry(iface)
    assert (entry["lat"], entry["lon"]) == (52.52, 13.405)


def test_a_pv_installation_still_wins_over_the_site_location():
    """A fallback, not an override -- an existing install must behave identically."""
    iface = _interface([{"name": "RoofA", "lat": 47.5, "lon": 8.5}],
                       site_location=(52.52, 13.405))
    assert _entry(iface)["lat"] == 47.5


def test_nothing_configured_still_means_no_temperature_request():
    assert _entry(_interface([])) is None
    assert _entry(_interface([], site_location=(0.0, 0.0))) is None


@pytest.mark.parametrize("bad", [
    (0.0, 0.0),          # the "not set" value a numeric field needs
    (91.0, 10.0),        # off the planet
    (10.0, 181.0),
    ("north", "east"),
    None,
    (),
])
def test_unusable_coordinates_are_ignored_rather_than_sent(bad):
    from src.interfaces.pv_interface import _clean_site_location
    assert _clean_site_location(bad) is None


def test_a_real_pair_survives_cleaning():
    from src.interfaces.pv_interface import _clean_site_location
    assert _clean_site_location(("52.52", "13.405")) == (52.52, 13.405)
    # Greenwich is a real place and a real longitude.
    assert _clean_site_location((51.48, 0.0)) == (51.48, 0.0)
