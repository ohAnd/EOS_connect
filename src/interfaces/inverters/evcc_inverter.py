"""EVCC external inverter control implementation."""

import logging
from ..base_inverter import BaseInverter  # pylint: disable=relative-beyond-top-level

logger = logging.getLogger("__main__").getChild("EvccInverter")


class EvccInverter(BaseInverter):
    """
    EVCC external control inverter implementation.

    Used when:
    - type: "evcc" - EVCC handles control externally via EvccInterface

    All control methods are no-ops, returning success without actual hardware interaction.
    Control is delegated to EvccInterface in the main application.
    """

    supports_extended_monitoring_default = False

    def __init__(self, config):
        """Initialize with minimal config."""
        super().__init__(config)
        self.is_authenticated: bool = False
        logger.info(
            "[EvccInverter] Initialized in EVCC external control mode (delegating to EvccInterface)"
        )

    def initialize(self):
        """No initialization needed - EVCC handles this externally."""
        logger.debug("[EvccInverter] No initialization required")
        self.is_authenticated = True

    def connect_inverter(self) -> bool:
        """No connection needed - EVCC handles connection."""
        logger.debug(
            "[EvccInverter] connect_inverter() called (no-op, delegated to EVCC)"
        )
        return True

    def disconnect_inverter(self) -> bool:
        """No disconnection needed - EVCC handles disconnection."""
        logger.debug(
            "[EvccInverter] disconnect_inverter() called (no-op, delegated to EVCC)"
        )
        return True

    def set_battery_mode(self, mode: str) -> bool:
        """Battery mode control delegated to EvccInterface."""
        logger.debug(
            "[EvccInverter] set_battery_mode(%s) called (delegated to EVCC)", mode
        )
        return True

    def set_mode_avoid_discharge(self) -> bool:
        """Discharge control delegated to EvccInterface."""
        logger.debug(
            "[EvccInverter] set_mode_avoid_discharge() called (delegated to EVCC)"
        )
        return True

    def set_mode_allow_discharge(self) -> bool:
        """Discharge control delegated to EvccInterface."""
        logger.debug(
            "[EvccInverter] set_mode_allow_discharge() called (delegated to EVCC)"
        )
        return True

    def set_mode_force_charge(self, charge_power_w: int) -> bool:
        """Charge control delegated to EvccInterface."""
        logger.debug(
            "[EvccInverter] set_mode_force_charge(%d W) called (delegated to EVCC)",
            charge_power_w,
        )
        return True

    def set_allow_grid_charging(self, value: bool):
        """Grid charging control delegated to EvccInterface."""
        logger.debug(
            "[EvccInverter] set_allow_grid_charging(%s) called (delegated to EVCC)",
            value,
        )

    def get_battery_info(self) -> dict:
        """Return empty battery info - EVCC provides this."""
        logger.debug("[EvccInverter] get_battery_info() called (delegated to EVCC)")
        return {}

    def fetch_inverter_data(self) -> dict:
        """Return empty inverter data - EVCC provides this."""
        logger.debug("[EvccInverter] fetch_inverter_data() called (delegated to EVCC)")
        return {}

    # Capability flag is set via supports_extended_monitoring_default class attribute
    # and handled by BaseInverter.__init__
