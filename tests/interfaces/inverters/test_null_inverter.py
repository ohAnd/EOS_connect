"""Unit tests for NullInverter."""

# pylint: disable=import-error,redefined-outer-name,too-few-public-methods

import pytest
from src.interfaces.inverters import create_inverter, NullInverter, BaseInverter


@pytest.fixture
def null_config_default():
    """Config for default (display-only) mode."""
    return {
        "type": "default",
        "address": "192.168.1.100",
        "max_grid_charge_rate": 5000,
        "max_pv_charge_rate": 5000,
    }


@pytest.fixture
def null_config_evcc():
    """Config for EVCC mode."""
    return {
        "type": "evcc",
        "max_grid_charge_rate": 5000,
        "max_pv_charge_rate": 5000,
    }


class TestNullInverterCreation:
    """Test NullInverter instantiation through factory."""

    def test_factory_creates_null_inverter_for_default(self, null_config_default):
        """Factory should create NullInverter for type 'default'."""
        inverter = create_inverter(null_config_default)

        assert isinstance(inverter, NullInverter)
        assert isinstance(inverter, BaseInverter)
        assert inverter.config["type"] == "default"

    def test_factory_creates_null_inverter_for_evcc(self, null_config_evcc):
        """Factory should create NullInverter for type 'evcc'."""
        inverter = create_inverter(null_config_evcc)

        assert isinstance(inverter, NullInverter)
        assert isinstance(inverter, BaseInverter)
        assert inverter.config["type"] == "evcc"


class TestNullInverterInitialization:
    """Test NullInverter initialization and configuration."""

    def test_initialization_with_default_config(self, null_config_default):
        """NullInverter should initialize properly with default config."""
        inverter = NullInverter(null_config_default)

        assert inverter.address == "192.168.1.100"
        assert inverter.max_grid_charge_rate == 5000
        assert inverter.max_pv_charge_rate == 5000
        assert inverter.inverter_type == "NullInverter"

    def test_initialize_sets_authenticated(self, null_config_default):
        """initialize() should set is_authenticated to True."""
        inverter = NullInverter(null_config_default)

        assert inverter.is_authenticated is False  # Before initialize
        inverter.initialize()
        assert inverter.is_authenticated is True  # After initialize


class TestNullInverterNoOpBehavior:
    """Test that all control methods are no-ops returning success."""

    @pytest.fixture
    def null_inverter(self, null_config_default):
        """Create initialized null inverter."""
        inverter = NullInverter(null_config_default)
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


class TestNullInverterCapabilities:
    """Test capability detection methods."""

    def test_supports_extended_monitoring_returns_false(self, null_config_default):
        """supports_extended_monitoring() should return False."""
        inverter = NullInverter(null_config_default)
        assert inverter.supports_extended_monitoring() is False


class TestNullInverterIntegration:
    """Test NullInverter works correctly in isinstance checks."""

    def test_isinstance_base_inverter(self, null_config_default):
        """NullInverter should pass isinstance check for BaseInverter."""
        inverter = create_inverter(null_config_default)

        # This is the critical check used in eos_connect.py
        assert isinstance(inverter, BaseInverter)

    def test_works_in_change_control_state_pattern(self, null_config_evcc):
        """Test the pattern used in change_control_state() function."""
        inverter = create_inverter(null_config_evcc)

        # Simulate the check from eos_connect.py line 1079-1083
        inverter_fronius_en = False
        inverter_evcc_en = False

        if null_config_evcc["type"] == "evcc":
            inverter_evcc_en = True
        elif (
            isinstance(inverter, BaseInverter) and null_config_evcc["type"] != "default"
        ):
            inverter_fronius_en = True

        # For NullInverter with evcc type, inverter_evcc_en should be True
        assert inverter_evcc_en is True
        assert inverter_fronius_en is False

    def test_default_type_neither_fronius_nor_evcc(self, null_config_default):
        """Test that default type doesn't enable either control path."""
        inverter = create_inverter(null_config_default)

        inverter_fronius_en = False
        inverter_evcc_en = False

        if null_config_default["type"] == "evcc":
            inverter_evcc_en = True
        elif (
            isinstance(inverter, BaseInverter)
            and null_config_default["type"] != "default"
        ):
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

    def test_supports_extended_monitoring_check(self, null_config_default):
        """Test the pattern used in __run_data_loop() function."""
        inverter = create_inverter(null_config_default)

        # Simulate check from eos_connect.py line 990
        should_fetch_data = inverter.supports_extended_monitoring()

        # NullInverter should not fetch extended monitoring data
        assert should_fetch_data is False
