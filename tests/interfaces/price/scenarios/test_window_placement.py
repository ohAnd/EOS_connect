# pylint: disable=protected-access
"""
Characterization of how the price timeseries path places values on the slot grid.

The hourly/matching-resolution path maps payload entries to slots BY POSITION,
not by timestamp (discussion #214): entry 0 lands on slot 0 no matter when it
starts, a missing hour shifts everything after it, and the optimization window
is never checked. The plain tests here pin that behaviour so the extraction and
anchor refactors in between can prove they changed nothing; the xfail tests
state the timestamp-placed behaviour that `fix: align timeseries prices by
timestamp` makes true — removing their markers is that commit's acceptance.

Values are EUR/Wh and encode their origin as 0.<day><hour>: yesterday's hour h
is 0.100 + h/1000, today's is 0.200 + h/1000, tomorrow's 0.300 + h/1000 — so a
mis-placed slot names the day it was taken from.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.interfaces.price_interface import PriceInterface

ANCHOR = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)  # local midnight "today"


def hourly_entries(day_offset, hours, base):
    """Hourly entries starting *day_offset* days from the anchor, value base + h/1000."""
    start = ANCHOR + timedelta(days=day_offset)
    return [
        {
            "start": (start + timedelta(hours=h)).isoformat(),
            "end": (start + timedelta(hours=h + 1)).isoformat(),
            "value": round(base + h / 1000, 9),
        }
        for h in hours
    ]


def seven_days(price_entries_builder=hourly_entries):
    """A week of hourly entries, one base value per day (0.200 … 0.800)."""
    return [
        entry
        for day in range(7)
        for entry in price_entries_builder(day, range(24), 0.200 + day / 10)
    ]


@pytest.fixture(name="price_interface")
def price_interface_fixture(monkeypatch):
    """Hourly-system price interface with the timeseries source configured."""
    monkeypatch.setattr(
        "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
        lambda self: None,
    )
    return PriceInterface(
        {
            "source": "timeseries",
            "data_url": "http://test.local/prices",
            "data_path": "data",
            "data_token": "",
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )


def parse(price_interface, entries):
    """Run the private parser with the anchor every production caller passes."""
    return price_interface._PriceInterface__parse_price_timeseries(
        entries, 48, start_time=ANCHOR
    )


class TestTodaysPositionalPlacement:
    """Pins the current positional mapping. Rewritten by the timestamp-placement fix."""

    def test_a_series_starting_yesterday_fills_slot_zero_with_yesterday(
        self, price_interface
    ):
        """The #214 report: a source that also publishes the previous day is a day late."""
        entries = hourly_entries(-1, range(24), 0.100) + hourly_entries(
            0, range(24), 0.200
        )
        prices = parse(price_interface, entries)
        assert prices[0] == pytest.approx(0.100)

    def test_a_missing_hour_shifts_every_later_price(self, price_interface):
        """Slot 5 (= 05:00) carries the 06:00 price; everything after is an hour early."""
        entries = hourly_entries(0, [h for h in range(24) if h != 5], 0.200)
        prices = parse(price_interface, entries)
        assert prices[5] == pytest.approx(0.206)
        assert prices[6] == pytest.approx(0.207)

    def test_a_series_entirely_tomorrow_is_served_as_today(self, price_interface):
        """The window is never checked: tomorrow's prices pass as today's."""
        prices = parse(price_interface, hourly_entries(1, range(48), 0.300))
        assert prices[0] == pytest.approx(0.300)

    def test_a_series_entirely_in_the_past_is_served_as_today(self, price_interface):
        """A source frozen three days ago still answers as if it were current."""
        prices = parse(price_interface, hourly_entries(-3, range(48), 0.100))
        assert prices[0] == pytest.approx(0.100)

    def test_a_short_series_is_padded_with_its_last_value(self, price_interface):
        """The tail is flat-filled and nothing marks it as invented."""
        prices = parse(price_interface, hourly_entries(0, range(24), 0.200))
        assert len(prices) == 48
        assert prices[24:] == [pytest.approx(0.223)] * 24

    def test_an_overlong_series_is_returned_uncut(self, price_interface):
        """168 hourly entries come back as 168 prices: pad-only, never trimmed (W7)."""
        prices = parse(price_interface, seven_days())
        assert len(prices) == 168


XFAIL_REASON = "timestamp placement lands with 'fix: align timeseries prices by timestamp'"


@pytest.mark.xfail(reason=XFAIL_REASON, strict=True)
class TestTimestampPlacementContract:
    """What the same inputs must produce once values are placed by timestamp."""

    def test_a_series_starting_yesterday_lands_on_todays_slots(self, price_interface):
        """Yesterday's 24 entries are dropped; slot 0 carries today's 00:00 price."""
        entries = hourly_entries(-1, range(24), 0.100) + hourly_entries(
            0, range(24), 0.200
        )
        prices = parse(price_interface, entries)
        assert prices[0] == pytest.approx(0.200)
        assert prices[23] == pytest.approx(0.223)

    def test_a_missing_hour_does_not_shift_later_prices(self, price_interface):
        """Entries after a gap stay on the slots their timestamps name."""
        entries = hourly_entries(0, [h for h in range(24) if h != 5], 0.200)
        prices = parse(price_interface, entries)
        assert prices[4] == pytest.approx(0.204)
        assert prices[6] == pytest.approx(0.206)

    def test_a_series_entirely_tomorrow_is_rejected(self, price_interface):
        """No entry touches today: [] lets the caller's fallback chain serve last good data."""
        assert parse(price_interface, hourly_entries(1, range(48), 0.300)) == []

    def test_a_series_entirely_in_the_past_is_rejected(self, price_interface):
        """A stale series must not be served as current."""
        assert parse(price_interface, hourly_entries(-3, range(48), 0.100)) == []

    def test_an_overlong_series_is_trimmed_to_the_horizon(self, price_interface):
        """Entries beyond the 48-slot window are dropped, not handed to the optimizer."""
        prices = parse(price_interface, seven_days())
        assert len(prices) == 48
        assert prices[0] == pytest.approx(0.200)


class TestHorizonUnits:
    """`tgt_duration` means hours in the update loop and slots in the parser."""

    @pytest.fixture(name="quarter_hour_interface")
    def quarter_hour_interface_fixture(self, monkeypatch):
        """15-minute-system price interface with the timeseries source configured."""
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        return PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://test.local/prices",
                "data_path": "data",
                "data_token": "",
            },
            time_frame_base=900,
            timezone=timezone.utc,
        )

    def _force_fallback(self, iface, monkeypatch):
        """Prime 192 cached slots, fail the fetch, update the way the daemon does."""
        monkeypatch.setattr(
            iface,
            "_PriceInterface__retrieve_prices_from_url",
            lambda tgt_duration, start_time=None: [],
        )
        iface.last_successful_prices = [0.0001] * 192
        iface.last_successful_prices_direct = [0.0001] * 192
        iface.consecutive_failures = 0
        # The update loop at price_interface.py:238 passes a literal 48
        # ("hours") while hot_reload passes 192 ("slots") to the same method.
        iface.update_prices(48, ANCHOR)

    def test_the_update_loops_literal_48_truncates_the_fallback(
        self, quarter_hour_interface, monkeypatch
    ):
        """Today a fallback on a 15-minute system serves 48 of the 192 cached slots."""
        self._force_fallback(quarter_hour_interface, monkeypatch)
        assert len(quarter_hour_interface.get_current_prices()) == 48

    @pytest.mark.xfail(
        reason="unified slot horizon lands with 'fix: express the price horizon in slots'",
        strict=True,
    )
    def test_a_fallback_on_a_15_minute_system_keeps_all_192_slots(
        self, quarter_hour_interface, monkeypatch
    ):
        """Once the horizon is expressed in slots everywhere, nothing truncates it."""
        self._force_fallback(quarter_hour_interface, monkeypatch)
        assert len(quarter_hour_interface.get_current_prices()) == 192
