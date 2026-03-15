"""
Unit tests for FroniusLegacy inverter implementation.

Tests Fronius Legacy-specific functionality:
- MD5 authentication attributes
- hash_utf8 and strip_dict utility functions
- Inverter data structure
- Configuration paths

Common interface compliance tests are inherited from BaseInverterTestSuite.
"""

# pylint: disable=import-error,redefined-outer-name,import-outside-toplevel,too-few-public-methods

from unittest.mock import patch
import pytest
from src.interfaces.inverters.fronius_legacy import FroniusLegacy
from .base_inverter_tests import BaseInverterTestSuite


# =========================================================================
# Test Configuration
# =========================================================================


class TestFroniusLegacyBase(BaseInverterTestSuite):
    """
    Base test suite for FroniusLegacy.
    Inherits common interface compliance tests from BaseInverterTestSuite.
    """

    inverter_class = FroniusLegacy  # type: ignore[assignment]
    minimal_config = {  # type: ignore[assignment]
        "address": "192.168.1.100",
        "type": "fronius_gen24_legacy",
        "user": "customer",
        "password": "test_password",
    }
    expected_extended_monitoring = True  # type: ignore[assignment]

    @classmethod
    def setup_mocks(cls, monkeypatch):
        """Set up mocks for FroniusLegacy (requests.Session)."""
        monkeypatch.setattr(
            "src.interfaces.inverters.fronius_legacy.requests.Session", lambda: None
        )


# =========================================================================
# Fronius Legacy-Specific Tests
# =========================================================================


class TestFroniusLegacyAuthentication:
    """Tests for Fronius Legacy MD5 authentication attributes."""

    @pytest.fixture
    def fronius_instance(self):
        """Create FroniusLegacy instance for testing."""
        config = {
            "address": "192.168.1.100",
            "user": "customer",
            "password": "test_password",
            "type": "fronius_gen24_legacy",
        }
        with patch("src.interfaces.inverters.fronius_legacy.requests.Session"):
            return FroniusLegacy(config)

    def test_auth_attributes_initialized(self, fronius_instance):
        """Test that authentication-related attributes are initialized."""
        assert fronius_instance.subsequent_login is False
        assert fronius_instance.ncvalue_num == 1
        assert fronius_instance.login_attempts == 0


class TestFroniusLegacyInverterData:
    """Tests for Fronius Legacy inverter data structure."""

    @pytest.fixture
    def fronius_instance(self):
        """Create FroniusLegacy instance for testing."""
        config = {"address": "192.168.1.100", "type": "fronius_gen24_legacy"}
        with patch("src.interfaces.inverters.fronius_legacy.requests.Session"):
            return FroniusLegacy(config)

    def test_inverter_data_structure_initialized(self, fronius_instance):
        """Test that Fronius-specific inverter data structure is initialized."""
        assert (
            "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"
            in fronius_instance.inverter_current_data
        )
        assert (
            "MODULE_TEMPERATURE_MEAN_01_F32" in fronius_instance.inverter_current_data
        )
        assert "FANCONTROL_PERCENT_01_F32" in fronius_instance.inverter_current_data

    def test_get_inverter_current_data_returns_dict(self, fronius_instance):
        """Test that get_inverter_current_data returns the internal data dict."""
        result = fronius_instance.get_inverter_current_data()
        assert isinstance(result, dict)
        assert "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32" in result

    def test_has_api_set_max_grid_charge_rate_method(self, fronius_instance):
        """Fronius Legacy provides hardware-specific grid charge rate control."""
        assert hasattr(fronius_instance, "api_set_max_grid_charge_rate")
        assert callable(fronius_instance.api_set_max_grid_charge_rate)


class TestFroniusLegacyUtilityFunctions:
    """Tests for Fronius Legacy utility functions (hash_utf8, strip_dict)."""

    def test_hash_utf8_with_string(self):
        """Test hash_utf8 function with string input."""
        from src.interfaces.inverters.fronius_legacy import hash_utf8

        result = hash_utf8("test")
        assert isinstance(result, str)
        assert len(result) == 32  # MD5 hash length

    def test_hash_utf8_with_bytes(self):
        """Test hash_utf8 function with bytes input."""
        from src.interfaces.inverters.fronius_legacy import hash_utf8

        result = hash_utf8(b"test")
        assert isinstance(result, str)
        assert len(result) == 32

    def test_strip_dict_removes_underscore_keys(self):
        """Test strip_dict removes keys starting with underscore."""
        from src.interfaces.inverters.fronius_legacy import strip_dict

        test_dict = {"key": "value", "_private": "hidden", "public": "visible"}
        result = strip_dict(test_dict)
        assert "key" in result
        assert "public" in result
        assert "_private" not in result

    def test_strip_dict_handles_non_dict_input(self):
        """Test strip_dict returns non-dict input unchanged."""
        from src.interfaces.inverters.fronius_legacy import strip_dict

        assert strip_dict("string") == "string"
        assert strip_dict(123) == 123
        assert strip_dict(None) is None


class TestFroniusLegacyConfiguration:
    """Tests for Fronius Legacy configuration handling."""

    def test_config_paths_are_set(self):
        """Test that configuration file paths are set."""
        from src.interfaces.inverters.fronius_legacy import (
            TIMEOFUSE_CONFIG_FILENAME,
            BATTERY_CONFIG_FILENAME,
        )

        assert "timeofuse_config.json" in TIMEOFUSE_CONFIG_FILENAME
        assert "battery_config.json" in BATTERY_CONFIG_FILENAME

    def test_initialization_with_minimal_config(self):
        """Test initialization with minimal configuration."""
        minimal_config = {"address": "192.168.1.1", "type": "fronius_gen24_legacy"}
        with patch("src.interfaces.inverters.fronius_legacy.requests.Session"):
            instance = FroniusLegacy(minimal_config)
            assert instance.address == "192.168.1.1"
            assert instance.min_soc == 5  # Default value
