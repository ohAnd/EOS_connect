"""
Unit tests for NullInverter.

Tests NullInverter-specific functionality:
- Factory creation for default and EVCC modes
- No-op behavior (all methods return True/empty dicts)
- Control state pattern integration
- Display-only vs EVCC mode differences

Common interface compliance tests are inherited from BaseInverterTestSuite.
"""

# pylint: disable=import-error,redefined-outer-name,too-few-public-methods

import pytest
from src.interfaces.inverters import create_inverter, NullInverter, BaseInverter
from .base_inverter_tests import BaseInverterTestSuite


# =========================================================================
# Test Configuration
# =========================================================================


class TestNullInverterBase(BaseInverterTestSuite):
    """
    Base test suite for NullInverter.
    Inherits common interface compliance tests from BaseInverterTestSuite.
    """

    inverter_class = NullInverter  # type: ignore[assignment]
    minimal_config = {"type": "default", "address": "null"}  # type: ignore[assignment]
    expected_extended_monitoring = False  # type: ignore[assignment]

    @classmethod
    def setup_mocks(cls, monkeypatch):
        """No mocks needed for NullInverter."""


# =========================================================================
# NullInverter-Specific Tests
# =========================================================================


class TestNullInverterCreation:
    """Test NullInverter instantiation through factory."""

    def test_factory_creates_null_inverter_for_default(self):
        """Factory should create NullInverter for type 'default'."""
        config = {
            "type": "default",
            "address": "192.168.1.100",
            "max_grid_charge_rate": 5000,
            "max_pv_charge_rate": 5000,
        }
        inverter = create_inverter(config)

        assert isinstance(inverter, NullInverter)
        assert isinstance(inverter, BaseInverter)
        assert inverter.config["type"] == "default"

    def test_factory_creates_null_inverter_for_evcc(self):
        """Factory should create NullInverter for type 'evcc'."""
        config = {
            "type": "evcc",
            "max_grid_charge_rate": 5000,
            "max_pv_charge_rate": 5000,
        }
        inverter = create_inverter(config)

        assert isinstance(inverter, NullInverter)
        assert isinstance(inverter, BaseInverter)
        assert inverter.config["type"] == "evcc"


class TestNullInverterInitialization:
    """Test NullInverter initialization and authentication flag."""

    def test_initialize_sets_authenticated(self):
        """initialize() should set is_authenticated to True."""
        config = {"type": "default", "address": "192.168.1.100"}
        inverter = NullInverter(config)

        assert inverter.is_authenticated is False  # Before initialize
        inverter.initialize()
        assert inverter.is_authenticated is True  # After initialize


class TestNullInverterNoOpBehavior:
    """Test that all control methods are no-ops returning success."""

    @pytest.fixture
    def null_inverter(self):
        """Create initialized null inverter."""
        config = {
            "type": "default",
            "address": "192.168.1.100",
            "max_grid_charge_rate": 5000,
            "max_pv_charge_rate": 5000,
        }
        inverter = NullInverter(config)
        inverter.initialize()
        return inverter

    def test_connect_inverter_returns_true(self, null_inverter):
        """connect_inverter() should return True without error."""
        assert null_inverter.connect_inverter() is True

    def test_disconnect_inverter_returns_true(self, null_inverter):
        """disconnect_inverter() should return True without error."""
        assert null_inverter.disconnect_inverter() is True

    def test_set_battery_mode_returns_true(self, null_inverter):
        """set_battery_mode() should return True for any mode."""
        assert null_inverter.set_battery_mode("normal") is True
        assert null_inverter.set_battery_mode("hold") is True
        assert null_inverter.set_battery_mode("charge") is True

    def test_set_mode_avoid_discharge_returns_true(self, null_inverter):
        """set_mode_avoid_discharge() should return True."""
        assert null_inverter.set_mode_avoid_discharge() is True

    def test_set_mode_allow_discharge_returns_true(self, null_inverter):
        """set_mode_allow_discharge() should return True."""
        assert null_inverter.set_mode_allow_discharge() is True

    def test_set_mode_force_charge_returns_true(self, null_inverter):
        """set_mode_force_charge() should return True for any power level."""
        assert null_inverter.set_mode_force_charge(0) is True
        assert null_inverter.set_mode_force_charge(1000) is True
        assert null_inverter.set_mode_force_charge(5000) is True

    def test_set_allow_grid_charging_no_error(self, null_inverter):
        """set_allow_grid_charging() should not raise errors."""
        null_inverter.set_allow_grid_charging(True)  # Should not raise
        null_inverter.set_allow_grid_charging(False)  # Should not raise

    def test_get_battery_info_returns_empty_dict(self, null_inverter):
        """get_battery_info() should return empty dict."""
        info = null_inverter.get_battery_info()
        assert isinstance(info, dict)
        assert len(info) == 0

    def test_fetch_inverter_data_returns_empty_dict(self, null_inverter):
        """fetch_inverter_data() should return empty dict."""
        data = null_inverter.fetch_inverter_data()
        assert isinstance(data, dict)
        assert len(data) == 0


class TestNullInverterIntegration:
    """Test NullInverter integration with control state patterns."""

    def test_evcc_mode_enables_evcc_control(self):
        """Test the pattern used in change_control_state() function."""
        config = {"type": "evcc", "max_grid_charge_rate": 5000}
        inverter = create_inverter(config)

        # Simulate the check from eos_connect.py line 1079-1083
        inverter_fronius_en = False
        inverter_evcc_en = False

        if config["type"] == "evcc":
            inverter_evcc_en = True
        elif isinstance(inverter, BaseInverter) and config["type"] != "default":
            inverter_fronius_en = True

        # For NullInverter with evcc type, inverter_evcc_en should be True
        assert inverter_evcc_en is True
        assert inverter_fronius_en is False

    def test_default_type_neither_fronius_nor_evcc(self):
        """Test that default type doesn't enable either control path."""
        config = {"type": "default", "address": "192.168.1.100"}
        inverter = create_inverter(config)

        inverter_fronius_en = False
        inverter_evcc_en = False

        if config["type"] == "evcc":
            inverter_evcc_en = True
        elif isinstance(inverter, BaseInverter) and config["type"] != "default":
            inverter_fronius_en = True

        # For default type, neither should be enabled (display-only mode)
        assert inverter_fronius_en is False
        assert inverter_evcc_en is False

    def test_real_inverter_enables_fronius_control(self):
        """Test that real hardware inverters enable fronius_en flag."""
        # Test with actual Fronius config
        fronius_config = {
            "type": "fronius_gen24",
            "address": "192.168.1.100",
            "user": "customer",
            "password": "test",
            "max_grid_charge_rate": 5000,
            "max_pv_charge_rate": 5000,
        }
        inverter = create_inverter(fronius_config)

        inverter_fronius_en = False
        inverter_evcc_en = False

        if fronius_config["type"] == "evcc":
            inverter_evcc_en = True
        elif isinstance(inverter, BaseInverter) and fronius_config["type"] != "default":
            inverter_fronius_en = True

        # Real hardware inverter should enable fronius_en
        assert inverter_fronius_en is True
        assert inverter_evcc_en is False
