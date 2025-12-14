"""Tests for the BaseInverter interface and its mock implementation."""

# test_base_inverter.py

# pylint: disable=import-error,redefined-outer-name,import-outside-toplevel,duplicate-code,too-few-public-methods

# from unittest.mock import MagicMock
import pytest

from src.interfaces.inverters import BaseInverter


# --- Mock class for tests ---
class MockInverter(BaseInverter):
    """Mock implementation of BaseInverter used for unit tests."""

    def initialize(self):
        """Initialize the mock inverter."""
        self.inverter_connected = True

    def connect_inverter(self) -> bool:
        """Connect to the mock inverter."""
        self.inverter_connected = True
        return True

    def disconnect_inverter(self) -> bool:
        """Disconnect from the mock inverter."""
        self.inverter_connected = False
        return True

    def set_battery_mode(self, mode: str) -> bool:
        """Set the battery mode for the mock inverter."""
        self.last_mode_set = mode
        return True

    def get_battery_info(self) -> dict:
        """Get battery information from the mock inverter."""
        return {"charge": 50, "status": "ok"}

    def fetch_inverter_data(self) -> dict:
        """Fetch inverter data from the mock inverter."""
        return {"voltage": 230, "current": 10}

    def set_mode_force_charge(self, charge_power_w: int) -> bool:
        """Set force charge mode for the mock inverter."""
        self.forced_charge_power = charge_power_w
        return True

    def set_allow_grid_charging(self, value):
        """Set allow grid charging for the mock inverter."""
        self.grid_charging_allowed = value

    def set_mode_allow_discharge(self):
        """Set mode to allow discharge for the mock inverter."""
        return super().set_mode_allow_discharge()

    def set_mode_avoid_discharge(self):
        """Set mode to avoid discharge for the mock inverter."""
        return super().set_mode_avoid_discharge()


# --- Fixtures ---
@pytest.fixture
def config():
    """Provide a mock configuration dictionary."""
    return {"address": "192.168.0.100", "user": "admin", "password": "secret"}


@pytest.fixture
def inverter(config):
    """Provide a mock inverter instance."""
    return MockInverter(config)


# --- Tests ---


def test_initialization(inverter, config):
    """Test that the inverter initializes correctly."""
    assert inverter.address == config["address"]
    assert inverter.user == config["user"].lower()
    assert inverter.password == config["password"]
    assert inverter.inverter_type == "MockInverter"
    assert inverter.is_authenticated is False


def test_connect_inverter(inverter):
    """Test connecting to the inverter."""
    result = inverter.connect_inverter()
    assert result is True
    assert inverter.inverter_connected is True


def test_disconnect_inverter(inverter):
    """Test disconnecting from the inverter."""
    result = inverter.disconnect_inverter()
    assert result is True
    assert inverter.inverter_connected is False


def test_authenticate_sets_flag(inverter):
    """Test that authentication sets the flag."""
    result = inverter.authenticate()
    assert result is True
    assert inverter.is_authenticated is True


def test_set_battery_mode(inverter):
    """Test setting the battery mode."""
    result = inverter.set_battery_mode("normal")
    assert result is True
    assert inverter.last_mode_set == "normal"


def test_set_mode_avoid_discharge(inverter):
    """Test setting mode to avoid discharge."""
    inverter.set_mode_avoid_discharge()
    assert inverter.last_mode_set == "hold"


def test_set_mode_allow_discharge(inverter):
    """Test setting mode to allow discharge."""
    inverter.set_mode_allow_discharge()
    assert inverter.last_mode_set == "normal"


def test_get_battery_info(inverter):
    """Test getting battery info."""
    info = inverter.get_battery_info()
    assert isinstance(info, dict)
    assert "charge" in info
    assert "status" in info


def test_fetch_inverter_data(inverter):
    """Test fetching inverter data."""
    data = inverter.fetch_inverter_data()
    assert isinstance(data, dict)
    assert "voltage" in data
    assert "current" in data


def test_set_mode_force_charge(inverter):
    """Test setting force charge mode."""
    result = inverter.set_mode_force_charge(500)
    assert result is True
    assert inverter.forced_charge_power == 500


def test_disconnect_logs(caplog, inverter):
    """Test that disconnect logs the session closure."""
    with caplog.at_level("INFO"):
        inverter.disconnect()
    assert f"[{inverter.inverter_type}] Session closed" in caplog.text
