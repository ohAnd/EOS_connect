
"""Base inverter interface definitions.

This module provides the Abstract Base Class for inverter implementations
used by EOS_connect, including common helpers and utility methods.
"""

from abc import ABC, abstractmethod
import logging

logger = logging.getLogger("__main__").getChild("BaseInverter")
logger.setLevel(logging.INFO)


class BaseInverter(ABC):
    """Abstract base class for different inverter types."""

    def __init__(self, config: dict):
        # Store complete config (for tests & future extensions)
        self.config = config

        # Extract common values as well
        self.address = config.get("address")
        self.user = config.get("user", "customer").lower()
        self.password = config.get("password", "")
        self.max_grid_charge_rate = config.get("max_grid_charge_rate")
        self.max_pv_charge_rate = config.get("max_pv_charge_rate")

        self.is_authenticated = False
        self.inverter_type = self.__class__.__name__

        logger.info(f"[{self.inverter_type}] Initialized for {self.address}")

    # --- Optionale Authentifizierung ---

    @abstractmethod
    def initialize(self):
        """Heavy initialization (API calls)."""
        raise NotImplementedError()

    def authenticate(self) -> bool:
        """
        Optional authentication.
        By default, this method does nothing and returns True.
        Subclasses can override it if authentication is needed.
        """
        logger.debug(f"[{self.inverter_type}] No authentication required")
        self.is_authenticated = True
        return True

    # --- Mandatory Methods for All Inverters ---

    @abstractmethod
    def set_battery_mode(self, mode: str) -> bool:
        """Sets the battery mode (e.g., normal, hold, charge)."""
        raise NotImplementedError()

    # --- EOS Connect Helpers ---

    @abstractmethod
    def set_mode_avoid_discharge(self) -> bool:
        """Prevents battery discharge (Hold Mode)."""
        return self.set_battery_mode("hold")

    @abstractmethod
    def set_mode_allow_discharge(self) -> bool:
        """Allows battery discharge (Normal Mode)."""
        return self.set_battery_mode("normal")

    @abstractmethod
    def set_allow_grid_charging(self, value: bool):
        """Enable or disable charging from the grid."""
        raise NotImplementedError()

    @abstractmethod
    def get_battery_info(self) -> dict:
        """Reads current battery information."""
        raise NotImplementedError()

    @abstractmethod
    def fetch_inverter_data(self) -> dict:
        """Reads current inverter data."""
        raise NotImplementedError()

    @abstractmethod
    def set_mode_force_charge(self, charge_power_w: int) -> bool:
        """
        Force charge mode with specific power.
        Each subclass must implement this method.
        """
        raise NotImplementedError()

    @abstractmethod
    def connect_inverter(self) -> bool:
        """
        Establishes a connection to the inverter.

        This method is required to be implemented by all subclasses.
        It should return True if the connection was successful, False otherwise.
        """
        raise NotImplementedError()

    @abstractmethod
    def disconnect_inverter(self) -> bool:
        """
        Disconnect from the inverter.

        This method is required to be implemented by all subclasses.
        It should return True if the disconnection was successful, False otherwise.
        """
        raise NotImplementedError()

    # --- Common Utility Methods ---

    def api_set_max_pv_charge_rate(self, max_pv_charge_rate: int):
        """
        Set the maximum PV charge rate (optional, inverter-specific).

        This method is specific to certain inverters (e.g., Fronius) that support
        dynamic PV charge rate limiting. Default implementation is a no-op.
        Override in subclasses that support this feature.

        Args:
            max_pv_charge_rate: Maximum charge rate from PV in watts
        """
        logger.debug(
            "[%s] api_set_max_pv_charge_rate(%d W) not implemented (no-op)",
            self.inverter_type,
            max_pv_charge_rate,
        )

    def supports_extended_monitoring(self) -> bool:
        """
        Indicates whether this inverter supports extended monitoring data
        (temperature sensors, fan control, etc.).

        Returns:
            False by default. Subclasses providing extended monitoring
            should override this method to return True.
        """
        return False

    def disconnect(self):
        """Session closes itself."""
        logger.info(f"[{self.inverter_type}] Session closed")

    def shutdown(self):
        """Standard shutdown (can be overridden)."""
        self.disconnect()
