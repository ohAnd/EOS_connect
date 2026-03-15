"""
Unit tests for VictronInverter implementation.

Tests for the Victron Modbus/TCP inverter interface.
Most methods are now implemented; only set_battery_mode() and get_battery_info()
remain as stubs pending implementation.
"""

# pylint: disable=import-error,redefined-outer-name,import-outside-toplevel,too-few-public-methods

import pytest
import src.interfaces.inverters.victron as victron_mod

from src.interfaces.inverters import create_inverter, VictronInverter, BaseInverter


class DummyResponse:
    """Minimal pymodbus-like response stub."""

    def __init__(self, registers, error: bool = False):
        self.registers = registers
        self._error = error

    def isError(self):  # pylint: disable=invalid-name
        """Return whether response indicates an error."""
        return self._error


class DummyModbusTcpClient:
    """Fake ModbusTcpClient for testing without network."""

    def __init__(self, host, port=502):
        """Initialize dummy Modbus TCP client."""
        self.host = host
        self.port = port
        self.connected = False

    def connect(self):
        """Simulate connection to Modbus device."""
        self.connected = True
        return True

    def close(self):
        """Simulate closing Modbus connection."""
        self.connected = False

    def read_holding_registers(
        self, _address, _count, _device_id=None, _unit=None
    ):  # pylint: disable=unused-argument
        """Return a dummy response."""
        return DummyResponse([500], error=False)


@pytest.fixture
def victron_config():
    """Provide a default configuration for VictronInverter."""
    return {
        "address": "192.168.1.200",
        "max_pv_charge_rate": 15000,
        "max_grid_charge_rate": 10000,
        "type": "victron",
    }


@pytest.fixture
def victron_instance(monkeypatch, victron_config):
    """Create a VictronInverter instance with mocked Modbus client."""
    monkeypatch.setattr(
        victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
    )
    return VictronInverter(victron_config)


class TestVictronInverterInitialization:
    """Tests for VictronInverter initialization."""

    def test_initialization_succeeds(self, victron_instance, victron_config):
        """Test that VictronInverter can be instantiated."""
        assert isinstance(victron_instance, VictronInverter)
        assert victron_instance.address == victron_config["address"]

    def test_inherits_from_base_inverter(self, victron_instance):
        """Test that VictronInverter inherits from BaseInverter."""
        assert isinstance(victron_instance, BaseInverter)

    def test_has_base_inverter_attributes(self, victron_instance):
        """Test that inherited base attributes are available."""
        assert hasattr(victron_instance, "address")
        assert hasattr(victron_instance, "max_pv_charge_rate")
        assert hasattr(victron_instance, "max_grid_charge_rate")


class TestVictronInverterCapabilities:
    """Tests for VictronInverter capability detection."""

    def test_supports_extended_monitoring_returns_false_by_default(
        self, victron_instance
    ):
        """Test that extended monitoring is not supported by default."""
        assert victron_instance.supports_extended_monitoring is False

    def test_has_api_set_max_pv_charge_rate_from_base(self, victron_instance):
        """Test that API method exists from base class."""
        assert hasattr(victron_instance, "api_set_max_pv_charge_rate")


class TestVictronInverterStubImplementation:
    """
    Tests for stub implementation methods that still raise NotImplementedError.

    Only set_battery_mode() and get_battery_info() remain unimplemented.
    """

    def test_set_battery_mode_raises_not_implemented(self, victron_instance):
        """Verify set_battery_mode raises NotImplementedError as stub."""
        with pytest.raises(NotImplementedError):
            victron_instance.set_battery_mode("normal")

    def test_get_battery_info_raises_not_implemented(self, victron_instance):
        """Verify get_battery_info raises NotImplementedError as stub."""
        with pytest.raises(NotImplementedError):
            victron_instance.get_battery_info()


class TestVictronFetchInverterData:
    """Tests for fetch_inverter_data() with the new register handling."""

    def test_fetch_inverter_data_calls_read_registers_success(
        self, monkeypatch, victron_instance
    ):
        """
        Ensure fetch_inverter_data does not raise and reads registers.

        We don't assume the exact return type (some implementations return None and only log).
        We only assert the Modbus read is called with unit=100 and that the method does not crash.
        """
        calls = []

        def fake_read_registers(*args, **kwargs):
            # support both signatures:
            #   read_registers(Reg.X, unit=100)
            #   read_registers(address, count=?, unit=100)
            calls.append((args, kwargs))
            return DummyResponse([500], error=False)

        # monkeypatch read_register wird durch fake_read_registers ersetzt
        monkeypatch.setattr(
            victron_instance, "read_registers", fake_read_registers, raising=True
        )

        # should not raise
        _ = victron_instance.fetch_inverter_data()

        assert calls, "fetch_inverter_data() did not call read_registers()"
        # Ensure unit=100 is used (either explicitly passed or defaulted by wrapper)
        assert any(call_kwargs.get("unit", 100) == 100 for _, call_kwargs in calls)

    def test_fetch_inverter_data_handles_error_response(
        self, monkeypatch, victron_instance
    ):
        """If Modbus response isError(), fetch_inverter_data should not raise."""

        def fake_read_registers(*_args, **_kwargs):  # pylint: disable=unused-argument
            return DummyResponse([], error=True)

        monkeypatch.setattr(
            victron_instance, "read_registers", fake_read_registers, raising=True
        )

        # should not raise
        _ = victron_instance.fetch_inverter_data()


class TestVictronInverterOptionalMethods:
    """Tests for optional methods inherited from base class."""

    def test_api_set_max_pv_charge_rate_uses_base_implementation(
        self, victron_instance
    ):
        """Test that api_set_max_pv_charge_rate uses safe base implementation."""
        # Should not raise NotImplementedError - uses base class no-op
        victron_instance.api_set_max_pv_charge_rate(5000)


class TestVictronInverterModbusImport:  # pylint: disable=too-few-public-methods
    """Tests for pymodbus import handling."""

    def test_imports_victron_module(self):
        """Test that the victron module can be imported."""
        # Verify module is importable (already imported at module level)
        assert victron_mod is not None


class TestVictronInverterConfigurationHandling:
    """Tests for configuration handling."""

    def test_initialization_with_minimal_config(self, monkeypatch):
        """Test initialization with minimal configuration."""
        monkeypatch.setattr(
            victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
        )
        minimal_config = {"address": "192.168.1.1", "type": "victron"}
        instance = VictronInverter(minimal_config)
        assert instance.address == "192.168.1.1"

    def test_initialization_with_full_config(self, monkeypatch, victron_config):
        """Test initialization with full configuration."""
        monkeypatch.setattr(
            victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
        )
        instance = VictronInverter(victron_config)
        assert instance.address == "192.168.1.200"
        assert instance.max_pv_charge_rate == 15000
        assert instance.max_grid_charge_rate == 10000


class TestVictronInverterFactory:
    """Tests for inverter factory creation (create_inverter)."""

    def test_create_inverter_returns_victron(self, monkeypatch, victron_config):
        """Verify factory creates VictronInverter instance."""
        monkeypatch.setattr(
            victron_mod, "ModbusTcpClient", DummyModbusTcpClient, raising=True
        )
        inv = create_inverter(victron_config)
        assert isinstance(inv, VictronInverter)


class TestVictronInverterFutureImplementation:
    """Tests documenting expected behavior / interface compliance."""

    def test_has_required_abstract_methods_defined(self, victron_instance):
        """Test that all required abstract methods are defined."""
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
