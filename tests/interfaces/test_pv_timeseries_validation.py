# pylint: disable=protected-access
"""
Tests for timeseries PV source configuration validation.
Ensures validation catches missing/invalid configurations early.
"""

from unittest.mock import patch
import pytest

from src.interfaces.pv_interface import PvInterface

TIME_FRAME_BASE_HOURLY = 3600


@pytest.fixture(autouse=True)
def patch_thread(monkeypatch):
    """Avoid starting the real background update thread during tests."""

    class DummyThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("threading.Thread", DummyThread)


class TestTimeseriesValidation:
    """Tests for timeseries source configuration validation."""

    def test_timeseries_missing_data_url_and_no_ha_integration(self):
        """Timeseries source must have either data_url or use_ha_central_data_source."""
        source_config = {"source": "timeseries"}  # missing both data_url and use_ha_central_data_source
        config = [{"name": "test", "power": 1000}]

        # At startup (strict=False), should log warning but still initialize
        pv = PvInterface(source_config, config, TIME_FRAME_BASE_HOURLY, {}, timezone="UTC")
        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_timeseries_with_data_url_only(self):
        """Timeseries with just data_url should be valid."""
        source_config = {
            "source": "timeseries",
            "data_url": "http://example.com/pv/forecast"
        }
        config = [{"name": "test", "power": 1000}]

        pv = PvInterface(source_config, config, TIME_FRAME_BASE_HOURLY, {}, timezone="UTC")
        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True

    def test_timeseries_with_ha_integration_only(self):
        """Timeseries with just use_ha_central_data_source should be valid."""
        source_config = {
            "source": "timeseries",
            "use_ha_central_data_source": True
        }
        config = [{"name": "test", "power": 1000}]

        pv = PvInterface(source_config, config, TIME_FRAME_BASE_HOURLY, {}, timezone="UTC")
        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True

    def test_timeseries_invalid_url_format(self):
        """Timeseries data_url must be a valid HTTP/HTTPS URL."""
        source_config = {
            "source": "timeseries",
            "data_url": "ftp://invalid.com/pv"  # FTP is not allowed
        }
        config = [{"name": "test", "power": 1000}]

        pv = PvInterface(source_config, config, TIME_FRAME_BASE_HOURLY, {}, timezone="UTC")
        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_timeseries_no_lat_lon_when_temp_disabled(self):
        """Timeseries doesn't require lat/lon when temperature forecast is disabled."""
        source_config = {
            "source": "timeseries",
            "data_url": "http://example.com/pv/forecast"
        }
        config = [{"name": "test", "power": 1000}]  # no lat/lon

        pv = PvInterface(
            source_config,
            config,
            TIME_FRAME_BASE_HOURLY,
            {},
            temperature_forecast_enabled=False,
            timezone="UTC"
        )
        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True

    def test_timeseries_requires_lat_lon_when_temp_enabled(self):
        """Timeseries requires lat/lon when temperature forecast is enabled."""
        source_config = {
            "source": "timeseries",
            "data_url": "http://example.com/pv/forecast"
        }
        config = [{"name": "test", "power": 1000}]  # no lat/lon

        pv = PvInterface(
            source_config,
            config,
            TIME_FRAME_BASE_HOURLY,
            {},
            temperature_forecast_enabled=True,
            timezone="UTC"
        )
        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False

    def test_timeseries_with_complete_config_and_temp_enabled(self):
        """Timeseries with data_url and lat/lon should be valid when temp is enabled."""
        source_config = {
            "source": "timeseries",
            "data_url": "http://example.com/pv/forecast"
        }
        config = [{"name": "test", "power": 1000, "lat": 52.5, "lon": 13.4}]

        pv = PvInterface(
            source_config,
            config,
            TIME_FRAME_BASE_HOURLY,
            {},
            temperature_forecast_enabled=True,
            timezone="UTC"
        )
        assert pv.configuration_state == "valid"
        assert pv.configuration_valid is True

    def test_timeseries_with_whitespace_only_data_url(self):
        """Whitespace-only data_url should be treated as missing."""
        source_config = {
            "source": "timeseries",
            "data_url": "   ",
            "use_ha_central_data_source": False
        }
        config = [{"name": "test", "power": 1000}]

        pv = PvInterface(source_config, config, TIME_FRAME_BASE_HOURLY, {}, timezone="UTC")
        assert pv.configuration_state == "incomplete"
        assert pv.configuration_valid is False
