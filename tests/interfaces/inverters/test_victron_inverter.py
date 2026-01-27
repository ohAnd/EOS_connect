"""
Unit tests for VictronInverter.

Version passend zu deinem aktuellen victron.py:
- __init__ ruft initialize() automatisch auf
- initialize() erstellt ModbusTcpClient und ruft connect_inverter()
- daher: ModbusTcpClient wird gemockt, keine echten Netzwerkcalls
- initialize/connect/disconnect werden NICHT als NotImplemented getestet
- NotImplemented-Tests bleiben nur für wirklich stub-methoden
"""

# pylint: disable=import-error,redefined-outer-name,import-outside-toplevel,too-few-public-methods

from __future__ import annotations

import logging
import struct
import pytest
import math


import src.interfaces.inverters.victron as victron_mod
from src.interfaces.inverters import create_inverter, VictronInverter, BaseInverter
from src.interfaces.inverters.ccgx_registers import RegisterDef, Reg, REGISTERS


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


@pytest.fixture
def victron_config():
    return {
        "address": "192.168.1.50",
        "max_pv_charge_rate": 15000,
        "max_grid_charge_rate": 10000,
        "type": "victron",
    }


@pytest.fixture
def victron_instance(monkeypatch, victron_config):
    # Patch ModbusTcpClient used inside victron.py BEFORE instantiation
    # ersetze ModbusTcpClient durch DummyModbusTcpClient
    monkeypatch.setattr(
        victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
    )
    victron_mod.logger.setLevel(logging.INFO)

    return VictronInverter(victron_config)


class TestVictronInverterInitialization:
    def test_initialization_succeeds(self, victron_instance, victron_config):
        assert isinstance(victron_instance, VictronInverter)
        assert victron_instance.address == victron_config["address"]

    def test_inherits_from_base_inverter(self, victron_instance):
        assert isinstance(victron_instance, BaseInverter)

    def test_has_base_inverter_attributes(self, victron_instance):
        assert hasattr(victron_instance, "address")
        assert hasattr(victron_instance, "max_pv_charge_rate")
        assert hasattr(victron_instance, "max_grid_charge_rate")

    def test_client_is_created_and_connected(self, victron_instance):
        assert isinstance(victron_instance.client, DummyModbusTcpClient)
        assert victron_instance.client.connected is True
        assert victron_instance.unit_id == 100
        assert victron_instance.port == 502


class TestVictronInverterCapabilities:
    def test_supports_extended_monitoring_returns_false_by_default(
        self, victron_instance
    ):
        assert victron_instance.supports_extended_monitoring() is False

    def test_has_api_set_max_pv_charge_rate_from_base(self, victron_instance):
        assert hasattr(victron_instance, "api_set_max_pv_charge_rate")


class TestVictronInverterFetchData:
    def test_fetch_inverter_data_reads_soc(self, monkeypatch, victron_instance):
        calls = []

        def fake_read_registers(*args, **kwargs):
            calls.append((args, kwargs))
            return DummyResponse([500], error=False)

        # monkeypatch ersetzt read_registers durch fake_read_registers
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
        def fake_read_registers(*args, **kwargs):
            return DummyResponse([], error=True)

        monkeypatch.setattr(
            victron_instance, "read_registers", fake_read_registers, raising=True
        )

        caplog.set_level(logging.ERROR)

        _ = victron_instance.fetch_inverter_data()

        assert any(
            rec.levelno >= logging.ERROR for rec in caplog.records
        ), "Expected an ERROR log entry when Modbus response isError()==True"


class TestVictronWriteRegister:
    def test_write_register_writes_correct_register(self, victron_instance):
        # Act: ESS max discharge current (fractional) = 49%
        victron_instance.write_register(
            unit=100,
            reg=Reg.SETTINGS_SETTINGS_CGWACS_MAXDISCHARGEPERCENTAGE,
            value=49,
        )

        # Assert: Dummy hat den Schreibzugriff protokolliert
        assert victron_instance.client.last_write == {
            "address": 2702,
            "values": [49],
            "unit": 100,
        }


class TestVictronInverterOptionalMethods:
    def test_api_set_max_pv_charge_rate_uses_base_implementation(
        self, victron_instance
    ):
        victron_instance.api_set_max_pv_charge_rate(5000)


class TestVictronInverterModbusImport:
    def test_imports_victron_module(self):
        from src.interfaces.inverters import victron  # noqa: F401

        assert victron is not None


class TestVictronInverterConfigurationHandling:
    def test_initialization_with_minimal_config(self, monkeypatch):
        monkeypatch.setattr(
            victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
        )
        minimal_config = {"address": "192.168.1.1", "type": "victron"}
        instance = VictronInverter(minimal_config)
        assert instance.address == "192.168.1.1"

    def test_initialization_with_full_config(self, victron_instance, victron_config):
        assert victron_instance.address == "192.168.1.50"
        assert victron_instance.max_pv_charge_rate == 15000
        assert victron_instance.max_grid_charge_rate == 10000


class TestVictronInverterFactory:
    def test_create_inverter_returns_victron(self, monkeypatch, victron_config):
        monkeypatch.setattr(
            victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
        )
        inv = create_inverter(victron_config)
        assert isinstance(inv, VictronInverter)


class TestVictronInverterFutureImplementation:
    def test_has_required_abstract_methods_defined(self, victron_instance):
        required_methods = [
            "initialize",
            "connect_inverter",
            "disconnect_inverter",
            "set_battery_mode",
            "set_mode_avoid_discharge",
            "set_mode_allow_discharge",
            "set_mode_force_charge",
            "set_allow_grid_charging",
            "get_battery_info",
            "fetch_inverter_data",
        ]

        for method in required_methods:
            assert hasattr(victron_instance, method)
            assert callable(getattr(victron_instance, method))


class TestVictronEncodeWords:
    def test_uint16_no_scale(self):
        reg = RegisterDef(address=1, count=1, type="uint16", scale=1.0)
        assert VictronInverter._encode_words(reg, 42) == [42]

    def test_int16_negative(self):
        reg = RegisterDef(address=1, count=1, type="int16", scale=1.0)
        assert VictronInverter._encode_words(reg, -1) == [0xFFFF]

    def test_uint32_big_endian(self):
        reg = RegisterDef(address=1, count=2, type="uint32", scale=1.0)
        assert VictronInverter._encode_words(reg, 0x11223344) == [0x1122, 0x3344]

    def test_int32_negative_one(self):
        reg = RegisterDef(address=1, count=2, type="int32", scale=1.0)
        assert VictronInverter._encode_words(reg, -1) == [0xFFFF, 0xFFFF]

    def test_float32_encoding(self):
        reg = RegisterDef(address=1, count=2, type="float32", scale=1.0)
        value = 12.5

        b = struct.pack(">f", float(value))
        expected = [int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big")]

        assert VictronInverter._encode_words(reg, value) == expected

    def test_string_padded_to_word_count(self):
        reg = RegisterDef(address=1, count=4, type="string[7]", scale=1.0)
        # 4 words => 8 bytes: "ABC" -> 41 42 43 00 00 00 00 00
        assert VictronInverter._encode_words(reg, "ABC") == [
            0x4142,
            0x4300,
            0x0000,
            0x0000,
        ]

    def test_inverse_scale_applied(self):
        reg = RegisterDef(address=1, count=1, type="uint16", scale=10.0)
        # decode multiplies by 10 => write must divide by 10
        assert VictronInverter._encode_words(reg, 50) == [5]

    @pytest.mark.parametrize(
        "type_name,value,expected",
        [
            ("uint64", 0x0102030405060708, [0x0102, 0x0304, 0x0506, 0x0708]),
            ("int64", -1, [0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF]),
        ],
    )
    def test_64bit_types(self, type_name, value, expected):
        reg = RegisterDef(address=1, count=4, type=type_name, scale=1.0)
        assert VictronInverter._encode_words(reg, value) == expected


class TestVictronForceChargeWatts:
    def test_set_mode_force_charge_sets_2701_and_2705(
        self, monkeypatch, victron_instance
    ):
        # --- Arrange ---
        # 1) Alle Writes mitschneiden
        writes = []

        # 2) Original Write Register speichern
        orig_write_register = victron_instance.client.write_register

        # 3) Write Register mit dem neuen Wrapper ersetzen
        def spy_write_register(address, value, device_id=None, unit=None):
            slave = unit if unit is not None else device_id
            writes.append(
                {"address": int(address), "values": [int(value)], "unit": int(slave)}
            )
            return orig_write_register(address, value, device_id=device_id, unit=unit)

        monkeypatch.setattr(
            victron_instance.client, "write_register", spy_write_register, raising=True
        )

        # 2) Battery voltage decode für address=840 fix auf 52.0 V
        orig_decode = RegisterDef.decode

        def fake_decode(self, words):
            if getattr(self, "address", None) == 840:
                return 52.0
            return orig_decode(self, words)

        monkeypatch.setattr(RegisterDef, "decode", fake_decode, raising=True)

        # 3) read_registers so patchen, dass Voltage-Read "erfolgreich" ist
        class DummyResp:
            def __init__(self):
                self.registers = [0]  # egal, weil decode gepatcht

            def isError(self):
                return False

        def fake_read_registers(address, count=1, unit=None):
            # optional: sicherstellen, dass wirklich Voltage gelesen wird
            assert int(address) == REGISTERS[Reg.SYSTEM_DC_BATTERY_VOLTAGE].address
            return DummyResp()

        monkeypatch.setattr(
            victron_instance, "read_registers", fake_read_registers, raising=True
        )

        # --- Act ---
        victron_instance.set_mode_force_charge(3000)  # 3000 W

        # --- Assert ---
        # Erwarteter Strom: ceil(3000/52.0) = 58 A
        expected_a = int(math.ceil(3000 / 52.0))

        # Wir erwarten mindestens 2 Writes:
        # - 2701 (max charge percentage) = 100
        # - 2705 (max charge current) = expected_a
        assert {"address": 2701, "values": [100], "unit": 100} in writes
        assert {"address": 2705, "values": [expected_a], "unit": 100} in writes
