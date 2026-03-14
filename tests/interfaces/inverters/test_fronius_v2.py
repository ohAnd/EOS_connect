"""
Unit tests for FroniusV2 inverter implementation.

Tests Fronius V2-specific functionality:
- SHA256 authentication attributes
- hash_utf8_md5/hash_utf8_sha256 and strip_dict utility functions
- User lowercase conversion
- Inverter data structure
- Configuration paths

Common interface compliance tests are inherited from BaseInverterTestSuite.
"""

# pylint: disable=import-error,redefined-outer-name,import-outside-toplevel,duplicate-code,too-few-public-methods

from unittest.mock import patch
import pytest
from src.interfaces.inverters.fronius_v2 import FroniusV2
from .base_inverter_tests import BaseInverterTestSuite


# =========================================================================
# Test Configuration
# =========================================================================


class TestFroniusV2Base(BaseInverterTestSuite):
    """
    Base test suite for FroniusV2.
    Inherits common interface compliance tests from BaseInverterTestSuite.
    """

    inverter_class = FroniusV2  # type: ignore[assignment]
    minimal_config = {"address": "192.168.1.102", "type": "fronius_gen24"}  # type: ignore[assignment]
    expected_extended_monitoring = True  # type: ignore[assignment]

    @classmethod
    def setup_mocks(cls, monkeypatch):
        """Set up mocks for FroniusV2 (requests.Session)."""
        monkeypatch.setattr(
            "src.interfaces.inverters.fronius_v2.requests.Session", lambda: None
        )


# =========================================================================
# Fronius V2-Specific Tests
# =========================================================================


class TestFroniusV2Authentication:
    """Tests for Fronius V2 SHA256 authentication attributes."""

    @pytest.fixture
    def fronius_instance(self):
        """Create FroniusV2 instance for testing."""
        config = {
            "address": "192.168.1.102",
            "user": "customer",
            "password": "test_password",
            "type": "fronius_gen24",
        }
        with patch("src.interfaces.inverters.fronius_v2.requests.Session"):
            return FroniusV2(config)

    def test_auth_attributes_initialized(self, fronius_instance):
        """Test that authentication-related attributes are initialized."""
        assert fronius_instance.subsequent_login is False
        assert fronius_instance.ncvalue_num == 1
        assert fronius_instance.algorithm == "SHA256"
        assert fronius_instance.login_attempts == 0

    def test_user_is_converted_to_lowercase(self):
        """Test that user field is always converted to lowercase."""
        config = {
            "address": "192.168.1.1",
            "user": "CUSTOMER",
            "password": "test",
            "type": "fronius_gen24",
        }
        with patch("src.interfaces.inverters.fronius_v2.requests.Session"):
            instance = FroniusV2(config)
            assert instance.user == "customer"


class TestFroniusV2InverterData:
    """Tests for Fronius V2 inverter data structure."""

    @pytest.fixture
    def fronius_instance(self):
        """Create FroniusV2 instance for testing."""
        config = {"address": "192.168.1.102", "type": "fronius_gen24"}
        with patch("src.interfaces.inverters.fronius_v2.requests.Session"):
            return FroniusV2(config)

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
        """Fronius V2 provides hardware-specific grid charge rate control."""
        assert hasattr(fronius_instance, "api_set_max_grid_charge_rate")
        assert callable(fronius_instance.api_set_max_grid_charge_rate)


class TestFroniusV2HashingFunctions:
    """Tests for Fronius V2 hashing utility functions (MD5 and SHA256)."""

    def test_hash_utf8_md5_with_string(self):
        """Test hash_utf8_md5 function with string input."""
        from src.interfaces.inverters.fronius_v2 import hash_utf8_md5

        result = hash_utf8_md5("test")
        assert isinstance(result, str)
        assert len(result) == 32  # MD5 hash length

    def test_hash_utf8_md5_with_bytes(self):
        """Test hash_utf8_md5 function with bytes input."""
        from src.interfaces.inverters.fronius_v2 import hash_utf8_md5

        result = hash_utf8_md5(b"test")
        assert isinstance(result, str)
        assert len(result) == 32

    def test_hash_utf8_sha256_with_string(self):
        """Test hash_utf8_sha256 function with string input."""
        from src.interfaces.inverters.fronius_v2 import hash_utf8_sha256

        result = hash_utf8_sha256("test")
        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 hash length

    def test_hash_utf8_sha256_with_bytes(self):
        """Test hash_utf8_sha256 function with bytes input."""
        from src.interfaces.inverters.fronius_v2 import hash_utf8_sha256

        result = hash_utf8_sha256(b"test")
        assert isinstance(result, str)
        assert len(result) == 64


class TestFroniusV2UtilityFunctions:
    """Tests for Fronius V2 utility functions (strip_dict)."""

    def test_strip_dict_removes_underscore_keys(self):
        """Test strip_dict removes keys starting with underscore."""
        from src.interfaces.inverters.fronius_v2 import strip_dict

        test_dict = {"key": "value", "_private": "hidden", "public": "visible"}
        result = strip_dict(test_dict)
        assert "key" in result
        assert "public" in result
        assert "_private" not in result

    def test_strip_dict_handles_non_dict_input(self):
        """Test strip_dict returns non-dict input unchanged."""
        from src.interfaces.inverters.fronius_v2 import strip_dict

        assert strip_dict("string") == "string"
        assert strip_dict(123) == 123
        assert strip_dict(None) is None


class TestFroniusV2Configuration:
    """Tests for Fronius V2 configuration handling."""

    def test_config_paths_are_set(self):
        """Test that configuration file paths are set."""
        from src.interfaces.inverters.fronius_v2 import (
            TIMEOFUSE_CONFIG_FILENAME,
            BATTERY_CONFIG_FILENAME,
        )

        assert "timeofuse_config.json" in TIMEOFUSE_CONFIG_FILENAME
        assert "battery_config.json" in BATTERY_CONFIG_FILENAME

    def test_initialization_with_minimal_config(self):
        """Test initialization with minimal configuration."""
        minimal_config = {"address": "192.168.1.1", "type": "fronius_gen24"}
        with patch("src.interfaces.inverters.fronius_v2.requests.Session"):
            instance = FroniusV2(minimal_config)
            assert instance.address == "192.168.1.1"
            assert instance.user == "customer"  # Default
            assert instance.min_soc == 5  # Default value

    def test_initialization_with_custom_user(self):
        """Test initialization with custom user that gets lowercased."""
        config = {
            "address": "192.168.1.1",
            "user": "MyUser",
            "type": "fronius_gen24",
        }
        with patch("src.interfaces.inverters.fronius_v2.requests.Session"):
            instance = FroniusV2(config)
            assert instance.user == "myuser"


class TestFroniusV2Algorithms:
    """Tests for Fronius V2 algorithm selection."""

    @pytest.fixture
    def fronius_instance(self):
        """Create FroniusV2 instance for testing."""
        config = {"address": "192.168.1.102", "type": "fronius_gen24"}
        with patch("src.interfaces.inverters.fronius_v2.requests.Session"):
            return FroniusV2(config)

    def test_algorithm_defaults_to_sha256(self, fronius_instance):
        """Test that algorithm defaults to SHA256."""
        assert fronius_instance.algorithm == "SHA256"
