"""
This module provides the EOSBackend class for interacting with the EOS optimization server.
It includes methods for sending optimization requests, managing configuration, and handling
measurement data.
"""

import logging
import sys
import time
import json
from datetime import datetime
import requests
import pandas as pd
import numpy as np

logger = logging.getLogger("__main__")


class EOSBackend:
    """
    Backend for direct EOS server optimization.
    Accepts and returns EOS-format requests/responses.
    """

    def __init__(self, base_url, time_frame_base, time_zone):
        self.base_url = base_url
        self.time_frame_base = time_frame_base
        self.time_zone = time_zone
        self.last_optimization_runtimes = [0] * 5
        self.last_optimization_runtime_number = 0
        self.eos_version = ">=2025-04-09"  # default
        try:
            self.eos_version = self._retrieve_eos_version()
            if self.eos_version in ["0.1.0+dev", "0.2.0+dev"]:
                # check config for needed values
                config_optimization = self.__get_config_path("optimization")
                config_optimization_upodate_needed = False
                if config_optimization.get("horizon_hours", 0) != 48:
                    config_optimization["horizon_hours"] = 48
                    config_optimization_upodate_needed = True
                if config_optimization.get("genetic", None) is None:
                    config_optimization["genetic"] = {
                        "individuals": 300,
                        "generations": 400,
                    }
                    config_optimization_upodate_needed = True
                if config_optimization_upodate_needed:
                    self.__set_config_path("optimization", config_optimization)
                    logger.warning(
                        "[EOS] Detected EOS version 0.1.0+dev or above - config updated with "
                        + ": %s",
                        config_optimization,
                    )
                else:
                    logger.info(
                        "[EOS] Detected EOS version 0.1.0+dev or above - config optimization values OK"
                    )

                config_devices = self.__get_config_path("devices/electric_vehicles")
                if config_devices is None:
                    # if config_devices[0].get("charge_rates", None) is None:
                    config_devices = [{}]
                    config_devices[0]["charge_rates"] = sorted(
                        [0.0, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]
                    )
                    self.__set_config_path("devices/electric_vehicles", config_devices)
                    logger.warning(
                        "[EOS] Detected EOS version 0.1.0+dev - config updated with charge "
                        + "rates for electric vehicles"
                    )
                elif "charge_rates" not in config_devices[0]:
                    config_devices[0]["charge_rates"] = sorted(
                        [0.0, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]
                    )
                    self.__set_config_path("devices/electric_vehicles", config_devices)
                    logger.warning(
                        "[EOS] Detected EOS version 0.1.0+dev - config updated with charge "
                        + "rates for electric vehicles"
                    )
                else:
                    logger.info(
                        "[EOS] Detected EOS version 0.1.0+dev - config charge rates for "
                        + "electric vehicles OK"
                    )
            logger.info("[EOS] Configuration validation successful")
        except ValueError as e:
            logger.error("[EOS] EOS backend configuration error: %s", str(e))
            logger.error("[EOS] We have to exit now ...")
            sys.exit(1)  # Exit if configuration is invalid

    def optimize(self, eos_request, timeout=180):
        """
        Send the optimize request to the EOS server.
        Returns (response_json, avg_runtime)
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
        response = None
        try:
            start_time = time.time()
            response = requests.post(
                request_url, headers=headers, json=eos_request, timeout=timeout
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
            # Store runtime in circular list
            if all(runtime == 0 for runtime in self.last_optimization_runtimes):
                self.last_optimization_runtimes = [elapsed_time] * 5
            else:
                self.last_optimization_runtimes[
                    self.last_optimization_runtime_number
                ] = elapsed_time
            self.last_optimization_runtime_number = (
                self.last_optimization_runtime_number + 1
            ) % 5
            avg_runtime = sum(self.last_optimization_runtimes) / 5
            return response.json(), avg_runtime
        except requests.exceptions.Timeout:
            logger.error("[EOS] OPTIMIZE Request timed out after %s seconds", timeout)
            return {"error": "Request timed out - trying again with next run"}, None
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[EOS] OPTIMIZE Connection error - EOS server not reachable at %s "
                "will try again with next cycle - error: %s",
                request_url,
                str(e),
            )
            return {
                "error": f"EOS server not reachable at {self.base_url} "
                "will try again with next cycle"
            }, None
        except requests.exceptions.RequestException as e:
            logger.error("[EOS] OPTIMIZE Request failed: %s", e)
            if response is not None:
                logger.error("[EOS] OPTIMIZE Response status: %s", response.status_code)
                logger.debug(
                    "[EOS] OPTIMIZE ERROR - response of EOS is:\n%s",
                    response.text,
                )
            logger.debug(
                "[EOS] OPTIMIZE ERROR - payload for the request was:\n%s",
                eos_request,
            )
            return {"error": str(e)}, None

    def __get_config_path(self, path):
        """
        Get a configuration value from the EOS server.
        """
        # Always specify a timeout to avoid hanging indefinitely
        response = requests.get(self.base_url + "/v1/config/" + path, timeout=10)
        response.raise_for_status()
        config_value = response.json()
        return config_value

    def __set_config_path(self, path, value):
        """
        Set a configuration value on the EOS server.

        Args:
            path (str): The configuration path.
            value (dict or list): The configuration value as a JSON-serializable
            object or a list of such objects.
        """

        def convert_sets(obj):
            """Recursively convert sets to lists in a dict or list."""
            if isinstance(obj, dict):
                return {k: convert_sets(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_sets(v) for v in obj]
            elif isinstance(obj, set):
                return list(obj)
            else:
                return obj

        headers = {"Content-Type": "application/json"}
        try:
            value_serializable = convert_sets(value)
            response = requests.put(
                self.base_url + "/v1/config/" + path,
                data=json.dumps(value_serializable),
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error("[EOS] Failed to set config value for path '%s': %s", path, e)

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
                "[EOS] Failed to send data to EOS server. Status code: %s, Response: %s",
                response.status_code,
                response.text,
            )

    def save_config_to_config_file(self):
        """
        Save the current configuration to the configuration file on the EOS server.
        """
        response = requests.put(self.base_url + "/v1/config/file", timeout=10)
        response.raise_for_status()
        logger.debug("[EOS] CONFIG saved to config file successfully.")

    def update_config_from_config_file(self):
        """
        Update the current configuration from the configuration file on the EOS server.
        """
        try:
            response = requests.post(self.base_url + "/v1/config/update", timeout=10)
            response.raise_for_status()
            logger.info("[EOS] CONFIG updated from config file successfully.")
        except requests.exceptions.Timeout:
            logger.error(
                "[EOS] CONFIG Request timed out while updating config from config file."
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EOS] CONFIG Request failed while updating config from config file: %s",
                e,
            )

    def _retrieve_eos_version(self):
        """
        Get the EOS version from the server.
        Returns: str
        """
        try:
            response = requests.get(self.base_url + "/v1/health", timeout=10)
            response.raise_for_status()
            eos_version = response.json().get("status")
            eos_version_real = response.json().get("version", "unknown")
            if eos_version == "alive" and eos_version_real == "unknown":
                eos_version = ">=2025-04-09"
            else:
                eos_version = eos_version_real
                # raise ValueError(
                #     f"EOS version {eos_version_real} currently not supported!"
                # )
            logger.info("[EOS] Getting EOS version: %s", eos_version)
            return eos_version
        except requests.exceptions.HTTPError as e:
            if hasattr(e, "response") and e.response and e.response.status_code == 404:
                eos_version = "<2025-04-09"
                logger.info("[EOS] Getting EOS version: %s", eos_version)
                return eos_version
            else:
                logger.error(
                    "[EOS] HTTP error occurred while getting EOS version - use preset version:"
                    + " %s : %s - Response: %s",
                    self.eos_version,
                    e,
                    (
                        e.response.text
                        if hasattr(e, "response") and e.response
                        else "No response"
                    ),
                )
                return self.eos_version
        except requests.exceptions.ConnectTimeout:
            logger.error(
                "[EOS] Failed to get EOS version  - use preset version: '%s' - Server not "
                + "reachable: Connection to %s timed out",
                self.eos_version,
                self.base_url,
            )
            return self.eos_version
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[EOS] Failed to get EOS version - use preset version: '%s' - Connection error: %s",
                self.eos_version,
                e,
            )
            return self.eos_version
        except requests.exceptions.RequestException as e:
            logger.error(
                "[EOS] Failed to get EOS version - use preset version: '%s' - Error: %s ",
                self.eos_version,
                e,
            )
            return self.eos_version
        except json.JSONDecodeError as e:
            logger.error(
                "[EOS] Failed to decode EOS version - use preset version: '%s' - response: %s ",
                self.eos_version,
                e,
            )
            return self.eos_version

    def get_eos_version(self):
        """
        Get the EOS version from the server.
        Returns: str
        """
        return self.eos_version

    def create_dataframe(self, profile):
        """
        Creates a pandas DataFrame with hourly energy values for a given profile.
        Args:
            profile (list of tuples): Each tuple: (month, weekday, hour, energy)
        Returns:
            pandas.DataFrame: DateTime index for 2025, 'Household' column.
        """
        dates = pd.date_range(start="1/1/2025", end="31/12/2025", freq="H")
        df = pd.DataFrame(index=dates)
        df["Household"] = np.nan
        for entry in profile:
            month, weekday, hour, energy = entry
            matching_dates = df[
                (df.index.month == month)
                & (df.index.weekday == weekday)
                & (df.index.hour == hour)
            ].index
            for date in matching_dates:
                df.loc[date, "Household"] = energy
        return df

    def _validate_eos_input(self, eos_request):
        """
        Validate EOS-format optimization request.
        Returns: (bool, list[str]) - valid, errors
        """
        errors = []
        if not isinstance(eos_request, dict):
            errors.append("Request must be a dictionary.")
        # Add more checks as needed
        # Example: check required keys
        required_keys = ["ems", "pv_akku"]
        for key in required_keys:
            if key not in eos_request:
                errors.append(f"Missing required key: {key}")
        return len(errors) == 0, errors
