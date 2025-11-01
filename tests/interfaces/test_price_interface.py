"""Tests for the Stromligning price interface integration."""

from datetime import datetime, timezone

import pytest

from src.interfaces.price_interface import PriceInterface, STROMLIGNING_API_BASE

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


def _build_sample_response():
    """Create a minimal Stromligning payload fixture."""
    # First 16 entries (4 hours) from sample_response.json
    base = [
        ("2025-10-20T22:00:00.000Z", 2.132412),
        ("2025-10-20T22:15:00.000Z", 1.991901),
        ("2025-10-20T22:30:00.000Z", 1.879959),
        ("2025-10-20T22:45:00.000Z", 1.805363),
        ("2025-10-20T23:00:00.000Z", 1.896951),
        ("2025-10-20T23:15:00.000Z", 1.844108),
        ("2025-10-20T23:30:00.000Z", 1.776420),
        ("2025-10-20T23:45:00.000Z", 1.635256),
        ("2025-10-21T00:00:00.000Z", 1.813112),
        ("2025-10-21T00:15:00.000Z", 1.703971),
        ("2025-10-21T00:30:00.000Z", 1.669427),
        ("2025-10-21T00:45:00.000Z", 1.566541),
        ("2025-10-21T01:00:00.000Z", 1.679790),
        ("2025-10-21T01:15:00.000Z", 1.588948),
        ("2025-10-21T01:30:00.000Z", 1.543481),
        ("2025-10-21T01:45:00.000Z", 1.539093),
    ]
    return [{"date": date, "price": price, "resolution": "15m"} for date, price in base]


class DummyResponse:
    """Minimal requests.Response stub for tests."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_stromligning_hourly_aggregation(monkeypatch):
    sample_payload = _build_sample_response()

    expected_url = (
        f"{STROMLIGNING_API_BASE}&productId=velkommen_gron_el"
        "&supplierId=radius_c&customerGroupId=c"
    )

    def fake_get(url, headers=None, timeout=None):
        # pylint: disable=unused-argument
        assert url.startswith(f"{expected_url}&forecast=true&to=")
        to_segment = url.split("&to=", 1)[1]
        datetime.strptime(to_segment, "%Y-%m-%dT%H:%M")
        return DummyResponse(sample_payload)

    monkeypatch.setattr(
        "src.interfaces.price_interface.requests.get",
        fake_get,
    )
    monkeypatch.setattr(
        PriceInterface,
        "_PriceInterface__start_update_service",
        lambda self: None,
    )

    price_interface = PriceInterface(
        {
            "source": "stromligning",
            "token": "radius_c/velkommen_gron_el/c",
            "feed_in_price": 0,
            "negative_price_switch": False,
        },
        timezone=timezone.utc,
    )

    assert price_interface._stromligning_url == expected_url

    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 22, tzinfo=timezone.utc)
    )

    expected_hourly_prices = [
        round(0.00195240875, 9),
        round(0.00178818375, 9),
        round(0.00168826275, 9),
        round(0.001587828, 9),
    ]

    assert price_interface.get_current_prices() == pytest.approx(
        expected_hourly_prices, rel=1e-9
    )
    assert price_interface.current_prices_direct == pytest.approx(
        expected_hourly_prices, rel=1e-9
    )
    assert price_interface.get_current_feedin_prices() == [0.0] * 4


@pytest.mark.parametrize(
    "token,expected_query",
    [
        (
            "radius_c/velkommen_gron_el/c",
            "productId=velkommen_gron_el&supplierId=radius_c&customerGroupId=c",
        ),
        (
            "nke-elnet/forsyningen",
            "productId=forsyningen&supplierId=nke-elnet",
        ),
    ],
)
def test_stromligning_token_parsing(monkeypatch, token, expected_query):
    """Validate that the token config param for Stromligning becomes the expected query string."""
    monkeypatch.setattr(
        PriceInterface,
        "_PriceInterface__start_update_service",
        lambda self: None,
    )

    price_interface = PriceInterface(
        {
            "source": "stromligning",
            "token": token,
            "feed_in_price": 0,
            "negative_price_switch": False,
        },
        timezone=timezone.utc,
    )

    assert price_interface._stromligning_url == (
        f"{STROMLIGNING_API_BASE}&{expected_query}"
    )


@pytest.mark.parametrize(
    "token",
    [
        "",
        "radius_c",
        "radius_c/velkommen_gron_el/extra/segment",
        "radius_c//velkommen_gron_el",
    ],
)
def test_stromligning_token_parsing_invalid(monkeypatch, token):
    """Invalid token value for Stromligning should trigger the default price source."""
    monkeypatch.setattr(
        PriceInterface,
        "_PriceInterface__start_update_service",
        lambda self: None,
    )

    price_interface = PriceInterface(
        {
            "source": "stromligning",
            "token": token,
            "feed_in_price": 0,
            "negative_price_switch": False,
        },
        timezone=timezone.utc,
    )

    assert price_interface.src == "default"
    assert price_interface._stromligning_url is None
