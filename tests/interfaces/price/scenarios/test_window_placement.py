# pylint: disable=protected-access
"""
Window placement of the price timeseries path.

Prices land on the slot grid by their timestamps (intensive overlap placement):
an entry's position in the payload means nothing, a missing hour does not shift
anything, and a series that never touches the optimization window is rejected
so the fallback chain serves the last good data. Slots the series does not
cover are filled by fill_price_gaps and a filled tail is reported through the
forecast metadata, so the UI can hatch it.

Values are EUR/Wh and encode their origin as 0.<day><hour>: yesterday's hour h
is 0.100 + h/1000, today's is 0.200 + h/1000, tomorrow's 0.300 + h/1000 — so a
mis-placed slot names the day it was taken from.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.interfaces.price_interface import PriceInterface
from src.timeseries_grid import horizon_slots

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


def seven_days():
    """A week of hourly entries, one base value per day (0.200 … 0.800)."""
    return [
        entry
        for day in range(7)
        for entry in hourly_entries(day, range(24), 0.200 + day / 10)
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
    """Run the parser with the anchor every production caller passes."""
    return price_interface._PriceInterface__parse_price_timeseries(
        entries, 48, start_time=ANCHOR
    )


class TestTimestampPlacement:
    """The payload's order means nothing; its timestamps mean everything."""

    def test_a_series_starting_yesterday_lands_on_todays_slots(self, price_interface):
        """The #214 report: yesterday's entries are dropped, not served as today."""
        entries = hourly_entries(-1, range(24), 0.100) + hourly_entries(
            0, range(24), 0.200
        )
        prices = parse(price_interface, entries)
        assert prices[0] == pytest.approx(0.200)
        assert prices[23] == pytest.approx(0.223)
        assert len(prices) == 48

    def test_a_missing_hour_does_not_shift_later_prices(self, price_interface):
        """Entries after a gap stay on the slots their timestamps name."""
        entries = hourly_entries(0, [h for h in range(24) if h != 5], 0.200)
        prices = parse(price_interface, entries)
        assert prices[4] == pytest.approx(0.204)
        assert prices[6] == pytest.approx(0.206)

    def test_an_interior_hole_is_filled_forward_and_warned_about(
        self, price_interface, caplog
    ):
        """The hole carries the last known price, and the log says so."""
        entries = hourly_entries(0, [h for h in range(24) if h != 5], 0.200)
        with caplog.at_level("WARNING"):
            prices = parse(price_interface, entries)
        assert prices[5] == pytest.approx(0.204)
        assert "filled from their neighbours" in caplog.text

    def test_a_series_entirely_in_the_past_is_rejected(self, price_interface):
        """A stale series must not be served as current — the fallback takes over."""
        assert parse(price_interface, hourly_entries(-3, range(48), 0.100)) == []

    def test_a_series_entirely_tomorrow_backfills_today(self, price_interface):
        """Tomorrow's series covers slots 24-47; today is backfilled, not shifted."""
        prices = parse(price_interface, hourly_entries(1, range(24), 0.300))
        assert len(prices) == 48
        assert prices[0] == pytest.approx(0.300)  # first known price, backwards
        assert prices[24] == pytest.approx(0.300)
        assert prices[47] == pytest.approx(0.323)

    def test_an_overlong_series_is_trimmed_to_the_horizon(self, price_interface):
        """Entries beyond the 48-slot window are dropped, not handed to the optimizer."""
        prices = parse(price_interface, seven_days())
        assert len(prices) == 48
        assert prices[0] == pytest.approx(0.200)
        assert prices[47] == pytest.approx(0.323)


class TestForecastMetadata:
    """A filled tail is marked so the UI can hatch it (the missing N3 signal)."""

    def test_a_short_series_marks_where_the_repetition_starts(self, price_interface):
        prices = parse(price_interface, hourly_entries(0, range(24), 0.200))
        assert len(prices) == 48
        # Tomorrow repeats today's pattern...
        assert prices[24] == pytest.approx(0.200)
        assert prices[47] == pytest.approx(0.223)
        # ...and says so.
        metadata = price_interface.get_forecast_metadata()
        assert metadata["forecast_start_index"] == 24
        assert metadata["forecast_type"] == "simple_repetition"

    def test_a_full_series_reports_all_real(self, price_interface):
        parse(price_interface, hourly_entries(0, range(24), 0.200)
              + hourly_entries(1, range(24), 0.300))
        metadata = price_interface.get_forecast_metadata()
        assert metadata["forecast_start_index"] is None
        assert metadata["forecast_type"] == "all_real"


class TestHorizonUnits:
    """`tgt_duration` counts slots on every path."""

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
        iface.consecutive_failures = 0
        # Mirrors the update loop: tgt_duration counts slots on every path.
        iface.update_prices(horizon_slots(iface.time_frame_base), ANCHOR)

    def test_a_fallback_on_a_15_minute_system_keeps_all_192_slots(
        self, quarter_hour_interface, monkeypatch
    ):
        """The horizon is expressed in slots everywhere; nothing truncates it."""
        self._force_fallback(quarter_hour_interface, monkeypatch)
        assert len(quarter_hour_interface.get_current_prices()) == 192
