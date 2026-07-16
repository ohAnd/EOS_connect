"""
Test EVCC incomplete data handling — right-side (future) cyclic padding.

When EPEX or grid price providers don't have data for tomorrow, EVCC
returns incomplete data ending today. The conversion should repeat today's
price pattern for tomorrow instead of just forward-filling the last value.
"""

import pytest
from datetime import datetime, timezone, timedelta
from src.interfaces.price_interface import PriceInterface

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


def _make_price_interface(time_frame_base, monkeypatch):
    """Helper to create price interface with mocked time."""
    monkeypatch.setattr(
        "src.interfaces.price_interface.PriceInterface."
        "_PriceInterface__start_update_service",
        lambda self: None,
    )
    cfg = {"source": "fixed", "fixed_price_eur_per_mwh": 30}
    return PriceInterface(cfg, time_frame_base, timezone=timezone.utc)


class TestEVCCIncompleteDataCyclicPadding:
    """Test cyclic daily pattern padding for incomplete EVCC data."""

    def test_evcc_stops_at_23_repeats_daily_pattern_for_tomorrow(self, monkeypatch):
        """
        Test EVCC data that ends at 23:00 today (no tomorrow data available).

        When incomplete, slots 24-47 (tomorrow) should cycle through the pattern
        of slots 0-23 (today) for better forecasting than just forward-filling.
        """
        iface = _make_price_interface(3600, monkeypatch)

        # Simulate EVCC data: 14 hourly prices from hour 9 to hour 22 (09:00-23:00 today)
        # This is after converting 15-min data to hourly
        timeseries = []
        base_time = datetime(2026, 7, 8, 7, 0, 0, tzinfo=timezone.utc)  # 09:00 CEST = 07:00 UTC
        
        # 56 15-min entries = 14 hours of data
        for i in range(56):
            ts = base_time + timedelta(minutes=15 * i)
            ts_str = ts.replace(tzinfo=None).isoformat() + "Z"
            ts_end_str = (ts + timedelta(minutes=15)).replace(tzinfo=None).isoformat() + "Z"
            
            # Simple pattern: price varies by hour-of-day
            hour_utc = (7 + i * 15 // 60) % 24
            price = 0.150 if 10 <= hour_utc <= 15 else 0.100
            
            timeseries.append({
                "start": ts_str,
                "end": ts_end_str,
                "value": price,
            })

        # Window starts at Berlin midnight (which is UTC 22:00 previous day)
        window_start = datetime(2026, 7, 7, 22, 0, 0, tzinfo=timezone.utc)
        
        result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, window_start
        )

        # Must return exactly 48 slots
        assert len(result) == 48
        
        # Extract just the values for pattern matching
        values = [r["value"] for r in result]
        
        # Today's slots (0-23) should have the data pattern
        today_pattern = values[0:24]
        
        # Tomorrow's slots (24-47) should cycle through today's pattern
        tomorrow_pattern = values[24:48]
        
        # Verify tomorrow cycles today
        for tomorrow_idx, today_idx in enumerate(range(0, 24)):
            assert tomorrow_pattern[tomorrow_idx] == pytest.approx(
                today_pattern[today_idx], abs=1e-6
            ), f"Slot {24+tomorrow_idx} should repeat slot {today_idx}"
