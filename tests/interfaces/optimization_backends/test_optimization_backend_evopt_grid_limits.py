"""
Unit tests for external EVopt backend grid limits configuration.

Tests verify that configurable grid import/export limits are correctly
passed to the EVopt server, addressing issue #268.

Usage:
    pytest tests/interfaces/optimization_backends/test_optimization_backend_evopt_grid_limits.py -v
"""

import pytz
import pytest

from src.interfaces.optimization_backends.optimization_backend_evopt import EVOptBackend


@pytest.fixture(name="berlin_timezone")
def fixture_berlin_timezone():
    """
    Provides a pytz timezone for Europe/Berlin.

    Returns:
        pytz.timezone: Timezone object for Europe/Berlin.
    """
    return pytz.timezone("Europe/Berlin")


class TestEVOptBackendGridLimits:
    """
    Verify that external EVopt backend correctly accepts and uses
    configurable grid import/export limits.
    """

    def test_custom_grid_limits_passed_to_backend(self, berlin_timezone):
        """
        Test that custom grid limits are stored in backend instance.
        Issue #268: Grid limits should be configurable, not hardcoded to 10kW.
        """
        backend = EVOptBackend(
            "http://localhost:7050",
            time_frame_base=3600,
            time_zone=berlin_timezone,
            max_grid_import_w=16000,
            max_grid_export_w=16000,
        )
        assert backend.max_grid_import_w == 16000
        assert backend.max_grid_export_w == 16000

    def test_default_grid_limits_when_not_specified(self, berlin_timezone):
        """
        Test that grid limits default to 10kW when not specified.
        """
        backend = EVOptBackend(
            "http://localhost:7050",
            time_frame_base=3600,
            time_zone=berlin_timezone,
        )
        assert backend.max_grid_import_w == 10000
        assert backend.max_grid_export_w == 10000

    def test_zero_grid_limit_defaults_to_10kw(self, berlin_timezone):
        """
        Test that 0 (from config) is treated as "not specified" and defaults to 10kW.
        0 means the config field was not set (will be passed as None or 0 from schema).
        """
        backend = EVOptBackend(
            "http://localhost:7050",
            time_frame_base=3600,
            time_zone=berlin_timezone,
            max_grid_import_w=0,
            max_grid_export_w=0,
        )
        assert backend.max_grid_import_w == 10000
        assert backend.max_grid_export_w == 10000

    def test_none_grid_limit_defaults_to_10kw(self, berlin_timezone):
        """
        Test that None (unset) defaults to 10kW.
        """
        backend = EVOptBackend(
            "http://localhost:7050",
            time_frame_base=3600,
            time_zone=berlin_timezone,
            max_grid_import_w=None,
            max_grid_export_w=None,
        )
        assert backend.max_grid_import_w == 10000
        assert backend.max_grid_export_w == 10000

    def test_asymmetric_grid_limits(self, berlin_timezone):
        """
        Test that import and export limits can be set independently.
        Real-world scenario: 16kW import, 8kW export.
        """
        backend = EVOptBackend(
            "http://localhost:7050",
            time_frame_base=3600,
            time_zone=berlin_timezone,
            max_grid_import_w=16000,
            max_grid_export_w=8000,
        )
        assert backend.max_grid_import_w == 16000
        assert backend.max_grid_export_w == 8000

    def test_large_grid_limits_supported(self, berlin_timezone):
        """
        Test that large grid limits (e.g., 3-phase 32A @ 400V = ~22kW) are supported.
        """
        backend = EVOptBackend(
            "http://localhost:7050",
            time_frame_base=3600,
            time_zone=berlin_timezone,
            max_grid_import_w=22000,
            max_grid_export_w=22000,
        )
        assert backend.max_grid_import_w == 22000
        assert backend.max_grid_export_w == 22000

    def test_small_grid_limits_supported(self, berlin_timezone):
        """
        Test that small grid limits (e.g., single-phase 16A @ 230V = ~3.7kW) are supported.
        """
        backend = EVOptBackend(
            "http://localhost:7050",
            time_frame_base=3600,
            time_zone=berlin_timezone,
            max_grid_import_w=3700,
            max_grid_export_w=3700,
        )
        assert backend.max_grid_import_w == 3700
        assert backend.max_grid_export_w == 3700

    def test_15min_mode_with_custom_grid_limits(self, berlin_timezone):
        """
        Test that custom grid limits work with 15-minute time frame.
        """
        backend = EVOptBackend(
            "http://localhost:7050",
            time_frame_base=900,  # 15 minutes
            time_zone=berlin_timezone,
            max_grid_import_w=12000,
            max_grid_export_w=12000,
        )
        assert backend.max_grid_import_w == 12000
        assert backend.max_grid_export_w == 12000
        assert backend.time_frame_base == 900
