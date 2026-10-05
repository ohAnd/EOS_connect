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
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytz

from src.interfaces.price_interface import PriceInterface
from src.interfaces.pv_interface import PvInterface
from src.interfaces.timeseries_normalizer import (
    detect_resolution_seconds,
    normalize_entries,
)

# The fixtures are pinned to this date; placement is timestamp-based, so the
# parse calls pass it as the window anchor.
WINDOW = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)


def hourly(hours, value=0.25, base=None):
    """Distinct hourly entries — duplicates are rejected by the normalizer now."""
    start = base or WINDOW
    return [
        {
            "start": (start + timedelta(hours=h)).isoformat(),
            "end": (start + timedelta(hours=h + 1)).isoformat(),
            "value": value(h) if callable(value) else value,
        }
        for h in range(hours)
    ]


def quarter_hourly(count, value=0.25, base=None):
    """Distinct 15-minute entries."""
    start = base or WINDOW
    return [
        {
            "start": (start + timedelta(minutes=15 * i)).isoformat(),
            "end": (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": value(i) if callable(value) else value,
        }
        for i in range(count)
    ]


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
        timeseries = hourly(48)

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert prices is not None
        assert len(prices) == 48
        assert all(isinstance(p, float) for p in prices)

    def test_valid_15min_format(self, price_interface):
        """Valid 15-minute timeseries format is converted to hourly (system is 3600s)."""
        timeseries = quarter_hourly(192)

        # System is configured for 3600s, so 15-min data lands on hourly slots
        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
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
        entries = normalize_entries(hourly(2), pytz.UTC)
        assert detect_resolution_seconds(entries) == 3600

    def test_detect_15min_resolution(self, price_interface):
        """900-second gaps detected as 15-minute."""
        entries = normalize_entries(quarter_hourly(2), pytz.UTC)
        assert detect_resolution_seconds(entries) == 900

    def test_unix_timestamp_resolution_detection(self, price_interface):
        """Unix timestamps are correctly converted and resolution detected."""
        timeseries = [
            {"start": 1704067200, "end": 1704070800, "value": 0.25},  # 2024-01-01 00:00-01:00
            {"start": 1704070800, "end": 1704074400, "value": 0.28},  # 2024-01-01 01:00-02:00
        ]
        entries = normalize_entries(timeseries, pytz.UTC)
        assert detect_resolution_seconds(entries) == 3600

    def test_unsupported_resolution(self, price_interface):
        """Unsupported resolution (not 900 or 3600 seconds) returns None."""
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:10:00Z", "value": 0.25},
            {"start": "2024-01-01T00:10:00Z", "end": "2024-01-01T00:20:00Z", "value": 0.28},
        ]
        entries = normalize_entries(timeseries, pytz.UTC)
        assert detect_resolution_seconds(entries) is None

    def test_insufficient_entries_for_detection(self, price_interface):
        """Single timeseries entry cannot determine resolution."""
        entries = normalize_entries(
            [{"start": "2024-01-01T00:00:00Z", "value": 0.25}], pytz.UTC
        )
        assert detect_resolution_seconds(entries) is None


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

        # 192 × 15-min entries; the first hour repeats the quartet to average
        quartet = [0.20, 0.24, 0.28, 0.30]
        timeseries = quarter_hourly(192, value=lambda i: quartet[i % 4])

        prices = iface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
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
        timeseries = hourly(48)

        prices = iface._PriceInterface__parse_price_timeseries(
            timeseries, 192, start_time=WINDOW
        )
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
        """4 × 15-min values averaged to 1 hourly value (filled to 48 slots)."""
        quartet = [0.20, 0.30, 0.40, 0.50]
        timeseries = quarter_hourly(4, value=lambda i: quartet[i])

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert len(prices) == 48
        # First slot: Average of 4 × 15-min values = (0.20 + 0.30 + 0.40 + 0.50) / 4 = 0.35
        assert 0.349 < prices[0] < 0.351
        # Remaining slots carry the value on (forward fill + daily pattern)
        assert all(abs(value - 0.35) < 1e-6 for value in prices)

    def test_average_multiple_hours(self, price_interface):
        """Multiple hours of 15-min data correctly averaged per hour."""
        timeseries = quarter_hourly(12, value=lambda i: 0.20 + (i // 4) * 0.05)

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert len(prices) == 48
        assert abs(prices[0] - 0.20) < 1e-6  # Hour 0: all 0.20
        assert abs(prices[1] - 0.25) < 1e-6  # Hour 1: all 0.25
        assert abs(prices[2] - 0.30) < 1e-6  # Hour 2: all 0.30

    def test_incomplete_group_averages_what_is_present(self, price_interface):
        """A partially covered hour averages its two quarters, not a guess."""
        quartet = [0.20, 0.30]
        timeseries = quarter_hourly(2, value=lambda i: quartet[i])

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert len(prices) == 48
        assert abs(prices[0] - 0.25) < 1e-6


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
        band = [-0.5, 0.0, 1.0]
        timeseries = hourly(48, value=lambda h: band[h % 3])

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert len(prices) == 48
        assert all(-0.5 <= p <= 1.0 for p in prices)

    def test_value_too_low_clamped(self, price_interface, caplog):
        """Value below -0.5 is clamped to -0.5."""
        timeseries = hourly(48, value=-0.9)

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert len(prices) == 48
        assert all(p == -0.5 for p in prices)
        assert "clamping" in caplog.text.lower()

    def test_value_too_high_clamped(self, price_interface, caplog):
        """Value above 1.0 is clamped to 1.0."""
        timeseries = hourly(48, value=2.5)

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert len(prices) == 48
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

    def test_incomplete_hourly_data_filled(self, price_interface):
        """Incomplete hourly data (< 48 values) is filled to the horizon."""
        timeseries = hourly(24)  # Only 24 hours instead of 48

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert prices is not None
        assert len(prices) == 48  # Filled to 48
        assert all(p == 0.25 for p in prices)
        # ...and the invented tail is reported to the UI.
        assert price_interface.get_forecast_metadata()["forecast_start_index"] == 24

    def test_complete_hourly_data_no_padding(self, price_interface):
        """Complete hourly data (48 values) requires no filling."""
        timeseries = hourly(48)

        prices = price_interface._PriceInterface__parse_price_timeseries(
            timeseries, 48, start_time=WINDOW
        )
        assert prices is not None
        assert len(prices) == 48
        assert price_interface.get_forecast_metadata()["forecast_start_index"] is None
