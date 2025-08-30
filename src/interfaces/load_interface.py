"""
This module provides the `LoadInterface` class, which is used to fetch and process energy data
from various sources such as OpenHAB and Home Assistant. It also includes methods to create
load profiles based on historical energy consumption data.
"""

from datetime import datetime, timedelta
import logging
from urllib.parse import quote
import zoneinfo
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
        timezone=None,  # Changed default to None
    ):
        self.src = config.get("source", "")
        self.url = config.get("url", "")
        self.load_sensor = config.get("load_sensor", "")
        self.car_charge_load_sensor = config.get("car_charge_load_sensor", "")
        self.additional_load_1_sensor = config.get("additional_load_1_sensor", "")
        self.access_token = config.get("access_token", "")

        # Handle timezone properly
        if timezone == "UTC" or timezone is None:
            self.time_zone = None  # Use local timezone
        elif isinstance(timezone, str):
            # Try to convert string timezone to proper timezone object
            try:
                self.time_zone = zoneinfo.ZoneInfo(timezone)
            except ImportError:
                # Fallback for older Python versions
                try:
                    self.time_zone = pytz.timezone(timezone)
                except ImportError:
                    logger.warning(
                        "[LOAD-IF] Cannot parse timezone '%s', using local time",
                        timezone,
                    )
                    self.time_zone = None
        else:
            self.time_zone = timezone

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

    # get load data from url persistance source
    def __fetch_historical_energy_data_from_openhab(
        self, openhab_item, start_time, end_time
    ):
        """
        Fetch energy data from the specified OpenHAB item URL within the given time range.
        """
        if openhab_item == "":
            return {"data": []}
        openhab_item_url = self.url + "/rest/persistence/items/" + openhab_item
        params = {"starttime": start_time.isoformat(), "endtime": end_time.isoformat()}
        try:
            response = requests.get(openhab_item_url, params=params, timeout=10)
            response.raise_for_status()
            # logger.debug(
            #     "[LOAD-IF] OPENHAB - Fetched data from %s to %s",
            #     start_time.isoformat(),
            #     end_time.isoformat()
            # )

            historical_data = (response.json())["data"]
            # Extract only 'state' and 'last_updated' from the historical data
            filtered_data = [
                {
                    "state": entry["state"],
                    "last_updated": datetime.utcfromtimestamp(
                        entry["time"] / 1000
                    ).isoformat(),
                }
                for entry in historical_data
            ]
            return filtered_data
        except requests.exceptions.Timeout:
            logger.error(
                "[LOAD-IF] OPENHAB - Request timed out while fetching energy data."
            )
            return {"data": []}
        except requests.exceptions.RequestException as e:
            logger.error(
                "[LOAD-IF] OPENHAB - Request failed while fetching energy data: %s", e
            )
            return {"data": []}

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
            # logger.debug("[LOAD-IF] HOMEASSISTANT get historical values"+
            # " - No entity_id configured.")
            return []
        # Headers for the API request
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        # API endpoint to get the history of the entity
        url = f"{self.url}/api/history/period/{start_time.isoformat()}"

        # Parameters for the API request
        params = {"filter_entity_id": entity_id, "end_time": end_time.isoformat()}

        # Make the API request
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            # Check if the request was successful
            if response.status_code == 200:
                historical_data = response.json()
                # Extract only 'state' and 'last_updated' from the historical data
                filtered_data = [
                    {"state": entry["state"], "last_updated": entry["last_updated"]}
                    for sublist in historical_data
                    for entry in sublist
                ]
                return filtered_data
            logger.error(
                "[LOAD-IF] HOMEASSISTANT - Failed to retrieve"
                + " historical data for '%s' - error: %s",
                entity_id,
                response.status_code,
            )
            logger.error(response.text)
            return []
        except requests.exceptions.Timeout:
            logger.error(
                "[LOAD-IF] HOMEASSISTANT - Request timed out"
                + " while fetching historical energy data for '%s'.",
                entity_id,
            )
            return []
        except requests.exceptions.RequestException as e:
            logger.error(
                "[LOAD-IF] HOMEASSISTANT - Request failed while fetching"
                + " historical energy data for '%s' - error: %s",
                entity_id,
                e,
            )
            return []

    def __process_energy_data(self, data, debug_sensor=None):
        """
        Processes energy data to calculate the average energy consumption based on timestamps.
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
                data["data"][i + 1]["state"] == "unavailable"
                or data["data"][i]["state"] == "unavailable"
            ):
                # if debug_name != "add_load_1":
                #     logger.error(
                #         "[LOAD-IF] state 'unavailable' in data '%s' at index %d: %s",
                #         debug_name if debug_name is not None else '',
                #         i,
                #         data["data"][i],
                #     )
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
                logger.error(
                    "[LOAD-IF] Error processing energy ('%s') data"
                    + " at index %d: %s (next: %s) - %s %s",
                    debug_sensor if debug_sensor is not None else "",
                    i,
                    data["data"][i],
                    data["data"][i + 1],
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
                        + ")"
                    )
                logger.error(
                    "[LOAD-IF] DATA ERROR load smaller than car load "
                    + "- Energy for %s: %5.1f Wh (sum add energy %5.1f Wh - car load: %5.1f Wh) %s",
                    current_hour,
                    round(energy, 1),
                    round(sum_controlable_energy_load, 1),
                    round(car_load_energy, 1),
                    debug_url,
                )
            if energy == 0:
                logger.warning(
                    "[LOAD-IF] load = 0 ... Energy for %s: %5.1f Wh"
                    + " (sum add energy %5.1f Wh - car load: %5.1f Wh)",
                    current_hour,
                    round(energy, 1),
                    round(sum_controlable_energy_load, 1),
                    round(car_load_energy, 1),
                )
                # current_hour += timedelta(hours=1)
                # continue

            energy_sum = energy

            load_profile.append(energy_sum)
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
        if self.src == "default":
            logger.info("[LOAD-IF] Using load source default")
            return default_profile[:tgt_duration]
        if self.src in ("openhab", "homeassistant"):
            if self.load_sensor == "" or self.load_sensor is None:
                logger.error(
                    "[LOAD-IF] Load sensor not configured for source '%s'. Using default.",
                    self.src,
                )
                return default_profile[:tgt_duration]
            return self.__create_load_profile_weekdays()

        logger.error(
            "[LOAD-IF] Load source '%s' currently not supported. Using default.",
            self.src,
        )
        return default_profile[:tgt_duration]

    # additional load planning methods
    def get_additional_load_remaining_hours(self, target_runtime, limit):
        """
        Calculates the remaining hours needed to reach the target runtime,
        considering the hours already run today. If the remaining hours needed
        exceed the hours left until midnight, returns 0 (impossible to complete today).

        Args:
            target_runtime (float): The desired total runtime in hours.
            limit (Any): A parameter used to determine the already run hours 
                         (passed to get_additional_load_start_and_duration_today).
        Returns:
            float: The number of additional hours required to reach the target runtime.
                Returns 0 if the target has already been met, exceeded, 
                or cannot be completed today.
        """

        already_runned_hours = self.get_additional_load_start_and_duration_today(limit)
        hours_needed_for_target = target_runtime

        if already_runned_hours and already_runned_hours.get("duration") is not None:
            remaining_hours_above_limit = already_runned_hours["duration"]
            hours_needed_for_target = max(
                0, target_runtime - remaining_hours_above_limit
            )

        # Calculate hours remaining until midnight in the configured timezone
        if self.time_zone is None:
            now = datetime.now()
        else:
            now = datetime.now(self.time_zone)

        # Calculate midnight of the next day
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # Calculate remaining hours until midnight (as float for precision)
        hours_until_midnight = (next_midnight - now).total_seconds() / 3600

        # If the needed hours exceed the available hours until midnight, return 0
        if hours_needed_for_target > hours_until_midnight:
            logger.debug(
                "[LOAD-IF] Cannot complete target runtime today. "
                "Needed: %.2f hours, Available until midnight: %.2f hours",
                hours_needed_for_target,
                hours_until_midnight,
            )
            return 0.0

        return hours_needed_for_target

    def get_additional_load_start_and_duration_today(self, limit=10):
        """
        Finds the hour when the additional load last started being above the given limit
        and the duration of the current/last continuous load period.

        Args:
            limit (float): The threshold value for the additional load (default: 10).

        Returns:
            dict: Dictionary with 'start_hour' and 'duration', or empty dict if no periods found.
              Example: {'start_hour': 7, 'duration': 4}
        """
        load_profile = self.__get_additional_load_profile_today_until_now()
        logger.debug(
            "[LOAD-IF] Additional load profile (today until now): %s", load_profile
        )

        if not load_profile:
            return {}

        # Find all hours above the limit
        hours_above_limit = [i for i, value in enumerate(load_profile) if value > limit]

        if not hours_above_limit:
            return {}  # No hours above limit

        # Find continuous periods by looking for gaps
        periods = []
        current_period_start = hours_above_limit[0]

        for i in range(1, len(hours_above_limit)):
            prev_hour = hours_above_limit[i - 1]
            curr_hour = hours_above_limit[i]

            # Check if there's a gap (not consecutive hours)
            if curr_hour - prev_hour > 1:
                # End current period and start new one
                period_duration = prev_hour - current_period_start + 1
                periods.append(
                    {
                        "start_hour": current_period_start,
                        "end_hour": prev_hour,
                        "duration": period_duration,
                    }
                )
                current_period_start = curr_hour

        # Add the last period
        last_end_hour = hours_above_limit[-1]
        last_duration = last_end_hour - current_period_start + 1
        periods.append(
            {
                "start_hour": current_period_start,
                "end_hour": last_end_hour,
                "duration": last_duration,
            }
        )

        # Return the last period's start and duration
        if periods:
            last_period = periods[-1]
            return {
                "start_hour": last_period["start_hour"],
                "duration": last_period["duration"],
            }

        # If not found, return dict with None content as requested
        return {"start_hour": None, "duration": None}

    def __get_additional_load_profile_today_until_now(self):
        """
        Generate the additional load profile for today from midnight until the current time.
        This method calculates the energy consumption for each hour from midnight of the current day
        up to the present moment, based on the configured additional load sensor. The energy values
        are aggregated into a list, where each entry corresponds to the energy consumed
        in a specific hour.
        Returns:
            list: A list of energy values (in Wh) for each hour from midnight to now.
                  If no data is available, the list will be empty.
        Logs:
            - Debug messages for the creation of the load profile and energy values per hour.
            - Warning if the energy value for an hour is zero.
            - Error if no load profile data is available for the specified day.
        """
        load_profile = []

        if self.time_zone is None:
            now = datetime.now()
            today_midnight = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            now = datetime.now(self.time_zone)
            today_midnight = datetime.now(self.time_zone).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        logger.debug(
            "[LOAD-IF] Creating additional load profile for today from %s to %s",
            today_midnight,
            now,
        )

        current_hour = today_midnight
        end_time = now

        while current_hour < end_time:
            next_hour = current_hour + timedelta(hours=1)

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

            energy = add_load_data_1_energy

            if energy == 0:
                logger.warning(
                    "[LOAD-IF] Additional load = 0 ... Energy for %s: %5.1f Wh",
                    current_hour,
                    round(energy, 1),
                )
                # current_hour += timedelta(hours=1)
                # continue

            energy_sum = energy

            load_profile.append(energy_sum)
            logger.debug(
                "[LOAD-IF] additional load - energy for %s: %5.1f Wh",
                current_hour,
                round(energy, 1),
            )
            current_hour += timedelta(hours=1)
        if not load_profile:
            logger.error(
                "[LOAD-IF] additional load - No load profile data available for the specified day - % s to % s",
                current_hour,
                end_time,
            )
        return load_profile
