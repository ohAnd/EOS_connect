# pylint: disable=protected-access
"""
fixed_price_adder_ct and relative_price_multiplier apply on every EXCHANGE source.

The schema has always offered both fields, but they were applied inside the
Akkudoktor fetch only — a silent no-op on exactly the sources (EPEX-style
timeseries, smartenergy_at) whose raw exchange prices need the grid fee the
most. They now run centrally in __retrieve_prices, in the canonical order
adder → multiplier → clamp, and the fallback cache stores the adjusted values
so nothing is ever adjusted twice.

"Every source" stops at EXCHANGE_PRICE_SOURCES. Tibber reports price["total"],
Stromligning is queried with ?lean=true, and EVCC's /api/tariff/grid has the
user's own tariff applied — those three already include the fee, levies and VAT.
fixed_24h is a figure the user typed by hand. Adding the adder to any of them
would charge the grid fee twice, so TestFinalTariffSourcesAreUntouched pins that
they come through unchanged.
"""

from datetime import datetime, timezone

import pytest

from src.interfaces.price_interface import PriceInterface

ANCHOR = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(name="iface")
def iface_fixture(monkeypatch):
    """Timeseries-source interface with an adder and a multiplier configured."""
    monkeypatch.setattr(
        "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
        lambda self: None,
    )
    return PriceInterface(
        {
            "source": "timeseries",
            "data_url": "http://test.local/prices",
            "data_path": "data",
            "fixed_price_adder_ct": 2.0,  # 2 ct/kWh = 0.00002 EUR/Wh
            "relative_price_multiplier": 0.10,
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )


def _fetch(iface, monkeypatch, raw_prices):
    """Serve *raw_prices* as the source result and run one update."""
    monkeypatch.setattr(
        iface,
        "_PriceInterface__retrieve_prices_from_url",
        lambda tgt_duration, start_time=None: list(raw_prices),
    )
    iface.update_prices(48, ANCHOR)
    return iface.get_current_prices()


class TestAdjustmentsOnEverySource:
    def test_adder_then_multiplier_on_the_timeseries_source(self, iface, monkeypatch):
        """(price + adder) * (1 + multiplier) — N1: no longer Akkudoktor-only."""
        prices = _fetch(iface, monkeypatch, [0.0002] * 48)
        assert prices[0] == pytest.approx((0.0002 + 0.00002) * 1.10)

    def test_a_negative_exchange_price_keeps_the_fee_arithmetic(
        self, iface, monkeypatch
    ):
        """-5 ct plus a 2 ct fee is -3 ct, and the multiplier applies to that."""
        prices = _fetch(iface, monkeypatch, [-0.00005] * 48)
        assert prices[0] == pytest.approx((-0.00005 + 0.00002) * 1.10)

    def test_the_clamp_runs_after_the_adjustments(self, iface, monkeypatch, caplog):
        """A multiplied price beyond 1.0 EUR/Wh is clamped, with one warning."""
        with caplog.at_level("WARNING"):
            prices = _fetch(iface, monkeypatch, [0.95] * 48)
        assert prices[0] == 1.0  # (0.95 + 0.00002) * 1.1 > 1.0
        assert "clamping" in caplog.text.lower()

    def test_the_fallback_serves_adjusted_prices_unchanged(self, iface, monkeypatch):
        """The cache stores adjusted values; a failed fetch must not adjust again."""
        first = _fetch(iface, monkeypatch, [0.0002] * 48)
        monkeypatch.setattr(
            iface,
            "_PriceInterface__retrieve_prices_from_url",
            lambda tgt_duration, start_time=None: [],
        )
        iface.update_prices(48, ANCHOR)
        assert iface.get_current_prices() == pytest.approx(first)

    def test_defaults_change_nothing(self, monkeypatch):
        """adder 0 / multiplier 0 is the identity, bit for bit."""
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
        prices = _fetch(iface, monkeypatch, [0.0002, 0.0003] + [0.0002] * 46)
        assert prices[0] == 0.0002
        assert prices[1] == 0.0003


class TestAkkudoktorAppliesOnce:
    """The move must not double-count on the path that already applied them."""

    def test_market_price_gets_the_adder_exactly_once(self, monkeypatch):
        fake_values = [{"marketpriceEurocentPerKWh": 10.0}] * 48  # 10 ct/kWh

        def fake_get(url, timeout=None):
            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"values": fake_values}

            assert "akkudoktor" in url
            return Response()

        monkeypatch.setattr(
            "src.interfaces.price_interface.requests.get", fake_get
        )
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "default",
                "fixed_price_adder_ct": 1.0,
                "relative_price_multiplier": 0.0,
            },
            time_frame_base=3600,
            timezone=timezone.utc,
        )
        iface.update_prices(48, ANCHOR)
        # 10 ct market + 1 ct fee = 11 ct/kWh = 0.00011 EUR/Wh — not 0.00012.
        assert iface.get_current_prices()[0] == pytest.approx(0.00011)


class TestFinalTariffSourcesAreUntouched:
    """
    A source that already serves a final tariff must ignore the adder entirely.

    Not an oversight but the point: these four report what the meter costs, so a
    grid fee on top is double-counting. Pinned per source because the central
    call site is one line away from catching all of them again.
    """

    @staticmethod
    def _interface(monkeypatch, source, extra=None):
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        config = {
            "source": source,
            "fixed_price_adder_ct": 2.0,
            "relative_price_multiplier": 0.10,
        }
        config.update(extra or {})
        return PriceInterface(config, time_frame_base=3600, timezone=timezone.utc)

    @pytest.mark.parametrize(
        "source, fetcher, extra",
        [
            # Both of these fall back to "default" without a usable token, which
            # would quietly turn the test into an Akkudoktor run.
            (
                "tibber",
                "_PriceInterface__retrieve_prices_from_tibber",
                {"token": "tibber-test-token"},
            ),
            (
                "stromligning",
                "_PriceInterface__retrieve_prices_from_stromligning",
                {"token": "supplier-1/product-2"},
            ),
            ("evcc", "_PriceInterface__retrieve_prices_from_evcc", {}),
        ],
    )
    def test_a_final_tariff_source_keeps_its_own_prices(
        self, monkeypatch, source, fetcher, extra
    ):
        iface = self._interface(monkeypatch, source, extra)
        assert iface.src == source, "config rejected; the test would not test it"
        monkeypatch.setattr(
            iface, fetcher, lambda tgt_duration, start_time=None: [0.0002] * 48
        )
        iface.update_prices(48, ANCHOR)
        # Untouched: (0.0002 + 0.00002) * 1.1 would be 0.000242.
        assert iface.get_current_prices()[0] == pytest.approx(0.0002)

    def test_fixed_24h_keeps_the_array_the_user_typed(self, monkeypatch):
        """20 ct/kWh typed in stays 20 ct/kWh served."""
        iface = self._interface(
            monkeypatch, "fixed_24h", {"fixed_24h_array": [20.0] * 24}
        )
        iface.update_prices(48, ANCHOR)
        assert iface.get_current_prices()[0] == pytest.approx(0.0002)

    def test_an_unsupported_source_serves_the_placeholder_unadjusted(
        self, monkeypatch
    ):
        """The 10 ct/kWh placeholder is not an exchange price either."""
        iface = self._interface(monkeypatch, "nonexistent_source")
        iface.update_prices(48, ANCHOR)
        prices = iface.get_current_prices()
        assert len(prices) == 48
        assert prices[0] == pytest.approx(0.0001)


class TestTheCacheNeverCompounds:
    """
    last_successful_prices stores ADJUSTED values, so no path may re-adjust it.

    The EVCC history fallback splices the cache straight into the array it
    returns, and that array used to flow back through the central adjustment —
    so every 15-minute cycle multiplied the spliced tail again, reaching the
    EUR/Wh clamp in about a day. EVCC is no longer an exchange source, which
    removes the loop; these tests keep it removed.
    """

    @staticmethod
    def _evcc(monkeypatch, **overrides):
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        config = {
            "source": "evcc",
            "fixed_price_adder_ct": 2.0,
            "relative_price_multiplier": 0.10,
        }
        config.update(overrides)
        return PriceInterface(config, time_frame_base=3600, timezone=timezone.utc)

    def test_repeated_incomplete_cycles_do_not_drift(self, monkeypatch):
        """Full grid once, then short grids forever: the tail must stay put."""
        iface = self._evcc(monkeypatch)
        state = {"count": 48}
        monkeypatch.setattr(
            iface,
            "_PriceInterface__retrieve_prices_from_evcc",
            lambda tgt_duration, start_time=None: [0.0002] * state["count"],
        )

        served = []
        for _ in range(8):
            iface.update_prices(48, ANCHOR)
            served.append(iface.get_current_prices()[-1])
            state["count"] = 30  # incomplete from here on -> history fallback

        assert served == pytest.approx([0.0002] * 8)

    def test_a_source_change_drops_the_previous_cache(self, monkeypatch):
        """Switching source must not inherit the old source's adjusted prices."""
        iface = self._evcc(monkeypatch, source="timeseries", data_url="http://x", data_path="d")
        monkeypatch.setattr(
            iface,
            "_PriceInterface__retrieve_prices_from_url",
            lambda tgt_duration, start_time=None: [0.0002] * 48,
        )
        iface.update_prices(48, ANCHOR)
        # timeseries is an exchange source, so the cache is adjusted.
        assert iface.last_successful_prices[0] == pytest.approx(0.000242)

        iface.drop_fallback_cache()
        assert iface.last_successful_prices == []
        assert iface.consecutive_failures == 0
