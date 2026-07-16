# pylint: disable=protected-access
"""
Tests for the PV "timeseries" data source: fetch, resolution detection,
15-min-to-hourly conversion, and parsing/validation.

Mirrors tests/interfaces/test_timeseries_parsing.py (PriceInterface), adapted
for PV semantics: values are energy-per-slot (Wh), 15-min slots are summed
(not averaged) when converted to hourly, incomplete data is padded with 0 (no
production) instead of the last known value, and - critically, matching
__get_pv_forecast_evcc_api's existing behaviour - entries are aligned to the
array by absolute timestamp starting at local midnight today (not
positionally / not starting from "now"), since get_ems_data() in
eos_connect.py indexes pv_forcast_array by slots-since-midnight.
"""

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytz
import pytest

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


def midnight_today_utc():
    return datetime.now(pytz.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def make_pv_interface(source_config=None, config=None, time_frame_base=TIME_FRAME_BASE_HOURLY):
    source_config = source_config or {"source": "timeseries"}
    config = config if config is not None else [{"name": "test_array", "power": 4000}]
    return PvInterface(source_config, config, time_frame_base, {}, timezone="UTC")


class TestPvTimeseriesFormatValidation:
    """Strict format validation for incoming PV timeseries data."""

    def test_valid_hourly_format(self):
        pv = make_pv_interface()
        base = midnight_today_utc()
        timeseries = [
            {"start": (base + timedelta(hours=h)).isoformat(), "end": (base + timedelta(hours=h + 1)).isoformat(), "value": 100.0}
            for h in range(48)
        ]

        values = pv._PvInterface__parse_pv_timeseries(timeseries, 48)
        assert values is not None
        assert len(values) == 48
        assert all(isinstance(v, float) for v in values)
        assert all(v == 100.0 for v in values)

    def test_missing_start_field(self):
        pv = make_pv_interface()
        timeseries = [{"end": "2024-01-01T01:00:00Z", "value": 100.0}]
        assert pv._PvInterface__parse_pv_timeseries(timeseries, 48) == []

    def test_missing_value_field(self):
        pv = make_pv_interface()
        timeseries = [{"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z"}]
        assert pv._PvInterface__parse_pv_timeseries(timeseries, 48) == []

    def test_not_a_list(self):
        pv = make_pv_interface()
        assert pv._PvInterface__parse_pv_timeseries({"data": "not a list"}, 48) == []

    def test_empty_list(self):
        pv = make_pv_interface()
        assert pv._PvInterface__parse_pv_timeseries([], 48) == []

    def test_negative_value_clamped_to_zero(self):
        pv = make_pv_interface()
        base = midnight_today_utc()
        timeseries = [
            {"start": (base + timedelta(hours=h)).isoformat(), "end": (base + timedelta(hours=h + 1)).isoformat(), "value": -50.0}
            for h in range(48)
        ]
        values = pv._PvInterface__parse_pv_timeseries(timeseries, 48)
        assert all(v == 0.0 for v in values)


class TestPvResolutionDetection:
    def test_detect_hourly_resolution(self):
        pv = make_pv_interface()
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": 100.0},
            {"start": "2024-01-01T01:00:00Z", "end": "2024-01-01T02:00:00Z", "value": 200.0},
        ]
        assert pv._PvInterface__detect_pv_timeseries_resolution(timeseries) == 3600

    def test_detect_15min_resolution(self):
        pv = make_pv_interface()
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:15:00Z", "value": 100.0},
            {"start": "2024-01-01T00:15:00Z", "end": "2024-01-01T00:30:00Z", "value": 100.0},
        ]
        assert pv._PvInterface__detect_pv_timeseries_resolution(timeseries) == 900

    def test_hourly_source_to_15min_system_rejected(self):
        pv = make_pv_interface(time_frame_base=TIME_FRAME_BASE_15MIN)
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": 100.0},
            {"start": "2024-01-01T01:00:00Z", "end": "2024-01-01T02:00:00Z", "value": 200.0},
        ] * 24
        assert pv._PvInterface__parse_pv_timeseries(timeseries, 192) == []


class TestPvAveraging:
    """15-min PV values are summed into hourly totals, not averaged (energy, not a rate)."""

    def test_sum_4_values_to_1(self):
        pv = make_pv_interface()
        timeseries = [
            {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:15:00Z", "value": 100.0},
            {"start": "2024-01-01T00:15:00Z", "end": "2024-01-01T00:30:00Z", "value": 150.0},
            {"start": "2024-01-01T00:30:00Z", "end": "2024-01-01T00:45:00Z", "value": 200.0},
            {"start": "2024-01-01T00:45:00Z", "end": "2024-01-01T01:00:00Z", "value": 250.0},
        ]
        hourly = pv._PvInterface__convert_15min_to_hourly_pv_timeseries(timeseries)
        assert len(hourly) == 1
        # Sum: 100 + 150 + 200 + 250 = 700 (not the 175 average)
        assert hourly[0]["value"] == 700.0

    def test_15min_source_to_hourly_system_end_to_end(self):
        pv = make_pv_interface()
        base = midnight_today_utc()
        timeseries = []
        for h in range(48):
            hour_start = base + timedelta(hours=h)
            for q in range(4):
                timeseries.append(
                    {
                        "start": (hour_start + timedelta(minutes=15 * q)).isoformat(),
                        "end": (hour_start + timedelta(minutes=15 * (q + 1))).isoformat(),
                        "value": 100.0,
                    }
                )
        values = pv._PvInterface__parse_pv_timeseries(timeseries, 48)
        assert len(values) == 48
        assert values[0] == 400.0  # 4 x 100 Wh summed


class TestPvDataCompleteness:
    def test_incomplete_hourly_data_padded_with_zero(self):
        pv = make_pv_interface()
        base = midnight_today_utc()
        timeseries = [
            {"start": (base + timedelta(hours=h)).isoformat(), "end": (base + timedelta(hours=h + 1)).isoformat(), "value": 500.0}
            for h in range(24)  # only today's 24 hours, tomorrow left empty
        ]
        values = pv._PvInterface__parse_pv_timeseries(timeseries, 48)
        assert len(values) == 48
        assert values[:24] == [500.0] * 24
        # PV pads missing slots (no data returned for them) with 0, not the last known value
        assert values[24:] == [0.0] * 24


class TestPvTimeseriesFetchDispatch:
    """get_summarized_pv_forecast() should route to the timeseries fetcher and
    bypass the per-pv_forecast-entry loop, mirroring the existing evcc special case."""

    def test_dispatches_to_timeseries_and_skips_per_entry_loop(self):
        source_config = {
            "source": "timeseries",
            "data_url": "http://test.local/timeseries",
            "data_path": "data",
        }
        # Two pv_forecast entries configured, like Nord/Ost/West - if the dispatcher
        # fell through to the per-entry loop, the (wrong) default forecast would be
        # requested and summed twice.
        config = [
            {"name": "Nord", "power": 4800},
            {"name": "Ost", "power": 2000},
        ]
        pv = make_pv_interface(source_config=source_config, config=config)
        pv.configuration_valid = True

        base = midnight_today_utc()
        api_timeseries = [
            {"start": (base + timedelta(hours=h)).isoformat(), "end": (base + timedelta(hours=h + 1)).isoformat(), "value": 42.0}
            for h in range(48)
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": api_timeseries}

        with patch("requests.get", return_value=mock_response) as mock_get:
            result = pv.get_summarized_pv_forecast()

        mock_get.assert_called_once()
        assert result[0] == 42.0
        assert len(result) == 48
        # Not 84.0 (which double-counting via the per-entry loop would produce)

    def test_missing_data_url_falls_back_to_default(self):
        source_config = {"source": "timeseries"}  # no data_url
        pv = make_pv_interface(source_config=source_config, config=[{"name": "test", "power": 1000}])
        pv.configuration_valid = True

        result = pv.get_summarized_pv_forecast()
        # __get_default_pv_forcast repeats its 24h triangle pattern for 48h total
        assert len(result) == 48
        assert max(result) == 700.0  # 1000 W * 0.7 peak factor
        assert result[0] == 0.0  # 00:00 is always 0 in the default shape

    def test_values_align_to_midnight_not_to_now(self):
        """The critical regression test: a source whose first entry is "now" (not
        midnight) - exactly what our own pv-forecast-corrector service returns -
        must still land at the correct slots-since-midnight index, not at index 0."""
        source_config = {
            "source": "timeseries",
            "data_url": "http://test.local/timeseries",
            "data_path": "data",
        }
        pv = make_pv_interface(source_config=source_config, config=[{"name": "test", "power": 1000}])
        pv.configuration_valid = True

        now = datetime.now(pytz.UTC).replace(minute=0, second=0, microsecond=0)
        base = midnight_today_utc()
        hours_since_midnight = int((now - base).total_seconds() // 3600)

        # Source has two real data points starting at "now" (needs >= 2 entries for
        # resolution detection); everything else in its own 48h window would also
        # be present in a real feed, but for this regression check that's enough.
        api_timeseries = [
            {"start": now.isoformat(), "end": (now + timedelta(hours=1)).isoformat(), "value": 777.0},
            {"start": (now + timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=2)).isoformat(), "value": 888.0},
        ]
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": api_timeseries}

        with patch("requests.get", return_value=mock_response):
            result = pv.get_summarized_pv_forecast()

        assert result[hours_since_midnight] == 777.0
        if hours_since_midnight > 0:
            assert result[0] == 0.0  # midnight slot itself has no data in this feed
