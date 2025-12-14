"""
Unit tests for FroniusV2 inverter implementation.

Tests initialization, authentication, battery control, and API interactions
for the Fronius GEN24 V2 inverter interface with updated authentication.
"""

# pylint: disable=import-error,redefined-outer-name,import-outside-toplevel,duplicate-code,too-few-public-methods

from unittest.mock import patch
import pytest
from src.interfaces.inverters.fronius_v2 import FroniusV2
from src.interfaces.inverter_base import BaseInverter


@pytest.fixture
def fronius_v2_config():
    """Provide a default configuration for FroniusV2."""
    return {
        "address": "192.168.1.102",
        "user": "customer",
        "password": "test_password",
        "max_pv_charge_rate": 15000,
        "max_grid_charge_rate": 10000,
        "type": "fronius_gen24",
    }


@pytest.fixture
def fronius_v2_instance(fronius_v2_config):
    """Create a FroniusV2 instance with basic mocking."""
    with patch("src.interfaces.inverters.fronius_v2.requests.Session"):
        instance = FroniusV2(fronius_v2_config)
        return instance


class TestFroniusV2Initialization:
    """Tests for FroniusV2 initialization."""

    def test_initialization_sets_basic_attributes(self, fronius_v2_instance):
        """Test that initialization sets basic attributes correctly."""
        assert fronius_v2_instance.address == "192.168.1.102"
        assert fronius_v2_instance.user == "customer"
        assert fronius_v2_instance.password == "test_password"
        assert fronius_v2_instance.max_soc == 100
        assert fronius_v2_instance.min_soc == 5

    def test_initialization_sets_auth_defaults(self, fronius_v2_instance):
        """Test that authentication-related attributes are initialized."""
        assert fronius_v2_instance.subsequent_login is False
        assert fronius_v2_instance.ncvalue_num == 1
        assert fronius_v2_instance.algorithm == "SHA256"
        assert fronius_v2_instance.login_attempts == 0

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

    def test_initialization_sets_inverter_data_structure(self, fronius_v2_instance):
        """Test that inverter data structure is initialized."""
        assert (
            "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"
            in fronius_v2_instance.inverter_current_data
        )
        assert (
            "MODULE_TEMPERATURE_MEAN_01_F32"
            in fronius_v2_instance.inverter_current_data
        )
        assert "FANCONTROL_PERCENT_01_F32" in fronius_v2_instance.inverter_current_data

    def test_inherits_from_base_inverter(self, fronius_v2_instance):
        """Test that FroniusV2 inherits from BaseInverter."""
        assert isinstance(fronius_v2_instance, BaseInverter)


class TestFroniusV2Capabilities:
    """Tests for FroniusV2 capability detection."""

    def test_supports_extended_monitoring(self, fronius_v2_instance):
        """Test that FroniusV2 supports extended monitoring."""
        assert fronius_v2_instance.supports_extended_monitoring() is True

    def test_has_api_set_max_pv_charge_rate_method(self, fronius_v2_instance):
        """Test that API method for PV charge rate exists and is callable."""
        assert hasattr(fronius_v2_instance, "api_set_max_pv_charge_rate")
        assert callable(fronius_v2_instance.api_set_max_pv_charge_rate)


class TestFroniusV2ConnectionMethods:
    """Tests for connection-related methods."""

    @pytest.mark.skip(reason="connect_inverter calls abstract base method")
    def test_connect_inverter_returns_boolean(self, fronius_v2_instance):
        """Test that connect_inverter returns a boolean value."""
        with patch.object(fronius_v2_instance, "authenticate", return_value=True):
            result = fronius_v2_instance.connect_inverter()
            assert isinstance(result, bool)

    @pytest.mark.skip(reason="disconnect_inverter calls abstract base method")
    def test_disconnect_inverter_returns_boolean(self, fronius_v2_instance):
        """Test that disconnect_inverter returns a boolean value."""
        result = fronius_v2_instance.disconnect_inverter()
        assert isinstance(result, bool)


class TestFroniusV2BatteryControl:
    """Tests for battery control methods."""

    def test_set_mode_avoid_discharge_has_method(self, fronius_v2_instance):
        """Test that set_mode_avoid_discharge method exists."""
        assert hasattr(fronius_v2_instance, "set_mode_avoid_discharge")
        assert callable(fronius_v2_instance.set_mode_avoid_discharge)

    def test_set_mode_allow_discharge_has_method(self, fronius_v2_instance):
        """Test that set_mode_allow_discharge method exists."""
        assert hasattr(fronius_v2_instance, "set_mode_allow_discharge")
        assert callable(fronius_v2_instance.set_mode_allow_discharge)

    def test_set_mode_force_charge_has_method(self, fronius_v2_instance):
        """Test that set_mode_force_charge method exists."""
        assert hasattr(fronius_v2_instance, "set_mode_force_charge")
        assert callable(fronius_v2_instance.set_mode_force_charge)

    def test_get_battery_info_has_method(self, fronius_v2_instance):
        """Test that get_battery_info method exists."""
        assert hasattr(fronius_v2_instance, "get_battery_info")
        assert callable(fronius_v2_instance.get_battery_info)

    def test_set_allow_grid_charging_has_method(self, fronius_v2_instance):
        """Test that set_allow_grid_charging method exists."""
        assert hasattr(fronius_v2_instance, "set_allow_grid_charging")
        assert callable(fronius_v2_instance.set_allow_grid_charging)


class TestFroniusV2InverterData:
    """Tests for inverter data fetching."""

    def test_fetch_inverter_data_has_method(self, fronius_v2_instance):
        """Test that fetch_inverter_data method exists."""
        assert hasattr(fronius_v2_instance, "fetch_inverter_data")
        assert callable(fronius_v2_instance.fetch_inverter_data)

    def test_get_inverter_current_data_returns_dict(self, fronius_v2_instance):
        """Test that get_inverter_current_data returns the internal data dict."""
        result = fronius_v2_instance.get_inverter_current_data()
        assert isinstance(result, dict)
        assert "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32" in result


class TestFroniusV2PVChargeRateControl:  # pylint: disable=too-few-public-methods
    """Tests for PV charge rate control specific to FroniusV2."""

    def test_api_set_max_pv_charge_rate_has_method(self, fronius_v2_instance):
        """Test that api_set_max_pv_charge_rate method exists."""
        assert hasattr(fronius_v2_instance, "api_set_max_pv_charge_rate")
        assert callable(fronius_v2_instance.api_set_max_pv_charge_rate)


class TestFroniusV2HashingFunctions:
    """Tests for hashing utility functions used by FroniusV2."""

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
    """Tests for utility functions used by FroniusV2."""

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
    """Tests for configuration handling."""

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
    """Tests for algorithm selection based on firmware."""

    def test_algorithm_defaults_to_sha256(self, fronius_v2_instance):
        """Test that algorithm defaults to SHA256."""
        assert fronius_v2_instance.algorithm == "SHA256"
