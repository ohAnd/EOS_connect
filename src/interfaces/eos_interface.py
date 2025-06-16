"""
This module provides an interface for interacting with an EOS server.
The `EosInterface` class includes methods for setting configuration values,
sending measurement data, sending optimization requests, saving configurations
to a file, and updating configurations from a file. It uses HTTP requests to
communicate with the EOS server.
Classes:
    EosInterface: A class that provides methods to interact with the EOS server.
Dependencies:
    - logging: For logging messages.
    - time: For measuring elapsed time.
    - json: For handling JSON data.
    - datetime: For working with date and time.
    - requests: For making HTTP requests.
Usage:
    Create an instance of the `EosInterface` class by providing the EOS server
    address, port, and timezone. Use the provided methods to interact with the
    EOS server for various operations such as setting configuration values,
    sending measurement data, and managing configurations.
"""

import logging
import time
import json
from datetime import datetime
import requests
import pandas as pd
import numpy as np

logger = logging.getLogger("__main__")
logger.info("[EOS] loading module ")


# EOS_API_PUT_LOAD_SERIES = {
#     f"http://{EOS_SERVER}:{EOS_SERVER_PORT}/v1/measurement/load-mr/series/by-name"  #
# }  # ?name=Household

# EOS_API_GET_CONFIG_VALUES = {f"http://{EOS_SERVER}:{EOS_SERVER_PORT}/v1/config"}

# EOS_API_PUT_LOAD_PROFILE = {
#     f"http://{EOS_SERVER}:{EOS_SERVER_PORT}/v1/measurement/load-mr/value/by-name"
# }


class EosInterface:
    """
    EosInterface is a class that provides an interface for interacting with an EOS server.
    This class includes methods for setting configuration values, sending measurement data,
    sending optimization requests, saving configurations to a file, and updating configurations
    from a file. It uses HTTP requests to communicate with the EOS server.
    Attributes:
        eos_server (str): The hostname or IP address of the EOS server.
        eos_port (int): The port number of the EOS server.
        base_url (str): The base URL constructed from the server and port.
        time_zone (timezone): The timezone used for time-related operations.
    Methods:
        set_config_value(key, value):
        send_measurement_to_eos(dataframe):
            Send measurement data to the EOS server.
        eos_set_optimize_request(payload, timeout=180):
            Send an optimization request to the EOS server.
        eos_save_config_to_config_file():
        eos_update_config_from_config_file():
    """

    def __init__(self, eos_server, eos_port, timezone):
        self.eos_server = eos_server
        self.eos_port = eos_port
        self.base_url = f"http://{eos_server}:{eos_port}"
        self.time_zone = timezone
        self.last_start_solution = None
        self.home_appliance_released = False
        self.home_appliance_start_hour = None
        self.eos_version = None
        self.eos_version = self.__retrieve_eos_version()

    # EOS basic API helper
    def set_config_value(self, key, value):
        """
        Set a configuration value on the EOS server.
        """
        if isinstance(value, list):
            value = json.dumps(value)
        params = {"key": key, "value": value}
        response = requests.put(
            self.base_url + "/v1/config/value", params=params, timeout=10
        )
        response.raise_for_status()
        logger.info(
            "[EOS] Config value set successfully. Key: {key} \t\t => Value: {value}"
        )

    def send_measurement_to_eos(self, dataframe):
        """
        Send the measurement data to the EOS server.
        """
        params = {
            "data": dataframe.to_json(orient="index"),
            "dtype": "float64",
            "tz": "UTC",
        }
        response = requests.put(
            self.base_url
            + "/v1/measurement/load-mr/series/by-name"
            + "?name=Household",
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        if response.status_code == 200:
            logger.debug("[EOS] Measurement data sent to EOS server successfully.")
        else:
            logger.debug(
                "[EOS]"
                "Failed to send data to EOS server. Status code: {response.status_code}"
                ", Response: {response.text}"
            )

    def eos_set_optimize_request(self, payload, timeout=180):
        """
        Send the optimize request to the EOS server.
        """
        headers = {"accept": "application/json", "Content-Type": "application/json"}
        request_url = (
            self.base_url
            + "/optimize"
            + "?start_hour="
            + str(datetime.now(self.time_zone).hour)
        )
        logger.info(
            "[EOS] OPTIMIZE request optimization with: %s - and with timeout: %s",
            request_url,
            timeout,
        )
        try:
            start_time = time.time()
            response = requests.post(
                request_url, headers=headers, json=payload, timeout=timeout
            )
            end_time = time.time()
            elapsed_time = end_time - start_time
            minutes, seconds = divmod(elapsed_time, 60)
            logger.info(
                "[EOS] OPTIMIZE response retrieved successfully in %d min %.2f sec",
                int(minutes),
                seconds,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            logger.error("[EOS] OPTIMIZE Request timed out after %s seconds", timeout)
            return {"error": "Request timed out - trying again with next run"}
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EOS] OPTIMIZE Request failed: %s - response: %s", e, response.text
            )
            logger.debug(
                "[EOS] The payload for the optimization request was: %s", payload
            )
            return {"error": str(e)}

    def examine_response_to_control_data(self, optimized_response_in):
        """
        Examines the optimized response data for control parameters such as AC charge demand,
        DC charge demand, and discharge allowance for the current hour.
        Args:
            optimized_response_in (dict): A dictionary containing control data with keys
                                          "ac_charge", "dc_charge", and "discharge_allowed".
                                          Each key maps to a list or dictionary where the
                                          current hour's data can be accessed.
        Returns:
            tuple: A tuple containing:
                - ac_charge_demand_relative (float or None): The AC charge demand percentage
                  for the current hour, or None if not present.
                - dc_charge_demand_relative (float or None): The DC charge demand percentage
                  for the current hour, or None if not present.
                - discharge_allowed (bool or None): Whether discharge is allowed for the
                  current hour, or None if not present.
        Logs:
            - Debug logs for AC charge demand, DC charge demand, and discharge allowance
              values for the current hour if they are present in the input.
            - An error log if no control data is found in the optimized response.
        """
        current_hour = datetime.now(self.time_zone).hour
        ac_charge_demand_relative = None
        dc_charge_demand_relative = None
        discharge_allowed = None
        response_error = False
        # ecar_response = None
        if "ac_charge" in optimized_response_in:
            ac_charge_demand_relative = optimized_response_in["ac_charge"]
            # getting entry for current hour
            ac_charge_demand_relative = ac_charge_demand_relative[current_hour]

            logger.debug(
                "[EOS] RESPONSE AC charge demand for current hour %s:00 -> %s %%",
                current_hour,
                ac_charge_demand_relative * 100,
            )
        if "dc_charge" in optimized_response_in:
            dc_charge_demand_relative = optimized_response_in["dc_charge"]
            # getting entry for current hour
            dc_charge_demand_relative = dc_charge_demand_relative[current_hour]
            logger.debug(
                "[EOS] RESPONSE DC charge demand for current hour %s:00 -> %s %%",
                current_hour,
                dc_charge_demand_relative * 100,
            )
        if "discharge_allowed" in optimized_response_in:
            discharge_allowed = optimized_response_in["discharge_allowed"]
            # getting entry for current hour
            discharge_allowed = bool(discharge_allowed[current_hour])
            logger.debug(
                "[EOS] RESPONSE Discharge allowed for current hour %s:00 %s",
                current_hour,
                discharge_allowed,
            )
        # if "eauto_obj" in optimized_response_in:
        #     eauto_obj = optimized_response_in["eauto_obj"]

        if (
            "start_solution" in optimized_response_in
            and len(optimized_response_in["start_solution"]) > 1
        ):
            self.set_last_start_solution(optimized_response_in["start_solution"])
            logger.debug(
                "[EOS] RESPONSE Start solution for current hour %s:00 %s",
                current_hour,
                self.get_last_start_solution(),
            )
        else:
            logger.error("[EOS] RESPONSE No control data in optimized response")
            response_error = True

        if "washingstart" in optimized_response_in:
            self.home_appliance_start_hour = optimized_response_in["washingstart"]
            if self.home_appliance_start_hour == current_hour:
                self.home_appliance_released = True
            else:
                self.home_appliance_released = False
            logger.debug(
                "[EOS] RESPONSE Home appliance - current hour %s:00 - start hour %s - is Released: %s",
                current_hour,
                self.home_appliance_start_hour,
                self.home_appliance_released,
            )

        return (
            ac_charge_demand_relative,
            dc_charge_demand_relative,
            discharge_allowed,
            response_error,
        )

    def eos_save_config_to_config_file(self):
        """
        Save the current configuration to the configuration file on the EOS server.
        """
        response = requests.put(self.base_url + "/v1/config/file", timeout=10)
        response.raise_for_status()
        logger.debug("[EOS] CONFIG saved to config file successfully.")

    def eos_update_config_from_config_file(self):
        """
        Update the current configuration from the configuration file on the EOS server.
        """
        try:
            response = requests.post(self.base_url + "/v1/config/update", timeout=10)
            response.raise_for_status()
            logger.info("[EOS] CONFIG Config updated from config file successfully.")
        except requests.exceptions.Timeout:
            logger.error(
                "[EOS] CONFIG Request timed out while updating config from config file."
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EOS] CONFIG Request failed while updating config from config file: %s",
                e,
            )

    def set_last_start_solution(self, last_start_solution):
        """
        Set the last start solution for the EOS interface.

        Args:
            last_start_solution (str): The last start solution to set.
        """
        self.last_start_solution = last_start_solution

    def get_last_start_solution(self):
        '''
        """
        Get the last start solution for the EOS interface.

        Returns:
            str: The last start solution.
        """
        '''
        return self.last_start_solution

    def get_home_appliance_released(self):
        """
        Get the home appliance released status.

        Returns:
            bool: True if the home appliance is released, False otherwise.
        """
        return self.home_appliance_released
    
    def get_home_appliance_start_hour(self):
        """
        Get the home appliance start hour.

        Returns:
            int: The hour when the home appliance starts.
        """
        return self.home_appliance_start_hour

    # function that creates a pandas dataframe with a DateTimeIndex with the given average profile
    def create_dataframe(self, profile):
        """
        Creates a pandas DataFrame with hourly energy values for a given profile.

        Args:
            profile (list of tuples): A list of tuples where each tuple contains:
                - month (int): The month (1-12).
                - weekday (int): The day of the week (0=Monday, 6=Sunday).
                - hour (int): The hour of the day (0-23).
                - energy (float): The energy value to set.

        Returns:
            pandas.DataFrame: A DataFrame with a DateTime index for the year 2025 and a 'Household'
            column containing the energy values from the profile.
        """

        # create a list of all dates in the year
        dates = pd.date_range(start="1/1/2025", end="31/12/2025", freq="H")
        # create an empty dataframe with the dates as index
        df = pd.DataFrame(index=dates)
        # add a column 'Household' to the dataframe with NaN values
        df["Household"] = np.nan
        # iterate over the profile and set the energy values in the dataframe
        for entry in profile:
            month = entry[0]
            weekday = entry[1]
            hour = entry[2]
            energy = entry[3]
            # get the dates that match the month, weekday and hour
            dates = df[
                (df.index.month == month)
                & (df.index.weekday == weekday)
                & (df.index.hour == hour)
            ].index
            # set the energy value for the dates
            for date in dates:
                df.loc[date, "Household"] = energy
        return df

    def __retrieve_eos_version(self):
        """
        Get the EOS version from the server. Dirty hack to get something to distinguish between
        different versions of the EOS server.

        Returns:
            str: The EOS version.
        """
        try:
            response = requests.get(self.base_url + "/v1/health", timeout=10)
            response.raise_for_status()
            eos_version = response.json().get("status")
            if eos_version == "alive":
                eos_version = ">=2025-04-09"
            logger.info("[EOS] Getting EOS version: %s", eos_version)
            return eos_version
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # if not found, assume version < 2025-04-09
                eos_version = "<2025-04-09"
                logger.info("[EOS] Getting EOS version: %s", eos_version)
                return eos_version
            else:
                logger.error(
                    "[EOS] HTTP error occurred while getting EOS version: %s", e
                )
            return None
        except requests.exceptions.ConnectTimeout:
            logger.error(
                "[EOS] Failed to get EOS version - Server not reachable:"+
                " Connection to %s timed out",
                self.base_url,
            )
            return "Server not reachable"
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[EOS] Failed to get EOS version - Server not reachable: Connection error: %s",
                e,
            )
            return "Server not reachable"
        except requests.exceptions.RequestException as e:
            logger.error("[EOS] Failed to get EOS version - Error: %s", e)
            return None
        except json.JSONDecodeError as e:
            logger.error("[EOS] Failed to decode EOS version response: %s", e)
            return None

    def get_eos_version(self):
        """
        Get the EOS version from the server.

        Returns:
            str: The EOS version.
        """
        return self.eos_version
