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
import os
import time
import json
from datetime import datetime, timedelta
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

    def __init__(self, config, timezone):
        self.eos_source = config.get("source", "eos_server")
        self.eos_server = config.get("server", "192.168.1.1")
        self.eos_port = config.get("port", 8503)
        self.base_url = f"http://{self.eos_server}:{self.eos_port}"
        self.time_zone = timezone
        self.switch_to_evcc_opt = False
        if self.eos_source == "evcc_opt":
            self.switch_to_evcc_opt = True
            logger.info("[EOS] Using evcc opt as the optimization interface")
        self.last_start_solution = None
        self.home_appliance_released = False
        self.home_appliance_start_hour = None
        self.eos_version = (
            ">=2025-04-09"  # use as default value in case version check fails
        )
        if self.eos_source == "eos_server":
            self.eos_version = self.__retrieve_eos_version()

        self.last_control_data = [
            {
                "ac_charge_demand": 0,
                "dc_charge_demand": 0,
                "discharge_allowed": False,
                "error": 0,
                "hour": -1,
            },
            {
                "ac_charge_demand": 0,
                "dc_charge_demand": 0,
                "discharge_allowed": False,
                "error": 0,
                "hour": -1,
            },
        ]

        self.last_optimization_runtimes = [0] * 5  # list to store last 5 runtimes
        self.last_optimization_runtime_number = 0  # index for circular list
        self.is_first_run = True  # Add flag to track first run

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

        if self.switch_to_evcc_opt:
            evcc_opt_response, avg_runtime, evcc_opt_request = (
                self.evcc_opt_set_optimize_request(payload, timeout)
            )
            evcc_opt_to_eos_response = self.evcc_opt_transform_response(
                evcc_opt_response, evcc_opt_request
            )
            return evcc_opt_to_eos_response, avg_runtime

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
        response = None  # Initialize response variable
        try:
            start_time = time.time()
            response = requests.post(
                request_url, headers=headers, json=payload, timeout=timeout
            )
            end_time = time.time()
            elapsed_time = end_time - start_time
            minutes, seconds = divmod(elapsed_time, 60)
            logger.info(
                "[EOS] OPTIMIZE response retrieved successfully in %d min %.2f sec for current run",
                int(minutes),
                seconds,
            )
            response.raise_for_status()
            # Check if the array is still filled with zeros
            if all(runtime == 0 for runtime in self.last_optimization_runtimes):
                # Fill all entries with the first real value
                self.last_optimization_runtimes = [elapsed_time] * 5
            else:
                # Store the runtime in the circular list only if successful
                self.last_optimization_runtimes[
                    self.last_optimization_runtime_number
                ] = elapsed_time
            self.last_optimization_runtime_number = (
                self.last_optimization_runtime_number + 1
            ) % 5
            # logger.debug(
            #     "[EOS] OPTIMIZE Last 5 runtimes in seconds: %s",
            #     self.last_optimization_runtimes,
            # )
            avg_runtime = sum(self.last_optimization_runtimes) / 5
            return response.json(), avg_runtime
        except requests.exceptions.Timeout:
            logger.error("[EOS] OPTIMIZE Request timed out after %s seconds", timeout)
            return {"error": "Request timed out - trying again with next run"}
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[EOS] OPTIMIZE Connection error - EOS server not reachable at %s "
                + "will try again with next cycle - error: %s",
                request_url,
                str(e),
            )
            return {
                "error": f"EOS server not reachable at {self.base_url} "
                + "will try again with next cycle"
            }
        except requests.exceptions.RequestException as e:
            logger.error("[EOS] OPTIMIZE Request failed: %s", e)
            if response is not None:
                logger.error("[EOS] OPTIMIZE Response status: %s", response.status_code)
                logger.debug(
                    "[EOS] OPTIMIZE ERROR - response of EOS is:"
                    + "\n---RESPONSE-------------------------------------------------\n %s"
                    + "\n------------------------------------------------------------",
                    response.text,
                )
            logger.debug(
                "[EOS] OPTIMIZE ERROR - payload for the request was:"
                + "\n---REQUEST--------------------------------------------------\n %s"
                + "\n------------------------------------------------------------",
                payload,
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
            self.last_control_data[0]["ac_charge_demand"] = ac_charge_demand_relative[
                current_hour
            ]
            self.last_control_data[1]["ac_charge_demand"] = ac_charge_demand_relative[
                current_hour + 1 if current_hour < 23 else 0
            ]
            # getting entry for current hour
            ac_charge_demand_relative = ac_charge_demand_relative[current_hour]
            logger.debug(
                "[EOS] RESPONSE AC charge demand for current hour %s:00 -> %s %%",
                current_hour,
                ac_charge_demand_relative * 100,
            )
        if "dc_charge" in optimized_response_in:
            dc_charge_demand_relative = optimized_response_in["dc_charge"]
            self.last_control_data[0]["dc_charge_demand"] = dc_charge_demand_relative[
                current_hour
            ]
            self.last_control_data[1]["dc_charge_demand"] = dc_charge_demand_relative[
                current_hour + 1 if current_hour < 23 else 0
            ]

            # getting entry for current hour
            dc_charge_demand_relative = dc_charge_demand_relative[current_hour]
            logger.debug(
                "[EOS] RESPONSE DC charge demand for current hour %s:00 -> %s %%",
                current_hour,
                dc_charge_demand_relative * 100,
            )
        if "discharge_allowed" in optimized_response_in:
            discharge_allowed = optimized_response_in["discharge_allowed"]
            self.last_control_data[0]["discharge_allowed"] = discharge_allowed[
                current_hour
            ]
            self.last_control_data[1]["discharge_allowed"] = discharge_allowed[
                current_hour + 1 if current_hour < 23 else 0
            ]
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

        self.last_control_data[0]["error"] = int(response_error)
        self.last_control_data[1]["error"] = int(response_error)
        self.last_control_data[0]["hour"] = current_hour
        self.last_control_data[1]["hour"] = current_hour + 1 if current_hour < 23 else 0

        if "washingstart" in optimized_response_in:
            self.home_appliance_start_hour = optimized_response_in["washingstart"]
            if self.home_appliance_start_hour == current_hour:
                self.home_appliance_released = True
            else:
                self.home_appliance_released = False
            logger.debug(
                "[EOS] RESPONSE Home appliance - current hour %s:00"
                + " - start hour %s - is Released: %s",
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

    def get_last_control_data(self):
        """
        Get the last control data for the EOS interface.

        Returns:
            list: The last control data.
        """
        return self.last_control_data

    def set_last_start_solution(self, last_start_solution):
        """
        Set the last start solution for the EOS interface.

        Args:
            last_start_solution (str): The last start solution to set.
        """
        self.last_start_solution = last_start_solution

    def get_last_start_solution(self):
        """
        Get the last start solution for the EOS interface.

        Returns:
            str: The last start solution.
        """
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
                    "[EOS] HTTP error occurred while getting EOS version"
                    + " - use preset version: %s : %s - Response: %s",
                    self.eos_version,
                    e,
                    e.response.text if e.response else "No response",
                )
                return self.eos_version  # return preset version if error occurs
        except requests.exceptions.ConnectTimeout:
            logger.error(
                "[EOS] Failed to get EOS version  - use preset version: '%s'"
                + " - Server not reachable: Connection to %s timed out",
                self.eos_version,
                self.base_url,
            )
            return self.eos_version  # return preset version if error occurs
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[EOS] Failed to get EOS version - use preset version: '%s' - Connection error: %s",
                self.eos_version,
                e,
            )
            return self.eos_version  # return preset version if error occurs
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EOS] Failed to get EOS version - use preset version: '%s' - Error: %s ",
                self.eos_version,
                e,
            )
            return self.eos_version  # return preset version if error occurs
        except json.JSONDecodeError as e:
            logger.error(
                "[EOS] Failed to decode EOS version - use preset version: '%s' - response: %s ",
                self.eos_version,
                e,
            )
            return self.eos_version  # return preset version if error occurs

    def get_eos_version(self):
        """
        Get the EOS version from the server.

        Returns:
            str: The EOS version.
        """
        return self.eos_version

    def calculate_next_run_time(self, current_time, avg_runtime, update_interval):
        """
        Calculate the next run time prioritizing quarter-hour alignment with improved gap filling.
        """
        # Calculate minimum time between runs
        min_gap_seconds = max((update_interval + avg_runtime) * 0.7, 30)

        # Find next quarter-hour from current time
        next_quarter = current_time.replace(second=0, microsecond=0)
        current_minute = next_quarter.minute

        minutes_past_quarter = current_minute % 15
        if minutes_past_quarter == 0 and current_time.second > 0:
            minutes_to_add = 15
        elif minutes_past_quarter == 0:
            minutes_to_add = 15
        else:
            minutes_to_add = 15 - minutes_past_quarter

        next_quarter += timedelta(minutes=minutes_to_add)

        quarter_aligned_start = next_quarter - timedelta(seconds=avg_runtime)

        # **BUG FIX**: Check if quarter_aligned_start is in the past
        if quarter_aligned_start <= current_time:
            # Move to the next quarter-hour
            next_quarter += timedelta(minutes=15)
            quarter_aligned_start = next_quarter - timedelta(seconds=avg_runtime)
            logger.debug(
                "[OPTIMIZATION] Quarter start was in past, moved to next: %s",
                next_quarter.strftime("%H:%M:%S"),
            )

        time_until_quarter_start = (
            quarter_aligned_start - current_time
        ).total_seconds()

        # Debug logging
        logger.debug(
            "[OPTIMIZATION] Debug: current=%s, next_quarter=%s, quarter_start=%s, time_until=%.1fs",
            current_time.strftime("%H:%M:%S"),
            next_quarter.strftime("%H:%M:%S"),
            quarter_aligned_start.strftime("%H:%M:%S"),
            time_until_quarter_start,
        )

        # More aggressive gap-filling: if we have at least 2x the update interval,
        # try a gap-fill run
        if (
            time_until_quarter_start >= (2 * update_interval)
            and time_until_quarter_start >= min_gap_seconds
        ):
            normal_next_start = current_time + timedelta(seconds=update_interval)
            logger.info(
                "[OPTIMIZATION] Gap-fill run: start %s (quarter-aligned run follows at %s)",
                normal_next_start.strftime("%H:%M:%S"),
                next_quarter.strftime("%H:%M:%S"),
            )
            return normal_next_start

        # Otherwise, use quarter-aligned timing
        absolute_min_seconds = max(avg_runtime * 0.5, 30)
        if time_until_quarter_start < absolute_min_seconds:
            next_quarter += timedelta(minutes=15)
            quarter_aligned_start = next_quarter - timedelta(seconds=avg_runtime)
            logger.debug(
                "[OPTIMIZATION] Quarter too close, moved to next: %s",
                next_quarter.strftime("%H:%M:%S"),
            )

        logger.info(
            "[OPTIMIZATION] Quarter-hour aligned run: start %s, finish at %s",
            quarter_aligned_start.strftime("%H:%M:%S"),
            next_quarter.strftime("%H:%M:%S"),
        )
        return quarter_aligned_start

    # evcc optimization

    def evcc_opt_set_optimize_request(self, payload, timeout=180):
        """
        Send the optimize request to the evcc optimization server.
        """
        # transform eos request to evcc opt request
        evcc_opt_request = {}
        evcc_opt_request, errors = self.evcc_opt_transform_request(payload)
        if errors:
            logger.error(
                "[EOS] EVCC OPT transformed request with %s errors: %s",
                len(errors),
                errors,
            )
        # write transformed payload to json file for debugging
        debug_path = os.path.join(
            os.path.dirname(__file__), "..", "json", "optimize_request_evcc_opt.json"
        )
        debug_path = os.path.abspath(debug_path)
        with open(debug_path, "w", encoding="utf-8") as fh:
            json.dump(evcc_opt_request, fh, indent=2, ensure_ascii=False)
        payload = evcc_opt_request
        # -> http://homeassistant:7050/optimize/charge-schedule
        self.base_url = "http://homeassistant:7050"
        request_url = self.base_url + "/optimize/charge-schedule"
        logger.info(
            "[EOS] EVCC OPT request optimization with: %s - and with timeout: %s",
            request_url,
            timeout,
        )
        headers = {"accept": "application/json", "Content-Type": "application/json"}
        response = None  # Initialize response variable
        try:
            start_time = time.time()
            response = requests.post(
                request_url, headers=headers, json=payload, timeout=timeout
            )
            end_time = time.time()
            elapsed_time = end_time - start_time
            minutes, seconds = divmod(elapsed_time, 60)
            logger.info(
                "[EOS] EVCC OPT response retrieved successfully in %d min %.2f sec for current run",
                int(minutes),
                seconds,
            )
            response.raise_for_status()
            # Check if the array is still filled with zeros
            if all(runtime == 0 for runtime in self.last_optimization_runtimes):
                # Fill all entries with the first real value
                self.last_optimization_runtimes = [elapsed_time] * 5
            else:
                # Store the runtime in the circular list only if successful
                self.last_optimization_runtimes[
                    self.last_optimization_runtime_number
                ] = elapsed_time
            self.last_optimization_runtime_number = (
                self.last_optimization_runtime_number + 1
            ) % 5
            # logger.debug(
            #     "[EOS] OPTIMIZE Last 5 runtimes in seconds: %s",
            #     self.last_optimization_runtimes,
            # )
            avg_runtime = sum(self.last_optimization_runtimes) / 5
            return response.json(), avg_runtime, evcc_opt_request
        except requests.exceptions.Timeout:
            logger.error("[EOS] EVCC OPT Request timed out after %s seconds", timeout)
            return {"error": "Request timed out - trying again with next run"}
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[EOS] EVCC OPT Connection error - EOS server not reachable at %s "
                + "will try again with next cycle - error: %s",
                request_url,
                str(e),
            )
            return {
                "error": f"EVCC OPT server not reachable at {self.base_url} "
                + "will try again with next cycle"
            }
        except requests.exceptions.RequestException as e:
            logger.error("[EOS] EVCC OPT Request failed: %s", e)
            if response is not None:
                logger.error("[EOS] EVCC OPT Response status: %s", response.status_code)
                logger.debug(
                    "[EOS] EVCC OPT ERROR - response of EOS is:"
                    + "\n---RESPONSE-------------------------------------------------\n %s"
                    + "\n------------------------------------------------------------",
                    response.text,
                )
            logger.debug(
                "[EOS] EVCC OPT ERROR - payload for the request was:"
                + "\n---REQUEST--------------------------------------------------\n %s"
                + "\n------------------------------------------------------------",
                payload,
            )
            return {"error": str(e)}

    def _validate_eos_input(self, eos_request):
        """
        External validation of EOS input -> returns (is_ok, external_errors).
        External errors are problems with incoming data and should be returned to callers.
        """
        errors = []
        if not isinstance(eos_request, dict):
            return False, ["eos_request must be a dict"]

        ems = eos_request.get("ems")
        if ems is None:
            errors.append("ems missing")
            return False, errors
        if not isinstance(ems, dict):
            errors.append("ems must be an object")
            return False, errors

        series_keys = [
            "pv_prognose_wh",
            "strompreis_euro_pro_wh",
            "einspeiseverguetung_euro_pro_wh",
            "gesamtlast",
        ]
        present_lengths = {}
        for key in series_keys:
            arr = ems.get(key)
            if arr is None:
                errors.append(f"ems.{key} missing")
                continue
            if not isinstance(arr, list):
                errors.append(f"ems.{key} must be a list")
                continue
            if len(arr) == 0:
                errors.append(f"ems.{key} must not be empty")
                continue
            # element-wise quick checks
            for i, v in enumerate(arr):
                try:
                    v_float = float(v)
                except (ValueError, TypeError):
                    errors.append(f"ems.{key}[{i}] is not numeric")
                    continue
                if v_float != v_float:
                    errors.append(f"ems.{key}[{i}] is NaN")
                if v_float == float("inf") or v_float == float("-inf"):
                    errors.append(f"ems.{key}[{i}] is infinite")
            present_lengths[key] = len(arr)

        # length mismatch among present series is an external error
        if len(present_lengths) >= 2:
            lengths_set = set(present_lengths.values())
            if len(lengths_set) > 1:
                errors.append(f"ems time-series lengths mismatch: {present_lengths}")

        # battery external sanity checks
        pv_akku = eos_request.get("pv_akku")
        if pv_akku is not None:
            if not isinstance(pv_akku, dict):
                errors.append("pv_akku must be an object if present")
            else:
                try:
                    cap = float(pv_akku.get("capacity_wh", 0))
                except (ValueError, TypeError):
                    errors.append("pv_akku.capacity_wh not numeric")
                else:
                    if cap <= 0:
                        errors.append("pv_akku.capacity_wh must be > 0")
                for fld in (
                    "initial_soc_percentage",
                    "min_soc_percentage",
                    "max_soc_percentage",
                ):
                    if fld in pv_akku:
                        try:
                            v = float(pv_akku[fld])
                        except (ValueError, TypeError):
                            errors.append(f"pv_akku.{fld} not numeric")
                        else:
                            if not 0.0 <= v <= 100.0:
                                errors.append(f"pv_akku.{fld} out of range 0-100: {v}")

        eauto = eos_request.get("eauto")
        if eauto is not None:
            if not isinstance(eauto, dict):
                errors.append("eauto must be an object if present")
            else:
                try:
                    evcap = float(eauto.get("capacity_wh", 0))
                except (ValueError, TypeError):
                    errors.append("eauto.capacity_wh not numeric")
                else:
                    if evcap < 0:
                        errors.append("eauto.capacity_wh must not be negative")
                for fld in (
                    "initial_soc_percentage",
                    "min_soc_percentage",
                    "max_soc_percentage",
                ):
                    if fld in eauto:
                        try:
                            v = float(eauto[fld])
                        except (ValueError, TypeError):
                            errors.append(f"eauto.{fld} not numeric")
                        else:
                            if not 0.0 <= v <= 100.0:
                                errors.append(f"eauto.{fld} out of range 0-100: {v}")

        # timestamp parse check (optional)
        if "timestamp" in eos_request:
            try:
                datetime.fromisoformat(
                    str(eos_request["timestamp"]).replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                errors.append("timestamp not in a recognized ISO format")

        return (len(errors) == 0), errors

    def _validate_evcc_request(self, evcc_req):
        """
        Internal schema validation of produced EVCC payload.
        Findings are returned, but are intended for logging only (not returned to external callers).
        Returns (is_valid, errors)
        """
        errors = []

        def is_number(x):
            return isinstance(x, (int, float)) and not isinstance(x, bool)

        if not isinstance(evcc_req, dict):
            return False, ["evcc_req must be a dict"]

        # strategy
        strat = evcc_req.get("strategy")
        if not isinstance(strat, dict):
            errors.append("strategy missing or not an object")
        else:
            for key in ("charging_strategy", "discharging_strategy"):
                if key not in strat or not isinstance(strat[key], str):
                    errors.append(f"strategy.{key} missing or not string")

        # grid
        grid = evcc_req.get("grid")
        if not isinstance(grid, dict):
            errors.append("grid missing or not an object")
        else:
            for key in ("p_max_imp", "p_max_exp", "prc_p_imp_exc"):
                if key not in grid or not is_number(grid[key]):
                    errors.append(f"grid.{key} missing or not numeric")

        # time_series
        ts = evcc_req.get("time_series")
        if not isinstance(ts, dict):
            errors.append("time_series missing or not an object")
        else:
            required_ts = ("dt", "gt", "ft", "p_N", "p_E")
            lengths = {}
            for key in required_ts:
                arr = ts.get(key)
                if not isinstance(arr, list):
                    errors.append(f"time_series.{key} missing or not a list")
                else:
                    if len(arr) == 0:
                        errors.append(f"time_series.{key} is empty")
                    else:
                        for i, v in enumerate(arr):
                            if not is_number(v):
                                errors.append(f"time_series.{key}[{i}] not numeric")
                        lengths[key] = len(arr)
            if lengths:
                unique_lengths = set(lengths.values())
                if len(unique_lengths) > 1:
                    errors.append(f"time_series arrays length mismatch: {lengths}")

        # batteries
        batteries = evcc_req.get("batteries")
        if not isinstance(batteries, list) or len(batteries) == 0:
            errors.append("batteries missing or not a non-empty list")
        else:
            numeric_fields = (
                "s_min",
                "s_max",
                "s_initial",
                "c_min",
                "c_max",
                "d_max",
                "p_a",
            )
            bool_fields = ("charge_from_grid", "discharge_to_grid")
            ts_len = None
            if isinstance(ts, dict) and isinstance(ts.get("dt"), list):
                ts_len = len(ts["dt"])
            for bi, b in enumerate(batteries):
                if not isinstance(b, dict):
                    errors.append(f"batteries[{bi}] not an object")
                    continue
                for bf in bool_fields:
                    if bf not in b or not isinstance(b[bf], bool):
                        errors.append(f"batteries[{bi}].{bf} missing or not boolean")
                for nf in numeric_fields:
                    if nf not in b or not is_number(b[nf]):
                        errors.append(f"batteries[{bi}].{nf} missing or not numeric")
                for list_key in ("p_demand", "s_goal"):
                    if list_key in b:
                        if not isinstance(b[list_key], list):
                            errors.append(f"batteries[{bi}].{list_key} must be a list")
                        elif ts_len is not None and len(b[list_key]) != ts_len:
                            errors.append(
                                f"batteries[{bi}].{list_key} length mismatch with time_series"
                            )

        for eff in ("eta_c", "eta_d"):
            if eff in evcc_req and not is_number(evcc_req[eff]):
                errors.append(f"{eff} must be numeric")

        return (len(errors) == 0), errors

    def evcc_opt_transform_request(self, eos_request):
        """
        Translate EOS request -> EVCC request.

        Returns:
            (evcc_req: dict, external_errors: list[str])
            external_errors is non-empty when incoming EOS data is problematic.
        """
        eos_request = eos_request or {}

        # external input validation first (so caller gets clear external errors)
        ok_eos, external_errors = self._validate_eos_input(eos_request)
        # build the payload anyway so caller can inspect it
        ems = eos_request.get("ems", {}) or {}

        pv_series = ems.get("pv_prognose_wh", []) or []
        price_series = ems.get("strompreis_euro_pro_wh", []) or []
        feed_series = ems.get("einspeiseverguetung_euro_pro_wh", []) or []
        load_series = ems.get("gesamtlast", []) or []

        # Slice series to start from current hour (assuming arrays start at midnight today)
        current_hour = datetime.now(self.time_zone).hour
        pv_series = (
            pv_series[current_hour:] if len(pv_series) > current_hour else pv_series
        )
        price_series = (
            price_series[current_hour:]
            if len(price_series) > current_hour
            else price_series
        )
        feed_series = (
            feed_series[current_hour:]
            if len(feed_series) > current_hour
            else feed_series
        )
        load_series = (
            load_series[current_hour:]
            if len(load_series) > current_hour
            else load_series
        )

        # choose horizon length (prefer smallest present length to avoid accidental growth)
        lengths = [
            len(s)
            for s in (pv_series, price_series, feed_series, load_series)
            if len(s) > 0
        ]
        n = min(lengths) if lengths else 1

        def normalize(arr):
            if not arr:
                return [0.0] * n
            if len(arr) >= n:
                return [float(x) for x in arr[:n]]
            last = float(arr[-1])
            return [float(x) for x in arr] + [last] * (n - len(arr))

        pv_ts = normalize(pv_series)  # forecast PV generation (Wh)
        price_ts = normalize(price_series)  # price per Wh (€/Wh)
        feed_ts = normalize(feed_series)  # feed-in remuneration (€/Wh)
        load_ts = normalize(load_series)  # household load (Wh)

        # battery mapping
        pv_akku = eos_request.get("pv_akku") or {}
        batt_capacity_wh = float(pv_akku.get("capacity_wh", 0))
        batt_initial_pct = float(pv_akku.get("initial_soc_percentage", 0))
        batt_min_pct = float(pv_akku.get("min_soc_percentage", 0))
        batt_max_pct = float(pv_akku.get("max_soc_percentage", 100))
        batt_c_max = float(pv_akku.get("max_charge_power_w", 0))
        batt_eta_c = float(pv_akku.get("charging_efficiency", 0.95))
        batt_eta_d = float(pv_akku.get("discharging_efficiency", 0.95))

        s_min = batt_capacity_wh * (batt_min_pct / 100.0)
        s_max = batt_capacity_wh * (batt_max_pct / 100.0)
        s_initial = batt_capacity_wh * (batt_initial_pct / 100.0)

        batteries = []
        if batt_capacity_wh > 0:
            batteries.append(
                {
                    "device_id": pv_akku.get("device_id", "battery1"),
                    "charge_from_grid": True,
                    "discharge_to_grid": bool(any(x > 0 for x in feed_ts)),
                    # "discharge_to_grid": False,
                    "s_min": s_min,
                    "s_max": s_max,
                    "s_initial": s_initial,
                    "p_demand": [0.0] * n,
                    # "s_goal": [s_max] * n,
                    "s_goal": [0.0] * n,
                    "c_min": 0.0,
                    "c_max": batt_c_max,
                    "d_max": batt_c_max,
                    "p_a": 0.0,
                }
            )
        else:
            batteries.append(
                {
                    "device_id": "battery_placeholder",
                    # "charge_from_grid": True,
                    "charge_from_grid": False,
                    "discharge_to_grid": True,
                    "s_min": 0.0,
                    "s_max": 0.0,
                    "s_initial": 0.0,
                    "p_demand": [0.0] * n,
                    "s_goal": [0.0] * n,
                    "c_min": 0.0,
                    "c_max": 0.0,
                    "d_max": 0.0,
                    "p_a": 0.0,
                }
            )

        # # optional EV
        # eauto = eos_request.get("eauto") or {}
        # if eauto:
        #     ev_capacity_wh = float(eauto.get("capacity_wh", 0))
        #     ev_initial_pct = float(eauto.get("initial_soc_percentage", 0))
        #     ev_min_pct = float(eauto.get("min_soc_percentage", 0))
        #     ev_max_pct = float(eauto.get("max_soc_percentage", 100))
        #     ev_c_max = float(eauto.get("max_charge_power_w", 0))
        #     ev_s_min = ev_capacity_wh * (ev_min_pct / 100.0)
        #     ev_s_max = ev_capacity_wh * (ev_max_pct / 100.0)
        #     ev_s_initial = ev_capacity_wh * (ev_initial_pct / 100.0)

        #     batteries.append(
        #         {
        #             "device_id": eauto.get("device_id", "ev1"),
        #             "charge_from_grid": True,
        #             "discharge_to_grid": False,
        #             "s_min": ev_s_min,
        #             "s_max": ev_s_max,
        #             "s_initial": ev_s_initial,
        #             "p_demand": [0.0] * n,
        #             "s_goal": [ev_s_initial] * n,
        #             "c_min": 0.0,
        #             "c_max": ev_c_max,
        #             "d_max": 0.0,
        #             "p_a": 0.0,
        #         }
        #     )

        # conservative grid limits
        # p_max_imp = int(max(load_ts)) if load_ts else 0
        # p_max_exp = int(max(pv_ts)) if pv_ts else 0

        p_max_imp = 10000
        p_max_exp = 10000

        # IMPORTANT: mapping fixed to match your schema:
        #   time_series.gt  <- household consumption (gesamtlast)
        #   time_series.ft  <- forecasted solar generation (pv_prognose_wh)
        #   time_series.p_N <- price per Wh from grid (strompreis_euro_pro_wh)
        #   time_series.p_E <- remuneration per Wh fed into grid (einspeiseverguetung_euro_pro_wh)
        evcc_req = {
            "strategy": {
                "charging_strategy": "charge_before_export",  # "cost",
                # "charging_strategy": "cost",
                "discharging_strategy": "discharge_before_import",  # "always",
                # "discharging_strategy": "always",
            },
            "grid": {
                "p_max_imp": p_max_imp,
                "p_max_exp": p_max_exp,
                "prc_p_imp_exc": 0,
            },
            "batteries": batteries,
            "time_series": {
                "dt": [3600.0] * n,
                "gt": [float(x) for x in load_ts],  # household consumption
                "ft": [float(x) for x in pv_ts],  # PV forecast (solar generation)
                "p_N": [float(x) for x in price_ts],  # price per Wh (€/Wh)
                "p_E": [float(x) for x in feed_ts],  # feed-in remuneration (€/Wh)
            },
            "eta_c": batt_eta_c if batt_capacity_wh > 0 else 0.95,
            "eta_d": batt_eta_d if batt_capacity_wh > 0 else 0.95,
        }

        # If external validation failed, return external errors (caller can inspect payload)
        if not ok_eos:
            logger.warning(
                "[EOS] EVCC OPT transform - external input validation failed (%d error(s))",
                len(external_errors),
            )
            for e in external_errors:
                logger.debug("[EOS] EVCC OPT transform - external error %s", e)
            return evcc_req, external_errors

        # run internal schema check and log issues, but do not return internal errors
        valid_internal, internal_errors = self._validate_evcc_request(evcc_req)
        if not valid_internal:
            logger.warning(
                "[EOS] EVCC OPT transform - internal schema check found %d issue(s) (logged only)",
                len(internal_errors),
            )
            for e in internal_errors:
                logger.debug("[EOS] EVCC OPT transform - internal error %s", e)

        # no external errors -> return empty external error list
        return evcc_req, []

    def evcc_opt_transform_response(self, evcc_resp, evcc_req=None):
        """
        Translate EVCC optimizer response -> EOS-style optimize response.

        Produces a fuller EOS-shaped response using the sample `src/json/optimize_response.json`
        as guidance. The mapping is conservative and uses available EVCC fields:

        - ac_charge, dc_charge, discharge_allowed, start_solution
        - result.* arrays: Last_Wh_pro_Stunde, EAuto_SoC_pro_Stunde,
            Einnahmen_Euro_pro_Stunde, Kosten_Euro_pro_Stunde, Netzbezug_Wh_pro_Stunde,
            Netzeinspeisung_Wh_pro_Stunde, Verluste_Pro_Stunde, akku_soc_pro_stunde,
            Electricity_price
        - numeric summaries: Gesamt_Verluste, Gesamtbilanz_Euro, Gesamteinnahmen_Euro,
            Gesamtkosten_Euro
        - eauto_obj, washingstart, timestamp

        evcc_resp: dict (raw EVCC JSON) or {"response": {...}}.
        evcc_req: optional EVCC request dict (used to read p_N, p_E, eta_c, eta_d,
            battery s_max/c_max).
        """
        # defensive guard
        if not isinstance(evcc_resp, dict):
            logger.debug(
                "[EOS] EVCC transform response - input not a dict, returning empty dict"
            )
            return {}

        # EVCC might wrap actual payload under "response"
        resp = evcc_resp.get("response", evcc_resp)

        # Set total hours and slice for future
        current_hour = datetime.now(self.time_zone).hour
        n_total = 48  # Total hours from midnight today to midnight tomorrow
        n_future = n_total - current_hour  # Hours from now to end of tomorrow
        n = n_future  # Override n to focus on future horizon

        # # determine horizon length n
        # n = 0
        # if isinstance(resp.get("grid_import"), list):
        #     n = len(resp.get("grid_import"))
        # elif isinstance(resp.get("grid_export"), list):
        #     n = len(resp.get("grid_export"))
        # else:
        #     # try batteries[*].charging_power
        #     b_list = resp.get("batteries") or []
        #     if isinstance(b_list, list) and len(b_list) > 0:
        #         b0 = b_list[0]
        #         if isinstance(b0.get("charging_power"), list):
        #             n = len(b0.get("charging_power"))
        # if n == 0:
        #     n = 24

        # primary battery arrays (first battery)
        batteries_resp = resp.get("batteries") or []
        first_batt = batteries_resp[0] if batteries_resp else {}
        charging_power = list(first_batt.get("charging_power") or [0.0] * n)[:n]
        discharging_power = list(first_batt.get("discharging_power") or [0.0] * n)[:n]
        soc_wh = list(first_batt.get("state_of_charge") or [])[:n]

        # grid arrays
        grid_import = list(resp.get("grid_import") or [0.0] * n)[:n]
        grid_export = list(resp.get("grid_export") or [0.0] * n)[:n]

        # harvest pricing from evcc_req when available (per-Wh units)
        p_n = None
        p_e = None
        electricity_price = [None] * n
        if isinstance(evcc_req, dict):
            ts = evcc_req.get("time_series", {}) or {}
            p_n = ts.get("p_N")
            p_e = ts.get("p_E")
            # if p_N/p_E are lists, normalize length
            if isinstance(p_n, list):
                p_n = (
                    [float(x) for x in p_n[:n]]
                    + [float(p_n[-1])] * max(0, n - len(p_n))
                    if p_n
                    else None
                )
            if isinstance(p_e, list):
                p_e = (
                    [float(x) for x in p_e[:n]]
                    + [float(p_e[-1])] * max(0, n - len(p_e))
                    if p_e
                    else None
                )
            if isinstance(p_n, list):
                electricity_price = [float(x) for x in p_n[:n]]
            elif isinstance(p_n, (int, float)):
                electricity_price = [float(p_n)] * n

        # fallback price arrays if missing
        if not any(isinstance(x, (int, float)) for x in electricity_price):
            electricity_price = [0.0] * n
        if p_n is None:
            p_n = electricity_price
        if p_e is None:
            p_e = [0.0] * n

        # battery parameters from request if present (s_max in Wh, eta_c, eta_d)
        s_max_req = None
        eta_c = None
        eta_d = None
        if isinstance(evcc_req, dict):
            breq = evcc_req.get("batteries")
            if isinstance(breq, list) and len(breq) > 0:
                b0r = breq[0]
                try:
                    s_max_req = float(b0r.get("s_max", 0.0))
                except (ValueError, TypeError):
                    s_max_req = None
                try:
                    eta_c = float(evcc_req.get("eta_c", b0r.get("eta_c", 0.95) or 0.95))
                except (ValueError, TypeError):
                    eta_c = 0.95
                try:
                    eta_d = float(evcc_req.get("eta_d", b0r.get("eta_d", 0.95) or 0.95))
                except (ValueError, TypeError):
                    eta_d = 0.95
        # Set defaults
        eta_c = eta_c if eta_c is not None else 0.95
        eta_d = eta_d if eta_d is not None else 0.95
        s_max_val = s_max_req if s_max_req not in (None, 0) else None

        # compute ac_charge fraction: charging_power normalized to c_max from request
        # or observed max
        c_max = None
        d_max = None
        if isinstance(evcc_req, dict):
            breq = evcc_req.get("batteries")
            if isinstance(breq, list) and len(breq) > 0:
                try:
                    c_max = float(breq[0].get("c_max", 0.0))
                except (ValueError, TypeError):
                    c_max = None
                try:
                    d_max = float(breq[0].get("d_max", 0.0))
                except (ValueError, TypeError):
                    d_max = None
        # fallback observed maxima
        try:
            if not c_max:
                observed_max_ch = (
                    max([float(x) for x in charging_power]) if charging_power else 0.0
                )
                c_max = observed_max_ch if observed_max_ch > 0 else 1.0
            if not d_max:
                observed_max_dch = (
                    max([float(x) for x in discharging_power])
                    if discharging_power
                    else 0.0
                )
                d_max = observed_max_dch if observed_max_dch > 0 else 1.0
        except (ValueError, TypeError):
            c_max = c_max or 1.0
            d_max = d_max or 1.0

        ac_charge = []
        for v in charging_power:
            try:
                frac = float(v) / float(c_max) if float(c_max) > 0 else 0.0
            except (ValueError, TypeError):
                frac = 0.0
            if frac != frac:
                frac = 0.0
            ac_charge.append(max(0.0, min(1.0, frac)))

        # Adjust ac_charge: set to 0 if no grid import (PV-only charging)
        for i in range(n):
            if grid_import[i] <= 0:
                ac_charge[i] = 0.0

        # dc_charge: mark 1.0 if charging_power > 0 (conservative)
        dc_charge = [1.0 if float(v) > 0.0 else 0.0 for v in charging_power]

        # discharge_allowed: 1 if discharging_power > tiny epsilon
        discharge_allowed = [1 if float(v) > 1e-9 else 0 for v in discharging_power]

        # start_solution: prefer resp['start_solution'] if present, else try
        # eauto_obj.charge_array -> ints, otherwise zeros
        start_solution = None
        if isinstance(resp.get("start_solution"), list):
            # coerce to numbers
            start_solution = [
                float(x) if isinstance(x, (int, float)) else 0
                for x in resp.get("start_solution")[:n]
            ]
        else:
            eauto_obj = resp.get("eauto_obj") or evcc_resp.get("eauto_obj")
            if isinstance(eauto_obj, dict) and isinstance(
                eauto_obj.get("charge_array"), list
            ):
                # map boolean/float charge_array to integers (placeholder)
                start_solution = [
                    int(1 if float(x) > 0 else 0)
                    for x in eauto_obj.get("charge_array")[:n]
                ]
        if start_solution is None:
            start_solution = [0] * n

        # washingstart if present
        washingstart = resp.get("washingstart")

        # compute per-hour costs and revenues in Euro (using €/Wh units from p_N/p_E)
        kosten_per_hour = []
        einnahmen_per_hour = []
        for i in range(n):
            gi = float(grid_import[i]) if i < len(grid_import) else 0.0
            ge = float(grid_export[i]) if i < len(grid_export) else 0.0
            pr = (
                float(p_n[i])
                if isinstance(p_n, list) and i < len(p_n)
                else float(p_n[i]) if isinstance(p_n, list) and len(p_n) > 0 else 0.0
            )
            pe = (
                float(p_e[i])
                if isinstance(p_e, list) and i < len(p_e)
                else (float(p_e[i]) if isinstance(p_e, list) and len(p_e) > 0 else 0.0)
            )
            # if p_N/p_E are scalars (should be lists), handle above; fallback zero if missing
            if isinstance(p_n, (int, float)):
                pr = float(p_n)
            if isinstance(p_e, (int, float)):
                pe = float(p_e)

            kosten = gi * pr
            einnahmen = ge * pe
            kosten_per_hour.append(kosten)
            einnahmen_per_hour.append(einnahmen)

        # estimate per-hour battery losses: charging_loss + discharging_loss
        verluste_per_hour = []
        for i in range(n):
            ch = float(charging_power[i]) if i < len(charging_power) else 0.0
            dch = float(discharging_power[i]) if i < len(discharging_power) else 0.0
            loss = ch * (1.0 - eta_c) + dch * (1.0 - eta_d)
            verluste_per_hour.append(loss)

        # Akku SoC percent per hour (if soc_wh available): convert to percent using s_max_req
        # or inferred max
        akku_soc_pct = []
        if soc_wh:
            # determine s_max reference: use s_max_req if provided, otherwise attempt to
            # infer from soc_wh max
            ref = s_max_val
            if not ref:
                try:
                    ref = max([float(x) for x in soc_wh]) if soc_wh else None
                except (ValueError, TypeError):
                    ref = None
            for v in soc_wh:
                try:
                    if ref and ref > 0:
                        pct = float(v) / float(ref) * 100.0
                    else:
                        pct = float(v)
                except (ValueError, TypeError):
                    pct = 0.0
                akku_soc_pct.append(pct)
        else:
            akku_soc_pct = []

        # totals
        gesamt_kosten = sum(kosten_per_hour) if kosten_per_hour else 0.0
        gesamt_einnahmen = sum(einnahmen_per_hour) if einnahmen_per_hour else 0.0
        gesamt_verluste = sum(verluste_per_hour) if verluste_per_hour else 0.0
        gesamt_bilanz = gesamt_einnahmen - gesamt_kosten

        # build result dict like optimize_response.json
        result = {}
        # Prefer household load ('gt') from the EVCC request if available,
        # otherwise fall back to EVCC response grid_import (parity with previous behavior).
        last_wh = None
        if isinstance(evcc_req, dict):
            ts = evcc_req.get("time_series", {}) or {}
            gt = ts.get("gt")
            if isinstance(gt, list) and len(gt) > 0:
                # normalize/trim/pad gt to length n (similar to other normalizations)
                if len(gt) >= n:
                    last_wh = [float(x) for x in gt[:n]]
                else:
                    last_val = float(gt[-1])
                    last_wh = [float(x) for x in gt] + [last_val] * (n - len(gt))
        # fallback to grid_import if gt not present or invalid
        if last_wh is None:
            last_wh = [float(x) for x in grid_import[:n]]

        result["Last_Wh_pro_Stunde"] = last_wh

        if akku_soc_pct:
            # EAuto_SoC_pro_Stunde - fallback to eauto object SOC or same as akku
            # percent if appropriate
            result["EAuto_SoC_pro_Stunde"] = (
                [
                    float(x)
                    for x in (
                        evcc_resp.get("eauto_obj", {}).get("soc_wh")
                        or (
                            []
                            if not isinstance(evcc_resp.get("eauto_obj", {}), dict)
                            else []
                        )
                    )[:n]
                ]
                if evcc_resp.get("eauto_obj")
                else []
            )
        # Einnahmen & Kosten per hour
        result["Einnahmen_Euro_pro_Stunde"] = [float(x) for x in einnahmen_per_hour]
        result["Kosten_Euro_pro_Stunde"] = [float(x) for x in kosten_per_hour]
        result["Gesamt_Verluste"] = float(gesamt_verluste)
        result["Gesamtbilanz_Euro"] = float(gesamt_bilanz)
        result["Gesamteinnahmen_Euro"] = float(gesamt_einnahmen)
        result["Gesamtkosten_Euro"] = float(gesamt_kosten)
        # Home appliance placeholder (zeros)
        result["Home_appliance_wh_per_hour"] = [0.0] * n
        result["Netzbezug_Wh_pro_Stunde"] = [float(x) for x in grid_import[:n]]
        result["Netzeinspeisung_Wh_pro_Stunde"] = [float(x) for x in grid_export[:n]]
        result["Verluste_Pro_Stunde"] = [float(x) for x in verluste_per_hour]
        if akku_soc_pct:
            result["akku_soc_pro_stunde"] = [float(x) for x in akku_soc_pct[:n]]
        # Electricity price array
        result["Electricity_price"] = [float(x) for x in electricity_price[:n]]

        # Pad past hours with zeros for control arrays
        pad_past = [0.0] * current_hour

        eos_resp = {
            "ac_charge": pad_past + [float(x) for x in ac_charge],
            "dc_charge": pad_past + [float(x) for x in dc_charge],
            "discharge_allowed": pad_past + [int(x) for x in discharge_allowed],
            "eautocharge_hours_float": None,
            "result": result,  # result arrays remain unpadded (start from current time)
        }

        # attach eauto_obj if present in resp
        if "eauto_obj" in resp:
            eos_resp["eauto_obj"] = resp.get("eauto_obj")

        # map start_solution and washingstart if present
        eos_resp["start_solution"] = pad_past + start_solution
        if washingstart is not None:
            eos_resp["washingstart"] = pad_past + washingstart

        # timestamp
        try:
            eos_resp["timestamp"] = datetime.now(self.time_zone).isoformat()
        except (ValueError, TypeError):
            eos_resp["timestamp"] = datetime.now().isoformat()

        return eos_resp
