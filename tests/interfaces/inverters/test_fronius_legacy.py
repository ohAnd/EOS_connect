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

from unittest.mock import patch, MagicMock
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


class TestFroniusLegacyTimeOfUseModes:
    """
    Tests for the Time-of-Use (TOU) inverter mode methods.

    Verifies that set_mode_avoid_discharge() and set_mode_allow_discharge()
    send the correct TOU schedule entries to the Gen24 for every optimizer
    state combination (dc_charge / discharge_allowed).
    """

    @pytest.fixture
    def fronius(self):
        """Return a FroniusLegacy instance with set_time_of_use mocked out."""
        config = {
            "address": "192.168.1.100",
            "type": "fronius_gen24_legacy",
            "max_pv_charge_rate": 5000,
            "max_grid_charge_rate": 3000,
        }
        with patch("src.interfaces.inverters.fronius_legacy.requests.Session"):
            instance = FroniusLegacy(config)
        instance.set_time_of_use = MagicMock(return_value=True)
        return instance

    # ------------------------------------------------------------------
    # set_mode_allow_discharge
    # ------------------------------------------------------------------

    def test_allow_discharge_with_positive_pv_rate_sends_charge_max(self, fronius):
        """Normal discharge: CHARGE_MAX with configured rate (no empty list)."""
        fronius.max_pv_charge_rate = 5000
        fronius.set_mode_allow_discharge()

        fronius.set_time_of_use.assert_called_once()
        tou_list = fronius.set_time_of_use.call_args[0][0]
        assert len(tou_list) == 1
        entry = tou_list[0]
        assert entry["ScheduleType"] == "CHARGE_MAX"
        assert entry["Power"] == 5000
        assert entry["Active"] is True

    def test_allow_discharge_with_zero_pv_rate_sends_charge_max_zero(self, fronius):
        """Evening-discharge / no-PV-refill: CHARGE_MAX:0 must be sent explicitly.

        Previously the code sent an empty TOU list which wiped all Gen24 rules.
        """
        fronius.max_pv_charge_rate = 0
        fronius.set_mode_allow_discharge()

        fronius.set_time_of_use.assert_called_once()
        tou_list = fronius.set_time_of_use.call_args[0][0]
        assert len(tou_list) == 1, "Must not send empty list - that wipes all TOU rules"
        entry = tou_list[0]
        assert entry["ScheduleType"] == "CHARGE_MAX"
        assert entry["Power"] == 0
        assert entry["Active"] is True

    def test_allow_discharge_never_sends_empty_list(self, fronius):
        """set_mode_allow_discharge must never send an empty TOU list."""
        fronius.max_pv_charge_rate = 0
        fronius.set_mode_allow_discharge()

        tou_list = fronius.set_time_of_use.call_args[0][0]
        assert tou_list != [], "Empty TOU list would wipe all existing Gen24 rules"

    # ------------------------------------------------------------------
    # set_mode_avoid_discharge
    # ------------------------------------------------------------------

    def test_avoid_discharge_with_positive_pv_rate_sends_only_discharge_max(
        self, fronius
    ):
        """Daytime hold: block discharging, PV may still charge freely."""
        fronius.max_pv_charge_rate = 5000
        fronius.set_mode_avoid_discharge()

        fronius.set_time_of_use.assert_called_once()
        tou_list = fronius.set_time_of_use.call_args[0][0]
        schedule_types = [e["ScheduleType"] for e in tou_list]
        assert "DISCHARGE_MAX" in schedule_types
        assert (
            "CHARGE_MAX" not in schedule_types
        ), "Should not restrict PV charging when dc_charge > 0"
        discharge_entry = next(
            e for e in tou_list if e["ScheduleType"] == "DISCHARGE_MAX"
        )
        assert discharge_entry["Power"] == 0

    def test_avoid_discharge_with_zero_pv_rate_sends_discharge_and_charge_max(
        self, fronius
    ):
        """Isolation mode: block both discharge AND PV charging."""
        fronius.max_pv_charge_rate = 0
        fronius.set_mode_avoid_discharge()

        fronius.set_time_of_use.assert_called_once()
        tou_list = fronius.set_time_of_use.call_args[0][0]
        schedule_types = [e["ScheduleType"] for e in tou_list]
        assert "DISCHARGE_MAX" in schedule_types
        assert (
            "CHARGE_MAX" in schedule_types
        ), "Isolation mode must also block PV charging"
        charge_entry = next(e for e in tou_list if e["ScheduleType"] == "CHARGE_MAX")
        assert charge_entry["Power"] == 0

    def test_avoid_discharge_all_weekdays_active(self, fronius):
        """TOU rules must apply to every day of the week."""
        fronius.max_pv_charge_rate = 0
        fronius.set_mode_avoid_discharge()

        tou_list = fronius.set_time_of_use.call_args[0][0]
        for entry in tou_list:
            weekdays = entry["Weekdays"]
            assert all(
                weekdays[day]
                for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            )

    def test_allow_discharge_all_weekdays_active(self, fronius):
        """TOU rules must apply to every day of the week."""
        fronius.max_pv_charge_rate = 5000
        fronius.set_mode_allow_discharge()

        tou_list = fronius.set_time_of_use.call_args[0][0]
        for entry in tou_list:
            weekdays = entry["Weekdays"]
            assert all(
                weekdays[day]
                for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            )

    # ------------------------------------------------------------------
    # api_set_max_pv_charge_rate
    # ------------------------------------------------------------------

    def test_api_set_max_pv_charge_rate_stores_value(self, fronius):
        """api_set_max_pv_charge_rate must update the stored rate."""
        fronius.api_set_max_pv_charge_rate(3000)
        assert fronius.max_pv_charge_rate == 3000

    def test_api_set_max_pv_charge_rate_zero_is_accepted(self, fronius):
        """Rate of 0 W is a valid and important value (block PV charging)."""
        fronius.api_set_max_pv_charge_rate(0)
        assert fronius.max_pv_charge_rate == 0

    def test_api_set_max_pv_charge_rate_negative_rejected(self, fronius):
        """Negative values must be rejected and the rate must stay unchanged."""
        fronius.max_pv_charge_rate = 5000
        fronius.api_set_max_pv_charge_rate(-100)
        assert fronius.max_pv_charge_rate == 5000
