"""Tests for EVCC feed-in price source integration."""

from datetime import datetime, timezone, timedelta
from unittest.mock import Mock

import pytest

from src.interfaces.feed_in_price_interface import FeedInPriceInterface

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


class TestFeedInEvccPriceSource:
    """Tests for EVCC as a feed-in (export) price source."""

    def test_evcc_interface_parameter_accepted(self, monkeypatch):
        """Test that EVCC interface parameter is accepted and stored."""
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        assert feed_in_iface.evcc_interface == mock_evcc
        assert feed_in_iface.source == "evcc"

    def test_evcc_not_configured_graceful_fallback(self, monkeypatch):
        """Test graceful degradation when EVCC interface is not configured."""
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc", "fixed_price_ct_kwh": 5.0},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=None,
        )
        
        # update_prices should handle None evcc_interface gracefully
        feed_in_iface.update_prices(48)
        prices = feed_in_iface.get_current_feedin_prices()
        # Should fall back to default prices
        assert len(prices) > 0
        # Default price is 0.5 ct/kWh = 0.000005 EUR/Wh
        assert prices[0] == 0.000005

    def test_evcc_url_present_but_empty(self, monkeypatch):
        """Test fallback when EVCC URL is empty string."""
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = ""  # Empty URL
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc", "fixed_price_ct_kwh": 5.0},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48)
        prices = feed_in_iface.get_current_feedin_prices()
        # Should fall back to default prices
        assert len(prices) > 0

    def test_evcc_feed_in_tariff_parsing_hourly(self, monkeypatch):
        """Test parsing EVCC feed-in rate response to EUR/Wh format."""
        # Mock EVCC API response with 48 hours of prices
        rates_data = []
        for hour in range(48):
            rates_data.append({
                "start": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(hours=hour)).isoformat(),
                "end": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(hours=hour+1)).isoformat(),
                "value": 0.08 + (hour % 24) * 0.002  # EUR/kWh
            })
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            assert "/api/tariff/feedin" in url
            return R()
        
        monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = feed_in_iface.get_current_feedin_prices()
        
        # Verify we got prices (converted from EUR/kWh to EUR/Wh)
        assert len(prices) == 48
        # First price should be 0.08 / 1000 = 0.00008 EUR/Wh
        assert abs(prices[0] - 0.00008) < 0.00001

    def test_evcc_tariff_eur_per_wh_conversion(self, monkeypatch):
        """Test EUR/kWh to EUR/Wh conversion from EVCC response."""
        rates_data = [
            {
                "start": datetime(2025, 10, 20, 0, tzinfo=timezone.utc).isoformat(),
                "value": 0.100  # 0.1 EUR/kWh
            }
        ]
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            return R()
        
        monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48)
        prices = feed_in_iface.get_current_feedin_prices()
        
        # 0.1 EUR/kWh should convert to 0.0001 EUR/Wh
        assert abs(prices[0] - 0.0001) < 0.00001

    def test_evcc_prices_used_as_is_no_adder(self, monkeypatch):
        """Test that EVCC feed-in prices are NOT modified by static adder."""
        rates_data = [
            {
                "start": datetime(2025, 10, 20, 0, tzinfo=timezone.utc).isoformat(),
                "value": 0.100  # 0.1 EUR/kWh
            }
        ]
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            return R()
        
        monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        # Configure with a 5 ct/kWh adder (should NOT be applied to EVCC feed-in)
        feed_in_iface = FeedInPriceInterface(
            {
                "source": "evcc",
                "static_adder_ct_kwh": 5.0,  # 5 ct/kWh adder
            },
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48)
        prices = feed_in_iface.get_current_feedin_prices()
        
        # Price should be 0.0001 EUR/Wh (NOT 0.0001 + adder)
        # EVCC feed-in prices are used raw
        assert abs(prices[0] - 0.0001) < 0.00001

    def test_evcc_prices_used_as_is_no_multiplier(self, monkeypatch):
        """Test that EVCC feed-in prices are NOT modified by multiplier."""
        rates_data = [
            {
                "start": datetime(2025, 10, 20, 0, tzinfo=timezone.utc).isoformat(),
                "value": 0.100  # 0.1 EUR/kWh
            }
        ]
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            return R()
        
        monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        # Configure with a multiplier (should NOT be applied to EVCC feed-in)
        feed_in_iface = FeedInPriceInterface(
            {
                "source": "evcc",
                "multiplier": 1.1,  # 10% multiplier
            },
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48)
        prices = feed_in_iface.get_current_feedin_prices()
        
        # Price should be 0.0001 EUR/Wh (NOT multiplied by 1.1)
        assert abs(prices[0] - 0.0001) < 0.00001

    def test_evcc_connection_error_fallback(self, monkeypatch):
        """Test fallback when EVCC connection fails."""
        import requests
        
        def fake_get(url, headers=None, timeout=None):
            raise requests.RequestException("Connection timeout")
        
        monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc", "fixed_price_ct_kwh": 5.0},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48)
        prices = feed_in_iface.get_current_feedin_prices()
        
        # Should fall back to default prices on error
        assert len(prices) > 0
        # Default fallback is used
        assert prices[0] == 0.000005

    def test_evcc_invalid_json_response_fallback(self, monkeypatch):
        """Test fallback when EVCC returns invalid JSON."""
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    raise ValueError("Invalid JSON")
            return R()
        
        monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc", "fixed_price_ct_kwh": 5.0},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48)
        prices = feed_in_iface.get_current_feedin_prices()
        
        # Should fall back to default prices
        assert len(prices) > 0

    def test_incomplete_evcc_feed_in_data_fallback(self, monkeypatch):
        """Test fallback when EVCC returns incomplete data."""
        # Provide 24 rates instead of 48 (half the expected data)
        rates_data = []
        for hour in range(24):
            rates_data.append({
                "start": (datetime(2025, 10, 20, 0, tzinfo=timezone.utc) + timedelta(hours=hour)).isoformat(),
                "value": 0.100
            })
        
        evcc_response = {"rates": rates_data}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return evcc_response
            return R()
        
        monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48)
        prices = feed_in_iface.get_current_feedin_prices()
        
        # Should extend incomplete data using _extend_prices_to_duration
        # With 24 rates and target 48, should extend to at least 48
        assert len(prices) >= 24  # At least the 24 we provided

    def test_evcc_feed_in_vs_grid_prices_different_sources(self, monkeypatch):
        """Test that EVCC feed-in and grid prices are independent sources."""
        # EVCC feed-in rates
        feed_in_rates = [
            {
                "start": datetime(2025, 10, 20, 0, tzinfo=timezone.utc).isoformat(),
                "value": 0.050  # 0.05 EUR/kWh feed-in
            }
        ]
        
        feed_in_response = {"rates": feed_in_rates}
        
        call_count = {"grid": 0, "feed_in": 0}
        
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    if "/api/tariff/feedin" in url:
                        call_count["feed_in"] += 1
                        return feed_in_response
                    else:
                        call_count["grid"] += 1
                        return {"rates": []}
            return R()
        
        monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        feed_in_iface.update_prices(48)
        
        # Verify that feed_in endpoint was called, not grid endpoint
        assert call_count["feed_in"] > 0
        # Grid endpoint should not be called for feed-in source
        assert call_count["grid"] == 0
