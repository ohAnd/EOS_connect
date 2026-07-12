"""
Test EVCC complete data handling — no cyclic padding when tomorrow's data is available.

When EVCC or grid providers have complete data for both today and tomorrow,
the conversion should use real data without applying cyclic padding.
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


class TestCompleteEVCCData:
    """Test that complete data uses real transformation, not cyclic padding."""

    def test_evcc_complete_48_hours_no_cyclic_padding(self, monkeypatch):
        """
        Test EVCC with complete data for today AND tomorrow (192 entries).

        Scenario: EPEX/grid provider has full 48-hour data from 00:00 today to 00:00 next day.
        EVCC returns 192 15-min prices (full 48 hours).
        
        Expected: Tomorrow's prices (slots 24-47) use REAL data, NOT cyclic pattern.
        Verify: Each tomorrow slot has unique real value based on its hour, not repeated from today.
        """
        iface = _make_price_interface(3600, monkeypatch)

        # Generate complete 48-hour dataset (192 15-min entries)
        # Create a distinct price pattern: low at midnight, high at noon, low again at midnight
        timeseries = []
        window_start = datetime(2026, 7, 8, 0, 0, 0, tzinfo=timezone.utc)
        
        for i in range(192):  # 192 * 15min = 48 hours = 2 days
            ts = window_start + timedelta(minutes=15 * i)
            ts_str = ts.replace(tzinfo=None).isoformat() + "Z"
            ts_end_str = (ts + timedelta(minutes=15)).replace(tzinfo=None).isoformat() + "Z"
            
            # Unique daily pattern that repeats but with different base per day
            hour_of_day = (i * 15 // 60) % 24  # Which hour within the day (0-23)
            day_num = i * 15 // (60 * 24)  # Which day (0 = today, 1 = tomorrow)
            
            # Price pattern: low 0.10 at midnight, high 0.20 at noon
            base_price = 0.10 if day_num == 0 else 0.12  # Different base per day
            if 8 <= hour_of_day <= 16:  # Midday peak
                price = base_price + 0.10
            else:
                price = base_price
            
            timeseries.append({
                "start": ts_str,
                "end": ts_end_str,
                "value": price,
            })

        result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, window_start
        )

        # Must return exactly 48 slots
        assert len(result) == 48

        # Extract values for analysis
        values = [r["value"] for r in result]
        
        # Today's slots (0-23)
        today_pattern = values[0:24]
        
        # Tomorrow's slots (24-47)
        tomorrow_pattern = values[24:48]
        
        # **CRITICAL**: Tomorrow's prices should NOT exactly repeat today's
        # Because the input data has different base prices per day (0.10 vs 0.12)
        # If cyclic padding was applied, all tomorrow values would equal today's
        # But with real data transformation, they should be different
        
        # Count midday peaks in each day
        today_high_count = sum(1 for p in today_pattern if p > 0.15)  # High = 0.20
        tomorrow_high_count = sum(1 for p in tomorrow_pattern if p > 0.16)  # High = 0.22
        
        # Both should have peaks, but at different absolute values
        assert today_high_count >= 8, f"Today should have ~9 high-price hours, got {today_high_count}"
        assert tomorrow_high_count >= 8, f"Tomorrow should have ~9 high-price hours, got {tomorrow_high_count}"
        
        # Tomorrow's midday value should be ~0.22 (0.12 + 0.10), NOT 0.20 (today's midday)
        # Verify by checking that tomorrow's high prices are higher than today's
        today_avg_high = sum(p for p in today_pattern if p > 0.15) / (today_high_count or 1)
        tomorrow_avg_high = sum(p for p in tomorrow_pattern if p > 0.16) / (tomorrow_high_count or 1)
        
        # Tomorrow's average high should be ~0.22, today's ~0.20
        assert tomorrow_avg_high > today_avg_high, (
            f"Tomorrow's high prices ({tomorrow_avg_high:.3f}) should be higher than "
            f"today's ({today_avg_high:.3f}) due to different base prices in input data"
        )
        
        # Explicitly verify that tomorrow is NOT cycled from today
        # If cyclic padding was wrong: tomorrow_pattern == today_pattern (would be True)
        assert tomorrow_pattern != today_pattern, (
            "ERROR: Tomorrow's prices exactly match today's — cyclic padding was incorrectly applied!"
        )

    def test_evcc_complete_data_vs_incomplete_data(self, monkeypatch):
        """
        Direct comparison: Complete data (192) vs Incomplete data (96).
        
        Complete: Real data for both days → different values per day
        Incomplete: Only today's data → tomorrow cycles today's pattern
        
        Verify the difference in behavior.
        """
        iface = _make_price_interface(3600, monkeypatch)
        window_start = datetime(2026, 7, 8, 0, 0, 0, tzinfo=timezone.utc)

        # === COMPLETE DATA (192 entries, 48 hours) ===
        complete_data = []
        for i in range(192):
            ts = window_start + timedelta(minutes=15 * i)
            ts_str = ts.replace(tzinfo=None).isoformat() + "Z"
            ts_end_str = (ts + timedelta(minutes=15)).replace(tzinfo=None).isoformat() + "Z"
            
            hour_of_day = (i * 15 // 60) % 24
            day_num = i * 15 // (60 * 24)
            
            base_price = 0.10 if day_num == 0 else 0.15  # Clearly different bases
            if 12 <= hour_of_day <= 14:  # Small peak window
                price = base_price + 0.05
            else:
                price = base_price
            
            complete_data.append({
                "start": ts_str,
                "end": ts_end_str,
                "value": price,
            })

        # === INCOMPLETE DATA (96 entries, 24 hours) ===
        incomplete_data = []
        for i in range(96):  # Only first 24 hours
            ts = window_start + timedelta(minutes=15 * i)
            ts_str = ts.replace(tzinfo=None).isoformat() + "Z"
            ts_end_str = (ts + timedelta(minutes=15)).replace(tzinfo=None).isoformat() + "Z"
            
            hour_of_day = (i * 15 // 60) % 24
            
            # Same base as complete data's first day
            base_price = 0.10
            if 12 <= hour_of_day <= 14:
                price = base_price + 0.05
            else:
                price = base_price
            
            incomplete_data.append({
                "start": ts_str,
                "end": ts_end_str,
                "value": price,
            })

        # Convert both
        complete_result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            complete_data, window_start
        )
        incomplete_result = iface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            incomplete_data, window_start
        )

        complete_values = [r["value"] for r in complete_result]
        incomplete_values = [r["value"] for r in incomplete_result]

        # Both should have 48 values
        assert len(complete_values) == 48
        assert len(incomplete_values) == 48

        # Today (slots 0-23) should be similar (same data)
        today_complete = complete_values[0:24]
        today_incomplete = incomplete_values[0:24]
        
        # They should match (or be very close)
        for i in range(24):
            assert today_complete[i] == pytest.approx(today_incomplete[i], abs=1e-6)

        # Tomorrow (slots 24-47) should DIFFER
        # Complete: Tomorrow uses day 2 base (0.15) → values around 0.15-0.20
        # Incomplete: Tomorrow cycles today (0.10 base) → values around 0.10-0.15
        
        tomorrow_complete = complete_values[24:48]
        tomorrow_incomplete = incomplete_values[24:48]

        complete_tomorrow_avg = sum(tomorrow_complete) / len(tomorrow_complete)
        incomplete_tomorrow_avg = sum(tomorrow_incomplete) / len(tomorrow_incomplete)

        # Complete tomorrow average should be higher (0.15 base vs 0.10 base)
        assert complete_tomorrow_avg > incomplete_tomorrow_avg, (
            f"Complete data tomorrow ({complete_tomorrow_avg:.3f}) should be higher than "
            f"incomplete data tomorrow ({incomplete_tomorrow_avg:.3f})"
        )

        # Verify the specific difference
        # Complete: mostly 0.15 with peaks at 0.20
        # Incomplete: cycles to 0.10 with peaks at 0.15 (cycled from today)
        assert complete_tomorrow_avg >= 0.14, "Complete tomorrow should average ~0.15+"
        assert incomplete_tomorrow_avg <= 0.11, "Incomplete tomorrow should average ~0.10 (cycled)"
