"""Tests for timestamp-aware 15-min to hourly price conversion.

This module tests the fix for Issue #267: "422 Unprocessable Content" errors
from EOS server when using EVCC as price source with 15-minute data.

The bug was in __convert_15min_to_hourly_price_timeseries() which did naive
index-based grouping (0-3, 4-7, ...) instead of timestamp-based hour slot
alignment. This caused misaligned prices that EOS server rejected.

The fix uses timestamps to determine which hour slot each 15-min price belongs
to, ensuring correct alignment to window boundaries.
"""

from datetime import datetime, timezone, timedelta
import pytest
from src.interfaces.price_interface import PriceInterface

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


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


class TestTimestampAware15minConversion:
    """Tests for timestamp-aware 15-min to hourly conversion (Issue #267 fix)."""

    def test_basic_15min_to_hourly_conversion(self, monkeypatch):
        """
        Test basic conversion: 4 consecutive 15-min prices → full 48-slot array.

        Verifies that:
        - 4 prices within hour 0 are correctly averaged into slot 0
        - All 48 slots are returned (gap-filled slots extend the last known price)
        - Slot 0 has correct averaged value and real timestamp
        """
        iface = _make_price_interface(3600, monkeypatch)

        # 4 prices for 1 hour starting at midnight
        timeseries = [
            {
                "start": "2026-07-08T00:00:00Z",
                "end": "2026-07-08T00:15:00Z",
                "value": 0.100,
            },
            {
                "start": "2026-07-08T00:15:00Z",
                "end": "2026-07-08T00:30:00Z",
                "value": 0.110,
            },
            {
                "start": "2026-07-08T00:30:00Z",
                "end": "2026-07-08T00:45:00Z",
                "value": 0.120,
            },
            {
                "start": "2026-07-08T00:45:00Z",
                "end": "2026-07-08T01:00:00Z",
                "value": 0.130,
            },
        ]

        start_time = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
        result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, start_time
        )

        # Always returns 48 slots (gap-filled with last known price)
        assert len(result) == 48
        # Slot 0 has averaged value and real timestamps
        assert result[0]["value"] == pytest.approx(0.115, abs=1e-6)  # (0.1+0.11+0.12+0.13)/4
        assert result[0]["start"] == "2026-07-08T00:00:00Z"
        assert result[0]["end"] == "2026-07-08T01:00:00Z"
        # Remaining slots are gap-filled with same value (last known = slot 0)
        for slot in result[1:]:
            assert slot["value"] == pytest.approx(0.115, abs=1e-6)

    def test_159_evcc_prices_to_48_hourly_slots(self, monkeypatch):
        """
        Test conversion of 159 EVCC 15-min prices to exactly 48 hourly slots.

        Real-world scenario from Issue #267: EVCC returns 159 prices in 15-min
        intervals starting around current time (rolling window). The method must
        produce exactly 48 hourly values aligned to the window, with gap-filling
        for past hours (before EVCC data starts) and future hours (beyond coverage).
        """
        iface = _make_price_interface(3600, monkeypatch)

        # Simulate EVCC's rolling window: starts at 09:00 (current hour),
        # covering ~39.75 hours forward (159 * 15min). The optimization
        # window starts at midnight (hour 0).
        base_time = datetime(2026, 7, 8, 9, 0, tzinfo=timezone.utc)  # EVCC starts at 09:00
        window_start = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)  # window from midnight

        timeseries = []
        for i in range(159):
            ts = base_time + timedelta(minutes=15 * i)
            ts_str = ts.replace(tzinfo=None).isoformat() + "Z"
            ts_end_str = (ts + timedelta(minutes=15)).replace(tzinfo=None).isoformat() + "Z"
            timeseries.append({
                "start": ts_str,
                "end": ts_end_str,
                "value": 0.100 + (i % 10) * 0.001,
            })

        result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, window_start
        )

        # Always exactly 48 slots
        assert len(result) == 48
        # All values are valid EUR/Wh prices
        for entry in result:
            assert isinstance(entry["value"], (int, float))
            assert 0 < entry["value"] < 1
        # Hours 0-8: gap-filled with first EVCC hourly average (average of hour 9 data)
        # Hour 9 has prices for i=0..3 → values 0.100, 0.101, 0.102, 0.103 → avg=0.1015
        expected_first_hourly = sum(0.100 + (i % 10) * 0.001 for i in range(4)) / 4
        for slot in result[0:9]:
            assert slot["value"] == pytest.approx(expected_first_hourly, abs=1e-6)

    def test_incomplete_hours_handled_gracefully(self, monkeypatch):
        """
        Test handling of incomplete hours (fewer than 4 prices in an hour).

        When data has gaps or incomplete data, prices should still average
        correctly using only available values. Always returns 48 slots.
        """
        iface = _make_price_interface(3600, monkeypatch)

        # Hour 0: 4 prices (complete)
        # Hour 1: 2 prices (incomplete, 01:30-02:00)
        # Hour 2: 3 prices (incomplete, 02:00-02:45)
        timeseries = [
            # Hour 0: complete
            {
                "start": "2026-07-08T00:00:00Z",
                "end": "2026-07-08T00:15:00Z",
                "value": 0.100,
            },
            {
                "start": "2026-07-08T00:15:00Z",
                "end": "2026-07-08T00:30:00Z",
                "value": 0.110,
            },
            {
                "start": "2026-07-08T00:30:00Z",
                "end": "2026-07-08T00:45:00Z",
                "value": 0.120,
            },
            {
                "start": "2026-07-08T00:45:00Z",
                "end": "2026-07-08T01:00:00Z",
                "value": 0.130,
            },
            # Hour 1: incomplete (gap from 01:00-01:30)
            {
                "start": "2026-07-08T01:30:00Z",
                "end": "2026-07-08T01:45:00Z",
                "value": 0.140,
            },
            {
                "start": "2026-07-08T01:45:00Z",
                "end": "2026-07-08T02:00:00Z",
                "value": 0.150,
            },
            # Hour 2: incomplete (only 45 min of data)
            {
                "start": "2026-07-08T02:00:00Z",
                "end": "2026-07-08T02:15:00Z",
                "value": 0.160,
            },
            {
                "start": "2026-07-08T02:15:00Z",
                "end": "2026-07-08T02:30:00Z",
                "value": 0.170,
            },
            {
                "start": "2026-07-08T02:30:00Z",
                "end": "2026-07-08T02:45:00Z",
                "value": 0.180,
            },
        ]

        start_time = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
        result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, start_time
        )

        # Always returns 48 slots
        assert len(result) == 48
        # Verify all 48 have a numeric value
        assert all(isinstance(e["value"], (int, float)) for e in result)

        # Access by slot index (0-based from window_start)
        # Slot 0: average of 4 = 0.115
        assert result[0]["value"] == pytest.approx(0.115, abs=1e-6)
        # Slot 1: average of 2 = 0.145
        assert result[1]["value"] == pytest.approx(0.145, abs=1e-6)
        # Slot 2: average of 3 = 0.170
        assert result[2]["value"] == pytest.approx(0.170, abs=1e-6)
        # Slots 3-23: same-day gap-filled with last known value (slot 2 = 0.170)
        for slot in result[3:24]:
            assert slot["value"] == pytest.approx(0.170, abs=1e-6)
        # Slots 24-47: next-day cyclic repeat of slots 0-23
        # Slot 24 = slot 0 (0.115), slot 25 = slot 1 (0.145), slot 26 = slot 2 (0.170)
        assert result[24]["value"] == pytest.approx(0.115, abs=1e-6)  # cycle slot 0
        assert result[25]["value"] == pytest.approx(0.145, abs=1e-6)  # cycle slot 1
        assert result[26]["value"] == pytest.approx(0.170, abs=1e-6)  # cycle slot 2
        # Slots 27-47 cycle through slots 3-23 (all 0.170)
        for slot in result[27:]:
            assert slot["value"] == pytest.approx(0.170, abs=1e-6)

    def test_skipped_hours_create_gaps(self, monkeypatch):
        """
        Test that hours with no data are gap-filled with adjacent price.

        If hour 1 has no data (gap between hours 0 and 2), it is filled with
        the last known price before the gap (hour 0's value). Always 48 slots.
        """
        iface = _make_price_interface(3600, monkeypatch)

        timeseries = [
            # Hour 0
            {
                "start": "2026-07-08T00:00:00Z",
                "end": "2026-07-08T00:15:00Z",
                "value": 0.100,
            },
            {
                "start": "2026-07-08T00:15:00Z",
                "end": "2026-07-08T00:30:00Z",
                "value": 0.110,
            },
            {
                "start": "2026-07-08T00:30:00Z",
                "end": "2026-07-08T00:45:00Z",
                "value": 0.120,
            },
            {
                "start": "2026-07-08T00:45:00Z",
                "end": "2026-07-08T01:00:00Z",
                "value": 0.130,
            },
            # SKIP hour 1 entirely
            # Hour 2 starts here
            {
                "start": "2026-07-08T02:00:00Z",
                "end": "2026-07-08T02:15:00Z",
                "value": 0.140,
            },
            {
                "start": "2026-07-08T02:15:00Z",
                "end": "2026-07-08T02:30:00Z",
                "value": 0.150,
            },
            {
                "start": "2026-07-08T02:30:00Z",
                "end": "2026-07-08T02:45:00Z",
                "value": 0.160,
            },
            {
                "start": "2026-07-08T02:45:00Z",
                "end": "2026-07-08T03:00:00Z",
                "value": 0.170,
            },
        ]

        start_time = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
        result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, start_time
        )

        # Always returns 48 slots
        assert len(result) == 48
        # Slot 0: hour 0, average 0.115
        assert result[0]["value"] == pytest.approx(0.115, abs=1e-6)
        # Slot 1: inner gap — forward-filled with last known before gap = slot 0 value = 0.115
        assert result[1]["value"] == pytest.approx(0.115, abs=1e-6)
        # Slot 2: hour 2, average = (0.140+0.150+0.160+0.170)/4 = 0.155
        assert result[2]["value"] == pytest.approx(0.155, abs=1e-6)

    def test_midnight_boundary_crossing(self, monkeypatch):
        """
        Test correct slot assignment when data starts before window.

        The 23:45 price is before window start so it is skipped.
        Data starting at 00:00 falls in slot 0. Always 48 slots returned.
        """
        iface = _make_price_interface(3600, monkeypatch)

        timeseries = [
            {
                "start": "2026-07-07T23:45:00Z",
                "end": "2026-07-08T00:00:00Z",
                "value": 0.100,  # BEFORE window - must be ignored
            },
            {
                "start": "2026-07-08T00:00:00Z",
                "end": "2026-07-08T00:15:00Z",
                "value": 0.110,
            },
            {
                "start": "2026-07-08T00:15:00Z",
                "end": "2026-07-08T00:30:00Z",
                "value": 0.120,
            },
            {
                "start": "2026-07-08T00:30:00Z",
                "end": "2026-07-08T00:45:00Z",
                "value": 0.130,
            },
            {
                "start": "2026-07-08T00:45:00Z",
                "end": "2026-07-08T01:00:00Z",
                "value": 0.140,
            },
        ]

        start_time = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
        result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, start_time
        )

        # Always 48 slots
        assert len(result) == 48
        # Slot 0: 4 prices from 00:00-01:00, average = (0.110+0.120+0.130+0.140)/4 = 0.125
        assert result[0]["value"] == pytest.approx(0.125, abs=1e-6)
        # The 23:45 price (0.100) must NOT influence slot 0
        assert result[0]["value"] != pytest.approx(0.100, abs=1e-6)
        # Slots 1-47: gap-filled with last known = 0.125
        for slot in result[1:]:
            assert slot["value"] == pytest.approx(0.125, abs=1e-6)

    def test_very_small_dataset(self, monkeypatch):
        """Test conversion with only 2 prices (less than 1 hour of data). Always 48 slots."""
        iface = _make_price_interface(3600, monkeypatch)

        timeseries = [
            {
                "start": "2026-07-08T00:00:00Z",
                "end": "2026-07-08T00:15:00Z",
                "value": 0.100,
            },
            {
                "start": "2026-07-08T00:15:00Z",
                "end": "2026-07-08T00:30:00Z",
                "value": 0.110,
            },
        ]

        start_time = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
        result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, start_time
        )

        # Always returns 48 slots
        assert len(result) == 48
        # Slot 0: average of 2 prices = 0.105
        assert result[0]["value"] == pytest.approx(0.105, abs=1e-6)
        # Slots 1-47: gap-filled with last known = 0.105
        for slot in result[1:]:
            assert slot["value"] == pytest.approx(0.105, abs=1e-6)
