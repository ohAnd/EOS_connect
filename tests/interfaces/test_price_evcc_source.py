"""Tests for EVCC price source integration."""

from datetime import datetime, timezone, timedelta
from unittest.mock import Mock

import pytest

from src.interfaces.price_interface import PriceInterface

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


class TestEvccPriceSource:
    """Tests for EVCC as a price source."""

    def test_evcc_interface_required(self, monkeypatch):
        """Test that EVCC interface parameter is accepted and stored."""
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        assert price_iface.evcc_interface == mock_evcc
        assert price_iface.src == "evcc"

    def test_evcc_not_configured_graceful(self, monkeypatch):
        """Test graceful handling when EVCC interface is not configured."""
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=None,
        )
        
        # update_prices should handle None evcc_interface gracefully
        price_iface.update_prices(24)
        # Should use default prices
        prices = price_iface.get_current_prices()
        assert len(prices) > 0
        # First price should be default (0.0001)
        assert prices[0] == 0.0001

    def test_evcc_tariff_parsing_hourly(self, monkeypatch):
        """Test parsing EVCC rate response in EUR/kWh to EUR/Wh format."""
        # Mock EVCC API response - provide 48 hours of 15-minute intervals (192 rates)
        rates_data = []
        for hour in range(48):
            for interval in range(4):  # 4x 15-minute intervals per hour
                rates_data.append({
                    "start": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(hours=hour, minutes=15*interval)).isoformat(),
                    "end": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(hours=hour, minutes=15*(interval+1))).isoformat(),
                    "value": 0.120 + (hour % 24) * 0.001
                })
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            assert "/api/tariff/grid" in url
            return R()
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        price_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = price_iface.get_current_prices()
        
        # Verify we got prices (converted from EUR/kWh to EUR/Wh)
        assert len(prices) > 0
        # First price should be close to 0.120 / 1000 = 0.00012
        assert prices[0] == pytest.approx(0.120 / 1000, rel=1e-6)

    def test_evcc_tariff_partial_data(self, monkeypatch):
        """Test EVCC with partial data (less than requested duration)."""
        evcc_response = {
            "rates": [
                {"start": "2025-10-20T00:00:00Z", "end": "2025-10-20T00:15:00Z", "value": 0.125},
                {"start": "2025-10-20T00:15:00Z", "end": "2025-10-20T00:30:00Z", "value": 0.130},
                {"start": "2025-10-20T00:30:00Z", "end": "2025-10-20T00:45:00Z", "value": 0.135},
                {"start": "2025-10-20T00:45:00Z", "end": "2025-10-20T01:00:00Z", "value": 0.140},
            ]
        }
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            return R()
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        # Request 48 hours but only 4x15-min = 1 hour provided
        price_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = price_iface.get_current_prices()
        
        # Should handle gracefully and have prices
        assert len(prices) > 0
        # First price should be from EVCC data (converted from EUR/kWh to EUR/Wh)
        # The exact value depends on timeseries averaging, just verify it's in the right range
        assert 0.00012 < prices[0] < 0.00014

    def test_evcc_api_failure_graceful(self, monkeypatch):
        """Test graceful handling when EVCC API call fails."""
        def fake_get(url, headers=None, timeout=None):
            raise ConnectionError("EVCC server unreachable")
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        # Should not raise, should fall back gracefully
        price_iface.update_prices(24)
        prices = price_iface.get_current_prices()
        # Should use default prices
        assert len(prices) > 0

    def test_evcc_invalid_response_format(self, monkeypatch):
        """Test handling of invalid EVCC response format."""
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {"invalid": "format"}  # No rates key
            return R()
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        # Should handle gracefully without crashing
        price_iface.update_prices(24)
        prices = price_iface.get_current_prices()
        assert isinstance(prices, list)

    def test_evcc_missing_fields_in_tariff(self, monkeypatch):
        """Test handling rates with missing start or value fields."""
        evcc_response = {
            "rates": [
                {"start": "2025-10-20T00:00:00Z", "end": "2025-10-20T00:15:00Z", "value": 0.125},
                {"start": "2025-10-20T00:15:00Z", "end": "2025-10-20T00:30:00Z"},  # Missing value
                {"end": "2025-10-20T00:45:00Z", "value": 0.130},  # Missing start
                {"start": "2025-10-20T00:30:00Z", "end": "2025-10-20T00:45:00Z", "value": 0.135},
                {"start": "2025-10-20T00:45:00Z", "end": "2025-10-20T01:00:00Z", "value": 0.140},
            ]
        }
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            return R()
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        price_iface.update_prices(8, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = price_iface.get_current_prices()
        
        # Should skip invalid entries and use only valid ones
        assert len(prices) >= 3  # At least 3 valid rates

    def test_evcc_empty_tariffs(self, monkeypatch):
        """Test handling empty rates array from EVCC."""
        evcc_response = {"rates": []}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            return R()
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        price_iface.update_prices(24)
        prices = price_iface.get_current_prices()
        # Should fall back gracefully
        assert isinstance(prices, list)

    def test_evcc_incomplete_with_energyforecast_fallback(self, monkeypatch):
        """Test EVCC fallback to energyforecast when data is incomplete."""
        # Mock EVCC returning only 24 hours (half the data)
        rates_data = []
        for i in range(96):  # 96 slots = 24 hours of 15-min intervals
            rates_data.append({
                "start": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(minutes=15*i)).isoformat(),
                "end": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(minutes=15*(i+1))).isoformat(),
                "value": 0.120 + (i % 24) * 0.001
            })
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    if "/api/tariff/grid" in url:
                        return evcc_response
                    return {"error": "unknown"}
            return R()
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        # Mock the energyforecast fallback method to return forecast prices
        forecast_prices = [0.125 + (i % 24) * 0.0005 for i in range(24)]
        monkeypatch.setattr(
            price_iface,
            "_fetch_adaptive_energyforecast_fallback",
            lambda known_prices, num_missing_hours: forecast_prices if num_missing_hours == 24 else []
        )
        
        price_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = price_iface.get_current_prices()
        
        # Should have complete 48 hours despite incomplete EVCC data
        assert len(prices) == 48
        # Forecast metadata should be set
        assert price_iface.forecast_start_index == 24
        assert price_iface.forecast_type == "smart_forecast"
        assert price_iface.forecast_source == "energyforecast.de"

    def test_evcc_incomplete_with_yesterday_fallback(self, monkeypatch):
        """Test EVCC fallback to yesterday's prices when energyforecast unavailable."""
        # Mock EVCC returning only 24 hours (half the data)
        rates_data = []
        for i in range(96):  # 96 slots = 24 hours of 15-min intervals
            rates_data.append({
                "start": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(minutes=15*i)).isoformat(),
                "end": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(minutes=15*(i+1))).isoformat(),
                "value": 0.120 + (i % 24) * 0.001
            })
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    if "/api/tariff/grid" in url:
                        return evcc_response
                    # Energyforecast returns empty (unavailable)
                    return []
            return R()
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        # Pre-populate yesterday's prices
        yesterday_prices = [0.11 + (i % 24) * 0.001 for i in range(48)]
        price_iface.last_successful_prices = yesterday_prices.copy()
        
        price_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = price_iface.get_current_prices()
        
        # Should have complete 48 hours with yesterday's prices filling the gap
        assert len(prices) == 48
        # First 24 hours from EVCC, last 24 from yesterday
        assert prices[24] == pytest.approx(yesterday_prices[24])
        # Forecast metadata should be set to fallback_history
        assert price_iface.forecast_start_index == 24
        assert price_iface.forecast_type == "fallback_history"
        assert price_iface.forecast_source == "yesterday_prices"

    def test_evcc_incomplete_with_last_value_fallback(self, monkeypatch):
        """Test EVCC fallback to today's prices repetition when no better fallback available."""
        # Mock EVCC returning only 24 hours (half the data)
        rates_data = []
        for i in range(96):  # 96 slots = 24 hours of 15-min intervals
            rates_data.append({
                "start": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(minutes=15*i)).isoformat(),
                "end": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(minutes=15*(i+1))).isoformat(),
                "value": 0.120 + (i % 24) * 0.001
            })
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    if "/api/tariff/grid" in url:
                        return evcc_response
                    # Energyforecast returns empty (unavailable)
                    return []
            return R()
        
        monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            PriceInterface, "_PriceInterface__start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        price_iface = PriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        # No yesterday's prices available (first run scenario)
        price_iface.last_successful_prices = []
        
        price_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = price_iface.get_current_prices()
        
        # Should have complete 48 hours with today's prices repeated for tomorrow
        assert len(prices) == 48
        # Tomorrow (hours 24-47) should repeat today's pattern (hours 0-23)
        for i in range(24):
            assert prices[24 + i] == pytest.approx(prices[i], rel=1e-9)
        # Forecast metadata should indicate simple_repetition
        assert price_iface.forecast_start_index == 24
        assert price_iface.forecast_type == "simple_repetition"
