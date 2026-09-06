"""
Payload parsing for the push endpoint.

These cover the shapes the issue thread actually asked for - a daily average, a 24 h
array, a 96-slot array - plus the unit and alignment mistakes a first integration makes.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.loads.injection import (
    InjectionError,
    PushedContingent,
    PushedProfile,
    parse_push,
    resample,
)

BERLIN = ZoneInfo("Europe/Berlin")
ANCHOR = datetime(2026, 6, 1, 0, 0, tzinfo=BERLIN)
NOW = datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc)


def _push(payload, slot_count=48, base=3600, current_slot=10):
    return parse_push(
        payload,
        anchor=ANCHOR,
        slot_count=slot_count,
        time_frame_base=base,
        current_slot=current_slot,
        now=NOW,
    )


# --- the shapes from the issue -------------------------------------------------------

def test_constant_hourly_average_covers_the_whole_horizon():
    """ohAnd's proposal: push one average and have it apply for today and tomorrow."""
    result = _push({"value_wh": 1500})
    assert isinstance(result, PushedProfile)
    assert result.slots_wh == [1500.0] * 48


def test_constant_average_is_split_across_quarter_hour_slots():
    """1500 Wh per hour is 375 Wh per 15-minute slot - energy, not power, is conserved."""
    result = _push({"value_wh": 1500}, slot_count=192, base=900)
    assert result.slots_wh == [375.0] * 192
    assert sum(result.slots_wh) == pytest.approx(48 * 1500)


def test_twentyfour_hourly_values_fill_the_first_day():
    """The 0,0,0,0,1.2,1.4,... form from the issue - day two is left alone."""
    values = [0, 0, 0, 0, 1200, 1400, 1500, 1600] + [0] * 16
    result = _push({"values": values})
    assert result.slots_wh[:24] == [float(v) for v in values]
    assert result.slots_wh[24:] == [0.0] * 24
    assert result.source_resolution_s == 3600


def test_ninetysix_slot_array_is_accepted_and_aggregated_to_hourly():
    """j4rvisstant's 96-slot form, pushed to an optimizer running on 60-minute slots."""
    result = _push({"values": [250.0] * 96})
    assert result.source_resolution_s == 900
    assert result.slots_wh[:24] == [1000.0] * 24
    assert result.slots_wh[24:] == [0.0] * 24


def test_192_slot_array_maps_one_to_one_at_15_minutes():
    result = _push({"values": [100.0] * 192}, slot_count=192, base=900)
    assert result.slots_wh == [100.0] * 192


def test_energy_contingent_form():
    result = _push({"total_wh": 8000, "deadline_hours": 24})
    assert isinstance(result, PushedContingent)
    assert result.total_wh == 8000.0
    assert result.deadline_slot == 34  # current_slot 10 + 24 hourly slots


def test_contingent_deadline_is_capped_at_the_horizon():
    result = _push({"total_wh": 8000, "deadline_hours": 500})
    assert result.deadline_slot == 47


def test_a_bare_number_over_mqtt_is_the_hourly_average_form():
    """Publishing a plain number is the easiest thing to do from an automation."""
    result = _push(900)
    assert result.slots_wh == [900.0] * 48


# --- units and resolution ------------------------------------------------------------

def test_watts_are_converted_using_the_source_slot_length():
    """800 W held for a quarter hour is 200 Wh - converting after resampling gets this wrong."""
    result = _push({"values": [800.0] * 96, "unit": "W"}, slot_count=192, base=900)
    assert result.slots_wh[:96] == [200.0] * 96


def test_watts_at_hourly_resolution():
    result = _push({"values": [800.0] * 24, "unit": "W"})
    assert result.slots_wh[:24] == [800.0] * 24


def test_explicit_resolution_overrides_the_inferred_one():
    result = _push({"values": [100.0] * 48, "resolution": "15min"}, slot_count=192, base=900)
    assert result.source_resolution_s == 900
    assert result.slots_wh[:48] == [100.0] * 48


def test_resample_conserves_energy_both_ways():
    hourly = [100.0, 200.0, 300.0]
    quarterly = resample(hourly, 3600, 900)
    assert len(quarterly) == 12
    assert sum(quarterly) == pytest.approx(sum(hourly))
    assert resample(quarterly, 900, 3600) == pytest.approx(hourly)


# --- alignment -----------------------------------------------------------------------

def test_start_now_offsets_the_series_to_the_current_slot():
    result = _push({"values": [500.0] * 24, "start": "now"}, current_slot=10)
    assert result.slots_wh[:10] == [0.0] * 10
    assert result.slots_wh[10:34] == [500.0] * 24


def test_start_accepts_an_iso_timestamp():
    result = _push({
        "values": [500.0] * 4,
        "resolution": "hourly",
        "start": "2026-06-01T06:00:00+02:00",
    })
    assert result.slots_wh[6:10] == [500.0] * 4
    assert result.slots_wh[:6] == [0.0] * 6


def test_values_past_the_horizon_are_dropped_with_a_warning(caplog):
    """Silently discarding half a push is how an alignment bug survives for weeks."""
    with caplog.at_level("WARNING", logger="__main__"):
        result = _push({"values": [100.0] * 48, "start": "now"}, current_slot=40)
    assert result.slots_wh[40:] == [100.0] * 8
    assert any("outside" in r.getMessage() for r in caplog.records)


# --- rejection -----------------------------------------------------------------------

def test_negative_values_are_allowed():
    """A downward correction is exactly issue #55's cooling-versus-heating delta."""
    result = _push({"values": [-300.0] * 24})
    assert result.slots_wh[:24] == [-300.0] * 24


@pytest.mark.parametrize("payload,fragment", [
    ({}, "must contain"),
    ({"values": []}, "must not be empty"),
    ({"values": "nope"}, "must be an array"),
    ({"values": [1, "x", 3]}, "must be a number"),
    ({"values": [1.0] * 7}, "cannot infer"),  # a short series must say its resolution
    ({"values": [1.0] * 24, "unit": "kWh"}, "unknown unit"),
    ({"values": [1.0] * 24, "resolution": "daily"}, "unknown resolution"),
    ({"values": [1.0] * 24, "start": "sometime"}, "ISO timestamp"),
    ({"values": [float("inf")] * 24}, "finite"),
    ({"values": [500000.0] * 24}, "plausible range"),
    ({"total_wh": -5}, "cannot be negative"),
    ({"total_wh": 100, "deadline_hours": 0}, "must be positive"),
    (None, "empty payload"),
    ("string", "expected a JSON object"),
])
def test_malformed_payloads_are_rejected_with_a_useful_message(payload, fragment):
    with pytest.raises(InjectionError) as excinfo:
        _push(payload)
    assert fragment in str(excinfo.value)


def test_ttl_defaults_and_overrides():
    default = _push({"value_wh": 100})
    assert default.valid_until == NOW + timedelta(minutes=1440)
    explicit = _push({"value_wh": 100, "ttl_minutes": 60})
    assert explicit.valid_until == NOW + timedelta(minutes=60)
