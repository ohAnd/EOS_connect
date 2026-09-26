"""
This module provides the `LoadInterface` class, which is used to fetch and process energy data
from various sources such as OpenHAB and Home Assistant. It also includes methods to create
load profiles based on historical energy consumption data.
"""

from datetime import datetime, timedelta, timezone
import logging
from urllib.parse import quote
import threading
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

    # How long a profile that fell back to the built-in curve is kept before another
    # attempt. Roughly one optimizer interval: long enough that a source which is down
    # is not hammered on every run, short enough that a brief outage at startup does
    # not cost the whole day.
    __PROFILE_RETRY_SECONDS = 300

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
        # It arrives from the caller so this interface stays unaware of what a managed
        # load is - as a callable where the set can change while running, because
        # enabling and disabling a managed load is a hot-reloadable setting and a
        # disabled one stops contributing its forecast the moment it is switched off.
        self.__extra_subtract_source = extra_subtract_sensors
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

        # One fetched day of Home Assistant samples per entity, keyed by entity id.
        # Issue #302: asking the history endpoint once per slot means 24 requests
        # per sensor per day, which times out against a large recorder and drags
        # the other interfaces down with it. The day is fetched once and every
        # slot is then cut out of the cached series locally.
        self.__homeassistant_history_cache = {}
        # unit_of_measurement / device_class per entity. Recorder statistics rows
        # carry no unit, so the statistics fallback has to learn it from the
        # entity itself to return watts like the history path does.
        self.__homeassistant_attribute_cache = {}
        # Set once the recorder.get_statistics action answers 4xx, so an older
        # Home Assistant is asked exactly once instead of once per sensor per day.
        self.__statistics_unsupported = False
        # Same idea for a statistics call that times out, but only for the current
        # profile build - the next one tries again.
        self.__statistics_failed_this_build = False
        # True only while __prefetch_homeassistant_day is fetching, which is the one
        # caller whose result may be cached. Set around a single synchronous call,
        # and the profile rebuild that drives it already holds __profile_lock.
        self.__prefetching = False

        # The finished load profile, and the local calendar day it describes.
        # Every value in it comes from four fixed historical days, so it only
        # changes at midnight - but it used to be rebuilt from scratch on every
        # optimizer run, 480 times a day at the default refresh time.
        self.__profile_cache = None
        self.__profile_day = None
        # The managed load sensors the held profile was built against. They can be
        # switched off while running, and a profile that still has a disabled load
        # subtracted from it understates the household for the rest of the day.
        self.__profile_sensors = None
        # A profile that fell back to the built-in curve is not worth holding until
        # midnight; this is when it may be attempted again.
        self.__profile_retry_after = None
        self.__profile_degraded = False
        # Held across a rebuild so a second caller waits for the result instead of
        # starting a second one. get_load_profile() reaches this from the optimizer
        # loop and, via the managed-load base load, from the load manager.
        self.__profile_lock = threading.Lock()

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
        status_sink=None,
    ):
        """
        Perform an HTTP request with retries and exponential backoff.
        Returns the requests.Response on success, or None on final failure.

        `status_sink`, when given, is a list the final HTTP status is appended to.
        A caller needs it to tell "this endpoint does not exist" from "the network
        was briefly unhappy" — both of which return None.
        """
        # pylint: disable=too-many-arguments,too-many-positional-arguments
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
                # A 4xx is an answer, not a hiccup: the entity does not exist, the
                # token is wrong, or the action is unknown to this Home Assistant
                # version. Retrying five times with backoff only delays startup and
                # fills the log. 429 is the exception - that one does mean "later".
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    if status_sink is not None:
                        status_sink.append(status)
                    self.__log_request_failure(
                        url, self.max_retries, self.max_retries, e, item_label
                    )
                    return None
                self.__log_request_failure(
                    url, attempt, self.max_retries, e, item_label
                )
                if attempt == self.max_retries:
                    return None
                sleep_seconds = self.retry_backoff * (2 ** (attempt - 1))
                sleep_seconds = sleep_seconds + random.uniform(0, sleep_seconds * 0.5)
                time.sleep(sleep_seconds)

    @property
    def extra_subtract_sensors(self):
        """Power sensors of the managed loads currently contributing a forecast.

        Resolved on every read rather than captured at construction: a managed load
        can be disabled from the web UI without a restart, and from that moment its
        prediction is no longer added on top - so its history must stop being taken
        out of the base load too, or the household is left looking lighter than it is.
        """
        source = self.__extra_subtract_source
        if callable(source):
            try:
                source = source()
            except (TypeError, ValueError, AttributeError, KeyError) as e:
                logger.warning(
                    "[LOAD-IF] Could not read the managed load sensors (%s); "
                    "leaving the base load untouched this time.",
                    e,
                )
                return []
        return [
            str(sensor).strip()
            for sensor in (source or [])
            if str(sensor or "").strip()
        ]

    def __now(self):
        """Current local time, in the frame the load profile is built in.

        Naive when no time zone is configured, which is what the day boundaries
        below have always used. One place for it so a test can move the clock
        without patching datetime for the whole module.
        """
        if self.time_zone is None:
            return datetime.now()
        return datetime.now(self.time_zone)

    def __to_utc(self, value):
        """Return `value` as a timezone-aware UTC instant, or None.

        Everything in the Home Assistant path - cache bounds, slot boundaries,
        sample ordering - is compared as UTC. Mixing naive and aware datetimes is
        what lets a `time_zone: UTC` setup on a host in another zone shift every
        slot boundary without anyone noticing, and it is also why the repeated
        hour at the end of DST cannot be resolved by wall clock alone.

        A naive datetime is the caller's local wall clock: the configured
        `time_zone` if there is one, otherwise the host's.
        """
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            if self.time_zone is None:
                # A naive datetime is local time; astimezone() attaches the host zone.
                value = value.astimezone()
            elif hasattr(self.time_zone, "localize"):
                # pytz: replace(tzinfo=...) would attach the zone's LMT offset.
                value = self.time_zone.localize(value)
            else:
                value = value.replace(tzinfo=self.time_zone)
        return value.astimezone(timezone.utc)

    @staticmethod
    def __as_float(value):
        """Parse a sensor/statistics value, or None when it is not a number."""
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(number) else number

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

    # --- Home Assistant -----------------------------------------------------
    #
    # The day is fetched once per sensor and every slot is then cut out of that
    # cached series (issue #302). The slice has to answer a slot exactly the way a
    # per-slot request to the history endpoint used to, otherwise the profile
    # handed to the optimizer changes - the caching is meant to save requests, not
    # to produce different numbers.

    def __homeassistant_headers(self):
        """Auth headers for the Home Assistant REST API."""
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

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
            list: A list of historical state changes for the entity, states in W.

        Served from the cached day series when `__prefetch_homeassistant_day` has
        already fetched a range covering this interval; otherwise the state
        history endpoint is queried, with recorder statistics as the fallback for
        sensors whose state history has been purged.
        """
        if entity_id == "" or entity_id is None:
            return []

        start_utc = self.__to_utc(start_time)
        end_utc = self.__to_utc(end_time)
        if start_utc is None or end_utc is None:
            return []

        cached = self.__cached_samples(entity_id, start_utc, end_utc)
        if cached is not None:
            return self.__slice_series(cached, entity_id, start_utc, end_utc)

        samples = self.__fetch_homeassistant_series(entity_id, start_utc, end_utc)
        if samples and self.__prefetching:
            # Only the day prefetch fills the cache - see __prefetch_homeassistant_day.
            self.__homeassistant_history_cache[entity_id] = {
                "start": start_utc,
                "end": end_utc,
                "samples": samples,
            }
        if not samples:
            # An absent interval is an ordinary data-quality condition here: it is
            # one slot of many, the caller handles an empty result, and the
            # aggregate complaint is raised once in __create_load_profile_weekdays.
            logger.debug(
                "[LOAD-IF] HOMEASSISTANT - No history or recorder statistics for "
                "'%s' from %s to %s.",
                entity_id,
                start_time,
                end_time,
            )
            return []

        # Whatever Home Assistant returned for the range it was asked for is the
        # answer, unfiltered - the same bytes the per-slot request produced before
        # the cache existed. Only a slice taken out of a *cached* series has to
        # reconstruct the interval boundaries itself.
        return self.__convert_history_units(
            [dict(sample) for _, sample in samples], entity_id
        )

    def __cached_samples(self, entity_id, start_utc, end_utc):
        """Cached (utc, sample) pairs covering the interval, or None."""
        entry = self.__homeassistant_history_cache.get(entity_id)
        if not entry:
            return None
        if entry["start"] <= start_utc and entry["end"] >= end_utc:
            return entry["samples"]
        return None

    def __fetch_homeassistant_series(self, entity_id, start_utc, end_utc):
        """Fetch one range from Home Assistant as sorted (utc, sample) pairs.

        State history is preferred: it has the original sample resolution and the
        attributes the unit conversion needs. When it is missing or too short to
        integrate, recorder statistics stand in - 5-minute buckets first, hourly
        long-term statistics second. Long-term statistics are never purged, so
        they still cover the optimizer's 14-day look-back when state history for
        that day is long gone.
        """
        samples = self.__fetch_homeassistant_history(entity_id, start_utc, end_utc)
        if len(samples) >= 2:
            return samples

        short_term = self.__fetch_statistics(entity_id, start_utc, end_utc, "5minute")
        if self.__covers(short_term, start_utc, end_utc):
            logger.info(
                "[LOAD-IF] HOMEASSISTANT - Using 5-minute statistics for '%s' (%d samples).",
                entity_id,
                len(short_term),
            )
            return short_term

        hourly = self.__fetch_statistics(entity_id, start_utc, end_utc, "hour")
        if hourly:
            logger.info(
                "[LOAD-IF] HOMEASSISTANT - Using hourly statistics for '%s' (%d samples).",
                entity_id,
                len(hourly),
            )
            return hourly
        return short_term or samples

    @staticmethod
    def __covers(samples, start_utc, end_utc):
        """True when the samples reach both ends of the requested interval."""
        return bool(samples) and samples[0][0] <= start_utc and samples[-1][0] >= end_utc

    def __fetch_homeassistant_history(self, entity_id, start_utc, end_utc):
        """State history from /api/history/period as sorted (utc, sample) pairs."""
        response = self.__request_with_retries(
            "get",
            f"{self.url}/api/history/period/{start_utc.isoformat()}",
            params={
                "filter_entity_id": entity_id,
                "end_time": end_utc.isoformat(),
            },
            headers=self.__homeassistant_headers(),
            item_label=entity_id,
        )
        if response is None:
            # Do not log error here; already logged in __request_with_retries
            return []
        try:
            historical_data = response.json()
        except (ValueError, TypeError):
            historical_data = None
        if not historical_data:
            return []

        samples = []
        try:
            for sublist in historical_data:
                for entry in sublist:
                    stamp = self.__to_utc(entry["last_updated"])
                    if stamp is None:
                        continue
                    samples.append(
                        (
                            stamp,
                            {
                                "state": entry["state"],
                                "last_updated": entry["last_updated"],
                                "attributes": entry.get("attributes", {}),
                            },
                        )
                    )
        except (ValueError, KeyError, TypeError):
            logger.error(
                "[LOAD-IF] HOMEASSISTANT - Failed to process energy data for '%s'.",
                entity_id,
            )
            return []

        samples.sort(key=lambda pair: pair[0])
        return samples

    def __entity_attributes(self, entity_id):
        """Attributes of an entity, fetched once per process."""
        if entity_id in self.__homeassistant_attribute_cache:
            return self.__homeassistant_attribute_cache[entity_id]

        attributes = {}
        response = self.__request_with_retries(
            "get",
            f"{self.url}/api/states/{entity_id}",
            headers=self.__homeassistant_headers(),
            item_label=f"{entity_id} (attributes)",
        )
        if response is not None:
            try:
                attributes = response.json().get("attributes", {}) or {}
            except (ValueError, TypeError, AttributeError):
                attributes = {}
        self.__homeassistant_attribute_cache[entity_id] = attributes
        return attributes

    def __statistics_unit_factor(self, entity_id):
        """Multiplier that brings statistics values to W / Wh.

        Statistics rows carry no unit, so it comes from the entity. The same
        factor serves `mean` (kW to W) and `change` (kWh to Wh) because the unit
        itself says which of the two the row holds.
        """
        unit = str(
            self.__entity_attributes(entity_id).get("unit_of_measurement", "")
        ).strip()
        return 1000.0 if unit in ("kW", "kWh") else 1.0

    def __fetch_statistics(self, entity_id, start_utc, end_utc, period):
        """Recorder statistics for a range, as sorted (utc, sample) pairs in W."""
        if self.__statistics_unsupported or self.__statistics_failed_this_build:
            return []

        # Home Assistant omits the bucket that starts exactly at end_time, which
        # is the one covering the last slot of the day. Ask for one period more
        # and clip the result back to the requested interval.
        padding = timedelta(hours=1) if period == "hour" else timedelta(minutes=5)
        refused = []
        response = self.__request_with_retries(
            "post",
            f"{self.url}/api/services/recorder/get_statistics",
            params={"return_response": "true"},
            headers=self.__homeassistant_headers(),
            timeout=max(self.request_timeout, 30),
            item_label=f"{entity_id} ({period} statistics)",
            json_data={
                "statistic_ids": [entity_id],
                "start_time": start_utc.isoformat(),
                "end_time": (end_utc + padding).isoformat(),
                "period": period,
                "types": ["mean", "change", "state"],
            },
            status_sink=refused,
        )
        if response is None:
            # A 4xx means this Home Assistant has no recorder.get_statistics action at
            # all, so stop asking for the rest of the process. Anything else - a
            # timeout, most likely the same overloaded recorder this whole change is
            # about - stays retryable, but not within this build: the fallback is
            # reached once per sensor per day, and eight 30-second timeouts in a row
            # would turn one unreachable Home Assistant into a stalled startup.
            if refused:
                logger.info(
                    "[LOAD-IF] HOMEASSISTANT - recorder.get_statistics is unavailable "
                    "(HTTP %s); continuing with state history only.",
                    refused[0],
                )
                self.__statistics_unsupported = True
            else:
                self.__statistics_failed_this_build = True
            return []

        try:
            rows = (
                response.json()
                .get("service_response", {})
                .get("statistics", {})
                .get(entity_id, [])
            )
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "[LOAD-IF] HOMEASSISTANT - Invalid %s statistics response for '%s'.",
                period,
                entity_id,
            )
            return []

        return self.__statistics_rows_to_samples(entity_id, rows, start_utc, end_utc)

    def __statistics_row_power(self, row, factor, hours, previous_state):
        """Average power for one statistics bucket, and the meter reading to carry.

        Order matters. A total_increasing sensor has no `mean` but does have both
        `state` and `change`; taking `state` is how a 100 kWh meter reading turns
        into 100 kW. `state` is only meaningful as a difference between buckets,
        which is why the previous one is threaded through.
        """
        state = self.__as_float(row.get("state"))
        value = self.__as_float(row.get("mean"))
        if value is not None:
            value *= factor
        else:
            change = self.__as_float(row.get("change"))
            if change is not None:
                value = change * factor / hours
            elif state is not None and previous_state is not None:
                value = (state - previous_state) * factor / hours
        return value, state if state is not None else previous_state

    def __statistics_rows_to_samples(self, entity_id, rows, start_utc, end_utc):
        """Turn statistics buckets into the sample shape the history path produces.

        `mean` is the average power over a measurement bucket. `change` is the
        energy accumulated in the bucket and divides into an average power. `state`
        is the meter reading at the end of the bucket - usable only as a difference
        between consecutive buckets, never as a value in its own right, which is
        what made a 100 kWh counter arrive at the optimizer as 100 kW.
        """
        factor = self.__statistics_unit_factor(entity_id)
        deduped = {}
        previous_state = None

        for row in rows or []:
            if not isinstance(row, dict):
                continue
            row_start = self.__to_utc(row.get("start"))
            row_end = self.__to_utc(row.get("end"))
            if row_start is None or row_end is None or row_end <= row_start:
                continue

            hours = (row_end - row_start).total_seconds() / 3600.0
            value, previous_state = self.__statistics_row_power(
                row, factor, hours, previous_state
            )
            if value is None:
                continue

            if row_end <= start_utc or row_start >= end_utc:
                continue
            clipped_start = max(row_start, start_utc)
            clipped_end = min(row_end, end_utc)
            if clipped_end <= clipped_start:
                continue

            # A bucket is a constant value across its span. Two samples, one at
            # each edge, give __process_energy_data the same timestamped shape the
            # history path produces and preserve the bucket's average exactly.
            for stamp in (clipped_start, clipped_end):
                deduped[stamp] = {
                    "state": value,
                    "last_updated": stamp.isoformat(),
                    "attributes": {},
                }

        return [(stamp, deduped[stamp]) for stamp in sorted(deduped)]

    def __slice_series(self, samples, entity_id, start_utc, end_utc):
        """Cut one slot out of a cached day series.

        Home Assistant opens every history period with the state in effect at
        `start_time`, and holds the last state to the end of the period. A plain
        timestamp filter does neither, which is what left the last slot of a day
        holding a single sample - no duration to integrate, so 0 Wh - and what the
        hard-coded 23:00 special case was patching around. Reconstructing both
        edges instead covers every slot, at any `time_frame_base`.
        """
        before = None
        inside = []
        for stamp, sample in samples:
            if stamp < start_utc:
                before = sample
            elif stamp <= end_utc:
                inside.append((stamp, sample))

        # Copies throughout: the unit conversion below rewrites "state" in place,
        # and adjacent slots share their boundary sample with the cached series.
        window = [dict(sample) for _, sample in inside]
        if before is not None and (not inside or inside[0][0] > start_utc):
            opening = dict(before)
            opening["last_updated"] = start_utc.isoformat()
            window.insert(0, opening)
        if not window:
            return []

        # Convert before padding: for an energy counter the conversion is a delta
        # across the samples it is given, so a synthetic closing sample repeating
        # the last meter reading would flatten the slot's rate.
        window = self.__convert_history_units(window, entity_id)
        if not window:
            return []

        last_stamp = self.__to_utc(window[-1]["last_updated"])
        if last_stamp is not None and last_stamp < end_utc:
            closing = dict(window[-1])
            closing["last_updated"] = end_utc.isoformat()
            window.append(closing)
        return window

    def __convert_history_units(self, history_values, entity_id):
        """Bring history samples to W, exactly as the per-slot path always did.

        An energy counter becomes the average power over the samples given; a kW
        reading is scaled to W. Operates on the caller's list, which must already
        be a copy of anything held in the cache.
        """
        if not history_values:
            return []

        try:
            first_attributes = history_values[0].get("attributes") or {}
            if first_attributes.get("device_class") == "energy":
                start_idx = 0
                end_idx = len(history_values) - 1
                while start_idx < end_idx:
                    if self.__as_float(history_values[start_idx].get("state")) is not None:
                        break
                    start_idx += 1
                while start_idx < end_idx:
                    if self.__as_float(history_values[end_idx].get("state")) is not None:
                        break
                    end_idx -= 1

                first_state = float(history_values[start_idx]["state"])
                last_state = float(history_values[end_idx]["state"])
                first_time = datetime.fromisoformat(
                    history_values[start_idx]["last_updated"]
                )
                last_time = datetime.fromisoformat(
                    history_values[end_idx]["last_updated"]
                )
                duration_hours = (last_time - first_time).total_seconds() / 3600.0

                if duration_hours > 0:
                    # Counter resets must not read as negative consumption.
                    power_w = max(0.0, (last_state - first_state) / duration_hours)
                else:
                    power_w = 0.0

                history_values = [
                    {**history_values[start_idx], "state": power_w},
                    {**history_values[end_idx], "state": power_w},
                ]

            if history_values:
                unit = (history_values[0].get("attributes") or {}).get(
                    "unit_of_measurement"
                )
                if unit == "kW":
                    for entry in history_values:
                        value = self.__as_float(entry.get("state"))
                        if value is not None:
                            entry["state"] = value * 1000

            return history_values
        except (ValueError, KeyError, TypeError):
            logger.error(
                "[LOAD-IF] HOMEASSISTANT - Failed to process energy data for '%s'.",
                entity_id,
            )
            return []

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
        """Fetch one whole day up front so the slot loop stays local.

        The profile is still built slot by slot, but the day is requested once and
        every slot is then cut out of the cached series. This is the only writer of
        that cache: it is the only caller that asks for a complete, finished day.
        A rolling window ending at "now" - what `fetch_historical_energy_data` gets
        from the battery price handler - must keep going out to Home Assistant, or it
        would be answered from a frozen snapshot. That is why the fetch below is the
        only one allowed to fill the cache, flagged rather than passed as an argument
        so the normal fetch entry point stays the single place a range is requested.
        """
        if not entity_id or self.src != "homeassistant":
            return

        start_utc = self.__to_utc(start_time)
        end_utc = self.__to_utc(end_time)
        if start_utc is None or end_utc is None:
            return
        if self.__cached_samples(entity_id, start_utc, end_utc) is not None:
            return

        # Drop the previous day before refetching. Nothing is written when a fetch
        # comes back empty, so a stale entry left here would answer this day's
        # coverage check with the wrong day's data.
        self.__homeassistant_history_cache.pop(entity_id, None)

        logger.debug(
            "[LOAD-IF] HOMEASSISTANT - Prefetching '%s' for %s to %s once for the complete day.",
            entity_id,
            start_time,
            end_time,
        )
        self.__prefetching = True
        try:
            self.__fetch_historical_energy_data_from_homeassistant(
                entity_id, start_time, end_time
            )
        finally:
            self.__prefetching = False

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

        # Fetch every Home Assistant sensor this day needs exactly once. The slot
        # loop below then reads from the local cache, instead of the 24 requests
        # per sensor that made a large recorder time out (issue #302). Managed
        # loads belong in here too: they are subtracted per slot further down, so
        # leaving them out would keep the request storm for anyone using them.
        if self.src == "homeassistant":
            entities = [
                self.load_sensor,
                self.car_charge_load_sensor,
                self.additional_load_1_sensor,
                *self.extra_subtract_sensors,
            ]
            for entity_id in dict.fromkeys(entity for entity in entities if entity):
                self.__prefetch_homeassistant_day(entity_id, start_time, end_time)

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
        now = self.__now()
        self.__statistics_failed_this_build = False

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

        # Check if load profile contains useful values (not all zeros). An all-zero
        # profile is how a source that answers but has nothing to say looks — Home
        # Assistant returns 200 and an empty list for an entity that does not exist
        # — so it has to keep triggering the fallback and the warning below.
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
                # Nothing was read. Recorded so the caller knows this result is not
                # worth holding until midnight the way a real profile is.
                self.__profile_degraded = True
                logger.info(
                    "[LOAD-IF] Temporary default profile active -"
                    + " will improve with collected data"
                )

        # The four days of raw samples have served their purpose. Nothing until the
        # next rebuild reads them, and they are the largest thing this interface holds.
        self.__homeassistant_history_cache.clear()
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

        The profile itself is built once per day and held; see
        `refresh_load_profile`. This returns a copy of it.
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
            return self.refresh_load_profile()

        logger.error(
            "[LOAD-IF] Load source '%s' currently not supported. Using default.",
            self.src,
        )
        return self._get_default_profile()[:tgt_duration]

    def refresh_load_profile(self, force=False):
        """
        The current load profile, rebuilt from the recorder only when it has to be.

        Every value comes from four fixed historical days, so the profile describes a
        calendar day and changes only at midnight. Rebuilding it on each optimizer run
        re-read the same finished days hundreds of times a day and re-ran the whole
        per-slot aggregation on top.

        Call it once at startup so the first optimizer run finds a profile ready; after
        that `get_load_profile` keeps it current on its own.

        Args:
            force (bool): Rebuild even if the held profile is still valid.

        Returns:
            list: A copy of the profile. Callers adjust what they get back - the EOS
            request builder discounts the in-progress slot - and handing out the held
            list itself would let that accumulate, shrinking the slot on every run.
        """
        if self.src not in ("openhab", "homeassistant") or not self.load_sensor:
            return self._get_default_profile()

        with self.__profile_lock:
            if force or self.__profile_needs_rebuild():
                self.__profile_degraded = False
                profile = self.__create_load_profile_weekdays()
                now = self.__now()
                self.__profile_cache = profile
                self.__profile_day = now.date()
                self.__profile_sensors = tuple(self.extra_subtract_sensors)
                # A profile built from real history stands until midnight. One that
                # fell back to the built-in curve gets another attempt shortly, so a
                # source that was briefly unreachable at startup does not freeze the
                # optimizer onto a synthetic curve for the rest of the day.
                self.__profile_retry_after = (
                    now + timedelta(seconds=self.__PROFILE_RETRY_SECONDS)
                    if self.__profile_degraded
                    else None
                )
            return list(self.__profile_cache)

    def __profile_needs_rebuild(self):
        """True when the held profile is missing, stale, degraded, or out of date."""
        if self.__profile_cache is None or self.__profile_day is None:
            return True
        if tuple(self.extra_subtract_sensors) != self.__profile_sensors:
            # A managed load was enabled or disabled. Its history is subtracted from
            # the profile, so the held one no longer describes the same household.
            return True
        now = self.__now()
        if now.date() != self.__profile_day:
            return True
        if self.__profile_retry_after is not None and now >= self.__profile_retry_after:
            return True
        return False

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
