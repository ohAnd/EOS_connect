"""
- BatteryInterface: A class to interact with SOC data sources, retrieve battery SOC, and calculate
  dynamic maximum charge power based on SOC.
- threading: For managing background update services.
- time: For managing sleep intervals in the update loop.
sensor identifier, access token (if required), and maximum fixed charge power. Use the
`battery_request_current_soc` method to fetch the current SOC value or `get_max_charge_power_dyn`
to calculate the dynamic maximum charge power.
    access_token=None,
    max_charge_power_w=3000
max_charge_power = battery_interface.get_max_charge_power_dyn()
print(f"Max Charge Power: {max_charge_power}W")
This module provides the `BatteryInterface` class, which serves as an interface for fetching
the State of Charge (SOC) data of a battery from various sources such as OpenHAB and Home Assistant.
The `BatteryInterface` class allows users to configure the source of SOC data and retrieve the
current SOC value through its methods. It supports fetching SOC data from OpenHAB using its REST API
and from Home Assistant using its API with authentication.
Classes:
    - BatteryInterface: A class to interact with SOC data sources and retrieve battery SOC.
Dependencies:
    - logging: For logging information, warnings, and errors.
    - requests: For making HTTP requests to the SOC data sources.
Usage:
    Create an instance of the `BatteryInterface` class by providing the source, URL,
    sensor identifier, and access token (if required). Use the `battery_request_current_soc`
    method to fetch the current SOC value.
Example:
    ```python
    battery_interface = BatteryInterface(
        src="openhab",
        url="http://openhab-server",
        soc_sensor="BatterySOC",
        access_token=None
    current_soc = battery_interface.battery_request_current_soc()
    print(f"Current SOC: {current_soc}%")
    ```
"""

import logging
import threading
import time
import requests

logger = logging.getLogger("__main__")
logger.info("[BATTERY-IF] loading module ")


class BatteryInterface:
    """
    BatteryInterface is a class that provides an interface for fetching the State of Charge (SOC)
    data of a battery from different sources such as OpenHAB and Home Assistant.
    Attributes:
        src (str): The source of the SOC data. Can be "default", "openhab", or "homeassistant".
        url (str): The base URL of the SOC data source.
        soc_sensor (str): The identifier of the SOC sensor in the data source.
        access_token (str): The access token for authentication (used for Home Assistant).
    Methods:
        fetch_soc_data_from_openhab():
            Fetches the SOC data from the OpenHAB server using its REST API.
        fetch_soc_data_from_homeassistant():
            Fetches the SOC data from the Home Assistant API.
        battery_request_current_soc():
            Fetches the current SOC of the battery based on the configured source.
    """

    def __init__(self, config, on_bat_max_changed=None):
        self.src = config.get("source", "default")
        self.url = config.get("url", "")
        self.soc_sensor = config.get("soc_sensor", "")
        self.access_token = config.get("access_token", "")
        self.max_charge_power_fix = config.get("max_charge_power_w", 1000)
        self.battery_data = config
        self.max_charge_power_dyn = 0
        self.last_max_charge_power_dyn = 0
        self.current_soc = 0
        self.current_usable_capacity = 0
        self.on_bat_max_changed = on_bat_max_changed
        self.min_soc_set = config.get("min_soc_percentage", 0)
        self.max_soc_set = config.get("max_soc_percentage", 100)
        self.price_euro_per_wh = float(config.get("price_euro_per_wh_accu", 0.0))
        self.price_source = config.get("price_euro_per_wh_source", "config")
        self.price_sensor = config.get("price_euro_per_wh_sensor", "")

        self.soc_fail_count = 0

        self.update_interval = 30
        self._update_thread = None
        self._stop_event = threading.Event()
        self.start_update_service()

    def __fetch_soc_data_from_openhab(self):
        """
        Fetches the State of Charge (SOC) data for the battery from the OpenHAB server.

        This method sends a GET request to the OpenHAB REST API to retrieve the SOC value
        for the battery. If the request is successful, the SOC value is extracted, converted
        to a percentage, and returned. In case of a timeout or request failure, a default
        SOC value of 5% is returned, and an error is logged.

        Returns:
            int: The SOC value as a percentage (0-100). Defaults to 5% in case of an error.
        """
        logger.debug("[BATTERY-IF] getting SOC from openhab ...")
        openhab_url = self.url + "/rest/items/" + self.soc_sensor
        soc = 5  # Default SOC value in case of error
        try:
            response = requests.get(openhab_url, timeout=6)
            response.raise_for_status()
            data = response.json()
            raw_state = str(data["state"]).strip()
            # Take only the first part before any space (handles "90", "90 %", "0.11 %", etc.)
            cleaned_value = raw_state.split()[0]
            raw_value = float(cleaned_value)

            # Auto-detect format: if value is <= 1.0, assume it's decimal (0.0-1.0)
            # if value is > 1.0, assume it's already percentage (0-100)
            if raw_value <= 1.0:
                soc = raw_value * 100  # Convert decimal to percentage
                logger.debug(
                    "[BATTERY-IF] Detected decimal format (0.0-1.0): %s -> %s%%",
                    raw_value,
                    soc,
                )
            else:
                soc = raw_value  # Already in percentage format
                logger.debug(
                    "[BATTERY-IF] Detected percentage format (0-100): %s%%", soc
                )
            self.soc_fail_count = 0  # Reset fail count on success
            return round(soc, 1)
        except requests.exceptions.Timeout:
            return self._handle_soc_error(
                "openhab", "Request timed out", self.current_soc
            )
        except requests.exceptions.RequestException as e:
            return self._handle_soc_error("openhab", e, self.current_soc)

    def __fetch_soc_data_from_homeassistant(self):
        """
        Fetches the state of charge (SOC) data from the Home Assistant API.
        This method sends a GET request to the Home Assistant API to retrieve the SOC
        value for a specific sensor. The SOC value is expected to be in the 'state' field
        of the API response and is converted to a percentage.
        Returns:
            int: The SOC value as a percentage, rounded to the nearest integer.
                 Returns a default value of 5% in case of a timeout or request failure.
        Raises:
            requests.exceptions.Timeout: If the request to the Home Assistant API times out.
            requests.exceptions.RequestException: If there is an error during the request.
        """
        homeassistant_url = f"{self.url}/api/states/{self.soc_sensor}"
        # Headers for the API request
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        soc = 5  # Default SOC value in case of error
        try:
            response = requests.get(homeassistant_url, headers=headers, timeout=6)
            response.raise_for_status()
            entity_data = response.json()
            soc = float(entity_data["state"])
            self.soc_fail_count = 0  # Reset fail count on success
            return round(soc, 1)
        except requests.exceptions.Timeout:
            return self._handle_soc_error(
                "homeassistant", "Request timed out", self.current_soc
            )
        except requests.exceptions.RequestException as e:
            return self._handle_soc_error("homeassistant", e, self.current_soc)

    def __battery_request_current_soc(self):
        """
        Fetch the current state of charge (SOC) of the battery from OpenHAB.
        """
        # default value for start SOC = 5
        default = False
        if self.src == "default":
            self.current_soc = 5
            default = True
            logger.debug("[BATTERY-IF] source set to default with start SOC = 5%")
        elif self.src == "openhab":
            self.current_soc = self.__fetch_soc_data_from_openhab()
        elif self.src == "homeassistant":
            self.current_soc = self.__fetch_soc_data_from_homeassistant()
        else:
            self.current_soc = 5
            default = True
            logger.error(
                "[BATTERY-IF] source currently not supported. Using default start SOC = 5%."
            )
        if default is False:
            logger.debug(
                "[BATTERY-IF] successfully fetched SOC = %s %%", self.current_soc
            )
        return self.current_soc

    def __fetch_price_data_from_openhab(self):
        """
        Fetch the current battery energy price from an OpenHAB item.

        Returns:
            float: Battery energy cost in €/Wh provided by the configured item.
        """
        if not self.price_sensor:
            raise ValueError("price_euro_per_wh_sensor must be configured for OpenHAB.")

        logger.debug("[BATTERY-IF] getting price from openhab ...")
        openhab_url = self.url + "/rest/items/" + self.price_sensor
        try:
            response = requests.get(openhab_url, timeout=6)
            response.raise_for_status()
            data = response.json()
            raw_state = str(data["state"]).strip()
            # Take only the first part before any space (handles "0.0001", "0.0001 €/Wh", etc.)
            cleaned_value = raw_state.split()[0]
            price = float(cleaned_value)
            logger.debug("[BATTERY-IF] Fetched price from OpenHAB: %s €/Wh", price)
            return price
        except requests.exceptions.Timeout:
            raise requests.exceptions.Timeout(
                "Request timed out while fetching price from OpenHAB"
            )
        except requests.exceptions.RequestException as e:
            raise requests.exceptions.RequestException(
                f"Error fetching price from OpenHAB: {e}"
            )

    def __fetch_price_data_from_homeassistant(self):
        """
        Fetch the current battery energy price from a Home Assistant sensor.

        Returns:
            float: Battery energy cost in €/Wh provided by the configured sensor.
        """
        if not self.price_sensor:
            raise ValueError(
                "price_euro_per_wh_sensor must be configured for Home Assistant."
            )

        logger.debug("[BATTERY-IF] getting price from homeassistant ...")
        homeassistant_url = f"{self.url}/api/states/{self.price_sensor}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        response = requests.get(homeassistant_url, headers=headers, timeout=6)
        response.raise_for_status()
        entity_data = response.json()
        price = float(entity_data["state"])
        logger.debug("[BATTERY-IF] Fetched price from Home Assistant: %s €/Wh", price)
        return price

    def __update_price_euro_per_wh(self):
        """
        Update the battery price from the configured source if needed.
        """
        if self.price_source == "config":
            return self.price_euro_per_wh

        source_name = self.price_source.upper()
        try:
            if self.price_source == "homeassistant":
                latest_price = self.__fetch_price_data_from_homeassistant()
            elif self.price_source == "openhab":
                latest_price = self.__fetch_price_data_from_openhab()
            else:
                logger.warning(
                    "[BATTERY-IF] Unknown price source '%s'. Keeping last value %s.",
                    self.price_source,
                    self.price_euro_per_wh,
                )
                return self.price_euro_per_wh
        except requests.exceptions.Timeout:
            logger.warning(
                "[BATTERY-IF] %s - Request timed out while fetching "
                + "price_euro_per_wh_accu. Keeping last value %s.",
                source_name,
                self.price_euro_per_wh,
            )
            return self.price_euro_per_wh
        except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
            logger.warning(
                "[BATTERY-IF] %s - Error fetching price sensor data: %s. "
                + "Keeping last value %s.",
                source_name,
                exc,
                self.price_euro_per_wh,
            )
            return self.price_euro_per_wh

        self.price_euro_per_wh = latest_price
        logger.debug(
            "[BATTERY-IF] Updated price_euro_per_wh_accu from %s sensor %s: %s",
            self.price_source,
            self.price_sensor,
            self.price_euro_per_wh,
        )
        return self.price_euro_per_wh

    def _handle_soc_error(self, source, error, last_soc):
        self.soc_fail_count += 1
        if self.soc_fail_count < 5:
            logger.warning(
                "[BATTERY-IF] %s - Error fetching battery SOC: %s. Failure count: %d/5."
                + " Using last known SOC = %s%%.",
                source.upper(),
                error,
                self.soc_fail_count,
                last_soc,
            )
            return last_soc
        else:
            logger.error(
                "[BATTERY-IF] %s - 5 consecutive SOC fetch failures. Using fallback SOC = 5%%.",
                source.upper(),
            )
            self.soc_fail_count = 0  # Reset after fallback
            return 5

    def get_current_soc(self):
        """
        Returns the current state of charge (SOC) of the battery.
        """
        return self.current_soc

    def get_max_charge_power(self):
        """
        Returns the maximum charge power of the battery.
        """
        return round(self.max_charge_power_dyn, 0)

    def get_current_usable_capacity(self):
        """
        Returns the current usable capacity of the battery.
        """
        return round(self.current_usable_capacity, 2)

    def get_min_soc(self):
        """
        Returns the minimum state of charge (SOC) percentage of the battery.
        """
        return self.min_soc_set

    def get_price_euro_per_wh(self):
        """
        Returns the current battery price in €/Wh.
        """
        return self.price_euro_per_wh

    def set_min_soc(self, min_soc):
        """
        Sets the minimum state of charge (SOC) percentage of the battery.
        """
        # check that min_soc is not greater than max_soc and not less than configured min_soc
        if min_soc > self.max_soc_set:
            logger.warning(
                "[BATTERY-IF] Attempted to set min SOC (%s) higher than max SOC (%s)."
                + " Adjusting min SOC to max SOC.",
                min_soc,
                self.max_soc_set,
            )
            min_soc = self.max_soc_set - 1
        if min_soc < self.battery_data.get("min_soc_percentage", 0):
            logger.warning(
                "[BATTERY-IF] Attempted to set min SOC (%s) lower than configured min SOC (%s)."
                + " setting to configured min SOC.",
                min_soc,
                self.battery_data.get("min_soc_percentage", 0),
            )
            min_soc = self.battery_data.get("min_soc_percentage", 0)
        self.min_soc_set = min_soc

    def get_max_soc(self):
        """
        Returns the maximum state of charge (SOC) percentage of the battery.
        """
        return self.max_soc_set

    def set_max_soc(self, max_soc):
        """
        Sets the maximum state of charge (SOC) percentage of the battery.
        """
        # check that max_soc is not less than min_soc and not greater than configured max_soc
        if max_soc < self.min_soc_set:
            logger.warning(
                "[BATTERY-IF] Attempted to set max SOC (%s) lower than min SOC (%s)."
                + " Adjusting max SOC to min SOC.",
                max_soc,
                self.min_soc_set,
            )
            max_soc = self.min_soc_set + 1
        if max_soc > self.battery_data.get("max_soc_percentage", 100):
            logger.warning(
                "[BATTERY-IF] Attempted to set max SOC (%s) higher than configured max SOC (%s)."
                + " setting to configured max SOC.",
                max_soc,
                self.battery_data.get("max_soc_percentage", 100),
            )
            max_soc = self.battery_data.get("max_soc_percentage", 100)
        self.max_soc_set = max_soc

    def __get_max_charge_power_dyn(self, soc=None, min_charge_power=500):
        """
        Calculates the maximum charge power of the battery dynamically based on SOC
        using a decay function that incorporates the C-rate.

        The formula reduces the charge power as SOC increases:
        - At low SOC, the charge power is close to the maximum C-rate (e.g., 1C).
        - As SOC approaches 100%, the charge power decreases exponentially.
        - The charge power is never less than the specified minimum value.

        Args:
            soc (float, optional): The state of charge to use for calculation.
                                If None, the current SOC is used.
            min_charge_power (float): The minimum charge power in watts.

        Returns:
            float: The dynamically calculated maximum charge power in watts.
        """
        if not self.battery_data.get("charging_curve_enabled", True):
            self.max_charge_power_dyn = self.max_charge_power_fix
            logger.debug(
                "[BATTERY-IF] Charging curve is disabled, using fixed max charge power."
            )
            return

        if soc is None:
            soc = self.current_soc

        # Get the battery capacity in watt-hours
        battery_capacity_wh = self.battery_data.get("capacity_wh", 0)

        if battery_capacity_wh <= 0:
            logger.warning("[BATTERY-IF] Battery capacity is not set or invalid.")
            return min_charge_power

        # Ensure SOC is within valid bounds
        if soc < 0 or soc > 100:
            logger.warning(
                "[BATTERY-IF] Invalid SOC value: %s. Returning minimum charge power.",
                soc,
            )
            return min_charge_power

        # Define the maximum C-rate (e.g., 1C at low SOC)
        max_c_rate = 1.0  # 1C means charging at full capacity per hour
        min_c_rate = 0.05  # Minimum C-rate at high SOC (e.g., 5% of capacity)

        if soc <= 50:
            # Linear decrease of C-rate up to 50% SOC
            c_rate = max_c_rate
        else:
            # Logarithmic decrease of C-rate after 50% SOC
            c_rate = max(min_c_rate, max_c_rate * (1 - (soc - 50) / 60) ** 2)

        # Calculate the maximum charge power in watts
        max_charge_power = c_rate * battery_capacity_wh

        # Ensure the charge power does not exceed the fixed maximum charge power
        max_charge_power = min(max_charge_power, self.max_charge_power_fix)

        # Round the charge power to the nearest 50 watts
        max_charge_power = round(max_charge_power / 50) * 50

        self.max_charge_power_dyn = max(max_charge_power, min_charge_power)
        if self.max_charge_power_dyn != self.last_max_charge_power_dyn:
            self.last_max_charge_power_dyn = self.max_charge_power_dyn
            logger.info(
                "[BATTERY-IF] Max dynamic charge power changed to %s W",
                self.max_charge_power_dyn,
            )
            if self.on_bat_max_changed:
                self.on_bat_max_changed()

    def start_update_service(self):
        """
        Starts the background thread to periodically update the state.
        """
        if self._update_thread is None or not self._update_thread.is_alive():
            self._stop_event.clear()
            self._update_thread = threading.Thread(
                target=self._update_state_loop, daemon=True
            )
            self._update_thread.start()
            logger.info("[BATTERY-IF] Update service started.")

    def shutdown(self):
        """
        Stops the background thread and shuts down the update service.
        """
        if self._update_thread and self._update_thread.is_alive():
            self._stop_event.set()
            self._update_thread.join()
            logger.info("[BATTERY-IF] Update service stopped.")

    def _update_state_loop(self):
        """
        The loop that runs in the background thread to update the state.
        """
        while not self._stop_event.is_set():
            try:
                self.__battery_request_current_soc()
                self.current_usable_capacity = max(
                    0,
                    (
                        self.battery_data.get("capacity_wh", 0)
                        * self.battery_data.get("discharge_efficiency", 1.0)
                        * (
                            self.current_soc
                            - self.battery_data.get("min_soc_percentage", 0)
                        )
                        / 100
                    ),
                )
                self.__get_max_charge_power_dyn()
                self.__update_price_euro_per_wh()

            except (requests.exceptions.RequestException, ValueError, KeyError) as e:
                logger.error("[BATTERY-IF] Error while updating state: %s", e)
                # Break the sleep interval into smaller chunks to allow immediate shutdown
            sleep_interval = self.update_interval
            while sleep_interval > 0:
                if self._stop_event.is_set():
                    return  # Exit immediately if stop event is set
                time.sleep(min(1, sleep_interval))  # Sleep in 1-second chunks
                sleep_interval -= 1

        self.start_update_service()
