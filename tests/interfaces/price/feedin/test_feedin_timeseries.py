# pylint: disable=protected-access
"""
The feed-in timeseries source: the price pipeline, pointed the other way.

A feed-in tariff in Home Assistant could not be used at all before this source
existed (N4 in the #214 review). The path is the same chain the grid-price
timeseries source runs — shared normalizer, intensive overlap placement, the
established gap policy, forecast metadata — plus the feed-in adjustment
convention ((price + adder) * multiplier, then the negative-price clamp).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.interfaces.feed_in_price_interface import FeedInPriceInterface

ANCHOR = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)


def _payload(hours, base=0.10):
    """Hourly feed-in prices in EUR/kWh, value encodes the hour."""
    return {
        "attributes": {
            "data": [
                {
                    "start": (ANCHOR + timedelta(hours=h)).isoformat(),
                    "value": round(base + h / 1000, 6),
                }
                for h in hours
            ]
        }
    }


def _interface(monkeypatch, payload, **config_overrides):
    def fake_get(url, headers=None, timeout=None):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        assert url == "http://ha.local:8123/api/states/sensor.feed_in_prices"
        return Response()

    monkeypatch.setattr(
        "src.interfaces.feed_in_price_interface.requests.get", fake_get
    )
    monkeypatch.setattr(
        FeedInPriceInterface, "_start_update_service", lambda self: None
    )
    config = {
        "source": "timeseries",
        "data_url": "http://ha.local:8123/api/states/sensor.feed_in_prices",
        "data_path": "attributes.data",
        "value_unit": "EUR/kWh",
    }
    config.update(config_overrides)
    return FeedInPriceInterface(
        config, time_frame_base=3600, timezone=timezone.utc, evcc_interface=Mock()
    )


class TestTimeseriesFeedInSource:
    def test_values_land_on_their_slots_and_convert(self, monkeypatch):
        iface = _interface(monkeypatch, _payload(range(48)))
        iface.update_prices(48, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert len(prices) == 48
        assert prices[0] == pytest.approx(0.10 / 1000)
        assert prices[47] == pytest.approx(0.147 / 1000)

    def test_a_short_series_fills_and_reports_the_tail(self, monkeypatch):
        iface = _interface(monkeypatch, _payload(range(24)))
        iface.update_prices(48, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert len(prices) == 48
        # Tomorrow repeats today's pattern...
        assert prices[24] == pytest.approx(0.10 / 1000)
        # ...and the metadata says where invention starts.
        metadata = iface.get_forecast_metadata()
        assert metadata["forecast_start_index"] == 24
        assert metadata["forecast_type"] == "simple_repetition"

    def test_adder_and_multiplier_apply_in_the_feed_in_convention(
        self, monkeypatch
    ):
        iface = _interface(
            monkeypatch,
            _payload(range(48)),
            static_adder_ct_kwh=2.0,  # 2 ct/kWh = 0.00002 EUR/Wh
            multiplier=1.1,
        )
        iface.update_prices(48, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert prices[0] == pytest.approx((0.10 / 1000 + 0.00002) * 1.1)

    def test_negative_price_switch_clamps_after_the_adjustments(self, monkeypatch):
        iface = _interface(
            monkeypatch,
            _payload(range(48), base=-0.05),
            negative_price_switch=True,
        )
        iface.update_prices(48, start_time=ANCHOR)

        assert iface.get_current_feedin_prices()[0] == 0.0

    def test_a_stale_series_falls_back_to_the_last_good_prices(self, monkeypatch):
        iface = _interface(monkeypatch, _payload(range(48)))
        iface.last_successful_prices = [0.00004] * 48
        iface.update_prices(
            48, start_time=ANCHOR + timedelta(days=3)  # window far beyond the data
        )

        assert iface.get_current_feedin_prices() == [0.00004] * 48

    def test_field_aliases_work_here_too(self, monkeypatch):
        payload = {
            "attributes": {
                "data": [
                    {
                        "start_time": (ANCHOR + timedelta(hours=h)).isoformat(),
                        "price_per_kwh": 0.10,
                    }
                    for h in range(48)
                ]
            }
        }
        iface = _interface(monkeypatch, payload)
        iface.update_prices(48, start_time=ANCHOR)

        assert iface.get_current_feedin_prices()[0] == pytest.approx(0.0001)

    def test_a_missing_url_is_an_error_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(
            FeedInPriceInterface, "_start_update_service", lambda self: None
        )
        iface = FeedInPriceInterface(
            {"source": "timeseries"}, time_frame_base=3600, timezone=timezone.utc
        )
        iface.update_prices(48, start_time=ANCHOR)

        # No URL -> no prices -> default fallback.
        assert iface.get_current_feedin_prices()[0] == pytest.approx(0.000005)
