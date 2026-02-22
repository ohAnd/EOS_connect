"""
This module provides the `LoadInterface` class, which is used to fetch and process energy data
from various sources such as OpenHAB and Home Assistant. It also includes methods to create
load profiles based on historical energy consumption data.
"""

from datetime import datetime, timedelta, timezone
import logging
from urllib.parse import quote
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import random
import requests
import pytz


logger = logging.getLogger("__main__")
logger.info("[LOAD-IF] loading module ")


class LoadInterface:
    """
    LoadInterface class provides methods to fetch and process energy data from various sources
    such as OpenHAB and Home Assistant. It also supports creating load profiles based on the
    retrieved energy data.
    """

    def __init__(
        self,
        config,
        time_frame_base,
        tz_name=None,  # Changed default to None
        request_timeout=10,  # Default timeout for API requests
    ):
        self.src = config.get("source", "")
        self.url = config.get("url", "")
        self.load_sensor = config.get("load_sensor", "")
        self.car_charge_load_sensor = config.get("car_charge_load_sensor", "")
        self.additional_load_1_sensor = config.get("additional_load_1_sensor", "")
        self.access_token = config.get("access_token", "")

        # retry config
        self.max_retries = config.get("max_retries", 5)
        self.retry_backoff = config.get("retry_backoff", 1)  # base seconds for backoff
        # optional warning threshold (when to escalate to error)
        self.warning_threshold = config.get(
            "warning_threshold", max(1, self.max_retries - 1)
        )
        self.time_frame_base = time_frame_base
        self.time_zone = None
        self.request_timeout = request_timeout  # Store configurable timeout

        logger.debug("[LOAD-IF] Initializing LoadInterface with source: %s", self.src)
        logger.debug("[LOAD-IF] Using URL: %s", self.url)
        logger.debug("[LOAD-IF] Using access token: %s", self.access_token)

        # Handle timezone properly
        if tz_name == "UTC" or tz_name is None:
            self.time_zone = None  # Use local timezone
        elif isinstance(tz_name, str):
            # Try to convert string timezone to proper timezone object
            try:
                # zoneinfo.ZoneInfo may raise ZoneInfoNotFoundError
                self.time_zone = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                # fallback to pytz if available, otherwise use local (None)
                try:
                    self.time_zone = pytz.timezone(tz_name)
                except pytz.UnknownTimeZoneError:
                    logger.warning(
                        "[LOAD-IF] Cannot parse timezone '%s', using local time",
                        tz_name,
                    )
                    self.time_zone = None

        self.__check_config()

    def __check_config(self):
        """
        Checks if the configuration is valid.
        Returns:
            bool: True if the configuration is valid, False otherwise.
        """
        if self.src not in ["openhab", "homeassistant", "default"]:
            logger.error(
                "[LOAD-IF] Invalid source '%s' configured. Using default.", self.src
            )
            self.src = "default"
            return False
        if self.src != "default":
            if self.url == "":
                logger.error(
                    "[LOAD-IF] Source '%s' selected, but URL not configured. Using default.",
                    self.src,
                )
                self.src = "default"
                return False
            if self.access_token == "" and self.src == "homeassistant":
                logger.error(
                    "[LOAD-IF] Source '%s' selected, but access_token not configured."
                    + " Using default.",
                    self.src,
                )
                self.src = "default"
                return False
            if self.load_sensor == "":
                logger.error("[LOAD-IF] Load sensor not configured. Using default.")
                self.src = "default"
                return False
            logger.debug("[LOAD-IF] Config check successful using '%s'", self.src)
        else:
            logger.debug("[LOAD-IF] Using default load profile.")
        return True

    def __log_request_failure(self, url, attempt, max_retries, error, item_label=""):
        """
        Centralized logging for request failures.
        Logs a warning for intermediate failed attempts and an error when all attempts exhausted.
        """
        # Only log warning for the pre-last attempt, error for the last
        if attempt == max_retries - 1:
            logger.warning(
                "[LOAD-IF] Request attempt %d/%d failed for %s %s: %s",
                attempt,
                max_retries,
                url,
                f"({item_label})" if item_label else "",
                str(error),
            )
        elif attempt == max_retries:
            logger.error(
                "[LOAD-IF] Request failed after %d attempts for %s %s: %s",
                max_retries,
                url,
                f"({item_label})" if item_label else "",
                str(error),
            )
        else:
            logger.debug(
                "[LOAD-IF] Request attempt %d/%d failed for %s %s: %s",
                attempt,
                max_retries,
                url,
                f"({item_label})" if item_label else "",
                str(error),
            )

    def __request_with_retries(
        self, method, url, params=None, headers=None, timeout=None, item_label=""
    ):
        """
        Perform an HTTP request with retries and exponential backoff.
        Returns the requests.Response on success, or None on final failure.
        """
        # Use instance timeout if not explicitly provided
        if timeout is None:
            timeout = self.request_timeout

        attempt = 0
        while attempt < self.max_retries:
            attempt += 1
            try:
                if method.lower() == "get":
                    response = requests.get(
                        url, params=params, headers=headers, timeout=timeout
                    )
                else:
                    response = requests.request(
                        method, url, params=params, headers=headers, timeout=timeout
                    )
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                self.__log_request_failure(
                    url, attempt, self.max_retries, e, item_label
                )
                if attempt == self.max_retries:
                    return None
                sleep_seconds = self.retry_backoff * (2 ** (attempt - 1))
                sleep_seconds = sleep_seconds + random.uniform(0, sleep_seconds * 0.5)
                time.sleep(sleep_seconds)

    # get load data from url persistance source
    def fetch_historical_energy_data(self, entity_id, start_time, end_time):
        """
        Public wrapper to fetch historical energy data from the configured source.
        """
        if self.src == "homeassistant":
            return self.__fetch_historical_energy_data_from_homeassistant(
                entity_id, start_time, end_time
            )
        elif self.src == "openhab":
            return self.__fetch_historical_energy_data_from_openhab(
                entity_id, start_time, end_time
            )
        return []

    def __fetch_historical_energy_data_from_openhab(
        self, openhab_item, start_time, end_time
    ):
        """
        Fetch energy data from the specified OpenHAB item URL within the given time range.
        """
        if openhab_item == "":
            return []
        openhab_item_url = self.url + "/rest/persistence/items/" + openhab_item
        params = {"starttime": start_time.isoformat(), "endtime": end_time.isoformat()}
        response = self.__request_with_retries(
            "get", openhab_item_url, params=params, item_label=openhab_item
        )
        if response is None:
            # Do not log error here; already logged in __request_with_retries
            return []
        try:
            historical_data = (response.json())["data"]
            filtered_data = [
                {
                    "state": entry["state"],
                    "last_updated": datetime.fromtimestamp(
                        entry["time"] / 1000, tz=timezone.utc
                    ).isoformat(),
                }
                for entry in historical_data
            ]
            return filtered_data
        except (ValueError, KeyError, TypeError) as e:
            # Only log if it's a JSON or data processing error, not a request error
            logger.error("[LOAD-IF] OPENHAB - Failed to process energy data: %s", e)
            return []

    def __fetch_historical_energy_data_from_homeassistant(
        self, entity_id, start_time, end_time
    ):
        """
        Fetch historical energy data for a specific entity from Home Assistant.

        Args:
            entity_id (str): The ID of the entity to fetch data for.
            start_time (datetime): The start time for the historical data.
            end_time (datetime): The end time for the historical data.

        Returns:
            list: A list of historical state changes for the entity.
        """
        if entity_id == "" or entity_id is None:
            return []
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        url = f"{self.url}/api/history/period/{start_time.isoformat()}"
        params = {"filter_entity_id": entity_id, "end_time": end_time.isoformat()}
        response = self.__request_with_retries(
            "get", url, params=params, headers=headers, item_label=entity_id
        )
        if response is None:
            # Do not log error here; already logged in __request_with_retries
            return []
        try:
            historical_data = response.json()
            filtered_data = [
                {
                    "state": entry["state"],
                    "last_updated": entry["last_updated"],
                    "attributes": entry.get("attributes", {}),
                }
                for sublist in historical_data
                for entry in sublist
            ]

            # if device_class is energy, convert to power
            if (
                filtered_data
                and "attributes" in filtered_data[0]
                and "device_class" in filtered_data[0]["attributes"]
            ):
                device_class = filtered_data[0]["attributes"]["device_class"]
                if device_class == "power":
                    pass
                elif device_class == "energy":

                    # convert energy (Wh) to power (W) over the time frame
                    # 1. find the first entry with valid data
                    # 2. find the last entry with valid data
                    # 3. take the delta & compute W from Wh.
                    # 4. overwrite the orginal data structure.
                    start_idx = 0
                    end_idx = len(filtered_data) - 1
                    while start_idx < end_idx:
                        try:
                            float(filtered_data[start_idx]["state"])
                            break
                        except ValueError:
                            start_idx += 1
                    while start_idx < end_idx:
                        try:
                            float(filtered_data[end_idx]["state"])
                            break
                        except ValueError:
                            end_idx -= 1
                    first_state = float(filtered_data[start_idx]["state"])
                    last_state = float(filtered_data[end_idx]["state"])
                    first_time = datetime.fromisoformat(
                        filtered_data[start_idx]["last_updated"]
                    )
                    last_time = datetime.fromisoformat(
                        filtered_data[end_idx]["last_updated"]
                    )
                    duration_hours = (last_time - first_time).total_seconds() / 3600.0

                    filtered_data_new = []
                    if duration_hours > 0:
                        power_w = (last_state - first_state) / duration_hours
                        power_w = max(
                            0, power_w
                        )  # Prevent negative from counter resets
                        filtered_data[start_idx]["state"] = power_w
                        filtered_data[end_idx]["state"] = power_w
                        filtered_data_new.append(filtered_data[start_idx])
                        filtered_data_new.append(filtered_data[end_idx])
                        logger.debug(
                            "[LOAD-IF] HOMEASSISTANT - Converted energy to power for '%s': "
                            "%.1f Wh over %.2f hours = %.1f W",
                            entity_id,
                            last_state - first_state,
                            duration_hours,
                            power_w,
                        )
                    else:
                        filtered_data[start_idx]["state"] = 0.0
                        filtered_data[end_idx]["state"] = 0.0
                        filtered_data_new.append(filtered_data[start_idx])
                        filtered_data_new.append(filtered_data[end_idx])
                        logger.debug(
                            "[LOAD-IF] HOMEASSISTANT - Duration is zero for energy to"
                            + " power conversion for '%s', assuming 0W",
                            entity_id,
                        )

                    filtered_data = filtered_data_new

            # check if the data are delivered with unit kW and convert to W
            if (
                filtered_data
                and "attributes" in filtered_data[0]
                and "unit_of_measurement" in filtered_data[0]["attributes"]
            ):
                unit = filtered_data[0]["attributes"]["unit_of_measurement"]
                if unit == "kW":
                    for entry in filtered_data:
                        try:
                            entry["state"] = float(entry["state"]) * 1000
                        except ValueError:
                            continue
            return filtered_data
        except (ValueError, KeyError, TypeError) as e:
            logger.error(
                "[LOAD-IF] HOMEASSISTANT - Failed to process energy data for '%s': %s",
                entity_id,
                str(e),
            )
            return []

    def __process_energy_data(self, data, debug_sensor=None):
        """
        Calculate the average power (in W) from a sequence of historical sensor samples.

        The function expects `data` to be a dict with a "data" key containing a list of
        timestamped samples. Each sample is a dict with at least:
            - "state": numeric or numeric-string sensor value (power in W)
            - "last_updated": ISO 8601 timestamp string

        Important expectations and behavior:
        - The list must be time-ordered with the most recent entry first (index 0) and
          older entries later (index n-1). The algorithm computes values using consecutive
          pairs (current, next) from the list.
        - For each consecutive pair the duration in seconds is computed from their
          timestamps and the product state * duration (W * s) is accumulated.
        - The returned value is an average power in watts (W). This is computed as:
            average_W = (sum over intervals of state * duration) / (total duration)
          and rounded to 4 decimal places.
        - Entries with missing keys, non-numeric states, or states equal to "unavailable"
          are skipped. Parsing errors are logged; when the source is Home Assistant a
          helpful debug URL fragment is generated if possible using `debug_sensor`.
        - If the total measured duration is less than one hour (3600 s), the code
          extrapolates the last known state forward (up to the next hour boundary) to avoid
          extremely short-sample bias.
        - If no valid duration was accumulated, the function returns 0.0.

        Args:
            data (dict): {"data": [ {"state": str|float, "last_updated": ISOtimestamp}, ... ]}
            debug_sensor (str|None): optional sensor id used to build debug URLs when logging.

        Returns:
            float: average power in watts (W), rounded to 4 decimals. Returns 0.0 if no valid data.
        """
        total_energy = 0.0
        total_duration = 0.0
        current_state = 0.0
        last_state = 0.0
        current_time = datetime.now()
        duration = 0.0

        for i in range(len(data["data"]) - 1):
            # check if data are available
            if (
                "state" not in data["data"][i + 1]
                or "state" not in data["data"][i]
                or data["data"][i + 1].get("state") == "unavailable"
                or data["data"][i].get("state") == "unavailable"
                or data["data"][i + 1].get("state") == "unknown"
                or data["data"][i].get("state") == "unknown"
            ):
                continue
            try:
                current_state = float(data["data"][i]["state"])
                last_state = float(data["data"][i + 1]["state"])
                current_time = datetime.fromisoformat(data["data"][i]["last_updated"])
                next_time = datetime.fromisoformat(data["data"][i + 1]["last_updated"])
            except (ValueError, KeyError) as e:
                debug_url = None
                if self.src == "homeassistant":
                    current_time = datetime.fromisoformat(
                        data["data"][i]["last_updated"]
                    )
                    debug_url = (
                        "(check: "
                        + self.url
                        + "/history?entity_id="
                        + quote(debug_sensor)
                        + "&start_date="
                        + quote((current_time - timedelta(hours=2)).isoformat())
                        + "&end_date="
                        + quote((current_time + timedelta(hours=2)).isoformat())
                        + ")"
                    )
                logger.info(
                    "[LOAD-IF] Skipping invalid sensor data for '%s' at %s: state '%s' cannot be"
                    + " processed (%s). "
                    "This may indicate missing or corrupted data in the database. %s",
                    debug_sensor if debug_sensor is not None else "unknown sensor",
                    datetime.fromisoformat(data["data"][i]["last_updated"]).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    data["data"][i]["state"],
                    str(e),
                    debug_url if debug_url is not None else "",
                )
                continue

            duration = (next_time - current_time).total_seconds()
            total_energy += current_state * duration
            total_duration += duration
        # After the for-loop, check if the last sample is before the end of the interval
        if len(data["data"]) > 0 and total_duration > 0:
            # Get the timestamp of the last sample
            last_sample_time = datetime.fromisoformat(data["data"][-1]["last_updated"])
            # The interval end is the latest timestamp in the interval
            # (should be provided externally)
            # If not available, assume the interval is 1 hour after the first sample
            interval_end = None
            if "interval_end" in data:
                interval_end = data["interval_end"]
            else:
                # fallback: interval is 1 hour after the first sample
                interval_end = datetime.fromisoformat(
                    data["data"][0]["last_updated"]
                ) + timedelta(seconds=self.time_frame_base)
            # If the last sample is before the interval end, extend its value
            if last_sample_time < interval_end:
                extension_duration = (interval_end - last_sample_time).total_seconds()
                try:
                    last_state = float(data["data"][-1]["state"])
                    total_energy += last_state * extension_duration
                    total_duration += extension_duration
                except (ValueError, KeyError):
                    pass
        # add last data point to total energy calculation if duration is less than 1 hour
        # if total_duration < self.time_frame_base:
        #     duration = (
        #         (current_time + timedelta(seconds=self.time_frame_base)).replace(
        #             minute=0, second=0, microsecond=0
        #         )
        #         - current_time
        #     ).total_seconds()
        #     total_energy += last_state * duration
        #     total_duration += duration
        if total_duration > 0:
            return round(total_energy / total_duration, 4)
        return 0

    def __get_additional_load_list_from_to(self, item, start_time, end_time):
        """
        Retrieves and processes additional load data within a specified time range.
        This method fetches historical energy data for additional loads from Home Assistant,
        determines the maximum additional load, and adjusts the unit of measurement if necessary.
        The processed data is then returned with all values converted to the appropriate unit.
        Args:
            start_time (datetime): The start time of the data retrieval period.
            end_time (datetime): The end time of the data retrieval period.
        Returns:
            list[dict]: A list of dictionaries containing the processed additional load data.
                        Each dictionary includes a "state" key with the adjusted load value.
        Raises:
            ValueError: If a data entry's "state" value cannot be converted to a float.
            KeyError: If a data entry does not contain the "state" key.
        Notes:
            - If the maximum additional load is between 0 and 23 (assumed to be in kW), it is
              converted to W.
            - All load values are multiplied by the determined unit factor before being returned.
        """

        if self.src == "openhab":
            additional_load_data = self.__fetch_historical_energy_data_from_openhab(
                item, start_time, end_time
            )
        elif self.src == "homeassistant":
            additional_load_data = (
                self.__fetch_historical_energy_data_from_homeassistant(
                    item, start_time, end_time
                )
            )
        else:
            logger.error(
                "[LOAD-IF] Car Load source '%s' currently not supported. Using default.",
                self.src,
            )
            return []

        # multiply every value with car_load_unit_factor before returning
        for data_entry in additional_load_data:
            try:
                data_entry["state"] = float(
                    data_entry["state"]
                )  # * car_load_unit_factor
            except ValueError:
                continue
            except KeyError:
                continue
        # print(f'HA Car load data: {car_load_data}')
        return additional_load_data

    def get_load_profile_for_day(self, start_time, end_time):
        """
        Retrieves the load profile for a specific day by fetching energy data from Home Assistant
        or using the default profile.

        Args:
            start_time (datetime): The start time for the load profile.
            end_time (datetime): The end time for the load profile.

        Returns:
            list: A list of energy consumption values for the specified day.
        """
        if self.src == "default":
            # Calculate number of intervals
            num_intervals = int(
                (end_time - start_time).total_seconds() // self.time_frame_base
            )
            default_profile = self._get_default_profile()
            # For 3600s, 48 values for 2 days, 24 for 1 day; for 900s, 192 for 2 days, 96 for 1 day
            # Return the first num_intervals values
            return default_profile[:num_intervals]

        logger.debug(
            "[LOAD-IF] Creating day load profile from %s to %s", start_time, end_time
        )

        load_profile = []
        current_time_slot = start_time

        while current_time_slot < end_time:
            next_slot = current_time_slot + timedelta(seconds=self.time_frame_base)
            # logger.debug(
            #     "[LOAD-IF] Fetching data for %s to %s", current_time_slot, next_slot
            # )
            if self.src == "openhab":
                energy_data = self.__fetch_historical_energy_data_from_openhab(
                    self.load_sensor, current_time_slot, next_slot
                )
            elif self.src == "homeassistant":
                energy_data = self.__fetch_historical_energy_data_from_homeassistant(
                    self.load_sensor, current_time_slot, next_slot
                )
            else:
                logger.error(
                    "[LOAD-IF] Load source '%s' currently not supported. Using default.",
                    self.src,
                )
                return []

            car_load_energy = 0
            # check if car load sensor is configured
            if self.car_charge_load_sensor != "":
                car_load_data = self.__get_additional_load_list_from_to(
                    self.car_charge_load_sensor, current_time_slot, next_slot
                )
                car_load_energy = abs(
                    self.__process_energy_data(
                        {"data": car_load_data}, self.car_charge_load_sensor
                    )
                )
            car_load_energy = max(car_load_energy, 0)  # prevent negative values

            add_load_data_1_energy = 0
            # check if additional load 1 sensor is configured
            if self.additional_load_1_sensor != "":
                add_load_data_1 = self.__get_additional_load_list_from_to(
                    self.additional_load_1_sensor, current_time_slot, next_slot
                )
                add_load_data_1_energy = abs(
                    self.__process_energy_data(
                        {"data": add_load_data_1}, self.additional_load_1_sensor
                    )
                )
            add_load_data_1_energy = max(
                add_load_data_1_energy, 0
            )  # prevent negative values

            energy = abs(
                self.__process_energy_data({"data": energy_data}, self.load_sensor)
            )

            # Convert average power (W) to energy (Wh) for the interval
            interval_hours = self.time_frame_base / 3600.0
            energy_wh = energy * interval_hours
            car_load_energy_wh = car_load_energy * interval_hours
            add_load_data_1_energy_wh = add_load_data_1_energy * interval_hours

            # sum_controlable_energy_load = car_load_energy + add_load_data_1_energy
            sum_controlable_energy_load_wh = (
                car_load_energy_wh + add_load_data_1_energy_wh
            )

            if sum_controlable_energy_load_wh <= energy_wh:
                energy_wh = energy_wh - sum_controlable_energy_load_wh
            else:
                debug_url = None
                if self.src == "homeassistant":
                    current_time = datetime.fromisoformat(current_time_slot.isoformat())
                    debug_url = (
                        "(check: "
                        + self.url
                        + "/history?entity_id="
                        + quote(self.load_sensor)
                        + "&start_date="
                        + quote((current_time - timedelta(hours=2)).isoformat())
                        + "&end_date="
                        + quote((current_time + timedelta(hours=2)).isoformat())
                        + " )"
                    )
                logger.warning(
                    "[LOAD-IF] DATA ERROR load smaller than car load "
                    + "- Energy for %s: %5.1f Wh (sum add energy %5.1f Wh - car load: %5.1f Wh) %s",
                    current_time_slot,
                    round(energy_wh, 1),
                    round(sum_controlable_energy_load_wh, 1),
                    round(car_load_energy, 1),
                    debug_url,
                )
            if energy_wh == 0:
                current_time = datetime.fromisoformat(current_time_slot.isoformat())
                debug_url = (
                    "(check: "
                    + self.url
                    + "/history?entity_id="
                    + quote(self.load_sensor)
                    + "&start_date="
                    + quote((current_time - timedelta(minutes=15)).isoformat())
                    + "&end_date="
                    + quote((current_time + timedelta(minutes=15)).isoformat())
                    + " )"
                )
                logger.debug(
                    "[LOAD-IF] load = 0 ... Energy for %s: %5.1f Wh"
                    + " (sum add energy %5.1f Wh - car load: %5.1f Wh - debug: %s)",
                    current_time_slot,
                    round(energy_wh, 1),
                    round(sum_controlable_energy_load_wh, 1),
                    round(car_load_energy, 1),
                    debug_url,
                )

            # Sanity check: filter out implausible values
            if energy_wh < 0 or energy_wh > 100000:
                logger.info(
                    "[LOAD-IF] Outlier detected in load profile: %s Wh at %s."
                    + " Value replaced with 0.",
                    energy_wh,
                    current_time_slot,
                )
                energy_wh = 0

            load_profile.append(energy_wh)
            logger.debug(
                "[LOAD-IF] Energy for %s: %5.1f Wh (sum add energy %5.1f Wh - car load: %5.1f Wh)",
                current_time_slot,
                round(energy_wh, 1),
                round(sum_controlable_energy_load_wh, 1),
                round(car_load_energy, 1),
            )
            current_time_slot += timedelta(seconds=self.time_frame_base)
        if not load_profile:
            logger.error(
                "[LOAD-IF] No load profile data available for the specified day - % s to % s",
                start_time,
                end_time,
            )
        return load_profile

    def __create_load_profile_weekdays(self):
        """
        Creates a load profile for weekdays based on historical data.
        This method calculates the average load profile for the same day of the week
        from one and two weeks prior, as well as the following day from one and two weeks prior.
        The resulting load profile is a combination of these averages.
        Args:
            tgt_duration (int): Target duration for the load profile
            (not currently used in the method).
        Returns:
            list: A list of 48 values representing the combined load profile for the specified days.
        """
        # Use datetime.now() without timezone or with proper timezone object
        if self.time_zone is None:
            now = datetime.now()
        else:
            now = datetime.now(self.time_zone)

        day_one_week_before = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=7)
        day_two_week_before = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=14)

        day_tomorrow_one_week_before = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=6)
        day_tomorrow_two_week_before = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=13)
        logger.info(
            "[LOAD-IF] creating load profile for weekdays %s (%s) and %s (%s)",
            day_one_week_before,
            day_one_week_before.strftime("%A"),
            day_tomorrow_one_week_before,
            day_tomorrow_one_week_before.strftime("%A"),
        )

        # get load profile for day one week before
        load_profile_one_week_before = self.get_load_profile_for_day(
            day_one_week_before, day_one_week_before + timedelta(days=1)
        )
        # get load profile for day two week before
        load_profile_two_week_before = self.get_load_profile_for_day(
            day_two_week_before, day_two_week_before + timedelta(days=1)
        )
        # get load profile for day tomorrow one week before
        load_profile_tomorrow_one_week_before = self.get_load_profile_for_day(
            day_tomorrow_one_week_before,
            day_tomorrow_one_week_before + timedelta(days=1),
        )
        # get load profile for day tomorrow two week before
        load_profile_tomorrow_two_week_before = self.get_load_profile_for_day(
            day_tomorrow_two_week_before,
            day_tomorrow_two_week_before + timedelta(days=1),
        )
        # combine load profiles with average of the connected days and
        # combine to a list with 48 values
        load_profile = []
        for i, value in enumerate(load_profile_one_week_before):
            if (
                load_profile_two_week_before
                and len(load_profile_two_week_before) >= 24
                and not all(v == 0 for v in load_profile_two_week_before)
            ):
                load_profile.append(
                    round((value + load_profile_two_week_before[i]) / 2, 3)
                )
            else:
                load_profile.append(round(value, 3))
        for i, value in enumerate(load_profile_tomorrow_one_week_before):
            if (
                load_profile_tomorrow_two_week_before
                and len(load_profile_tomorrow_two_week_before) >= 24
                and not all(v == 0 for v in load_profile_tomorrow_two_week_before)
            ):
                load_profile.append(
                    round((value + load_profile_tomorrow_two_week_before[i]) / 2, 3)
                )
            else:
                load_profile.append(round(value, 3))

        # Check if load profile contains useful values (not all zeros)
        if not load_profile or all(value == 0 for value in load_profile):
            logger.info(
                "[LOAD-IF] No historical data available from 7 and 14 days ago. "
                + "This is normal for new installations - using yesterday's data as fallback. "
                + "Load profiles will improve automatically as the system collects"
                + " more historical data."
            )
            # Get yesterday's load profile
            yesterday = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=1)
            yesterday_profile = self.get_load_profile_for_day(
                yesterday, yesterday + timedelta(days=1)
            )

            # Double yesterday's profile to create 48 hours
            if yesterday_profile and not all(value == 0 for value in yesterday_profile):
                load_profile = yesterday_profile + yesterday_profile
                logger.info(
                    "[LOAD-IF] Using yesterday's consumption pattern doubled"
                    + " for 48-hour forecast"
                )
            else:
                logger.info(
                    "[LOAD-IF] No recent consumption data available yet. "
                    + "Using built-in default profile as temporary fallback. "
                    + "This will automatically switch to real data as your system runs"
                    + " and collects sensor data."
                )
                load_profile = self._get_default_profile()
                logger.info(
                    "[LOAD-IF] Temporary default profile active -"
                    + " will improve with collected data"
                )

        return load_profile

    def get_load_profile(self, tgt_duration, start_time=None):
        """
        Retrieves the load profile based on the configured source.

        Depending on the configuration, this function fetches the load profile from one of the
        following sources:
        - Default: Returns a predefined static load profile.
        - OpenHAB: Fetches the load profile from an OpenHAB instance.
        - Home Assistant: Fetches the load profile from a Home Assistant instance.

        Args:
            tgt_duration (int): The target duration in hours for which the load profile is needed.
            start_time (datetime, optional): The start time for fetching the load profile.
            Defaults to None.

        Returns:
            list: A list of energy consumption values for the specified duration.
        """
        if self.src == "default":
            logger.info("[LOAD-IF] Using load source default")
            return self._get_default_profile()[:tgt_duration]
        if self.src in ("openhab", "homeassistant"):
            if self.load_sensor == "" or self.load_sensor is None:
                logger.error(
                    "[LOAD-IF] Load sensor not configured for source '%s'. Using default.",
                    self.src,
                )
                return self._get_default_profile()[:tgt_duration]
            return self.__create_load_profile_weekdays()

        logger.error(
            "[LOAD-IF] Load source '%s' currently not supported. Using default.",
            self.src,
        )
        return self._get_default_profile()[:tgt_duration]

    def _get_default_profile(self):
        """
        Returns the default load profile that can be reused across methods.

        Returns:
            list: A list of 48 default energy consumption values.
        """
        default_profile = [
            200.0,  # 0:00 - 1:00 -- day 1
            200.0,  # 1:00 - 2:00
            200.0,  # 2:00 - 3:00
            200.0,  # 3:00 - 4:00
            200.0,  # 4:00 - 5:00
            300.0,  # 5:00 - 6:00
            350.0,  # 6:00 - 7:00
            400.0,  # 7:00 - 8:00
            350.0,  # 8:00 - 9:00
            300.0,  # 9:00 - 10:00
            300.0,  # 10:00 - 11:00
            550.0,  # 11:00 - 12:00
            450.0,  # 12:00 - 13:00
            400.0,  # 13:00 - 14:00
            300.0,  # 14:00 - 15:00
            300.0,  # 15:00 - 16:00
            400.0,  # 16:00 - 17:00
            450.0,  # 17:00 - 18:00
            500.0,  # 18:00 - 19:00
            500.0,  # 19:00 - 20:00
            500.0,  # 20:00 - 21:00
            400.0,  # 21:00 - 22:00
            300.0,  # 22:00 - 23:00
            200.0,  # 23:00 - 0:00
            200.0,  # 0:00 - 1:00 -- day 2
            200.0,  # 1:00 - 2:00
            200.0,  # 2:00 - 3:00
            200.0,  # 3:00 - 4:00
            200.0,  # 4:00 - 5:00
            300.0,  # 5:00 - 6:00
            350.0,  # 6:00 - 7:00
            400.0,  # 7:00 - 8:00
            350.0,  # 8:00 - 9:00
            300.0,  # 9:00 - 10:00
            300.0,  # 10:00 - 11:00
            550.0,  # 11:00 - 12:00
            450.0,  # 12:00 - 13:00
            400.0,  # 13:00 - 14:00
            300.0,  # 14:00 - 15:00
            300.0,  # 15:00 - 16:00
            400.0,  # 16:00 - 17:00
            450.0,  # 17:00 - 18:00
            500.0,  # 18:00 - 19:00
            500.0,  # 19:00 - 20:00
            500.0,  # 20:00 - 21:00
            400.0,  # 21:00 - 22:00
            300.0,  # 22:00 - 23:00
            200.0,  # 23:00 - 0:00
        ]
        if self.time_frame_base == 900:
            # convert to 15 min time frame
            default_profile = [value / 4 for value in default_profile for _ in range(4)]
        return default_profile
