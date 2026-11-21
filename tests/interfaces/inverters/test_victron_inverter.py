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
        """Initialize dummy response with registers and error flag."""
        self.registers = registers or []
        self._error = error

    def isError(self):  # pylint: disable=invalid-name
        """Return whether response indicates an error."""
        return self._error


class DummyModbusTcpClient:
    """Fake for pymodbus.client.ModbusTcpClient."""

    def __init__(self, host, port=502):
        """Initialize dummy Modbus TCP client."""
        self.host = host
        self.port = port
        self.connected = False
        self.closed = False
        self.last_read = None
        self.last_write = None
        self.next_response = DummyResponse([500], error=False)

    def connect(self):
        """Simulate connection to Modbus device."""
        self.connected = True
        return True

    def close(self):
        """Simulate closing Modbus connection."""
        self.closed = True
        self.connected = False

    def read_holding_registers(self, address, count, device_id=None, unit=None):
        """Simulate reading Modbus holding registers."""
        slave = unit if unit is not None else device_id
        self.last_read = {
            "address": int(address),
            "count": int(count),
            "unit": int(slave),
        }
        return self.next_response

    def write_register(self, address, value, device_id=None, unit=None):
        """Simulate writing single Modbus register."""
        slave = unit if unit is not None else device_id
        self.last_write = {
            "address": int(address),
            "values": [int(value)],
            "unit": int(slave),
        }
        return True

    def write_registers(self, address, values, device_id=None, unit=None):
        """Simulate writing multiple Modbus registers."""
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
            reg=Reg.SETTINGS_Settings_Cgwacs_MaxDischargePercentage,
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
    """Test encode_words static method for various data types."""

    def test_uint16_no_scale(self):
        """Verify uint16 encoding with no scale applied."""
        reg = RegisterDef(address=1, count=1, type="uint16", scale=1.0)
        assert VictronInverter.encode_words(reg, 50) == [50]

    def test_int16_negative(self):
        """Verify int16 negative value encoding as two's complement."""
        reg = RegisterDef(address=1, count=1, type="int16", scale=1.0)
        assert VictronInverter.encode_words(reg, -1) == [0xFFFF]

    def test_uint32_big_endian(self):
        """Verify uint32 big-endian encoding across two words."""
        reg = RegisterDef(address=1, count=2, type="uint32", scale=1.0)
        assert VictronInverter.encode_words(reg, 0x11223344) == [
            0x1122,
            0x3344,
        ]

    def test_int32_negative_one(self):
        """Verify int32 negative value as two's complement across two words."""
        reg = RegisterDef(address=1, count=2, type="int32", scale=1.0)
        assert VictronInverter.encode_words(reg, -1) == [
            0xFFFF,
            0xFFFF,
        ]

    def test_float32_encoding(self):
        """Verify float32 IEEE 754 encoding across two words."""
        reg = RegisterDef(address=1, count=2, type="float32", scale=1.0)
        value = 12.5

        b = struct.pack(">f", float(value))
        expected = [int.from_bytes(b[0:2], "big"), int.from_bytes(b[2:4], "big")]

        assert VictronInverter.encode_words(reg, value) == expected

    def test_string_padded_to_word_count(self):
        """Verify string encoding padded to full word count with null bytes."""
        reg = RegisterDef(address=1, count=4, type="string[7]", scale=1.0)
        # 4 words => 8 bytes: "ABC" -> 41 42 43 00 00 00 00 00
        assert VictronInverter.encode_words(reg, "ABC") == [
            0x4142,
            0x4300,
            0x0000,
            0x0000,
        ]

    def test_inverse_scale_applied(self):
        """Verify inverse scale factor is applied during encoding."""
        reg = RegisterDef(address=1, count=1, type="uint16", scale=10.0)
        # decode multiplies by 10 => write must divide by 10
        assert VictronInverter.encode_words(reg, 50) == [5]

    @pytest.mark.parametrize(
        "type_name,value,expected",
        [
            ("uint64", 0x0102030405060708, [0x0102, 0x0304, 0x0506, 0x0708]),
            ("int64", -1, [0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF]),
        ],
    )
    def test_64bit_types(self, type_name, value, expected):
        """Verify uint64 and int64 encoding across four words."""
        reg = RegisterDef(address=1, count=4, type=type_name, scale=1.0)
        assert VictronInverter.encode_words(reg, value) == expected


class TestVictronForceCharge:
    """Test set_mode_force_charge with power-to-current calculation."""

    def test_set_mode_force_charge_sets_percentage_and_current(
        self, monkeypatch, victron_instance
    ):
        """Verify force charge writes VE.Bus setpoints."""
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

        # Mock write_register_verified to skip verification
        def fake_write_register_verified(reg, value, retries=0, delay_s=0.0):
            victron_instance.write_register(
                unit=victron_instance.unit_id, reg=reg, value=value
            )
            return True

        monkeypatch.setattr(
            victron_instance,
            "write_register_verified",
            fake_write_register_verified,
            raising=True,
        )

        # Act: Request force charge (500W distributed across 3 phases)
        victron_instance.set_mode_force_charge(500)

        # Assert: VE.Bus setpoints were written (addresses 37, 40, 41 on unit 227)
        # with value 167W (500W / 3 per phase ≈ 167W)
        assert {"address": 37, "values": [167], "unit": 227} in writes
        assert {"address": 40, "values": [167], "unit": 227} in writes
        assert {"address": 41, "values": [167], "unit": 227} in writes


class TestVictronAvoidDischargeExternalControl:
    """Test set_mode_avoid_discharge() with External Control + VE.Bus keepalive."""

    def test_set_mode_avoid_discharge_sets_hub4mode_and_starts_keepalive(
        self, monkeypatch, victron_instance
    ):
        """Verify avoid discharge mode sets Hub4Mode and starts VE.Bus keepalive."""
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
        assert ka["interval_s"] == 1.0

        assert ka["targets"][Reg.VEBUS_Hub4_L1_AcPowerSetpoint_37] == 0
        assert ka["targets"][Reg.VEBUS_Hub4_L2_AcPowerSetpoint_40] == 0
        assert ka["targets"][Reg.VEBUS_Hub4_L3_AcPowerSetpoint_41] == 0


# =========================================================================
# Avoid discharge via ESS minimum SoC (issue #344)
# =========================================================================

ADDR_SOC = REGISTERS[Reg.SYSTEM_Dc_Battery_Soc].address  # 843
ADDR_BL_STATE = REGISTERS[Reg.SETTINGS_Settings_CGwacs_BatteryLife_State].address
ADDR_MIN_SOC = REGISTERS[
    Reg.SETTINGS_Settings_CGwacs_BatteryLife_MinimumSocLimit
].address  # 2901
ADDR_HUB4MODE = REGISTERS[Reg.SETTINGS_Settings_Cgwacs_Hub4Mode].address  # 2902


class StatefulModbusClient(DummyModbusTcpClient):
    """Modbus fake that remembers written registers, so readback works."""

    def __init__(self, host, port=502):
        """Start from a GX device in normal ESS with a 10 % minimum SoC."""
        super().__init__(host, port)
        self.store: dict[int, int] = {
            ADDR_SOC: 62,
            ADDR_BL_STATE: 10,
            ADDR_MIN_SOC: 100,  # raw: 10.0 %
            ADDR_HUB4MODE: 1,
        }
        self.writes: list[tuple[int, int]] = []

    def read_holding_registers(self, address, count, device_id=None, unit=None):
        """Return the stored word for the address (0 if never set)."""
        return DummyResponse([self.store.get(int(address), 0)] * int(count))

    def write_register(self, address, value, device_id=None, unit=None):
        """Store the word and log the write."""
        self.store[int(address)] = int(value)
        self.writes.append((int(address), int(value)))
        return True


@pytest.fixture
def make_min_soc_victron(monkeypatch):
    """Factory for a VictronInverter on the stateful fake, sleeps disabled."""
    monkeypatch.setattr(
        victron_mod, "ModbusTcpClient", StatefulModbusClient, raising=True
    )
    monkeypatch.setattr(victron_mod.time, "sleep", lambda _s: None)
    victron_mod.logger.setLevel(logging.INFO)

    def _make(**extra):
        config = {
            "address": "192.168.1.50",
            "type": "victron",
            "victron_avoid_discharge_method": "min_soc",
            "battery_min_soc_percentage": 15,
        }
        config.update(extra)
        return VictronInverter(config)

    return _make


class TestVictronAvoidDischargeMinSoc:
    """Avoid discharge via the ESS minimum SoC instead of a 0 W setpoint."""

    def test_default_method_is_external_control(self, victron_instance):
        """Without the option, the tested 0 W external-control path stays."""
        assert victron_instance.avoid_discharge_method == "external_control"

    def test_unknown_method_falls_back_to_external_control(
        self, make_min_soc_victron, caplog
    ):
        """A typo must not silently enable an untested path."""
        with caplog.at_level(logging.WARNING):
            inv = make_min_soc_victron(victron_avoid_discharge_method="minsoc")
        assert inv.avoid_discharge_method == "external_control"
        assert "Unknown victron_avoid_discharge_method" in caplog.text

    def test_avoid_discharge_raises_min_soc_to_current_soc(
        self, make_min_soc_victron, monkeypatch
    ):
        """SoC 62 % -> reg 2901 raw 620; ESS mode untouched, no keepalive."""
        inv = make_min_soc_victron()
        client = inv.client
        keepalive_calls = []
        monkeypatch.setattr(
            inv, "start_vebus_keepalive", lambda *a, **k: keepalive_calls.append(a)
        )

        inv.set_mode_avoid_discharge()

        assert client.store[ADDR_MIN_SOC] == 620
        assert client.store[ADDR_HUB4MODE] == 1
        assert ADDR_HUB4MODE not in [addr for addr, _ in client.writes]
        assert not keepalive_calls
        assert inv._min_soc_override_active  # pylint: disable=protected-access

    def test_min_soc_never_below_restore_target(self, make_min_soc_victron):
        """At a SoC under the battery minimum, the minimum itself is held."""
        inv = make_min_soc_victron()
        inv.client.store[ADDR_SOC] = 8

        inv.set_mode_avoid_discharge()

        assert inv.client.store[ADDR_MIN_SOC] == 150

    def test_allow_discharge_restores_battery_min_soc(self, make_min_soc_victron):
        """Discharge allowed puts reg 2901 back to battery.min_soc_percentage."""
        inv = make_min_soc_victron()
        inv.set_mode_avoid_discharge()

        inv.set_mode_allow_discharge()

        assert inv.client.store[ADDR_MIN_SOC] == 150
        assert not inv._min_soc_override_active  # pylint: disable=protected-access

    def test_allow_discharge_after_restart_resets_raised_min_soc(
        self, make_min_soc_victron
    ):
        """A raised minimum left over from before a restart is reset, too."""
        inv = make_min_soc_victron()
        inv.client.store[ADDR_MIN_SOC] = 630  # override survived a restart

        inv.set_mode_allow_discharge()

        assert inv.client.store[ADDR_MIN_SOC] == 150

    def test_allow_discharge_skips_write_when_already_restored(
        self, make_min_soc_victron
    ):
        """No needless settings write when 2901 already holds the target."""
        inv = make_min_soc_victron()
        inv.client.store[ADDR_MIN_SOC] = 150

        inv.set_mode_allow_discharge()

        assert ADDR_MIN_SOC not in [addr for addr, _ in inv.client.writes]

    def test_avoid_discharge_after_force_charge_leaves_external_control(
        self, make_min_soc_victron
    ):
        """From force charge, ESS returns to normal mode before the hold."""
        inv = make_min_soc_victron()
        inv.set_mode_force_charge(3000)
        assert inv.client.store[ADDR_HUB4MODE] == 3

        inv.set_mode_avoid_discharge()
        inv.stop_vebus_keepalive(write_zero=False)

        assert inv.client.store[ADDR_HUB4MODE] == 1
        assert inv.client.store[ADDR_MIN_SOC] == 620
        hub4_write = max(
            i
            for i, (addr, val) in enumerate(inv.client.writes)
            if addr == ADDR_HUB4MODE and val == 1
        )
        min_soc_write = max(
            i for i, (addr, _) in enumerate(inv.client.writes) if addr == ADDR_MIN_SOC
        )
        assert hub4_write < min_soc_write

    def test_force_charge_releases_min_soc_hold(self, make_min_soc_victron):
        """Switching to grid charging does not leave the raised minimum behind."""
        inv = make_min_soc_victron()
        inv.set_mode_avoid_discharge()

        inv.set_mode_force_charge(3000)
        inv.stop_vebus_keepalive(write_zero=False)

        assert inv.client.store[ADDR_MIN_SOC] == 150

    def test_shutdown_restores_min_soc(self, make_min_soc_victron):
        """Shutdown resets reg 2901 before restoring the ESS mode."""
        inv = make_min_soc_victron()
        inv.set_mode_avoid_discharge()

        inv.shutdown()

        assert inv.client.store[ADDR_MIN_SOC] == 150
        assert inv.client.closed

    def test_min_soc_readback_compares_raw_words(self, make_min_soc_victron):
        """Reg 2901 is verified as raw tenths of a percent, not via decode().

        RegisterDef.decode() multiplies by the scalefactor, which would turn raw
        620 into 6200 and fail every verification.
        """
        inv = make_min_soc_victron()
        rdef = REGISTERS[Reg.SETTINGS_Settings_CGwacs_BatteryLife_MinimumSocLimit]
        assert rdef.decode([620]) != 62  # the trap this path avoids

        inv._write_min_soc_pct(62)  # pylint: disable=protected-access

        assert inv.client.store[ADDR_MIN_SOC] == 620
        assert inv._read_min_soc_pct() == 62  # pylint: disable=protected-access

    def test_missing_battery_min_soc_uses_default(self, make_min_soc_victron):
        """Without a battery minimum the restore target falls back to 5 %."""
        inv = make_min_soc_victron(battery_min_soc_percentage=None)
        assert inv.restore_min_soc_pct == 5.0
