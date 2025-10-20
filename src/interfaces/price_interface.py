"""
This module provides the `PriceInterface` class for retrieving and processing electricity price
data from various sources.

Supported sources:
    - Akkudoktor API (default)
    - Tibber API
    - SmartEnergy AT API
    - Stromligning.dk API
    - Fixed 24-hour price array

Features:
    - Fetches and updates current prices for a specified duration and start time.
    - Generates feed-in prices based on configuration.
    - Handles negative price switching and feed-in tariff logic.
    - Provides default fallback prices if external data is unavailable.

Usage:
    config = {
        "source": "tibber",
        "token": "your_access_token",
        "feed_in_tariff_price": 5.0,
        "negative_price_switch": True,
        "fixed_24h_array": [10.0] * 24
    }
    price_interface = PriceInterface(config, timezone="Europe/Berlin")
    price_interface.update_prices(tgt_duration=24, start_time=datetime.now())
    current_prices = price_interface.get_current_prices()
    current_feedin_prices = price_interface.get_current_feedin_prices()
"""

from datetime import datetime, timedelta
from collections import defaultdict
import json
import logging
import threading
import requests


logger = logging.getLogger("__main__")
logger.info("[PRICE-IF] loading module ")

AKKUDOKTOR_API_PRICES = "https://api.akkudoktor.net/prices"
TIBBER_API = "https://api.tibber.com/v1-beta/gql"
SMARTENERGY_API = "https://apis.smartenergy.at/market/v1/price"
STROMLIGNING_API_BASE = "https://stromligning.dk/api/prices?lean=true"


class PriceInterface:
    """
    The PriceInterface class manages electricity price data retrieval and processing from
    various sources.

    Attributes:
        src (str): Source of the price data
                   (e.g., 'tibber', 'stromligning', 'smartenergy_at', 'fixed_24h', 'default').
        access_token (str): Access token for authenticating with the price source.
        fixed_24h_array (list): Optional fixed 24-hour price array.
        feed_in_tariff_price (float): Feed-in tariff price in cents per kWh.
        negative_price_switch (bool): If True, sets feed-in prices to 0 for negative prices.
        time_zone (str): Timezone for date and time operations.
        current_prices (list): Current prices including taxes.
        current_prices_direct (list): Current prices without tax.
        current_feedin (list): Current feed-in prices.
        default_prices (list): Default price list if external data is unavailable.

    Methods:
        update_prices(tgt_duration, start_time):
            Updates current_prices and current_feedin for the given duration and start time.
        get_current_prices():
            Returns the current prices.
        get_current_feedin_prices():
            Returns the current feed-in prices.
        __create_feedin_prices():
            Generates feed-in prices based on current_prices_direct and configuration.
        __retrieve_prices(tgt_duration, start_time=None):
            Dispatches price retrieval to the configured source.
        __retrieve_prices_from_akkudoktor(tgt_duration, start_time=None):
            Fetches prices from the Akkudoktor API.
        __retrieve_prices_from_tibber(tgt_duration, start_time=None):
            Fetches prices from the Tibber API.
        __retrieve_prices_from_smartenergy_at(tgt_duration, start_time=None):
            Fetches prices from the SmartEnergy AT API.
        __retrieve_prices_from_stromligning(tgt_duration, start_time=None):
            Fetches prices from the Stromligning.dk API.
        __retrieve_prices_from_fixed24h_array(tgt_duration, start_time=None):
            Returns prices from a fixed 24-hour array.
    """

    def __init__(
        self,
        config,
        timezone="UTC",
    ):
        self.src = config["source"]
        self.access_token = config.get("token", "")
        self.stromligning_query = config.get("stromligning_url", "")
        self._stromligning_url = None
        self.fixed_price_adder_ct = config.get("fixed_price_adder_ct", 0.0)
        self.relative_price_multiplier = config.get("relative_price_multiplier", 0.0)
        self.fixed_24h_array = config.get("fixed_24h_array", False)
        # for HA addon config - if string, convert to list of floats
        if isinstance(self.fixed_24h_array, str) and self.fixed_24h_array != "":
            self.fixed_24h_array = [
                float(price) for price in self.fixed_24h_array.split(",")
            ]
        elif not isinstance(self.fixed_24h_array, list):
            self.fixed_24h_array = False
        self.feed_in_tariff_price = config.get("feed_in_price", 0.0)
        self.negative_price_switch = config.get("negative_price_switch", False)
        self.time_zone = timezone
        self.current_prices = []
        self.current_prices_direct = []  # without tax
        self.current_feedin = []
        self.default_prices = [0.0001] * 48  # if external data are not available
        self.price_currency = self.__determine_price_currency()

        # Add retry mechanism attributes
        self.last_successful_prices = []
        self.last_successful_prices_direct = []
        self.consecutive_failures = 0
        self.max_failures = 24  # Max consecutive failures before using default prices

        # Background thread attributes
        self._update_thread = None
        self._stop_event = threading.Event()
        self.update_interval = 900  # 15 minutes in seconds

        self.__check_config()  # Validate configuration parameters
        logger.info(
            "[PRICE-IF] Initialized with"
            + " source: %s, feed_in_tariff_price: %s, negative_price_switch: %s",
            self.src,
            self.feed_in_tariff_price,
            self.negative_price_switch,
        )

        # Start the background update service
        self.__start_update_service()

    def __start_update_service(self):
        """
        Starts the background thread to periodically update prices.
        """
        if self._update_thread is None or not self._update_thread.is_alive():
            self._stop_event.clear()
            self._update_thread = threading.Thread(
                target=self.__update_prices_loop, daemon=True
            )
            self._update_thread.start()
            logger.info("[PRICE-IF] Background price update service started")

    def shutdown(self):
        """
        Stops the background thread and shuts down the update service.
        """
        if self._update_thread and self._update_thread.is_alive():
            logger.info("[PRICE-IF] Shutting down background price update service")
            self._stop_event.set()
            self._update_thread.join(timeout=5)
            if self._update_thread.is_alive():
                logger.warning(
                    "[PRICE-IF] Background thread did not shut down gracefully"
                )
            else:
                logger.info("[PRICE-IF] Background price update service stopped")

    def __update_prices_loop(self):
        """
        The loop that runs in the background thread to update prices periodically.
        """
        # Initial update
        try:
            self.update_prices(
                48,
                datetime.now(self.time_zone).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
            )  # Get 48 hours of price data
            logger.info("[PRICE-IF] Initial price update completed")
        except RuntimeError as e:
            logger.error("[PRICE-IF] Error during initial price update: %s", e)

        while not self._stop_event.is_set():
            try:
                # Wait for the update interval or until stop event is set
                if self._stop_event.wait(timeout=self.update_interval):
                    break  # Stop event was set

                # Perform price update
                self.update_prices(
                    48,
                    datetime.now(self.time_zone).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ),
                )  # Get 48 hours of price data
                logger.debug("[PRICE-IF] Periodic price update completed")

            except Exception as e:
                logger.error("[PRICE-IF] Error during periodic price update: %s", e)
                # Continue the loop even if update fails

        # Restart the service if it wasn't intentionally stopped
        if not self._stop_event.is_set():
            logger.warning(
                "[PRICE-IF] Background price update thread stopped unexpectedly, restarting..."
            )
            self.__start_update_service()

    def __check_config(self):
        """
        Checks the configuration for required parameters.

        This function checks if the necessary parameters are present in the configuration.
        If any required parameter is missing, it raises a ValueError.

        Raises:
            ValueError: If any required parameter is missing from the configuration.
        """
        if not self.src:
            self.src = "default"  # Default to 'default' if no source is specified
            logger.error(
                "[PRICE-IF] No source specified in configuration. Defaulting to 'default'."
            )
        if self.src == "tibber" and not self.access_token:
            self.src = "default"  # Fallback to default if no access token is provided
            logger.error(
                "[PRICE-IF] Access token is required for Tibber source but not provided."
                + " Usiung default price source."
            )
        if self.src == "stromligning" and not self.stromligning_query:
            self.src = "default"
            logger.error(
                "[PRICE-IF] stromligning_url must be provided when using the Stromligning source. "
                "Falling back to default prices."
            )
        if self.src == "stromligning":
            query_str = (self.stromligning_query or "").strip().lstrip("?&")
            if not query_str:
                self.src = "default"
                self._stromligning_url = None
                logger.error(
                    "[PRICE-IF] stromligning_url is empty after trimming. "
                    "Falling back to default prices."
                )
            else:
                self._stromligning_url = f"{STROMLIGNING_API_BASE}&{query_str}"
        else:
            self._stromligning_url = None

    def update_prices(self, tgt_duration, start_time=None):
        """
        Updates the current prices and feed-in prices based on the target duration
        and start time provided.

        Args:
            tgt_duration (int): The target duration for which prices need to be retrieved.
            start_time (datetime): The starting time for retrieving prices.

        Updates:
            self.current_prices: Updates with the retrieved prices for the given duration
                                 and start time.
            self.current_feedin: Updates with the generated feed-in prices.

        Logs:
            Logs a debug message indicating that prices have been updated.
        """
        if start_time is None:
            start_time = datetime.now(self.time_zone).replace(
                minute=0, second=0, microsecond=0
            )
        self.current_prices = self.__retrieve_prices(tgt_duration, start_time)
        self.current_feedin = self.__create_feedin_prices()
        logger.debug(
            "[PRICE-IF] Prices updated for %d hours starting from %s",
            tgt_duration,
            start_time.strftime("%Y-%m-%d %H:%M"),
        )

    def get_current_prices(self):
        """
        Returns the current prices.

        This function returns the current prices fetched from the price source.
        If the source is not supported, it returns an empty list.

        Returns:
            list: A list of current prices.
        """
        # logger.debug("[PRICE-IF] Returning current prices: %s", self.current_prices)
        return self.current_prices

    def get_current_feedin_prices(self):
        """
        Returns the current feed-in prices.

        This function returns the current feed-in prices fetched from the price source.
        If the source is not supported, it returns an empty list.

        Returns:
            list: A list of current feed-in prices.
        """
        # logger.debug(
        #     "[PRICE-IF] Returning current feed-in prices: %s", self.current_feedin
        # )
        return self.current_feedin

    def get_price_currency(self):
        """
        Return the currency identifier for the currently configured price source.

        Returns:
            str: ISO 4217 currency code (e.g. 'EUR', 'DKK').
        """
        return self.price_currency

    def __create_feedin_prices(self):
        """
        Creates feed-in prices based on the current prices.

        This function generates feed-in prices based on the current prices and the
        configured feed-in tariff price. If the negative price switch is enabled,
        feed-in prices are set to 0 for negative prices. Otherwise, the feed-in tariff
        price is used for all prices.

        Returns:
            list: A list of feed-in prices.
        """
        if self.negative_price_switch:
            self.current_feedin = [
                0 if price < 0 else round(self.feed_in_tariff_price / 1000, 9)
                for price in self.current_prices_direct
            ]
            logger.debug(
                "[PRICE-IF] Negative price switch is enabled."
                + " Feed-in prices set to 0 for negative prices."
            )
        else:
            self.current_feedin = [
                round(self.feed_in_tariff_price / 1000, 9)
                for _ in self.current_prices_direct
            ]
            logger.debug(
                "[PRICE-IF] Feed-in prices created based on current"
                + " prices and feed-in tariff price."
            )
        return self.current_feedin

    def __retrieve_prices(self, tgt_duration, start_time=None):
        """
        Retrieve prices based on the target duration and optional start time.

        This function fetches prices from different sources based on the configuration.
        It supports fetching prices from 'tibber' and 'default' sources.

        Args:
            tgt_duration (int): The target duration for which prices are to be fetched.
            start_time (datetime, optional): The start time from which prices are to be fetched.
            Defaults to None.

        Returns:
            list: A list of prices for the specified duration and start time. Returns an empty list
            if the price source is not supported.
        """
        prices = []
        if self.src == "tibber":
            prices = self.__retrieve_prices_from_tibber(tgt_duration, start_time)
        elif self.src == "smartenergy_at":
            prices = self.__retrieve_prices_from_smartenergy_at(
                tgt_duration, start_time
            )
        elif self.src == "stromligning":
            prices = self.__retrieve_prices_from_stromligning(tgt_duration, start_time)
        elif self.src == "fixed_24h":
            prices = self.__retrieve_prices_from_fixed24h_array(
                tgt_duration, start_time
            )
        elif self.src == "default":
            prices = self.__retrieve_prices_from_akkudoktor(tgt_duration, start_time)
        else:
            prices = self.default_prices
            self.current_prices_direct = self.default_prices.copy()
            logger.error(
                "[PRICE-IF] Price source currently not supported."
                + " Using default prices (0,10 ct/kWh)."
            )

        if not prices:
            self.consecutive_failures += 1

            if (
                self.consecutive_failures <= self.max_failures
                and len(self.last_successful_prices) > 0  # Changed condition
            ):
                logger.warning(
                    "[PRICE-IF] No prices retrieved (failure %d/%d). Using last successful prices.",
                    self.consecutive_failures,
                    self.max_failures,
                )
                prices = self.last_successful_prices[:tgt_duration]
                self.current_prices_direct = self.last_successful_prices_direct[
                    :tgt_duration
                ]

                # Extend if needed
                if len(prices) < tgt_duration:
                    remaining_hours = tgt_duration - len(prices)
                    prices.extend(self.last_successful_prices[:remaining_hours])
                    self.current_prices_direct.extend(
                        self.last_successful_prices_direct[:remaining_hours]
                    )
            else:
                if len(self.last_successful_prices) == 0:
                    logger.error(
                        "[PRICE-IF] No prices retrieved (failure %d) and no previous"
                        + " successful prices available. Using default prices (0.10 ct/kWh).",
                        self.consecutive_failures,
                    )
                else:
                    logger.error(
                        "[PRICE-IF] No prices retrieved after %d consecutive failures."
                        + " Using default prices (0.10 ct/kWh).",
                        self.consecutive_failures,
                    )
                prices = self.default_prices[:tgt_duration]
                self.current_prices_direct = self.default_prices[:tgt_duration].copy()
        else:
            # Success - reset failure counter and store successful prices
            self.consecutive_failures = 0
            self.last_successful_prices = prices.copy()
            self.last_successful_prices_direct = self.current_prices_direct.copy()
            logger.debug("[PRICE-IF] Prices retrieved successfully. Stored as backup.")

        return prices

    def __determine_price_currency(self):
        """
        Determine the currency used by the configured price source.

        Returns:
            str: ISO 4217 currency code.
        """
        if self.src == "stromligning":
            return "DKK"
        if self.src == "smartenergy_at":
            return "EUR"
        if self.src == "fixed_24h":
            return "EUR"
        if self.src == "tibber":
            # Tibber exposes prices in the account currency; default to EUR.
            return "EUR"
        if self.src == "default":
            return "EUR"
        return "EUR"

    def __retrieve_prices_from_akkudoktor(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices for today and tomorrow.

        This function retrieves electricity prices for today and tomorrow from an API,
        processes the prices, and returns a list of prices for the specified duration starting
        from the specified start time. If tomorrow's prices are not available, today's prices are
        repeated for tomorrow.

        Args:
            tgt_duration (int): The target duration in hours for which the prices are needed.
            start_time (datetime, optional): The start time for fetching prices. Defaults to None.

        Returns:
            list: A list of electricity prices for the specified duration starting
                from the specified start time.
        """
        if self.src != "default":
            logger.error(
                "[PRICE-IF] Price source %s currently not supported. Default prices will be used.",
                self.src,
            )
            return []
        logger.debug("[PRICE-IF] Fetching prices from akkudoktor ...")
        if start_time is None:
            start_time = datetime.now(self.time_zone).replace(
                minute=0, second=0, microsecond=0
            )
        current_hour = start_time.hour
        request_url = (
            AKKUDOKTOR_API_PRICES
            + "?start="
            + start_time.strftime("%Y-%m-%d")
            + "&end="
            + (start_time + timedelta(days=1)).strftime("%Y-%m-%d")
        )
        logger.debug("[PRICE-IF] Requesting prices from akkudoktor: %s", request_url)
        try:
            response = requests.get(request_url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.Timeout:
            logger.error(
                "[PRICE-IF] Request timed out while fetching prices from akkudoktor."
            )
            return []
        except requests.exceptions.RequestException as e:
            logger.error(
                "[PRICE-IF] Request failed while fetching prices from akkudoktor: %s",
                e,
            )
            return []

        prices = []
        for price in data["values"]:
            price_with_fixed = (
                round(price["marketpriceEurocentPerKWh"] / 100000, 9)
                + self.fixed_price_adder_ct / 100000
            )
            price_final = round(
                price_with_fixed * (1 + self.relative_price_multiplier), 9
            )
            prices.append(price_final)

        if start_time is None:
            start_time = datetime.now(self.time_zone).replace(
                minute=0, second=0, microsecond=0
            )
        current_hour = start_time.hour
        extended_prices = prices[current_hour : current_hour + tgt_duration]

        if len(extended_prices) < tgt_duration:
            remaining_hours = tgt_duration - len(extended_prices)
            extended_prices.extend(prices[:remaining_hours])
        logger.debug("[PRICE-IF] Prices from AKKUDOKTOR fetched successfully.")
        self.current_prices_direct = extended_prices.copy()
        return extended_prices

    def __retrieve_prices_from_tibber(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices for today and tomorrow.

        This function retrieves electricity prices for today and tomorrow from a web service,
        processes the prices, and returns a list of prices for the specified duration starting
        from the specified start time. If tomorrow's prices are not available, today's prices are
        repeated for tomorrow.

        Args:
            tgt_duration (int): The target duration in hours for which the prices are needed.
            start_time (datetime, optional): The start time for fetching prices. Defaults to None.

        Returns:
            list: A list of electricity prices for the specified duration starting
                from the specified start time.
        """
        logger.debug("[PRICE-IF] Prices fetching from TIBBER started")
        if self.src != "tibber":
            logger.error(
                "[PRICE-IF] Price source '%s' currently not supported.", self.src
            )
            return []  # Changed from self.default_prices to []
        headers = {
            "Authorization": self.access_token,
            "Content-Type": "application/json",
        }
        query = """
        {
            viewer {
                homes {
                    currentSubscription {
                        priceInfo {
                            today {
                                total
                                energy
                                startsAt
                                currency
                            }
                            tomorrow {
                                total
                                energy
                                startsAt
                            }
                        }
                    }
                }
            }
        }
        """
        try:
            response = requests.post(
                TIBBER_API, headers=headers, json={"query": query}, timeout=10
            )
            response.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error(
                "[PRICE-IF] Request timed out while fetching prices from Tibber."
            )
            return []  # Changed from self.default_prices to []
        except requests.exceptions.RequestException as e:
            logger.error(
                "[PRICE-IF] Request failed while fetching prices from Tibber: %s",
                e,
            )
            return []  # Changed from self.default_prices to []

        response.raise_for_status()
        data = response.json()
        if "errors" in data and data["errors"] is not None:
            logger.error(
                "[PRICE-IF] Error fetching prices - tibber API response: %s",
                data["errors"][0]["message"],
            )
            return []

        today_prices = json.dumps(
            data["data"]["viewer"]["homes"][0]["currentSubscription"]["priceInfo"][
                "today"
            ]
        )
        tomorrow_prices = json.dumps(
            data["data"]["viewer"]["homes"][0]["currentSubscription"]["priceInfo"][
                "tomorrow"
            ]
        )
        try:
            self.price_currency = (
                (
                    data["data"]["viewer"]["homes"][0]["currentSubscription"][
                        "priceInfo"
                    ]["today"][0]["currency"]
                )
                .strip()
                .upper()
            )
        except (KeyError, IndexError, TypeError):
            pass

        today_prices_json = json.loads(today_prices)
        tomorrow_prices_json = json.loads(tomorrow_prices)
        prices = []
        prices_direct = []

        for price in today_prices_json:
            prices.append(round(price["total"] / 1000, 9))
            prices_direct.append(round(price["energy"] / 1000, 9))
            # logger.debug(
            #     "[Main] day 1 - price for %s -> %s", price["startsAt"], price["total"]
            # )
        if tomorrow_prices_json:
            for price in tomorrow_prices_json:
                prices.append(round(price["total"] / 1000, 9))
                prices_direct.append(round(price["energy"] / 1000, 9))
                # logger.debug(
                #     "[Main] day 2 - price for %s -> %s", price["startsAt"], price["total"]
                # )
        else:
            prices.extend(prices[:24])  # Repeat today's prices for tomorrow
            prices_direct.extend(
                prices_direct[:24]
            )  # Repeat today's prices for tomorrow

        if start_time is None:
            start_time = datetime.now(self.time_zone).replace(
                minute=0, second=0, microsecond=0
            )
        current_hour = start_time.hour
        extended_prices = prices[current_hour : current_hour + tgt_duration]
        extended_prices_direct = prices_direct[
            current_hour : current_hour + tgt_duration
        ]

        if len(extended_prices) < tgt_duration:
            remaining_hours = tgt_duration - len(extended_prices)
            extended_prices.extend(prices[:remaining_hours])
            extended_prices_direct.extend(prices_direct[:remaining_hours])
        self.current_prices_direct = extended_prices_direct.copy()
        logger.debug("[PRICE-IF] Prices from TIBBER fetched successfully.")
        return extended_prices

    def __retrieve_prices_from_stromligning(self, tgt_duration, start_time=None):
        logger.debug("[PRICE-IF] Prices fetching from STROMLIGNING started")
        if self.src != "stromligning":
            logger.error(
                "[PRICE-IF] Price source '%s' currently not supported.",
                self.src,
            )
            return []

        if start_time is None:
            start_time = datetime.now(self.time_zone).replace(
                minute=0, second=0, microsecond=0
            )

        if start_time.tzinfo is None and hasattr(self.time_zone, "localize"):
            start_time = self.time_zone.localize(start_time)

        headers = {"accept": "application/json"}
        try:
            response = requests.get(self._stromligning_url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.Timeout:
            logger.error(
                "[PRICE-IF] Request timed out while fetching prices from STROMLIGNING."
            )
            return []
        except requests.exceptions.RequestException as e:
            logger.error(
                "[PRICE-IF] Request failed while fetching prices from STROMLIGNING: %s",
                e,
            )
            return []
        except ValueError as e:
            logger.error(
                "[PRICE-IF] Failed to parse STROMLIGNING response as JSON: %s",
                e,
            )
            return []

        if not isinstance(data, list) or len(data) == 0:
            logger.error("[PRICE-IF] STROMLIGNING API returned no price entries.")
            return []

        tzinfo = start_time.tzinfo
        horizon_end = start_time + timedelta(hours=tgt_duration)

        processed_entries = []
        for entry in data:
            try:
                price_value = float(entry["price"])
                entry_start = entry["date"]
                resolution_value = str(entry.get("resolution", "15m")).lower()
            except (KeyError, TypeError, ValueError):
                logger.debug(
                    "[PRICE-IF] Skipping malformed STROMLIGNING entry: %s", entry
                )
                continue

            try:
                entry_start_dt = datetime.fromisoformat(
                    entry_start.replace("Z", "+00:00")
                )
            except ValueError:
                logger.debug(
                    "[PRICE-IF] Skipping STROMLIGNING entry with invalid datetime: %s",
                    entry_start,
                )
                continue

            if tzinfo is not None:
                entry_start_dt = entry_start_dt.astimezone(tzinfo)

            resolution_map = {"15m": 15, "30m": 30, "60m": 60}
            minutes = resolution_map.get(resolution_value, 15)
            entry_end_dt = entry_start_dt + timedelta(minutes=minutes)

            if entry_end_dt <= start_time or entry_start_dt >= horizon_end:
                continue

            processed_entries.append(
                (entry_start_dt, entry_end_dt, price_value / 1000.0)
            )

        if not processed_entries:
            logger.error(
                "[PRICE-IF] No relevant STROMLIGNING price entries found within horizon."
            )
            return []

        processed_entries.sort(key=lambda item: item[0])

        hourly_prices = []
        current_slot_start = start_time
        coverage_warning = False

        while current_slot_start < horizon_end:
            current_slot_end = current_slot_start + timedelta(hours=1)
            weighted_sum = 0.0
            covered_seconds = 0.0

            for entry_start, entry_end, price_per_wh in processed_entries:
                overlap_start = max(entry_start, current_slot_start)
                overlap_end = min(entry_end, current_slot_end)
                if overlap_start >= overlap_end:
                    continue
                duration = (overlap_end - overlap_start).total_seconds()
                weighted_sum += price_per_wh * duration
                covered_seconds += duration

            if covered_seconds == 0:
                coverage_warning = True
                if hourly_prices:
                    hourly_prices.append(hourly_prices[-1])
                else:
                    hourly_prices.append(processed_entries[0][2])
            else:
                hourly_prices.append(round(weighted_sum / covered_seconds, 9))

            current_slot_start = current_slot_end

        if coverage_warning:
            logger.warning(
                "[PRICE-IF] Incomplete STROMLIGNING price coverage detected; "
                "missing intervals reused the prior value."
            )

        self.current_prices_direct = hourly_prices.copy()
        logger.debug("[PRICE-IF] Prices from STROMLIGNING fetched successfully.")
        return hourly_prices

    def __retrieve_prices_from_smartenergy_at(self, tgt_duration, start_time=None):
        logger.debug("[PRICE-IF] Prices fetching from SMARTENERGY_AT started")
        if self.src != "smartenergy_at":
            logger.error(
                "[PRICE-IF] Price source '%s' currently not supported.",
                self.src,
            )
            return []
        if start_time is None:
            start_time = datetime.now(self.time_zone).replace(
                minute=0, second=0, microsecond=0
            )
        request_url = SMARTENERGY_API
        logger.debug(
            "[PRICE-IF] Requesting prices from SMARTENERGY_AT: %s", request_url
        )
        try:
            response = requests.get(request_url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.Timeout:
            logger.error(
                "[PRICE-IF] Request timed out while fetching prices from SMARTENERGY_AT."
            )
            return []
        except requests.exceptions.RequestException as e:
            logger.error(
                "[PRICE-IF] Request failed while fetching prices from SMARTENERGY_AT: %s",
                e,
            )
            return []

        # Summarize to hourly averages
        hourly = defaultdict(list)
        for entry in data["data"]:
            hour = datetime.fromisoformat(entry["date"]).hour
            hourly[hour].append(entry["value"] / 100000)  # Convert to euro/wh
        # Compute the average for each hour (0-23)
        hourly_prices = []
        for hour in range(24):
            values = hourly.get(hour, [])
            avg = sum(values) / len(values) if values else 0
            hourly_prices.append(round(avg, 9))

        # Optionally extend to tgt_duration if needed
        extended_prices = hourly_prices
        if len(extended_prices) < tgt_duration:
            remaining_hours = tgt_duration - len(extended_prices)
            extended_prices.extend(hourly_prices[:remaining_hours])

        # Catch case where all prices are zero (or data is empty)
        if not any(extended_prices):
            logger.error(
                "[PRICE-IF] SMARTENERGY_AT API returned only zero prices or empty data."
            )
            return []

        logger.debug("[PRICE-IF] Prices from SMARTENERGY_AT fetched successfully.")
        self.current_prices_direct = extended_prices.copy()
        return extended_prices

    def __retrieve_prices_from_fixed24h_array(self, tgt_duration, start_time=None):
        """
        Returns a fixed 24-hour array of prices.

        This function returns a fixed 24-hour array of prices based on the configured
        feed-in tariff price. It is used when the `fixed_24h_array` configuration is set to True.

        Args:
            tgt_duration (int): The target duration for which prices are needed.
            start_time (datetime, optional): The start time for fetching prices. Defaults to None.

        Returns:
            list: A list of fixed prices for the specified duration.
        """
        if not self.fixed_24h_array:
            logger.error(
                "[PRICE-IF] fixed_24h is configured,"
                + " but no 'fixed_24h_array' is provided."
            )
            return []
        if len(self.fixed_24h_array) != 24:
            logger.error("[PRICE-IF] fixed_24h_array must contain exactly 24 entries.")
            return []
        # Convert each entry in fixed_24h_array from ct/kWh to €/Wh (divide by 100000)
        extended_prices = [round(price / 100000, 9) for price in self.fixed_24h_array]
        # Extend to tgt_duration if needed
        if len(extended_prices) < tgt_duration:
            remaining_hours = tgt_duration - len(extended_prices)
            extended_prices.extend(extended_prices[:remaining_hours])
        self.current_prices_direct = extended_prices.copy()
        return extended_prices
