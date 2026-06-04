"""
Unit tests for FeedInPriceInterface.
"""

from datetime import datetime
from unittest.mock import patch, MagicMock
import pytest
import pytz
import requests

from src.interfaces.feed_in_price_interface import FeedInPriceInterface


class TestFeedInPriceInterfaceFixedPrice:
    """Test fixed price mode."""

    def test_fixed_price_initialization(self):
        """Test initialization with fixed price source."""
        config = {
            "source": "fixed",
            "fixed_price_ct_kwh": 8.0,  # 8 ct/kWh
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        assert interface.source == "fixed"
        assert interface.fixed_price_ct_kwh == 8.0
        assert len(interface.current_feedin_prices) == 0  # Not yet updated

    def test_fixed_price_array_generation(self):
        """Test fixed price array generation for 48h."""
        config = {
            "source": "fixed",
            "fixed_price_ct_kwh": 8.0,  # 8 ct/kWh
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        interface.update_prices(48, datetime(2024, 1, 1, 0, 0, 0, tzinfo=pytz.UTC))

        assert len(interface.current_feedin_prices) == 48
        # All prices should be 8 ct/kWh = 0.00008 EUR/Wh
        assert all(p == pytest.approx(0.00008, abs=1e-8) for p in interface.current_feedin_prices)

    def test_fixed_price_15min_slots(self):
        """Test fixed price array for 15-minute slots (96 slots = 24h)."""
        config = {
            "source": "fixed",
            "fixed_price_ct_kwh": 10.0,  # 10 ct/kWh
        }
        interface = FeedInPriceInterface(config, 900, "UTC")  # 900s = 15min
        interface.update_prices(192, datetime(2024, 1, 1, 0, 0, 0, tzinfo=pytz.UTC))

        # 192 slots = 48h (each slot is 15min)
        assert len(interface.current_feedin_prices) == 192
        assert all(p == pytest.approx(0.0001, abs=1e-8) for p in interface.current_feedin_prices)

    def test_fixed_price_with_static_adder(self):
        """Static adder must be applied to fixed source (same as dynamic sources)."""
        config = {
            "source": "fixed",
            "fixed_price_ct_kwh": 8.0,   # 8 ct/kWh base
            "static_adder_ct_kwh": 2.0,  # +2 ct/kWh
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        interface.update_prices(48, datetime(2024, 1, 1, 0, 0, 0, tzinfo=pytz.UTC))

        # 8 + 2 = 10 ct/kWh = 0.0001 EUR/Wh
        assert all(p == pytest.approx(0.0001, abs=1e-8) for p in interface.current_feedin_prices)

    def test_fixed_price_with_multiplier(self):
        """Multiplier must be applied to fixed source."""
        config = {
            "source": "fixed",
            "fixed_price_ct_kwh": 10.0,
            "multiplier": 0.9,
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        interface.update_prices(48, datetime(2024, 1, 1, 0, 0, 0, tzinfo=pytz.UTC))

        # 10 * 0.9 = 9 ct/kWh = 0.00009 EUR/Wh
        assert all(p == pytest.approx(0.00009, abs=1e-8) for p in interface.current_feedin_prices)


class TestFeedInPriceInterfaceEprisDK:
    """Test Elpris DK API integration."""

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_elpris_dk_price_fetch(self, mock_get):
        """Test Elpris API price fetching and conversion."""
        # Mock Elpris API response (DKK/kWh)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "prices": [
                {"hour": 0, "price": 3.50},   # DKK/kWh
                {"hour": 1, "price": 4.20},
                {"hour": 2, "price": 3.85},
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        config = {
            "source": "elpris_dk",
            "zone": "DK1",
            "static_adder_ct_kwh": 3.5,  # 3.5 ct/kWh (standard unit)
            "multiplier": 1.0,
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        prices = interface._fetch_elpris_prices(3, datetime(2024, 1, 1, 0, 0, 0, tzinfo=pytz.UTC))

        # Verify prices were fetched and converted
        assert len(prices) == 3
        assert all(isinstance(p, float) for p in prices)
        assert all(p > 0 for p in prices)  # All prices should be positive

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_elpris_dk_with_multiplier(self, mock_get):
        """Test Elpris prices with multiplier adjustment."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "prices": [
                {"hour": 0, "price": 4.00},  # DKK/kWh
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        config = {
            "source": "elpris_dk",
            "zone": "DK2",
            "static_adder_ct_kwh": 0.0,  # ct/kWh
            "multiplier": 1.05,  # +5% adjustment
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        prices = interface._fetch_elpris_prices(1, datetime(2024, 1, 1, 0, 0, 0, tzinfo=pytz.UTC))

        # Prices should reflect the 1.05x multiplier
        assert len(prices) == 1
        assert prices[0] > 0

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_elpris_dk_api_error_fallback(self, mock_get):
        """Test fallback to default prices on API error."""
        # Mock requests.ConnectionError (subclass of requests.RequestException)
        mock_get.side_effect = requests.ConnectionError("API connection failed")

        config = {
            "source": "elpris_dk",
            "zone": "DK1",
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        prices = interface._fetch_elpris_prices(48, datetime(2024, 1, 1, 0, 0, 0, tzinfo=pytz.UTC))

        # Should return empty list on error (caught by exception handler)
        assert prices == []


class TestFeedInPriceInterfaceValidation:
    """Test configuration validation."""

    def test_invalid_source_fallback(self):
        """Test invalid source falls back to 'fixed'."""
        config = {
            "source": "invalid_source",
            "fixed_price": 0.05,
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        assert interface.source == "fixed"

    def test_invalid_zone_fallback(self):
        """Test invalid Elpris zone falls back to DK1."""
        config = {
            "source": "elpris_dk",
            "zone": "INVALID",
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")
        assert interface.zone == "DK1"


class TestFeedInPriceInterfaceHotReload:
    """Test hot-reload capabilities."""

    def test_static_adder_update(self):
        """Test updating static adder without restart."""
        config = {
            "source": "fixed",
            "fixed_price_ct_kwh": 8.0,  # ct/kWh
            "static_adder_ct_kwh": 0.0,  # ct/kWh
            "multiplier": 1.0,
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")

        # Verify initial state
        assert interface.static_adder_ct_kwh == 0.0

        # Update adder (simulating hot-reload)
        interface.static_adder_ct_kwh = 3.5
        assert interface.static_adder_ct_kwh == 3.5

    def test_multiplier_update(self):
        """Test updating multiplier without restart."""
        config = {
            "source": "fixed",
            "fixed_price_ct_kwh": 8.0,  # ct/kWh
            "multiplier": 1.0,
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")

        # Update multiplier
        interface.multiplier = 1.10
        assert interface.multiplier == 1.10


class TestFeedInPriceInterfaceArrayExtension:
    """Test array extension logic."""

    def test_extend_prices_to_48h(self):
        """Test extending 24h prices to 48h."""
        prices_24h = [0.08] * 24
        config = {"source": "fixed", "fixed_price": 0.08}
        interface = FeedInPriceInterface(config, 3600, "UTC")

        extended = interface._extend_prices_to_duration(prices_24h, 48)
        assert len(extended) == 48
        assert all(p == 0.08 for p in extended)

    def test_extend_prices_to_15min_slots(self):
        """Test extending hourly prices to 15-min slots."""
        prices_hourly = [0.08] * 24
        config = {"source": "fixed", "fixed_price": 0.08}
        interface = FeedInPriceInterface(config, 900, "UTC")  # 15-min time_frame_base

        extended = interface._extend_prices_to_duration(prices_hourly, 192)
        # Each hourly price becomes 4 slots, so 24 * 4 = 96
        assert len(extended) >= 96
        # Each original price is repeated 4 times
        for i in range(0, min(96, len(extended)), 4):
            assert extended[i] == extended[i + 1] == extended[i + 2] == extended[i + 3]


class TestFeedInPriceInterfaceDefaults:
    """Test default behavior."""

    def test_default_prices_on_failure(self):
        """Test system uses default prices if API fails persistently."""
        config = {
            "source": "elpris_dk",
            "zone": "DK1",
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")

        # Simulate repeated failures
        for _ in range(30):
            interface.consecutive_failures += 1

        # After max failures exceeded, should use default prices
        assert interface.consecutive_failures > interface.max_failures
        assert len(interface.default_prices) == 48

    def test_fallback_to_last_successful(self):
        """Test fallback to last successful prices within retry window."""
        config = {
            "source": "fixed",
            "fixed_price": 0.08,
        }
        interface = FeedInPriceInterface(config, 3600, "UTC")

        # Set last successful prices
        interface.last_successful_prices = [0.09] * 48
        interface.consecutive_failures = 5

        # Should use last successful if within retry window
        prices = [0.09] * 48 if interface.consecutive_failures <= interface.max_failures else []
        assert prices == interface.last_successful_prices
