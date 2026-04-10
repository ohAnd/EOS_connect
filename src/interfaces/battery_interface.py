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
from datetime import datetime
import requests
from .battery_price_handler import BatteryPriceHandler

logger = logging.getLogger("__main__")
logger.info("[BATTERY-IF] loading module ")

# Temperature Compensation
# ========================
# Battery temperature derating uses a generic curve derived from official BYD HVM
# datasheet specifications. This approach is equipment-agnostic and works with any
# battery capacity or inverter combination while maintaining manufacturer-validated
# safety principles for LiFePO4 batteries.
#
# The __calculate_temp_multiplier method implements thermal protection with these zones:
# - Cold protection (-10°C): Prevents lithium-plating damage
# - Optimal range (12-40°C): 100% charging capacity
# - Thermal protection (40-60°C): Reduces power to protect battery
#
# Generic implementation - suitable for any LiFePO4 battery system.


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

    def __init__(
        self,
        config,
        on_bat_max_changed=None,
        load_interface=None,
        timezone=None,
        base_control=None,
        request_timeout=10,  # Default timeout for API requests
    ):
        self.src = config.get("source", "default")
        self.url = config.get("url", "")
        self.soc_sensor = config.get("soc_sensor", "")
        self.temp_sensor = config.get("sensor_battery_temperature", "")
        raw_token = config.get("access_token", "")
        # Strip leading/trailing whitespace that can be introduced by YAML >- block
        # scalar style when long tokens wrap across multiple lines
        self.access_token = str(raw_token).strip()
        if self.access_token != raw_token:
            logger.warning(
                "[BATTERY-IF] access_token had leading/trailing whitespace stripped. "
                "Check your access token setting for unintended spaces."
            )
        elif " " in self.access_token or "\n" in self.access_token:
            logger.warning(
                "[BATTERY-IF] access_token contains internal whitespace. This will cause "
                "HTTP 403 errors. Re-enter the token in Settings → Data Source "
                "without extra spaces or line breaks."
            )
        self.max_charge_power_fix = config.get("max_charge_power_w", 1000)
        self.battery_data = config
        self.max_charge_power_dyn = 0
        self.last_max_charge_power_dyn = 0
        self.current_soc = 0
        self.current_temp = None  # None = sensor not configured or failed
        self.temp_fail_count = 0
        self.last_logged_temp_multiplier = None  # To reduce log spam
        self.current_usable_capacity = 0
        self.on_bat_max_changed = on_bat_max_changed
        self.base_control = base_control  # Store reference to base_control
        self.min_soc_set = config.get("min_soc_percentage", 0)
        self.max_soc_set = config.get("max_soc_percentage", 100)
        self.price_euro_per_wh = float(config.get("price_euro_per_wh_accu", 0.0))
        self.price_sensor = config.get("price_euro_per_wh_sensor", "")
        self.request_timeout = request_timeout  # Store configurable timeout

        self.soc_fail_count = 0

        # Initialize dynamic price handler
        self.price_handler = BatteryPriceHandler(
            config, load_interface=load_interface, timezone=timezone
        )

        self.update_interval = 30
        self._update_thread = None
        self._stop_event = threading.Event()
        self.start_update_service()

    # source-specific SOC fetchers removed — use __fetch_soc_data_unified

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
        else:
            try:
                self.current_soc = self.__fetch_soc_data_unified()
            except ValueError:
                # Unknown/invalid source -> fallback to default behavior
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

    def __battery_request_current_temp(self):
        """
        Fetch the current battery temperature from the configured source.
        Returns None if sensor not configured or fetch fails.
        """
        if not self.temp_sensor:
            return None  # Sensor not configured - temperature compensation disabled

        if self.src == "default":
            return None  # Default mode doesn't support temperature

        try:
            raw_state = self.__fetch_remote_state(self.src, self.temp_sensor)
            cleaned_value = raw_state.split()[0]
            temp = float(cleaned_value)

            # Sanity check: battery temperature should be reasonable
            if temp < -30 or temp > 70:
                logger.warning(
                    "[BATTERY-IF] Unrealistic battery temperature: %s°C. Ignoring.",
                    temp,
                )
                return self.current_temp  # Keep last valid value

            self.temp_fail_count = 0
            self.current_temp = temp
            logger.debug("[BATTERY-IF] Successfully fetched Temp = %s°C", temp)
            return temp

        except (requests.exceptions.RequestException, ValueError, KeyError) as e:
            self.temp_fail_count += 1
            if self.temp_fail_count >= 5:
                logger.warning(
                    "[BATTERY-IF] Temperature sensor unavailable (%d failures). "
                    "Temperature compensation disabled.",
                    self.temp_fail_count,
                )
                return None
            logger.debug(
                "[BATTERY-IF] Error fetching temperature (attempt %d/5): %s",
                self.temp_fail_count,
                e,
            )
            return self.current_temp  # Use last known value

    # source-specific price fetchers removed — use __fetch_price_data_unified

    def __fetch_remote_state(self, source, sensor):
        """Fetch the raw state string from OpenHAB or Home Assistant.

        Returns the trimmed state string. Raises the original requests
        exceptions for callers to handle.
        """
        if not sensor:
            raise ValueError("Sensor/item identifier must be provided")

        if source == "openhab":
            url = self.url + "/rest/items/" + sensor
            response = requests.get(url, timeout=self.request_timeout)
            response.raise_for_status()
            data = response.json()
            return str(data.get("state", "")).strip()
        elif source == "homeassistant":
            url = f"{self.url}/api/states/{sensor}"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            response = requests.get(url, headers=headers, timeout=self.request_timeout)
            response.raise_for_status()
            data = response.json()
            return str(data.get("state", "")).strip()
        else:
            raise ValueError(f"Unknown source: {source}")

    def __fetch_soc_data_unified(self):
        """Unified SOC fetch using the configured `self.src` source."""
        try:
            raw_state = self.__fetch_remote_state(self.src, self.soc_sensor)
            cleaned_value = raw_state.split()[0]
            raw_value = float(cleaned_value)
            if raw_value <= 1.0:
                # Use history to decide which format is closer to the last known value.
                # If it's the first run (current_soc == 0), we default to decimal (x100)
                # UNLESS the value is exactly 1.0, which we treat as 1% to avoid
                # jumping to 100% incorrectly.
                if self.current_soc > 0:
                    diff_as_decimal = abs((raw_value * 100) - self.current_soc)
                    diff_as_percent = abs(raw_value - self.current_soc)

                    if diff_as_percent < diff_as_decimal:
                        soc = raw_value
                        # logger.debug(
                        #     "[BATTERY-IF] Auto-detected percentage format "
                        #     "(0-1) based on history: %s%%",
                        #     soc,
                        # )
                    else:
                        soc = raw_value * 100
                        logger.debug(
                            "[BATTERY-IF] Auto-detected decimal format "
                            "(0.0-1.0) based on history: %s -> %s%%",
                            raw_value,
                            soc,
                        )
                elif raw_value == 1.0:
                    # Special case for first run: 1.0 is more likely 1% than 100%
                    # if we want to avoid accidental full-battery assumptions.
                    soc = 1.0
                    logger.debug(
                        "[BATTERY-IF] First run: assuming 1.0 is 1%% (percentage format)"
                    )
                else:
                    # Default for other values <= 1.0 on first run: assume decimal
                    soc = raw_value * 100
                    logger.debug(
                        "[BATTERY-IF] First run: assuming decimal format "
                        "for value %s -> %s%%",
                        raw_value,
                        soc,
                    )
            else:
                # Values > 1.0 are clearly percentage format (0-100).
                soc = raw_value
                # logger.debug(
                #     "[BATTERY-IF] Detected percentage format (0-100): %s%%", soc
                # )

            self.soc_fail_count = 0
            return round(soc, 1)
        except requests.exceptions.Timeout:
            return self._handle_soc_error(
                self.src, "Request timed out", self.current_soc
            )
        except requests.exceptions.RequestException as e:
            return self._handle_soc_error(self.src, e, self.current_soc)
        except (ValueError, KeyError) as e:
            return self._handle_soc_error(self.src, e, self.current_soc)

    def __fetch_price_data_unified(self):
        """Unified price fetch using configured `self.src` (top-level source)."""
        # If no sensor is configured, fall back to the static configured price
        if not self.price_sensor:
            return self.price_euro_per_wh

        # Use top-level `source` for all remote fetches (SOC and price)
        raw_state = self.__fetch_remote_state(self.src, self.price_sensor)
        cleaned_value = raw_state.split()[0]
        return float(cleaned_value)

    def __update_price_euro_per_wh(self):
        """
        Update the battery price from the configured source if needed.
        """
        # If dynamic price calculation is enabled, use the handler
        if self.price_handler and self.price_handler.price_calculation_enabled:
            if self.price_handler.update_price_if_needed(
                inventory_wh=self.current_usable_capacity
            ):
                self.price_euro_per_wh = self.price_handler.get_current_price()
                logger.info(
                    "[BATTERY-IF] Dynamic battery price updated: %.4f €/kWh",
                    self.price_euro_per_wh * 1000,
                )
            return self.price_euro_per_wh

        # If top-level source is default, keep configured static price
        if self.src == "default":
            return self.price_euro_per_wh

        # If no sensor configured, use static configured price
        if not self.price_sensor:
            return self.price_euro_per_wh

        source_name = self.src.upper()
        if self.src not in ("homeassistant", "openhab"):
            logger.warning(
                "[BATTERY-IF] Unknown price source '%s'. Keeping last value %s.",
                self.src,
                self.price_euro_per_wh,
            )
            return self.price_euro_per_wh

        try:
            latest_price = self.__fetch_price_data_unified()
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
            self.src,
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

    def get_stored_energy_info(self):
        """
        Returns detailed information about the stored energy cost analysis.
        """
        results = self.price_handler.get_analysis_results().copy()
        results["enabled"] = self.price_handler.price_calculation_enabled
        results["price_source"] = "sensor" if self.price_sensor else "fixed"
        return results

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

    def __calculate_temp_multiplier(self, temp):
        """
        Calculate temperature compensation multiplier using a generic thermal derating curve
        derived from BYD HVM battery specifications.

        This C-rate multiplier is equipment-agnostic and works with any battery capacity
        or inverter combination. While derived from BYD HVM specifications, it's implemented
        as a generic solution suitable for LiFePO4 batteries from any manufacturer.

        Derating zones (derived from BYD HVM specifications):
        - Below -10°C: 0% (Charging locked - cell damage protection)
        - -10 to 0°C: Linear ramp 7.5% → 50% (Lithium-plating danger)
        - 0 to 5°C: 50% (Moderate derating)
        - 5 to 12°C: Linear ramp 50% → 77% (Light derating - battery warming)
        - 12 to 40°C: 100% (Optimal range)
        - 40 to 50°C: Linear ramp 100% → 50% (Thermal derating)
        - 50 to 60°C: Linear ramp 50% → 0% (Severe derating)
        - Above 60°C: 0% (Shutdown - overheat protection)

        Args:
            temp (float): Battery temperature in °C

        Returns:
            float: C-rate multiplier between 0.0 and 1.0
        """
        if temp is None:
            return 1.0  # No temperature sensor - assume optimal conditions

        if temp < -10:
            # Charging locked below -10°C - prevents cell damage
            return 0.0
        elif temp < 0:
            # Strong derating: linear ramp from 7.5% to 50%
            # Protects against lithium-plating in extreme cold
            return 0.075 + ((temp + 10) / 10) * (0.50 - 0.075)
        elif temp < 5:
            # Moderate derating: flat 50%
            return 0.50
        elif temp < 12:
            # Light derating: linear ramp from 50% to 77%
            # Battery gradually accepts more power as it warms from 5-12°C
            progress = (temp - 5) / 7
            return 0.50 + progress * (0.77 - 0.50)
        elif temp <= 40:
            # Optimal operating range: 100% capacity
            return 1.0
        elif temp < 50:
            # Thermal derating: linear ramp from 100% to 50%
            # Battery protection during heat conditions (40-50°C)
            progress = (temp - 40) / 10
            return 1.0 - progress * (1.0 - 0.50)
        elif temp < 60:
            # Severe derating: linear ramp from 50% to 0%
            # Critical heat protection (50-60°C)
            progress = (temp - 50) / 10
            return 0.50 * (1 - progress)
        else:
            # Shutdown above 60°C - overheat protection
            return 0.0

    def __get_max_charge_power_dyn(self, soc=None, temp=None, min_charge_power=500):
        """
        Calculates the maximum charge power dynamically based on SOC and temperature.

        The formula reduces the charge power as SOC increases and applies temperature
        compensation to protect the battery:
        - At low SOC, the charge power is close to the maximum C-rate (e.g., 1C).
        - As SOC approaches 100%, the charge power decreases exponentially.
        - Temperature compensation is automatically applied if sensor is configured.
        - The charge power is never less than the specified minimum value.

        Args:
            soc (float, optional): The state of charge to use for calculation.
                                If None, the current SOC is used.
            temp (float, optional): The battery temperature in °C.
                                If None, the current temperature is used.
            min_charge_power (float): The minimum charge power in watts.

        Returns:
            float: The dynamically calculated maximum charge power in watts.
        """
        if not self.battery_data.get("charging_curve_enabled", True):
            max_charge_power = self.max_charge_power_fix
            if max_charge_power != self.last_max_charge_power_dyn:
                self.max_charge_power_dyn = max_charge_power
                self.last_max_charge_power_dyn = max_charge_power
                logger.info(
                    "[BATTERY-IF] Max charge power: %s W (charging curve disabled, fixed)",
                    self.max_charge_power_dyn,
                )

                if self.base_control:
                    self.base_control.set_current_bat_charge_max(self.max_charge_power_dyn)

                if self.on_bat_max_changed:
                    self.on_bat_max_changed()
            else:
                self.max_charge_power_dyn = max_charge_power

            logger.debug(
                "[BATTERY-IF] Charging curve disabled, using fixed max charge power."
            )
            return

        if soc is None:
            soc = self.current_soc
        if temp is None:
            temp = self.current_temp  # May be None if not configured

        battery_capacity_wh = self.battery_data.get("capacity_wh", 0)

        if battery_capacity_wh <= 0:
            logger.warning("[BATTERY-IF] Battery capacity not set or invalid.")
            self.max_charge_power_dyn = min_charge_power
            return

        if soc < 0 or soc > 100:
            logger.warning(
                "[BATTERY-IF] Invalid SOC value: %s. Using minimum charge power.",
                soc,
            )
            self.max_charge_power_dyn = min_charge_power
            return

        # SOC-based C-rate calculation
        # Max C-rate is based on configured max charge power relative to battery capacity
        max_c_rate = self.max_charge_power_fix / battery_capacity_wh
        min_c_rate = 0.05

        if soc <= 50:
            c_rate_soc = max_c_rate
        else:
            c_rate_soc = max(min_c_rate, max_c_rate * (1 - (soc - 50) / 60) ** 2)

        # Temperature compensation
        temp_multiplier = self.__calculate_temp_multiplier(temp)

        # Combine factors
        final_c_rate = c_rate_soc * temp_multiplier

        # Calculate power
        max_charge_power = final_c_rate * battery_capacity_wh
        max_charge_power = min(max_charge_power, self.max_charge_power_fix)
        max_charge_power = round(max_charge_power / 50) * 50
        max_charge_power = max(max_charge_power, min_charge_power)

        # Update and notify if changed
        if max_charge_power != self.last_max_charge_power_dyn:
            self.max_charge_power_dyn = max_charge_power
            self.last_max_charge_power_dyn = max_charge_power

            # Enhanced logging with context
            temp_info = (
                f", Temp: {temp:.1f}°C (×{temp_multiplier:.2f})"
                if temp is not None
                else ""
            )
            logger.info(
                "[BATTERY-IF] Max charge power: %s W (SOC: %s%%%s)",
                self.max_charge_power_dyn,
                soc,
                temp_info,
            )

            if self.base_control:
                self.base_control.set_current_bat_charge_max(self.max_charge_power_dyn)

            if self.on_bat_max_changed:
                self.on_bat_max_changed()
        else:
            self.max_charge_power_dyn = max_charge_power

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
                self.__battery_request_current_temp()
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

    def perform_initial_price_calculation(self):
        """
        Perform initial battery price calculation synchronously if enabled.
        This should be called during startup before the first optimization run.

        Returns:
            bool: True if calculation completed successfully (or was not needed),
                  False if calculation failed.
        """
        if not self.price_handler or not self.price_handler.price_calculation_enabled:
            logger.info(
                "[BATTERY-IF] Battery price calculation disabled - using static config value: %.6f €/Wh",
                self.price_euro_per_wh,
            )
            return True

        logger.info(
            "[BATTERY-IF] Battery price calculation enabled - performing initial calculation..."
        )
        start_calc_time = time.time()

        # Ensure we have current SOC, temperature and usable capacity before calculation
        self.__battery_request_current_soc()
        self.__battery_request_current_temp()
        self.current_usable_capacity = max(
            0,
            (
                self.battery_data.get("capacity_wh", 0)
                * self.battery_data.get("discharge_efficiency", 1.0)
                * (self.current_soc - self.battery_data.get("min_soc_percentage", 0))
                / 100
            ),
        )

        # **FIX: Set timestamp BEFORE calculation to prevent race condition**
        self.price_handler.last_price_calculation = (
            datetime.now(self.price_handler.timezone)
            if self.price_handler.timezone
            else datetime.now()
        )

        # Perform the calculation (full lookback, no workaround)
        initial_price = self.price_handler.calculate_battery_price_from_history(
            inventory_wh=self.current_usable_capacity
        )

        calc_duration = time.time() - start_calc_time

        if initial_price is not None:
            self.price_euro_per_wh = initial_price
            # timestamp already set above
            logger.info(
                "[BATTERY-IF] Initial battery price calculated: %.6f €/Wh (%.4f €/kWh) in %.1f seconds",
                initial_price,
                initial_price * 1000,
                calc_duration,
            )
            return True
        else:
            logger.warning(
                "[BATTERY-IF] Initial battery price calculation failed after %.1f seconds - "
                "using static config value: %.6f €/Wh",
                calc_duration,
                self.price_euro_per_wh,
            )
            return False
