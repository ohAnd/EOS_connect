"""
Unit tests for FeedInPriceInterface.
"""

from datetime import datetime
import threading
from unittest.mock import patch, MagicMock
import pytest
import pytz
import requests

from src.interfaces.feed_in_price_interface import FeedInPriceInterface


# One real day of elprisenligenu.dk, captured from
# https://www.elprisenligenu.dk/api/v1/prices/2026/10-11_DK1.json on 2026-10-11.
# The endpoint answers with a BARE ARRAY — the parser used to ask for a
# {"prices": [{"hour", "price"}]} wrapper that the API has never served, and
# every test mocked that invented shape, which is why a crash on every real
# startup (issue #321) stayed invisible for four releases. Mock the real thing.
ELPRIS_DAY1 = [
    {"DKK_per_kWh": 0.8523, "EUR_per_kWh": 0.114, "EXR": 7.476273,
     "time_start": "2026-10-11T00:00:00+02:00",
     "time_end": "2026-10-11T01:00:00+02:00"},
    {"DKK_per_kWh": 0.77058, "EUR_per_kWh": 0.10307, "EXR": 7.476273,
     "time_start": "2026-10-11T01:00:00+02:00",
     "time_end": "2026-10-11T02:00:00+02:00"},
    {"DKK_per_kWh": 0.75159, "EUR_per_kWh": 0.10053, "EXR": 7.476273,
     "time_start": "2026-10-11T02:00:00+02:00",
     "time_end": "2026-10-11T03:00:00+02:00"},
]

ELPRIS_DAY2 = [
    {"DKK_per_kWh": 1.31754, "EUR_per_kWh": 0.17623, "EXR": 7.476273,
     "time_start": "2026-10-12T00:00:00+02:00",
     "time_end": "2026-10-12T01:00:00+02:00"},
    {"DKK_per_kWh": 1.2691, "EUR_per_kWh": 0.16975, "EXR": 7.476273,
     "time_start": "2026-10-12T01:00:00+02:00",
     "time_end": "2026-10-12T02:00:00+02:00"},
    {"DKK_per_kWh": 1.23995, "EUR_per_kWh": 0.16585, "EXR": 7.476273,
     "time_start": "2026-10-12T02:00:00+02:00",
     "time_end": "2026-10-12T03:00:00+02:00"},
]

START = datetime(2026, 10, 11, 0, 0, 0, tzinfo=pytz.UTC)


def _json_response(payload):
    """A requests response whose .json() yields *payload*."""
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _elpris_interface(**overrides):
    """An elpris_dk interface with the background thread kept out of the way."""
    config = {"source": "elpris_dk", "zone": "DK1"}
    config.update(overrides)
    time_frame_base = config.pop("time_frame_base", 3600)
    with patch.object(FeedInPriceInterface, "_start_update_service", lambda self: None):
        return FeedInPriceInterface(config, time_frame_base, "UTC")


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
        # 48 hours of prices generated during initialization (30-min intervals = 48 slots)
        assert len(interface.current_feedin_prices) == 48

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


class TestFeedInPriceInterfaceElprisDK:
    """Elpris DK against the response shape the API actually serves."""

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_real_api_shape_parses_to_published_euro_price(self, mock_get):
        """The bare array parses, and EUR_per_kWh lands unchanged in EUR/Wh."""
        mock_get.side_effect = [_json_response(ELPRIS_DAY1),
                                _json_response(ELPRIS_DAY2)]

        prices = _elpris_interface()._fetch_elpris_prices(6, START)

        # 0.114 EUR/kWh published -> 0.000114 EUR/Wh internally.
        assert prices[:3] == [0.000114, 0.00010307, 0.00010053]
        assert prices[3:6] == [0.00017623, 0.00016975, 0.00016585]

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_bare_array_does_not_raise_attribute_error(self, mock_get):
        """Regression for #321: .get() on the top-level list killed the thread."""
        mock_get.side_effect = [_json_response(ELPRIS_DAY1),
                                _json_response(ELPRIS_DAY2)]

        prices = _elpris_interface()._fetch_elpris_prices(6, START)

        assert prices  # no AttributeError, and not an empty fallback either

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_exchange_rate_comes_from_the_payload(self, mock_get):
        """A record without EUR_per_kWh converts with its own EXR, not 7.46."""
        entry = {"DKK_per_kWh": 0.8523, "EXR": 7.476273}
        mock_get.side_effect = [_json_response([entry]),
                                requests.HTTPError("404 Client Error")]

        prices = _elpris_interface()._fetch_elpris_prices(1, START)

        # 0.8523 / 7.476273 = 0.114 EUR/kWh; the hard-coded 7.46 would give
        # 0.11425 and silently overstate every Danish feed-in price.
        assert prices[0] == pytest.approx(0.000114, abs=1e-9)
        assert prices[0] != pytest.approx(0.8523 / 7.46 / 1000, abs=1e-12)

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_both_days_are_fetched(self, mock_get):
        """Day two is a second request, not a repeat of day one."""
        mock_get.side_effect = [_json_response(ELPRIS_DAY1),
                                _json_response(ELPRIS_DAY2)]

        prices = _elpris_interface()._fetch_elpris_prices(6, START)

        assert mock_get.call_count == 2
        assert "10-11_DK1.json" in mock_get.call_args_list[0].args[0]
        assert "10-12_DK1.json" in mock_get.call_args_list[1].args[0]
        assert prices[:3] != prices[3:6]

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_missing_tomorrow_cycles_today(self, mock_get):
        """Before the day-ahead auction clears, tomorrow's file 404s."""
        mock_get.side_effect = [_json_response(ELPRIS_DAY1),
                                requests.HTTPError("404 Client Error")]

        prices = _elpris_interface()._fetch_elpris_prices(6, START)

        assert len(prices) == 6
        assert prices[3:6] == prices[0:3]

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_zone_selects_the_file(self, mock_get):
        """DK2 asks for the DK2 file."""
        mock_get.side_effect = [_json_response(ELPRIS_DAY1),
                                requests.HTTPError("404")]

        _elpris_interface(zone="DK2")._fetch_elpris_prices(3, START)

        assert "_DK2.json" in mock_get.call_args_list[0].args[0]

    @pytest.mark.parametrize("payload", [
        {"prices": [{"hour": 0, "price": 3.50}]},  # the shape the old mocks used
        {"foo": 1},
        "not json we know",
        42,
        [{"unexpected": "keys"}],
        [None],
    ])
    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_unexpected_payload_returns_empty_without_raising(self, mock_get, payload):
        """Any shape must cost a cycle, never an uncaught exception."""
        mock_get.side_effect = [_json_response(payload), _json_response(payload)]

        prices = _elpris_interface()._fetch_elpris_prices(48, START)

        assert prices == []

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_api_error_returns_empty(self, mock_get):
        """A connection error yields [] so update_prices can fall back."""
        mock_get.side_effect = requests.ConnectionError("API connection failed")

        prices = _elpris_interface()._fetch_elpris_prices(48, START)

        assert prices == []

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_adjustments_applied_once_via_retrieve(self, mock_get):
        """Adder and multiplier arrive through _apply_adjustments, exactly once."""
        mock_get.side_effect = [_json_response(ELPRIS_DAY1),
                                _json_response(ELPRIS_DAY2)]
        interface = _elpris_interface(static_adder_ct_kwh=3.5, multiplier=1.05)

        prices = interface._retrieve_prices(6, START)

        # (0.114 EUR/kWh + 3.5 ct/kWh) * 1.05, in EUR/Wh
        assert prices[0] == pytest.approx(
            (0.000114 + 3.5 / 100000) * 1.05, abs=1e-12
        )

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_fetch_returns_unadjusted_prices(self, mock_get):
        """The fetcher itself stays raw — otherwise adjustments double up."""
        mock_get.side_effect = [_json_response(ELPRIS_DAY1),
                                requests.HTTPError("404")]
        interface = _elpris_interface(static_adder_ct_kwh=3.5, multiplier=1.05)

        prices = interface._fetch_elpris_prices(3, START)

        assert prices[0] == 0.000114

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_15min_resolution_expands_each_hour(self, mock_get):
        """Hourly records become four quarter-hour slots each."""
        mock_get.side_effect = [_json_response(ELPRIS_DAY1),
                                _json_response(ELPRIS_DAY2)]

        interface = _elpris_interface(time_frame_base=900)
        prices = interface._fetch_elpris_prices(192, START)

        assert len(prices) == 192
        assert prices[0] == prices[1] == prices[2] == prices[3] == 0.000114


class TestFeedInPriceInterfaceEpexSpot:
    """EPEX spot via Akkudoktor — the feed-in side had no coverage at all."""

    # Captured from https://api.akkudoktor.net/prices?start=2026-10-11&end=2026-10-12
    AKKUDOKTOR = {
        "meta": {},
        "values": [
            {"start": "2026-10-10T22:00:00.000Z", "marketprice": 115.07,
             "unit": "Eur/MWh", "marketpriceEurocentPerKWh": 11.51},
            {"start": "2026-10-10T23:00:00.000Z", "marketprice": 103.07,
             "unit": "Eur/MWh", "marketpriceEurocentPerKWh": 10.31},
        ],
    }

    @staticmethod
    def _interface(**overrides):
        config = {"source": "epex_spot"}
        config.update(overrides)
        with patch.object(FeedInPriceInterface, "_start_update_service",
                          lambda self: None):
            return FeedInPriceInterface(config, 3600, "UTC")

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_marketprice_converts_to_eur_per_wh(self, mock_get):
        """11.51 ct/kWh published -> 0.0001151 EUR/Wh internally."""
        mock_get.return_value = _json_response(self.AKKUDOKTOR)

        prices = self._interface()._fetch_epex_spot_prices(2, START)

        assert prices == [0.0001151, 0.0001031]

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_adjustments_applied_once_via_retrieve(self, mock_get):
        """Adder and multiplier arrive through _apply_adjustments."""
        mock_get.return_value = _json_response(self.AKKUDOKTOR)
        interface = self._interface(static_adder_ct_kwh=2.0, multiplier=0.9)

        prices = interface._retrieve_prices(2, START)

        assert prices[0] == pytest.approx((0.0001151 + 2.0 / 100000) * 0.9,
                                          abs=1e-12)

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_negative_price_clamped_via_retrieve(self, mock_get):
        """The negative switch still clamps on the epex path."""
        mock_get.return_value = _json_response(
            {"values": [{"marketpriceEurocentPerKWh": -1.5},
                        {"marketpriceEurocentPerKWh": 11.51}]}
        )
        interface = self._interface(negative_price_switch=True)

        prices = interface._retrieve_prices(2, START)

        assert prices[0] == 0.0
        assert prices[1] == pytest.approx(0.0001151, abs=1e-12)

    @pytest.mark.parametrize("payload", [["bare", "array"], {"values": "text"},
                                         {"values": [None]}, 7])
    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_unexpected_payload_returns_empty(self, mock_get, payload):
        """A shape change must not raise past the fetcher."""
        mock_get.return_value = _json_response(payload)

        assert self._interface()._fetch_epex_spot_prices(48, START) == []


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

    def test_config_as_list_fallback(self):
        """Test defensive fallback when config is a list instead of dict.
        
        Regression test for issue #321: If config is somehow a list,
        calling .get() on it would raise AttributeError. The fix should
        gracefully handle this and use empty defaults.
        """
        config = ["not", "a", "dict"]  # Intentionally wrong type
        interface = FeedInPriceInterface(config, 3600, "UTC")

        # Should fall back to defaults without crashing
        assert interface.source == "fixed"
        assert interface.zone == "DK1"
        assert interface.fixed_price_ct_kwh == 0.0
        assert interface.multiplier == 1.0

    def test_config_as_none_fallback(self):
        """Test defensive fallback when config is None."""
        interface = FeedInPriceInterface(None, 3600, "UTC")

        # Should fall back to defaults without crashing
        assert interface.source == "fixed"
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


class TestFeedInPriceInterfaceNegativePrices:
    """Test negative price handling.

    Regression tests for the negative_price_switch feature that was
    lost during the migration from PriceInterface to FeedInPriceInterface.
    """

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_negative_price_clamping_elpris_dk(self, mock_get):
        """A negative Elpris hour is clamped to 0 when the switch is on."""
        day = [
            {"DKK_per_kWh": -0.11214, "EUR_per_kWh": -0.015, "EXR": 7.476273},
            {"DKK_per_kWh": 0.8523, "EUR_per_kWh": 0.114, "EXR": 7.476273},
        ]
        mock_get.side_effect = [_json_response(day), requests.HTTPError("404")]
        interface = _elpris_interface(
            static_adder_ct_kwh=0.0,
            multiplier=1.0,
            negative_price_switch=True,
        )

        prices = interface._retrieve_prices(2, START)

        assert prices[0] == 0.0
        assert prices[1] == pytest.approx(0.000114, abs=1e-12)

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_negative_price_no_clamping(self, mock_get):
        """With the switch off the negative hour survives as a negative price."""
        day = [
            {"DKK_per_kWh": -0.11214, "EUR_per_kWh": -0.015, "EXR": 7.476273},
            {"DKK_per_kWh": 0.8523, "EUR_per_kWh": 0.114, "EXR": 7.476273},
        ]
        mock_get.side_effect = [_json_response(day), requests.HTTPError("404")]
        interface = _elpris_interface(
            static_adder_ct_kwh=0.0,
            multiplier=1.0,
            negative_price_switch=False,
        )

        prices = interface._retrieve_prices(2, START)

        assert prices[0] == pytest.approx(-0.000015, abs=1e-12)
        assert prices[1] > 0


class TestFeedInPriceInterfaceResilience:
    """The price service must outlive a bad response.

    Issue #321: one unexpected payload raised out of the fetcher, skipped the
    whole last_successful_prices / default_prices chain, and ended the
    background thread for the lifetime of the process.
    """

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_bad_shape_falls_back_to_default_prices(self, mock_get):
        """current_feedin_prices gets defaults, not an empty list."""
        mock_get.side_effect = [_json_response({"foo": 1}),
                                _json_response({"foo": 1})]
        interface = _elpris_interface()

        interface.update_prices(48, START)

        assert len(interface.current_feedin_prices) == 48
        assert all(p == 0.000005 for p in interface.current_feedin_prices)

    @patch('src.interfaces.feed_in_price_interface.requests.get')
    def test_bad_shape_reuses_last_successful_prices(self, mock_get):
        """A good cycle followed by a bad one keeps the good prices."""
        mock_get.side_effect = [
            _json_response(ELPRIS_DAY1), requests.HTTPError("404"),
            _json_response("garbage"), _json_response("garbage"),
        ]
        interface = _elpris_interface()

        interface.update_prices(3, START)
        good = list(interface.current_feedin_prices)
        interface.update_prices(3, START)

        assert good[0] == 0.000114
        assert interface.current_feedin_prices == good

    def test_update_loop_survives_a_raising_update(self):
        """The loop keeps running after update_prices raises.

        The guard used to wrap the entire loop, so the first exception ended
        it — and the "restarting..." branch could never restart anything,
        because _start_update_service runs on the thread it would replace and
        its is_alive() guard declines.
        """
        interface = _elpris_interface()
        interface.update_interval = 0.01
        calls = []
        done = threading.Event()

        def flaky(_tgt_duration, _start_time=None):
            calls.append(len(calls))
            if len(calls) == 1:
                raise AttributeError("'list' object has no attribute 'get'")
            if len(calls) >= 3:
                done.set()

        interface.update_prices = flaky
        thread = threading.Thread(target=interface._update_prices_loop, daemon=True)
        thread.start()
        try:
            assert done.wait(timeout=5), f"loop stopped after {len(calls)} call(s)"
        finally:
            interface._stop_event.set()
            thread.join(timeout=5)

        # First call raised; the loop still reached the later ones.
        assert len(calls) >= 3

    def test_loop_does_not_call_start_update_service_from_itself(self):
        """The old self-restart was a no-op that only logged a promise."""
        interface = _elpris_interface()
        interface.update_prices = lambda *_args, **_kwargs: None
        interface._stop_event.set()  # exit immediately

        with patch.object(FeedInPriceInterface, "_start_update_service") as start:
            interface._update_prices_loop()

        start.assert_not_called()


class TestFeedInPriceInterfaceHorizonLength:
    """A price list must match the optimizer horizon exactly."""

    def test_extend_truncates_overlong_input(self):
        """DST days publish 23 or 25 hourly values; two days can be 49."""
        interface = _elpris_interface()

        extended = interface._extend_prices_to_duration([0.00008] * 49, 48)

        assert len(extended) == 48

    def test_extend_truncates_overlong_input_15min(self):
        """Same for the quarter-hour grid: 49 hours would expand to 196."""
        interface = _elpris_interface(time_frame_base=900)

        extended = interface._extend_prices_to_duration([0.00008] * 49, 192)

        assert len(extended) == 192

    def test_extend_fills_short_input_completely(self):
        """A source far shorter than the horizon still fills it.

        Cycling once only reached 2x the input length, so 24 quarter-hour
        slots became 48 instead of 192 — a short array the optimizer would
        read as a truncated forecast.
        """
        interface = _elpris_interface(time_frame_base=900)

        extended = interface._extend_prices_to_duration([0.00008] * 6, 192)

        assert len(extended) == 192
        assert all(p == 0.00008 for p in extended)

    def test_extend_handles_empty_input(self):
        """No prices in, no prices out — and no infinite loop."""
        interface = _elpris_interface()

        assert interface._extend_prices_to_duration([], 48) == []
