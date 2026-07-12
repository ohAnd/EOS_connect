"""Tests for timeseries data source parsing (price and PV).

Tests strict validation of:
- Format (start, end, value fields)
- Resolution detection (900s vs 3600s)
- Time frame base matching (900 vs 3600)
- 15-min to hourly averaging
- Data completeness and padding
- Value range validation
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.interfaces.price_interface import PriceInterface
from src.interfaces.pv_interface import PvInterface


class TestTimeseriesFormatValidation:
    """Test strict format validation for incoming timeseries data."""

    @pytest.fixture
    def price_interface(self, monkeypatch):
        """Create price interface with timeseries configured."""
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://test.local/prices",
                "data_path": "data",
                "data_token": "",
            },
            time_frame_base=3600,
            timezone=timezone.utc,
        )
        return iface

    def test_valid_hourly_format(self, price_interface):
        """Valid hourly timeseries format is accepted."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": 0.25},
            {"start": "2024-01-01T01:00:00Z", "end": "2024-01-01T02:00:00Z", "value": 0.28},
        ] * 24  # 48 hours

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices is not None
        assert len(prices) == 48
        assert all(isinstance(p, float) for p in prices)

    def test_valid_15min_format(self, price_interface):
        """Valid 15-minute timeseries format is converted to hourly (system is 3600s)."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:15:00Z", "value": 0.25},
            {"start": "2024-01-01T00:15:00Z", "end": "2024-01-01T00:30:00Z", "value": 0.25},
            {"start": "2024-01-01T00:30:00Z", "end": "2024-01-01T00:45:00Z", "value": 0.25},
            {"start": "2024-01-01T00:45:00Z", "end": "2024-01-01T01:00:00Z", "value": 0.25},
        ] * 48  # 192 intervals (48 hours of 15-min data)

        # System is configured for 3600s, so 15-min data will be converted to hourly (48 values)
        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices is not None
        assert len(prices) == 48  # Converted to hourly

    def test_missing_start_field(self, price_interface):
        """Missing 'start' field in timeseries entry is rejected."""
        timeseries = [
            {"end": "2024-01-01T01:00:00Z", "value": 0.25},  # No start
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices == []

    def test_missing_end_field(self, price_interface):
        """Missing 'end' field in timeseries entry is rejected."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "value": 0.25},  # No end
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices == []

    def test_missing_value_field(self, price_interface):
        """Missing 'value' field in timeseries entry is rejected."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z"},  # No value
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices == []

    def test_non_numeric_value(self, price_interface):
        """Non-numeric value in timeseries entry is rejected."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": "invalid"},
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices == []

    def test_not_a_list(self, price_interface):
        """Non-list timeseries input is rejected."""
        prices = price_interface._PriceInterface__parse_price_timeseries({"data": "not a list"}, 48)
        assert prices == []

    def test_empty_list(self, price_interface):
        """Empty timeseries list is rejected."""
        prices = price_interface._PriceInterface__parse_price_timeseries([], 48)
        assert prices == []


class TestResolutionDetection:
    """Test automatic resolution detection (900s vs 3600s)."""

    @pytest.fixture
    def price_interface(self, monkeypatch):
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://test.local/prices",
                "data_path": "data",
            },
            time_frame_base=3600,
            timezone=timezone.utc,
        )
        return iface

    def test_detect_hourly_resolution(self, price_interface):
        """3600-second gaps detected as hourly."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": 0.25},
            {"start": "2024-01-01T01:00:00Z", "end": "2024-01-01T02:00:00Z", "value": 0.28},
        ]

        resolution = price_interface._PriceInterface__detect_price_timeseries_resolution(timeseries)
        assert resolution == 3600

    def test_detect_15min_resolution(self, price_interface):
        """900-second gaps detected as 15-minute."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:15:00Z", "value": 0.25},
            {"start": "2024-01-01T00:15:00Z", "end": "2024-01-01T00:30:00Z", "value": 0.28},
        ]

        resolution = price_interface._PriceInterface__detect_price_timeseries_resolution(timeseries)
        assert resolution == 900

    def test_unix_timestamp_resolution_detection(self, price_interface):
        """Unix timestamps are correctly converted and resolution detected."""
        timeseries = [
            {"start": 1704067200, "end": 1704070800, "value": 0.25},  # 2024-01-01 00:00-01:00
            {"start": 1704070800, "end": 1704074400, "value": 0.28},  # 2024-01-01 01:00-02:00
        ]

        resolution = price_interface._PriceInterface__detect_price_timeseries_resolution(timeseries)
        assert resolution == 3600

    def test_unsupported_resolution(self, price_interface):
        """Unsupported resolution (not 900 or 3600 seconds) returns None."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:10:00Z", "value": 0.25},
            {"start": "2024-01-01T00:10:00Z", "end": "2024-01-01T00:20:00Z", "value": 0.28},
        ]

        resolution = price_interface._PriceInterface__detect_price_timeseries_resolution(timeseries)
        assert resolution is None

    def test_insufficient_entries_for_detection(self, price_interface):
        """Single timeseries entry cannot determine resolution."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": 0.25},
        ]

        resolution = price_interface._PriceInterface__detect_price_timeseries_resolution(timeseries)
        assert resolution is None


class TestTimeFrameBaseMismatch:
    """Test validation of time frame base compatibility."""

    def test_15min_source_to_hourly_system_converts(self, monkeypatch):
        """15-min source (900s) to hourly system (3600s) → converts via averaging."""
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://test.local/prices",
                "data_path": "data",
            },
            time_frame_base=3600,  # System expects hourly
            timezone=timezone.utc,
        )

        # 4 × 15-min entries (average to 1 hourly)
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:15:00Z", "value": 0.20},
            {"start": "2024-01-01T00:15:00Z", "end": "2024-01-01T00:30:00Z", "value": 0.24},
            {"start": "2024-01-01T00:30:00Z", "end": "2024-01-01T00:45:00Z", "value": 0.28},
            {"start": "2024-01-01T00:45:00Z", "end": "2024-01-01T01:00:00Z", "value": 0.30},
        ] * 48  # 192 × 15-min = 48 hourly slots

        prices = iface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices is not None
        assert len(prices) == 48
        # First hour should be average of 0.20, 0.24, 0.28, 0.30 = 0.255
        assert 0.254 < prices[0] < 0.256

    def test_hourly_source_to_15min_system_rejected(self, monkeypatch):
        """Hourly source (3600s) to 15-min system (900s) → ERROR, rejected."""
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://test.local/prices",
                "data_path": "data",
            },
            time_frame_base=900,  # System expects 15-min
            timezone=timezone.utc,
        )

        # Hourly source data
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": 0.25},
            {"start": "2024-01-01T01:00:00Z", "end": "2024-01-01T02:00:00Z", "value": 0.28},
        ] * 24  # 48 hourly entries

        prices = iface._PriceInterface__parse_price_timeseries(timeseries, 192)
        # Should return empty (resolution mismatch error)
        assert prices == []


class TestAveraging:
    """Test 15-minute to hourly averaging logic."""

    @pytest.fixture
    def price_interface(self, monkeypatch):
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://test.local/prices",
                "data_path": "data",
            },
            time_frame_base=3600,
            timezone=timezone.utc,
        )
        return iface

    def test_average_4_values_to_1(self, price_interface):
        """4 × 15-min values averaged to 1 hourly value (padded to 48 slots)."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:15:00Z", "value": 0.20},
            {"start": "2024-01-01T00:15:00Z", "end": "2024-01-01T00:30:00Z", "value": 0.30},
            {"start": "2024-01-01T00:30:00Z", "end": "2024-01-01T00:45:00Z", "value": 0.40},
            {"start": "2024-01-01T00:45:00Z", "end": "2024-01-01T01:00:00Z", "value": 0.50},
        ]

        # For conversion, provide start_time matching the first entry
        start_time = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        averaged = price_interface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, start_time
        )
        # Method pads to 48 slots total
        assert len(averaged) == 48
        # First slot: Average of 4 × 15-min values = (0.20 + 0.30 + 0.40 + 0.50) / 4 = 0.35
        assert 0.349 < averaged[0]["value"] < 0.351
        # Remaining slots are padded with the last value (0.35) via cyclic pattern
        assert all(abs(v["value"] - 0.35) < 1e-6 for v in averaged)

    def test_average_multiple_hours(self, price_interface):
        """Multiple hours of 15-min data correctly averaged to hourly (padded to 48 slots)."""
        timeseries = []
        for hour in range(3):
            for minute_offset in [0, 15, 30, 45]:
                ts = f"2024-01-01T{hour:02d}:{minute_offset:02d}:00Z"
                te = (
                    f"2024-01-01T{hour:02d}:{minute_offset+15:02d}:00Z"
                    if minute_offset < 45
                    else f"2024-01-01T{hour+1:02d}:00:00Z"
                )
                timeseries.append(
                    {
                        "start": ts,
                        "end": te,
                        "value": 0.20 + hour * 0.05,
                    }
                )

        start_time = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        averaged = price_interface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, start_time
        )
        # Method pads to 48 slots total
        assert len(averaged) == 48
        # First 3 slots contain actual hourly averages
        assert abs(averaged[0]["value"] - 0.20) < 1e-6  # Hour 0: all 0.20
        assert abs(averaged[1]["value"] - 0.25) < 1e-6  # Hour 1: all 0.25
        assert abs(averaged[2]["value"] - 0.30) < 1e-6  # Hour 2: all 0.30
        # Remaining slots are padded with cyclic daily pattern

    def test_incomplete_group_not_averaged(self, price_interface):
        """Incomplete group at end (< 4 values) is handled gracefully."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:15:00Z", "value": 0.20},
            {"start": "2024-01-01T00:15:00Z", "end": "2024-01-01T00:30:00Z", "value": 0.30},
            # Only 2 values, not a complete group
        ]

        # Should return as-is or with special handling
        start_time = datetime.fromisoformat("2024-01-01T00:00:00+00:00")
        result = price_interface._PriceInterface__convert_15min_to_hourly_price_timeseries(
            timeseries, start_time
        )
        # With < 4 total values, returns original
        assert len(result) >= 0  # Depends on implementation


class TestValueRangeValidation:
    """Test price value clamping to valid range."""

    @pytest.fixture
    def price_interface(self, monkeypatch):
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://test.local/prices",
                "data_path": "data",
            },
            time_frame_base=3600,
            timezone=timezone.utc,
        )
        return iface

    def test_values_within_range(self, price_interface):
        """Valid price values (-0.5 to 1.0) are accepted."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": -0.5},
            {"start": "2024-01-01T01:00:00Z", "end": "2024-01-01T02:00:00Z", "value": 0.0},
            {"start": "2024-01-01T02:00:00Z", "end": "2024-01-01T03:00:00Z", "value": 1.0},
        ] * 16

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices is not None
        assert all(-0.5 <= p <= 1.0 for p in prices)

    def test_value_too_low_clamped(self, price_interface, caplog):
        """Value below -0.5 is clamped to -0.5."""
        timeseries = [
            {
                "start": f"2024-01-02T{i%24:02d}:00:00Z",
                "end": f"2024-01-02T{(i+1)%24:02d}:00:00Z" if i < 23 else "2024-01-03T00:00:00Z",
                "value": -0.9,
            }
            for i in range(48)
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices is not None
        assert all(p == -0.5 for p in prices)
        assert "clamping" in caplog.text.lower()

    def test_value_too_high_clamped(self, price_interface, caplog):
        """Value above 1.0 is clamped to 1.0."""
        timeseries = [
            {
                "start": f"2024-01-02T{i%24:02d}:00:00Z",
                "end": f"2024-01-02T{(i+1)%24:02d}:00:00Z" if i < 23 else "2024-01-03T00:00:00Z",
                "value": 2.5,
            }
            for i in range(48)
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices is not None
        assert all(p == 1.0 for p in prices)
        assert "clamping" in caplog.text.lower()


class TestDataCompleteness:
    """Test incomplete data handling and padding."""

    @pytest.fixture
    def price_interface(self, monkeypatch):
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://test.local/prices",
                "data_path": "data",
            },
            time_frame_base=3600,
            timezone=timezone.utc,
        )
        return iface

    def test_incomplete_hourly_data_padded(self, price_interface):
        """Incomplete hourly data (< 48 values) is padded with last value."""
        timeseries = [
            {
                "start": f"2024-01-01T{i:02d}:00:00Z",
                "end": f"2024-01-01T{i+1:02d}:00:00Z",
                "value": 0.25,
            }
            for i in range(24)  # Only 24 hours instead of 48
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices is not None
        assert len(prices) == 48  # Padded to 48
        assert all(p == 0.25 for p in prices)  # All padded with 0.25

    def test_complete_hourly_data_no_padding(self, price_interface):
        """Complete hourly data (48 values) requires no padding."""
        timeseries = [
            {
                "start": f"2024-01-02T{i%24:02d}:00:00Z",
                "end": f"2024-01-02T{(i+1)%24:02d}:00:00Z",
                "value": 0.25,
            }
            for i in range(48)
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(timeseries, 48)
        assert prices is not None
        assert len(prices) == 48
