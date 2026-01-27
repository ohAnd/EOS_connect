"""
Unit tests for VictronInverter (fresh scaffold).

Goal:
- Start minimal and add tests step-by-step.
- No real network calls: ModbusTcpClient is patched with DummyModbusTcpClient.
- supports_extended_monitoring is a boolean attribute (capability flag), not a method.
"""

# pylint: disable=import-error,redefined-outer-name,import-outside-toplevel,too-few-public-methods

from __future__ import annotations

import logging
import pytest

import src.interfaces.inverters.victron as victron_mod
from src.interfaces.inverters import VictronInverter, BaseInverter


# -------------------------
# Dummies / Fakes
# -------------------------


class DummyResponse:
    """Minimal pymodbus-like response stub."""

    def __init__(self, registers=None, error: bool = False):
        self.registers = registers or []
        self._error = error

    def isError(self) -> bool:  # pymodbus compatibility
        return self._error


class DummyModbusTcpClient:
    """Fake for pymodbus.client.ModbusTcpClient used by victron.py"""

    def __init__(self, host, port=502):
        self.host = host
        self.port = port
        self.connected = False
        self.closed = False

        # record last operations
        self.last_read = None
        self.last_write = None

        # configurable response for reads
        self.next_response = DummyResponse([0], error=False)

    def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.closed = True
        self.connected = False

    def read_holding_registers(self, address, count, device_id=None, unit=None):
        slave = unit if unit is not None else device_id
        self.last_read = {
            "address": int(address),
            "count": int(count),
            "unit": int(slave),
        }
        return self.next_response

    def write_register(self, address, value, device_id=None, unit=None):
        slave = unit if unit is not None else device_id
        self.last_write = {
            "address": int(address),
            "values": [int(value)],
            "unit": int(slave),
        }
        return True

    def write_registers(self, address, values, device_id=None, unit=None):
        slave = unit if unit is not None else device_id
        self.last_write = {
            "address": int(address),
            "values": [int(v) for v in values],
            "unit": int(slave),
        }
        return True


# -------------------------
# Fixtures
# -------------------------


@pytest.fixture
def victron_config():
    """Minimal config that matches your BaseInverter expectations."""
    return {
        "address": "192.168.1.50",
        "type": "victron",
        # optional:
        "max_pv_charge_rate": 15000,
        "max_grid_charge_rate": 10000,
    }


@pytest.fixture
def victron_instance(monkeypatch, victron_config):
    """
    Patch ModbusTcpClient used inside victron.py BEFORE instantiation
    so VictronInverter() does not do real network calls.
    """
    monkeypatch.setattr(
        victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
    )
    victron_mod.logger.setLevel(logging.INFO)

    return VictronInverter(victron_config)


# -------------------------
# Tests start here (keep minimal)
# -------------------------


def test_victron_initializes_and_inherits_base(victron_instance):
    """Sanity test: instance can be created and is a BaseInverter."""
    assert isinstance(victron_instance, VictronInverter)
    assert isinstance(victron_instance, BaseInverter)


def test_victron_creates_dummy_modbus_client_and_connects(victron_instance):
    """Sanity test: client is our dummy and connect() was called."""
    assert isinstance(victron_instance.client, DummyModbusTcpClient)
    assert victron_instance.client.connected is True


def test_victron_supports_extended_monitoring_is_bool_and_default_false(
    victron_instance,
):
    # 1) Attribut existiert
    assert hasattr(victron_instance, "supports_extended_monitoring")

    # 2) Ist wirklich ein bool (kein callable mehr)
    assert isinstance(victron_instance.supports_extended_monitoring, bool)

    # 3) Default-Wert (wie aktuell im VictronInverter gesetzt)
    assert victron_instance.supports_extended_monitoring is False
