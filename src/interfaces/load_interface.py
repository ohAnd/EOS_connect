"""
This module provides the `LoadInterface` class, which is used to fetch and process energy data
from various sources such as OpenHAB and Home Assistant. It also includes methods to create
load profiles based on historical energy consumption data.
"""

from datetime import datetime, timedelta, timezone
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
        tz_name=None,  # Changed default to None
        tz_name=None,  # Changed default to None
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

        self.time_zone = None

        # Handle timezone properly
        if tz_name == "UTC" or tz_name is None:
        if tz_name == "UTC" or tz_name is None:
            self.time_zone = None  # Use local timezone
        elif isinstance(tz_name, str):
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
        self, method, url, params=None, headers=None, timeout=10, item_label=""
    ):
        """
        Perform an HTTP request with retries and exponential backoff.
        Returns the requests.Response on success, or None on final failure.
        """
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
            "get", openhab_item_url, params=params, timeout=10, item_label=openhab_item
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
            "get", url, params=params, headers=headers, timeout=10, item_label=entity_id
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
        # add last data point to total energy calculation if duration is less than 1 hour
        if total_duration < 3600:
            duration = (
                (current_time + timedelta(seconds=3600)).replace(
                    minute=0, second=0, microsecond=0
                )
                - current_time
            ).total_seconds()
            total_energy += last_state * duration
            total_duration += duration
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

    def __get_car_load_list_from_to(self, start_time, end_time):
        """
        Retrieves and processes car load data within a specified time range.
        This method fetches historical energy data for car charging from Home Assistant,
        determines the maximum car load, and adjusts the unit of measurement if necessary.
        The processed data is then returned with all values converted to the appropriate unit.
        Args:
            start_time (datetime): The start time of the data retrieval period.
            end_time (datetime): The end time of the data retrieval period.
        Returns:
            list[dict]: A list of dictionaries containing the processed car load data.
                        Each dictionary includes a "state" key with the adjusted load value.
        Raises:
            ValueError: If a data entry's "state" value cannot be converted to a float.
            KeyError: If a data entry does not contain the "state" key.
        Notes:
            - If the maximum car load is between 0 and 23 (assumed to be in kW), it is
              converted to W.
            - All load values are multiplied by the determined unit factor before being returned.
        """

        if self.src == "openhab":
            car_load_data = self.__fetch_historical_energy_data_from_openhab(
                self.car_charge_load_sensor, start_time, end_time
            )
        elif self.src == "homeassistant":
            car_load_data = self.__fetch_historical_energy_data_from_homeassistant(
                self.car_charge_load_sensor, start_time, end_time
            )
        else:
            logger.error(
                "[LOAD-IF] Car Load source '%s' currently not supported. Using default.",
                self.src,
            )
            return []

        # multiply every value with car_load_unit_factor before returning
        for data_entry in car_load_data:
            try:
                data_entry["state"] = float(
                    data_entry["state"]
                )  # * car_load_unit_factor
            except ValueError:
                continue
            except KeyError:
                continue
        # print(f'HA Car load data: {car_load_data}')
        return car_load_data

    def get_load_profile_for_day(self, start_time, end_time):
        """
        Retrieves the load profile for a specific day by fetching energy data from Home Assistant.

        Args:
            start_time (datetime): The start time for the load profile.
            end_time (datetime): The end time for the load profile.

        Returns:
            list: A list of energy consumption values for the specified day.
        """
        logger.debug(
            "[LOAD-IF] Creating day load profile from %s to %s", start_time, end_time
        )

        load_profile = []
        current_hour = start_time

        while current_hour < end_time:
            next_hour = current_hour + timedelta(hours=1)
            # logger.debug("[LOAD-IF] Fetching data for %s to %s", current_hour, next_hour)
            if self.src == "openhab":
                energy_data = self.__fetch_historical_energy_data_from_openhab(
                    self.load_sensor, current_hour, next_hour
                )
            elif self.src == "homeassistant":
                energy_data = self.__fetch_historical_energy_data_from_homeassistant(
                    self.load_sensor, current_hour, next_hour
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
                    self.car_charge_load_sensor, current_hour, next_hour
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
                    self.additional_load_1_sensor, current_hour, next_hour
                )
                add_load_data_1_energy = abs(
                    self.__process_energy_data(
                        {"data": add_load_data_1}, self.additional_load_1_sensor
                    )
                )
            add_load_data_1_energy = max(
                add_load_data_1_energy, 0
            )  # prevent negative values

            sum_controlable_energy_load = car_load_energy + add_load_data_1_energy
            energy = abs(
                self.__process_energy_data({"data": energy_data}, self.load_sensor)
            )

            if sum_controlable_energy_load <= energy:
                energy = energy - sum_controlable_energy_load
            else:
                debug_url = None
                if self.src == "homeassistant":
                    current_time = datetime.fromisoformat(current_hour.isoformat())
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
                        + " )"
                    )
                logger.warning(
                logger.warning(
                    "[LOAD-IF] DATA ERROR load smaller than car load "
                    + "- Energy for %s: %5.1f Wh (sum add energy %5.1f Wh - car load: %5.1f Wh) %s",
                    current_hour,
                    round(energy, 1),
                    round(sum_controlable_energy_load, 1),
                    round(car_load_energy, 1),
                    debug_url,
                )
            if energy == 0:
                logger.debug(
                logger.debug(
                    "[LOAD-IF] load = 0 ... Energy for %s: %5.1f Wh"
                    + " (sum add energy %5.1f Wh - car load: %5.1f Wh)",
                    current_hour,
                    round(energy, 1),
                    round(sum_controlable_energy_load, 1),
                    round(car_load_energy, 1),
                )

            load_profile.append(energy)
            load_profile.append(energy)
            logger.debug(
                "[LOAD-IF] Energy for %s: %5.1f Wh (sum add energy %5.1f Wh - car load: %5.1f Wh)",
                current_hour,
                round(energy, 1),
                round(sum_controlable_energy_load, 1),
                round(car_load_energy, 1),
            )
            current_hour += timedelta(hours=1)
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
            if load_profile_two_week_before and len(load_profile_two_week_before) >= 24:
                load_profile.append((value + load_profile_two_week_before[i]) / 2)
            else:
                load_profile.append(value)
        for i, value in enumerate(load_profile_tomorrow_one_week_before):
            if (
                load_profile_tomorrow_two_week_before
                and len(load_profile_tomorrow_two_week_before) >= 24
            ):
                load_profile.append(
                    (value + load_profile_tomorrow_two_week_before[i]) / 2
                )
            else:
                load_profile.append(value)

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
        return [
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
        return [
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
