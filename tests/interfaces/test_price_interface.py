"""Tests for the Stromligning price interface integration."""

from datetime import datetime, timezone, timedelta


import pytest

from src.interfaces.price_interface import PriceInterface, STROMLIGNING_API_BASE

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


# =========================================================================
# price token YAML >- stripping
# =========================================================================


def _make_price_iface(token, monkeypatch):
    """Helper: build a PriceInterface with the given token, suppressing the update service."""
    monkeypatch.setattr(
        "src.interfaces.price_interface.PriceInterface."
        "_PriceInterface__start_update_service",
        lambda self: None,
    )
    cfg = {"source": "tibber", "token": token}
    return PriceInterface(cfg, 3600)


class TestPriceInterfaceTokenStripping:
    """Tests for YAML >- block-scalar whitespace stripping on the price token."""

    def test_leading_trailing_whitespace_stripped(self, monkeypatch, caplog):
        """token with surrounding whitespace is stripped and a warning is logged."""
        iface = _make_price_iface("  mytoken  ", monkeypatch)
        assert iface.access_token == "mytoken"
        assert "whitespace stripped" in caplog.text

    def test_newline_stripped(self, monkeypatch, caplog):
        """token with trailing newline from YAML >- is stripped."""
        iface = _make_price_iface("mytoken\n", monkeypatch)
        assert iface.access_token == "mytoken"
        assert "whitespace stripped" in caplog.text

    def test_internal_whitespace_warns(self, monkeypatch, caplog):
        """token with internal space logs an authentication-failure warning."""
        iface = _make_price_iface("part1 part2", monkeypatch)
        assert iface.access_token == "part1 part2"
        assert "internal whitespace" in caplog.text

    def test_clean_token_no_warning(self, monkeypatch, caplog):
        """Clean token produces no whitespace warning."""
        _make_price_iface("cleantoken", monkeypatch)
        assert "whitespace" not in caplog.text


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


# ============================================================================
# Energyforecast Smart Price Prediction Matrix Tests
# ============================================================================


def test_tibber_energyforecast_fallback_enabled(monkeypatch):
    """Test Tibber with energyforecast smart price prediction (tomorrow missing)."""
    # Simulate Tibber response with only today (no tomorrow)
    today = [
        {
            "total": 250 + i * 10,  # 25.0, 26.0, 27.0, 28.0 ct/kWh
            "energy": 0.1,
            "startsAt": f"2025-10-20T{i:02d}:00:00.000Z",
            "currency": "EUR",
        }
        for i in range(4)
    ]
    tibber_response = {
        "data": {
            "viewer": {
                "homes": [
                    {
                        "currentSubscription": {
                            "priceInfo": {"today": today, "tomorrow": []}
                        }
                    }
                ]
            }
        }
    }

    # Simulate energyforecast response (EPEX spot prices)
    energyforecast_data = {
        "data": [
            {
                "start": f"2025-10-20T{i:02d}:00:00.000Z",
                "end": f"2025-10-20T{i+1:02d}:00:00.000Z",
                "price_ct_per_kwh": 10.0 + i,
            }
            for i in range(8)  # 0-7 hours
        ]
    }

    def fake_post(url, headers=None, json=None, timeout=None):
        """Mock both Tibber and energyforecast API calls."""

        class R:
            def raise_for_status(self):
                return None

            def json(self):
                if "tibber" in url:
                    return tibber_response
                elif "energyforecast" in url:
                    return energyforecast_data
                return {}

        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.post", fake_post)
    monkeypatch.setattr(
        "src.interfaces.price_interface.requests.get",
        lambda url, params=None, timeout=None: fake_post(url),
    )
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    price_interface = PriceInterface(
        {
            "source": "tibber",
            "token": "dummy",
            "energyforecast_enabled": True,
            "energyforecast_token": "test_token",
            "energyforecast_market_zone": "DE-LU",
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        8, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )

    prices = price_interface.get_current_prices()
    # First 4 hours should be Tibber prices
    assert prices[0] == pytest.approx(0.250, rel=1e-9)  # 25.0 ct/kWh
    assert prices[1] == pytest.approx(0.260, rel=1e-9)  # 26.0 ct/kWh
    # Hours 4-7 should be populated by energyforecast prediction (not simple repetition)
    assert len(prices) == 8


def test_tibber_energyforecast_disabled(monkeypatch):
    """Test Tibber with energyforecast disabled (uses simple price repetition)."""
    today = [
        {
            "total": 250 + i * 10,
            "energy": 0.1,
            "startsAt": f"2025-10-20T{i:02d}:00:00.000Z",
            "currency": "EUR",
        }
        for i in range(4)
    ]
    tibber_response = {
        "data": {
            "viewer": {
                "homes": [
                    {
                        "currentSubscription": {
                            "priceInfo": {"today": today, "tomorrow": []}
                        }
                    }
                ]
            }
        }
    }

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return tibber_response

        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.post", fake_post)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    price_interface = PriceInterface(
        {
            "source": "tibber",
            "token": "dummy",
            "energyforecast_enabled": False,  # Disabled
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        8, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )

    prices = price_interface.get_current_prices()
    # First 4 hours from Tibber
    assert prices[0] == pytest.approx(0.250, rel=1e-9)
    # Hours 4-7 should be simple repetition of last known prices
    assert prices[4] == pytest.approx(0.250, rel=1e-9)  # Repeats first hour


def test_smartenergy_at_energyforecast_enabled(monkeypatch):
    """Test SmartEnergy AT with energyforecast smart price prediction."""
    # SmartEnergy AT response for today only (first 4 hours)
    smartenergy_data = {
        "data": [
            {
                "date": (datetime(2025, 10, 20, 0) + timedelta(hours=i)).isoformat(),
                "value": (20.0 + i) * 1000,  # API returns in 0.00001 euro units
            }
            for i in range(4)
        ]
    }

    energyforecast_data = {
        "data": [
            {
                "start": f"2025-10-20T{i:02d}:00:00.000Z",
                "end": f"2025-10-20T{i+1:02d}:00:00.000Z",
                "price_ct_per_kwh": 8.0 + i,
            }
            for i in range(8)
        ]
    }

    def fake_get(url, params=None, timeout=None):
        class R:
            def raise_for_status(self):
                return None

            def json(self):
                if "smartenergy" in url:
                    return smartenergy_data
                elif "energyforecast" in url:
                    return energyforecast_data
                return {}

        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    price_interface = PriceInterface(
        {
            "source": "smartenergy_at",
            "energyforecast_enabled": True,
            "energyforecast_token": "test_token",
            "energyforecast_market_zone": "AT",
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        8, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )

    prices = price_interface.get_current_prices()
    # First 4 hours from SmartEnergy AT
    assert prices[0] == pytest.approx(0.200, rel=1e-9)  # 20.0 ct/kWh
    assert prices[1] == pytest.approx(0.210, rel=1e-9)  # 21.0 ct/kWh
    assert prices[2] == pytest.approx(0.220, rel=1e-9)  # 22.0 ct/kWh
    assert prices[3] == pytest.approx(0.230, rel=1e-9)  # 23.0 ct/kWh
    # SmartEnergy AT creates 24-hour array, energyforecast won't trigger unless > 24
    assert len(prices) >= 4


def test_smartenergy_at_energyforecast_disabled(monkeypatch):
    """Test SmartEnergy AT with energyforecast disabled."""
    smartenergy_data = {
        "data": [
            {
                "date": (datetime(2025, 10, 20, 0) + timedelta(hours=i)).isoformat(),
                "value": (20.0 + i) * 1000,
            }
            for i in range(4)
        ]
    }

    def fake_get(url, params=None, timeout=None):
        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return smartenergy_data

        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    price_interface = PriceInterface(
        {
            "source": "smartenergy_at",
            "energyforecast_enabled": False,
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        8, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )

    prices = price_interface.get_current_prices()
    assert prices[0] == pytest.approx(0.200, rel=1e-9)
    assert prices[1] == pytest.approx(0.210, rel=1e-9)
    assert prices[2] == pytest.approx(0.220, rel=1e-9)
    assert prices[3] == pytest.approx(0.230, rel=1e-9)
    # SmartEnergy AT creates 24-hour array
    assert len(prices) >= 4


def test_stromligning_energyforecast_enabled(monkeypatch):
    """
    Test Stromligning with energyforecast enabled (should be blocked due to DKK currency).

    Smart price prediction currently only supports EUR-based price sources.
    Stromligning uses DKK, so energyforecast should NOT be called.
    Instead, simple price repetition should be used.
    """
    # Stromligning only has partial data
    stromligning_data = _build_sample_response()[:16]  # 4 hours of 15-min data

    energyforecast_called = False

    def fake_get(url, params=None, headers=None, timeout=None):
        nonlocal energyforecast_called

        class R:
            def raise_for_status(self):
                return None

            def json(self):
                if "stromligning" in url or "api.prod" in url:
                    return stromligning_data
                elif "energyforecast" in url:
                    energyforecast_called = True
                    # Should not be called due to currency check
                    return []
                return {}

        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    price_interface = PriceInterface(
        {
            "source": "stromligning",
            "token": "nke-elnet/forsyningen",
            "energyforecast_enabled": True,
            "energyforecast_token": "test_token",
            "energyforecast_market_zone": "DK1",
        },
        time_frame_base=900,  # 15-min
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        32, start_time=datetime(2025, 10, 20, 22, tzinfo=timezone.utc)
    )

    prices = price_interface.get_current_prices()
    # Should have prices populated using simple repetition instead of energyforecast
    assert len(prices) >= 16
    # Verify energyforecast was NOT called (blocked due to DKK currency)
    assert (
        not energyforecast_called
    ), "Energyforecast should be blocked for non-EUR currencies"


def test_akkudoktor_energyforecast_no_effect(monkeypatch):
    """Test that Akkudoktor source doesn't use energyforecast prediction."""
    # Akkudoktor has its own optimization-based pricing
    fake_values = [{"marketpriceEurocentPerKWh": 100000 + i * 10000} for i in range(48)]

    def fake_get(url, params=None, headers=None, timeout=None):
        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return {"values": fake_values}

        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.get", fake_get)
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    price_interface = PriceInterface(
        {
            "source": "default",  # Akkudoktor uses "default" source
            "energyforecast_enabled": True,  # Should have no effect for Akkudoktor
            "energyforecast_token": "test_token",
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )

    prices = price_interface.get_current_prices()
    expected = [round((100000 + i * 10000) / 100000, 9) for i in range(4)]
    assert prices[:4] == pytest.approx(expected, rel=1e-9)


def test_energyforecast_config_validation():
    """Test that energyforecast config is properly structured."""
    # Simple test to verify energyforecast config fields exist
    # The actual validation logic is tested through integration tests
    # and is called automatically in ConfigManager.load_config()

    # Test that PriceInterface properly handles energyforecast config
    monkeypatch_instance = pytest.MonkeyPatch()
    monkeypatch_instance.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    # Config with all energyforecast fields
    config_with_energyforecast = {
        "source": "tibber",
        "token": "test_token",
        "energyforecast_enabled": True,
        "energyforecast_token": "api_token",
        "energyforecast_market_zone": "DE-LU",
    }

    price_interface = PriceInterface(
        config_with_energyforecast,
        time_frame_base=3600,
        timezone=timezone.utc,
    )

    # Verify config was properly loaded
    assert price_interface.energyforecast_enabled is True
    assert price_interface.energyforecast_token == "api_token"
    assert price_interface.energyforecast_market_zone == "DE-LU"

    monkeypatch_instance.undo()


def test_energyforecast_insufficient_overlap(monkeypatch):
    """Test energyforecast prediction with insufficient overlapping data."""
    # Only 2 hours of overlap (less than MIN_OVERLAP_HOURS=6)
    today = [
        {
            "total": 250,
            "energy": 0.1,
            "startsAt": f"2025-10-20T{i:02d}:00:00.000Z",
            "currency": "EUR",
        }
        for i in range(2)
    ]
    tibber_response = {
        "data": {
            "viewer": {
                "homes": [
                    {
                        "currentSubscription": {
                            "priceInfo": {"today": today, "tomorrow": []}
                        }
                    }
                ]
            }
        }
    }

    energyforecast_data = {
        "data": [
            {
                "start": f"2025-10-20T{i:02d}:00:00.000Z",
                "end": f"2025-10-20T{i+1:02d}:00:00.000Z",
                "price_ct_per_kwh": 10.0,
            }
            for i in range(4)
        ]
    }

    def fake_post(url, headers=None, json=None, timeout=None):
        class R:
            def raise_for_status(self):
                return None

            def json(self):
                if "tibber" in url:
                    return tibber_response
                elif "energyforecast" in url:
                    return energyforecast_data
                return {}

        return R()

    monkeypatch.setattr("src.interfaces.price_interface.requests.post", fake_post)
    monkeypatch.setattr(
        "src.interfaces.price_interface.requests.get",
        lambda url, params=None, timeout=None: fake_post(url),
    )
    monkeypatch.setattr(
        PriceInterface, "_PriceInterface__start_update_service", lambda self: None
    )

    price_interface = PriceInterface(
        {
            "source": "tibber",
            "token": "dummy",
            "energyforecast_enabled": True,
            "energyforecast_token": "test_token",
            "energyforecast_market_zone": "DE-LU",
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )

    # Should handle gracefully (fallback to simple repetition when insufficient overlap)
    price_interface.update_prices(
        4, start_time=datetime(2025, 10, 20, 0, tzinfo=timezone.utc)
    )
    prices = price_interface.get_current_prices()
    assert len(prices) == 4
