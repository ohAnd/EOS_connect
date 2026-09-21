"""
This module provides the `LoadInterface` class, which is used to fetch and process energy data
from various sources such as OpenHAB and Home Assistant. It also includes methods to create
load profiles based on historical energy consumption data.
"""

from datetime import datetime, timedelta, timezone
import logging
from urllib.parse import quote
import time
import math
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
        extra_subtract_sensors=None,
    ):
        self.src = config.get("source", "")
        self.url = config.get("url", "")
        self.load_sensor = config.get("load_sensor", "")
        self.car_charge_load_sensor = config.get("car_charge_load_sensor", "")
        self.additional_load_1_sensor = config.get("additional_load_1_sensor", "")
        # Power sensors of managed loads, whose measured history has to leave the
        # household base load for the same reason the two above do: their predicted
        # consumption is added back on top, and counting the appliance twice is exactly
        # what makes a heat pump in `additional_load_1` worse than not configuring it.
        # The list arrives from the caller so this interface stays unaware of what a
        # managed load is.
        self.extra_subtract_sensors = [
            str(sensor).strip()
            for sensor in (extra_subtract_sensors or [])
            if str(sensor or "").strip()
        ]
        raw_token = config.get("access_token", "")
        # Strip leading/trailing whitespace that can be introduced by YAML >- block
        # scalar style when long tokens wrap across multiple lines
        self.access_token = str(raw_token).strip()
        if self.access_token != raw_token:
            logger.warning(
                "[LOAD-IF] access_token had leading/trailing whitespace stripped. "
                "Check your access token setting for unintended spaces."
            )
        elif " " in self.access_token or "\n" in self.access_token:
            logger.warning(
                "[LOAD-IF] access_token contains internal whitespace. This will cause "
                "HTTP 403 errors. Re-enter the token in Settings → Data Source "
                "without extra spaces or line breaks."
            )

        # SSL verification
        self.ssl_ignore = bool(config.get("ssl_ignore", False))
        if self.ssl_ignore:
            logger.warning(
                "[LOAD-IF] ssl_ignore=True: SSL certificate verification is disabled. "
                "Only use this with a trusted private network."
            )

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

        # Cache for Home Assistant history.
        # Home Assistant may return empty results or time out for historical
        # intervals although the recorder contains the requested data.
        # Once a complete history range has been retrieved, subsequent
        # hourly requests can be served locally from this cache.
        self.__homeassistant_history_cache = {}

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

        self.configuration_state = "unknown"  # 'valid', 'incomplete', or 'invalid'
        self.configuration_valid = False
        self.configuration_message = ""
        self.__check_config()

    def __check_config(self):
        """
        Checks if the configuration is valid.

        Falling back to the default profile is a legitimate outcome, so this never
        raises. It does record *why* it fell back, because the difference between "no
        source configured" and "a source is configured but unreadable" is invisible in
        the resulting load profile — both produce the same synthetic curve.

        Returns:
            bool: True if the configuration is valid, False otherwise.
        """
        if self.src not in ["openhab", "homeassistant", "default"]:
            self.configuration_state = "invalid"
            self.configuration_message = (
                f"Load source '{self.src}' is not supported. "
                "Using the built-in default load profile."
            )
            logger.error(
                "[LOAD-IF] Invalid source '%s' configured. Using default.", self.src
            )
            self.src = "default"
            return False
        if self.src != "default":
            missing, where = None, "Data Source"
            if self.url == "":
                missing = "the data source URL is not configured"
            elif self.access_token == "" and self.src == "homeassistant":
                missing = "the Home Assistant access token is not configured"
            elif self.load_sensor == "":
                missing, where = "no load sensor is set", "Load"

            if missing:
                self.configuration_state = "incomplete"
                self.configuration_message = (
                    f"Load source '{self.src}' is selected, but {missing}. "
                    "The built-in default load profile is used instead — set it under "
                    f"Settings > {where} to optimize against your real consumption."
                )
                logger.warning("[LOAD-IF] %s", self.configuration_message)
                self.src = "default"
                return False

            logger.debug("[LOAD-IF] Config check successful using '%s'", self.src)
        else:
            logger.debug("[LOAD-IF] Using default load profile.")
        self.configuration_state = "valid"
        self.configuration_valid = True
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
        self,
        method,
        url,
        params=None,
        headers=None,
        timeout=None,
        item_label="",
        json_data=None,
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
                        url,
                        params=params,
                        headers=headers,
                        timeout=timeout,
                        verify=not self.ssl_ignore,
                    )
                else:
                    response = requests.request(
                        method,
                        url,
                        params=params,
                        headers=headers,
                        json=json_data,
                        timeout=timeout,
                        verify=not self.ssl_ignore,
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

    def __normalize_history_timestamp(self, timestamp, reference_time):
        """Normalize a history timestamp to the timezone semantics of a reference time.

        Home Assistant Recorder timestamps are normally timezone-aware UTC values,
        while the load-profile code historically uses naive local datetimes. Simply
        stripping the UTC tzinfo is incorrect because it changes the represented
        instant (for example 22:00 UTC becomes 22:00 local instead of 00:00 CEST).
        Convert aware timestamps to the reference timezone first, and only then
        remove tzinfo when the reference itself is naive.
        """
        if timestamp is None or reference_time is None:
            return timestamp

        timestamp_tz = timestamp.tzinfo
        reference_tz = reference_time.tzinfo

        if reference_tz is None:
            if timestamp_tz is None:
                return timestamp

            # The existing load-profile code treats naive datetimes as local time.
            # Convert HA's timezone-aware timestamp to the host's local timezone
            # before dropping tzinfo so the wall-clock time remains correct.
            return timestamp.astimezone().replace(tzinfo=None)

        if timestamp_tz is None:
            return timestamp.replace(tzinfo=reference_tz)

        return timestamp.astimezone(reference_tz)

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

        Home Assistant's history REST endpoint only exposes recorder state
        changes. For sensors with Recorder statistics, current and older data
        can also be retrieved through the recorder.get_statistics action:
        5-minute short-term statistics are preferred and hourly long-term
        statistics are used when short-term statistics do not cover the
        requested interval.

        The returned structure deliberately remains the same as the existing
        history path (state + last_updated), so the load-profile processing
        below does not need a separate statistics code path.
        """
        if entity_id == "" or entity_id is None:
            return []

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        url = f"{self.url}/api/history/period/{start_time.isoformat()}"

        def normalize_timestamp(value, reference):
            try:
                timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
            except (ValueError, TypeError):
                return None
            return self.__normalize_history_timestamp(timestamp, reference)

        def filter_history(history_data):
            filtered = []
            for entry in history_data:
                entry_time = normalize_timestamp(entry.get("last_updated"), start_time)
                if entry_time is None:
                    continue
                if start_time <= entry_time <= end_time:
                    filtered.append(entry)
            return filtered

        def normalize_statistics(rows, period):
            """Convert HA recorder statistics rows into state-like samples."""
            samples = []
            for row in rows or []:
                try:
                    row_start = datetime.fromisoformat(row["start"])
                    row_end = datetime.fromisoformat(row["end"])
                except (KeyError, TypeError, ValueError):
                    continue

                row_start = self.__normalize_history_timestamp(row_start, start_time)
                row_end = self.__normalize_history_timestamp(row_end, end_time)

                # For measurement sensors (e.g. sensor.hausverbrauch), mean is
                # the average power for the statistics bucket. For total/energy
                # sensors, change is the energy accumulated during the bucket;
                # convert it to an average W value so the existing load-profile
                # processing can consume it unchanged.
                value = row.get("mean")
                if value is None:
                    value = row.get("state")
                if value is None and row.get("change") is not None:
                    try:
                        value = float(row["change"]) * 1000.0 / (
                            (row_end - row_start).total_seconds() / 3600.0
                        )
                    except (TypeError, ValueError, ZeroDivisionError):
                        value = None

                if value is None:
                    continue
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue

                if row_end <= start_time or row_start >= end_time:
                    continue

                # Statistics are interval values. Represent each bucket as a
                # constant state from its start through its end. This gives
                # __process_energy_data the same timestamped sample format as
                # the history REST path and preserves the bucket's mean.
                clipped_start = max(row_start, start_time)
                clipped_end = min(row_end, end_time)
                if clipped_end <= clipped_start:
                    continue

                samples.append(
                    {
                        "state": value,
                        "last_updated": clipped_start.isoformat(),
                        "attributes": {},
                    }
                )
                samples.append(
                    {
                        "state": value,
                        "last_updated": clipped_end.isoformat(),
                        "attributes": {},
                    }
                )

            # Collapse duplicate timestamps while retaining the last value.
            deduped = {}
            for sample in samples:
                deduped[sample["last_updated"]] = sample
            return sorted(deduped.values(), key=lambda entry: entry["last_updated"])

        def normalize_history_values(history_values):
            """Preserve the existing history unit/device-class conversions."""
            if not history_values:
                return []

            try:
                if (
                    "attributes" in history_values[0]
                    and "device_class" in history_values[0]["attributes"]
                ):
                    device_class = history_values[0]["attributes"]["device_class"]
                    if device_class == "energy":
                        start_idx = 0
                        end_idx = len(history_values) - 1
                        while start_idx < end_idx:
                            try:
                                float(history_values[start_idx]["state"])
                                break
                            except (ValueError, TypeError):
                                start_idx += 1
                        while start_idx < end_idx:
                            try:
                                float(history_values[end_idx]["state"])
                                break
                            except (ValueError, TypeError):
                                end_idx -= 1

                        first_state = float(history_values[start_idx]["state"])
                        last_state = float(history_values[end_idx]["state"])
                        first_time = datetime.fromisoformat(
                            history_values[start_idx]["last_updated"]
                        )
                        last_time = datetime.fromisoformat(
                            history_values[end_idx]["last_updated"]
                        )
                        duration_hours = (
                            last_time - first_time
                        ).total_seconds() / 3600.0

                        if duration_hours > 0:
                            power_w = max(
                                0, (last_state - first_state) / duration_hours
                            )
                        else:
                            power_w = 0.0

                        history_values = [
                            {**history_values[start_idx], "state": power_w},
                            {**history_values[end_idx], "state": power_w},
                        ]

                if (
                    history_values
                    and "attributes" in history_values[0]
                    and history_values[0]["attributes"].get("unit_of_measurement") == "kW"
                ):
                    for entry in history_values:
                        try:
                            entry["state"] = float(entry["state"]) * 1000
                        except (ValueError, TypeError):
                            continue

                return history_values
            except (ValueError, KeyError, TypeError):
                logger.error(
                    "[LOAD-IF] HOMEASSISTANT - Failed to process energy data for '%s'.",
                    entity_id,
                )
                return []

        def statistics_request(period):
            statistics_url = f"{self.url}/api/services/recorder/get_statistics"

            # Home Assistant may omit the final hourly bucket when the requested
            # end_time is exactly aligned with the day boundary. Request one
            # additional hour for hourly statistics; normalize_statistics()
            # clips the result back to the original requested interval.
            statistics_end_time = end_time
            if period == "hour":
                statistics_end_time = end_time + timedelta(hours=1)

            payload = {
                "statistic_ids": [entity_id],
                "start_time": start_time.isoformat(),
                "end_time": statistics_end_time.isoformat(),
                "period": period,
                "types": ["mean", "state", "change"],
            }
            response = self.__request_with_retries(
                "post",
                statistics_url,
                params={"return_response": "true"},
                headers=headers,
                timeout=max(self.request_timeout, 30),
                item_label=f"{entity_id} ({period} statistics)",
                json_data=payload,
            )
            if response is None:
                return []
            try:
                response_data = response.json()
                rows = (
                    response_data.get("service_response", {})
                    .get("statistics", {})
                    .get(entity_id, [])
                )
                return normalize_statistics(rows, period)
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "[LOAD-IF] HOMEASSISTANT - Invalid %s statistics response for '%s'.",
                    period,
                    entity_id,
                )
                return []

        # First use the existing state-history path. It remains the preferred
        # source because it has the original sample resolution and attributes.
        cached_history = self.__homeassistant_history_cache.get(entity_id)
        filtered_data = None

        if cached_history is not None:
            cached_start = self.__normalize_history_timestamp(
                cached_history["start_time"], start_time
            )
            cached_end = self.__normalize_history_timestamp(
                cached_history["end_time"], end_time
            )
            requested_start = self.__normalize_history_timestamp(start_time, cached_start)
            requested_end = self.__normalize_history_timestamp(end_time, cached_end)
            if cached_start <= requested_start and cached_end >= requested_end:
                cached_filtered = filter_history(cached_history["data"])
                if len(cached_filtered) >= 2:
                    filtered_data = cached_filtered
                elif (
                    len(cached_filtered) == 1
                    and start_time.hour == 23
                    and start_time.minute == 0
                    and start_time.second == 0
                    and end_time == start_time + timedelta(hours=1)
                    and cached_filtered[0].get("last_updated") is not None
                ):
                    # The complete-day statistics cache can contain the final
                    # 23:00 bucket with its start sample but no explicit
                    # 00:00 boundary. For this final slot, the statistics
                    # value is constant over the bucket, so use the same
                    # value at the slot end instead of falling through to
                    # another HA history/statistics request and producing
                    # an artificial 0 Wh hour.
                    cached_filtered = [
                        cached_filtered[0],
                        {
                            **cached_filtered[0],
                            "last_updated": end_time.isoformat(),
                        },
                    ]
                    filtered_data = cached_filtered

        if filtered_data is None:
            response = self.__request_with_retries(
                "get",
                url,
                params={
                    "filter_entity_id": entity_id,
                    "end_time": end_time.isoformat(),
                },
                headers=headers,
                item_label=entity_id,
            )

            historical_data = None
            if response is not None:
                try:
                    historical_data = response.json()
                except (ValueError, TypeError):
                    historical_data = None

            if historical_data:
                history_data = [
                    {
                        "state": entry["state"],
                        "last_updated": entry["last_updated"],
                        "attributes": entry.get("attributes", {}),
                    }
                    for sublist in historical_data
                    for entry in sublist
                ]
                history_data.sort(key=lambda entry: entry.get("last_updated", ""))
                self.__homeassistant_history_cache[entity_id] = {
                    "start_time": start_time,
                    "end_time": end_time,
                    "data": history_data,
                }
                filtered_data = filter_history(history_data)

            # Do not retry historical intervals with a current-time end_time.
            # That fallback can make Home Assistant return a much larger history
            # payload than requested and is especially expensive when this method
            # is called for many hourly intervals. Recorder statistics below are
            # the controlled fallback for sensors whose state history is empty.

            if filtered_data and len(filtered_data) >= 2:
                return normalize_history_values(filtered_data)

            # State history is unavailable or contains too few samples to
            # calculate an interval average. Use Recorder statistics instead. Prefer 5-minute short-term
            # statistics; if they do not cover the requested interval, use the
            # hourly long-term statistics. The latter are retained indefinitely
            # and therefore cover the 14-day look-back used by the optimizer.
            short_term_data = statistics_request("5minute")
            if short_term_data:
                first_time = normalize_timestamp(short_term_data[0]["last_updated"], start_time)
                last_time = normalize_timestamp(short_term_data[-1]["last_updated"], end_time)
                if first_time is not None and last_time is not None:
                    # normalize_statistics() clips the first/last bucket to the
                    # requested interval, so a non-empty result that reaches both
                    # boundaries covers the complete requested interval.
                    if first_time <= start_time and last_time >= end_time:
                        logger.info(
                            "[LOAD-IF] HOMEASSISTANT - Using 5-minute statistics for '%s' (%d samples).",
                            entity_id,
                            len(short_term_data),
                        )
                        self.__homeassistant_history_cache[entity_id] = {
                            "start_time": start_time,
                            "end_time": end_time,
                            "data": short_term_data,
                        }
                        return short_term_data

            long_term_data = statistics_request("hour")
            if long_term_data:
                logger.info(
                    "[LOAD-IF] HOMEASSISTANT - Using hourly statistics for '%s' (%d samples).",
                    entity_id,
                    len(long_term_data),
                )
                self.__homeassistant_history_cache[entity_id] = {
                    "start_time": start_time,
                    "end_time": end_time,
                    "data": long_term_data,
                }
                return long_term_data

            # A missing historical interval is an expected data-quality fallback
            # condition and can occur for individual hourly requests. Do not emit a
            # warning for every interval; callers already handle an empty result and
            # the aggregate LOAD-IF warning is emitted at the appropriate level.
            logger.debug(
                "[LOAD-IF] HOMEASSISTANT - No history or recorder statistics available for '%s' from %s to %s.",
                entity_id,
                start_time,
                end_time,
            )
            return []

        return filtered_data or []

    def __fill_missing_values_in_data(self, data, debug_sensor=None):
        """
        Forward-fill missing or invalid sensor values in historical data.

        This method scans through historical sensor data and replaces invalid values
        (empty strings, None, NaN) with the last known valid value (forward-fill/LOCF).
        This ensures that energy calculation always has valid data and prevents 422 errors
        when sending incomplete arrays to EOS.

        Args:
            data (dict): {"data": [ {"state": str|float, "last_updated": ISOtimestamp}, ... ]}
            debug_sensor (str|None): Sensor name for logging

        Returns:
            dict: Modified data dict with filled values
        """
        if data is None or "data" not in data or len(data["data"]) == 0:
            return data

        filled_indices = []
        last_valid_state = None

        for i in range(len(data["data"])):
            try:
                state = data["data"][i].get("state")
                # Check if state is valid/non-empty
                if state is None or state == "" or state == "unavailable" or state == "unknown":
                    # Invalid state - try to fill with last known value
                    if last_valid_state is not None:
                        data["data"][i]["state"] = last_valid_state
                        filled_indices.append(i)
                    continue

                # Try to convert to float to verify it's numeric
                numeric_val = float(state)

                # Check for NaN
                if math.isnan(numeric_val):
                    if last_valid_state is not None:
                        data["data"][i]["state"] = last_valid_state
                        filled_indices.append(i)
                    continue

                # Valid numeric value - save as last known
                last_valid_state = state
            except (ValueError, TypeError, KeyError):
                # Cannot convert to float - try to fill
                if last_valid_state is not None:
                    try:
                        data["data"][i]["state"] = last_valid_state
                        filled_indices.append(i)
                    except (TypeError, KeyError):
                        pass

        # Log debug if any values were filled
        if filled_indices:
            logger.debug(
                "[LOAD-IF] DATA QUALITY: Filled %d missing/invalid values in '%s' at indices %s. "
                "Last known value was used. This indicates data gaps or corrupted states in Home Assistant history.",
                len(filled_indices),
                debug_sensor if debug_sensor else "unknown sensor",
                filled_indices[:10] + ["..."] if len(filled_indices) > 10 else filled_indices
            )

        return data

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
        # Forward-fill missing/invalid values before processing
        data = self.__fill_missing_values_in_data(data, debug_sensor)

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
            except (ValueError, KeyError, TypeError) as e:
                debug_url = None
                error_context = None
                problematic_state = None
                problematic_time = None

                # Determine which datapoint caused the error
                if self.src == "homeassistant":
                    try:
                        float(data["data"][i]["state"])
                        float(data["data"][i + 1]["state"])
                        # If both conversions work, error must be in datetime parsing
                        error_context = (
                            f"[Index {i}] '{data['data'][i].get('state', 'N/A')}' @ "
                            f"{data['data'][i].get('last_updated', 'N/A')}, "
                            f"[Index {i+1}] '{data['data'][i+1].get('state', 'N/A')}' @ "
                            f"{data['data'][i+1].get('last_updated', 'N/A')} (datetime parse error)"
                        )
                        problematic_time = None
                    except (ValueError, KeyError):
                        # Error is in state conversion - find which one
                        try:
                            float(data["data"][i]["state"])
                            # data[i] is valid, so error is in data[i+1]
                            problematic_state = data["data"][i + 1].get("state", "N/A")
                            problematic_time = data["data"][i + 1].get("last_updated", "N/A")
                            error_context = (
                                f"[Index {i}] Valid: '{data['data'][i]['state']}' @ "
                                f"{data['data'][i]['last_updated'].split('.')[0] if '.' in str(data['data'][i]['last_updated']) else data['data'][i]['last_updated']}, "
                                f"[Index {i+1}] PROBLEMATIC: '{problematic_state}' @ {problematic_time}"
                            )
                        except (ValueError, KeyError):
                            # Error is in data[i]
                            problematic_state = data["data"][i].get("state", "N/A")
                            problematic_time = data["data"][i].get("last_updated", "N/A")
                            error_context = (
                                f"[Index {i}] PROBLEMATIC: '{problematic_state}' @ {problematic_time}"
                            )

                    # Generate debug URL only if we have a valid timestamp
                    if problematic_time and isinstance(problematic_time, str):
                        try:
                            parsed_time = datetime.fromisoformat(problematic_time)
                            debug_url = (
                                "(check: "
                                + self.url
                                + "/history?entity_id="
                                + quote(debug_sensor)
                                + "&start_date="
                                + quote((parsed_time - timedelta(hours=2)).isoformat())
                                + "&end_date="
                                + quote((parsed_time + timedelta(hours=2)).isoformat())
                                + ")"
                            )
                        except (ValueError, TypeError):
                            debug_url = "(invalid timestamp format in data)"
                else:
                    # For non-HA sources, show both values for clarity
                    error_context = (
                        f"[Index {i}] '{data['data'][i].get('state', 'N/A')}' vs "
                        f"[Index {i+1}] '{data['data'][i + 1].get('state', 'N/A')}'"
                    )

                logger.info(
                    "[LOAD-IF] Skipping invalid sensor data for '%s': %s "
                    "cannot be processed (%s). "
                    "This may indicate missing or corrupted data in the database. %s",
                    debug_sensor if debug_sensor is not None else "unknown sensor",
                    error_context if error_context else f"state '{problematic_state}'",
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

    def __prefetch_homeassistant_day(self, entity_id, start_time, end_time):
        """Fetch one complete day once so hourly profile processing stays local.

        The load-profile algorithm still processes one-hour slots, but the HA
        data source is queried only once for the complete day. The normal
        history/statistics method stores the result in the per-entity cache,
        allowing the 24 hourly calls to be served without additional HA requests.
        """
        if not entity_id or self.src != "homeassistant":
            return

        cached = self.__homeassistant_history_cache.get(entity_id)
        if cached is not None:
            cached_start = self.__normalize_history_timestamp(
                cached["start_time"], start_time
            )
            cached_end = self.__normalize_history_timestamp(
                cached["end_time"], end_time
            )
            requested_start = self.__normalize_history_timestamp(start_time, cached_start)
            requested_end = self.__normalize_history_timestamp(end_time, cached_end)
            if cached_start <= requested_start and cached_end >= requested_end:
                return

        logger.debug(
            "[LOAD-IF] HOMEASSISTANT - Prefetching '%s' for %s to %s once for the complete day.",
            entity_id,
            start_time,
            end_time,
        )
        self.__fetch_historical_energy_data_from_homeassistant(
            entity_id, start_time, end_time
        )

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

        # Fetch each Home Assistant source for the complete day once. The hourly
        # loop below then reads only from the local cache. This prevents the
        # previous 24-requests-per-sensor pattern and keeps statistics fallback
        # requests bounded to one call per day and sensor.
        if self.src == "homeassistant":
            entities = [
                self.load_sensor,
                self.car_charge_load_sensor,
                self.additional_load_1_sensor,
            ]
            for entity_id in dict.fromkeys(entity for entity in entities if entity):
                self.__prefetch_homeassistant_day(entity_id, start_time, end_time)

        load_profile = []
        day_has_data = False

        # The complete-day prefetch is the authoritative indication that
        # historical source data exists. Individual hourly cache slices can
        # legitimately be empty at a boundary even though the day-level
        # statistics response contains valid data.
        if self.src == "homeassistant":
            cached_load = self.__homeassistant_history_cache.get(self.load_sensor)
            if cached_load:
                cached_data = cached_load.get("data") or []
                if cached_data:
                    day_has_data = True
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

            # Distinguish missing source data from a valid zero-consumption
            # interval. The old logic represented both cases as 0 Wh, which
            # made a complete statistics response look like missing history.
            if energy_data:
                day_has_data = True

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

            managed_load_energy = 0
            for sensor in self.extra_subtract_sensors:
                managed_data = self.__get_additional_load_list_from_to(
                    sensor, current_time_slot, next_slot
                )
                managed_load_energy += max(
                    abs(self.__process_energy_data({"data": managed_data}, sensor)), 0
                )

            energy = abs(
                self.__process_energy_data({"data": energy_data}, self.load_sensor)
            )

            # Convert average power (W) to energy (Wh) for the interval
            interval_hours = self.time_frame_base / 3600.0
            energy_wh = energy * interval_hours
            car_load_energy_wh = car_load_energy * interval_hours
            add_load_data_1_energy_wh = add_load_data_1_energy * interval_hours
            managed_load_energy_wh = managed_load_energy * interval_hours

            # sum_controlable_energy_load = car_load_energy + add_load_data_1_energy
            sum_controlable_energy_load_wh = (
                car_load_energy_wh + add_load_data_1_energy_wh + managed_load_energy_wh
            )

            # Save original household sensor value before potential modification
            original_household_energy_wh = energy_wh

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
                    "[LOAD-IF] DATA ERROR household load smaller than controllables (excess: %5.1f Wh) - Energy for %s - household: %5.1f Wh | car: %5.1f Wh + additional: %5.1f Wh + managed: %5.1f Wh | total: %5.1f Wh %s",
                    round(
                        sum_controlable_energy_load_wh - original_household_energy_wh, 1
                    ),
                    current_time_slot,
                    round(original_household_energy_wh, 1),
                    round(car_load_energy_wh, 1),
                    round(add_load_data_1_energy_wh, 1),
                    round(managed_load_energy_wh, 1),
                    round(sum_controlable_energy_load_wh, 1),
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
                    "[LOAD-IF] load = 0 ... DATA ERROR household load smaller than controllables (excess: %5.1f Wh) - Energy for %s - household: %5.1f Wh | car: %5.1f Wh + additional: %5.1f Wh | car+add: %5.1f Wh - debug: %s",
                    round(
                        sum_controlable_energy_load_wh - original_household_energy_wh, 1
                    ),
                    current_time_slot,
                    round(original_household_energy_wh, 1),
                    round(car_load_energy_wh, 1),
                    round(add_load_data_1_energy_wh, 1),
                    round(sum_controlable_energy_load_wh, 1),
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
                "[LOAD-IF] Energy for %s - final: %5.1f Wh (household: %5.1f Wh | car: %5.1f Wh + additional: %5.1f Wh | car+add: %5.1f Wh)",
                current_time_slot,
                round(energy_wh, 1),
                round(original_household_energy_wh, 1),
                round(car_load_energy_wh, 1),
                round(add_load_data_1_energy_wh, 1),
                round(sum_controlable_energy_load_wh, 1),
            )
            current_time_slot += timedelta(seconds=self.time_frame_base)

        if not day_has_data:
            logger.debug(
                "[LOAD-IF] No source data returned for '%s' from %s to %s.",
                self.load_sensor,
                start_time,
                end_time,
            )
            return []

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

        # An all-zero profile can be valid data. Missing history is represented
        # by an empty day profile, so use data availability rather than the
        # numeric value to decide whether the historical fallback is needed.
        historical_profiles = (
            load_profile_one_week_before,
            load_profile_two_week_before,
            load_profile_tomorrow_one_week_before,
            load_profile_tomorrow_two_week_before,
        )
        if not any(historical_profiles):
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
            if yesterday_profile:
                load_profile = yesterday_profile + yesterday_profile
                logger.info(
                    "[LOAD-IF] Using yesterday's consumption pattern doubled"
                    + " for 48-hour forecast"
                )
            else:
                # Nothing from 7 and 14 days ago is normal on a new install; nothing
                # from yesterday either, on a source that is actually connected, is
                # not. Home Assistant answers an unknown entity with 200 and an empty
                # list rather than a 404, so a wrong sensor name produces no error
                # anywhere — it just quietly becomes this synthetic curve, and the
                # dashboard looks plausible while the optimizer runs on fiction.
                # The "| Config:" and "| ACTION REQUIRED" suffixes are what
                # parseAlertMeta() in web/js/main.js turns into the startup-errors
                # panel's badge and deep link.
                if self.src in ("openhab", "homeassistant"):
                    logger.warning(
                        "[LOAD-IF] '%s' returned no consumption data for the last two "
                        "weeks. If this is not a new installation, check that the "
                        "sensor name is correct and that it has recorder history. "
                        "Using the built-in default profile meanwhile. "
                        "| Config: #load | ACTION REQUIRED",
                        self.load_sensor,
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
