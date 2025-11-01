"""
This module provides the `EvccInterface` class, which serves as an interface to interact
with the Electric Vehicle Charging Controller (EVCC) API. The class enables periodic
fetching of the charging state, charging mode, and detailed vehicle data, and triggers
a callback when either state or mode changes.

Classes:
    EvccInterface: A class to interact with the EVCC API, manage charging state, mode,
                   and detailed data updates, and handle state change callbacks.

Dependencies:
    - logging: For logging messages and errors.
    - threading: For managing background threads.
    - time: For implementing delays in the update loop.
    - requests: For making HTTP requests to the EVCC API.

Usage:
    Create an instance of the `EvccInterface` class by providing the EVCC API URL,
    an optional update interval, and a callback function to handle charging state or
    mode changes. The class will automatically start a background thread to periodically
    fetch the charging state, mode, and detailed data from the API.
"""

import logging
import threading
import time
import requests

logger = logging.getLogger("__main__")
logger.info("[EVCC] loading module ")

# mapping of charging modes to their priorities:
# off: 0, pv: 1, pvmin: 2, now: 3
CHARGING_MODE_PRIORITY = {
    "off": 0,
    "pv": 1,
    "minpv": 2,
    "pv+now": 3,
    "minpv+now": 4,
    "pv+plan": 5,
    "minpv+plan": 6,
    "now": 7,
}


class EvccInterface:
    """
    EvccInterface is a class that provides an interface to interact with the EVCC
    (Electric Vehicle Charging Controller) API.
    It periodically fetches the charging state and mode, and triggers a callback when
    either the state or mode changes.

    Attributes:
        last_known_charging_state (bool): The last known charging state.
        last_known_charging_mode (str): The last known charging mode.
        on_charging_state_change (callable): A callback function to be called when
                                             the charging state or mode changes.
        _update_thread (threading.Thread):   The background thread for updating
                                             the charging state and mode.
        _stop_event (threading.Event):       An event to signal the thread to stop.

    Methods:
        __init__(url, update_interval=15, on_charging_state_change=None):
        get_charging_state():
        get_charging_mode():
        start_update_service():
        shutdown():
        _update_charging_state_loop():
        __request_charging_state():
            Fetches the EVCC state from the API and updates the charging state and mode.
        fetch_evcc_state_via_api():
    """

    def __init__(
        self, url, ext_bat_mode=False, update_interval=15, on_charging_state_change=None
    ):
        """
        Initializes the EVCC interface and starts the update service.

        Args:
            url (str): The base URL for the EVCC API.
            ext_bat_mode (bool, optional): Enables external battery mode. Defaults to False.
            update_interval (int, optional): The interval (in seconds) for updating
            the charging state. Defaults to 15.
            on_charging_state_change (callable, optional): A callback function to be called
            when the charging state or mode changes. Defaults to None.
        """
        self.url = url
        self.last_known_charging_state = False
        # off, pv, pvmin, now
        self.last_known_charging_mode = None
        self.current_detail_data_list = [
            {
                "connected": False,
                "charging": False,
                "mode": "off",
                "chargeDuration": 0,
                "chargeRemainingDuration": 0,
                "chargedEnergy": 0,
                "chargeRemainingEnergy": 0,
                "sessionEnergy": 0,
                "vehicleSoc": 0,
                "vehicleRange": 0,
                "vehicleOdometer": 0,
                "vehicleName": "",
                "smartCostActive": False,
                "planActive": False,
            }
        ]
        self.external_battery_mode_en = ext_bat_mode
        self.external_battery_mode = "off"  # Default mode

        # Initialize with safe defaults
        self.last_known_charging_state = False
        self.last_known_charging_mode = "off"
        self.current_detail_data_list = self.__get_default_detail_data()

        self.evcc_version = None  # Placeholder for EVCC version if needed

        self.update_interval = update_interval
        self.on_charging_state_change = on_charging_state_change  # Store the callback
        self._update_thread = None
        self._stop_event = threading.Event()

        check_result = self.__check_config()
        if check_result is False:
            logger.error("[EVCC] Invalid configuration. Update service not started.")
            return
        elif check_result == 2:
            logger.info("[EVCC] Not configured. Update service not started.")
            return
        self.start_update_service()

    def __check_config(self):
        """
        Checks if the configuration is valid.
        """
        if not self.url or self.url == "http://yourEVCCserver:7070":
            logger.info("[EVCC] URL is not set. Assuming no evcc connection is needed.")
            if self.external_battery_mode_en:
                logger.error(
                    "[EVCC] External battery mode is enabled, but no EVCC URL is set."
                )
                return False
            return 2
        # check reachability of the EVCC server
        try:
            response = requests.get(self.url, timeout=5)
            if response.status_code != 200:
                logger.error(
                    "[EVCC] Unable to reach EVCC server at %s. Status code: %s",
                    self.url,
                    response.status_code,
                )
                return False
            response_api = requests.get(self.url + "/api/state", timeout=5)
            # check if the first entry in JSON is "result"
            if response_api.status_code == 200:
                if "result" in response_api.json():
                    self.evcc_version = (
                        response_api.json().get("result", {}).get("version", None)
                    )
                    logger.info(
                        "[EVCC] Successfully connected to EVCC server at %s. Old API Version: %s",
                        self.url,
                        self.evcc_version,
                    )
                else:  # assume new API version
                    self.evcc_version = response_api.json().get("version", None)
                    logger.info(
                        "[EVCC] Successfully connected to EVCC server at %s. New API Version: %s",
                        self.url,
                        self.evcc_version,
                    )
            return True
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[EVCC] Connection error while checking EVCC server reachability: %s"
                + "\n[EVCC] anyway ... starting loop and retrying...",
                e,
            )
            return True
        except requests.exceptions.Timeout as e:
            logger.error(
                "[EVCC] Timeout while checking EVCC server reachability: %s", e
            )
            return False
        except requests.exceptions.HTTPError as e:
            logger.error(
                "[EVCC] HTTP error while checking EVCC server reachability: %s", e
            )
            return False
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EVCC] Unexpected error while checking EVCC server reachability: %s", e
            )
            return False

    def __get_default_detail_data(self):
        """
        Returns default detail data when EVCC is unreachable.
        """
        return [
            {
                "connected": False,
                "charging": False,
                "mode": "off",
                "chargeDuration": 0,
                "chargeRemainingDuration": 0,
                "chargedEnergy": 0,
                "chargeRemainingEnergy": 0,
                "sessionEnergy": 0,
                "vehicleSoc": 0,
                "vehicleRange": 0,
                "vehicleOdometer": 0,
                "vehicleName": "",
                "smartCostActive": False,
                "planActive": False,
            }
        ]

    def get_charging_state(self):
        """
        Returns the last known charging state.
        """
        return self.last_known_charging_state

    def get_charging_mode(self):
        """
        Returns the last known charging mode.
        """
        return self.last_known_charging_mode

    def get_current_detail_data(self):
        """
        Returns the current detail data of the EVCC.
        """
        return self.current_detail_data_list

    def start_update_service(self):
        """
        Starts the background thread to periodically update the charging state.
        """
        if self._update_thread is None or not self._update_thread.is_alive():
            self._stop_event.clear()
            self._update_thread = threading.Thread(
                target=self._update_charging_state_loop, daemon=True
            )
            self._update_thread.start()
            logger.info("[EVCC] Update service started.")

    def shutdown(self):
        """
        Stops the background thread and shuts down the update service.
        """
        if self.external_battery_mode_en:
            self.__disable_external_battery_mode()
        if self._update_thread and self._update_thread.is_alive():
            self._stop_event.set()
            self._update_thread.join()
            logger.info("[EVCC] Update service stopped.")

    def _update_charging_state_loop(self):
        """
        The loop that runs in the background thread to update the charging state.
        """
        while not self._stop_event.is_set():
            try:
                result = self.__get_evcc_loadpoints_vehicles()
                if result is None:
                    # EVCC server unreachable, use last known values and continue
                    logger.warning("[EVCC] Server unreachable, using last known values")
                    # Skip this iteration but don't break the loop
                    sleep_interval = self.update_interval
                    while sleep_interval > 0:
                        if self._stop_event.is_set():
                            return
                        time.sleep(min(1, sleep_interval))
                        sleep_interval -= 1
                    continue

                loadpoints, vehicles = result
                self.__get_states_of_loadpoints(loadpoints, vehicles)

                sum_states = self.__get_states_modes_of_connected_loadpoints(loadpoints)
                self.__get_summerized_charging_state_n_mode(sum_states)

                if (
                    self.external_battery_mode_en
                    and self.external_battery_mode != "off"
                ):
                    # Set the external battery mode if it is set
                    self.__set_external_battery_mode_loop()
            except (requests.exceptions.RequestException, ValueError, KeyError) as e:
                logger.error(
                    "[EVCC] Error while updating charging state: %s."
                    + " Continuing with last known values",
                    e,
                )
            # Break the sleep interval into smaller chunks to allow immediate shutdown
            sleep_interval = self.update_interval
            while sleep_interval > 0:
                if self._stop_event.is_set():
                    return  # Exit immediately if stop event is set
                time.sleep(min(1, sleep_interval))  # Sleep in 1-second chunks
                sleep_interval -= 1

    def __get_evcc_loadpoints_vehicles(self):
        data = self.__fetch_evcc_state_via_api()
        if not data or not isinstance(data.get("loadpoints"), list):
            logger.error("[EVCC] Invalid or missing loadpoints in the response.")
            return None
        # check if there are more than one loadpoints
        loadpoints = data["loadpoints"] if data["loadpoints"] else None
        # logger.debug(
        #     "[EVCC] got 1st loadpoint: %s",
        #     loadpoints[0].get("title") if loadpoints else "None",
        # )

        vehicles = data.get("vehicles", {})

        return loadpoints, vehicles

    def __get_states_modes_of_connected_loadpoints(self, loadpoints):
        # check if there are more than one loadpoints
        collected_states_modes = []
        if len(loadpoints) > 0:
            # check for connected loadpoints
            for lp in loadpoints:
                if lp.get("connected", False):
                    # logger.info("[EVCC] Using connected loadpoint: %s", lp.get("title"))
                    collected_states_modes.append(
                        {
                            "charging": lp.get("charging", False),
                            "mode": lp.get("mode", "off"),
                            "smartCostActive": lp.get("smartCostActive", False),
                            "planActive": lp.get("planActive", False),
                        }
                    )
        # logger.debug(
        #     "[EVCC] Collected states and modes from connected loadpoints: %s",
        #     collected_states_modes,
        # )
        return collected_states_modes

    def __get_summerized_charging_state_n_mode(self, collected_states_modes):

        sum_mode_priority = 0
        sum_charging_mode = "off"
        sum_charging_state = False
        for entry in collected_states_modes:
            if entry["charging"]:
                mode = entry["mode"]
                sum_charging_state = True
                if mode in ("pv", "minpv") and entry.get("smartCostActive", False):
                    mode = mode + "+now"
                if mode in ("pv", "minpv") and entry.get("planActive", False):
                    mode = mode + "+plan"
                if sum_mode_priority < CHARGING_MODE_PRIORITY[mode]:
                    sum_mode_priority = CHARGING_MODE_PRIORITY[mode]
                    sum_charging_mode = mode
                if entry.get("smartCostActive", False):
                    sum_smart_cost_active = True
        # if no loadpoints are charging, set charging mode to the first one
        if sum_charging_state is False:
            sum_charging_mode = (
                collected_states_modes[0]["mode"] if collected_states_modes else "off"
            )
            if sum_charging_mode in ("pv", "minpv") and collected_states_modes[0].get(
                "smartCostActive", False
            ):
                sum_charging_mode = sum_charging_mode + "+now"
            if sum_charging_mode in ("pv", "minpv") and collected_states_modes[0].get(
                "planActive", False
            ):
                sum_charging_mode = sum_charging_mode + "+plan"

            # logger.debug(
            #     "[EVCC] No charging loadpoints found."
            #     + " Setting charging mode to first connected loadpoint."
            # )

        # Check if the charging state has changed
        if sum_charging_state != self.last_known_charging_state:
            logger.info("[EVCC] SUM Charging state changed to: %s", sum_charging_state)
            self.last_known_charging_state = sum_charging_state
            # Trigger the callback if provided
            if self.on_charging_state_change:
                self.on_charging_state_change(sum_charging_state)

        # logger.debug(
        #     "[EVCC] SUM Charging state: %s - Charging mode: %s - SmartCostActive: %s",
        #     sum_charging_state,
        #     sum_charging_mode,
        #     sum_smart_cost_active,
        # )
        # Check if the charging state has changed
        if sum_charging_mode != self.last_known_charging_mode:
            logger.info("[EVCC] SUM Charging mode changed to: %s", sum_charging_mode)
            self.last_known_charging_mode = sum_charging_mode
            # Trigger the callback if provided
            if self.on_charging_state_change:
                self.on_charging_state_change(sum_charging_state)

        return sum_charging_state, sum_charging_mode

    def __get_states_of_loadpoints(self, loadpoints, vehicles):
        """
        Fetches the EVCC state from the API and updates the charging state and mode.
        """
        self.current_detail_data_list = []
        for loadpoint in loadpoints:
            vehicle_name = vehicles.get(loadpoint.get("vehicleName", ""), {}).get(
                "title", ""
            )
            mode = loadpoint.get("mode", "off")
            if mode in ("pv", "minpv") and loadpoint.get("smartCostActive", False):
                mode = mode + "+now"
            if mode in ("pv", "minpv") and loadpoint.get("planActive", False):
                mode = mode + "+plan"
            detail_data = {
                "connected": loadpoint.get("connected", False),
                "charging": loadpoint.get("charging", False),
                "mode": mode,
                "chargeDuration": loadpoint.get("chargeDuration", 0),
                "chargeRemainingDuration": loadpoint.get("chargeRemainingDuration", 0),
                "chargedEnergy": loadpoint.get("chargedEnergy", 0),
                "chargeRemainingEnergy": loadpoint.get("chargeRemainingEnergy", 0),
                "sessionEnergy": loadpoint.get("sessionEnergy", 0),
                "vehicleSoc": loadpoint.get("vehicleSoc", 0),
                "vehicleRange": loadpoint.get("vehicleRange", 0),
                "vehicleOdometer": loadpoint.get("vehicleOdometer", 0),
                "vehicleName": vehicle_name,
                "smartCostActive": loadpoint.get("smartCostActive", False),
                "planActive": loadpoint.get("planActive", False),
            }
            self.current_detail_data_list.append(detail_data)
        return True

    def __fetch_evcc_state_via_api(self):
        """
        Fetches the state of the EVCC (Electric Vehicle Charging Controller) via its API.

        This method sends a GET request to the EVCC API endpoint to retrieve the current state.
        If the request is successful, the response is parsed as JSON and returned.
        In case of a timeout or other request-related errors, the method logs the error and
        returns None.

        Returns:
            dict: The JSON response from the EVCC API containing the state information,
                  or None if the request fails or times out.
        """
        evcc_url = self.url + "/api/state"
        # logger.debug("[EVCC] fetching evcc state with url: %s", evcc_url)
        try:
            response = requests.get(evcc_url, timeout=6)
            response.raise_for_status()

            if "result" in response.json():
                data = response.json()["result"]
            else:
                data = response.json()
            # logger.debug("[EVCC] successfully fetched EVCC state")
            return data
        except requests.exceptions.Timeout:
            logger.error("[EVCC] Request timed out while fetching EVCC state.")
            return None  # Default SOC value in case of timeout
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EVCC] Request failed while fetching EVCC state. Error: %s.", e
            )
            return None  # Default SOC value in case of request failure

    def set_external_battery_mode(self, mode):
        """
        Sets the external battery mode in the EVCC.

        Args:
            mode (str): The external battery mode to set. Can be one of:
                        "avoid_discharge", "discharge_allowed", "force_charge".
        """
        if mode not in ["avoid_discharge", "discharge_allowed", "force_charge", "off"]:
            logger.error(
                "[EVCC] Invalid external battery mode: %s. "
                + "Expected one of ['avoid_discharge', 'discharge_allowed',"
                + " 'force_charge', 'off'].",
                mode,
            )
        elif mode == "off":
            self.__disable_external_battery_mode()
            logger.info("[EVCC] External battery mode disabled.")
        else:
            self.external_battery_mode = mode

    def get_current_external_battery_mode(self):
        """
        Retrieves the current external battery mode from the EVCC.
        """
        return self.external_battery_mode

    def __disable_external_battery_mode(self):
        """
        Disables the external battery mode in the EVCC.
        """
        evcc_url = self.url + "/api/batterymode"
        try:
            response = requests.delete(evcc_url, timeout=6)
            response.raise_for_status()
            logger.info("[EVCC] External battery mode disabled. response: %s", response)
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EVCC] Request failed while disabling external battery mode. Error: %s.",
                e,
            )

    def __set_external_battery_mode_loop(self):
        """
        Sets the external battery mode in a loop until the mode is set successfully.

        Args:
            mode (str): The external battery mode to set. Can be one of:
                        "avoid_discharge", "discharge_allowed", "force_charge".
        """
        if self.external_battery_mode == "avoid_discharge":
            self.__set_external_battery_mode_avoid_discharge()
        elif self.external_battery_mode == "discharge_allowed":
            self.__set_external_battery_mode_discharge_allowed()
        elif self.external_battery_mode == "force_charge":
            self.__set_external_battery_mode_force_charge()
        else:
            logger.error(
                "[EVCC] Invalid external battery mode: %s. "
                + "Expected one of ['avoid_discharge', 'discharge_allowed', 'force_charge'].",
                self.external_battery_mode,
            )

    def __set_external_battery_mode_avoid_discharge(self):
        """
        Enables the external battery mode with AVOID DISCHARGE in the EVCC.
        """
        evcc_url = self.url + "/api/batterymode/hold"
        try:
            response = requests.post(evcc_url, timeout=6)
            response.raise_for_status()
            logger.debug(
                "[EVCC] External battery mode set AVOID DISCHARGE. response: %s",
                response,
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EVCC] Request failed while enabling external battery mode. Error: %s.",
                e,
            )

    def __set_external_battery_mode_discharge_allowed(self):
        """
        Enables the external battery mode with DISCHARGE ALLOWED in the EVCC.
        """
        evcc_url = self.url + "/api/batterymode/normal"
        try:
            response = requests.post(evcc_url, timeout=6)
            response.raise_for_status()
            logger.debug(
                "[EVCC] External battery mode set DISCHARGE ALLOWED. response: %s",
                response,
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EVCC] Request failed while enabling external battery mode. Error: %s.",
                e,
            )

    def __set_external_battery_mode_force_charge(self):
        """
        Enables the external battery mode with FORCE CHARGE in the EVCC.
        """
        evcc_url = self.url + "/api/batterymode/charge"
        try:
            response = requests.post(evcc_url, timeout=6)
            response.raise_for_status()
            logger.debug(
                "[EVCC] External battery mode set FORCE CHARGE. response: %s", response
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EVCC] Request failed while enabling external battery mode. Error: %s.",
                e,
            )
