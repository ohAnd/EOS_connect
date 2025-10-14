"""
This module provides the `PriceInterface` class for retrieving and processing electricity price
data from various sources.

Supported sources:
    - Akkudoktor API (default)
    - Tibber API
    - SmartEnergy AT API
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
        "fixed_24h_array": [10.0] * 24,
        "use_cache": False,
        "cache_expiry_minutes": 60
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
import requests

logger = logging.getLogger("__main__")
logger.info("[PRICE-IF] loading module ")

AKKUDOKTOR_API_PRICES = "https://api.akkudoktor.net/prices"
TIBBER_API = "https://api.tibber.com/v1-beta/gql"
SMARTENERGY_API = "https://apis.smartenergy.at/market/v1/price"


class PriceInterface:
    """
    The PriceInterface class manages electricity price data retrieval and processing from
    various sources.

    Attributes:
        src (str): Source of the price data
                   (e.g., 'tibber', 'default', 'smartenergy_at', 'fixed_24h').
        access_token (str): Access token for authenticating with the price source.
        fixed_24h_array (list): Optional fixed 24-hour price array.
        feed_in_tariff_price (float): Feed-in tariff price in cents per kWh.
        negative_price_switch (bool): If True, sets feed-in prices to 0 for negative prices.
        use_cache (bool): If True, caches successful Tibber API responses (default: True).
        cache_expiry_minutes (int): Cache validity duration in minutes (default: 60).
        time_zone (str): Timezone for date and time operations.
        current_prices (list): Current prices including taxes.
        current_prices_direct (list): Current prices without tax.
        current_feedin (list): Current feed-in prices.
        default_prices (list): Default price list if external data is unavailable.
        tibber_cache (dict): Cache storage for Tibber prices.
        tibber_cache_timestamp (datetime): Timestamp of last successful Tibber fetch.

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
        self.use_cache = config.get("use_cache", False)
        self.cache_expiry_minutes = config.get("cache_expiry_minutes", 60)
        self.time_zone = timezone
        self.current_prices = []
        self.current_prices_direct = []  # without tax
        self.current_feedin = []
        self.default_prices = [0.0001] * 48  # if external data are not available
        # Cache for Tibber prices
        self.tibber_cache = None
        self.tibber_cache_timestamp = None

        self.__check_config()  # Validate configuration parameters
        logger.info(
            "[PRICE-IF] Initialized with"
            + " source: %s, feed_in_tariff_price: %s, negative_price_switch: %s,"
            + " use_cache: %s, cache_expiry_minutes: %s",
            self.src,
            self.feed_in_tariff_price,
            self.negative_price_switch,
            self.use_cache,
            self.cache_expiry_minutes,
        )

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

    def update_prices(self, tgt_duration, start_time):
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
        self.current_prices = self.__retrieve_prices(tgt_duration, start_time)
        self.current_feedin = self.__create_feedin_prices()
        logger.info("[PRICE-IF] Prices updated")

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

    def __is_cache_valid(self):
        """
        Checks if the Tibber price cache is still valid.

        Returns:
            bool: True if cache exists and hasn't expired, False otherwise.
        """
        if not self.use_cache or self.tibber_cache is None or self.tibber_cache_timestamp is None:
            return False

        elapsed_minutes = (datetime.now() - self.tibber_cache_timestamp).total_seconds() / 60
        return elapsed_minutes < self.cache_expiry_minutes

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
            logger.error(
                "[PRICE-IF] No prices retrieved. Using default prices (0,10 ct/kWh)."
            )
            prices = self.default_prices
            self.current_prices_direct = self.default_prices.copy()

        return prices

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
            return self.default_prices
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
                + " Default prices will be used."
            )
            self.current_prices_direct = self.default_prices.copy()
            return self.default_prices
        except requests.exceptions.RequestException as e:
            logger.error(
                "[PRICE-IF] Request failed while fetching prices from akkudoktor: %s"
                + " Default prices will be used.",
                e,
            )
            self.current_prices_direct = self.default_prices.copy()
            return self.default_prices

        prices = []
        for price in data["values"]:
            prices.append(round(price["marketpriceEurocentPerKWh"] / 100000, 9))
            # logger.debug(
            #     "[Main] day 1 - price for %s -> %s", price["marketpriceEurocentPerKWh"],
            #       price["start"]
            # )

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
            return self.default_prices
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
            if self.__is_cache_valid():
                logger.warning(
                    "[PRICE-IF] Request timed out while fetching prices from Tibber."
                    + " Using cached prices."
                )
                self.current_prices_direct = self.tibber_cache["prices_direct"].copy()
                return self.tibber_cache["prices"]
            logger.error(
                "[PRICE-IF] Request timed out while fetching prices from Tibber."
                + " No valid cache available. Default prices will be used."
            )
            self.current_prices_direct = self.default_prices.copy()
            return self.default_prices
        except requests.exceptions.RequestException as e:
            if self.__is_cache_valid():
                logger.warning(
                    "[PRICE-IF] Request failed while fetching prices from Tibber: %s"
                    + " Using cached prices.",
                    e,
                )
                self.current_prices_direct = self.tibber_cache["prices_direct"].copy()
                return self.tibber_cache["prices"]
            logger.error(
                "[PRICE-IF] Request failed while fetching prices from Tibber: %s"
                + " No valid cache available. Default prices will be used.",
                e,
            )
            self.current_prices_direct = self.default_prices.copy()
            return self.default_prices

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

        # Cache the successful result
        if self.use_cache:
            self.tibber_cache = {
                "prices": extended_prices.copy(),
                "prices_direct": extended_prices_direct.copy()
            }
            self.tibber_cache_timestamp = datetime.now()
            logger.debug("[PRICE-IF] Tibber prices cached successfully.")

        logger.debug("[PRICE-IF] Prices from TIBBER fetched successfully.")
        return extended_prices

    def __retrieve_prices_from_smartenergy_at(self, tgt_duration, start_time=None):
        logger.debug("[PRICE-IF] Prices fetching from SMARTENERGY_AT started")
        if self.src != "smartenergy_at":
            logger.error(
                "[PRICE-IF] Price source '%s' currently not supported."
                + " Default prices will be used.",
                self.src,
            )
            return self.default_prices
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
                + " Default prices will be used."
            )
            return self.default_prices
        except requests.exceptions.RequestException as e:
            logger.error(
                "[PRICE-IF] Request failed while fetching prices from SMARTENERGY_AT: %s"
                + " Default prices will be used.",
                e,
            )
            return self.default_prices

        # Summarize to hourly averages
        hourly = defaultdict(list)
        for entry in data["data"]:
            # Parse the hour from the ISO date string
            hour = datetime.fromisoformat(entry["date"]).hour
            hourly[hour].append(entry["value"] / 100000)  # Convert to euro/wh
        # Compute the average for each hour (0-23)
        hourly_prices = []
        for hour in range(24):
            values = hourly.get(hour, [])
            avg = sum(values) / len(values) if values else 0
            hourly_prices.append(round(avg, 6))

        # Optionally extend to tgt_duration if needed
        extended_prices = hourly_prices
        if len(extended_prices) < tgt_duration:
            remaining_hours = tgt_duration - len(extended_prices)
            extended_prices.extend(hourly_prices[:remaining_hours])

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
        # if start_time is None:
        #     start_time = datetime.now(self.time_zone).replace(
        #         minute=0, second=0, microsecond=0
        #     )
        # current_hour = start_time.hour
        if not self.fixed_24h_array:
            logger.error(
                "[PRICE-IF] fixed_24h is configured,"
                + " but no 'fixed_24h_array' is provided."
                + " Default prices will be used."
            )
            self.current_prices_direct = self.default_prices
            return self.default_prices
        if len(self.fixed_24h_array) != 24:
            logger.error(
                "[PRICE-IF] fixed_24h_array must contain exactly 24 entries."
                + " Default prices will be used."
            )
            self.current_prices_direct = self.default_prices
            return self.default_prices
        # Convert each entry in fixed_24h_array from ct/kWh to €/Wh (divide by 100000)
        extended_prices = [round(price / 100000, 9) for price in self.fixed_24h_array]
        # Extend to tgt_duration if needed
        if len(extended_prices) < tgt_duration:
            remaining_hours = tgt_duration - len(extended_prices)
            extended_prices.extend(extended_prices[:remaining_hours])
        self.current_prices_direct = extended_prices.copy()
        return extended_prices
