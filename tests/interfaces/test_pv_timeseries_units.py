# pylint: disable=protected-access
"""
PV timeseries unit handling, driven by the reference sensor from discussion #214.

bennobiber's Home Assistant template sensor is the sensor the thread asked EOS Connect
to read: it emits `pv_estimate * 1000`, i.e. power in W per slot, which is what EVCC's
solar forecast carries. Reading those values as Wh-per-slot inflated 15-minute data
fourfold, so these tests pin the conversion and the timestamp handling around it.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import pytz

from src.interfaces.pv_interface import PvInterface

TIME_FRAME_BASE_HOURLY = 3600
TIME_FRAME_BASE_15MIN = 900


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    """Avoid starting the real background update thread during tests."""

    class DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("threading.Thread", DummyThread)


def make_pv_interface(source_config=None, time_frame_base=TIME_FRAME_BASE_HOURLY):
    source_config = source_config or {"source": "timeseries"}
    return PvInterface(
        source_config,
        [{"name": "test_array", "power": 4000}],
        time_frame_base,
        {},
        timezone="UTC",
    )


def midnight_today_utc():
    return datetime.now(pytz.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def bennobiber_payload(watts=4000.0, count=192, aware=True):
    """
    The shape bennobiber's template produces: start/end/value with value in W.

    With ``aware=False`` the timestamps are rendered the way his template actually
    renders them (``| timestamp_utc``), i.e. UTC wall clock with no offset.
    """
    base = midnight_today_utc()
    entries = []
    for i in range(count):
        start = base + timedelta(minutes=15 * i)
        end = start + timedelta(minutes=15)
        entries.append(
            {
                "start": start.isoformat() if aware else start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.isoformat() if aware else end.strftime("%Y-%m-%d %H:%M:%S"),
                "value": watts,
            }
        )
    return entries


class TestPowerToEnergyConversion:
    """4000 W held across an hour is 4000 Wh — not 16000."""

    def test_quarter_hourly_watts_summed_to_hourly_watt_hours(self):
        pv = make_pv_interface()

        values = pv._PvInterface__parse_pv_timeseries(
            bennobiber_payload(watts=4000.0), 48, value_unit="W"
        )

        assert values[0] == pytest.approx(4000.0)

    def test_reading_watts_as_watt_hours_is_the_fourfold_error(self):
        """Guards the regression: the old behaviour summed four W values as Wh."""
        pv = make_pv_interface()

        values = pv._PvInterface__parse_pv_timeseries(
            bennobiber_payload(watts=4000.0), 48, value_unit="Wh"
        )

        assert values[0] == pytest.approx(16000.0)

    def test_hourly_watts_need_no_scaling(self):
        pv = make_pv_interface()
        base = midnight_today_utc()
        payload = [
            {
                "start": (base + timedelta(hours=h)).isoformat(),
                "value": 4000.0,
            }
            for h in range(48)
        ]

        values = pv._PvInterface__parse_pv_timeseries(payload, 48, value_unit="W")

        assert values[0] == pytest.approx(4000.0)

    def test_kilowatt_source_scaled(self):
        pv = make_pv_interface()

        values = pv._PvInterface__parse_pv_timeseries(
            bennobiber_payload(watts=4.0), 48, value_unit="kW"
        )

        assert values[0] == pytest.approx(4000.0)

    def test_unknown_unit_is_rejected_not_guessed(self, caplog):
        pv = make_pv_interface()

        with caplog.at_level("ERROR"):
            values = pv._PvInterface__parse_pv_timeseries(
                bennobiber_payload(), 48, value_unit="MW"
            )

        assert values == []
        assert "unknown PV unit" in caplog.text


class TestReferenceSensorAcceptance:
    """
    The acceptance criterion from the plan: bennobiber's sensor reads correctly.

    Unit and shape come straight from his template; only the timestamp rendering has
    to be corrected to carry an offset, which is what the documented snippet does.
    """

    def test_documented_snippet_shape_lands_on_the_right_slots(self):
        pv = make_pv_interface()

        values = pv._PvInterface__parse_pv_timeseries(
            bennobiber_payload(watts=4000.0, aware=True), 48, value_unit="W"
        )

        assert len(values) == 48
        assert all(value == pytest.approx(4000.0) for value in values)

    def test_timestamp_utc_rendering_is_flagged_not_silently_shifted(self, caplog):
        pv = make_pv_interface()

        with caplog.at_level("WARNING"):
            pv._PvInterface__parse_pv_timeseries(
                bennobiber_payload(aware=False), 48, value_unit="W"
            )

        assert "no UTC offset" in caplog.text


class TestEmptyWindowIsReported:
    """48 zeros used to be indistinguishable from a genuinely dark forecast."""

    def test_data_outside_the_window_warns(self, caplog):
        pv = make_pv_interface()
        far_off = midnight_today_utc() + timedelta(days=30)
        payload = [
            {
                "start": (far_off + timedelta(hours=h)).isoformat(),
                "value": 4000.0,
            }
            for h in range(48)
        ]

        with caplog.at_level("WARNING"):
            values = pv._PvInterface__parse_pv_timeseries(payload, 48, value_unit="W")

        assert values == [0.0] * 48
        assert "None of" in caplog.text
        assert "window starting" in caplog.text


class TestEvccPvPathUnaffected:
    """The EVCC adapter pre-converts to Wh and passes value_unit=None."""

    def test_pre_normalized_entries_are_not_converted_again(self):
        pv = make_pv_interface()
        base = midnight_today_utc()
        entries = [
            {
                "start": (base + timedelta(hours=h)).isoformat(),
                "end": None,
                "value": 4000.0,
            }
            for h in range(48)
        ]

        values = pv._PvInterface__parse_pv_timeseries(entries, 48)

        assert values[0] == pytest.approx(4000.0)


class TestFullFetchPath:
    """Covers config → attribute → parser wiring, not just the parser."""

    def _response(self, payload):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    def test_bennobiber_sensor_read_end_to_end(self):
        """
        The acceptance criterion from #214: EOS Connect reads the sensor EVCC reads.

        A constant 4000 W feed is 4000 Wh per hour — 96 kWh/day. Reading the same
        values as Wh-per-slot would report 16000 Wh/h.
        """
        pv = make_pv_interface(
            {
                "source": "timeseries",
                "data_url": "http://homeassistant.local:8123/api/states/sensor.solcast_evcc_json",
                "data_path": "attributes.data",
                # value_unit omitted on purpose: must default to W
            }
        )
        pv.configuration_valid = True
        payload = {"attributes": {"data": bennobiber_payload(watts=4000.0)}}

        with patch("requests.get", return_value=self._response(payload)):
            forecast = pv._PvInterface__get_pv_forecast_timeseries(48)

        assert len(forecast) == 48
        assert forecast[0] == pytest.approx(4000.0)
        assert sum(forecast[:24]) == pytest.approx(96_000.0)

    def test_configured_unit_is_honoured_end_to_end(self):
        pv = make_pv_interface(
            {
                "source": "timeseries",
                "data_url": "http://ha.local/api/states/sensor.pv",
                "data_path": "attributes.data",
                "value_unit": "kW",
            }
        )
        pv.configuration_valid = True
        payload = {"attributes": {"data": bennobiber_payload(watts=4.0)}}

        with patch("requests.get", return_value=self._response(payload)):
            forecast = pv._PvInterface__get_pv_forecast_timeseries(48)

        assert forecast[0] == pytest.approx(4000.0)


class TestPvPlausibilityWarning:
    """Unlike prices, a PV unit mistake is subtle — it needs a warning of its own."""

    def test_energy_unit_on_a_power_source_warns(self, caplog):
        pv = make_pv_interface()  # configured for a 4000 W array

        with caplog.at_level("WARNING"):
            pv._PvInterface__parse_pv_timeseries(
                bennobiber_payload(watts=4000.0), 48, value_unit="Wh"
            )

        assert "implausible PV level" in caplog.text

    def test_correct_unit_produces_no_warning(self, caplog):
        pv = make_pv_interface()

        with caplog.at_level("WARNING"):
            pv._PvInterface__parse_pv_timeseries(
                bennobiber_payload(watts=4000.0), 48, value_unit="W"
            )

        assert "implausible PV level" not in caplog.text

    def test_active_unit_is_stated_once(self, caplog):
        pv = make_pv_interface()

        with caplog.at_level("INFO"):
            pv._PvInterface__parse_pv_timeseries(
                bennobiber_payload(), 48, value_unit="W"
            )
            first = caplog.text.count("interpreted as")
            pv._PvInterface__parse_pv_timeseries(
                bennobiber_payload(), 48, value_unit="W"
            )

        assert first == 1
        assert caplog.text.count("interpreted as") == 1
