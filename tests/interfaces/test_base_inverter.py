# test_base_inverter.py
import pytest

from src.interfaces.base_inverter import BaseInverter
from unittest.mock import MagicMock


# --- Mock-Klasse für Tests ---
class MockInverter(BaseInverter):

    def initialize(self):
        self.inverter_connected = True

    def connect_inverter(self) -> bool:

        self.inverter_connected = True
        return True

    def disconnect_inverter(self) -> bool:
        self.inverter_connected = False
        return True

    def set_battery_mode(self, mode: str) -> bool:
        self.last_mode_set = mode
        return True

    def get_battery_info(self) -> dict:
        return {"charge": 50, "status": "ok"}

    def fetch_inverter_data(self) -> dict:
        return {"voltage": 230, "current": 10}

    def set_mode_force_charge(self, charge_power_w: int) -> bool:
        self.forced_charge_power = charge_power_w
        return True

    def set_allow_grid_charging(self, value):
        self.grid_charging_allowed = value

    def set_mode_allow_discharge(self):
        return super().set_mode_allow_discharge()

    def set_mode_avoid_discharge(self):
        return super().set_mode_avoid_discharge()


# --- Fixtures ---
@pytest.fixture
def config():
    return {"address": "192.168.0.100", "user": "admin", "password": "secret"}


@pytest.fixture
def inverter(config):
    return MockInverter(config)


# --- Tests ---


def test_initialization(inverter, config):
    assert inverter.address == config["address"]
    assert inverter.user == config["user"].lower()
    assert inverter.password == config["password"]
    assert inverter.inverter_type == "MockInverter"
    assert inverter.is_authenticated is False


def test_connect_inverter(inverter):
    result = inverter.connect_inverter()
    assert result is True
    assert inverter.inverter_connected is True


def test_disconnect_inverter(inverter):
    result = inverter.disconnect_inverter()
    assert result is True
    assert inverter.inverter_connected is False


def test_authenticate_sets_flag(inverter):
    result = inverter.authenticate()
    assert result is True
    assert inverter.is_authenticated is True


def test_set_battery_mode(inverter):
    result = inverter.set_battery_mode("normal")
    assert result is True
    assert inverter.last_mode_set == "normal"


def test_set_mode_avoid_discharge(inverter):
    inverter.set_mode_avoid_discharge()
    assert inverter.last_mode_set == "hold"


def test_set_mode_allow_discharge(inverter):
    inverter.set_mode_allow_discharge()
    assert inverter.last_mode_set == "normal"


def test_get_battery_info(inverter):
    info = inverter.get_battery_info()
    assert isinstance(info, dict)
    assert "charge" in info
    assert "status" in info


def test_fetch_inverter_data(inverter):
    data = inverter.fetch_inverter_data()
    assert isinstance(data, dict)
    assert "voltage" in data
    assert "current" in data


def test_set_mode_force_charge(inverter):
    result = inverter.set_mode_force_charge(500)
    assert result is True
    assert inverter.forced_charge_power == 500


def test_disconnect_logs(caplog, inverter):
    with caplog.at_level("INFO"):
        inverter.disconnect()
    assert f"[{inverter.inverter_type}] Session closed" in caplog.text
