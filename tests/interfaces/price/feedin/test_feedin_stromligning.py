# pylint: disable=protected-access
"""
The Stromligning feed-in source: Danish sale prices per slot.

/api/prices/production serves the spot price excluding VAT less the production
tariffs (and, with a productId, the electricity company's deduction) in DKK/kWh:
published quarter hours first, then Strømligning's hourly forecast. The source
places them by timestamp like the other dynamic feed-in sources and passes them
on in the same unit the grid-price Stromligning source uses (DKK/Wh).
"""

from datetime import datetime, timedelta, timezone
import logging
from unittest.mock import Mock

import pytest
import requests

from src.interfaces.feed_in_price_interface import (
    STROMLIGNING_PRODUCTION_API,
    FeedInPriceInterface,
)
from src.version import __version__

ANCHOR = datetime(2026, 11, 22, 23, 0, tzinfo=timezone.utc)


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _payload(published_hours=24, forecast_hours=24):
    """Quarter-hour published prices, then hourly forecast entries (lean shape)."""
    entries = [
        {
            "date": _stamp(ANCHOR + timedelta(minutes=15 * q)),
            "price": round(0.5 + q / 1000, 6),
            "resolution": "15m",
        }
        for q in range(published_hours * 4)
    ]
    entries += [
        {
            "date": _stamp(ANCHOR + timedelta(hours=published_hours + h)),
            "price": round(0.8 + h / 100, 6),
            "resolution": "1h",
            "forecast": True,
        }
        for h in range(forecast_hours)
    ]
    return entries


def _interface(monkeypatch, payload, calls=None, time_frame_base=3600, **overrides):
    def fake_get(url, params=None, headers=None, timeout=None):
        # pylint: disable=unused-argument
        if calls is not None:
            calls.append({"url": url, "params": params, "headers": headers})

        class Response:  # pylint: disable=missing-class-docstring
            def raise_for_status(self):  # pylint: disable=missing-function-docstring
                return None

            def json(self):  # pylint: disable=missing-function-docstring
                return payload

        return Response()

    monkeypatch.setattr("src.interfaces.feed_in_price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        FeedInPriceInterface, "_start_update_service", lambda self: None
    )
    config = {"source": "stromligning", "token": "radius_c"}
    config.update(overrides)
    return FeedInPriceInterface(
        config,
        time_frame_base=time_frame_base,
        timezone=timezone.utc,
        evcc_interface=Mock(),
    )


class TestStromligningFeedInSource:
    """Request, placement, forecast marking and adjustments of the source."""

    def test_request_names_supplier_window_and_client(self, monkeypatch):
        """One request: supplier, the window in UTC, forecast, lean, and our name."""
        calls = []
        iface = _interface(monkeypatch, _payload(), calls)
        iface.update_prices(48, start_time=ANCHOR)

        assert calls[0]["url"] == STROMLIGNING_PRODUCTION_API
        assert calls[0]["params"] == {
            "supplierId": "radius_c",
            "from": "2026-11-22T23:00:00Z",
            "to": "2026-11-24T23:00:00Z",
            "forecast": "true",
            "lean": "true",
        }
        assert calls[0]["headers"]["User-Agent"] == f"EOS_connect/{__version__}"

    def test_production_agreement_is_passed_as_product_id(self, monkeypatch):
        """supplierId/productId adds the production agreement as productId."""
        calls = []
        iface = _interface(
            monkeypatch, _payload(), calls, token="radius_c/nrgi_produktion"
        )
        iface.update_prices(48, start_time=ANCHOR)

        assert calls[0]["params"]["supplierId"] == "radius_c"
        assert calls[0]["params"]["productId"] == "nrgi_produktion"

    def test_hourly_slots_average_quarters_and_take_forecast_hours(self, monkeypatch):
        """Hourly slots average four quarters; a forecast hour fills its slot."""
        iface = _interface(monkeypatch, _payload())
        iface.update_prices(48, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert len(prices) == 48
        # First hour: mean of 0.500, 0.501, 0.502, 0.503 DKK/kWh, as DKK/Wh
        assert prices[0] == pytest.approx(0.5015 / 1000)
        # The forecast hours hold their value across the hour
        assert prices[24] == pytest.approx(0.8 / 1000)
        assert prices[47] == pytest.approx(1.03 / 1000)

    def test_quarter_hour_slots_keep_published_quarters(self, monkeypatch):
        """Quarter-hour slots keep each published quarter as it is."""
        iface = _interface(monkeypatch, _payload(), time_frame_base=900)
        iface.update_prices(192, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert len(prices) == 192
        assert prices[1] == pytest.approx(0.501 / 1000)
        # An hourly forecast entry covers its four quarters
        assert prices[96:100] == pytest.approx([0.8 / 1000] * 4)

    def test_forecast_is_reported_where_it_starts(self, monkeypatch):
        """The first forecast entry marks the forecast start for the chart."""
        iface = _interface(monkeypatch, _payload())
        iface.update_prices(48, start_time=ANCHOR)

        assert iface.get_forecast_metadata() == {
            "forecast_start_index": 24,
            "forecast_type": "smart_forecast",
            "forecast_source": "stromligning.dk",
        }

    def test_all_published_is_all_real(self, monkeypatch):
        """A window of published prices only is reported as real data."""
        iface = _interface(monkeypatch, _payload(published_hours=48, forecast_hours=0))
        iface.update_prices(48, start_time=ANCHOR)

        assert iface.get_forecast_metadata()["forecast_type"] == "all_real"

    def test_adjustments_apply_in_the_feed_in_convention(self, monkeypatch):
        """(price + adder) * multiplier, as for every dynamic feed-in source."""
        iface = _interface(
            monkeypatch,
            _payload(),
            static_adder_ct_kwh=10.0,
            multiplier=0.5,
        )
        iface.update_prices(48, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert prices[24] == pytest.approx((0.8 / 1000 + 10.0 / 100000) * 0.5)

    @pytest.mark.parametrize("token", ["", "radius_c/", "a/b/c"])
    def test_a_bad_token_fetches_nothing(self, monkeypatch, token):
        """A malformed token is a config error, not a request."""
        calls = []
        iface = _interface(monkeypatch, _payload(), calls, token=token)

        assert iface._fetch_stromligning_prices(48, ANCHOR) == []
        assert not calls

    def test_a_non_list_response_fetches_nothing(self, monkeypatch):
        """An error object from the API yields no prices."""
        iface = _interface(monkeypatch, {"error": "Invalid supplier"})

        assert iface._fetch_stromligning_prices(48, ANCHOR) == []


class TestStromligningFeedInGaps:
    """Short series, broken entries and where the chart's forecast hatching starts."""

    def test_published_day_only_repeats_it_as_simple_repetition(self, monkeypatch):
        """No forecast entries: day two repeats day one and is marked as such."""
        iface = _interface(monkeypatch, _payload(published_hours=24, forecast_hours=0))
        iface.update_prices(48, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert len(prices) == 48
        assert prices[24:] == pytest.approx(prices[:24])
        assert iface.get_forecast_metadata() == {
            "forecast_start_index": 24,
            "forecast_type": "simple_repetition",
            "forecast_source": None,
        }

    def test_a_short_forecast_is_marked_where_it_starts(self, monkeypatch):
        """The forecast starts before the filled tail, so the forecast wins."""
        iface = _interface(monkeypatch, _payload(published_hours=24, forecast_hours=6))
        iface.update_prices(48, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert len(prices) == 48
        metadata = iface.get_forecast_metadata()
        assert metadata["forecast_start_index"] == 24
        assert metadata["forecast_type"] == "smart_forecast"

    def test_a_forecast_past_the_window_is_all_real(self, monkeypatch):
        """Forecast entries beyond the horizon do not move the marker."""
        iface = _interface(monkeypatch, _payload(published_hours=48, forecast_hours=4))
        iface.update_prices(48, start_time=ANCHOR)

        assert iface.get_forecast_metadata()["forecast_type"] == "all_real"

    def test_malformed_entries_are_skipped(self, monkeypatch):
        """Missing date, unknown resolution or a non-number price drop one entry."""
        payload = _payload(published_hours=48, forecast_hours=0)
        payload[0] = {"price": 9.9, "resolution": "15m"}
        payload[1] = dict(payload[1], resolution="5m")
        payload[2] = dict(payload[2], price=None)
        payload[3] = "not an entry"
        iface = _interface(monkeypatch, payload)
        iface.update_prices(48, start_time=ANCHOR)

        prices = iface.get_current_feedin_prices()
        assert len(prices) == 48
        # Only the first hour's four quarters were broken; slot 0 takes the
        # first known price backwards, slot 1 is untouched.
        assert prices[0] == pytest.approx(prices[1])
        assert prices[1] == pytest.approx(0.5055 / 1000)

    def test_only_malformed_entries_fetch_nothing(self, monkeypatch):
        """Nothing usable is [] so the caller keeps the last good prices."""
        iface = _interface(monkeypatch, [{"date": "yesterday"}, {"price": 1.0}])

        assert iface._fetch_stromligning_prices(48, ANCHOR) == []


def _http_error_get(status, text):
    """A requests.get stand-in whose response fails raise_for_status."""

    def fake_get(url, params=None, headers=None, timeout=None):
        # pylint: disable=unused-argument
        response = requests.Response()
        response.status_code = status
        response._content = text.encode()  # pylint: disable=protected-access
        response.url = url
        return response

    return fake_get


class TestStromligningFeedInErrors:
    """What the user reads in the log when a request fails."""

    def test_a_rejected_token_logs_the_api_reason(self, monkeypatch, caplog):
        """A 4xx names the token and the API's own reason, as a config error."""
        iface = _interface(monkeypatch, [], token="nope_x")
        monkeypatch.setattr(
            "src.interfaces.feed_in_price_interface.requests.get",
            _http_error_get(400, "Supplier not found"),
        )

        with caplog.at_level(logging.ERROR):
            assert iface._fetch_stromligning_prices(48, ANCHOR) == []

        assert "Supplier not found" in caplog.text
        assert "'nope_x'" in caplog.text
        assert "ACTION REQUIRED" in caplog.text

    def test_a_server_error_is_not_blamed_on_the_token(self, monkeypatch, caplog):
        """A 5xx is logged with its status but not as a config problem."""
        iface = _interface(monkeypatch, [])
        monkeypatch.setattr(
            "src.interfaces.feed_in_price_interface.requests.get",
            _http_error_get(503, "Service Unavailable"),
        )

        with caplog.at_level(logging.ERROR):
            assert iface._fetch_stromligning_prices(48, ANCHOR) == []

        assert "HTTP 503" in caplog.text
        assert "ACTION REQUIRED" not in caplog.text

    def test_a_connection_error_fetches_nothing(self, monkeypatch):
        """No connection, no prices: the caller's fallback chain takes over."""
        iface = _interface(monkeypatch, [])

        def unreachable(*args, **kwargs):
            raise requests.ConnectionError("unreachable")

        monkeypatch.setattr(
            "src.interfaces.feed_in_price_interface.requests.get", unreachable
        )

        assert iface._fetch_stromligning_prices(48, ANCHOR) == []


class TestFeedInCurrencyCheck:
    """DKK feed-in next to EUR grid prices is warned about, once."""

    def test_stromligning_serves_dkk(self, monkeypatch):
        """The source's currency is known, like the grid-price Stromligning one."""
        assert _interface(monkeypatch, []).get_price_currency() == "DKK"

    def test_same_currency_is_quiet(self, monkeypatch, caplog):
        """Strømligning on both sides: nothing to warn about."""
        iface = _interface(monkeypatch, [])

        with caplog.at_level(logging.WARNING):
            assert iface.check_currency_match("stromligning", "DKK") is True

        assert "compares them 1:1" not in caplog.text

    def test_a_mismatch_warns_once_per_pair(self, monkeypatch, caplog):
        """EUR grid prices: one warning, not one per optimization cycle."""
        iface = _interface(monkeypatch, [])

        with caplog.at_level(logging.WARNING):
            assert iface.check_currency_match("smartenergy_at", "EUR") is False
            assert iface.check_currency_match("smartenergy_at", "EUR") is False

        warnings = [r for r in caplog.records if "compares them 1:1" in r.message]
        assert len(warnings) == 1
        assert "DKK" in warnings[0].message and "EUR" in warnings[0].message

    def test_fixing_the_config_rearms_the_warning(self, monkeypatch, caplog):
        """A match in between clears it, so the next mismatch is reported."""
        iface = _interface(monkeypatch, [])

        with caplog.at_level(logging.WARNING):
            iface.check_currency_match("smartenergy_at", "EUR")
            iface.check_currency_match("stromligning", "DKK")
            iface.check_currency_match("smartenergy_at", "EUR")

        warnings = [r for r in caplog.records if "compares them 1:1" in r.message]
        assert len(warnings) == 2

    @pytest.mark.parametrize("currency,matches", [("DKK", True), ("EUR", False)])
    def test_tibber_is_judged_by_its_account_currency(
        self, monkeypatch, currency, matches
    ):
        """Tibber reports the account currency; a Danish account is DKK."""
        iface = _interface(monkeypatch, [])

        assert iface.check_currency_match("tibber", currency) is matches

    @pytest.mark.parametrize("grid_source", ["evcc", "timeseries", "fixed_24h"])
    def test_grid_sources_with_user_units_are_not_judged(
        self, monkeypatch, caplog, grid_source
    ):
        """PriceInterface only defaults these to EUR; a DKK setup is not wrong."""
        iface = _interface(monkeypatch, [])

        with caplog.at_level(logging.WARNING):
            assert iface.check_currency_match(grid_source, "EUR") is True

        assert "compares them 1:1" not in caplog.text

    @pytest.mark.parametrize("source", ["fixed", "evcc", "timeseries"])
    def test_feed_in_sources_with_user_units_are_not_judged(self, monkeypatch, source):
        """Feed-in sources whose unit the user sets have no currency to compare."""
        iface = _interface(monkeypatch, [], source=source)

        assert iface.get_price_currency() is None
        assert iface.check_currency_match("stromligning", "DKK") is True
