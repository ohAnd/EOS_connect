# -*- coding: utf-8 -*-
"""
This module provides the `FeedInPriceInterface` class for retrieving and processing electricity
feed-in (export) price data from various sources.

Supported sources:
    - Elpris (Dänemark): Spot-Preise für stromexport (in DKK/kWh, converted to ct/kWh)
    - EPEX-Spot (EU/AT): Netto-Börsenpreise via Akkudoktor (in ct/kWh)
    - EVCC: Real-time feed-in tariffs from EVCC charger (in EUR/kWh)
    - Fixed: Statischer Einspeisepreis (in ct/kWh)

Features:
    - Fetches and updates feed-in prices from external APIs
    - All prices use ct/kWh (cent per kilowatt-hour) for consistent user experience
    - Provides dynamic price array to optimizer (instead of constant value)
    - Background thread for periodic price updates with retry and fallback logic
    - Supports both hourly (48h) and 15-minute intervals (96h/192 slots)
    - Handles negative prices and fallback scenarios

Usage:
    config = {
        "source": "elpris_dk",
        "zone": "DK1",
        "static_adder_ct_kwh": 3.5,  # 3.5 ct/kWh (standard unit)
        "multiplier": 1.0,
    }
    feed_in_interface = FeedInPriceInterface(config, time_frame_base=3600, timezone="Europe/Berlin")
    feed_in_interface.update_prices(tgt_duration=48, start_time=datetime.now())
    current_feedin = feed_in_interface.get_current_feedin_prices()
"""

from datetime import datetime, timedelta
import logging
import threading
import requests
import pytz

logger = logging.getLogger("__main__")
logger.info("[FEEDIN-IF] loading module")

ELPRIS_API_BASE = "https://www.elprisenligenu.dk/api/v1/prices"
AKKUDOKTOR_API_PRICES = "https://api.akkudoktor.net/prices"


class FeedInPriceInterface:
    """
    The FeedInPriceInterface class manages electricity feed-in (export) price data retrieval
    and processing from various sources.

    All prices are consistently represented in ct/kWh (cent per kilowatt-hour) for user clarity.

    Attributes:
        source (str): Source of the feed-in price data ('elpris_dk', 'epex_spot', 'fixed')
        zone (str): Price zone for Elpris (DK1 or DK2)
        static_adder_ct_kwh (float): Static adjustment in ct/kWh (e.g., 3.5 for transport costs)
        multiplier (float): Relative multiplier (1.0 = no change, 1.05 = +5%)
        time_frame_base (int): Time frame in seconds (3600 = hourly, 900 = 15-min slots)
        time_zone (pytz.timezone): Timezone for date operations
        current_feedin_prices (list): Current feed-in prices in EUR/Wh
        default_prices (list): Default fallback prices
        last_successful_prices (list): Last successfully fetched prices for fallback
        consecutive_failures (int): Counter for consecutive API failures
    """

    def __init__(self, config, time_frame_base, timezone="UTC", evcc_interface=None):
        """
        Initialize the FeedInPriceInterface.

        Args:
            config (dict): Configuration dictionary with keys:
                - source: 'elpris_dk', 'epex_spot', 'fixed', or 'evcc'
                - zone: 'DK1' or 'DK2' (for elpris_dk only)
                - static_adder_ct_kwh: Static adjustment in ct/kWh (standard unit)
                - multiplier: Relative multiplier (default 1.0)
                - fixed_price_ct_kwh: Fixed price in ct/kWh (for 'fixed' source)
                - negative_price_switch: Boolean to clamp negative prices to 0 (default: False)
            time_frame_base (int): 3600 for hourly, 900 for 15-minute slots
            timezone (str): Timezone identifier (e.g., 'UTC', 'Europe/Berlin')
            evcc_interface: Optional EVCC interface instance for feed-in price retrieval
        """
        self.source = config.get("source", "fixed")
        self.zone = config.get("zone", "DK1")
        self.evcc_interface = evcc_interface

        # Primary: ct/kWh format (standard, user-facing unit)
        # Fallback: Support legacy øre format for backward compatibility
        if "static_adder_ct_kwh" in config:
            self.static_adder_ct_kwh = config.get("static_adder_ct_kwh", 0.0)
        else:
            # Legacy: øre format (øre / 100 = ct/kWh)
            self.static_adder_ct_kwh = config.get("static_adder_oere", 0.0) / 100.0

        self.multiplier = config.get("multiplier", 1.0)

        # Fixed price in ct/kWh
        fixed_price_ct_kwh = config.get("fixed_price_ct_kwh", 0.0)
        # Also try legacy key
        if fixed_price_ct_kwh == 0.0 and "fixed_price" in config:
            fixed_price_ct_kwh = config.get("fixed_price", 0.0)
        # If value is suspiciously small (e.g., EUR instead of ct), convert it
        if fixed_price_ct_kwh < 0.1 and fixed_price_ct_kwh > 0:
            # Looks like EUR/kWh, convert to ct/kWh
            fixed_price_ct_kwh = fixed_price_ct_kwh * 100
        self.fixed_price_ct_kwh = fixed_price_ct_kwh

        # Negative price switching: if True, clamps negative market prices to 0
        self.negative_price_switch = config.get("negative_price_switch", False)

        self.time_frame_base = time_frame_base
        # Handle both string and pytz.timezone objects
        if isinstance(timezone, str):
            self.time_zone = pytz.timezone(timezone)
        else:
            # Already a pytz timezone object
            self.time_zone = timezone
        self.current_feedin_prices = []

        # Default fallback prices (0.5 ct/kWh = 0.000005 EUR/Wh)
        self.default_prices = [0.000005] * 48

        # Retry mechanism
        self.last_successful_prices = []
        self.consecutive_failures = 0
        self.max_failures = 24  # Max consecutive failures before using default

        # Background thread attributes
        self._update_thread = None
        self._stop_event = threading.Event()
        self.update_interval = 900  # 15 minutes in seconds

        self._validate_config()
        logger.info(
            "[FEEDIN-IF] Initialized with source: %s, zone: %s, adder: %.2f ct/kWh, "
            "multiplier: %.2f",
            self.source,
            self.zone,
            self.static_adder_ct_kwh,
            self.multiplier,
        )

        # Start background update service
        self._start_update_service()

    def _validate_config(self):
        """Validate configuration parameters."""
        valid_sources = ["fixed", "elpris_dk", "epex_spot", "evcc"]
        if self.source not in valid_sources:
            logger.error(
                "[FEEDIN-IF] Invalid source: %s. Defaulting to 'fixed'.", self.source
            )
            self.source = "fixed"

        if self.source == "elpris_dk" and self.zone not in ["DK1", "DK2"]:
            logger.error(
                "[FEEDIN-IF] Invalid zone for Elpris: %s. Defaulting to DK1.",
                self.zone,
            )
            self.zone = "DK1"

    def _start_update_service(self):
        """Start background thread for periodic price updates."""
        if self._update_thread is None or not self._update_thread.is_alive():
            self._stop_event.clear()
            self._update_thread = threading.Thread(
                target=self._update_prices_loop, daemon=True
            )
            self._update_thread.start()
            logger.debug("[FEEDIN-IF] Background update service started")

    def stop(self):
        """Stop the background update service."""
        self._stop_event.set()
        if self._update_thread and self._update_thread.is_alive():
            self._update_thread.join(timeout=5)
            logger.debug("[FEEDIN-IF] Background update service stopped")

    def _update_prices_loop(self):
        """
        Background loop that periodically updates feed-in prices.
        Runs every 15 minutes (900 seconds).
        """
        try:
            # Initial update on startup
            start_time = datetime.now(self.time_zone).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            tgt_duration = 192 if self.time_frame_base == 900 else 48
            self.update_prices(tgt_duration, start_time)

            while not self._stop_event.is_set():
                # Wait for update interval or stop signal
                if self._stop_event.wait(timeout=self.update_interval):
                    break

                # Periodic update
                start_time = datetime.now(self.time_zone).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                tgt_duration = 192 if self.time_frame_base == 900 else 48
                self.update_prices(tgt_duration, start_time)
                logger.debug("[FEEDIN-IF] Periodic feed-in price update completed")

        except (requests.RequestException, KeyError, ValueError, AttributeError,
                TypeError, OSError, RuntimeError) as e:
            logger.error(
                "[FEEDIN-IF] Price fetch failed: %s | Config: #price | ACTION REQUIRED",
                e,
            )

        # Restart if not intentionally stopped
        if not self._stop_event.is_set():
            logger.warning(
                "[FEEDIN-IF] Background thread stopped unexpectedly, restarting..."
            )
            self._start_update_service()

    def update_prices(self, tgt_duration, start_time=None):
        """
        Update current feed-in prices based on source and configuration.

        Args:
            tgt_duration (int): Number of hours (48) or 15-min slots (192)
            start_time (datetime, optional): Start time (default: now at midnight)
        """
        if start_time is None:
            start_time = datetime.now(self.time_zone).replace(
                minute=0, second=0, microsecond=0
            )

        prices = self._retrieve_prices(tgt_duration, start_time)

        if not prices:
            self.consecutive_failures += 1

            if (self.consecutive_failures <= self.max_failures and
                    self.last_successful_prices):
                logger.warning(
                    "[FEEDIN-IF] No prices retrieved (failure %d/%d). "
                    "Using last successful prices.",
                    self.consecutive_failures,
                    self.max_failures,
                )
                prices = self.last_successful_prices[:tgt_duration]
            else:
                logger.error(
                    "[FEEDIN-IF] Failed to retrieve prices after %d attempts. "
                    "Using default prices.",
                    self.max_failures,
                )
                prices = self.default_prices
                if tgt_duration == 192:  # 15-min slots
                    prices = [p for p in prices for _ in range(4)]
        else:
            self.consecutive_failures = 0
            self.last_successful_prices = prices.copy()

        self.current_feedin_prices = prices
        logger.debug(
            "[FEEDIN-IF] Prices updated for %d slots starting from %s",
            tgt_duration,
            start_time.strftime("%Y-%m-%d %H:%M"),
        )

    def get_current_feedin_prices(self):
        """
        Get current feed-in prices.

        Returns:
            list: Feed-in prices in EUR/Wh
        """
        return self.current_feedin_prices

    def _retrieve_prices(self, tgt_duration, start_time):
        """
        Retrieve prices based on configured source.

        Args:
            tgt_duration (int): Target duration
            start_time (datetime): Start time for retrieval

        Returns:
            list: Prices in EUR/Wh or empty list on error
        """
        if self.source == "elpris_dk":
            return self._fetch_elpris_prices(tgt_duration, start_time)
        elif self.source == "epex_spot":
            return self._fetch_epex_spot_prices(tgt_duration, start_time)
        elif self.source == "evcc":
            return self._fetch_evcc_prices(tgt_duration, start_time)
        elif self.source == "fixed":
            return self._fetch_fixed_price(tgt_duration, start_time)
        else:
            logger.error(
                "[FEEDIN-IF] Unknown source: %s. Defaulting to fixed price.", self.source
            )
            return self._fetch_fixed_price(tgt_duration, start_time)

    def _fetch_elpris_prices(self, tgt_duration, start_time):
        """
        Fetch feed-in prices from Elpris API (Dänemark).

        API: https://www.elprisenligenu.dk/elpris-api
        Returns prices in DKK/kWh, converts to ct/kWh (0.134 DKK/EUR)

        Args:
            tgt_duration (int): 48 (hourly) or 192 (15-min slots)
            start_time (datetime): Start time

        Returns:
            list: Prices in EUR/Wh or empty list on error
        """
        try:
            # API format: YYYY/MM-DD_ZONE.json
            date_str = start_time.strftime("%Y/%m-%d")
            url = f"{ELPRIS_API_BASE}/{date_str}_{self.zone}.json"

            logger.debug("[FEEDIN-IF] Fetching Elpris prices from: %s", url)
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()
            prices_dkk = data.get("prices", [])

            if not prices_dkk:
                logger.warning("[FEEDIN-IF] Elpris API returned empty price list")
                return []

            # Elpris returns 24 hourly prices in DKK/kWh
            # DKK/EUR rate ≈ 7.46, so 1 DKK/kWh = 100/7.46 ≈ 13.41 ct/kWh
            dkk_per_eur = 7.46
            prices_eur_wh = []

            for price_entry in prices_dkk:
                price_dkk_kwh = price_entry.get("price", 0.0)

                # DKK/kWh → ct/kWh
                price_ct_kwh = price_dkk_kwh * 100 / dkk_per_eur

                # Add static adder and apply multiplier
                price_with_adder = price_ct_kwh + self.static_adder_ct_kwh
                price_adjusted = price_with_adder * self.multiplier

                # ct/kWh → EUR/Wh (1 ct/kWh = 0.00001 EUR/Wh)
                price_eur_wh = round(price_adjusted / 100000, 9)

                # Clamp to 0 if negative_price_switch enabled and price negative
                if self.negative_price_switch and price_eur_wh < 0:
                    price_eur_wh = 0.0

                prices_eur_wh.append(price_eur_wh)

            logger.debug(
                "[FEEDIN-IF] Fetched %d Elpris prices from %s",
                len(prices_eur_wh),
                self.zone,
            )

            # Extend to 48 or 96 hours if only 24h available
            prices_eur_wh = self._extend_prices_to_duration(prices_eur_wh, tgt_duration)

            return prices_eur_wh

        except requests.RequestException as e:
            logger.error("[FEEDIN-IF] Elpris API request failed: %s", e)
            return []
        except (KeyError, ValueError) as e:
            logger.error("[FEEDIN-IF] Elpris API response parsing failed: %s", e)
            return []

    def _fetch_epex_spot_prices(self, tgt_duration, start_time):
        """
        Fetch feed-in prices from EPEX-Spot via Akkudoktor API.

        Uses Akkudoktor's netto prices (without taxes/fees) as base.
        Applies static_adder and multiplier.

        Args:
            tgt_duration (int): 48 (hourly) or 192 (15-min slots)
            start_time (datetime): Start time

        Returns:
            list: Prices in EUR/Wh or empty list on error
        """
        try:
            start_date = start_time.strftime("%Y-%m-%d")
            end_date = (start_time + timedelta(days=1)).strftime("%Y-%m-%d")
            url = f"{AKKUDOKTOR_API_PRICES}?start={start_date}&end={end_date}"

            logger.debug("[FEEDIN-IF] Fetching EPEX prices from: %s", url)
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()
            prices_list = data.get("values", [])

            if not prices_list:
                logger.warning("[FEEDIN-IF] Akkudoktor API returned empty price list")
                return []

            prices_eur_wh = []
            for price_entry in prices_list:
                # API returns prices in ct/kWh (eurocentPerKWh)
                price_ct_kwh = price_entry.get("marketpriceEurocentPerKWh", 0.0)

                # Add static adder (already in ct/kWh) and apply multiplier
                price_with_adder = price_ct_kwh + self.static_adder_ct_kwh
                price_adjusted = price_with_adder * self.multiplier

                # Convert ct/kWh → EUR/Wh (1 ct/kWh = 0.00001 EUR/Wh)
                price_eur_wh = round(price_adjusted / 100000, 9)
                # Clamp to 0 if negative_price_switch enabled and price negative
                if self.negative_price_switch and price_eur_wh < 0:
                    price_eur_wh = 0.0
                prices_eur_wh.append(price_eur_wh)

            logger.debug(
                "[FEEDIN-IF] Fetched %d EPEX prices from Akkudoktor",
                len(prices_eur_wh),
            )

            # Extend to 48 or 96 hours if needed
            prices_eur_wh = self._extend_prices_to_duration(prices_eur_wh, tgt_duration)

            return prices_eur_wh

        except requests.RequestException as e:
            logger.error("[FEEDIN-IF] Akkudoktor API request failed: %s", e)
            return []
        except (KeyError, ValueError) as e:
            logger.error("[FEEDIN-IF] Akkudoktor API response parsing failed: %s", e)
            return []

    def _fetch_evcc_prices(self, tgt_duration, start_time):
        """
        Fetch feed-in prices from EVCC /api/tariff/feedin endpoint.

        EVCC provides real-time feed-in tariffs via REST API.
        Prices are used as-is (no static adder/multiplier applied, unlike grid prices).

        Args:
            tgt_duration (int): 48 (hourly) or 192 (15-min slots)
            start_time (datetime): Start time

        Returns:
            list: Prices in EUR/Wh or empty list on error
        """
        # Optional dependency: EVCC not required
        if not self.evcc_interface or not self.evcc_interface.url:
            logger.warning(
                "[FEEDIN-IF] EVCC interface not available or URL not configured. "
                "Cannot fetch feed-in prices from EVCC."
            )
            return []

        try:
            evcc_url = self.evcc_interface.url.rstrip("/")

            # Fetch feed-in tariff (export prices)
            feed_in_url = f"{evcc_url}/api/tariff/feedin"
            headers = {"Content-Type": "application/json"}

            try:
                response = requests.get(feed_in_url, headers=headers, timeout=10)
                response.raise_for_status()
            except requests.exceptions.RequestException as req_err:
                logger.error(f"[FEEDIN-IF] Failed to fetch EVCC feed-in tariff: {req_err}")
                return []

            feed_in_data = response.json()

            # Parse EVCC response format
            # Expected format: {"rates": [{"start": "...", "end": "...", "value": 0.125}, ...]}
            if not isinstance(feed_in_data, dict):
                logger.error(f"[FEEDIN-IF] Invalid EVCC response format: {type(feed_in_data)}")
                return []

            rates = feed_in_data.get("rates", [])
            if not isinstance(rates, list) or not rates:
                logger.error("[FEEDIN-IF] No rates found in EVCC feed-in response")
                return []

            # Log concise summary
            if rates:
                first_rate = rates[0].get("start", "unknown")
                last_rate = rates[-1].get("start", "unknown")
                prices_in_kwh = [float(r.get("value", 0)) for r in rates if "value" in r]
                if prices_in_kwh:
                    avg_price = sum(prices_in_kwh) / len(prices_in_kwh)
                    min_price = min(prices_in_kwh)
                    max_price = max(prices_in_kwh)
                    logger.debug(
                        "[FEEDIN-IF] EVCC feed-in tariff: %d rates from %s to %s, "
                        "avg=%.4f EUR/kWh, range=[%.4f, %.4f]",
                        len(rates),
                        first_rate,
                        last_rate,
                        avg_price,
                        min_price,
                        max_price,
                    )

            # Convert EVCC rates to hourly format
            # EVCC provides rates with start, end, and value (EUR/kWh)
            prices_eur_wh = []
            for rate in rates:
                if not isinstance(rate, dict) or "value" not in rate:
                    logger.warning(f"[FEEDIN-IF] Skipping invalid EVCC rate entry: {rate}")
                    continue

                try:
                    price_eur_kwh = float(rate["value"])
                    # Convert EUR/kWh to EUR/Wh (divide by 1000)
                    price_eur_wh = price_eur_kwh / 1000.0
                    # EVCC feed-in prices are used as-is (no adder/multiplier)
                    prices_eur_wh.append(price_eur_wh)

                except (ValueError, KeyError, TypeError) as e:
                    logger.warning(f"[FEEDIN-IF] Error parsing EVCC rate entry: {e}")
                    continue

            if not prices_eur_wh:
                logger.error("[FEEDIN-IF] No valid rates converted from EVCC response")
                return []

            logger.debug(
                "[FEEDIN-IF] Fetched %d EVCC feed-in prices",
                len(prices_eur_wh),
            )

            # Extend to 48 or 192 hours if needed
            prices_eur_wh = self._extend_prices_to_duration(prices_eur_wh, tgt_duration)

            return prices_eur_wh

        except requests.RequestException as e:
            logger.error("[FEEDIN-IF] EVCC feed-in API request failed: %s", e)
            return []
        except (KeyError, ValueError) as e:
            logger.error("[FEEDIN-IF] EVCC feed-in API response parsing failed: %s", e)
            return []

    def _fetch_fixed_price(self, tgt_duration, start_time):
        """
        Use fixed feed-in price for all time slots.

        Applies static_adder_ct_kwh and multiplier for consistency with dynamic sources.
        If negative_price_switch enabled, applies clamping based on market prices from
        Akkudoktor.

        Args:
            tgt_duration (int): Target duration
            start_time (datetime): Start time (not used for fixed prices)

        Returns:
            list: Fixed prices in EUR/Wh
        """
        # Apply static adder and multiplier (consistent with dynamic sources)
        price_ct_kwh = (self.fixed_price_ct_kwh + self.static_adder_ct_kwh) * self.multiplier
        # ct/kWh → EUR/Wh (1 ct/kWh = 0.00001 EUR/Wh)
        price_eur_wh = round(price_ct_kwh / 100000, 9)
        prices = [price_eur_wh] * tgt_duration

        logger.debug(
            "[FEEDIN-IF] Using fixed feed-in price: %.2f ct/kWh "
            "(base=%.2f, adder=%.2f, mult=%.2f) = %.9f EUR/Wh",
            price_ct_kwh,
            self.fixed_price_ct_kwh,
            self.static_adder_ct_kwh,
            self.multiplier,
            price_eur_wh,
        )

        # If negative_price_switch enabled, clamp to 0 where market prices are negative
        if self.negative_price_switch:
            market_prices = self._fetch_market_prices_for_reference(tgt_duration, start_time)
            if market_prices:
                # Clamp feed-in to 0 where market price < 0
                prices = [
                    0.0 if market_prices[i] < 0 else prices[i]
                    for i in range(min(len(prices), len(market_prices)))
                ]
                logger.debug(
                    "[FEEDIN-IF] Applied negative_price_switch to fixed prices "
                    "(clamped %d slots to 0 based on market prices)",
                    sum(1 for p in market_prices if p < 0),
                )
            else:
                logger.warning(
                    "[FEEDIN-IF] negative_price_switch enabled but failed to fetch "
                    "market prices"
                )

        return prices

    def _fetch_market_prices_for_reference(self, tgt_duration, start_time):
        """
        Fetch Akkudoktor market prices as reference for negative price detection
        (fixed source only).

        Used to determine when to clamp fixed feed-in prices to 0 when market prices
        are negative.

        Args:
            tgt_duration (int): Target duration
            start_time (datetime): Start time

        Returns:
            list: Prices in EUR/Wh or empty list on error
        """
        try:
            start_date = start_time.strftime("%Y-%m-%d")
            end_date = (start_time + timedelta(days=1)).strftime("%Y-%m-%d")
            url = f"{AKKUDOKTOR_API_PRICES}?start={start_date}&end={end_date}"

            logger.debug("[FEEDIN-IF] Fetching Akkudoktor market prices for reference: %s", url)
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()
            prices_list = data.get("values", [])

            if not prices_list:
                logger.warning(
                    "[FEEDIN-IF] Akkudoktor returned empty market price list for "
                    "reference"
                )
                return []

            prices_eur_wh = []
            for price_entry in prices_list:
                # API returns prices in ct/kWh (without tax)
                price_ct_kwh = price_entry.get("marketpriceEurocentPerKWh", 0.0)
                # Convert ct/kWh → EUR/Wh (1 ct/kWh = 0.00001 EUR/Wh)
                price_eur_wh = round(price_ct_kwh / 100000, 9)
                prices_eur_wh.append(price_eur_wh)

            logger.debug(
                "[FEEDIN-IF] Fetched %d Akkudoktor market prices for reference",
                len(prices_eur_wh),
            )

            # Extend to target duration if needed
            prices_eur_wh = self._extend_prices_to_duration(prices_eur_wh, tgt_duration)
            return prices_eur_wh

        except requests.RequestException as e:
            logger.error("[FEEDIN-IF] Failed to fetch Akkudoktor market prices: %s", e)
            return []
        except (KeyError, ValueError) as e:
            logger.error("[FEEDIN-IF] Failed to parse Akkudoktor response: %s", e)
            return []

    def _extend_prices_to_duration(self, prices, tgt_duration):
        """
        Extend price list to target duration by cycling.

        If prices is 24h and target is 48h, duplicate them.
        If time_frame_base is 900 (15-min), expand each hour to 4 slots.

        Args:
            prices (list): Input prices
            tgt_duration (int): Target duration in slots

        Returns:
            list: Extended price list
        """
        if not prices:
            return []

        # For 15-min resolution: expand hourly prices to 4 slots each
        if self.time_frame_base == 900:
            prices = [p for p in prices for _ in range(4)]
            tgt_duration = tgt_duration * 4 if tgt_duration < 100 else tgt_duration

        # If still short, cycle through available prices
        if len(prices) < tgt_duration:
            remaining_slots = tgt_duration - len(prices)
            prices.extend(prices[:remaining_slots])

        return prices
