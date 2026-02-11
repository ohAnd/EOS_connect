"""
Unit tests for VictronInverter.

Tests Victron-specific functionality:
- Modbus operations and register handling
- Force charge calculations
- Register encoding/decoding

Common interface compliance tests are inherited from BaseInverterTestSuite.
"""

# pylint: disable=import-error,redefined-outer-name,import-outside-toplevel,too-few-public-methods

from __future__ import annotations

import logging
import struct
import pytest
import math

import src.interfaces.inverters.victron as victron_mod
from src.interfaces.inverters import VictronInverter
from src.interfaces.inverters.ccgx_registers_all import RegisterDef, Reg, REGISTERS
from .base_inverter_tests import BaseInverterTestSuite


# =========================================================================
# Mock Objects
# =========================================================================


class DummyResponse:
    """Minimal pymodbus-like response stub."""

    def __init__(self, registers=None, error: bool = False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class DummyModbusTcpClient:
    """Fake for pymodbus.client.ModbusTcpClient."""

    def __init__(self, host, port=502):
        self.host = host
        self.port = port
        self.connected = False
        self.closed = False
        self.last_read = None
        self.last_write = None
        self.next_response = DummyResponse([500], error=False)

    def connect(self):
        self.connected = True
        return True

    def close(self):
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


# =========================================================================
# Base Test Suite Extension
# =========================================================================


class TestVictronInverterBase(BaseInverterTestSuite):
    """Tests common BaseInverter interface compliance for VictronInverter."""

    inverter_class = VictronInverter  # type: ignore[assignment]
    minimal_config = {"address": "192.168.1.50", "type": "victron"}  # type: ignore[assignment]
    expected_extended_monitoring = False  # type: ignore[assignment]

    @classmethod
    def setup_mocks(cls, monkeypatch):
        """Set up ModbusTcpClient mock before instantiation."""
        monkeypatch.setattr(
            victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
        )
        victron_mod.logger.setLevel(logging.INFO)


# =========================================================================
# Victron-Specific Tests
# =========================================================================


@pytest.fixture
def victron_config():
    """Victron configuration with all options."""
    return {
        "address": "192.168.1.50",
        "max_pv_charge_rate": 15000,
        "max_grid_charge_rate": 10000,
        "type": "victron",
    }


@pytest.fixture
def victron_instance(monkeypatch, victron_config):
    """Create VictronInverter instance with mocked Modbus client."""
    monkeypatch.setattr(
        victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
    )
    victron_mod.logger.setLevel(logging.INFO)
    return VictronInverter(victron_config)


class TestVictronModbusInitialization:
    """Test Victron-specific initialization (Modbus client setup)."""

    def test_client_is_created_and_connected(self, victron_instance):
        """Verify Modbus client is created and connected during init."""
        assert isinstance(victron_instance.client, DummyModbusTcpClient)
        assert victron_instance.client.connected is True
        assert victron_instance.unit_id == 100
        assert victron_instance.port == 502


class TestVictronFetchInverterData:
    """Test fetch_inverter_data with Modbus operations."""

    def test_fetch_inverter_data_reads_registers(self, monkeypatch, victron_instance):
        """Verify fetch_inverter_data calls read_registers."""
        calls = []

        def fake_read_registers(*_args, **_kwargs):  # pylint: disable=unused-argument
            calls.append((_args, _kwargs))
            return DummyResponse([500], error=False)

        monkeypatch.setattr(
            victron_instance, "read_registers", fake_read_registers, raising=True
        )

        result = victron_instance.fetch_inverter_data()

        assert calls, "fetch_inverter_data() did not call read_registers()"
        assert any(kwargs.get("unit") in (None, 100) for _, kwargs in calls)
        assert result is None or isinstance(result, (int, float, str, dict))

    def test_fetch_inverter_data_logs_error_on_modbus_error(
        self, monkeypatch, victron_instance, caplog
    ):
        """Verify error logging when Modbus response indicates an error."""

        def fake_read_registers(*_args, **_kwargs):  # pylint: disable=unused-argument
            return DummyResponse([], error=True)

        monkeypatch.setattr(
            victron_instance, "read_registers", fake_read_registers, raising=True
        )

        caplog.set_level(logging.ERROR)
        _ = victron_instance.fetch_inverter_data()

        assert any(
            rec.levelno >= logging.ERROR for rec in caplog.records
        ), "Expected ERROR log when Modbus isError()==True"


class TestVictronWriteRegister:
    """Test write_register with specific registers."""

    def test_write_register_writes_correct_address_and_value(self, victron_instance):
        """Verify write_register writes to correct Modbus address."""
        # Act: ESS max discharge current (fractional) = 49%
        victron_instance.write_register(
            unit=100,
            reg=Reg.SETTINGS_SETTINGS_CGWACS_MAXDISCHARGEPERCENTAGE,
            value=49,
        )

        # Assert: Dummy client recorded the write
        assert victron_instance.client.last_write == {
            "address": 2702,
            "values": [49],
            "unit": 100,
        }


class TestVictronStubMethods:
    """Test methods that are still stubs (raise NotImplementedError)."""

    def test_set_battery_mode_raises_not_implemented(self, victron_instance):
        """set_battery_mode is not yet implemented for Victron."""
        with pytest.raises(NotImplementedError):
            victron_instance.set_battery_mode("normal")

    def test_get_battery_info_raises_not_implemented(self, victron_instance):
        """get_battery_info is not yet implemented for Victron."""
        with pytest.raises(NotImplementedError):
            victron_instance.get_battery_info()


class TestVictronEncodeWords:
    """Test _encode_words static method for various data types."""

    def test_uint16_no_scale(self):
        reg = RegisterDef(address=1, count=1, type="uint16", scale=1.0)
        assert VictronInverter._encode_words(reg, 50) == [
            50
        ]  # pylint: disable=protected-access

    def test_int16_negative(self):
        reg = RegisterDef(address=1, count=1, type="int16", scale=1.0)
        assert VictronInverter._encode_words(reg, -1) == [
            0xFFFF
        ]  # pylint: disable=protected-access

    def test_uint32_big_endian(self):
        reg = RegisterDef(address=1, count=2, type="uint32", scale=1.0)
        assert VictronInverter._encode_words(reg, 0x11223344) == [
            0x1122,
            0x3344,
        ]  # pylint: disable=protected-access

    def test_int32_negative_one(self):
        reg = RegisterDef(address=1, count=2, type="int32", scale=1.0)
        assert VictronInverter._encode_words(reg, -1) == [
            0xFFFF,
            0xFFFF,
        ]  # pylint: disable=protected-access

    def test_float32_encoding(self):
        reg = RegisterDef(address=1, count=2, type="float32", scale=1.0)
        value = 12.5

        b = struct.pack(">f", float(value))
        expected = [int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big")]

        assert (
            VictronInverter._encode_words(reg, value) == expected
        )  # pylint: disable=protected-access

    def test_string_padded_to_word_count(self):
        reg = RegisterDef(address=1, count=4, type="string[7]", scale=1.0)
        # 4 words => 8 bytes: "ABC" -> 41 42 43 00 00 00 00 00
        # pylint: disable=protected-access
        assert VictronInverter._encode_words(reg, "ABC") == [
            0x4142,
            0x4300,
            0x0000,
            0x0000,
        ]

    def test_inverse_scale_applied(self):
        reg = RegisterDef(address=1, count=1, type="uint16", scale=10.0)
        # decode multiplies by 10 => write must divide by 10
        assert VictronInverter._encode_words(reg, 50) == [
            5
        ]  # pylint: disable=protected-access

    @pytest.mark.parametrize(
        "type_name,value,expected",
        [
            ("uint64", 0x0102030405060708, [0x0102, 0x0304, 0x0506, 0x0708]),
            ("int64", -1, [0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF]),
        ],
    )
    def test_64bit_types(self, type_name, value, expected):
        reg = RegisterDef(address=1, count=4, type=type_name, scale=1.0)
        assert (
            VictronInverter._encode_words(reg, value) == expected
        )  # pylint: disable=protected-access


class TestVictronForceCharge:
    """Test set_mode_force_charge with power-to-current calculation."""

    def test_set_mode_force_charge_sets_percentage_and_current(
        self, monkeypatch, victron_instance
    ):
        """Verify force charge writes both max charge percentage and current."""
        # Track all register writes
        writes = []
        orig_write_register = victron_instance.client.write_register

        def spy_write_register(address, value, device_id=None, unit=None):
            slave = unit if unit is not None else device_id
            writes.append(
                {"address": int(address), "values": [int(value)], "unit": int(slave)}
            )
            return orig_write_register(address, value, device_id=device_id, unit=unit)

        monkeypatch.setattr(
            victron_instance.client, "write_register", spy_write_register, raising=True
        )

        # Mock battery voltage read to return fixed 52.0V
        orig_decode = RegisterDef.decode

        def fake_decode(self, words):
            if getattr(self, "address", None) == 840:
                return 52.0
            return orig_decode(self, words)

        monkeypatch.setattr(RegisterDef, "decode", fake_decode, raising=True)

        # Mock read_registers to return successful response
        class DummyResp:
            def __init__(self):
                self.registers = [0]  # value doesn't matter, decode is patched

            def isError(self):
                return False

        def fake_read_registers(
            address, count=1, unit=None
        ):  # pylint: disable=unused-argument
            assert int(address) == REGISTERS[Reg.SYSTEM_DC_BATTERY_VOLTAGE].address
            return DummyResp()

        monkeypatch.setattr(
            victron_instance, "read_registers", fake_read_registers, raising=True
        )

        # Act: Request 3000W force charge
        victron_instance.set_mode_force_charge(3000)

        # Assert: Expected current = ceil(3000 / 52.0) = 58A
        expected_a = int(math.ceil(3000 / 52.0))

        # Verify both registers were written:
        # - 2701 (max charge percentage) = 100
        # - 2705 (max charge current) = expected_a
        assert {"address": 2701, "values": [100], "unit": 100} in writes
        assert {"address": 2705, "values": [expected_a], "unit": 100} in writes


class TestVictronAvoidDischargeExternalControl:
    """Test set_mode_avoid_discharge() with External Control + VE.Bus keepalive."""

    def test_set_mode_avoid_discharge_sets_hub4mode_and_starts_keepalive(
        self, monkeypatch, victron_instance
    ):
        calls_write_verified = []
        calls_write_holding = []
        calls_keepalive = []

        # --- spies ---
        def fake_write_register_verified(reg, value, retries=0, delay_s=0.0):
            calls_write_verified.append(
                {"reg": reg, "value": value, "retries": retries, "delay_s": delay_s}
            )
            return True

        def fake_write_holding_registers(unit, address, values):
            calls_write_holding.append(
                {"unit": int(unit), "address": int(address), "values": int(values)}
            )
            return True

        def fake_start_vebus_keepalive(targets, interval_s=0.5, unit=227):
            calls_keepalive.append(
                {
                    "targets": dict(targets),
                    "interval_s": float(interval_s),
                    "unit": int(unit),
                }
            )
            return True

        monkeypatch.setattr(
            victron_instance,
            "write_register_verified",
            fake_write_register_verified,
            raising=True,
        )
        monkeypatch.setattr(
            victron_instance,
            "write_holding_registers",
            fake_write_holding_registers,
            raising=True,
        )
        monkeypatch.setattr(
            victron_instance,
            "start_vebus_keepalive",
            fake_start_vebus_keepalive,
            raising=True,
        )

        # --- act ---
        victron_instance.set_mode_avoid_discharge()

        # --- assert: Hub4Mode set to external control (3) ---
        assert calls_write_verified, "Expected write_register_verified to be called"
        assert calls_write_verified[0]["reg"] == Reg.SETTINGS_Settings_Cgwacs_Hub4Mode
        assert calls_write_verified[0]["value"] == 3

        # --- assert: initial writes for the 3 VE.Bus phase setpoints ---
        expected_regs = [
            Reg.VEBUS_Hub4_L1_AcPowerSetpoint_37,
            Reg.VEBUS_Hub4_L2_AcPowerSetpoint_40,
            Reg.VEBUS_Hub4_L3_AcPowerSetpoint_41,
        ]
        expected_addrs = sorted([REGISTERS[r].address for r in expected_regs])

        assert len(calls_write_holding) == 3, "Expected exactly 3 initial VE.Bus writes"
        got_addrs = sorted([c["address"] for c in calls_write_holding])
        assert got_addrs == expected_addrs

        for c in calls_write_holding:
            assert c["unit"] == 227
            assert c["values"] == 0

        # --- assert: keepalive started once with correct params ---
        assert (
            len(calls_keepalive) == 1
        ), "Expected start_vebus_keepalive to be called once"
        ka = calls_keepalive[0]
        assert ka["unit"] == 227
        assert ka["interval_s"] == 0.5

        assert ka["targets"][Reg.VEBUS_Hub4_L1_AcPowerSetpoint_37] == 0
        assert ka["targets"][Reg.VEBUS_Hub4_L2_AcPowerSetpoint_40] == 0
        assert ka["targets"][Reg.VEBUS_Hub4_L3_AcPowerSetpoint_41] == 0
