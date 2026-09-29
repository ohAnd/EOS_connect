"""
Unit tests for the EvccInterface class in src.interfaces.evcc_interface.

Focused on the loadpoint mode translation needed to support EVCC >= 0.316.0's
"smart" + "alwaysCharge" mode scheme alongside the legacy "pv"/"minpv" scheme
(see GitHub issue #307).
"""

import pytest
from src.interfaces.evcc_interface import EvccInterface, NEW_MODE_API_MIN_VERSION

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access

PLACEHOLDER_URL = "http://yourEVCCserver:7070"


@pytest.fixture
def evcc_interface():
    """
    Returns an EvccInterface instance without a live EVCC connection or background thread.
    """
    return EvccInterface(PLACEHOLDER_URL)


def _loadpoint(mode, **overrides):
    """
    Builds a minimal raw loadpoint dict as returned by EVCC's /api/state.
    """
    data = {"mode": mode, "connected": True, "charging": True}
    data.update(overrides)
    return data


def test_smart_mode_with_always_charge_on_translates_to_minpv(evcc_interface):
    """smart + alwaysCharge on maps to legacy minpv."""
    lp = _loadpoint("smart", alwaysCharge="on")
    assert evcc_interface._EvccInterface__translate_loadpoint_mode(lp) == "minpv"


def test_smart_mode_with_always_charge_off_translates_to_pv(evcc_interface):
    """smart + alwaysCharge off maps to legacy pv."""
    lp = _loadpoint("smart", alwaysCharge="off")
    assert evcc_interface._EvccInterface__translate_loadpoint_mode(lp) == "pv"


def test_smart_mode_without_always_charge_field_translates_to_pv(evcc_interface):
    """Missing alwaysCharge is treated as off."""
    lp = _loadpoint("smart")
    assert evcc_interface._EvccInterface__translate_loadpoint_mode(lp) == "pv"


def test_always_charge_boolean_true_is_treated_as_on(evcc_interface):
    """alwaysCharge may be a real bool instead of the string 'on'."""
    lp = _loadpoint("smart", alwaysCharge=True)
    assert evcc_interface._EvccInterface__translate_loadpoint_mode(lp) == "minpv"


@pytest.mark.parametrize("legacy_mode", ["off", "pv", "minpv", "now"])
def test_legacy_modes_pass_through_unchanged(evcc_interface, legacy_mode):
    """Older EVCC mode strings must not be altered."""
    lp = _loadpoint(legacy_mode)
    assert evcc_interface._EvccInterface__translate_loadpoint_mode(lp) == legacy_mode


def test_unknown_mode_falls_back_to_off_without_raising(evcc_interface):
    """A future/unexpected mode string must degrade safely instead of crashing."""
    lp = _loadpoint("totally_new_future_mode")
    assert evcc_interface._EvccInterface__translate_loadpoint_mode(lp) == "off"


def test_smart_mode_marks_uses_new_mode_labels(evcc_interface):
    """Observing a raw 'smart' mode flips the UI label style, regardless of version info."""
    assert evcc_interface.get_mode_style() == "legacy"
    evcc_interface._EvccInterface__translate_loadpoint_mode(
        _loadpoint("smart", alwaysCharge="on")
    )
    assert evcc_interface.get_mode_style() == "smart"


def test_normalize_loadpoints_translates_mode_and_preserves_other_fields(evcc_interface):
    """Normalization only rewrites 'mode' and leaves every other loadpoint field intact."""
    loadpoints = [_loadpoint("smart", alwaysCharge="on", title="Carport")]
    normalized = evcc_interface._EvccInterface__normalize_loadpoints(loadpoints)
    assert normalized[0]["mode"] == "minpv"
    assert normalized[0]["title"] == "Carport"
    assert loadpoints[0]["mode"] == "smart"  # original input left untouched


@pytest.mark.parametrize(
    "version,expected_style",
    [
        ("0.315.0", "legacy"),
        ("0.316.0", "smart"),
        ("0.320.1", "smart"),
        (None, "legacy"),
        ("not-a-version", "legacy"),
    ],
)
def test_mode_style_from_version(evcc_interface, version, expected_style):
    """UI label style is derived from the EVCC version when available."""
    evcc_interface.evcc_version = version
    evcc_interface._EvccInterface__update_uses_new_mode_labels_from_version()
    assert evcc_interface.get_mode_style() == expected_style


def test_new_mode_api_min_version_constant_is_expected_value():
    """Guards against accidental changes to the documented threshold version."""
    assert NEW_MODE_API_MIN_VERSION == "0.316.0"


def test_end_to_end_smart_loadpoint_does_not_raise_and_normalizes(evcc_interface):
    """Full ingestion path (as used by the update loop) must not raise KeyError on 'smart'."""
    raw_loadpoints = [_loadpoint("smart", alwaysCharge="on", vehicleName="")]
    normalized = evcc_interface._EvccInterface__normalize_loadpoints(raw_loadpoints)

    evcc_interface._EvccInterface__get_states_of_loadpoints(normalized, {})
    sum_states = evcc_interface._EvccInterface__get_states_modes_of_connected_loadpoints(
        normalized
    )
    evcc_interface._EvccInterface__get_summerized_charging_state_n_mode(sum_states)

    assert evcc_interface.get_charging_mode() == "minpv"
    assert evcc_interface.get_current_detail_data()[0]["mode"] == "minpv"
