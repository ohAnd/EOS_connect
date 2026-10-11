"""Tests for EVCC feed-in price source integration."""

from datetime import datetime, timezone, timedelta
from unittest.mock import Mock

import pytest
import pytz

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
        
        # The rates are dated 2025-10-20, so the window must be too:
        # placement is by timestamp now, not by position in the list.
        feed_in_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = feed_in_iface.get_current_feedin_prices()
        
        # 0.1 EUR/kWh should convert to 0.0001 EUR/Wh
        assert abs(prices[0] - 0.0001) < 0.00001

    def test_evcc_prices_get_the_configured_adder(self, monkeypatch):
        """The adder applies on EVCC too — it was offered for every source but
        silently ignored here (P5 in the #214 review)."""
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
        
        # Configure with a 5 ct/kWh adder — applied like on every other source
        feed_in_iface = FeedInPriceInterface(
            {
                "source": "evcc",
                "static_adder_ct_kwh": 5.0,  # 5 ct/kWh adder
            },
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        # The rates are dated 2025-10-20, so the window must be too:
        # placement is by timestamp now, not by position in the list.
        feed_in_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = feed_in_iface.get_current_feedin_prices()
        
        # 0.1 EUR/kWh + 5 ct/kWh adder = 0.00015 EUR/Wh
        assert abs(prices[0] - 0.00015) < 0.0000001

    def test_evcc_prices_get_the_configured_multiplier(self, monkeypatch):
        """The multiplier applies on EVCC too (plain factor, feed-in convention)."""
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
        
        # Configure with a multiplier — applied like on every other source
        feed_in_iface = FeedInPriceInterface(
            {
                "source": "evcc",
                "multiplier": 1.1,  # 10% multiplier
            },
            time_frame_base=3600,
            timezone=timezone.utc,
            evcc_interface=mock_evcc,
        )
        
        # The rates are dated 2025-10-20, so the window must be too:
        # placement is by timestamp now, not by position in the list.
        feed_in_iface.update_prices(48, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc))
        prices = feed_in_iface.get_current_feedin_prices()
        
        # 0.0001 EUR/Wh * 1.1 = 0.00011 EUR/Wh
        assert abs(prices[0] - 0.00011) < 0.0000001

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


class TestEvccTimestampOffsets:
    """EVCC renders timestamps with a numeric offset, e.g. '+02:00'.

    The source's own offset is authoritative. A hand-rolled parser once split
    it off with rsplit('+') and re-stamped the naive remainder with
    `.replace(tzinfo=self.time_zone)` — on a pytz zone that attaches the LMT
    offset (+00:53 for Europe/Berlin), so in summer every 15-minute entry
    landed 67 minutes late and the hourly grouping put it one slot after its
    real hour (B5 in discussion #214's review). These tests guard against any
    parser that drops or re-derives an explicit offset.
    """

    BERLIN = pytz.timezone("Europe/Berlin")

    def _prices_for_a_berlin_summer_day(self, monkeypatch):
        """48 h of 15-min EVCC rates with '+02:00' offsets; EUR/kWh encodes the hour."""
        midnight = self.BERLIN.localize(datetime(2026, 7, 8, 0, 0))
        rates_data = []
        for hour in range(48):
            for quarter in range(4):
                begin = midnight + timedelta(hours=hour, minutes=15 * quarter)
                rates_data.append(
                    {
                        "start": begin.isoformat(),  # carries '+02:00'
                        "end": (begin + timedelta(minutes=15)).isoformat(),
                        "value": hour / 100,  # EUR/kWh encodes the hour
                    }
                )

        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"rates": rates_data}

            return R()

        monkeypatch.setattr(
            "src.interfaces.feed_in_price_interface.requests.get", fake_get
        )
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )

        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        feed_in_iface = FeedInPriceInterface(
            {"source": "evcc"},
            time_frame_base=3600,
            timezone=self.BERLIN,
            evcc_interface=mock_evcc,
        )
        feed_in_iface.update_prices(48, start_time=midnight)
        return feed_in_iface.get_current_feedin_prices()

    def test_an_entry_with_an_explicit_offset_lands_on_its_own_hour(self, monkeypatch):
        """Every hour's rate must land on the slot its own timestamp names."""
        prices = self._prices_for_a_berlin_summer_day(monkeypatch)
        assert prices[0] == pytest.approx(0.0)
        assert prices[5] == pytest.approx(0.05 / 1000)
        assert prices[6] == pytest.approx(0.06 / 1000)
        assert prices[47] == pytest.approx(0.47 / 1000)


class TestEvccFeedInPlacesByTimestamp:
    """
    The export side of discussion #214.

    EVCC feed-in was the one consumer the "five consumers, one placement"
    refactor did not reach: the timestamps were parsed and then discarded, and
    the values laid out by their position in the payload. A tariff that also
    publishes yesterday therefore served yesterday's prices as today's.
    """

    BERLIN = pytz.timezone("Europe/Berlin")
    TODAY = BERLIN.localize(datetime(2026, 8, 23))

    def _interface(self, monkeypatch, rates, time_frame_base=3600):
        def fake_get(url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"rates": rates}

            return R()

        monkeypatch.setattr(
            "src.interfaces.feed_in_price_interface.requests.get", fake_get
        )
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        mock_evcc = Mock()
        mock_evcc.url = "http://evcc.local:7070"
        return FeedInPriceInterface(
            {"source": "evcc"},
            time_frame_base=time_frame_base,
            timezone=self.BERLIN,
            evcc_interface=mock_evcc,
        )

    def _rates(self, count, start, step_hours=1):
        return [
            {
                "start": (start + timedelta(hours=i * step_hours)).isoformat(),
                "end": (start + timedelta(hours=(i + 1) * step_hours)).isoformat(),
                "value": round(0.05 + i * 0.001, 4),
            }
            for i in range(count)
        ]

    def test_a_series_starting_yesterday_lands_on_today(self, monkeypatch):
        rates = self._rates(72, self.TODAY - timedelta(days=1))
        iface = self._interface(monkeypatch, rates)

        iface.update_prices(48, start_time=self.TODAY)
        prices = iface.get_current_feedin_prices()

        assert len(prices) == 48
        # Today 00:00 is entry 24 of the payload, not entry 0.
        assert prices[0] == pytest.approx(0.074 / 1000, abs=1e-9)
        assert prices[12] == pytest.approx(0.086 / 1000, abs=1e-9)

    def test_an_hourly_tariff_holds_across_quarter_slots(self, monkeypatch):
        """Overlap placement replaces the old x4 positional expansion."""
        rates = self._rates(48, self.TODAY)
        iface = self._interface(monkeypatch, rates, time_frame_base=900)

        iface.update_prices(192, start_time=self.TODAY)
        prices = iface.get_current_feedin_prices()

        assert len(prices) == 192
        assert prices[0:4] == pytest.approx([0.05 / 1000] * 4, abs=1e-9)
        assert prices[4:8] == pytest.approx([0.051 / 1000] * 4, abs=1e-9)

    def test_a_quarter_hour_tariff_averages_into_hourly_slots(self, monkeypatch):
        """And replaces the old 15min-to-hourly conversion helper."""
        rates = self._rates(192, self.TODAY, step_hours=0.25)
        iface = self._interface(monkeypatch, rates)

        iface.update_prices(48, start_time=self.TODAY)
        prices = iface.get_current_feedin_prices()

        assert len(prices) == 48
        # Hour 0 averages the first four quarters: 0.050, 0.051, 0.052, 0.053.
        assert prices[0] == pytest.approx(0.0515 / 1000, abs=1e-9)
