"""Home Assistant inverter interface for EOS Connect.

This module provides the InverterHA class for controlling inverter/battery systems
via Home Assistant service calls. Any hardware that is controllable through HA entities
(e.g., Marstek, Sungrow, Goodwe via native integrations, ESPHome, or custom components)
can be used as an EOS Connect inverter.
"""

import logging
import requests

from ..base_inverter import BaseInverter  # pylint: disable=relative-beyond-top-level

logger = logging.getLogger("__main__").getChild("InverterHA")
logger.setLevel(logging.INFO)


class InverterHA(BaseInverter):
    """
    Inverter implementation that controls batteries via Home Assistant service calls.

    For each EOS state (charge_from_grid, avoid_discharge, discharge_allowed),
    the user defines a sequence of HA service calls in config.yaml.
    """

    supports_extended_monitoring_default = False

    def __init__(self, config: dict):
        # HA uses url instead of address — set address for BaseInverter
        if "address" not in config:
            config["address"] = config.get("url", "")

        # Set defaults before super().__init__ reads them
        config.setdefault("max_grid_charge_rate", 5000)
        config.setdefault("max_pv_charge_rate", 5000)

        super().__init__(config)

        self.is_authenticated = False
        # Re-declare inherited attribute so pylint tracks the setter correctly
        self.max_pv_charge_rate = self.max_pv_charge_rate
        self.url = config.get("url", "").rstrip("/")
        self.token = config.get("token", "")

        # Validate configuration
        if not self.url or not self.token:
            logger.error("[InverterHA] Missing URL or Token in configuration")

        # Load state configurations and tracking
        self.mode_sequences = {
            "force_charge": config.get("charge_from_grid", []),
            "avoid_discharge": config.get("avoid_discharge", []),
            "allow_discharge": config.get("discharge_allowed", []),
        }
        self.current_mode = None

        logger.info("[InverterHA] Initialized with URL: %s", self.url)

    def _call_service(self, service_call_config: dict, variables: dict = None) -> bool:
        """
        Executes a single service call to Home Assistant.

        Args:
            service_call_config: Configuration of the service call
                                 (service, entity_id, data/data_template).
            variables: Variables to replace in data_template (e.g. {{ power }}).

        Returns:
            True if the service call succeeded, False otherwise.
        """
        domain_service = service_call_config.get("service")
        if not domain_service or "." not in domain_service:
            logger.error("[InverterHA] Invalid service format: %s", domain_service)
            return False

        domain, service = domain_service.split(".", 1)
        endpoint = f"{self.url}/api/services/{domain}/{service}"

        # Prepare payload
        payload = {}
        if "entity_id" in service_call_config:
            payload["entity_id"] = service_call_config["entity_id"]

        # Handle data/data_template
        data_config = service_call_config.get(
            "data_template", service_call_config.get("data", {})
        )

        # Process templates if variables provided
        final_data = {}
        if variables:
            for key, value in data_config.items():
                if isinstance(value, str) and "{{" in value and "}}" in value:
                    if "{{ power }}" in value and "power" in variables:
                        if value.strip() == "{{ power }}":
                            final_data[key] = variables["power"]
                        else:
                            final_data[key] = value.replace(
                                "{{ power }}", str(variables["power"])
                            )
                    else:
                        final_data[key] = value
                else:
                    final_data[key] = value
        else:
            final_data = data_config

        payload.update(final_data)

        try:
            logger.debug(
                "[InverterHA] Calling service %s with payload %s",
                domain_service,
                payload,
            )
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            response = requests.post(
                endpoint, headers=headers, json=payload, timeout=10
            )
            response.raise_for_status()
            logger.debug("[InverterHA] Service call successful")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(
                "[InverterHA] Failed to call service %s: %s", domain_service, e
            )
            return False

    def _execute_sequence(self, sequence_config, variables=None) -> bool:
        """Executes a list of service calls.

        Returns:
            True if all steps succeeded, False otherwise.
        """
        if not sequence_config:
            logger.warning("[InverterHA] No configuration found for requested mode")
            return False

        success = True
        for step in sequence_config:
            if not self._call_service(step, variables):
                success = False
        return success

    # --- BaseInverter required implementations ---

    def initialize(self):
        """Initialize the inverter connection. HA uses stateless REST, no persistent connect."""
        self.is_authenticated = True

    def connect_inverter(self) -> bool:
        """Connect to the inverter. Stateless HTTP, always succeeds."""
        return True

    def disconnect_inverter(self) -> bool:
        """Disconnect from the inverter. Stateless HTTP, always succeeds."""
        return True

    def set_battery_mode(self, mode: str) -> bool:
        """Dispatch battery mode changes to the appropriate sequence.

        Args:
            mode: One of 'force_charge', 'avoid_discharge', 'allow_discharge'.

        Returns:
            True if the mode was set successfully, False otherwise.
        """
        if mode == "force_charge":
            return self.set_mode_force_charge()
        if mode == "avoid_discharge":
            return self.set_mode_avoid_discharge()
        if mode == "allow_discharge":
            return self.set_mode_allow_discharge()
        logger.error("[InverterHA] Unknown battery mode: %s", mode)
        return False

    def set_mode_force_charge(self, charge_power_w=None) -> bool:
        """Sets the inverter to charge from grid.

        Args:
            charge_power_w: Charge power in Watts. If None, uses max_grid_charge_rate.

        Returns:
            True if the mode was set successfully, False otherwise.
        """
        if charge_power_w is None:
            charge_power_w = self.max_grid_charge_rate

        charge_power_w = min(max(0, int(charge_power_w)), self.max_grid_charge_rate)

        logger.info(
            "[InverterHA] Setting mode: Force Charge (Power: %s W)", charge_power_w
        )
        result = self._execute_sequence(
            self.mode_sequences["force_charge"], variables={"power": charge_power_w}
        )
        self.current_mode = "force_charge"
        return result

    def set_mode_avoid_discharge(self) -> bool:
        """Sets the inverter to avoid discharge (passive/hold/charge-only).

        Returns:
            True if the mode was set successfully, False otherwise.
        """
        logger.info("[InverterHA] Setting mode: Avoid Discharge")
        result = self._execute_sequence(self.mode_sequences["avoid_discharge"])
        self.current_mode = "avoid_discharge"
        return result

    def set_mode_allow_discharge(self) -> bool:
        """Sets the inverter to allow discharge (normal operation).

        Returns:
            True if the mode was set successfully, False otherwise.
        """
        logger.info("[InverterHA] Setting mode: Allow Discharge")
        result = self._execute_sequence(self.mode_sequences["allow_discharge"])
        self.current_mode = "allow_discharge"
        return result

    def set_allow_grid_charging(self, value: bool):
        """Enable or disable grid charging.

        Args:
            value: If True, execute the charge sequence.
        """
        if value:
            self._execute_sequence(self.mode_sequences["force_charge"])

    def get_battery_info(self) -> dict:
        """Return battery info. HA does not provide direct battery data."""
        return {}

    def fetch_inverter_data(self) -> dict:
        """Return inverter data. HA does not provide direct inverter data."""
        return {}
