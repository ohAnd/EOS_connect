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
