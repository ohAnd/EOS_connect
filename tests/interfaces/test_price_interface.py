"""Tests for the Stromligning price interface integration."""

from datetime import datetime, timezone, timedelta


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
        """
        Mimics the behavior of the requests.Response.raise_for_status() method.

        This method does nothing and always returns None, simulating a successful HTTP response
        without raising an exception for HTTP error codes.
        """
        return None

    def json(self):
        """
        Return the payload as a JSON object.

        Returns:
            dict: The payload stored in the instance.
        """
        return self._payload


def test_stromligning_hourly_aggregation(monkeypatch):
    """
    Test the hourly aggregation logic of the Stromligning price interface.
    This test verifies that:
    - The constructed Stromligning API URL matches the expected format.
    - The API call is made with the correct parameters, including the 'forecast' and
     'to' query parameters.
    - The response is correctly parsed and hourly prices are aggregated as expected.
    - The `get_current_prices`, `current_prices_direct`, and `get_current_feedin_prices`
      methods return the correct values.
    Mocks:
    - The `requests.get` method is monkeypatched to return a dummy response with sample
      payload data.
    - The `_PriceInterface__start_update_service` method is monkeypatched to prevent side effects
      during testing.
    Assertions:
    - The generated Stromligning URL matches the expected URL.
    - The hourly prices returned by the interface match the expected values with high precision.
    - The feed-in prices are correctly set to zero for all hours.
    """
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
        time_frame_base=3600,
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


def test_stromligning_quarter_hour_aggregation(monkeypatch):
    """
    Test the 15-minute aggregation logic of the Stromligning price interface.
    This test verifies that:
    - The constructed Stromligning API URL matches the expected format.
    - The API call is made with the correct parameters, including the 'forecast' and
      'to' query parameters.
    - The response is correctly parsed and 15-minute prices are aggregated as expected.
    - The `get_current_prices`, `current_prices_direct`, and `get_current_feedin_prices`
      methods return the correct values.
    Mocks:
    - The `requests.get` method is monkeypatched to return a dummy response with sample
      payload data.
    - The `_PriceInterface__start_update_service` method is monkeypatched to prevent side effects
      during testing.
    Assertions:
    - The generated Stromligning URL matches the expected URL.
    - The 15-minute prices returned by the interface match the expected values with high precision.
    - The feed-in prices are correctly set to zero for all intervals.
    """
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
        time_frame_base=900,
        timezone=timezone.utc,
    )

    assert price_interface._stromligning_url == expected_url

    # Request 4 hours (should yield 16 intervals for 15-min resolution)
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 22, tzinfo=timezone.utc)
    )

    # Convert ct/kWh to €/kWh (divide by 1000)
    expected_15min_prices = [round(p["price"] / 1000, 9) for p in sample_payload]

    assert price_interface.get_current_prices() == pytest.approx(
        expected_15min_prices, rel=1e-9
    )
    assert price_interface.current_prices_direct == pytest.approx(
        expected_15min_prices, rel=1e-9
    )
    assert price_interface.get_current_feedin_prices() == [0.0] * 16


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
        time_frame_base=3600,
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
        time_frame_base=3600,
        timezone=timezone.utc,
    )

    assert price_interface.src == "default"
    assert price_interface._stromligning_url is None


def test_akkudoktor_hourly(monkeypatch):
    """Test Akkudoktor API price retrieval (hourly)."""
    # Simulate 48 hourly prices (2 days)
    fake_values = [{"marketpriceEurocentPerKWh": 100000 + i * 1000} for i in range(48)]

    def fake_get(url, timeout=None):
        class R:
            """Mock response for Akkudoktor API tests."""

            def raise_for_status(self):
                """Simulate successful HTTP response (no error)."""
                return None

            def json(self):
                """Return a dictionary containing fake values."""
                return {"values": fake_values}

        assert "akkudoktor" in url
        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )
    price_interface = PriceInterface(
        {"source": "default"}, time_frame_base=3600, timezone=timezone.utc
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    expected = [round((100000 + i * 1000) / 100000, 9) for i in range(4)]
    assert price_interface.get_current_prices() == pytest.approx(expected, rel=1e-9)


def test_akkudoktor_quarter_hour(monkeypatch):
    """Test Akkudoktor API price retrieval (15min)."""
    # Simulate 48 hourly prices (2 days)
    fake_values = [{"marketpriceEurocentPerKWh": 100000 + i * 1000} for i in range(48)]

    def fake_get(url, timeout=None):
        class R:
            """
            Mock response class for simulating HTTP responses in tests.
            """

            def raise_for_status(self):
                """Simulate successful HTTP response (no error)."""
                return None

            def json(self):
                """Return a dictionary containing fake values for testing purposes."""
                return {"values": fake_values}

        assert "akkudoktor" in url
        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )
    price_interface = PriceInterface(
        {"source": "default"}, time_frame_base=900, timezone=timezone.utc
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    # Each hour is split into 4 equal 15min intervals
    expected = [round((100000 + i // 4 * 1000) / 100000, 9) for i in range(16)]
    assert price_interface.get_current_prices() == pytest.approx(expected, rel=1e-9)


def test_tibber_hourly(monkeypatch):
    """Test Tibber API price retrieval (hourly)."""
    # Simulate Tibber GraphQL response
    today = [
        {
            "total": 0.2 + i * 0.01,
            "energy": 0.1,
            "startsAt": f"2025-10-20T{i:02d}:00:00Z",
            "currency": "EUR",
        }
        for i in range(4)
    ]
    tomorrow = []
    fake_response = {
        "data": {
            "viewer": {
                "homes": [
                    {
                        "currentSubscription": {
                            "priceInfo": {"today": today, "tomorrow": tomorrow}
                        }
                    }
                ]
            }
        }
    }

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            """Mock response class for simulating HTTP requests in tests."""

            def raise_for_status(self):
                """Simulate successful HTTP response (no error)."""
                return None

            def json(self):
                """Return fake Tibber GraphQL response for testing."""
                return fake_response

        assert "tibber" in url
        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.post", fake_post)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )
    price_interface = PriceInterface(
        {"source": "tibber", "token": "dummy"},
        time_frame_base=3600,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    expected = [round((0.2 + i * 0.01) / 1000, 9) for i in range(4)]
    assert price_interface.get_current_prices() == pytest.approx(expected, rel=1e-9)


def test_tibber_quarter_hour(monkeypatch):
    """Test Tibber API price retrieval (15min)."""
    # Simulate Tibber GraphQL response with 16 quarter-hourly prices
    today = [
        {
            "total": 0.2 + i * 0.01,
            "energy": 0.1,
            "startsAt": f"2025-10-20T{(i // 4):02d}:{((i % 4) * 15):02d}:00Z",
            "currency": "EUR",
        }
        for i in range(16)
    ]
    tomorrow = []
    fake_response = {
        "data": {
            "viewer": {
                "homes": [
                    {
                        "currentSubscription": {
                            "priceInfo": {"today": today, "tomorrow": tomorrow}
                        }
                    }
                ]
            }
        }
    }

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            """Test double for an HTTP response providing raise_for_status() and json()."""

            def raise_for_status(self):
                """Mock method that does nothing when called."""
                return None

            def json(self):
                """Return the fake response as JSON."""
                return fake_response

        assert "tibber" in url
        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.post", fake_post)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )
    price_interface = PriceInterface(
        {"source": "tibber", "token": "dummy"},
        time_frame_base=900,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    expected = [round((0.2 + i * 0.01) / 1000, 9) for i in range(16)]
    actual = price_interface.get_current_prices()
    assert actual[:16] == pytest.approx(expected, rel=1e-9)


def test_smartenergy_at_hourly(monkeypatch):
    """Test SmartEnergy AT API price retrieval (hourly)."""
    # Simulate 4 hourly prices
    fake_data = [
        {
            "date": (datetime(2025, 10, 20, 0) + timedelta(hours=i)).isoformat(),
            "value": 0.15 + i * 0.01,
        }
        for i in range(4)
    ]

    def fake_get(url, headers=None, timeout=None):
        class R:
            """Mock response class for simulating API calls in tests."""

            def raise_for_status(self):
                """Mock method that does nothing when called."""
                return None

            def json(self):
                """Return fake data as a JSON-like dictionary."""
                return {"data": fake_data}

        assert "smartenergy" in url
        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )
    price_interface = PriceInterface(
        {"source": "smartenergy_at"}, time_frame_base=3600, timezone=timezone.utc
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    expected = [round((0.15 + i * 0.01) / 100000, 9) for i in range(4)]
    actual = price_interface.get_current_prices()
    assert actual[:4] == pytest.approx(expected, rel=1e-9)


def test_smartenergy_at_quarter_hour(monkeypatch):
    """Test SmartEnergy AT API price retrieval (15min)."""
    # Simulate 16 quarter-hourly prices
    fake_data = [
        {
            "date": (datetime(2025, 10, 20, 0) + timedelta(minutes=15 * i)).isoformat(),
            "value": 0.15 + i * 0.01,
        }
        for i in range(16)
    ]

    def fake_get(url, headers=None, timeout=None):
        class R:
            """Mock response class for simulating HTTP requests in tests."""

            def raise_for_status(self):
                """Mock method to simulate HTTP response status check; does nothing."""
                return None

            def json(self):
                """Return a dictionary with fake data."""
                return {"data": fake_data}

        assert "smartenergy" in url
        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )
    price_interface = PriceInterface(
        {"source": "smartenergy_at"}, time_frame_base=900, timezone=timezone.utc
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    expected = [round((0.15 + i * 0.01) / 100000, 9) for i in range(16)]
    actual = price_interface.get_current_prices()
    assert actual[:16] == pytest.approx(expected, rel=1e-9)


def test_fixed_24h_array_hourly(monkeypatch):
    """Test fixed 24h array price retrieval."""
    fixed_array = [10.0 + i for i in range(24)]
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )
    price_interface = PriceInterface(
        {"source": "fixed_24h", "fixed_24h_array": fixed_array},
        time_frame_base=3600,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    expected = [round((10.0 + i) / 100000, 9) for i in range(4)]
    actual = price_interface.get_current_prices()
    assert actual[:4] == pytest.approx(expected, rel=1e-9)


def test_fixed_24h_array_quarter_hour(monkeypatch):
    """Test fixed 24h array price retrieval (15min)."""
    fixed_array = [10.0 + i for i in range(24)]
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )
    price_interface = PriceInterface(
        {"source": "fixed_24h", "fixed_24h_array": fixed_array},
        time_frame_base=900,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    # Each hour is split into 4 equal 15min intervals
    expected = [round((10.0 + i // 4) / 100000, 9) for i in range(16)]
    actual = price_interface.get_current_prices()
    assert actual[:16] == pytest.approx(expected, rel=1e-9)
