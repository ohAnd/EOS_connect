# pylint: disable=protected-access
"""
P4: probe green ⇔ the interface serves a non-fallback array.

The probe exists so "the test passed" means the running interface will succeed.
This file pins that equivalence across the window shapes from the #214 review:
for every payload, `analyse` saying ok must match the price interface actually
producing prices (rather than returning [] and falling back) — against the same
anchor.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.config_web.timeseries_probe import analyse
from src.interfaces.price_interface import PriceInterface

ANCHOR = datetime(2026, 8, 23, 0, 0, tzinfo=timezone(timedelta(hours=2)))


def hourly(day_offset, count):
    start = ANCHOR + timedelta(days=day_offset)
    return [
        {
            "start": (start + timedelta(hours=h)).isoformat(),
            "value": 0.25 + h / 1000,
        }
        for h in range(count)
    ]


SCENARIOS = [
    ("today_full", hourly(0, 24)),
    ("yesterday_and_today", hourly(-1, 48)),
    ("tomorrow_only", hourly(1, 24)),
    ("entirely_past", hourly(-3, 48)),
    ("twelve_hours", hourly(0, 12)),
    ("two_entries", hourly(0, 2)),
    ("single_entry", hourly(0, 1)),
    ("entirely_beyond_horizon", hourly(3, 24)),
]


@pytest.fixture(name="price_interface")
def price_interface_fixture(monkeypatch):
    """Timeseries-source interface, matching what the probe claims to predict."""
    monkeypatch.setattr(
        "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
        lambda self: None,
    )
    return PriceInterface(
        {
            "source": "timeseries",
            "data_url": "http://test.local/prices",
            "data_path": "data",
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )


@pytest.mark.parametrize("name,payload", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_probe_verdict_matches_the_interface_outcome(
    name, payload, price_interface
):
    probe_result = analyse(
        payload, "price", "EUR/kWh", "Europe/Berlin", anchor=ANCHOR
    )
    prices = price_interface._PriceInterface__parse_price_timeseries(
        payload, 48, start_time=ANCHOR, value_unit="EUR/kWh"
    )

    assert probe_result["ok"] == bool(prices), (
        f"{name}: probe says ok={probe_result['ok']} "
        f"({probe_result.get('error', '')}), interface returned "
        f"{len(prices)} prices"
    )
