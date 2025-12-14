"""Null Object Pattern inverter for testing and display-only mode."""

import logging
from ..inverter_base import BaseInverter  # pylint: disable=relative-beyond-top-level

logger = logging.getLogger("__main__").getChild("NullInverter")


class NullInverter(BaseInverter):
    """
    No-operation inverter implementation.

    Used when:
    - type: "default" - Display-only mode, no inverter control
    - type: "evcc" - EVCC handles control externally

    All control methods are no-ops, returning success without actual hardware interaction.
    """

    def __init__(self, config):
        """Initialize with minimal config."""
        super().__init__(config)
        logger.info(
            "[NullInverter] Initialized in display-only mode (type: %s)",
            config.get("type", "unknown"),
        )

    def initialize(self):
        """No initialization needed for null inverter."""
        logger.debug("[NullInverter] No initialization required")
        self.is_authenticated = True

    def connect_inverter(self) -> bool:
        """No connection needed."""
        logger.debug("[NullInverter] connect_inverter() called (no-op)")
        return True

    def disconnect_inverter(self) -> bool:
        """No disconnection needed."""
        logger.debug("[NullInverter] disconnect_inverter() called (no-op)")
        return True

    def set_battery_mode(self, mode: str) -> bool:
        """No battery mode control."""
        logger.debug("[NullInverter] set_battery_mode(%s) called (no-op)", mode)
        return True

    def set_mode_avoid_discharge(self) -> bool:
        """No discharge control."""
        logger.debug("[NullInverter] set_mode_avoid_discharge() called (no-op)")
        return True

    def set_mode_allow_discharge(self) -> bool:
        """No discharge control."""
        logger.debug("[NullInverter] set_mode_allow_discharge() called (no-op)")
        return True

    def set_mode_force_charge(self, charge_power_w: int) -> bool:
        """No charge control."""
        logger.debug(
            "[NullInverter] set_mode_force_charge(%d W) called (no-op)", charge_power_w
        )
        return True

    def set_allow_grid_charging(self, value: bool):
        """No grid charging control."""
        logger.debug("[NullInverter] set_allow_grid_charging(%s) called (no-op)", value)

    def get_battery_info(self) -> dict:
        """Return empty battery info."""
        logger.debug("[NullInverter] get_battery_info() called (no-op)")
        return {}

    def fetch_inverter_data(self) -> dict:
        """Return empty inverter data."""
        logger.debug("[NullInverter] fetch_inverter_data() called (no-op)")
        return {}

    def supports_extended_monitoring(self) -> bool:
        """Null inverter has no monitoring capabilities."""
        return False
