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
from collections import defaultdict, namedtuple
import json
import logging
import threading
import requests

try:  # running from src/ — src/ is on sys.path
    from timeseries_grid import (
        KIND_INTENSIVE,
        TimeseriesGridError,
        clamp_report,
        horizon_slots,
        local_midnight,
        place_on_grid,
    )
except ImportError:  # imported as src.interfaces (tests)
    from ..timeseries_grid import (
        KIND_INTENSIVE,
        TimeseriesGridError,
        clamp_report,
        horizon_slots,
        local_midnight,
        place_on_grid,
    )
try:  # running from src/ — src/ is on sys.path
    from interfaces.http_fetch import bearer_request_kwargs, retry_request
except ImportError:  # imported as src.interfaces (tests)
    from .http_fetch import bearer_request_kwargs, retry_request
from .forecast_metadata import ForecastMetadataMixin
from .timeseries_normalizer import (
    TEMPLATE_DOCS_URL,
    TimeseriesFormatError,
    convert_price_values,
    detect_resolution_seconds,
    extract_json_path,
    median_span_seconds,
    normalize_entries,
    price_plausibility_message,
)

logger = logging.getLogger("__main__")
logger.info("[PRICE-IF] loading module ")

#: What one placement of a price series produced. ``real_slots`` is the slot
#: index where invented data begins — the first slot after the last one the
#: source actually covered, or the full horizon when the source covered it all.
#: It is a SLOT index, which is the whole point: the number of entries a source
#: sent equals a slot index only when the series starts exactly at the anchor
#: AND its resolution matches time_frame_base.
PriceWindow = namedtuple("PriceWindow", "values first_covered real_slots")


def fill_price_gaps(values, coverage, slots_per_day):
    """
    Fill the slots a price series did not cover, by the established policy.

    - before the first covered slot: the first known price, backwards
    - an interior hole: the last known price, forwards
    - the second day, where uncovered: today's value for the same slot
      (tomorrow repeats today's pattern), else the last known price

    Args:
        values: Per-slot prices from the intensive placement.
        coverage: Per-slot coverage fractions; > 0 counts as real data.
        slots_per_day: 24 or 96 — where the pattern repetition starts.

    Returns:
        (filled_values, leading_count, forecast_start_index) —
        forecast_start_index is the first slot of the contiguous uncovered
        TAIL (None when the last slot is covered), which is exactly what
        _set_forecast_metadata reports to the UI. ([], 0, None) when nothing
        is covered at all, so the caller can fall back instead of inventing
        a flat price.
    """
    covered = [fraction > 0 for fraction in coverage]
    first_data = next((i for i, flag in enumerate(covered) if flag), None)
    if first_data is None:
        return [], 0, None
    last_data = max(i for i, flag in enumerate(covered) if flag)

    first_price = values[first_data]
    last_known = first_price
    filled = []
    for index, flag in enumerate(covered):
        if flag:
            last_known = values[index]
            filled.append(last_known)
        elif index < first_data:
            filled.append(first_price)
        elif index >= slots_per_day and covered[index - slots_per_day]:
            filled.append(values[index - slots_per_day])
        else:
            filled.append(last_known)

    forecast_start = last_data + 1 if last_data + 1 < len(values) else None
    return filled, first_data, forecast_start


AKKUDOKTOR_API_PRICES = "https://api.akkudoktor.net/prices"
TIBBER_API = "https://api.tibber.com/v1-beta/gql"
SMARTENERGY_API = "https://apis.smartenergy.at/market/v1/price"
STROMLIGNING_API_BASE = "https://stromligning.dk/api/prices?lean=true"
ENERGYFORECAST_API = "https://www.energyforecast.de/api/v1/predictions/next_48_hours"

# Sources that publish a raw exchange price, i.e. energy alone with no grid fee,
# levy or VAT folded in. Only these get fixed_price_adder_ct / relative_price_multiplier:
# the adder exists to turn an exchange price into what the meter actually costs.
#
# The others already serve a final tariff and must be left alone — Tibber reports
# price["total"] (energy + tax + fees), Stromligning is queried with ?lean=true
# (incl. tariffs and VAT), EVCC's /api/tariff/grid has the user's own tariff
# configuration applied, fixed_24h is a figure the user typed by hand, and
# default_prices is the 10 ct/kWh placeholder. Adding a grid fee on top of any of
# those would charge it twice.
EXCHANGE_PRICE_SOURCES = frozenset({"default", "smartenergy_at", "timeseries"})

# Energyforecast smart price prediction constants
ENERGYFORECAST_MIN_OVERLAP_HOURS = 6  # Minimum overlapping hours needed for learning
ENERGYFORECAST_MAX_FACTOR = 5.0  # Maximum allowed multiplicative factor
ENERGYFORECAST_MIN_FACTOR = 0.5  # Minimum allowed multiplicative factor
ENERGYFORECAST_MAX_OFFSET_CT = 50.0  # Maximum allowed offset in ct/kWh


class PriceInterface(ForecastMetadataMixin):
    """
    The PriceInterface class manages electricity price data retrieval and processing from
    various sources.

    Attributes:
        src (str): Source of the price data
                   (e.g., 'tibber', 'stromligning', 'smartenergy_at', 'fixed_24h', 'default').
        access_token (str): Access token for authenticating with the price source.
        fixed_24h_array (list): Optional fixed 24-hour price array (ct/kWh).
        feed_in_tariff_price (float): Feed-in tariff price in ct/kWh
                    (legacy, use FeedInPriceInterface).
        time_zone (str): Timezone for date and time operations.
        current_prices (list): Current prices including taxes (EUR/Wh).
        default_prices (list): Default price list if external data is unavailable (EUR/Wh).

    Methods:
        update_prices(tgt_duration, start_time):
            Updates current_prices and current_feedin for the given duration and start time.
        get_current_prices():
            Returns the current prices (EUR/Wh).
        get_current_feedin_prices():
            Returns the current feed-in prices (EUR/Wh).
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
        evcc_interface=None,
    ):
        self.src = config["source"]
        raw_token = config.get("token", "")
        # Strip leading/trailing whitespace that can be introduced by YAML >- block
        # scalar style when long tokens wrap across multiple lines
        self.access_token = str(raw_token).strip()
        if self.access_token != raw_token:
            logger.warning(
                "[PRICE-IF] token had leading/trailing whitespace stripped. "
                "Check config.yaml: avoid using YAML block scalar style ('>-') for tokens."
            )
        elif " " in self.access_token or "\n" in self.access_token:
            logger.warning(
                "[PRICE-IF] token contains internal whitespace. This will cause "
                "authentication failures. Use plain string style for long "
                "tokens — place the token directly after 'token: ' on the same line."
            )
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

        # Energyforecast.de smart price prediction configuration
        self.energyforecast_enabled = config.get("energyforecast_enabled", False)
        self.energyforecast_token = config.get("energyforecast_token", "demo_token")
        self.energyforecast_market_zone = config.get(
            "energyforecast_market_zone", "DE-LU"
        )

        # Timeseries data source configuration (HA or HTTP endpoint)
        self.data_url = config.get("data_url", "").strip()
        self.data_path = config.get("data_path", "attributes.data").strip()
        self.data_token = config.get("data_token", "").strip()
        # Unit of the source's "value" field. Default matches EVCC's `rates`, which is
        # the reference format for this source — see timeseries_normalizer.
        self.value_unit = config.get("value_unit", "EUR/kWh").strip()

        # EVCC interface for EVCC price source
        self.evcc_interface = evcc_interface

        self.time_frame_base = time_frame_base
        self.time_zone = timezone
        self.current_prices = []
        self.default_prices = [0.0001] * 48  # if external data are not available
        self.price_currency = self.__determine_price_currency()

        # Add retry mechanism attributes
        self.last_successful_prices = []
        self.consecutive_failures = 0
        self.max_failures = 24  # Max consecutive failures before using default prices

        # Background thread attributes
        self._update_thread = None
        self._stop_event = threading.Event()
        self.update_interval = 900  # 15 minutes in seconds

        # Smart caching for energyforecast.de API calls to stay within rate limits
        # Throttles calls to once per hour (max ~13/day) to avoid excessive API usage
        self._last_energyforecast_call_time = None  # Timestamp of last API call
        self._last_energyforecast_call_date = None  # Date of last API call
        self._energyforecast_cache = []  # Cached prediction from last call

        # Forecast metadata tracking for UI visualization
        self._init_forecast_metadata()
        # Prices and forecast metadata are read by the web thread while the
        # update thread writes them; this tuple is assigned once per update so
        # a reader never sees new metadata next to old prices.
        self._published = None

        self.__check_config()  # Validate configuration parameters
        logger.info(
            "[PRICE-IF] Initialized with source: %s, feed_in_tariff_price: %s",
            self.src,
            self.feed_in_tariff_price,
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
                horizon_slots(self.time_frame_base),
                local_midnight(self.time_zone),
            )  # Get the full optimization horizon of price data
            logger.info("[PRICE-IF] Initial price update completed")
        except RuntimeError as e:
            logger.error(
                "[price_interface] Price fetch failed: %s | Config: #price | ACTION REQUIRED",
                e
            )

        while not self._stop_event.is_set():
            try:
                # Wait for the update interval or until stop event is set
                if self._stop_event.wait(timeout=self.update_interval):
                    break  # Stop event was set

                # Perform price update
                self.update_prices(
                    horizon_slots(self.time_frame_base),
                    local_midnight(self.time_zone),
                )  # Get the full optimization horizon of price data
                logger.debug("[PRICE-IF] Periodic price update completed")

            except (requests.RequestException, KeyError, ValueError, AttributeError,
                    TypeError, OSError, RuntimeError) as e:
                logger.error(
                    "[price_interface] Price fetch failed: %s | Config: #price | ACTION REQUIRED",
                    e
                )
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
        Updates the current prices based on the target duration and start time provided.

        Args:
            tgt_duration (int): The target duration (hours or 15-min slots) for which prices
            need to be retrieved.
            start_time (datetime): The starting time for retrieving prices.

        Updates:
            self.current_prices: Updates with the retrieved prices for the given duration
                                 and start time.

        Logs:
            Logs a debug message indicating that prices have been updated.
        """
        if start_time is None:
            # Local midnight, not the top of the current hour: slot 0 of every
            # array this interface produces is midnight, so defaulting to "now"
            # placed a timestamped series `now.hour` slots early. Production
            # always passes an explicit anchor, which is why this never bit -
            # but the feed-in interface already defaults this way and the
            # asymmetry is a trap for the next caller.
            start_time = local_midnight(self.time_zone)
        self.current_prices = self.__retrieve_prices(tgt_duration, start_time)
        self._published = (
            list(self.current_prices),
            self.forecast_start_index,
            self.forecast_type,
            self.forecast_source,
        )
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
        if self._published is not None:
            return self._published[0]
        return self.current_prices

    def get_price_currency(self):
        """
        Return the currency identifier for the currently configured price source.

        Returns:
            str: ISO 4217 currency code (e.g. 'EUR', 'DKK').
        """
        return self.price_currency

    def get_forecast_metadata(self):
        """
        The mixin's triple, read from the published snapshot when one exists —
        a web-thread reader must never see new metadata next to old prices.
        """
        if self._published is not None:
            _, start_index, forecast_type, forecast_source = self._published
            return {
                "forecast_start_index": start_index,
                "forecast_type": forecast_type,
                "forecast_source": forecast_source,
            }
        return super().get_forecast_metadata()

    def clear_forecast_metadata(self):
        """
        Drop forecast markers belonging to the previous source.

        Used by hot reload when the price source changes without an immediate
        fetch: the old markers would otherwise keep hatching real prices as
        forecast until the next update cycle. The published snapshot carries
        its own copy of the triple, so it is cleared alongside.
        """
        super().clear_forecast_metadata()
        if self._published is not None:
            self._published = (self._published[0], None, None, None)

    def drop_fallback_cache(self):
        """
        Forget the last successful prices, for a source change.

        ``last_successful_prices`` holds values already shaped by the *previous*
        source — including its adder and multiplier, which only the exchange
        sources get (EXCHANGE_PRICE_SOURCES). Carrying that cache into a new
        source would serve the old source's adjusted prices whenever the new one
        is short or fails, and the EVCC path splices the cache directly into its
        own result. The failure counter goes with it: failures counted against
        the old endpoint say nothing about the new one.
        """
        self.last_successful_prices = []
        self.consecutive_failures = 0

    def __retrieve_prices(self, tgt_duration, start_time=None):
        """
        Retrieve prices based on the target duration and optional start time.

        Fetches prices from the configured source. Supported sources: 'tibber', 'smartenergy_at',
        'stromligning', 'fixed_24h', 'timeseries', 'default'.

        Args:
            tgt_duration (int): The horizon in slots (48 hourly, 192 quarter-hourly)
            for which prices are to be fetched.
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
        elif self.src == "timeseries":
            prices = self.__retrieve_prices_from_url(tgt_duration, start_time)
        elif self.src == "evcc":
            prices = self.__retrieve_prices_from_evcc(tgt_duration, start_time)
        elif self.src == "default":
            prices = self.__retrieve_prices_from_akkudoktor(tgt_duration, start_time)
        else:
            prices = self.default_prices
            logger.error(
                "[PRICE-IF] Price source currently not supported."
                + " Using default prices (0,10 ct/kWh)."
            )
            # Nothing below may adjust this: the placeholder is not an exchange
            # price, and an unsupported source must not silently grow a grid fee.
            return prices[:tgt_duration]

        if prices and self.src in EXCHANGE_PRICE_SOURCES:
            # Exchange prices only — see EXCHANGE_PRICE_SOURCES. The cache below
            # stores the adjusted values, so a fallback serves them as-is and
            # nothing is ever adjusted twice.
            prices = self.__apply_price_adjustments(prices)

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

                # Extend if needed
                if len(prices) < tgt_duration:
                    remaining_slots = tgt_duration - len(prices)
                    prices.extend(self.last_successful_prices[:remaining_slots])
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
        else:
            # Success - reset failure counter and store successful prices
            self.consecutive_failures = 0
            self.last_successful_prices = prices.copy()
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

    def __apply_price_adjustments(self, prices):
        """
        Apply the user's price corrections to freshly fetched prices.

        (price + fixed_price_adder_ct) * (1 + relative_price_multiplier), then
        the EUR/Wh sanity clamp — in that order, so a grid fee is multiplied
        like the energy price is (VAT applies to fees too) and the served value
        stays bounded.

        Called only for EXCHANGE_PRICE_SOURCES. It used to live in the Akkudoktor
        fetch alone, which left the fields a silent no-op on the other exchange
        sources (EPEX-style timeseries, smartenergy_at) whose raw prices need the
        fee the most — but a source that already reports a final tariff must not
        be touched at all, or the fee is charged twice.
        """
        adder = self.fixed_price_adder_ct / 100000
        multiplier = 1 + self.relative_price_multiplier
        if adder == 0 and multiplier == 1:
            return prices

        adjusted = [round((price + adder) * multiplier, 9) for price in prices]
        adjusted, clamped, worst = clamp_report(adjusted, -0.5, 1.0)
        if clamped:
            logger.warning(
                "[PRICE-IF] %d adjusted price value(s) outside the EUR/Wh range, "
                "clamping (worst: %.9f) — check fixed_price_adder_ct and "
                "relative_price_multiplier",
                clamped,
                worst,
            )
        return adjusted

    def __retrieve_prices_from_akkudoktor(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices for today and tomorrow from Akkudoktor API.

        Args:
            tgt_duration (int): The horizon in slots (48 hourly, 192 quarter-hourly).
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
        return self.__fetch_akkudoktor_prices(tgt_duration, start_time)

    def __fetch_akkudoktor_prices(self, tgt_duration, start_time=None):
        """
        Core Akkudoktor API fetch logic (without source validation).
        
        Only reachable for src="default" — __retrieve_prices_from_akkudoktor
        guards the source before delegating here.

        Args:
            tgt_duration (int): The horizon in slots (48 hourly, 192 quarter-hourly).
            start_time (datetime, optional): The start time for fetching prices.

        Returns:
            list: A list of electricity prices (€/Wh) for the specified duration.
        """
        logger.debug("[PRICE-IF] Fetching prices from akkudoktor ...")
        if start_time is None:
            # Local midnight, not the top of the current hour: slot 0 of every
            # array this interface produces is midnight, so defaulting to "now"
            # placed a timestamped series `now.hour` slots early. Production
            # always passes an explicit anchor, which is why this never bit -
            # but the feed-in interface already defaults this way and the
            # asymmetry is a trap for the next caller.
            start_time = local_midnight(self.time_zone)
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
            # Plain market price; the adder/multiplier are applied centrally in
            # __retrieve_prices for every dynamic source alike.
            prices.append(round(price["marketpriceEurocentPerKWh"] / 100000, 9))

        if start_time is None:
            # Local midnight, not the top of the current hour: slot 0 of every
            # array this interface produces is midnight, so defaulting to "now"
            # placed a timestamped series `now.hour` slots early. Production
            # always passes an explicit anchor, which is why this never bit -
            # but the feed-in interface already defaults this way and the
            # asymmetry is a trap for the next caller.
            start_time = local_midnight(self.time_zone)
        current_hour = start_time.hour
        # tgt_duration counts slots; this path works in hours until the final
        # expansion below, so convert once.
        tgt_hours = int(tgt_duration * self.time_frame_base) // 3600
        extended_prices = prices[current_hour : current_hour + tgt_hours]

        if len(extended_prices) < tgt_hours:
            remaining_hours = tgt_hours - len(extended_prices)
            extended_prices.extend(prices[:remaining_hours])
        logger.debug("[PRICE-IF] Prices from AKKUDOKTOR fetched successfully.")
        # for 15 min output only extend the array
        if self.time_frame_base == 900:
            extended_prices_15min = []
            for price in extended_prices:
                extended_prices_15min.extend([price] * 4)
            extended_prices = extended_prices_15min
        return extended_prices

    def __retrieve_prices_from_tibber(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices for today and tomorrow from Tibber API.

        Args:
            tgt_duration (int): The horizon in slots (48 hourly, 192 quarter-hourly).
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

        # HTTP headers only accept ASCII/latin-1 — reject tokens that would
        # cause a UnicodeEncodeError deep in the requests stack.
        try:
            self.access_token.encode("latin-1")
        except UnicodeEncodeError:
            logger.error(
                "[PRICE-IF] Tibber access token contains non-latin-1 characters "
                "and cannot be used in an HTTP header. "
                "Check your price.token configuration."
            )
            return []

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
        except UnicodeEncodeError as e:
            logger.error(
                "[PRICE-IF] Tibber token contains characters not valid in HTTP headers: %s. "
                "Check your price.token configuration.",
                e,
            )
            return []

        response.raise_for_status()
        data = response.json()
        if "errors" in data and data["errors"] is not None:
            logger.error(
                "[price_interface] Tibber API error: %s | Config: #price | ACTION REQUIRED",
                data["errors"][0]["message"],
            )
            return []

        try:
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
        except (KeyError, IndexError, TypeError) as e:
            logger.error(
                "[price_interface] Tibber price data invalid (missing priceInfo): %s "
                "| Config: #price | ACTION REQUIRED",
                e
            )
            return []
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

        if start_time is None:
            # Local midnight, not the top of the current hour: slot 0 of every
            # array this interface produces is midnight, so defaulting to "now"
            # placed a timestamped series `now.hour` slots early. Production
            # always passes an explicit anchor, which is why this never bit -
            # but the feed-in interface already defaults this way and the
            # asymmetry is a trap for the next caller.
            start_time = local_midnight(self.time_zone)

        prices = []
        prices_with_timestamps = []  # Keep timestamp info for smart price prediction
        today_cutoff_idx = 0  # Track where today's real data ends

        # Load today's prices and find where real data ends (end of calendar day)
        for price in today_prices_json:
            prices.append(round(price["total"] / 1000, 9))
            prices_with_timestamps.append(
                {
                    "price": round(price["total"] / 1000, 9),
                    "timestamp": price["startsAt"],
                }
            )

        # Calculate where "today" ends - today's data always goes to 23:59
        # In 15-min intervals: 96 items (24 * 4)
        # In hourly intervals: 24 items
        if self.time_frame_base == 900:
            today_cutoff_idx = 96  # Full day in 15-min intervals
        else:
            today_cutoff_idx = 24  # Full day in hourly intervals
        if tomorrow_prices_json:
            for price in tomorrow_prices_json:
                prices.append(round(price["total"] / 1000, 9))
                prices_with_timestamps.append(
                    {
                        "price": round(price["total"] / 1000, 9),
                        "timestamp": price["startsAt"],
                    }
                )
                # logger.debug(
                #     "[Main] day 2 - price for %s -> %s", price["startsAt"], price["total"]
                # )
            # All prices are real data from Tibber
            self._set_forecast_metadata(
                start_index=None,
                forecast_type="all_real",
                source=None,
            )
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
                forecast_start_idx = today_cutoff_idx
                prices.extend(forecast_prices)
                self._set_forecast_metadata(
                    start_index=forecast_start_idx,
                    forecast_type="smart_forecast",
                    source="energyforecast.de",
                )
            else:
                # Use simple price repetition when prediction unavailable
                forecast_start_idx = today_cutoff_idx
                prices.extend(prices[:extend_amount])
                self._set_forecast_metadata(
                    start_index=forecast_start_idx,
                    forecast_type="simple_repetition",
                    source=None,
                )

        if start_time is None:
            # Local midnight, not the top of the current hour: slot 0 of every
            # array this interface produces is midnight, so defaulting to "now"
            # placed a timestamped series `now.hour` slots early. Production
            # always passes an explicit anchor, which is why this never bit -
            # but the feed-in interface already defaults this way and the
            # asymmetry is a trap for the next caller.
            start_time = local_midnight(self.time_zone)
        current_hour = start_time.hour

        # tgt_duration already counts slots; only the offset depends on the frame.
        actual_slots = tgt_duration
        array_offset = current_hour * 4 if self.time_frame_base == 900 else current_hour

        extended_prices = prices[array_offset : array_offset + actual_slots]

        # Note: forecast_start_index remains canonical (relative to full 48-hour array)
        # UI will handle time offset when filtering for display

        # Fill any remaining gap with smart price prediction or simple repetition
        if len(extended_prices) < actual_slots:
            remaining_slots = actual_slots - len(extended_prices)
            remaining_hours = (
                remaining_slots
                if self.time_frame_base == 3600
                else remaining_slots // 4
            )
            logger.debug(
                "[PRICE-IF] Need %d more slots (%d hours) to reach target, "
                "trying smart price prediction",
                remaining_slots,
                remaining_hours,
            )

            # Try smart price prediction with energyforecast.de if enabled
            forecast_prices = self._fetch_adaptive_energyforecast_fallback(
                known_prices_with_ts=prices_with_timestamps,
                num_missing_hours=remaining_slots,
            )
            if forecast_prices:
                logger.info(
                    "[PRICE-IF] Using energyforecast.de smart price prediction"
                    + " to fill remaining %d slots (%d hours)",
                    remaining_slots,
                    remaining_hours,
                )
                extended_prices.extend(forecast_prices[:remaining_slots])
            else:
                # Fall back to repeating the last known price value
                pad_value = extended_prices[-1] if extended_prices else 0.0
                logger.debug(
                    "[PRICE-IF] Smart price prediction unavailable, padding %d slots "
                    "with last known price (likely DST adjustment)",
                    remaining_slots,
                )
                extended_prices.extend([pad_value] * remaining_slots)

        logger.debug("[PRICE-IF] Prices from TIBBER fetched successfully.")
        return extended_prices

    def __retrieve_prices_from_stromligning(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices from Stromligning.dk API.

        Args:
            tgt_duration (int): The horizon in slots (48 hourly, 192 quarter-hourly).
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
            # Local midnight, not the top of the current hour: slot 0 of every
            # array this interface produces is midnight, so defaulting to "now"
            # placed a timestamped series `now.hour` slots early. Production
            # always passes an explicit anchor, which is why this never bit -
            # but the feed-in interface already defaults this way and the
            # asymmetry is a trap for the next caller.
            start_time = local_midnight(self.time_zone)

        if start_time.tzinfo is None and hasattr(self.time_zone, "localize"):
            start_time = self.time_zone.localize(start_time)

        headers = {"accept": "application/json"}

        request_url = self._stromligning_url
        to_param = (
            start_time + timedelta(seconds=tgt_duration * self.time_frame_base)
        ).strftime("%Y-%m-%dT%H:%M")
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
        horizon_end = start_time + timedelta(
            seconds=tgt_duration * self.time_frame_base
        )

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

            logger.debug("[PRICE-IF] Prices from STROMLIGNING fetched successfully.")
            return prices

    def __retrieve_prices_from_smartenergy_at(self, tgt_duration, start_time=None):
        """
        Fetches and processes electricity prices from SmartEnergy AT API.

        Args:
            tgt_duration (int): The horizon in slots (48 hourly, 192 quarter-hourly).
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
            # Local midnight, not the top of the current hour: slot 0 of every
            # array this interface produces is midnight, so defaulting to "now"
            # placed a timestamped series `now.hour` slots early. Production
            # always passes an explicit anchor, which is why this never bit -
            # but the feed-in interface already defaults this way and the
            # asymmetry is a trap for the next caller.
            start_time = local_midnight(self.time_zone)
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
                    forecast_start_idx = len(extended_prices)
                    extended_prices.extend(forecast_prices)
                    self._set_forecast_metadata(
                        start_index=forecast_start_idx,
                        forecast_type="smart_forecast",
                        source="energyforecast.de",
                    )
                else:
                    # Use simple price repetition
                    forecast_start_idx = len(extended_prices)
                    extended_prices.extend(hourly_prices[:remaining_hours])
                    self._set_forecast_metadata(
                        start_index=forecast_start_idx,
                        forecast_type="simple_repetition",
                        source=None,
                    )
            else:
                # All prices are complete real data from SmartEnergy AT
                self._set_forecast_metadata(
                    start_index=None,
                    forecast_type="all_real",
                    source=None,
                )

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
                    forecast_start_idx = len(extended_prices)
                    extended_prices.extend(forecast_prices)
                    self._set_forecast_metadata(
                        start_index=forecast_start_idx,
                        forecast_type="smart_forecast",
                        source="energyforecast.de",
                    )
                else:
                    # Use simple price repetition
                    forecast_start_idx = len(extended_prices)
                    extended_prices.extend(prices_15min[:remaining_slots])
                    self._set_forecast_metadata(
                        start_index=forecast_start_idx,
                        forecast_type="simple_repetition",
                        source=None,
                    )
            else:
                # All prices are complete real data from SmartEnergy AT
                self._set_forecast_metadata(
                    start_index=None,
                    forecast_type="all_real",
                    source=None,
                )

        # Catch case where all prices are zero (or data is empty)
        if not any(extended_prices):
            logger.error(
                "[PRICE-IF] SMARTENERGY_AT API returned only zero prices or empty data."
            )
            return []

        logger.debug("[PRICE-IF] Prices from SMARTENERGY_AT fetched successfully.")
        return extended_prices

    def _should_call_energyforecast(self):
        """
        Determine if we should call energyforecast.de API based on throttling.

        Throttles API calls to avoid rate limiting:
        - Always call on first prediction request
        - Always call after midnight (when predictions for new day needed)
        - Otherwise, only call if 1+ hour has passed since last successful call
        - Use cached prediction within the 1-hour window

        Returns:
            bool: True if we should call the API, False if we should use cache.
        """
        now = datetime.now(self.time_zone)
        today = now.date()

        # First call ever - always proceed
        if self._last_energyforecast_call_time is None:
            logger.debug(
                "[PRICE-IF] First energyforecast call - proceeding (no prior call)"
            )
            return True

        # New calendar day detected - always call (handles midnight crossing)
        if self._last_energyforecast_call_date != today:
            logger.debug(
                "[PRICE-IF] New day detected - calling energyforecast "
                "(last: %s, today: %s)",
                self._last_energyforecast_call_date,
                today,
            )
            return True

        # Check if 1 hour has passed since last call
        time_since_last_call = now - self._last_energyforecast_call_time
        if time_since_last_call.total_seconds() >= 3600:  # 1 hour = 3600 seconds
            logger.debug(
                "[PRICE-IF] 1+ hour passed since last call (%.0f seconds ago) "
                "- calling energyforecast",
                time_since_last_call.total_seconds(),
            )
            return True

        # Within 1 hour of last call - use cached result
        logger.debug(
            "[PRICE-IF] Using cached energyforecast result "
            "(called %.0f seconds ago, threshold: 3600s)",
            time_since_last_call.total_seconds(),
        )
        return False

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

        Uses intelligent API throttling to manage rate limits:
        - Only calls API if 1+ hour has passed since last call or on new calendar day
        - Caches predictions for reuse within the 1-hour window
        - Minimizes API usage while maintaining current predictions

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

        # Apply smart caching - only call API if 1+ hour passed or new day
        if not self._should_call_energyforecast():
            if self._energyforecast_cache:
                logger.debug(
                    "[PRICE-IF] Using cached energyforecast prediction (%d prices)",
                    len(self._energyforecast_cache),
                )
                return self._energyforecast_cache
            logger.debug("[PRICE-IF] Throttled energyforecast call, no cache available")
            return []

        # Fetch full 48h of EPEX spot prices from energyforecast (NO markup applied)
        resolution = "QUARTER_HOURLY" if self.time_frame_base == 900 else "HOURLY"

        params = {
            "token": self.energyforecast_token,
            "market_zone": self.energyforecast_market_zone,
            "resolution": resolution,
            "fixed_cost_cent": 0,  # Get raw EPEX prices for learning
            "vat": 0,  # No markup - we'll learn the relationship
        }

        # Update call tracking before attempting API call
        now = datetime.now(self.time_zone)
        self._last_energyforecast_call_time = now
        self._last_energyforecast_call_date = now.date()

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
        except (ValueError, TypeError, ZeroDivisionError, OverflowError) as e:
            logger.warning("[PRICE-IF] Linear regression failed: %s", e)
            return []

        # Convert offset from EUR/Wh to ct/kWh for logging
        offset_ct_kwh = offset * 100000

        # Log sample comparison for learning quality (each as standalone entry for web UI)
        for i in range(min(3, overlap_size)):
            logger.info(
                "[PRICE-IF] Learning from %d samples - "
                "Sample %d: EPEX %.2f ct/kWh \u2192 Primary %.2f ct/kWh",
                overlap_size,
                i,
                epex_samples[i] * 100000,
                primary_samples[i] * 100000,
            )

        # Validate learned parameters
        if not ENERGYFORECAST_MIN_FACTOR <= factor <= ENERGYFORECAST_MAX_FACTOR:
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
            # Handle negative prices: if result is negative,
            # keep it (user pays negative = gets paid)
            adapted_prices.append(round(adapted_price, 9))

        logger.info(
            "[PRICE-IF] Generated %d adapted forecast prices (range: %.2f to %.2f ct/kWh)",
            len(adapted_prices),
            min(adapted_prices) * 100000 if adapted_prices else 0,
            max(adapted_prices) * 100000 if adapted_prices else 0,
        )

        # Cache the result for use within the next hour
        self._energyforecast_cache = adapted_prices

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

    def _retry_request(self, request_func, error_handler, max_retries=3, delay=1):
        """Delegate to the shared retry loop (interfaces.http_fetch)."""
        return retry_request(
            request_func, error_handler, max_retries=max_retries, delay=delay
        )

    def __retrieve_prices_from_fixed24h_array(
        self, tgt_duration, start_time=None  # pylint: disable=unused-argument
    ):
        """
        Returns a fixed 24-hour array of prices.

        Args:
            tgt_duration (int): The horizon in slots (48 hourly, 192 quarter-hourly).
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
        # tgt_duration counts slots; this path works in hours until the final
        # expansion below.
        tgt_hours = int(tgt_duration * self.time_frame_base) // 3600
        if len(extended_prices) < tgt_hours:
            remaining_hours = tgt_hours - len(extended_prices)
            extended_prices.extend(extended_prices[:remaining_hours])
        # for 15 min output only extend the array
        if self.time_frame_base == 900:
            extended_prices_15min = []
            for price in extended_prices:
                extended_prices_15min.extend([price] * 4)
            extended_prices = extended_prices_15min
        return extended_prices

    def __retrieve_prices_from_url(self, tgt_duration, start_time=None):
        """
        Retrieve grid prices from timeseries data source (Home Assistant or HTTP).

        Unified approach for both HA sensors and custom HTTP servers using the
        canonical timeseries format: [{start, end, value}, ...] — the format EVCC
        publishes, so an EVCC endpoint or an EVCC-shaped HA template sensor works
        unchanged. See timeseries_normalizer for the exact contract.

        Config fields used:
        - data_url: Full HTTP endpoint URL (HA or HTTP custom endpoint)
        - data_path: JSON path to timeseries array (e.g., 'attributes.data')
        - data_token: Optional bearer token for authentication
        - value_unit: Unit of the "value" field (default EUR/kWh, as EVCC delivers)

        Args:
            tgt_duration (int): Target duration in hours (48) or 15-min slots (192)
            start_time (datetime, optional): Optional start time
        
        Returns:
            list: Grid prices in EUR/Wh for each time period
        """
        if not self.data_url:
            logger.error(
                "[PRICE-IF] Data URL (data_url) not configured for timeseries"
            )
            return []


        logger.debug(
            "[PRICE-IF] Fetching prices from timeseries source: %s (path: %s, unit: %s)",
            self.data_url,
            self.data_path,
            self.value_unit,
        )

        def request_and_parse():
            """Fetch data and extract timeseries using data_path."""
            response = requests.get(
                self.data_url, **bearer_request_kwargs(self.data_token)
            )
            response.raise_for_status()
            response_data = response.json()

            # Extract timeseries using data_path
            timeseries = extract_json_path(
                response_data, self.data_path, label="PRICE-IF"
            )

            if not isinstance(timeseries, list):
                msg = f"Data at path '{self.data_path}' is not array"
                raise ValueError(msg)

            return timeseries

        def error_handler(error_type, exception):
            logger.error(f"[PRICE-IF] URL data source error: {exception}")
            return None

        timeseries = self._retry_request(request_and_parse, error_handler)
        if not timeseries:
            logger.error("[PRICE-IF] No valid timeseries data from source")
            return []

        # Parse and validate timeseries
        try:
            prices = self.__parse_price_timeseries(
                timeseries,
                tgt_duration,
                None,
                start_time,
                value_unit=self.value_unit,
            )
            if not prices:
                logger.error("[PRICE-IF] Failed to parse price timeseries data")
                return []

            # Clear any previous errors on success
            self.consecutive_failures = 0
            self.last_successful_prices = prices.copy()

            # State the unit once per unit change. The canonical unit moved from
            # EUR/Wh to EUR/kWh, so a config carried over from an earlier version
            # silently changes meaning — this makes the active interpretation visible
            # in the log without spamming it every cycle.
            if getattr(self, "_logged_value_unit", None) != self.value_unit:
                logger.info(
                    "[PRICE-IF] Timeseries prices interpreted as '%s' "
                    "(first slot: %.2f ct/kWh)",
                    self.value_unit,
                    prices[0] * 100_000,
                )
                self._logged_value_unit = self.value_unit

            logger.debug(
                "[PRICE-IF] Timeseries prices received: %d values, "
                "first 12h (EUR/Wh): %.9f, %.9f, ...",
                len(prices),
                prices[0],
                prices[1] if len(prices) > 1 else 0,
            )

            return prices

        except (ValueError, TypeError) as e:
            logger.error(f"[PRICE-IF] Error parsing price timeseries: {e}")
            return []

    def __retrieve_prices_from_evcc(self, tgt_duration, start_time=None):
        """
        Retrieve prices from EVCC /api/tariff/grid endpoint.

        EVCC provides grid consumption prices via REST API with 15-minute intervals.
        Converts EVCC rate format to standard EUR/Wh timeseries format.

        Args:
            tgt_duration (int): Target duration in hours (24 or 48)
            start_time (datetime, optional): Start time for price retrieval

        Returns:
            list: Prices in EUR/Wh format, or empty list on failure
        """
        if not self.evcc_interface or not self.evcc_interface.url:
            logger.warning(
                "[PRICE-IF] EVCC interface not available or URL not configured. "
                "Cannot fetch prices from EVCC."
            )
            return []

        try:
            evcc_url = self.evcc_interface.url.rstrip("/")

            # Fetch grid tariff (consumption prices)
            grid_url = f"{evcc_url}/api/tariff/grid"

            try:
                response = requests.get(grid_url, **bearer_request_kwargs())
                response.raise_for_status()
            except requests.exceptions.RequestException as req_err:
                logger.error(f"[PRICE-IF] Failed to fetch EVCC grid tariff: {req_err}")
                return []

            grid_data = response.json()

            # Parse EVCC response format
            # Expected format: {"rates": [{"start": "...", "end": "...", "value": 0.125}, ...]}
            if not isinstance(grid_data, dict):
                logger.error(f"[PRICE-IF] Invalid EVCC response format: {type(grid_data)}")
                return []

            rates = grid_data.get("rates", [])
            if not isinstance(rates, list) or not rates:
                logger.error("[PRICE-IF] No rates found in EVCC response")
                return []

            # Log concise summary instead of full response
            if rates:
                first_rate = rates[0].get("start", "unknown")
                last_rate = rates[-1].get("start", "unknown")
                prices_in_kwh = [float(r.get("value", 0)) for r in rates if "value" in r]
                if prices_in_kwh:
                    avg_price = sum(prices_in_kwh) / len(prices_in_kwh)
                    min_price = min(prices_in_kwh)
                    max_price = max(prices_in_kwh)
                    logger.debug(
                        "[PRICE-IF] EVCC grid tariff: %d rates from %s to %s, "
                        "avg=%.4f EUR/kWh, range=[%.4f, %.4f]",
                        len(rates),
                        first_rate,
                        last_rate,
                        avg_price,
                        min_price,
                        max_price,
                    )

            # EVCC provides rates with start, end, and value (EUR/kWh). Parse
            # them through the shared normalizer — the ends are authoritative,
            # the timestamps keep their own offsets — and convert the unit via
            # the common table.
            try:
                # Lenient: EVCC publishes rates straight from whatever tariff
                # plugin the user configured, and one malformed rate must not
                # cost the other 191. A skipped rate leaves a gap the placement
                # reports, which is strictly better than losing the fetch.
                timeseries = normalize_entries(
                    rates, self.time_zone, label="PRICE-IF", lenient=True
                )
                convert_price_values(timeseries, "EUR/kWh")
            except TimeseriesFormatError as exc:
                logger.error("[PRICE-IF] Invalid EVCC rates: %s", exc)
                return []

            # Same fallback the generic parser uses. Detecting from the first
            # delta alone is too strict here: skipping an unusable rate leaves
            # an irregular gap, and refusing the whole fetch because of it would
            # undo the point of reading the payload leniently.
            resolution_seconds = detect_resolution_seconds(
                timeseries
            ) or median_span_seconds(timeseries)
            if resolution_seconds is None:
                logger.warning("[PRICE-IF] Could not detect timeseries resolution")
                return []

            # Calculate expected timeseries length based on resolution
            if resolution_seconds == 900:  # 15-min resolution
                expected_timeseries_count = 192  # 192 * 15min = 2880min = 48h
            elif resolution_seconds == 3600:  # Hourly resolution
                expected_timeseries_count = 48  # 48 * 1h = 48h
            else:
                expected_timeseries_count = 48  # Fallback

            actual_timeseries_count = len(timeseries)
            is_incomplete = actual_timeseries_count < expected_timeseries_count

            # Parse the converted timeseries using standard parser
            # Pass resolution_seconds and start_time for timestamp-aware conversion
            window = self.__place_price_timeseries(
                timeseries, tgt_duration, resolution_seconds, start_time
            )
            prices = window.values

            expected_slots = horizon_slots(self.time_frame_base)

            # Validate EVCC data: only accept exactly 1 or 2 complete full days
            # Partial days, gaps, or mixed data → treat as incomplete error
            # This ensures cyclic padding only uses trustworthy complete daily patterns
            is_valid_complete_data = False
            if resolution_seconds == 900:  # 15-min resolution
                # Accept exactly 96 entries (1 day) or 192 entries (2 days)
                is_valid_complete_data = actual_timeseries_count in [96, 192]
            elif resolution_seconds == 3600:  # Hourly resolution
                # Accept exactly 24 entries (1 day) or 48 entries (2 days)
                is_valid_complete_data = actual_timeseries_count in [24, 48]

            if is_incomplete and is_valid_complete_data:
                logger.debug(
                    "[PRICE-IF] EVCC provided exactly %d entries (%s) — treating as "
                    "valid complete data and using cyclic pattern for gap-filling",
                    actual_timeseries_count,
                    "1 full day" if actual_timeseries_count in [96, 24] else "2 full days",
                )
                is_incomplete = False
            elif is_incomplete and not is_valid_complete_data:
                logger.warning(
                    "[PRICE-IF] EVCC data is incomplete/invalid: %d entries provided "
                    "(expected exactly 96 or 192 for 15-min, or 24 or 48 for hourly). "
                    "Falling back to cache or energyforecast",
                    actual_timeseries_count,
                )

            if prices and is_incomplete:
                # Data was incomplete, try fallback chain.
                #
                # The boundary comes from the PLACEMENT, never from len(rates):
                # an entry count equals a slot index only when the series starts
                # exactly at the anchor and its resolution matches
                # time_frame_base. It does not when EVCC publishes from the
                # current period onwards (the leading slots are backfill, not
                # real data) nor when an hourly source runs on a 15-minute
                # system, where 30 entries cover 120 slots.
                num_real_slots = window.real_slots
                num_missing_slots = expected_slots - num_real_slots

                # For energyforecast, pass the actual number of prices needed
                # (not converted to hours) - function returns that many prices
                num_forecast_prices_needed = num_missing_slots

                logger.info(
                    "[PRICE-IF] EVCC returned incomplete data: got %d real price slots, "
                    "need %d total (%d prices missing). Attempting fallback chain...",
                    num_real_slots,
                    expected_slots,
                    num_forecast_prices_needed,
                )

                # The real head of the placed array. Everything from
                # num_real_slots on is fill_price_gaps' invention, which the
                # fallbacks below try to replace with something better.
                real_prices = prices[:num_real_slots] if num_real_slots > 0 else []

                def _complete(head, tail):
                    """Head + tail, trimmed or padded to exactly the horizon.

                    Every branch must hand back a full-horizon array: the
                    optimizer pads a short one with its last value, which is how
                    a 60-value array silently became 192 flat slots.
                    """
                    combined = list(head) + list(tail)
                    if len(combined) > expected_slots:
                        return combined[:expected_slots]
                    if len(combined) < expected_slots:
                        filler = prices[len(combined):expected_slots]
                        combined.extend(filler)
                    return combined

                if real_prices and num_missing_slots > 0:
                    # Fallback 1: Try energyforecast.de smart price prediction
                    forecast_prices = self._fetch_adaptive_energyforecast_fallback(
                        known_prices=real_prices,
                        num_missing_hours=num_forecast_prices_needed,
                    )

                    if forecast_prices and len(forecast_prices) == num_forecast_prices_needed:
                        logger.info(
                            "[PRICE-IF] EVCC incomplete, using energyforecast.de smart "
                            "prediction to fill %d missing price slots",
                            num_forecast_prices_needed,
                        )
                        prices = _complete(real_prices, forecast_prices)

                        self._set_forecast_metadata(
                            start_index=num_real_slots,
                            forecast_type="smart_forecast",
                            source="energyforecast.de",
                        )
                    # Fallback 2: Try yesterday's prices from history
                    elif (
                        len(self.last_successful_prices) > 0
                        and len(self.last_successful_prices) == expected_slots
                    ):
                        logger.info(
                            "[PRICE-IF] EVCC incomplete, using yesterday's prices to fill "
                            "%d missing price slots",
                            num_missing_slots,
                        )
                        yesterday_fill = self.last_successful_prices[num_real_slots:]
                        prices = _complete(real_prices, yesterday_fill)
                        self._set_forecast_metadata(
                            start_index=num_real_slots,
                            forecast_type="fallback_history",
                            source="yesterday_prices",
                        )
                    # Fallback 3: keep the placement's own gap policy.
                    else:
                        logger.info(
                            "[PRICE-IF] EVCC incomplete, no fallback available. "
                            "Repeating today's prices for tomorrow."
                        )
                        # fill_price_gaps already repeated today's pattern into
                        # tomorrow's uncovered slots, so `prices` IS the
                        # repetition and is already horizon-length. Rebuilding
                        # it as real_prices + real_prices produced a
                        # 2*num_real_slots array instead.
                        self._set_forecast_metadata(
                            start_index=num_real_slots,
                            forecast_type="simple_repetition",
                            source=None,
                        )
            elif prices and not is_incomplete:
                # "Complete" here means EVCC sent whole days, not that the days
                # it sent cover the horizon: exactly 24 hourly rates is accepted
                # as valid, and fill_price_gaps then invents the second day from
                # the first. Reporting that as all_real served invented prices
                # to the UI as measured ones, so keep what the placement found.
                if window.real_slots < expected_slots:
                    self._set_forecast_metadata(
                        start_index=window.real_slots,
                        forecast_type="simple_repetition",
                        source=None,
                    )
                else:
                    self._set_forecast_metadata(
                        start_index=None,
                        forecast_type="all_real",
                        source=None,
                    )

            if prices:
                self.consecutive_failures = 0
                self.last_successful_prices = prices.copy()
                logger.info(
                    "[PRICE-IF] EVCC prices finalized: %d values, "
                    "first 4 rates (EUR/Wh): %.9f, %.9f, %.9f, %.9f",
                    len(prices),
                    prices[0] if len(prices) > 0 else 0,
                    prices[1] if len(prices) > 1 else 0,
                    prices[2] if len(prices) > 2 else 0,
                    prices[3] if len(prices) > 3 else 0,
                )

            return prices

        except Exception as e:
            logger.error(f"[PRICE-IF] Unexpected error fetching EVCC prices: {e}")
            return []

    def __parse_price_timeseries(self, *args, **kwargs):
        """The served array alone; see __place_price_timeseries for the window."""
        return self.__place_price_timeseries(*args, **kwargs).values

    def __place_price_timeseries(
        self,
        timeseries,
        tgt_duration,
        resolution_seconds=None,
        start_time=None,
        value_unit=None,
    ):
        """
        Parse a price timeseries and lay it onto the slot grid by timestamp.

        Standardized format: [{start, end, value}, ...] — accepted field
        spellings, authoritative ends and capped derived spans per
        timeseries_normalizer. Prices are a rate, so placement is intensive: a
        slot averages what covers it, a coarse entry holds its value across
        every sub-slot (timeseries_grid). An entry's position in the payload
        means nothing — a source that also publishes yesterday lands on
        today's slots by its timestamps (discussion #214).

        Slots the series does not cover are filled by fill_price_gaps (first
        known price backwards, last known forwards, tomorrow repeats today's
        pattern); a filled tail is reported via _set_forecast_metadata so the
        UI can hatch it. A series that never touches the window returns [] so
        the caller's fallback chain serves the last good data instead of
        fiction.

        Args:
            timeseries: List of price entries.
            tgt_duration: Unused; the horizon comes from time_frame_base.
                Retained for call-site compatibility.
            resolution_seconds: Pre-detected resolution, or None to detect.
            start_time: Window anchor (local midnight). None falls back to
                local_midnight(self.time_zone).
            value_unit: Unit of the incoming values (see
                timeseries_normalizer.PRICE_UNIT_TO_EUR_PER_WH). ``None``
                means the caller already supplies canonical EUR/Wh values.

        Returns:
            PriceWindow: the full-horizon EUR/Wh array plus where real data
            started and where invention begins. ``PriceWindow([], 0, 0)`` on
            error, so the caller's fallback chain takes over.
        """
        if not timeseries or not isinstance(timeseries, list):
            logger.error("[PRICE-IF] Price timeseries is not a list")
            return PriceWindow([], 0, 0)

        try:
            timeseries = normalize_entries(
                timeseries, self.time_zone, label="PRICE-IF"
            )
            if value_unit is not None:
                convert_price_values(timeseries, value_unit)
        except TimeseriesFormatError as exc:
            logger.error("[PRICE-IF] Invalid price timeseries: %s", exc)
            return PriceWindow([], 0, 0)

        if value_unit is not None:
            warning = price_plausibility_message(
                [entry["value"] for entry in timeseries], value_unit
            )
            if warning:
                logger.warning("[PRICE-IF] %s", warning)

        if resolution_seconds is None:
            resolution_seconds = detect_resolution_seconds(
                timeseries
            ) or median_span_seconds(timeseries)
            if resolution_seconds is None:
                logger.error("[PRICE-IF] Could not detect price timeseries resolution")
                return PriceWindow([], 0, 0)

        # Any grid places by overlap: a coarser price holds its value across
        # every slot it spans, a finer one averages into each slot. An hourly
        # source on a 15-minute system therefore just works. Only a grid so
        # coarse that one entry blankets many slots is worth flagging — that is
        # almost always a daily-average sensor configured by mistake.
        if resolution_seconds > 4 * self.time_frame_base:
            logger.warning(
                "[PRICE-IF] Source resolution is %ds — each entry spans %d slots "
                "and holds its price across all of them; check whether this is "
                "really a per-slot price series",
                resolution_seconds,
                resolution_seconds // self.time_frame_base,
            )
        elif resolution_seconds not in (900, 3600):
            logger.info(
                "[PRICE-IF] Unusual source resolution (%ds) — values are placed "
                "by overlap on the %ds grid",
                resolution_seconds,
                self.time_frame_base,
            )

        anchor = (
            start_time if start_time is not None else local_midnight(self.time_zone)
        )
        slot_count = horizon_slots(self.time_frame_base)
        try:
            placement = place_on_grid(
                timeseries,
                anchor,
                slot_count,
                self.time_frame_base,
                KIND_INTENSIVE,
            )
        except TimeseriesGridError as exc:
            logger.error("[PRICE-IF] Invalid price timeseries: %s", exc)
            return PriceWindow([], 0, 0)

        values, leading, forecast_start = fill_price_gaps(
            placement.values, placement.coverage, slots_per_day=slot_count // 2
        )
        if not values:
            logger.warning(
                "[PRICE-IF] None of %d price entries fell into the %d-slot window "
                "starting %s — keeping the last good prices. If the source renders "
                "UTC without an offset, every slot is shifted. See %s",
                placement.source_count,
                slot_count,
                anchor.isoformat(),
                TEMPLATE_DOCS_URL,
            )
            return PriceWindow([], 0, 0)

        covered = sum(1 for fraction in placement.coverage if fraction > 0)
        if forecast_start is not None:
            self._set_forecast_metadata(
                start_index=forecast_start,
                forecast_type="simple_repetition",
                source=None,
            )
        else:
            self._set_forecast_metadata(
                start_index=None, forecast_type="all_real", source=None
            )

        if leading >= slot_count // 2:
            # A rolling source fetched mid-day leads by a few hours — normal and
            # quiet. A series that only starts a full day after the anchor is a
            # misconfigured window, and its whole first day is invented.
            logger.warning(
                "[PRICE-IF] The price series only starts %d slot(s) after the "
                "window anchor %s — the first day is backfilled with its first "
                "known price",
                leading,
                anchor.isoformat(),
            )
        trailing = slot_count - forecast_start if forecast_start is not None else 0
        interior = slot_count - covered - trailing - leading
        if interior > 0:
            logger.warning(
                "[PRICE-IF] %d slot(s) inside the covered window carried no price "
                "and were filled from their neighbours — check the source for holes",
                interior,
            )
        if placement.dropped:
            logger.debug(
                "[PRICE-IF] %d of %d price entries fall outside the %d-slot window",
                placement.dropped,
                placement.source_count,
                slot_count,
            )

        values, clamped, worst = clamp_report(values, -0.5, 1.0)
        if clamped:
            logger.warning(
                "[PRICE-IF] %d price value(s) outside the EUR/Wh range, "
                "clamping (worst: %.9f)",
                clamped,
                worst,
            )

        # Round to 9 decimals (EUR precision)
        return PriceWindow(
            values=[round(value, 9) for value in values],
            first_covered=leading,
            # Where invention starts. forecast_start is None when the source
            # covered the final slot, i.e. the whole horizon is real.
            real_slots=forecast_start if forecast_start is not None else slot_count,
        )
