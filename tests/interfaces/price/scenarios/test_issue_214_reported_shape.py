# pylint: disable=protected-access
"""
The payload shape actually reported in discussion #214.

TobiasB71's sensor is the EPEX Spot HACS integration fed from Energyforecast.de:
15-minute resolution, fields `start_time`/`end_time`/`price_per_kwh`, and the
series starts on the previous day and runs four days — so it is simultaneously
"starts yesterday", "longer than the horizon" and "15-min source". The builder
below reproduces that shape; prices encode their origin as 0.<day+1> + hour/1000,
with day index 1 (base 0.2) being "today" for the anchor the tests pass.

The field names are accepted (the normalizer reads the sensor's own
spellings); landing the values on the right slots is
`fix: align timeseries prices by timestamp` — the xfail below is the
acceptance case for that commit.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytz

from src.interfaces.price_interface import PriceInterface
from src.interfaces.timeseries_normalizer import normalize_entries

BERLIN = pytz.timezone("Europe/Berlin")
CEST = timezone(timedelta(hours=2))
# Local midnight of the series' second day — "today" from the reporter's view.
ANCHOR = datetime(2026, 10, 3, 0, 0, tzinfo=CEST)


@pytest.fixture(name="payload")
def payload_fixture():
    """Four whole days of 15-minute entries, starting yesterday, in the sensor's shape."""
    start = ANCHOR - timedelta(days=1)
    entries = []
    for i in range(4 * 96):
        begin = start + timedelta(minutes=15 * i)
        entries.append(
            {
                "start_time": begin.isoformat(),
                "end_time": (begin + timedelta(minutes=15)).isoformat(),
                "price_per_kwh": round(0.1 * (i // 96 + 1) + (i % 96) // 4 / 1000, 6),
            }
        )
    return entries


def test_the_reported_field_names_are_accepted(payload):
    """The sensor's own spellings parse without a renaming template."""
    entries = normalize_entries(payload, BERLIN)
    assert len(entries) == 384
    assert entries[0]["value"] == pytest.approx(0.1)
    assert entries[0]["start"].utcoffset() == timedelta(hours=2)


@pytest.mark.xfail(
    reason="timestamp placement lands with 'fix: align timeseries prices by timestamp'",
    strict=True,
)
def test_the_reported_payload_lands_on_todays_slots(payload, monkeypatch):
    """The acceptance case for timestamp placement, in the reporter's own shape."""
    monkeypatch.setattr(
        "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
        lambda self: None,
    )
    iface = PriceInterface(
        {
            "source": "timeseries",
            "data_url": "http://test.local/prices",
            "data_path": "data",
            "data_token": "",
        },
        time_frame_base=900,
        timezone=BERLIN,
    )
    prices = iface._PriceInterface__parse_price_timeseries(
        payload, 192, start_time=ANCHOR, value_unit="EUR/kWh"
    )
    assert len(prices) == 192
    # Slot 0 is today 00:00 (base 0.2 EUR/kWh -> EUR/Wh), not yesterday's 0.1.
    assert prices[0] == pytest.approx(0.2 / 1000)
    # Slot 4 is 01:00 today.
    assert prices[4] == pytest.approx(0.201 / 1000)
    # The second half of the window is tomorrow (base 0.3), not a padded tail.
    assert prices[96] == pytest.approx(0.3 / 1000)
