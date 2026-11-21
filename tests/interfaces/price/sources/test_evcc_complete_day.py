"""A whole day from EVCC still leaves tomorrow to guess.

EVCC often publishes one calendar day at a time: today from midnight, with
tomorrow arriving in the early afternoon. That day is trustworthy data, so the
EVCC path treats it as complete and fills tomorrow by repeating it.

Repeating is the right guess only while there is nothing better. With
energyforecast.de configured there is, and the user configured it for exactly
this gap. These tests pin that the prediction gets its turn, and that the
repetition is still what everything falls back to.
"""

from datetime import datetime, timedelta
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from src.interfaces.price_interface import PriceInterface

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access

ZONE = ZoneInfo("Europe/Berlin")
ANCHOR = datetime(2026, 11, 10, 0, 0, tzinfo=ZONE)
SLOTS_PER_DAY = 96
HORIZON = 192

# The relationship hidden in the data: what the tariff makes of the spot price.
FACTOR = 1.19
OFFSET = 0.26


def _epex(slot):
    """EUR/kWh for one slot of the horizon; the two days do not look alike.

    Tomorrow has to differ from today, or repeating today would score as well
    as a learned prediction and the measurement would say nothing.
    """
    if slot < SLOTS_PER_DAY:
        return 0.06 + 0.012 * ((slot * 5) % 9)
    return 0.09 + 0.015 * ((slot * 7) % 11)


def _retail(slot):
    """What EVCC charges for that slot, in EUR/kWh."""
    return FACTOR * _epex(slot) + OFFSET


def _evcc_rates(slot_count):
    """Quarter-hourly rates from midnight, the way EVCC serves whole days."""
    rates = []
    for slot in range(slot_count):
        start = ANCHOR + timedelta(seconds=slot * 900)
        rates.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(seconds=900)).isoformat(),
                "value": _retail(slot),
            }
        )
    return rates


def _prediction():
    """48 hours of spot prices, spelled the way the live API answers."""
    return [
        {
            "start": (ANCHOR + timedelta(seconds=slot * 900)).isoformat(
                timespec="milliseconds"
            ),
            "end": (ANCHOR + timedelta(seconds=(slot + 1) * 900)).isoformat(
                timespec="milliseconds"
            ),
            "price": _epex(slot),
            "price_origin": "market",
        }
        for slot in range(HORIZON)
    ]


def _make(monkeypatch, rates, enabled=True, prediction=None):
    """A PriceInterface on EVCC, with the energyforecast calls recorded."""
    payload = _prediction() if prediction is None else prediction
    seen = []

    class Response:
        """Stands in for both APIs; the URL decides which one answered."""

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            """The stub never fails."""

        def json(self):
            """The recorded payload."""
            return self.body

    def fake_get(url, **_kwargs):
        if "/api/tariff/grid" in url:
            return Response({"rates": rates})
        seen.append(url)
        return Response(payload)

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    config = {
        "source": "evcc",
        "energyforecast_enabled": enabled,
        "energyforecast_token": "test_token",
        "energyforecast_market_zone": "DE-LU",
    }
    iface = PriceInterface(
        config,
        time_frame_base=900,
        timezone=ZONE,
        evcc_interface=Mock(url="http://evcc.local:7070"),
    )
    return iface, seen


class TestAWholeDayStillLeavesTomorrowToGuess:
    """One calendar day covers half the horizon; the other half is a guess."""

    def test_the_prediction_gets_its_turn(self, monkeypatch):
        """Tomorrow comes from what today taught, not from copying today."""
        iface, seen = _make(monkeypatch, _evcc_rates(SLOTS_PER_DAY))

        iface.update_prices(HORIZON, start_time=ANCHOR)

        prices = iface.get_current_prices()
        metadata = iface.get_forecast_metadata()
        assert len(prices) == HORIZON
        assert metadata["forecast_type"] == "smart_forecast"
        assert metadata["forecast_source"] == "energyforecast.de"
        assert len(seen) == 1
        expected = [_retail(slot) / 1000 for slot in range(SLOTS_PER_DAY, HORIZON)]
        assert prices[SLOTS_PER_DAY:] == pytest.approx(expected, rel=1e-6)

    def test_todays_own_slots_are_left_alone(self, monkeypatch):
        """The learning must not write over the rates EVCC actually sent."""
        iface, _ = _make(monkeypatch, _evcc_rates(SLOTS_PER_DAY))

        iface.update_prices(HORIZON, start_time=ANCHOR)

        prices = iface.get_current_prices()
        expected = [_retail(slot) / 1000 for slot in range(SLOTS_PER_DAY)]
        assert prices[:SLOTS_PER_DAY] == pytest.approx(expected, rel=1e-9)

    def test_without_the_option_tomorrow_repeats_today(self, monkeypatch):
        """Nothing configured, nothing asked - and the old policy stands."""
        iface, seen = _make(monkeypatch, _evcc_rates(SLOTS_PER_DAY), enabled=False)

        iface.update_prices(HORIZON, start_time=ANCHOR)

        prices = iface.get_current_prices()
        assert iface.get_forecast_metadata()["forecast_type"] == "simple_repetition"
        assert not seen
        assert prices[SLOTS_PER_DAY:] == prices[:SLOTS_PER_DAY]

    def test_a_prediction_that_does_not_arrive_keeps_todays_pattern(
        self, monkeypatch, caplog
    ):
        """Two runs, so yesterday's array is there to be picked by mistake.

        Fallback 2 serves what the previous run stored, and before midnight
        that is yesterday's curve - worse than today's for guessing tomorrow.
        A whole day repeated has to stay ahead of it.
        """
        iface, _ = _make(monkeypatch, _evcc_rates(SLOTS_PER_DAY), prediction=[])

        iface.update_prices(HORIZON, start_time=ANCHOR)
        assert len(iface.last_successful_prices) == HORIZON

        # The first run complained about the empty answer, which is fair and
        # throttled to once an hour. What must not come back is an alert about
        # the filling itself, on this run and every one after it.
        caplog.clear()
        with caplog.at_level("WARNING"):
            iface.update_prices(HORIZON, start_time=ANCHOR)

        prices = iface.get_current_prices()
        metadata = iface.get_forecast_metadata()
        assert metadata["forecast_type"] == "simple_repetition"
        assert metadata["forecast_source"] is None
        assert prices[SLOTS_PER_DAY:] == prices[:SLOTS_PER_DAY]
        # An ordinary day for this shape, not something to alert on every run.
        assert caplog.text == ""

    def test_two_whole_days_are_not_asked_about(self, monkeypatch):
        """Nothing is missing, so there is nothing to predict."""
        iface, seen = _make(monkeypatch, _evcc_rates(HORIZON))

        iface.update_prices(HORIZON, start_time=ANCHOR)

        prices = iface.get_current_prices()
        assert iface.get_forecast_metadata()["forecast_type"] == "all_real"
        assert not seen
        expected = [_retail(slot) / 1000 for slot in range(HORIZON)]
        assert prices == pytest.approx(expected, rel=1e-9)
