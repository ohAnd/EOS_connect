"""
This module provides the EOSBackend class for interacting with the EOS optimization server.
It includes methods for sending optimization requests, managing configuration, and handling
measurement data.
"""

import logging
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

    def __init__(self, base_url, time_zone):
        self.base_url = base_url
        self.time_zone = time_zone
        self.last_optimization_runtimes = [0] * 5
        self.last_optimization_runtime_number = 0
        self.eos_version = ">=2025-04-09"  # default
        self.eos_version = self._retrieve_eos_version()

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
            "[EOS] Config value set successfully. Key: %s => Value: %s", key, value
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
            if eos_version == "alive":
                eos_version = ">=2025-04-09"
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
