"""
The shared overlap placement: one procedure, two value kinds.

`place_on_grid` is the function every timeseries consumer converges on; these
tests pin the arithmetic both kinds share (overlap weighting against an
explicit anchor) and the properties each kind guarantees — energy conservation
for extensive values, hold-and-average for intensive ones — plus the coverage
report the gap policies and the UI's forecast marker are built on.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytz

from src.timeseries_grid import (
    KIND_EXTENSIVE,
    KIND_INTENSIVE,
    TimeseriesGridError,
    clamp_report,
    horizon_slots,
    local_midnight,
    place_on_grid,
)

BERLIN = pytz.timezone("Europe/Berlin")
ANCHOR = datetime(2026, 8, 23, 0, 0, tzinfo=ZoneInfo("UTC"))


def entry(start_hours, span_hours, value):
    """One entry, offsets in hours relative to the anchor."""
    start = ANCHOR + timedelta(hours=start_hours)
    return {"start": start, "end": start + timedelta(hours=span_hours), "value": value}


class TestExtensivePlacement:
    def test_hourly_entries_land_slot_for_slot(self):
        placement = place_on_grid(
            [entry(i, 1, 100.0 * i) for i in range(4)], ANCHOR, 48, 3600, KIND_EXTENSIVE
        )
        assert placement.values[:4] == [0.0, 100.0, 200.0, 300.0]
        assert placement.coverage[:4] == [1.0] * 4

    def test_quarter_hour_entries_sum_into_an_hourly_slot(self):
        entries = [entry(i / 4, 0.25, 250.0) for i in range(4)]
        placement = place_on_grid(entries, ANCHOR, 48, 3600, KIND_EXTENSIVE)
        assert placement.values[0] == pytest.approx(1000.0)

    def test_an_hourly_entry_is_split_evenly_across_quarter_slots(self):
        placement = place_on_grid([entry(0, 1, 1000.0)], ANCHOR, 192, 900, KIND_EXTENSIVE)
        assert placement.values[:4] == pytest.approx([250.0] * 4)
        assert placement.coverage[:4] == [1.0] * 4

    def test_energy_is_conserved_when_timestamps_straddle_boundaries(self):
        # 00:30–01:30: half the energy in each touched hour, none lost at the edges.
        placement = place_on_grid([entry(0.5, 1, 1000.0)], ANCHOR, 48, 3600, KIND_EXTENSIVE)
        assert placement.values[0] == pytest.approx(500.0)
        assert placement.values[1] == pytest.approx(500.0)
        assert sum(placement.values) == pytest.approx(1000.0)
        assert placement.coverage[0] == pytest.approx(0.5)

    def test_a_hole_is_zero_with_zero_coverage(self):
        entries = [entry(0, 1, 100.0), entry(2, 1, 300.0)]
        placement = place_on_grid(entries, ANCHOR, 48, 3600, KIND_EXTENSIVE)
        assert placement.values[1] == 0.0
        assert placement.coverage[1] == 0.0


class TestIntensivePlacement:
    def test_a_coarse_rate_holds_across_every_sub_slot(self):
        placement = place_on_grid([entry(0, 1, 0.25)], ANCHOR, 192, 900, KIND_INTENSIVE)
        assert placement.values[:4] == pytest.approx([0.25] * 4)
        assert placement.values[4] == 0.0

    def test_quarter_hour_rates_average_into_an_hourly_slot(self):
        entries = [entry(i / 4, 0.25, rate) for i, rate in enumerate([0.1, 0.2, 0.3, 0.4])]
        placement = place_on_grid(entries, ANCHOR, 48, 3600, KIND_INTENSIVE)
        assert placement.values[0] == pytest.approx(0.25)
        assert placement.coverage[0] == pytest.approx(1.0)

    def test_a_partially_covered_slot_averages_what_is_present(self):
        # Only two of four quarters: the mean of what exists, coverage says half.
        entries = [entry(0, 0.25, 0.2), entry(0.25, 0.25, 0.4)]
        placement = place_on_grid(entries, ANCHOR, 48, 3600, KIND_INTENSIVE)
        assert placement.values[0] == pytest.approx(0.3)
        assert placement.coverage[0] == pytest.approx(0.5)

    def test_an_uncovered_slot_is_zero_not_nan(self):
        placement = place_on_grid([entry(5, 1, 0.3)], ANCHOR, 48, 3600, KIND_INTENSIVE)
        assert placement.values[0] == 0.0
        assert placement.coverage[0] == 0.0


class TestWindowAccounting:
    def test_entries_outside_the_horizon_are_dropped_and_counted(self):
        entries = [entry(-24, 1, 1.0), entry(0, 1, 2.0), entry(60, 1, 3.0)]
        placement = place_on_grid(entries, ANCHOR, 48, 3600, KIND_EXTENSIVE)
        assert placement.dropped == 2
        assert placement.source_count == 3
        assert sum(placement.values) == pytest.approx(2.0)

    def test_an_entry_straddling_the_anchor_keeps_its_inside_share(self):
        placement = place_on_grid([entry(-0.5, 1, 1000.0)], ANCHOR, 48, 3600, KIND_EXTENSIVE)
        assert placement.values[0] == pytest.approx(500.0)
        assert placement.dropped == 0


class TestValidation:
    def test_a_zero_span_names_the_entry(self):
        broken = entry(0, 0, 1.0)
        with pytest.raises(TimeseriesGridError) as exc:
            place_on_grid([broken], ANCHOR, 48, 3600, KIND_EXTENSIVE)
        assert "entry 0 ends at or before it starts" in str(exc.value)

    def test_a_non_numeric_value_names_the_entry(self):
        broken = entry(0, 1, None) | {"value": "abc"}
        with pytest.raises(TimeseriesGridError) as exc:
            place_on_grid([broken], ANCHOR, 48, 3600, KIND_EXTENSIVE)
        assert "entries[0].value" in str(exc.value)
        assert "must be a number" in str(exc.value)

    def test_a_missing_end_names_the_entry(self):
        with pytest.raises(TimeseriesGridError) as exc:
            place_on_grid(
                [{"start": ANCHOR, "end": None, "value": 1.0}],
                ANCHOR, 48, 3600, KIND_EXTENSIVE,
            )
        assert "entry 0 has no 'end'" in str(exc.value)

    def test_an_unknown_kind_is_a_programming_error(self):
        with pytest.raises(ValueError):
            place_on_grid([], ANCHOR, 48, 3600, "sampled")

    def test_an_empty_series_places_nothing(self):
        placement = place_on_grid([], ANCHOR, 48, 3600, KIND_EXTENSIVE)
        assert placement.values == [0.0] * 48
        assert placement.source_count == 0


class TestLocalMidnight:
    """The anchor helper the DST fix converges on."""

    def test_matches_the_replace_pattern_on_an_ordinary_day(self):
        now = BERLIN.localize(datetime(2026, 8, 23, 15, 30))
        assert local_midnight(BERLIN, now) == now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    def test_carries_midnights_own_offset_on_the_long_day(self):
        now = BERLIN.localize(datetime(2026, 10, 25, 15, 30))  # +01:00 after fall-back
        midnight = local_midnight(BERLIN, now)
        assert midnight.utcoffset() == timedelta(hours=2)
        assert int((now - midnight).total_seconds()) // 3600 == 16

    def test_carries_midnights_own_offset_on_the_short_day(self):
        now = BERLIN.localize(datetime(2026, 3, 29, 15, 30))  # +02:00 after spring-fwd
        midnight = local_midnight(BERLIN, now)
        assert midnight.utcoffset() == timedelta(hours=1)
        assert int((now - midnight).total_seconds()) // 3600 == 14

    def test_works_with_zoneinfo_as_well_as_pytz(self):
        zone = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 10, 25, 15, 30, tzinfo=zone)
        midnight = local_midnight(zone, now)
        assert midnight.utcoffset() == timedelta(hours=2)

    def test_a_now_in_another_zone_is_read_in_the_target_zone(self):
        utc_now = datetime(2026, 8, 23, 23, 30, tzinfo=ZoneInfo("UTC"))  # 01:30 Berlin
        midnight = local_midnight(BERLIN, utc_now)
        assert midnight.day == 24
        assert midnight.hour == 0


class TestHorizonSlots:
    def test_the_two_system_resolutions(self):
        assert horizon_slots(3600) == 48
        assert horizon_slots(900) == 192

    def test_a_configurable_horizon_passes_through(self):
        assert horizon_slots(3600, hours=24) == 24


class TestClampReport:
    def test_clamps_and_counts_in_one_pass(self):
        clamped, count, worst = clamp_report([0.1, 5.0, -2.0, 0.2], -0.5, 1.0)
        assert clamped == [0.1, 1.0, -0.5, 0.2]
        assert count == 2
        assert worst == 5.0

    def test_a_clean_series_reports_nothing(self):
        clamped, count, worst = clamp_report([0.1, 0.2], -0.5, 1.0)
        assert clamped == [0.1, 0.2]
        assert count == 0
        assert worst is None
