"""
This module provides the EOSBackend class for interacting with the EOS optimization server.
It includes methods for sending optimization requests, managing configuration, and handling
measurement data.
"""

import logging
import time
import json
from datetime import datetime, timedelta
import requests
import pandas as pd
import numpy as np
from packaging import version
import pytz

logger = logging.getLogger("__main__")

# Charge rates EOS_connect pushes into an EOS server that has none of its own,
# as a factor of the maximum charging power.
DEFAULT_EV_CHARGE_RATES = sorted([0.0, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0])

# The EOS device rework keys ``devices/*`` by device_id instead of listing them,
# and fills in its own charge rates. It shipped in 0.3.0 dev builds, and PEP 440
# sorts those *before* 0.3.0 - comparing against the plain release would miss
# every build that actually carries the change.
EOS_DEVICE_MAPPING_VERSION = "0.3.0.dev0"


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
        self.eos_version = "0.0.2"  # default
        try:
            self.eos_version = self._retrieve_eos_version()
            # if self.eos_version in ["0.1.0+dev", "0.2.0+dev"]:
            if self.is_eos_version_at_least("0.1.0"):
                # check config for needed values
                config_optimization = self.__get_config_path("optimization")
                config_optimization_upodate_needed = False
                horizon_updated = False
                # Newer EOS builds keep the horizon per algorithm, under
                # optimization.genetic (or genetic0), and no longer read the
                # top-level key: writing only that one left them at their default
                # of 24 hours. Set the nested key on its own path, so the computed
                # fields of the optimization object are not sent back with it.
                algorithm_section = str(
                    config_optimization.get("algorithm", "GENETIC")
                ).lower()
                algorithm_config = config_optimization.get(algorithm_section)
                if (
                    isinstance(algorithm_config, dict)
                    and "horizon_hours" in algorithm_config
                ):
                    if algorithm_config["horizon_hours"] != 48:
                        horizon_updated = self.__set_config_path(
                            f"optimization/{algorithm_section}/horizon_hours", 48
                        )
                elif config_optimization.get("horizon_hours", 0) != 48:
                    config_optimization["horizon_hours"] = 48
                    config_optimization_upodate_needed = True
                if config_optimization.get("genetic", None) is None:
                    config_optimization["genetic"] = {
                        "individuals": 300,
                        "generations": 400,
                    }
                    config_optimization_upodate_needed = True
                if config_optimization_upodate_needed:
                    if self.__set_config_path("optimization", config_optimization):
                        logger.warning(
                            "[OPT-EOS] Detected EOS version >= 0.1.0 - config updated with "
                            + ": %s",
                            config_optimization,
                        )
                elif horizon_updated:
                    logger.warning(
                        "[OPT-EOS] Detected EOS version >= 0.1.0 - set "
                        + "optimization.%s.horizon_hours to 48",
                        algorithm_section,
                    )
                else:
                    logger.info(
                        "[OPT-EOS] Detected EOS version >= 0.1.0 - config optimization values OK"
                    )

                self.__ensure_ev_charge_rates()
            logger.info("[OPT-EOS] Configuration validation successful")
        except requests.exceptions.RequestException as e:
            self.__report_config_check_failed(e)
        except ValueError as e:
            logger.error("[OPT-EOS] EOS backend configuration error: %s", str(e))
            logger.error(
                "[OPT-EOS] EOS backend will start in degraded mode - "
                "Use Settings to fix configuration"
            )
            raise  # Let caller handle graceful degradation instead of sys.exit(1)
        except (KeyError, TypeError, IndexError, AttributeError) as e:
            # An EOS server whose config does not look the way we expect is not a
            # reason to refuse to start: this interface is created with
            # critical=True, long before the web server comes up, so raising here
            # takes the settings UI down with it - and the settings UI is exactly
            # what the user needs to get out of the situation (#349).
            self.__report_config_check_failed(e)

    def __report_config_check_failed(self, error):
        """
        Log that the EOS server configuration could not be checked, and carry on.

        The optimizer still works against whatever the server has configured; a
        run that then fails reports itself per cycle, which leaves the add-on
        running and the settings reachable.
        """
        logger.error(
            "[OPT-EOS] Could not check the configuration of the EOS server at %s: "
            "%s: %s | Config: #eos | ACTION REQUIRED",
            self.base_url,
            type(error).__name__,
            error,
        )
        logger.warning(
            "[OPT-EOS] Continuing with the configuration the EOS server already has - "
            "optimization may fail until it is corrected"
        )

    def __ensure_ev_charge_rates(self):
        """
        Make sure the electric vehicles on the EOS server carry charge rates.

        ``devices/electric_vehicles`` changed shape with the EOS device rework: it
        used to be a list of device dicts and is now a mapping keyed by device_id.
        Both are read here and written back the way the server handed them over.
        Indexing ``[0]`` into the mapping is what stopped the add-on from starting
        against a current EOS build (#349).
        """
        config_devices = self.__get_config_path("devices/electric_vehicles")

        if config_devices is None:
            if self.is_eos_version_at_least(EOS_DEVICE_MAPPING_VERSION):
                # Mapping-era servers default the charge rates themselves, and a
                # list pushed into a mapping field would only earn a 422.
                logger.info(
                    "[OPT-EOS] No electric vehicles configured - EOS supplies its own "
                    "charge rates"
                )
                return
            config_devices = [{"charge_rates": list(DEFAULT_EV_CHARGE_RATES)}]
            self.__set_config_path("devices/electric_vehicles", config_devices)
            logger.warning(
                "[OPT-EOS] Detected EOS version >= 0.1.0 - config updated with charge "
                + "rates for electric vehicles"
            )
            return

        devices = (
            list(config_devices.values())
            if isinstance(config_devices, dict)
            else list(config_devices)
        )
        # The dicts are the ones inside config_devices, so filling them in here is
        # what gets written back below - in the original shape, whichever it was.
        without_rates = [
            device
            for device in devices
            if isinstance(device, dict) and "charge_rates" not in device
        ]
        if not without_rates:
            logger.info(
                "[OPT-EOS] Detected EOS version >= 0.1.0 - config charge rates for "
                + "electric vehicles OK"
            )
            return

        for device in without_rates:
            device["charge_rates"] = list(DEFAULT_EV_CHARGE_RATES)
        self.__set_config_path("devices/electric_vehicles", config_devices)
        logger.warning(
            "[OPT-EOS] Detected EOS version >= 0.1.0 - config updated with charge "
            + "rates for electric vehicles"
        )

    def _get_expected_hourly_slots(self):
        """
        Returns the expected number of hourly slots for the 2-day window
        (today midnight to day-after-tomorrow midnight) that the EOS server
        validates arrays against.  Handles DST:
        - Normal day   : 48
        - Spring-forward: 47 (today = 23 h)
        - Fall-back     : 49 (today = 25 h)
        """
        tz = self.time_zone
        if isinstance(tz, str):
            tz = pytz.timezone(tz)
        today = datetime.now(tz).date()
        today_midnight = tz.localize(
            datetime(today.year, today.month, today.day, 0, 0, 0)
        )
        day_after = today + timedelta(days=2)
        day_after_midnight = tz.localize(
            datetime(day_after.year, day_after.month, day_after.day, 0, 0, 0)
        )
        return int((day_after_midnight - today_midnight).total_seconds()) // 3600

    def _adjust_arrays_for_dst(self, eos_request, expected_hourly):
        """
        Trim or pad all time-series arrays inside eos_request so every
        array has exactly *expected_hourly* elements (or expected_hourly * 4
        for 15-min mode).  Sources always produce 48/192 slots; on DST days
        old EOS servers (< 0.1.0) computed a DST-aware required slot count
        (47 on spring-forward, 49 on fall-back) and rejected mismatched arrays.

        For EOS >= 0.1.0, ``horizon_hours`` is explicitly set to 48 during
        initialisation.  Those server versions always expect exactly 48 input
        elements and handle DST internally.  Trimming or padding the arrays for
        those versions causes the optimizer to receive incomplete data and return
        ``"Optimize error: no solution stored by run."``.  We therefore skip the
        adjustment entirely for modern EOS builds.

        The adjustment is done in-place on eos_request.
        """
        if expected_hourly == 48:
            return  # Normal day – nothing to do

        # EOS >= 0.1.0: horizon_hours is configured to 48 by __init__.
        # These versions always require exactly 48 elements regardless of DST.
        if self.is_eos_version_at_least("0.1.0"):
            logger.debug(
                "[OPT-EOS] DST day (%d wall-clock hours) – skipping array adjustment "
                "because EOS >= 0.1.0 uses a fixed horizon_hours=48 and handles DST "
                "internally. Sending standard 48-slot arrays.",
                expected_hourly,
            )
            return

        expected_slots = (
            expected_hourly * 4 if self.time_frame_base == 900 else expected_hourly
        )

        def _fit(arr, fill_val=0.0):
            """Return arr trimmed or padded to expected_slots."""
            if not arr:
                return arr
            if len(arr) > expected_slots:
                return arr[:expected_slots]
            if len(arr) < expected_slots:
                pad = arr[-1] if arr else fill_val
                return arr + [pad] * (expected_slots - len(arr))
            return arr

        direction = "trimmed" if expected_hourly < 48 else "extended"
        logger.info(
            "[OPT-EOS] DST day detected: expected %d hourly slots (%d total slots). "
            "Arrays will be %s from standard 48-slot baseline.",
            expected_hourly,
            expected_slots,
            direction,
        )

        ems = eos_request.get("ems", {})
        for key in (
            "pv_prognose_wh",
            "strompreis_euro_pro_wh",
            "einspeiseverguetung_euro_pro_wh",
            "gesamtlast",
        ):
            if key in ems:
                ems[key] = _fit(ems[key])

        if "temperature_forecast" in eos_request:
            eos_request["temperature_forecast"] = _fit(
                eos_request["temperature_forecast"]
            )

    def optimize(self, eos_request, timeout=180):
        """
        Send the optimize request to the EOS server.
        Returns (response_json, avg_runtime)
        """
        # Adjust all arrays for DST before sending to EOS server.
        # Sources always produce 48 (hourly) or 192 (15-min) slots.
        # For old EOS (< 0.1.0), the server computed a DST-aware expected
        # slot count (47 on spring-forward, 49 on fall-back) and rejected
        # mismatched arrays.  _adjust_arrays_for_dst handles this trimming.
        # For EOS >= 0.1.0, horizon_hours is fixed at 48 and the server
        # handles DST internally, so _adjust_arrays_for_dst is a no-op.
        expected_hourly = self._get_expected_hourly_slots()
        self._adjust_arrays_for_dst(eos_request, expected_hourly)

        headers = {"accept": "application/json", "Content-Type": "application/json"}
        # start_hour is the EOS server's wire contract and means the wall-clock
        # hour — deliberately NOT the absolute slot index the rest of EOS
        # Connect counts in. Together with _adjust_arrays_for_dst above, this
        # pair is the only place allowed to know a local day can be 23 or 25
        # hours: it translates the internal fixed 48-slot grid into what an
        # EOS < 0.1.0 server expects.
        #
        # KNOWN GAP, deliberately not "fixed" here: the arrays sent alongside
        # are anchored on true local midnight, so on a DST day their index and
        # this wall-clock hour disagree by one — at 15:00 on the 25-hour day
        # the current slot is 16 while start_hour says 15. Making them agree
        # means changing what an EXTERNAL server is told, and the server's own
        # reading of start_hour cannot be verified from this side; getting it
        # wrong would shift the whole plan on every day of the year rather than
        # two. Left as the server's documented contract. Revisit only with a
        # real EOS instance to test against.
        request_url = (
            self.base_url
            + "/optimize"
            + "?start_hour="
            + str(datetime.now(self.time_zone).hour)
        )
        logger.info(
            "[OPT-EOS] OPTIMIZE request optimization with: %s - and with timeout: %s",
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
                "[OPT-EOS] OPTIMIZE response retrieved successfully "
                "in %d min %.2f sec for current run",
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
            logger.error(
                "[OPT-EOS] OPTIMIZE Request timed out after %s seconds | "
                "Config: #eos | ACTION REQUIRED",
                timeout,
            )
            return {"error": "Request timed out - trying again with next run"}, None
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[OPT-EOS] OPTIMIZE Connection error - EOS server not reachable at %s "
                "will try again with next cycle - error: %s | Config: #eos | ACTION REQUIRED",
                request_url,
                str(e),
            )
            return {
                "error": f"EOS server not reachable at {self.base_url} "
                "will try again with next cycle"
            }, None
        except requests.exceptions.RequestException as e:
            logger.error("[OPT-EOS] OPTIMIZE Request failed: %s", e)
            if response is not None:
                logger.error(
                    "[OPT-EOS] OPTIMIZE Response status: %s", response.status_code
                )
                logger.debug(
                    "[OPT-EOS] OPTIMIZE ERROR - response of EOS is:\n%s",
                    response.text,
                )
            logger.debug(
                "[OPT-EOS] OPTIMIZE ERROR - payload for the request was:\n%s",
                eos_request,
            )
            # The exception text is logged above but not returned: it carries the
            # resolved host, port and any proxy in the chain, and this dict is served
            # verbatim by GET /json/optimize_response.json.
            return {
                "error": f"Request to the EOS server at {self.base_url} failed "
                "- see the log for details, will try again with next cycle"
            }, None

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

        Returns:
            bool: True if EOS accepted the value.
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
            logger.error(
                "[OPT-EOS] Failed to set config value for path '%s': %s", path, e
            )
            return False
        return True

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
            logger.debug("[OPT-EOS] Measurement data sent to EOS server successfully.")
        else:
            logger.debug(
                "[OPT-EOS] Failed to send data to EOS server. Status code: %s, Response: %s",
                response.status_code,
                response.text,
            )

    def save_config_to_config_file(self):
        """
        Save the current configuration to the configuration file on the EOS server.
        """
        response = requests.put(self.base_url + "/v1/config/file", timeout=10)
        response.raise_for_status()
        logger.debug("[OPT-EOS] CONFIG saved to config file successfully.")

    def update_config_from_config_file(self):
        """
        Update the current configuration from the configuration file on the EOS server.
        """
        try:
            response = requests.post(self.base_url + "/v1/config/update", timeout=10)
            response.raise_for_status()
            logger.info("[OPT-EOS] CONFIG updated from config file successfully.")
        except requests.exceptions.Timeout:
            logger.error(
                "[OPT-EOS] CONFIG Request timed out while updating config from config file."
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "[OPT-EOS] CONFIG Request failed while updating config from config file: %s",
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
                eos_version = "0.0.2"
            else:
                eos_version = eos_version_real
                # raise ValueError(
                #     f"EOS version {eos_version_real} currently not supported!"
                # )
            logger.info("[OPT-EOS] Getting EOS version: %s", eos_version)
            return eos_version
        except requests.exceptions.HTTPError as e:
            if hasattr(e, "response") and e.response and e.response.status_code == 404:
                eos_version = "0.0.1"
                logger.info("[OPT-EOS] Getting EOS version: %s", eos_version)
                return eos_version
            else:
                logger.error(
                    "[OPT-EOS] HTTP error occurred while getting EOS version - use preset version:"
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
                "[OPT-EOS] Failed to get EOS version  - use preset version: '%s' - Server not "
                + "reachable: Connection to %s timed out | Config: #eos | ACTION REQUIRED",
                self.eos_version,
                self.base_url,
            )
            return self.eos_version
        except requests.exceptions.ConnectionError as e:
            logger.error(
                "[OPT-EOS] Failed to get EOS version - use preset version: '%s' - "
                "Connection error: %s | Config: #eos | ACTION REQUIRED",
                self.eos_version,
                e,
            )
            return self.eos_version
        except requests.exceptions.RequestException as e:
            logger.error(
                "[OPT-EOS] Failed to get EOS version - use preset version: '%s' - "
                "Error: %s | Config: #eos | ACTION REQUIRED",
                self.eos_version,
                e,
            )
            return self.eos_version
        except json.JSONDecodeError as e:
            logger.error(
                "[OPT-EOS] Failed to decode EOS version - use preset version: '%s' - response: %s ",
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

    def is_eos_version_at_least(self, version_string):
        """
        Check if the EOS version is at least the given version.
        Args:
            version_string (str): Version string to compare against.
        Returns: bool
        """
        try:
            return version.parse(self.eos_version) >= version.parse(version_string)
        except ImportError:
            logger.warning(
                "[OPT-EOS] 'packaging' module not found. Cannot compare EOS versions."
            )
            return False
        except version.InvalidVersion:
            logger.warning(
                "[OPT-EOS] Invalid EOS version string '%s'. "
                "Cannot compare versions. Assuming version < %s",
                self.eos_version,
                version_string,
            )
            return False

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
