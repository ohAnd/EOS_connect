"""
Base test suite for inverter implementations.

This module provides common test patterns that all inverter implementations should pass.
Each specific inverter test file should extend BaseInverterTestSuite and override
the class attributes to test its specific implementation.

Usage:
    class TestMyInverter(BaseInverterTestSuite):
        inverter_class = MyInverter
        minimal_config = {"address": "192.168.1.1", "type": "my_type"}
        expected_extended_monitoring = False
"""

# pylint: disable=too-few-public-methods

from typing import Optional, Type
import pytest
from src.interfaces.inverters import (
    create_inverter,
    BaseInverter,
)  # pylint: disable=import-error


class BaseInverterTestSuite:
    """
    Base test suite providing common tests for all inverter implementations.

    Subclasses must override class attributes:
    - inverter_class: The inverter class to test (e.g., VictronInverter)
    - minimal_config: Minimal configuration dict for initialization
    - expected_extended_monitoring: Expected supports_extended_monitoring value

    Optional overrides:
    - setup_mocks(monkeypatch): Hook to set up necessary mocks before instantiation

    Note: Pylance will report "not callable" errors for inverter_class usage.
    These are false positives - subclasses override inverter_class with concrete
    class objects. The pyright: ignore comments suppress these benign warnings.
    """

    inverter_class: Optional[Type[BaseInverter]] = None  # type: ignore[assignment]
    minimal_config: Optional[dict] = None  # type: ignore[assignment]
    expected_extended_monitoring: Optional[bool] = None  # type: ignore[assignment]

    @classmethod
    def setup_mocks(cls, monkeypatch):
        """
        Override this method to set up mocks before inverter instantiation.

        Example:
            @classmethod
            def setup_mocks(cls, monkeypatch):
                monkeypatch.setattr(victron_mod, "ModbusTcpClient", DummyClient)
        """

    @pytest.fixture
    def inverter_instance(self, monkeypatch):
        """Create an inverter instance with mocks applied."""
        assert (
            self.inverter_class is not None
        ), "Subclass must define inverter_class attribute"
        assert callable(self.inverter_class), "inverter_class must be callable"
        self.setup_mocks(monkeypatch)
        # pylint: disable=not-callable
        return self.inverter_class(
            self.minimal_config
        )  # pyright: ignore[reportGeneralTypeIssues]

    # =========================================================================
    # Interface Compliance Tests
    # =========================================================================

    def test_inverter_class_defined(self):
        """Verify that inverter_class is defined in subclass."""
        assert (
            self.inverter_class is not None
        ), "Subclass must define inverter_class attribute"

    def test_minimal_config_defined(self):
        """Verify that minimal_config is defined in subclass."""
        assert (
            self.minimal_config is not None
        ), "Subclass must define minimal_config attribute"

    def test_inherits_from_base_inverter(self, inverter_instance):
        """All inverters must inherit from BaseInverter."""
        assert isinstance(
            inverter_instance, BaseInverter
        ), f"{self.inverter_class.__name__} must inherit from BaseInverter"

    def test_has_address_attribute(self, inverter_instance):
        """All inverters must have address attribute from config."""
        assert hasattr(inverter_instance, "address")
        assert inverter_instance.address is not None

    def test_has_inverter_type_attribute(self, inverter_instance):
        """All inverters must have inverter_type attribute."""
        assert hasattr(inverter_instance, "inverter_type")
        assert inverter_instance.inverter_type == self.inverter_class.__name__

    def test_has_is_authenticated_attribute(self, inverter_instance):
        """All inverters must have is_authenticated attribute."""
        assert hasattr(inverter_instance, "is_authenticated")
        assert isinstance(inverter_instance.is_authenticated, bool)

    def test_has_config_attribute(self, inverter_instance):
        """All inverters must store complete config."""
        assert hasattr(inverter_instance, "config")
        assert isinstance(inverter_instance.config, dict)

    # =========================================================================
    # Required Method Tests
    # =========================================================================

    def test_has_required_abstract_methods(self, inverter_instance):
        """All inverters must implement required abstract methods from BaseInverter."""
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

        for method_name in required_methods:
            assert hasattr(
                inverter_instance, method_name
            ), f"{self.inverter_class.__name__} missing required method: {method_name}"
            assert callable(
                getattr(inverter_instance, method_name)
            ), f"{method_name} must be callable"

    # =========================================================================
    # Capability Tests
    # =========================================================================

    def test_supports_extended_monitoring_is_boolean_attribute(self, inverter_instance):
        """supports_extended_monitoring must be a boolean attribute (not a method)."""
        assert hasattr(
            inverter_instance, "supports_extended_monitoring"
        ), "All inverters must have supports_extended_monitoring attribute"
        assert isinstance(
            inverter_instance.supports_extended_monitoring, bool
        ), "supports_extended_monitoring must be a boolean attribute, not a method"

        if self.expected_extended_monitoring is not None:
            assert (
                inverter_instance.supports_extended_monitoring
                == self.expected_extended_monitoring
            ), (
                f"Expected supports_extended_monitoring={self.expected_extended_monitoring}, "
                f"got {inverter_instance.supports_extended_monitoring}"
            )

    # =========================================================================
    # Factory Tests
    # =========================================================================

    def test_factory_creates_correct_type(self, monkeypatch):
        """Test that create_inverter returns instance of correct type."""
        self.setup_mocks(monkeypatch)
        instance = create_inverter(self.minimal_config)
        # pylint: disable=isinstance-second-argument-not-valid-type
        assert isinstance(
            instance, self.inverter_class  # pyright: ignore[reportGeneralTypeIssues]
        ), f"create_inverter should return {self.inverter_class.__name__}"

    # =========================================================================
    # Initialization Tests
    # =========================================================================

    def test_initialization_with_minimal_config(self, monkeypatch):
        """Test inverter can be initialized with minimal configuration."""
        self.setup_mocks(monkeypatch)
        # pylint: disable=not-callable
        instance = self.inverter_class(
            self.minimal_config
        )  # pyright: ignore[reportGeneralTypeIssues]

        assert instance is not None
        assert instance.address == self.minimal_config.get("address")

    def test_config_values_extracted(self, inverter_instance):
        """Test common config values are extracted during initialization."""
        # BaseInverter.__init__ extracts these common values
        assert hasattr(inverter_instance, "max_grid_charge_rate")
        assert hasattr(inverter_instance, "max_pv_charge_rate")
        assert hasattr(inverter_instance, "user")
        assert hasattr(inverter_instance, "password")

    # =========================================================================
    # API Methods Tests
    # =========================================================================

    def test_has_api_set_max_pv_charge_rate_method(self, inverter_instance):
        """All inverters should have api_set_max_pv_charge_rate method."""
        assert hasattr(inverter_instance, "api_set_max_pv_charge_rate")
        assert callable(inverter_instance.api_set_max_pv_charge_rate)
