# -*- coding: utf-8 -*-
"""
This module provides the `PriceInterface` class for retrieving and processing electricity price
data from various sources.

Supported sources:
    - Akkudoktor API (default)
    - Tibber API
    - SmartEnergy AT API
    - Stromligning.dk API
    - Energyforecast.de API
    - Fixed 24-hour price array

Features:
    - Fetches and updates current prices for a specified duration and start time.
    - Generates feed-in prices based on configuration.
    - Handles negative price switching and feed-in tariff logic.
    - Provides default fallback prices if external data is unavailable.
    - Background thread for periodic price updates with retry and fallback logic.
    - Supports both hourly and 15-minute intervals.

Usage:
    config = {
        "source": "tibber",
        "token": "your_access_token",
        "feed_in_price": 5.0,
        "negative_price_switch": True,
        "fixed_24h_array": [10.0] * 24
    }
    price_interface = PriceInterface(config, time_frame_base=3600, timezone="Europe/Berlin")
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
ENERGYFORECAST_API = "https://www.energyforecast.de/api/v1/predictions/next_48_hours"

# Energyforecast smart price prediction constants
ENERGYFORECAST_MIN_OVERLAP_HOURS = 6  # Minimum overlapping hours needed for learning
ENERGYFORECAST_MAX_FACTOR = 5.0  # Maximum allowed multiplicative factor
ENERGYFORECAST_MIN_FACTOR = 0.5  # Minimum allowed multiplicative factor
ENERGYFORECAST_MAX_OFFSET_CT = 50.0  # Maximum allowed offset in ct/kWh


class PriceInterface:
    """
    The PriceInterface class manages electricity price data retrieval and processing from
    various sources.

    Attributes:
        src (str): Source of the price data
                   (e.g., 'tibber', 'stromligning', 'smartenergy_at', 'fixed_24h', 'default').
        access_token (str): Access token for authenticating with the price source.
        fixed_24h_array (list): Optional fixed 24-hour price array (ct/kWh).
        feed_in_tariff_price (float): Feed-in tariff price in ct/kWh.
        negative_price_switch (bool): If True, sets feed-in prices to 0 for negative prices.
        time_zone (str): Timezone for date and time operations.
        current_prices (list): Current prices including taxes (EUR/Wh).
        current_prices_direct (list): Current prices without tax (EUR/Wh).
        current_feedin (list): Current feed-in prices (EUR/Wh).
        default_prices (list): Default price list if external data is unavailable (EUR/Wh).

    Methods:
        update_prices(tgt_duration, start_time):
            Updates current_prices and current_feedin for the given duration and start time.
        get_current_prices():
            Returns the current prices (EUR/Wh).
        get_current_feedin_prices():
            Returns the current feed-in prices (EUR/Wh).
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
        time_frame_base,
        timezone="UTC",
    ):
        self.src = config["source"]
        self.access_token = config.get("token", "")
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

        # Energyforecast.de smart price prediction configuration
        self.energyforecast_enabled = config.get("energyforecast_enabled", False)
        self.energyforecast_token = config.get("energyforecast_token", "demo_token")
        self.energyforecast_market_zone = config.get(
            "energyforecast_market_zone", "DE-LU"
        )

        self.time_frame_base = time_frame_base
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
        if self.src == "stromligning":
            try:
                (
                    supplier_id,
                    product_id,
                    customer_group_id,
                ) = self._parse_stromligning_token(self.access_token)
            except ValueError as exc:
                self.src = "default"
                self._stromligning_url = None
                logger.error(
                    "[PRICE-IF] Invalid Stromligning token: %s. Falling back to default prices.",
                    exc,
                )
            else:
                query_parts = [
                    f"productId={product_id}",
                    f"supplierId={supplier_id}",
                ]
                if customer_group_id:
                    query_parts.append(f"customerGroupId={customer_group_id}")
                self._stromligning_url = (
                    f"{STROMLIGNING_API_BASE}&{'&'.join(query_parts)}"
                )
        else:
            self._stromligning_url = None

    @staticmethod
    def _parse_stromligning_token(token):
        """
        Parses the Stromligning token into its components.

        Args:
            token (str): The Stromligning token in the format
                         'supplierId/productId' or 'supplierId/productId/groupId'.

        Returns:
            tuple: A tuple containing supplierId, productId, and optionally customerGroupId.

        Raises:
            ValueError: If the token is missing, not a string, or not in the expected format.
        """
        if not token or not isinstance(token, str):
            raise ValueError("token must be provided for Stromligning.")

        parts = [segment.strip() for segment in token.strip().split("/")]
        if any(part == "" for part in parts):
            raise ValueError(
                "token segments must be non-empty when using Stromligning."
            )

        if len(parts) not in (2, 3):
            raise ValueError(
                "token must contain two or three segments separated by '/'."
            )

        supplier_id, product_id = parts[0], parts[1]
        customer_group_id = parts[2] if len(parts) == 3 else None
        return supplier_id, product_id, customer_group_id

    def update_prices(self, tgt_duration, start_time=None):
        """
        Updates the current prices and feed-in prices based on the target duration
        and start time provided.

        Args:
            tgt_duration (int): The target duration (hours or 15-min slots) for which prices
            need to be retrieved.
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

        Returns:
            list: A list of current prices (EUR/Wh) for the configured time frame.
        """
        # logger.debug("[PRICE-IF] Returning current prices: %s", self.current_prices)
        return self.current_prices

    def get_current_feedin_prices(self):
        """
        Returns the current feed-in prices.

        Returns:
            list: A list of current feed-in prices (EUR/Wh) for the configured time frame.
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

        If negative_price_switch is enabled, feed-in prices are set to 0 for negative prices.
        Otherwise, the feed-in tariff price is used for all prices.

        Returns:
            list: A list of feed-in prices (EUR/Wh).
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

        Fetches prices from the configured source. Supported sources: 'tibber', 'smartenergy_at',
        'stromligning', 'fixed_24h', 'default'.

        Args:
            tgt_duration (int): The target duration (hours or 15-min slots) for which prices
            are to be fetched.
            start_time (datetime, optional): The start time from which prices are to be fetched.

        Returns:
            list: A list of prices (EUR/Wh) for the specified duration and start time.
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
        Fetches and processes electricity prices for today and tomorrow from Akkudoktor API.

        Args:
            tgt_duration (int): The target duration in hours or 15-min slots.
            start_time (datetime, optional): The start time for fetching prices.

        Returns:
            list: A list of electricity prices (€/Wh) for the specified duration.
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
        # for 15 min output only extend the array
        if self.time_frame_base == 900:
            extended_prices_15min = []
            for price in extended_prices:
                extended_prices_15min.extend([price] * 4)
            extended_prices = extended_prices_15min
        self.current_prices_direct = extended_prices.copy()
        return extended_prices

    def __retrieve_prices_from_tibber(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices for today and tomorrow from Tibber API.

        Args:
            tgt_duration (int): The target duration in hours or 15-min slots.
            start_time (datetime, optional): The start time for fetching prices.

        Returns:
            list: A list of electricity prices (€/Wh) for the specified duration.
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
        # patching query if time_frame_base is set to 900 (15 minutes)
        # -> priceInfo(resolution: QUARTER_HOURLY)
        if self.time_frame_base == 900:
            query = query.replace(
                "priceInfo",
                "priceInfo(resolution: QUARTER_HOURLY)",
            )

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
        prices_with_timestamps = []  # Keep timestamp info for smart price prediction

        for price in today_prices_json:
            prices.append(round(price["total"] / 1000, 9))
            prices_direct.append(round(price["energy"] / 1000, 9))
            prices_with_timestamps.append(
                {
                    "price": round(price["total"] / 1000, 9),
                    "timestamp": price["startsAt"],
                }
            )
        if tomorrow_prices_json:
            for price in tomorrow_prices_json:
                prices.append(round(price["total"] / 1000, 9))
                prices_direct.append(round(price["energy"] / 1000, 9))
                prices_with_timestamps.append(
                    {
                        "price": round(price["total"] / 1000, 9),
                        "timestamp": price["startsAt"],
                    }
                )
                # logger.debug(
                #     "[Main] day 2 - price for %s -> %s", price["startsAt"], price["total"]
                # )
        else:
            extend_amount = 24
            if self.time_frame_base == 900:
                extend_amount = 96

            # Try smart price prediction with energyforecast.de if enabled
            # Pass known prices WITH timestamps for proper alignment
            forecast_prices = self._fetch_adaptive_energyforecast_fallback(
                known_prices_with_ts=prices_with_timestamps,
                num_missing_hours=extend_amount,
            )
            if forecast_prices:
                logger.info(
                    "[PRICE-IF] Tomorrow prices not available from Tibber, "
                    "using energyforecast.de smart price prediction for next %d hours",
                    extend_amount,
                )
                prices.extend(forecast_prices)
                prices_direct.extend(forecast_prices)
            else:
                # Use simple price repetition when prediction unavailable
                prices.extend(prices[:extend_amount])
                prices_direct.extend(prices_direct[:extend_amount])

        if start_time is None:
            start_time = datetime.now(self.time_zone).replace(
                minute=0, second=0, microsecond=0
            )
        current_hour = start_time.hour
        if self.time_frame_base == 900:
            tgt_duration = 192  # 48 hours in 15 min intervals
        extended_prices = prices[current_hour : current_hour + tgt_duration]
        extended_prices_direct = prices_direct[
            current_hour : current_hour + tgt_duration
        ]

        # Fill any remaining gap with smart price prediction or simple repetition
        if len(extended_prices) < tgt_duration:
            remaining_hours = tgt_duration - len(extended_prices)
            logger.debug(
                "[PRICE-IF] Need %d more hours to reach 48h target, trying smart price prediction",
                (
                    remaining_hours
                    if self.time_frame_base == 3600
                    else remaining_hours // 4
                ),
            )

            # Try smart price prediction with energyforecast.de if enabled
            forecast_prices = self._fetch_adaptive_energyforecast_fallback(
                known_prices_with_ts=prices_with_timestamps,
                num_missing_hours=remaining_hours,
            )
            if forecast_prices:
                logger.info(
                    "[PRICE-IF] Using energyforecast.de smart price prediction to fill remaining %d hours",
                    (
                        remaining_hours
                        if self.time_frame_base == 3600
                        else remaining_hours // 4
                    ),
                )
                extended_prices.extend(forecast_prices)
                extended_prices_direct.extend(forecast_prices)
            else:
                # Fall back to simple price repetition
                logger.debug(
                    "[PRICE-IF] Smart price prediction unavailable, using simple repetition for remaining %d hours",
                    (
                        remaining_hours
                        if self.time_frame_base == 3600
                        else remaining_hours // 4
                    ),
                )
                extended_prices.extend(prices[:remaining_hours])
                extended_prices_direct.extend(prices_direct[:remaining_hours])

        self.current_prices_direct = extended_prices_direct.copy()
        logger.debug("[PRICE-IF] Prices from TIBBER fetched successfully.")
        return extended_prices

    def __retrieve_prices_from_stromligning(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices from Stromligning.dk API.

        Args:
            tgt_duration (int): The target duration in hours or 15-min slots.
            start_time (datetime, optional): The start time for fetching prices.

        Returns:
            list: A list of electricity prices (€/Wh) for the specified duration.
        """
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

        request_url = self._stromligning_url
        to_param = (start_time + timedelta(hours=tgt_duration)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        request_url = f"{request_url}&forecast=true&to={to_param}"

        logger.debug("[PRICE-IF] Requesting prices from STROMLIGNING: %s", request_url)

        try:
            response = requests.get(request_url, headers=headers, timeout=10)
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

        logger.debug(
            "[PRICE-IF] Processing STROMLIGNING prices from %s to %s",
            start_time.strftime("%Y-%m-%d %H:%M"),
            horizon_end.strftime("%Y-%m-%d %H:%M"),
        )
        logger.debug(
            "[PRICE-IF] Total STROMLIGNING entries to process: %d",
            len(processed_entries),
        )

        # Output 15min or hourly values depending on self.time_frame_base
        if self.time_frame_base == 900:
            # 15min intervals, 192 values for 2 days
            interval = timedelta(minutes=15)
            num_slots = int((horizon_end - start_time).total_seconds() // 900)
            # Build a dict of all entries by their start time
            entry_map = {}
            for entry_start_dt, entry_end_dt, price_per_wh in processed_entries:
                duration = (entry_end_dt - entry_start_dt).total_seconds()
                if duration == 900:  # 15min
                    entry_map[entry_start_dt] = price_per_wh
                elif duration == 3600:  # 1h
                    # Fill 4x 15min slots for this hour
                    for i in range(4):
                        slot_time = entry_start_dt + timedelta(minutes=15 * i)
                        entry_map[slot_time] = price_per_wh
                else:
                    # If other durations, fill as many 15min slots as fit
                    n_slots = int(duration // 900)
                    for i in range(n_slots):
                        slot_time = entry_start_dt + timedelta(minutes=15 * i)
                        entry_map[slot_time] = price_per_wh

            prices = []
            current_slot_start = start_time
            coverage_warning = False

            for _ in range(num_slots):
                price = entry_map.get(current_slot_start)
                if price is None:
                    coverage_warning = True
                    if prices:
                        prices.append(prices[-1])
                    else:
                        # fallback: use first available value
                        if entry_map:
                            prices.append(next(iter(entry_map.values())))
                        else:
                            prices.append(0.0)
                else:
                    prices.append(round(price, 9))
                current_slot_start += interval

            if coverage_warning:
                logger.warning(
                    "[PRICE-IF] Incomplete STROMLIGNING price coverage detected; "
                    "missing intervals reused the prior value."
                )

            self.current_prices_direct = prices.copy()
            logger.debug("[PRICE-IF] Prices from STROMLIGNING fetched successfully.")
            return prices

        else:
            # hourly intervals, 48 values for 2 days
            interval = timedelta(hours=1)
            num_slots = int((horizon_end - start_time).total_seconds() // 3600)
            # For each hour, average all 15min slots or use the hourly value
            prices = []
            current_slot_start = start_time
            coverage_warning = False

            for _ in range(num_slots):
                current_slot_end = current_slot_start + interval
                # Collect all 15min slots in this hour
                slot_prices = []
                for entry_start_dt, entry_end_dt, price_per_wh in processed_entries:
                    duration = (entry_end_dt - entry_start_dt).total_seconds()
                    # If 1h and matches the hour, use directly
                    if duration == 3600 and entry_start_dt == current_slot_start:
                        slot_prices = [price_per_wh]
                        break
                    # If 15min and within this hour, collect
                    if (
                        duration == 900
                        and current_slot_start <= entry_start_dt < current_slot_end
                    ):
                        slot_prices.append(price_per_wh)
                if slot_prices:
                    avg_price = round(sum(slot_prices) / len(slot_prices), 9)
                    prices.append(avg_price)
                else:
                    coverage_warning = True
                    if prices:
                        prices.append(prices[-1])
                    else:
                        prices.append(0.0)
                current_slot_start = current_slot_end

            if coverage_warning:
                logger.warning(
                    "[PRICE-IF] Incomplete STROMLIGNING price coverage detected; "
                    "missing intervals reused the prior value."
                )

            self.current_prices_direct = prices.copy()
            logger.debug("[PRICE-IF] Prices from STROMLIGNING fetched successfully.")
            return prices

    def __retrieve_prices_from_smartenergy_at(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices from SmartEnergy AT API.

        Args:
            tgt_duration (int): The target duration in hours or 15-min slots.
            start_time (datetime, optional): The start time for fetching prices.

        Returns:
            list: A list of electricity prices (€/Wh) for the specified duration.
        """
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

        if self.time_frame_base == 3600:
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

            # Extend to tgt_duration if needed
            extended_prices = hourly_prices
            if len(extended_prices) < tgt_duration:
                remaining_hours = tgt_duration - len(extended_prices)

                # Try smart price prediction with energyforecast.de if enabled
                forecast_prices = self._fetch_adaptive_energyforecast_fallback(
                    known_prices=extended_prices,
                    num_missing_hours=remaining_hours,
                )
                if forecast_prices:
                    logger.info(
                        "[PRICE-IF] SmartEnergy AT incomplete, "
                        "using energyforecast.de smart price prediction for %d missing hours",
                        remaining_hours,
                    )
                    extended_prices.extend(forecast_prices)
                else:
                    # Use simple price repetition
                    extended_prices.extend(hourly_prices[:remaining_hours])

        elif self.time_frame_base == 900:
            # Use 15min values directly
            prices_15min = []
            for entry in data["data"]:
                prices_15min.append(round(entry["value"] / 100000, 9))  # euro/wh

            # Extend to tgt_duration if needed
            extended_prices = prices_15min
            if len(extended_prices) < tgt_duration:
                remaining_slots = tgt_duration - len(extended_prices)

                # Try smart price prediction with energyforecast.de if enabled
                forecast_prices = self._fetch_adaptive_energyforecast_fallback(
                    known_prices=extended_prices,
                    num_missing_hours=remaining_slots,
                )
                if forecast_prices:
                    logger.info(
                        "[PRICE-IF] SmartEnergy AT incomplete, "
                        "using energyforecast.de smart price prediction for %d missing slots",
                        remaining_slots,
                    )
                    extended_prices.extend(forecast_prices)
                else:
                    # Use simple price repetition
                    extended_prices.extend(prices_15min[:remaining_slots])

        # Catch case where all prices are zero (or data is empty)
        if not any(extended_prices):
            logger.error(
                "[PRICE-IF] SMARTENERGY_AT API returned only zero prices or empty data."
            )
            return []

        logger.debug("[PRICE-IF] Prices from SMARTENERGY_AT fetched successfully.")
        self.current_prices_direct = extended_prices.copy()
        return extended_prices

    def _fetch_adaptive_energyforecast_fallback(
        self, known_prices_with_ts=None, known_prices=None, num_missing_hours=None
    ):
        """
        Fetch smart price predictions from energyforecast.de using learned pattern.

        This method learns the relationship between primary source prices (e.g., Tibber)
        and energyforecast.de EPEX spot prices, then applies that learned pattern to
        predict future hours. Uses linear regression to find:
            primary_price = factor * epex_spot + offset

        This handles:
        - Variable taxes (percentage-based on EPEX)
        - Fixed grid fees and charges
        - Negative EPEX spot prices correctly
        - Timestamp-based alignment when available

        Args:
            known_prices_with_ts (list): List of dicts with 'price' and 'timestamp' keys (EUR/Wh).
            known_prices (list): Simple price list (EUR/Wh) for backward compatibility.
            num_missing_hours (int): Number of future hours/slots to predict.

        Returns:
            list: Adapted forecast prices in EUR/Wh, or empty list if learning failed.
        """
        if not self.energyforecast_enabled:
            logger.debug(
                "[PRICE-IF] Energyforecast.de smart price prediction disabled in config"
            )
            return []

        # Check currency compatibility - energyforecast.de only supports EUR
        if self.price_currency != "EUR":
            logger.info(
                "[PRICE-IF] Smart price prediction currently only supports EUR prices. "
                "Currency %s detected - using simple price repetition instead.",
                self.price_currency,
            )
            return []

        # Handle both timestamp and simple price list formats
        if known_prices_with_ts is not None:
            num_known_slots = len(known_prices_with_ts)
            use_timestamps = True
        elif known_prices is not None:
            num_known_slots = len(known_prices)
            use_timestamps = False
        else:
            logger.warning(
                "[PRICE-IF] No known prices provided for smart price prediction"
            )
            return []

        num_known_hours = (
            num_known_slots if self.time_frame_base == 3600 else num_known_slots // 4
        )

        logger.info(
            "[PRICE-IF] Fetching energyforecast.de smart price prediction "
            "(have %d hours, need %d more)",
            num_known_hours,
            (
                num_missing_hours
                if self.time_frame_base == 3600
                else num_missing_hours // 4
            ),
        )

        # Fetch full 48h of EPEX spot prices from energyforecast (NO markup applied)
        resolution = "QUARTER_HOURLY" if self.time_frame_base == 900 else "HOURLY"

        params = {
            "token": self.energyforecast_token,
            "market_zone": self.energyforecast_market_zone,
            "resolution": resolution,
            "fixed_cost_cent": 0,  # Get raw EPEX prices for learning
            "vat": 0,  # No markup - we'll learn the relationship
        }

        try:
            response = requests.get(ENERGYFORECAST_API, params=params, timeout=10)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            logger.warning(
                "[PRICE-IF] Energyforecast.de request timed out, using simple price repetition"
            )
            return []
        except requests.exceptions.RequestException as e:
            logger.warning(
                "[PRICE-IF] Energyforecast.de request failed (%s): %s",
                type(e).__name__,
                str(e),
            )
            return []

        try:
            data = response.json()
        except ValueError as e:
            logger.warning(
                "[PRICE-IF] Failed to parse energyforecast.de response: %s", e
            )
            return []

        if not isinstance(data, list) or len(data) == 0:
            logger.warning(
                "[PRICE-IF] Energyforecast.de returned invalid or empty data"
            )
            return []

        # Convert energyforecast data to EUR/Wh with timestamps for alignment
        energyforecast_data = []
        for entry in data:
            try:
                price_eur_per_kwh = float(entry["price"])
                price_eur_per_wh = price_eur_per_kwh / 1000
                timestamp_str = entry.get("start", "")
                energyforecast_data.append(
                    {
                        "price": round(price_eur_per_wh, 9),
                        "timestamp": timestamp_str,
                    }
                )
            except (KeyError, ValueError, TypeError) as e:
                logger.debug("[PRICE-IF] Error parsing energyforecast.de entry: %s", e)
                continue

        # Align by timestamps if available, otherwise use simple index matching
        if use_timestamps:
            # Create timestamp -> price mappings
            known_map = {
                entry["timestamp"]: entry["price"] for entry in known_prices_with_ts
            }
            epex_map = {
                entry["timestamp"]: entry["price"] for entry in energyforecast_data
            }

            # Find overlapping timestamps
            common_timestamps = sorted(set(known_map.keys()) & set(epex_map.keys()))

            if len(common_timestamps) == 0:
                logger.warning(
                    "[PRICE-IF] No overlapping timestamps between source and energyforecast"
                )
                return []

            # Extract aligned samples
            primary_samples = [known_map[ts] for ts in common_timestamps]
            epex_samples = [epex_map[ts] for ts in common_timestamps]

            overlap_size = len(common_timestamps)

            logger.info(
                "[PRICE-IF] Timestamp alignment: %d overlapping slots found",
                overlap_size,
            )

        else:
            # Simple index-based matching (backward compatibility for sources without timestamps)
            energyforecast_prices = [entry["price"] for entry in energyforecast_data]

            if len(energyforecast_prices) < num_known_slots + num_missing_hours:
                logger.warning(
                    "[PRICE-IF] Energyforecast.de returned insufficient data "
                    "(got %d, need %d)",
                    len(energyforecast_prices),
                    num_known_slots + num_missing_hours,
                )
                return []

            overlap_size = min(num_known_slots, len(energyforecast_prices))
            primary_samples = known_prices[:overlap_size]
            epex_samples = energyforecast_prices[:overlap_size]

        # Check minimum overlap requirement
        min_overlap_slots = (
            ENERGYFORECAST_MIN_OVERLAP_HOURS
            if self.time_frame_base == 3600
            else ENERGYFORECAST_MIN_OVERLAP_HOURS * 4
        )

        if overlap_size < min_overlap_slots:
            logger.warning(
                "[PRICE-IF] Insufficient overlap for learning "
                "(have %d slots, need %d minimum)",
                overlap_size,
                min_overlap_slots,
            )
            return []

        # Learn relationship using linear regression: primary = factor * epex + offset
        # Using simple least squares: y = a*x + b
        try:
            factor, offset = self._linear_regression(epex_samples, primary_samples)
        except Exception as e:
            logger.warning("[PRICE-IF] Linear regression failed: %s", e)
            return []

        # Convert offset from EUR/Wh to ct/kWh for logging
        offset_ct_kwh = offset * 100000

        # Log sample comparison for learning quality
        logger.info(
            "[PRICE-IF] Learning from %d samples - First 3 comparisons:",
            overlap_size,
        )
        for i in range(min(3, overlap_size)):
            logger.info(
                "  Sample %d: EPEX %.2f ct/kWh → Primary %.2f ct/kWh",
                i,
                epex_samples[i] * 100000,
                primary_samples[i] * 100000,
            )

        # Validate learned parameters
        if not (ENERGYFORECAST_MIN_FACTOR <= factor <= ENERGYFORECAST_MAX_FACTOR):
            logger.warning(
                "[PRICE-IF] Learned factor %.3f outside valid range [%.1f, %.1f], "
                "using price repetition",
                factor,
                ENERGYFORECAST_MIN_FACTOR,
                ENERGYFORECAST_MAX_FACTOR,
            )
            return []

        if abs(offset_ct_kwh) > ENERGYFORECAST_MAX_OFFSET_CT:
            logger.warning(
                "[PRICE-IF] Learned offset %.1f ct/kWh exceeds maximum ±%.1f ct/kWh, "
                "using price repetition",
                offset_ct_kwh,
                ENERGYFORECAST_MAX_OFFSET_CT,
            )
            return []

        logger.info(
            "[PRICE-IF] Learned adaptation from %d overlapping hours: "
            "factor=%.3f, offset=%.1f ct/kWh",
            overlap_size if self.time_frame_base == 3600 else overlap_size // 4,
            factor,
            offset_ct_kwh,
        )

        # Extract future prices from energyforecast (timestamps after known data)
        if use_timestamps:
            # Get last timestamp from known prices
            last_known_ts = known_prices_with_ts[-1]["timestamp"]

            # Filter energyforecast for future timestamps
            future_epex = [
                entry["price"]
                for entry in energyforecast_data
                if entry["timestamp"] > last_known_ts
            ][:num_missing_hours]

        else:
            # Simple index-based extraction
            energyforecast_prices = [entry["price"] for entry in energyforecast_data]
            future_epex = energyforecast_prices[
                num_known_slots : num_known_slots + num_missing_hours
            ]

        if len(future_epex) < num_missing_hours:
            logger.warning(
                "[PRICE-IF] Insufficient future prices from energyforecast "
                "(got %d, need %d)",
                len(future_epex),
                num_missing_hours,
            )
            return []

        # Apply learned pattern to future hours
        adapted_prices = []
        for epex_price in future_epex:
            adapted_price = factor * epex_price + offset
            # Handle negative prices: if result is negative, keep it (user pays negative = gets paid)
            adapted_prices.append(round(adapted_price, 9))

        logger.info(
            "[PRICE-IF] Generated %d adapted forecast prices (range: %.2f to %.2f ct/kWh)",
            len(adapted_prices),
            min(adapted_prices) * 100000 if adapted_prices else 0,
            max(adapted_prices) * 100000 if adapted_prices else 0,
        )

        return adapted_prices

    @staticmethod
    def _linear_regression(x_values, y_values):
        """
        Perform simple linear regression: y = a*x + b

        Args:
            x_values (list): Independent variable values (EPEX prices).
            y_values (list): Dependent variable values (primary source prices).

        Returns:
            tuple: (slope/factor, intercept/offset)

        Raises:
            ValueError: If regression cannot be computed.
        """
        n = len(x_values)
        if n < 2:
            raise ValueError("Need at least 2 data points for regression")

        # Calculate means
        mean_x = sum(x_values) / n
        mean_y = sum(y_values) / n

        # Calculate slope (a) and intercept (b)
        numerator = sum(
            (x_values[i] - mean_x) * (y_values[i] - mean_y) for i in range(n)
        )
        denominator = sum((x_values[i] - mean_x) ** 2 for i in range(n))

        if abs(denominator) < 1e-10:
            raise ValueError("Cannot compute regression - x values have no variance")

        slope = numerator / denominator
        intercept = mean_y - slope * mean_x

        return slope, intercept

    def __retrieve_prices_from_fixed24h_array(self, tgt_duration, start_time=None):
        """
        Returns a fixed 24-hour array of prices.

        Args:
            tgt_duration (int): The target duration in hours or 15-min slots.
            start_time (datetime, optional): The start time for fetching prices.

        Returns:
            list: A list of fixed prices (EUR/Wh) for the specified duration.
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
        # Convert each entry in fixed_24h_array from ct/kWh to EUR/Wh (divide by 100000)
        extended_prices = [round(price / 100000, 9) for price in self.fixed_24h_array]
        # Extend to tgt_duration if needed
        if len(extended_prices) < tgt_duration:
            remaining_hours = tgt_duration - len(extended_prices)
            extended_prices.extend(extended_prices[:remaining_hours])
        # for 15 min output only extend the array
        if self.time_frame_base == 900:
            extended_prices_15min = []
            for price in extended_prices:
                extended_prices_15min.extend([price] * 4)
            extended_prices = extended_prices_15min
        self.current_prices_direct = extended_prices.copy()
        return extended_prices
