# pylint: disable=protected-access
"""
End-to-end parsing of the payloads reported in discussion #214.

The canonical timeseries format is EVCC's, because that is the format users already
have — either straight from an EVCC endpoint or from a Home Assistant template sensor
written to feed EVCC. These tests use the payloads from the thread verbatim so the
reported symptoms stay pinned:

  - EVCC /api/tariff/grid was read as EUR/Wh and rendered 34660 ct/kWh
  - Tibber / EPEX HA integrations were rejected outright over field names
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.interfaces.price_interface import PriceInterface

# Values quoted in the discussion, in EUR/kWh.
TIBBER_PRICES_EUR_KWH = [0.3811, 0.3727, 0.3668]


@pytest.fixture(name="price_interface")
def fixture_price_interface(monkeypatch):
    """Price interface configured for the unified timeseries source."""
    monkeypatch.setattr(
        "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
        lambda self: None,
    )
    return PriceInterface(
        {
            "source": "timeseries",
            "data_url": "http://evcc.local/api/tariff/grid",
            "data_path": "rates",
        },
        time_frame_base=3600,
        timezone=timezone.utc,
    )


def _quarter_hourly(values, count=192, start=None):
    """Build a 15-min series repeating *values*, starting at midnight UTC today."""
    base = start or datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return [
        {
            "start": (base + timedelta(minutes=15 * i)).isoformat(),
            "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": values[i % len(values)],
        }
        for i in range(count)
    ]


def _to_ct_kwh(price_eur_per_wh):
    """Same conversion the schedule table applies (schedule.js)."""
    return price_eur_per_wh * 100_000


class TestEvccRatesPayload:
    """The reported symptom: EVCC's own format rendered 34660 ct/kWh."""

    def test_evcc_eur_per_kwh_lands_at_a_realistic_ct_per_kwh(self, price_interface):
        rates = _quarter_hourly(TIBBER_PRICES_EUR_KWH)

        prices = price_interface._PriceInterface__parse_price_timeseries(
            rates, 48, value_unit="EUR/kWh"
        )

        assert prices, "EVCC's canonical payload must parse"
        first_ct_kwh = _to_ct_kwh(prices[0])
        assert 30.0 < first_ct_kwh < 45.0, f"got {first_ct_kwh} ct/kWh"

    def test_the_reported_34660_value_no_longer_occurs(self, price_interface):
        rates = _quarter_hourly([0.3466])

        prices = price_interface._PriceInterface__parse_price_timeseries(
            rates, 48, value_unit="EUR/kWh"
        )

        assert _to_ct_kwh(prices[0]) == pytest.approx(34.66, abs=0.01)

    def test_reading_the_same_payload_as_eur_per_wh_still_warns(
        self, price_interface, caplog
    ):
        rates = _quarter_hourly(TIBBER_PRICES_EUR_KWH)

        with caplog.at_level("WARNING"):
            price_interface._PriceInterface__parse_price_timeseries(
                rates, 48, value_unit="EUR/Wh"
            )

        assert "implausible price level" in caplog.text

    def test_missing_end_is_accepted(self, price_interface):
        # EVCC supplies `end`; the Tibber HA integration does not. Neither is read.
        rates = [
            {key: value for key, value in entry.items() if key != "end"}
            for entry in _quarter_hourly(TIBBER_PRICES_EUR_KWH)
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(
            rates, 48, value_unit="EUR/kWh"
        )

        assert len(prices) == 48


class TestHomeAssistantIntegrationPayloads:
    """
    Foreign field names are rejected — but the message has to say what to do.

    The adaptation path is a HA template sensor (as agreed in the thread), so the
    error names the fields that were present instead of guessing at them.
    """

    def test_tibber_hacs_attributes_rejected_with_actionable_message(
        self, price_interface, caplog
    ):
        # Verbatim from #214: start_time + price_per_kwh, no end.
        payload = [
            {"start_time": "2026-08-23T00:00:00+02:00", "price_per_kwh": 0.3811},
            {"start_time": "2026-08-23T00:15:00+02:00", "price_per_kwh": 0.3727},
        ]

        with caplog.at_level("ERROR"):
            prices = price_interface._PriceInterface__parse_price_timeseries(
                payload, 48, value_unit="EUR/kWh"
            )

        assert prices == []
        assert "'start_time'" in caplog.text
        assert "'price_per_kwh'" in caplog.text
        assert "timeseries-templates" in caplog.text

    def test_epex_spot_attributes_rejected_with_actionable_message(
        self, price_interface, caplog
    ):
        # Verbatim from #214: start_time + end_time + price_per_kwh.
        payload = [
            {
                "start_time": "2026-08-22T00:00:00+02:00",
                "end_time": "2026-08-22T00:15:00+02:00",
                "price_per_kwh": 0.1766,
            },
        ]

        with caplog.at_level("ERROR"):
            prices = price_interface._PriceInterface__parse_price_timeseries(
                payload, 48, value_unit="EUR/kWh"
            )

        assert prices == []
        assert "'end_time'" in caplog.text

    def test_tibber_action_attributes_rejected_with_actionable_message(
        self, price_interface, caplog
    ):
        # Verbatim from #214: the built-in Tibber integration's action response.
        payload = [
            {"start_time": "2026-08-23T00:00:00.000+02:00", "price": 0.3811},
        ]

        with caplog.at_level("ERROR"):
            prices = price_interface._PriceInterface__parse_price_timeseries(
                payload, 48, value_unit="EUR/kWh"
            )

        assert prices == []
        assert "'price'" in caplog.text

    def test_a_template_shaped_payload_is_accepted(self, price_interface):
        """What the documented HA template snippet produces: start + value only."""
        base = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        payload = [
            {
                "start": (base + timedelta(minutes=15 * i)).isoformat(),
                "value": TIBBER_PRICES_EUR_KWH[i % 3],
            }
            for i in range(192)
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(
            payload, 48, value_unit="EUR/kWh"
        )

        assert len(prices) == 48
        assert 30.0 < _to_ct_kwh(prices[0]) < 45.0


class TestUnitEscapeHatch:
    """value_unit exists so a source that is not in EUR/kWh needs no template."""

    @pytest.mark.parametrize(
        "unit,raw",
        [("EUR/kWh", 0.3811), ("ct/kWh", 38.11), ("EUR/Wh", 0.0003811)],
    )
    def test_all_units_reach_the_same_schedule_value(self, price_interface, unit, raw):
        prices = price_interface._PriceInterface__parse_price_timeseries(
            _quarter_hourly([raw]), 48, value_unit=unit
        )

        assert _to_ct_kwh(prices[0]) == pytest.approx(38.11, abs=0.01)


class TestEvccInternalPathUnaffected:
    """
    The EVCC adapter pre-converts to EUR/Wh and passes value_unit=None.

    That path must keep working untouched, otherwise fixing the generic source would
    break the source it is modelled on.
    """

    def test_pre_normalized_entries_are_not_converted_again(self, price_interface):
        base = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Already EUR/Wh, exactly as __retrieve_prices_from_evcc builds it.
        entries = [
            {
                "start": (base + timedelta(minutes=15 * i)).isoformat(),
                "end": None,
                "value": 0.0003811,
            }
            for i in range(192)
        ]

        prices = price_interface._PriceInterface__parse_price_timeseries(entries, 48)

        assert _to_ct_kwh(prices[0]) == pytest.approx(38.11, abs=0.01)


class TestFullFetchPath:
    """
    Covers the wiring, not just the parser.

    The parser tests pass value_unit explicitly; these confirm the interface reads it
    from config (and defaults correctly) and carries it through the real fetch path.
    """

    def _rates_response(self, values):
        base = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rates = [
            {
                "start": (base + timedelta(minutes=15 * i)).isoformat(),
                "end": (base + timedelta(minutes=15 * (i + 1))).isoformat(),
                "value": values[i % len(values)],
            }
            for i in range(192)
        ]
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"rates": rates}
        return response, base

    def test_unit_defaults_to_evcc_semantics(self, price_interface):
        assert price_interface.value_unit == "EUR/kWh"

    def test_evcc_endpoint_end_to_end(self, price_interface):
        response, base = self._rates_response([0.3811])

        with patch("requests.get", return_value=response):
            prices = price_interface._PriceInterface__retrieve_prices_from_url(48, base)

        assert len(prices) == 48
        assert _to_ct_kwh(prices[0]) == pytest.approx(38.11, abs=0.01)

    def test_configured_unit_is_honoured_end_to_end(self, monkeypatch):
        monkeypatch.setattr(
            "src.interfaces.price_interface.PriceInterface._PriceInterface__start_update_service",
            lambda self: None,
        )
        iface = PriceInterface(
            {
                "source": "timeseries",
                "data_url": "http://ha.local/api/states/sensor.prices",
                "data_path": "rates",
                "value_unit": "ct/kWh",
            },
            time_frame_base=3600,
            timezone=timezone.utc,
        )
        response, base = self._rates_response([38.11])

        with patch("requests.get", return_value=response):
            prices = iface._PriceInterface__retrieve_prices_from_url(48, base)

        assert _to_ct_kwh(prices[0]) == pytest.approx(38.11, abs=0.01)


class TestNoSilentSlotShift:
    """
    A malformed entry must never shift the remaining prices.

    The hourly path maps entries to slots positionally, so skipping a bad entry would
    move every later price an hour early — cheaper-looking power at the wrong time,
    with nothing in the schedule to show for it.
    """

    def _hourly_series(self, bad_index=None, bad_value=None):
        base = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        series = []
        for hour in range(48):
            value = (10 + hour) / 100.0
            if bad_index is not None and hour == bad_index:
                value = bad_value
            series.append(
                {"start": (base + timedelta(hours=hour)).isoformat(), "value": value}
            )
        return series

    def test_clean_series_maps_one_to_one(self, price_interface):
        prices = price_interface._PriceInterface__parse_price_timeseries(
            self._hourly_series(), 48, value_unit="EUR/kWh"
        )

        assert [round(_to_ct_kwh(p), 1) for p in prices[4:9]] == [14.0, 15.0, 16.0, 17.0, 18.0]

    def test_null_midway_rejects_instead_of_shifting(self, price_interface):
        prices = price_interface._PriceInterface__parse_price_timeseries(
            self._hourly_series(bad_index=5, bad_value=None), 48, value_unit="EUR/kWh"
        )

        assert prices == []

    def test_rejection_leaves_the_previous_prices_in_place(self, price_interface):
        """An unusable payload must fall back, not overwrite good data."""
        good = self._hourly_series()
        price_interface.last_successful_prices = (
            price_interface._PriceInterface__parse_price_timeseries(
                good, 48, value_unit="EUR/kWh"
            )
        )
        cached = list(price_interface.last_successful_prices)

        price_interface._PriceInterface__parse_price_timeseries(
            self._hourly_series(bad_index=5, bad_value=None), 48, value_unit="EUR/kWh"
        )

        assert price_interface.last_successful_prices == cached
