# pylint: disable=protected-access
"""
The price gap-fill policy, pinned scenario by scenario.

These six scenarios originally pinned the 15-min-to-hourly converter written
for issue #267. The converter is gone — placement is the shared intensive
overlap in timeseries_grid, and the fill rules live in
price_interface.fill_price_gaps — but the policy the scenarios encode is the
contract that had to survive the move, with unchanged numbers:

  - a slot averages the prices that actually cover it (also when incomplete)
  - slots before the first data carry the first known price, backwards
  - an interior hole carries the last known price, forwards
  - tomorrow's uncovered slots repeat today's value for the same hour
  - entries before the window are dropped, never smeared into slot 0

The parser returns plain EUR/Wh floats now; the per-slot start/end echo died
with the converter's dict shape.
"""

from datetime import datetime, timezone, timedelta

import pytest

from src.interfaces.price_interface import PriceInterface, fill_price_gaps

WINDOW_START = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)


def _make_price_interface(time_frame_base=3600, monkeypatch=None):
    """Helper: create PriceInterface for testing."""
    if monkeypatch:
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface."
            "_PriceInterface__start_update_service",
            lambda self: None,
        )
    cfg = {"source": "fixed", "fixed_price_eur_per_mwh": 30}
    return PriceInterface(cfg, time_frame_base, timezone=timezone.utc)


def _quarters(start_offset_minutes, values, base=WINDOW_START):
    """15-min entries beginning *start_offset_minutes* after the window start."""
    first = base + timedelta(minutes=start_offset_minutes)
    return [
        {
            "start": (first + timedelta(minutes=15 * i)).isoformat(),
            "end": (first + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": value,
        }
        for i, value in enumerate(values)
    ]


def _parse(iface, timeseries, window_start=WINDOW_START):
    return iface._PriceInterface__parse_price_timeseries(
        timeseries, 48, None, window_start
    )


class TestQuarterHourScenarios:
    """The issue-#267 scenarios, numbers unchanged."""

    def test_basic_15min_to_hourly_conversion(self, monkeypatch):
        """4 prices within hour 0 average into slot 0; the rest carries it on."""
        iface = _make_price_interface(3600, monkeypatch)

        result = _parse(iface, _quarters(0, [0.100, 0.110, 0.120, 0.130]))

        assert len(result) == 48
        assert result[0] == pytest.approx(0.115, abs=1e-6)
        for value in result[1:]:
            assert value == pytest.approx(0.115, abs=1e-6)

    def test_159_evcc_prices_to_48_hourly_slots(self, monkeypatch):
        """EVCC's rolling window starting at 09:00: the past hours are backfilled."""
        iface = _make_price_interface(3600, monkeypatch)

        values = [0.100 + (i % 10) * 0.001 for i in range(159)]
        timeseries = _quarters(9 * 60, values)

        result = _parse(iface, timeseries)

        assert len(result) == 48
        for value in result:
            assert 0 < value < 1
        # Hours 0-8: backfilled with hour 9's average.
        expected_first_hourly = sum(values[:4]) / 4
        for value in result[0:9]:
            assert value == pytest.approx(expected_first_hourly, abs=1e-6)

    def test_incomplete_hours_handled_gracefully(self, monkeypatch):
        """Partially covered hours average what is present; tomorrow cycles today."""
        iface = _make_price_interface(3600, monkeypatch)

        timeseries = (
            _quarters(0, [0.100, 0.110, 0.120, 0.130])  # hour 0: complete
            + _quarters(90, [0.140, 0.150])  # hour 1: 01:30-02:00 only
            + _quarters(120, [0.160, 0.170, 0.180])  # hour 2: 45 minutes
        )

        result = _parse(iface, timeseries)

        assert len(result) == 48
        assert result[0] == pytest.approx(0.115, abs=1e-6)
        assert result[1] == pytest.approx(0.145, abs=1e-6)
        assert result[2] == pytest.approx(0.170, abs=1e-6)
        # Same-day tail: forward-filled with the last known value.
        for value in result[3:24]:
            assert value == pytest.approx(0.170, abs=1e-6)
        # Next day: cyclic repeat of today's pattern.
        assert result[24] == pytest.approx(0.115, abs=1e-6)
        assert result[25] == pytest.approx(0.145, abs=1e-6)
        assert result[26] == pytest.approx(0.170, abs=1e-6)
        for value in result[27:]:
            assert value == pytest.approx(0.170, abs=1e-6)

    def test_skipped_hours_create_gaps(self, monkeypatch):
        """An hour with no data carries the last known price, nothing shifts."""
        iface = _make_price_interface(3600, monkeypatch)

        timeseries = _quarters(0, [0.100, 0.110, 0.120, 0.130]) + _quarters(
            120, [0.140, 0.150, 0.160, 0.170]
        )

        result = _parse(iface, timeseries)

        assert len(result) == 48
        assert result[0] == pytest.approx(0.115, abs=1e-6)
        assert result[1] == pytest.approx(0.115, abs=1e-6)  # the hole, filled forward
        assert result[2] == pytest.approx(0.155, abs=1e-6)

    def test_midnight_boundary_crossing(self, monkeypatch):
        """An entry ending exactly at the window start belongs to yesterday."""
        iface = _make_price_interface(3600, monkeypatch)

        timeseries = _quarters(-15, [0.100]) + _quarters(
            0, [0.110, 0.120, 0.130, 0.140]
        )

        result = _parse(iface, timeseries)

        assert len(result) == 48
        assert result[0] == pytest.approx(0.125, abs=1e-6)
        assert result[0] != pytest.approx(0.100, abs=1e-6)
        for value in result[1:]:
            assert value == pytest.approx(0.125, abs=1e-6)

    def test_very_small_dataset(self, monkeypatch):
        """Two prices still produce a full horizon."""
        iface = _make_price_interface(3600, monkeypatch)

        result = _parse(iface, _quarters(0, [0.100, 0.110]))

        assert len(result) == 48
        assert result[0] == pytest.approx(0.105, abs=1e-6)
        for value in result[1:]:
            assert value == pytest.approx(0.105, abs=1e-6)


class TestFillPriceGaps:
    """The policy function itself, against hand-built coverage."""

    def test_leading_interior_and_cyclic_fill(self):
        values = [0.0, 0.2, 0.0, 0.4] + [0.0] * 44
        coverage = [0.0, 1.0, 0.0, 1.0] + [0.0] * 44

        filled, leading, forecast_start = fill_price_gaps(
            values, coverage, slots_per_day=24
        )

        assert leading == 1
        assert filled[0] == pytest.approx(0.2)  # backwards, first known
        assert filled[2] == pytest.approx(0.2)  # interior, last known
        assert filled[4] == pytest.approx(0.4)  # same-day tail, forward
        assert filled[25] == pytest.approx(0.2)  # tomorrow repeats today's slot 1
        assert filled[27] == pytest.approx(0.4)  # ... and slot 3
        assert forecast_start == 4

    def test_a_covered_last_slot_reports_no_forecast_tail(self):
        values = [0.1] * 48
        coverage = [1.0] * 48

        filled, leading, forecast_start = fill_price_gaps(
            values, coverage, slots_per_day=24
        )

        assert filled == values
        assert leading == 0
        assert forecast_start is None

    def test_no_coverage_at_all_returns_empty(self):
        filled, leading, forecast_start = fill_price_gaps(
            [0.0] * 48, [0.0] * 48, slots_per_day=24
        )

        assert filled == []
        assert leading == 0
        assert forecast_start is None
