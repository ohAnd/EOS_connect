"""
pv_interface.py

This module provides the PvInterface class, which serves as an interface for
fetching and summarizing photovoltaic (PV) power and temperature forecasts.
It handles configuration validation, periodic background updates, and provides
default fallback values in case of API errors. The module is designed to
interact with the EOS API to retrieve forecast data for one or more PV systems,
aggregate the results, and make them available for further processing or
monitoring.

Classes:
    PvInterface: Manages PV and temperature forecast retrieval, configuration
        validation, periodic updates, and provides summarized forecast data.

Constants:
    EOS_API_GET_PV_FORECAST: The endpoint URL for fetching PV forecast data
        from the EOS API.

Logging:
    Uses the standard Python logging module to log information, debug messages,
    and errors related to configuration, API requests, and background updates.
"""

from datetime import datetime, timedelta, timezone
import re
import threading
import logging
import time
import asyncio
import math
from collections import defaultdict
import aiohttp
import pytz
import requests
import pandas as pd
import numpy as np
from open_meteo_solar_forecast import OpenMeteoSolarForecast

# PV sources that derive the forecast from a physical installation's coordinates, and
# therefore need at least one entry in ``pv_forecast``.  Every other source carries its
# own configuration (a resource id, a URL, an EVCC instance) and works with an empty
# list.  Canonical copy lives in ``config_web/schema.py``; ``interfaces`` does not import
# ``config_web`` (they are sibling top-level packages at runtime), so the two are pinned
# equal by ``tests/interfaces/test_pv_interface_location_sources.py`` instead.
LOCATION_BASED_PV_SOURCES = ("akkudoktor", "openmeteo", "openmeteo_local", "forecast_solar")

# Nominal array size assumed wherever the code has to invent a power out of nothing:
# the configuration-free "default" source, which by design has no installation to read
# one from, and the fallbacks that stand in for a provider that failed.  A typical
# domestic roof, so the demo curve looks like a home rather than a garden shed.
DEFAULT_PV_NOMINAL_POWER_W = 4000

# The outside-temperature curve is hourly at the source and barely moves between two
# 15-minute PV cycles, so refetching it on every one of them was four requests an hour
# for one new data point.  A successful forecast is reused until it is this old.
TEMP_REFRESH_INTERVAL_S = 3600

# Retry policy for the temperature request.  Deliberately shorter than the PV one: the
# cache is the real recovery strategy here (a held forecast is a good answer, a held PV
# array is not), and every retry blocks the shared update loop.
TEMP_MAX_RETRIES = 2
TEMP_RETRY_DELAY_S = 2

# Forecast.Solar meters requests per "zone" - the caller's IP address, or the API key
# once one is configured - and the public tier allows 12 per hour.  Their own guidance
# is blunt about the failure mode: "If you ignore the 'retry at' timestamp and call over
# and over again, you get an infinite 429 loop."  These bound the pause we take after a
# 429 when the response does not tell us how long to wait, or tells us something absurd.
FORECAST_SOLAR_DEFAULT_HOLD_S = 3600  # the public tier's own period
FORECAST_SOLAR_MIN_HOLD_S = 60
FORECAST_SOLAR_MAX_HOLD_S = 2 * 3600


class _ForecastSolarRateLimit(Exception):
    """
    A 429 from Forecast.Solar, raised so it escapes ``_retry_request`` untouched.

    Deliberately not a ``RequestException``: that is one of the families
    ``_retry_request`` catches and retries three times, which is precisely how a single
    rate-limited cycle used to turn into three more requests against a quota that was
    already exhausted.  A plain ``Exception`` propagates on the first attempt.
    """

    def __init__(self, hold_seconds):
        super().__init__(f"rate limited, holding off for {hold_seconds}s")
        self.hold_seconds = hold_seconds


# api.akkudoktor.net proxies an upstream weather service and does not pass its status
# through: every upstream fault arrives as a 500 whose *body* names the real code, e.g.
# "Request failed with status code 429".  ``raise_for_status()`` reports only "500 Server
# Error" plus the URL, so the one fact that separates a transient quota exhaustion from a
# request the upstream rejects outright never reached the log.  Without it a rate-limited
# provider is indistinguishable from a misconfiguration, and the reports that came in
# described weeks of intermittent 500s against a request that was correct throughout.
AKKUDOKTOR_UPSTREAM_STATUS_RE = re.compile(r"status code (\d{3})")
AKKUDOKTOR_BODY_EXCERPT_CHARS = 200

# An upstream 429 is a quota window lasting minutes to hours, so the normal retry policy
# (for temperature: two attempts two seconds apart) cannot outlast it - it only spends the
# retries.  Hold off instead, as the Forecast.Solar path does.  Akkudoktor sends no
# Retry-After and documents no quota, so the wait is a fixed, deliberately modest guess:
# too short only wastes one request, while too long strands a recovered API.
AKKUDOKTOR_RATE_LIMIT_HOLD_S = 900


class _AkkudoktorRateLimit(Exception):
    """
    An upstream 429 relayed by akkudoktor, raised so it escapes ``_retry_request``.

    Not a ``RequestException`` for the same reason as ``_ForecastSolarRateLimit``: that
    family is caught and retried, which is exactly how one rate-limited cycle turns into
    further requests against a quota that is already exhausted.
    """

    def __init__(self, hold_seconds):
        super().__init__(f"upstream rate limit, holding off for {hold_seconds}s")
        self.hold_seconds = hold_seconds


def _akkudoktor_body_excerpt(response):
    """
    The error body of an akkudoktor response, whitespace-collapsed and trimmed.

    Returns "" when there is no readable body, so callers can treat a missing
    explanation the same as an empty one.
    """
    if response is None:
        return ""
    try:
        body = (response.text or "").strip()
    except (ValueError, UnicodeDecodeError):
        return ""
    if not body:
        return ""
    body = " ".join(body.split())
    if len(body) > AKKUDOKTOR_BODY_EXCERPT_CHARS:
        body = body[:AKKUDOKTOR_BODY_EXCERPT_CHARS] + "..."
    return body


def _akkudoktor_upstream_status(body):
    """
    The upstream status code named in an akkudoktor error body, or None.
    """
    match = AKKUDOKTOR_UPSTREAM_STATUS_RE.search(body)
    return int(match.group(1)) if match else None


def _describe_akkudoktor_error(tgt_value, exception):
    """
    Build the log message for a failed akkudoktor request, upstream cause included.

    The proxy's own "500 Server Error" is identical for every fault it relays, so the
    body is the whole diagnosis - and naming who is at fault keeps the next bug report
    from starting at zero.
    """
    message = f"Akkudoktor API error for {tgt_value}: {exception}"
    body = _akkudoktor_body_excerpt(getattr(exception, "response", None))
    if not body:
        return message

    upstream = _akkudoktor_upstream_status(body)
    if upstream == 429:
        hint = (
            " - the weather provider is rate limiting akkudoktor.net;"
            " the request itself is fine and this clears on its own"
        )
    elif upstream is not None:
        hint = f" - the weather provider rejected the request with {upstream}"
    else:
        hint = ""
    return f"{message} | upstream: {body}{hint}"


from .timeseries_normalizer import (
    TEMPLATE_DOCS_ANCHOR,
    TimeseriesFormatError,
    convert_pv_values,
    extract_json_path,
    normalize_entries,
    pv_plausibility_message,
)

logger = logging.getLogger("__main__")
logger.info("[PV-IF] loading module ")

EOS_API_GET_PV_FORECAST = "https://api.akkudoktor.net/forecast"


def wants_temperature_forecast(eos_config):
    """
    True when an outside-temperature curve should be fetched for the optimizer.

    EOS asks for one and models the house more precisely with it, so it is on by default
    there.  EVopt - local or external - does not use temperature at all, so nothing is
    fetched for it.  ``eos.temperature_forecast_enabled`` lets an EOS user opt out
    anyway, which is the only way to stop EOS Connect talking to the forecast provider;
    the static 15 degree default is sent instead.

    ``config_web.hot_reload`` holds an inline copy of this rule (it imports nothing from
    ``interfaces`` by design).  The two are pinned equal by
    ``tests/interfaces/test_pv_interface_temperature_gating.py``.
    """
    if not isinstance(eos_config, dict):
        return False
    if eos_config.get("source", "eos_server") != "eos_server":
        return False
    value = eos_config.get("temperature_forecast_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class PvInterface:
    """
    Interface for fetching and summarizing PV (photovoltaic) and temperature forecasts.
    Handles configuration validation, periodic updates, and default fallbacks.
    """

    def __init__(
        self,
        config_source,
        config,
        time_frame_base,
        config_special,
        temperature_forecast_enabled=False,
        timezone="UTC",
    ):
        self.config = config
        self.time_zone = timezone
        self.config_source = config_source
        # Set time_frame_base, defaulting to 3600 if None or not provided
        self.time_frame_base = time_frame_base if time_frame_base is not None else 3600
        self.config_special = config_special
        self.temperature_forecast_enabled = temperature_forecast_enabled
        # Extract source type value first (breaks taint chain from config dict)
        source_type = (
            self.config_source.get("source", "akkudoktor")
            if isinstance(self.config_source, dict)
            else "akkudoktor"
        )
        logger.debug("[PV-IF] Initializing with 1st source: %s", source_type)

        self.pv_forcast_array = []
        self.pv_forcast_array_raw = []
        self.pv_forcast_request_error = {
            "error": None,
            "timestamp": None,
            "message": None,
            "config_entry": None,
            "source": None,
        }
        # The temperature forecast keeps its own error slot. It is a separate provider
        # call with its own cache, and the update loop reads pv_forcast_request_error to
        # decide whether the *PV* fetch failed - a temperature error landing there made
        # a healthy PV cycle report itself as degraded.
        self.temp_forecast_request_error = {
            "error": None,
            "timestamp": None,
            "message": None,
            "config_entry": None,
            "source": None,
        }
        self.temp_forecast_array = self.__get_default_temperature_forecast()

        # Cache mechanism for fallback on API failures (similar to PriceInterface)
        # When Akkudoktor is unavailable, reuse last successful forecast
        self.last_successful_pv_forecast = []
        self.last_successful_temp_forecast = []
        self.consecutive_failures = 0
        self.consecutive_temp_failures = 0
        self.max_failures = 24  # Max consecutive failures before using defaults
        # Monotonic timestamp of the last successful temperature fetch (None = never).
        self._last_temp_fetch = None
        # When Forecast.Solar last said "429, come back later" - no request is made
        # before it passes.  See ``_ForecastSolarRateLimit``.
        self._forecast_solar_hold_until = None
        # The same, for an upstream 429 relayed by akkudoktor as a 500.  Shared by the
        # PV and temperature requests because the quota is the provider's, not ours:
        # holding one back while the other keeps hammering would not honour it.
        self._akkudoktor_hold_until = None

        self._update_thread = None
        self._stop_event = threading.Event()
        self._reload_lock = threading.Lock()
        self.update_interval = 15 * 60
        self.configuration_state = "unknown"  # 'valid', 'incomplete', or 'invalid'
        self.configuration_valid = False  # Will be set to True only if config is fully valid
        self.__configure_update_interval()

        # Startup validation: Use lenient mode to allow graceful degradation
        # Users can fix incomplete config via web UI without addon crash
        try:
            self.__check_config(strict=False)  # Lenient startup validation
            self.configuration_state = "valid"
            self.configuration_valid = True
            logger.info("[PV-IF] Configuration validation successful at startup")
        except ValueError as e:
            logger.warning("[PV-IF] PV Interface configuration incomplete: %s", str(e))
            logger.warning(
                "[PV-IF] Starting in DEGRADED mode - PV data unavailable until config is fixed"
            )
            logger.warning(
                "[PV-IF] Use Settings > PV Source to complete the configuration"
            )
            self.configuration_state = "incomplete"
            self.configuration_valid = False

        logger.info("[PV-IF] Initialized (config_state=%s)", self.configuration_state)
        # Autoscaler hook (injected later via eos_connect wiring)
        self._autoscaler = None

        self.__start_update_service()  # Start the background thread for periodic updates

    def set_autoscaler(self, autoscaler):
        """Attach a `PvAutoscaler` instance for runtime scaling of forecasts."""
        self._autoscaler = autoscaler

    def get_autoscaler(self):
        """Return the attached `PvAutoscaler`, or None when autoscaling is not wired up."""
        return self._autoscaler

    def refresh_scaled_forecast(self):
        """
        Re-derive the scaled forecast from the raw array already held in memory.

        The update thread starts in __init__, so its first fetch finishes before the
        autoscaler is attached. Without this the forecast handed to EOS stays unscaled
        until the next update cycle - up to 15 minutes, or hours on a slow provider.
        """
        if self.pv_forcast_array_raw:
            self.pv_forcast_array = self.apply_autoscaling(self.pv_forcast_array_raw)

    def __configure_update_interval(self):
        """Set update interval based on active PV provider and installation count."""
        source = self.config_source.get("source")
        if source == "solcast":
            if len(self.config) >= 2:
                # For each update 2 calls may be needed.
                self.update_interval = 6 * 60 * 60
            else:
                self.update_interval = 2.5 * 60 * 60
            logger.info("[PV-IF] Using extended update interval for Solcast: 2.5 hours")
        elif source == "victron":
            self.update_interval = 15 * 60
            logger.info("[PV-IF] Using standard update interval for Victron: 15 minutes")
        elif source == "forecast_solar":
            # One request per plane per cycle, against a public tier that allows 12 per
            # hour for the whole zone.  A flat 15 minutes meant three planes sat exactly
            # on the ceiling before a single retry, so scale with the plane count and
            # total traffic stays at four requests an hour whatever the array looks like.
            installations = max(1, len(self.config) if self.config else 1)
            self.update_interval = 15 * 60 * installations
            logger.info(
                "[PV-IF] Using update interval for Forecast.Solar: %d minutes"
                " (%d installation(s), 4 requests/hour total)",
                self.update_interval // 60,
                installations,
            )
        else:
            self.update_interval = 15 * 60

    def reload_config(
        self,
        config_source,
        config,
        config_special,
        temperature_forecast_enabled,
        timezone,
    ):
        """
        Reload PV configuration at runtime without restarting the full application.

        Validates new settings before applying. On validation failure, the previous
        configuration is restored and update service continues running.
        """
        with self._reload_lock:
            old_state = {
                "config": self.config,
                "config_source": self.config_source,
                "config_special": self.config_special,
                "temperature_forecast_enabled": self.temperature_forecast_enabled,
                "time_zone": self.time_zone,
                "update_interval": self.update_interval,
                "configuration_valid": self.configuration_valid,
                "configuration_state": self.configuration_state,
            }

            # Pause update loop before replacing runtime config.
            self.shutdown()

            self.config = config
            self.config_source = config_source
            self.config_special = config_special
            self.temperature_forecast_enabled = temperature_forecast_enabled
            self.time_zone = timezone
            self.pv_forcast_request_error = {
                "error": None,
                "timestamp": None,
                "message": None,
                "config_entry": None,
                "source": None,
            }
            self.temp_forecast_request_error = {
                "error": None,
                "timestamp": None,
                "message": None,
                "config_entry": None,
                "source": None,
            }
            # Reset cache when configuration changes (source switch, etc.)
            self.last_successful_pv_forecast = []
            self.last_successful_temp_forecast = []
            self.consecutive_failures = 0
            self.consecutive_temp_failures = 0
            self._last_temp_fetch = None
            # A reload is a deliberate user action, and adding or removing the API key
            # moves us to a different quota zone, so an old hold no longer describes
            # anything real.  It re-arms on the next 429 if we are still blocked.
            self._forecast_solar_hold_until = None
            self._akkudoktor_hold_until = None

            try:
                self.__configure_update_interval()
                self.__check_config()  # Uses strict=True by default for hot-reload
                self.configuration_valid = True
                self.configuration_state = "valid"
                logger.info(
                    "[PV-IF] Live config reload applied (source=%s, entries=%d)",
                    self.config_source.get("source", "akkudoktor"),
                    len(self.config),
                )
            except ValueError as exc:
                logger.warning("[PV-IF] Live config reload rejected: %s", exc)
                self.config = old_state["config"]
                self.config_source = old_state["config_source"]
                self.config_special = old_state["config_special"]
                self.temperature_forecast_enabled = old_state[
                    "temperature_forecast_enabled"
                ]
                self.time_zone = old_state["time_zone"]
                self.update_interval = old_state["update_interval"]
                self.configuration_valid = old_state["configuration_valid"]
                self.configuration_state = old_state["configuration_state"]
                # Revalidate old config defensively (should always pass).
                self.__check_config()
                raise
            finally:
                self.__start_update_service()

    def __check_config(self, strict=True):
        """
        Checks the configuration for required parameters.
        Separates validation into two paths:
        1. PV forecast parameters (source-specific)
        2. Temperature forecast parameters (minimal: lat/lon only)

        Args:
            strict: If True (default), enforce strict validation for hot-reload.
                   If False, allow graceful degradation at startup.

        Raises:
            ValueError: If any required parameter is missing from the configuration.
        """
        # First check: config must be a list
        if isinstance(self.config, dict):
            logger.error(
                "[PV-IF] PV forecast configuration error: pv_forecast must be a LIST"
            )
            logger.error("[PV-IF] Current format: pv_forecast: {name: ..., lat: ...}")
            logger.error("[PV-IF] Expected format: pv_forecast:")
            logger.error("[PV-IF]   - name: ...")
            logger.error("[PV-IF]     lat: ...")
            raise ValueError(
                "[PV-IF] pv_forecast must be a list (with '-' in YAML), not a single object"
            )

        # An empty list is only a problem for location-based sources, and
        # __validate_pv_source_requirements below already says so with the right
        # wording.  Raising here instead would degrade evcc, solcast, victron,
        # timeseries and default installs that never need an entry — and because
        # reload_config() runs this with strict=True and rolls back, it would also
        # refuse a switch to those sources from the web UI, leaving no way to fix it.
        logger.debug("[PV-IF] Initialize - pv entries found: %s", len(self.config))

        # VALIDATION PATH 1: Source-specific PV requirements
        self.__validate_pv_source_requirements(strict=strict)

        # VALIDATION PATH 2: Common PV parameters based on source
        self.__validate_pv_common_parameters(strict=strict)

        # VALIDATION PATH 3: Temperature-specific requirements (minimal)
        self.__validate_temperature_requirements()

    def __validate_pv_source_requirements(self, strict=True):
        """
        Validates source-specific PV forecast requirements.
        Each source (Victron, Solcast, etc.) has different needs.
        Resource IDs now read from pv_forecast_source.resource_id instead of array entries.

        Args:
            strict: If True, log errors; if False, log warnings (for startup degradation).
        """
        source = self.config_source.get("source", "akkudoktor")

        # Victron-specific validation
        if source == "victron":
            resource_id = str(self.config_source.get("resource_id", "")).strip()
            if not resource_id:
                log_func = logger.error if strict else logger.warning
                log_func(
                    "[PV-IF] Victron VRM ID missing in pv_forecast_source.resource_id"
                )
                log_func(
                    '[PV-IF] Please add resource_id to pv_forecast_source section '
                    '(e.g., resource_id: "your_victron_vrm_id")'
                )
                log_func("[PV-IF] Use Settings → PV Source to fix this")
                raise ValueError(
                    "[PV-IF] Victron VRM ID (resource_id in pv_forecast_source) "
                    "required - Use Settings → PV Source to fix"
                )

            if not self.config_source.get("api_key", "").strip():
                log_func = logger.error if strict else logger.warning
                log_func("[PV-IF] Victron API key missing in pv_forecast_source section")
                log_func("[PV-IF] Please set api_key in Settings → PV Source")
                raise ValueError(
                    "[PV-IF] Victron API key (api_key) required - Use Settings → PV Source to fix"
                )

            logger.debug("[PV-IF] Victron source-specific requirements validated")

        # Solcast-specific validation
        elif source == "solcast":
            if not self.config_source.get("api_key", "").strip():
                log_func = logger.error if strict else logger.warning
                log_func("[PV-IF] Solcast API key missing in pv_forecast_source section")
                log_func("[PV-IF] Please set api_key in Settings → PV Source")
                raise ValueError(
                    "[PV-IF] Solcast API key required - Use Settings → PV Source to fix"
                )

            resource_ids = str(self.config_source.get("resource_id", "")).strip()
            if not resource_ids:
                log_func = logger.error if strict else logger.warning
                log_func(
                    "[PV-IF] Resource IDs missing for Solcast - " +
                    "required in pv_forecast_source.resource_id"
                )
                log_func(
                    "[PV-IF] Please set resource_id in Settings → PV Source" +
                    " (comma-separated for multiple)"
                )
                raise ValueError(
                    "[PV-IF] Solcast resource_id required - Use Settings → PV Source to fix"
                )

            logger.debug("[PV-IF] Solcast source-specific requirements validated")

        elif source == "timeseries":
            # Timeseries source requires either data_url (for HTTP) or HA sensor integration
            data_url = self.config_source.get("data_url", "").strip()
            use_ha_central = self.config_source.get("use_ha_central_data_source", False)

            if not data_url and not use_ha_central:
                log_func = logger.error if strict else logger.warning
                log_func("[PV-IF] Timeseries data_url missing in pv_forecast_source section")
                log_func(
                    "[PV-IF] Please provide either:"
                    " (1) data_url - HTTP endpoint returning timeseries data, OR"
                    " (2) use_ha_central_data_source: true for HA sensor integration"
                )
                log_func("[PV-IF] Use Settings → PV Source to fix this")
                raise ValueError(
                    "[PV-IF] Timeseries requires data_url or use_ha_central_data_source"
                    " - Use Settings → PV Source to fix"
                )

            # If using HTTP URL, validate it's a valid URL format
            if data_url and not (
                data_url.startswith("http://") or data_url.startswith("https://")
            ):
                log_func = logger.error if strict else logger.warning
                log_func(
                    "[PV-IF] Timeseries data_url must be a valid HTTP/HTTPS URL,"
                    f" got: {data_url}"
                )
                log_func("[PV-IF] Use Settings → PV Source to fix this")
                raise ValueError(
                    "[PV-IF] Timeseries data_url must start with http:// or https://"
                )

            logger.debug(
                "[PV-IF] Timeseries source-specific requirements validated"
                " (data_url=%s, ha_central=%s)",
                "***" if data_url else "none",
                use_ha_central,
            )

        elif source == "evcc":
            # EVCC-specific validation handled separately
            logger.debug("[PV-IF] EVCC source-specific requirements validated")

        elif source == "default":
            # Default source uses fixed default values - no external configuration needed
            logger.debug("[PV-IF] Default source-specific requirements validated")

        elif source in LOCATION_BASED_PV_SOURCES:
            # Location-based sources - require at least one pv_forecast entry
            if not self.config or len(self.config) == 0:
                log_func = logger.error if strict else logger.warning
                log_func("[PV-IF] No PV forecast entries found for location-based source")
                log_func(
                    "[PV-IF] Please add at least one entry to PV "+
                    "Installations in Settings → PV Source"
                )
                raise ValueError(
                    f"[PV-IF] At least one PV forecast entry required for {source} source"
                )

            logger.debug("[PV-IF] Location-based source-specific requirements validated")

    def __validate_pv_common_parameters(self, strict=True):
        """
        Validates common PV parameters required based on source.
        Skips parameters not needed by the specific source.
        Sets sensible defaults where applicable.

        Args:
            strict: If True, enforce strict validation; if False, use graceful defaults.
        """
        source = self.config_source.get("source", "akkudoktor")

        for config_entry in self.config:
            entry_name = config_entry.get("name", "unnamed")

            # lat/lon - Required parameters depend on source and use case
            # - Victron / Timeseries / EVCC / default: never required.  These take the PV
            #   data from elsewhere, so coordinates are only ever used to ask for the
            #   outside temperature - and a missing temperature is not a broken PV
            #   configuration.  Without them the static default curve is served instead;
            #   failing validation here took the whole interface into DEGRADED mode and
            #   made hot-reload refuse the change, over an optional extra.
            # - Solcast: only needed if NO resource_id provided (rare case)
            # - Other sources: needed for location-based forecasting
            if source in ("victron", "timeseries", "evcc", "default"):
                needs_lat_lon = False
            elif source == "solcast":
                # Solcast: if resource_id provided, lat/lon not needed
                # If NO resource_id, they would be needed (but Solcast requires resource_id)
                has_resource_id = config_entry.get("resource_id", "").strip()
                needs_lat_lon = not has_resource_id
            else:
                # All other sources need lat/lon for their location-based API calls
                needs_lat_lon = True

            if needs_lat_lon:
                missing = []
                if config_entry.get("lat") is None:
                    missing.append("lat")
                if config_entry.get("lon") is None:
                    missing.append("lon")
                if missing:
                    raise ValueError(
                        "[PV-IF] Missing required parameters "
                        + f"for '{entry_name}': {', '.join(missing)}"
                    )

            # OPTIMIZATION: For sources that DON'T require full PV config
            # (Victron, Solcast, Timeseries, etc.), set sensible defaults
            if source in ("victron", "solcast", "timeseries", "evcc", "default"):
                # These sources don't need detailed panel orientation for PV forecasting.
                # The defaults still matter for the fallback curve that stands in when the
                # provider is unreachable, which is sized from them. The temperature
                # request no longer reads any of this - it sends its own fixed array.
                defaults_set = []

                if config_entry.get("azimuth") is None:
                    config_entry["azimuth"] = (
                        0.1  # South-facing (0.0 rejected by Akkudoktor API, use 0.1 instead)
                    )
                    defaults_set.append("azimuth")

                if config_entry.get("tilt") is None:
                    config_entry["tilt"] = 30.0  # Standard tilt
                    defaults_set.append("tilt")

                if config_entry.get("power") is None:
                    config_entry["power"] = float(DEFAULT_PV_NOMINAL_POWER_W)
                    defaults_set.append("power")

                if config_entry.get("powerInverter") is None:
                    config_entry["powerInverter"] = float(DEFAULT_PV_NOMINAL_POWER_W)
                    defaults_set.append("powerInverter")

                if config_entry.get("inverterEfficiency") is None:
                    config_entry["inverterEfficiency"] = (
                        0.95  # Modern inverter efficiency
                    )
                    defaults_set.append("inverterEfficiency")

                if defaults_set:
                    # Extract variables first to break taint chain
                    defaults_str = ", ".join(defaults_set)
                    source_str = str(source) if source else "unknown"
                    logger.debug(
                        "[PV-IF] Set %s defaults for '%s' (%s)",
                        defaults_str,
                        entry_name,
                        source_str,
                    )

            else:
                # OTHER SOURCES require full PV configuration
                # Check azimuth and tilt
                missing = []
                if config_entry.get("azimuth") is None:
                    missing.append("azimuth")
                if config_entry.get("tilt") is None:
                    missing.append("tilt")
                if missing:
                    raise ValueError(
                        "[PV-IF] Missing required parameters "
                        + f"for '{entry_name}': {', '.join(missing)}"
                    )

                # Check power
                if config_entry.get("power") is None:
                    raise ValueError(
                        "[PV-IF] Missing required parameter 'power' for '"
                        + entry_name
                        + "'"
                    )

                # Check powerInverter (not needed for forecast_solar)
                if source != "forecast_solar":
                    if config_entry.get("powerInverter") is None:
                        raise ValueError(
                            "[PV-IF] Missing required parameter 'powerInverter' for '"
                            + entry_name
                            + "'"
                        )

                # Check inverterEfficiency (not needed for forecast_solar)
                if source != "forecast_solar":
                    if config_entry.get("inverterEfficiency") is None:
                        raise ValueError(
                            "[PV-IF] Missing required parameter 'inverterEfficiency' for '"
                            + entry_name
                            + "'"
                        )

                logger.debug(
                    "[PV-IF] '%s' validated - all PV parameters present", entry_name
                )

            # horizon parameter for specific sources
            if source in ("openmeteo_local", "forecast_solar"):
                if "horizon" not in config_entry or not config_entry["horizon"]:
                    # Extract entry_name first to break taint chain
                    entry_name_str = str(entry_name) if entry_name else "unnamed"
                    logger.warning(
                        "[PV-IF] 'horizon' parameter missing for '%s' "
                        + "- using default (no shading)",
                        entry_name_str,
                    )
                    config_entry["horizon"] = [0] * (
                        24 if source == "forecast_solar" else 36
                    )

    def __validate_temperature_requirements(self):
        """
        Validates temperature forecast requirements (minimal).
        Temperature only needs lat/lon from at least one PV entry.
        This is optional and independent of PV source.
        All sources can support temperature via Akkudoktor API.
        """
        if not self.temperature_forecast_enabled:
            logger.debug(
                "[PV-IF] Temperature forecast disabled - skipping temperature validation"
            )
            return

        # Check if we have at least one config entry with lat/lon
        if not self.config or len(self.config) == 0:
            # Informational for every source: sources that need an installation for their
            # own forecast are already reported by __validate_pv_source_requirements, and
            # for the rest running without one is the expected state.
            logger.info(
                "[PV-IF] No PV forecast entries found - temperature forecast will use defaults"
            )
            return

        first_entry = self.config[0]
        entry_name = first_entry.get("name", "unnamed")
        # Extract to clean variable first to break taint chain
        entry_name_str = str(entry_name) if entry_name else "unnamed"

        if first_entry.get("lat") is None or first_entry.get("lon") is None:
            # Informational, not a warning: the temperature forecast is an optional input
            # to EOS and the static default is a valid answer.  Sources that do need
            # coordinates for their own forecast still fail validation above.
            logger.info(
                "[PV-IF] No lat/lon in first PV entry '%s'"
                + " - using static temperature forecast defaults (15°C)",
                entry_name_str,
            )
            return

        logger.debug(
            "[PV-IF] Temperature forecast requirements met for '%s' (lat/lon available)",
            entry_name_str,
        )

    def __start_update_service(self):
        """
        Starts the background thread to periodically update the charging state.
        """
        if self._update_thread is None or not self._update_thread.is_alive():
            self._stop_event.clear()
            self._update_thread = threading.Thread(
                target=self.__update_pv_state_loop, daemon=True
            )
            self._update_thread.start()
            logger.info("[PV-IF] Update service started.")

    def shutdown(self):
        """
        Stops the background thread and shuts down the update service.
        """
        if self._update_thread and self._update_thread.is_alive():
            self._stop_event.set()
            self._update_thread.join()
            logger.info("[PV-IF] Update service stopped.")

    def __update_pv_state_loop(self):
        """
        The loop that runs in the background thread to update the pv state.
        """
        while not self._stop_event.is_set():
            # Fetch the PV forecast data once per cycle and derive the scaled array
            # locally. Calling get_summarized_pv_forecast() twice would double the
            # upstream API traffic for every provider - which breaks the request budget
            # Solcast and forecast.solar are rate-limited on - and lets a transient
            # failure on one of the two calls desynchronise the raw/scaled pair.
            pv_forcast_array_raw = self.get_summarized_pv_forecast(scale=False)
            pv_forcast_array = self.apply_autoscaling(pv_forcast_array_raw)
            if not self.pv_forcast_request_error["error"]:
                logger.debug("[PV-IF] PV forecast updated successfully")
                self.pv_forcast_array = pv_forcast_array
                self.pv_forcast_array_raw = pv_forcast_array_raw
            elif pv_forcast_array:  # Fallback forecast available from cache
                # If there was an error but cache provided a forecast, use it
                logger.warning(
                    "[PV-IF] Using cached PV forecast due to API error: %s",
                    self.pv_forcast_request_error["message"],
                )
                self.pv_forcast_array = pv_forcast_array
                self.pv_forcast_array_raw = pv_forcast_array_raw
            elif self.pv_forcast_array == []:
                # If there was an error and no forecast was cached, use default values
                logger.warning(
                    "[PV-IF] Using default PV forecast due to previous error: %s",
                    self.pv_forcast_request_error["message"],
                )
                if self.config and len(self.config) > 0:
                    self.pv_forcast_array = self.__get_default_pv_forcast(
                        self.config[0]["power"]
                    )
                    self.pv_forcast_array_raw = self.__get_default_pv_forcast(
                        self.config[0]["power"]
                    )
                else:
                    self.pv_forcast_array = self.__get_default_pv_forcast(
                        DEFAULT_PV_NOMINAL_POWER_W
                    )
                    self.pv_forcast_array_raw = self.__get_default_pv_forcast(
                        DEFAULT_PV_NOMINAL_POWER_W
                    )
            else:
                # If there was an error but we have a previous forecast, log it
                logger.warning(
                    "[PV-IF] Using previous PV forecast due to error: %s",
                    self.pv_forcast_request_error["message"],
                )
            # Temperature forecast with minimal configuration (only needs lat/lon)
            # Works for all PV sources: Victron, Solcast, Akkudoktor, etc.
            # Not gated on configuration_valid: the temperature forecast has its own,
            # sufficient precondition - coordinates in the first entry - and an otherwise
            # incomplete PV configuration is no reason to drop a working one.
            if self.temperature_forecast_enabled:
                temp_config = self.__get_temperature_config_entry()
                if temp_config and not self.__temperature_forecast_is_fresh():
                    temp_result = self.__get_pv_forecast_akkudoktor_api(
                        tgt_value="temperature", pv_config_entry=temp_config
                    )
                    # Reject empty/None results and physically implausible values
                    # (e.g. PV Watts leaking into the temperature array) as a
                    # fail-safe on top of the target-aware cache/counter below.
                    if not temp_result or any(v > 60 or v < -60 for v in temp_result):
                        logger.warning(
                            "[PV-IF] Temperature forecast API failed - using default"
                            + " temperature forecast (15°C)"
                        )
                        self.temp_forecast_array = (
                            self.__get_default_temperature_forecast()
                        )
                    else:
                        self.temp_forecast_array = temp_result
                elif temp_config:
                    logger.debug(
                        "[PV-IF] Temperature forecast still fresh - keeping the cached"
                        " curve (refresh every %d s)",
                        TEMP_REFRESH_INTERVAL_S,
                    )
                    self.temp_forecast_array = list(self.last_successful_temp_forecast)
                else:
                    # lat/lon missing - already reported during config validation
                    self.temp_forecast_array = self.__get_default_temperature_forecast()
            else:
                logger.debug(
                    "[PV-IF] Temperature forecast disabled - using default (15°C)"
                )
                self.temp_forecast_array = self.__get_default_temperature_forecast()
            logger.info("[PV-IF] PV and Temperature updated")
            # Break the sleep interval into smaller chunks to allow immediate shutdown
            sleep_interval = self.update_interval
            while sleep_interval > 0:
                if self._stop_event.is_set():
                    return  # Exit immediately if stop event is set
                time.sleep(min(1, sleep_interval))  # Sleep in 1-second chunks
                sleep_interval -= 1

        self.__start_update_service()

    def get_current_pv_forecast(self, scale=True):
        """
        Returns a copy of the current photovoltaic (PV) forecast array.

        The copy is deliberate: callers adjust the series they get back - the EOS request
        builder discounts the in-progress slot to the fraction of it that is still ahead -
        and handing out the cached list itself let that adjustment accumulate into the
        cache, shrinking the current slot again on every optimizer run until the next
        provider fetch replaced the array.

        Returns:
            list: The current PV forecast values, scaled by the autoscaler unless
            `scale` is False.
        """
        # logger.debug(
        #     "[PV-IF] Returning current PV forecast: %s", self.pv_forcast_array
        # )
        if scale:
            return list(self.pv_forcast_array)
        return list(self.pv_forcast_array_raw)

    def get_forecast_day_totals(self, scale=True):
        """
        Total forecast energy in Wh for today and tomorrow.

        The dashboard header used to sum the array stored in `optimize_request.json`
        instead. That file is only rewritten once per optimizer run, so an autoscaler
        factor recomputed since then left the header disagreeing with the PV
        auto-scaling overlay, which always reads live. That array also carries the evopt
        partial-slot discount, which belongs to the optimizer's input rather than to a
        day's forecast total.

        Slot 0 is local midnight today, the same alignment `apply_scaling` relies on.
        A day with no slots reports None rather than 0.0, so a short or missing forecast
        is not published as "no sun".

        Returns:
            dict: `{"today_wh": float|None, "tomorrow_wh": float|None}`.
        """
        forecast = self.get_current_pv_forecast(scale=scale)
        slots_per_day = 24 * max(1, 3600 // int(self.time_frame_base or 3600))
        totals = {}
        for key, day in (("today_wh", 0), ("tomorrow_wh", 1)):
            slots = forecast[day * slots_per_day : (day + 1) * slots_per_day]
            try:
                totals[key] = round(sum(float(v) for v in slots), 1) if slots else None
            except (TypeError, ValueError):
                logger.warning("[PV-IF] Unusable forecast slot while summing %s", key)
                totals[key] = None
        return totals

    def __temperature_forecast_is_fresh(self):
        """
        True while the cached temperature forecast is young enough to reuse.

        Only ever True with a non-empty cache, so a failing provider is still retried on
        every update cycle - the throttle saves requests when things work, it does not
        delay recovery when they do not.
        """
        if not self.last_successful_temp_forecast or self._last_temp_fetch is None:
            return False
        return (time.monotonic() - self._last_temp_fetch) < TEMP_REFRESH_INTERVAL_S

    def get_current_temp_forecast(self):
        """
        Returns the current temperature forecast array.
        """
        # logger.debug(
        #     "[PV-IF] Returning current temp forecast: %s", self.temp_forecast_array
        # )
        return self.temp_forecast_array

    def __create_forecast_request(self, pv_config_entry):
        """
        Creates a forecast request parameters dict for the EOS server API.
        Returns parameters that will be passed to requests.get(url, params=...).
        This ensures proper numeric type handling by the requests library.
        """
        # Akkudoktor API rejects azimuth=0.0 with HTTP 400 ("wrongParameters").
        # Use 0.1 as a safe substitute that represents South-facing panels.
        raw_azimuth = (
            float(pv_config_entry["azimuth"])
            if pv_config_entry.get("azimuth") is not None
            else 0.0
        )
        akkudoktor_azimuth = raw_azimuth if raw_azimuth != 0.0 else 0.1
        params = {
            "lat": pv_config_entry["lat"],
            "lon": pv_config_entry["lon"],
            "azimuth": akkudoktor_azimuth,
            "tilt": pv_config_entry["tilt"],
            "power": pv_config_entry["power"],
            "powerInverter": pv_config_entry["powerInverter"],
            "inverterEfficiency": pv_config_entry["inverterEfficiency"],
            "timezone": self.time_zone,
        }

        # horizon must be converted from list to comma-separated string
        if pv_config_entry.get("horizon"):
            horizon = pv_config_entry["horizon"]
            if isinstance(horizon, list):
                params["horizont"] = ",".join(str(h) for h in horizon)
            else:
                params["horizont"] = str(horizon)

        return params

    def __create_temperature_request(self, pv_config_entry):
        """
        Build the parameters for an outside-temperature request.

        The endpoint is the PV forecast one, so it insists on a plausible installation -
        but the temperature it returns depends on the location and nothing else. Sending
        the user's panel geometry and horizon along made the request fail for reasons that
        cannot change the answer: a horizon list the API dislikes, or an azimuth of 0.0 it
        rejects outright. A fixed canonical array keeps the query identical for everyone at
        a given location, which also means the provider can serve it from cache.
        """
        return {
            "lat": pv_config_entry["lat"],
            "lon": pv_config_entry["lon"],
            "azimuth": 0.1,  # South (0.0 is rejected as a wrong parameter)
            "tilt": 30.0,
            "power": float(DEFAULT_PV_NOMINAL_POWER_W),
            "powerInverter": float(DEFAULT_PV_NOMINAL_POWER_W),
            "inverterEfficiency": 0.95,
            "timezone": self.time_zone,
        }

    def __get_temperature_config_entry(self):
        """
        Extracts temperature configuration from PV entries.
        Returns the first config entry (which already has all defaults set by validation).
        Temperature uses this full config to match the standard PV request format.

        Returns:
            dict: Full configuration entry with all parameters, or None if no valid config found
        """
        if self.config and len(self.config) > 0:
            first_entry = self.config[0]
            lat = first_entry.get("lat")
            lon = first_entry.get("lon")
            if lat is not None and lon is not None:
                logger.debug(
                    "[PV-IF] Using temperature config from '%s': lat=%s, lon=%s",
                    first_entry.get("name", "unnamed"),
                    lat,
                    lon,
                )
                return first_entry

        return None

    def __get_default_pv_forcast(self, pv_power):
        """
        Build the built-in PV forecast: a fixed bell curve scaled to *pv_power*.

        This is what the "default" source serves, and what every other source falls
        back to when its provider is unreachable. It contacts nothing.

        The shape of the returned array is the contract the rest of the system relies
        on, and matches what the real providers deliver:

        - index 0 is 00:00 local time, not "now" - the EOS request builder slices from
          ``seconds_since_midnight``, so a now-anchored array would be read as the
          wrong time of day;
        - one value per ``time_frame_base`` slot: 24 at 3600 s, 96 at 900 s;
        - doubled to cover 48 h, since the optimizer looks a day ahead.

        Peak is 70 % of *pv_power* at midday, zero before 06:00 and after 19:00.
        """
        # Create a 24-hour default forecast
        # Create a default 24-hour PV forecast.
        # If time_frame_base is 3600 (hourly), use 24 values.
        # If time_frame_base is 900 (15-min), use 96 values (4 per hour).
        if self.time_frame_base == 3600:
            forecast_24h = [
                pv_power * 0.0,  # 0% at 00:00
                pv_power * 0.0,  # 0% at 01:00
                pv_power * 0.0,  # 0% at 02:00
                pv_power * 0.0,  # 0% at 03:00
                pv_power * 0.0,  # 0% at 04:00
                pv_power * 0.0,  # 0% at 05:00
                pv_power * 0.1,  # 10% at 06:00
                pv_power * 0.2,  # 20% at 07:00
                pv_power * 0.3,  # 30% at 08:00
                pv_power * 0.4,  # 40% at 09:00
                pv_power * 0.5,  # 50% at 10:00
                pv_power * 0.6,  # 60% at 11:00
                pv_power * 0.7,  # 70% at 12:00
                pv_power * 0.6,  # 60% at 13:00
                pv_power * 0.5,  # 50% at 14:00
                pv_power * 0.4,  # 40% at 15:00
                pv_power * 0.3,  # 30% at 16:00
                pv_power * 0.2,  # 20% at 17:00
                pv_power * 0.1,  # 10% at 18:00
                pv_power * 0.0,  # 0% at 19:00
                pv_power * 0.0,  # 0% at 20:00
                pv_power * 0.0,  # 0% at 21:00
                pv_power * 0.0,  # 0% at 22:00
                pv_power * 0.0,  # 0% at 23:00
            ]
        elif self.time_frame_base == 900:
            # For 15-min intervals, interpolate each hour value to 4 values
            hourly_values = [
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 00:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 01:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 02:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 03:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 04:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 05:00
                pv_power * 0.025,
                pv_power * 0.05,
                pv_power * 0.075,
                pv_power * 0.1,  # 06:00
                pv_power * 0.125,
                pv_power * 0.15,
                pv_power * 0.175,
                pv_power * 0.2,  # 07:00
                pv_power * 0.225,
                pv_power * 0.25,
                pv_power * 0.275,
                pv_power * 0.3,  # 08:00
                pv_power * 0.325,
                pv_power * 0.35,
                pv_power * 0.375,
                pv_power * 0.4,  # 09:00
                pv_power * 0.425,
                pv_power * 0.45,
                pv_power * 0.475,
                pv_power * 0.5,  # 10:00
                pv_power * 0.525,
                pv_power * 0.55,
                pv_power * 0.575,
                pv_power * 0.6,  # 11:00
                pv_power * 0.625,
                pv_power * 0.65,
                pv_power * 0.675,
                pv_power * 0.7,  # 12:00
                pv_power * 0.675,
                pv_power * 0.65,
                pv_power * 0.625,
                pv_power * 0.6,  # 13:00
                pv_power * 0.575,
                pv_power * 0.55,
                pv_power * 0.525,
                pv_power * 0.5,  # 14:00
                pv_power * 0.475,
                pv_power * 0.45,
                pv_power * 0.425,
                pv_power * 0.4,  # 15:00
                pv_power * 0.375,
                pv_power * 0.35,
                pv_power * 0.325,
                pv_power * 0.3,  # 16:00
                pv_power * 0.275,
                pv_power * 0.25,
                pv_power * 0.225,
                pv_power * 0.2,  # 17:00
                pv_power * 0.175,
                pv_power * 0.15,
                pv_power * 0.125,
                pv_power * 0.1,  # 18:00
                pv_power * 0.075,
                pv_power * 0.05,
                pv_power * 0.025,
                pv_power * 0.0,  # 19:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 20:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 21:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 22:00
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,  # 23:00
            ]
            forecast_24h = hourly_values
        else:
            # Fallback to hourly if unknown time_frame_base
            forecast_24h = [
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.1,
                pv_power * 0.2,
                pv_power * 0.3,
                pv_power * 0.4,
                pv_power * 0.5,
                pv_power * 0.6,
                pv_power * 0.7,
                pv_power * 0.6,
                pv_power * 0.5,
                pv_power * 0.4,
                pv_power * 0.3,
                pv_power * 0.2,
                pv_power * 0.1,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
                pv_power * 0.0,
            ]
        # Repeat for the next day (48 hours total)
        # logger.debug("[PV-IF] Using default PV forecast with %s W max power", pv_power)
        return forecast_24h * 2

    def __get_default_temperature_forecast(self):
        """
        Creates a default temperature forecast with fixed values.
        The values are set to 15 degrees Celsius for the entire day.
        """
        # Create a 24-hour default temperature forecast
        forecast_24h = [15.0] * 24  # 15 degrees Celsius for each hour
        if self.time_frame_base == 900:
            forecast_24h = [15.0] * 96  # 15 degrees Celsius for each 15-min interval
        return forecast_24h * 2  # Repeat for the next day (48 hours total)

    def __get_pv_forecast(self, config_entry):
        """
        Retrieves the photovoltaic (PV) power forecast based on the configured
        data source.

        Args:
            config_entry (dict): Configuration entry containing necessary
            parameters for the forecast.
            tgt_duration (int, optional): Target duration in hours for the
            forecast. Defaults to 24.

        Returns:
            list or dict: PV forecast data as returned by the selected data
            source API or default method.

        Notes:
            - Supported sources: "akkudoktor", "openmeteo", "forecast_solar",
              "solcast", "evcc", "victron", "default".
            - Logs a warning if the default source is used.
            - Logs an error and falls back to the default forecast if no valid
              source is configured.
        """
        if self.config_source.get("source") == "akkudoktor":
            return self.__get_pv_forecast_akkudoktor_api("power", config_entry)
        elif self.config_source.get("source") == "openmeteo":
            # return self.__get_pv_forecast_openmeteo_api(config_entry, tgt_duration)
            return self.__get_pv_forecast_openmeteo_lib(config_entry)
        elif self.config_source.get("source") == "openmeteo_local":
            return self.__get_pv_forecast_openmeteo_api(config_entry)
        elif self.config_source.get("source") == "forecast_solar":
            return self.__get_pv_forecast_forecast_solar_api(config_entry)
        elif self.config_source.get("source") == "evcc":
            return self.__get_pv_forecast_evcc_api(config_entry)
        elif self.config_source.get("source") == "solcast":
            return self.__get_pv_forecast_solcast_api(config_entry)
        elif self.config_source.get("source") == "victron":
            return self.__get_pv_forecast_victron_api(config_entry)
        elif self.config_source.get("source") == "default":
            logger.warning("[PV-IF] Using default PV forecast source")
            return self.__get_default_pv_forcast(
                config_entry.get("power") or DEFAULT_PV_NOMINAL_POWER_W
            )
        else:
            logger.error("[PV-IF] No valid source configured for PV forecast")
            return self.__get_default_pv_forcast(
                config_entry.get("power") or DEFAULT_PV_NOMINAL_POWER_W
            )

    def get_summarized_pv_forecast(self, scale: bool = True):
        """
        Request PV forecast for each config entry and summarize the values.

        Args:
            scale: If True (default), apply the current autoscaler factors when
                available and enabled. If False, return the raw aggregated source
                values without any autoscaling adjustment.

        Returns an empty forecast array if configuration is incomplete or invalid.
        On success, caches the result for fallback on future API failures.
        """
        # Guard: If configuration is incomplete, return empty array
        # This allows the system to continue running while user fixes config via web UI
        if not self.configuration_valid:
            logger.debug(
                "[PV-IF] Skipping PV forecast retrieval - configuration state: %s",
                self.configuration_state,
            )
            return self.__get_default_pv_forcast(0)  # Return zeros for all time slots

        forecast_values = []
        if self.config_special and self.config_source.get("source") == "evcc":
            logger.debug("[PV-IF] fetching forecast for evcc config")
            forecast = self.__get_pv_forecast("evcc_config")
            forecast_values = forecast
        elif self.config_source.get("source") == "timeseries":
            logger.debug("[PV-IF] fetching forecast for timeseries config")
            forecast = self.__get_pv_forecast_timeseries()
            forecast_values = forecast
        elif self.config_source.get("source") == "default" and not self.config:
            # "default" is configuration-free by design: the setup wizard asks for no
            # installation at all, so there is usually nothing here to read a power
            # from and the loop below would summarize an empty list into no forecast.
            # Assume a typical home array instead. With installations present the loop
            # still runs and the curve is summed per entry, at the user's real sizes.
            logger.debug(
                "[PV-IF] building the built-in forecast for a nominal %s W array"
                " (no installation configured)",
                DEFAULT_PV_NOMINAL_POWER_W,
            )
            forecast_values = self.__get_default_pv_forcast(DEFAULT_PV_NOMINAL_POWER_W)
        else:
            for config_entry in self.config:
                logger.debug("[PV-IF] fetching forecast for '%s'", config_entry["name"])
                forecast = self.__get_pv_forecast(config_entry)
                # print("values for " + config_entry+ " -> ")
                # print(forecast)
                if not forecast_values:
                    forecast_values = forecast
                else:
                    forecast_values = [x + y for x, y in zip(forecast_values, forecast)]
        # round all values to 1 decimal place
        forecast_values = [round(value, 1) for value in forecast_values]
        logger.debug("[PV-IF] Summarized PV forecast values: %s", forecast_values)

        # Cache successful forecast for fallback on future failures. The cache must hold
        # unscaled source values: the autoscaler derives its factors from this array, so a
        # scaled array served from the cache would feed the correction its own output.
        if forecast_values:
            self.last_successful_pv_forecast = forecast_values.copy()
            self.consecutive_failures = 0  # Reset failure counter on success
            logger.debug(
                "[PV-IF] PV forecast cached (%d values) for fallback on future API failures",
                len(forecast_values),
            )

        # Keep all autoscaler logic in one place: the final summary boundary.
        if scale:
            forecast_values = self.apply_autoscaling(forecast_values)

        return forecast_values

    def apply_autoscaling(self, forecast_values):
        """
        Apply the autoscaler's timeframe multipliers to an unscaled forecast array.

        Returns an unscaled copy when no autoscaler is attached, it is disabled, or
        scaling raises - the unscaled forecast is always a valid result. It is a copy so
        that the caller never ends up storing the scaled and raw arrays as one object:
        aliasing them would let a single in-place edit corrupt both, including the raw
        array the autoscaler records as `forecast_kwh` and trains the correction on.
        """
        if not forecast_values:
            return list(forecast_values)
        if self._autoscaler is None or not getattr(self._autoscaler, "enabled", False):
            return list(forecast_values)
        try:
            scaled = self._autoscaler.apply_scaling(forecast_values, self.time_frame_base)
        except Exception:
            logger.exception("[PV-IF] Error applying autoscaler - returning raw forecast")
            return list(forecast_values)
        if logger.isEnabledFor(logging.DEBUG):
            getter = getattr(self._autoscaler, "get_scale_factors", None)
            logger.debug(
                "[PV-IF] Auto-scaling applied. Multipliers: %s",
                getter() if callable(getter) else "n/a",
            )
        return scaled

    def __get_pv_forecast_timeseries(self, tgt_duration=48):
        """
        Retrieve the PV forecast from a generic timeseries data source (HTTP
        endpoint or Home Assistant sensor), analogous to
        PriceInterface.__retrieve_prices_from_url.

        Fetched once globally (not per pv_forecast.N array entry), since the
        external source is expected to already provide the combined forecast
        for the whole installation - mirrors how the "evcc" source is handled.

        Canonical format: [{start, end, value}, ...] — the format EVCC publishes, so
        an EVCC-shaped HA template sensor works unchanged. See timeseries_normalizer
        for the exact contract. Resolution (hourly or 15-min) is auto-detected.

        Config fields used (from pv_forecast_source):
        - data_url: Full HTTP endpoint URL (HA or HTTP custom endpoint)
        - data_path: JSON path to the timeseries array (e.g. "attributes.data")
        - data_token: Optional bearer token for authentication
        - value_unit: Unit of the "value" field (default W, as EVCC delivers)
        """
        data_url = self.config_source.get("data_url", "").strip()
        data_path = (
            self.config_source.get("data_path", "attributes.data").strip()
            or "attributes.data"
        )
        data_token = self.config_source.get("data_token", "").strip()
        value_unit = self.config_source.get("value_unit", "W").strip() or "W"

        fallback_power = self.config[0]["power"] if self.config else 1000

        if not data_url:
            return self._handle_interface_error(
                "config_error",
                "Data URL (data_url) not configured for timeseries PV source",
                "timeseries_config",
                "timeseries",
            ) or self.__get_default_pv_forcast(fallback_power)

        headers = {"Content-Type": "application/json"}
        if data_token:
            headers["Authorization"] = f"Bearer {data_token}"

        logger.debug(
            "[PV-IF] Fetching PV forecast from timeseries source: %s (path: %s, unit: %s)",
            data_url,
            data_path,
            value_unit,
        )

        def request_and_parse():
            response = requests.get(data_url, headers=headers, timeout=10)
            response.raise_for_status()
            response_data = response.json()
            timeseries = extract_json_path(response_data, data_path, label="PV-IF")
            if not isinstance(timeseries, list):
                raise ValueError(f"Data at path '{data_path}' is not a list")
            return timeseries

        def error_handler(error_type, exception):
            error_detail = str(exception)
            # Provide more helpful error messages based on error type
            if error_type == "timeout":
                error_msg = (
                    f"Timeseries data source timeout after 10s - "
                    f"check network connectivity to {data_url} | "
                    f"Recovery: {self.consecutive_failures + 1}/{self.max_failures}"
                )
            elif error_type == "request_failed":
                error_msg = (
                    f"Timeseries data source request failed: {error_detail} | "
                    f"Recovery: {self.consecutive_failures + 1}/{self.max_failures}"
                )
            elif error_type == "invalid_json":
                error_msg = (
                    f"Timeseries data source returned invalid JSON: {error_detail} | "
                    f"check data_url and data_path | "
                    f"Recovery: {self.consecutive_failures + 1}/{self.max_failures}"
                )
            elif error_type == "parsing_error":
                error_msg = (
                    f"Failed to extract data from path '{data_path}': {error_detail} | "
                    f"check data_path setting | "
                    f"Recovery: {self.consecutive_failures + 1}/{self.max_failures}"
                )
            else:
                error_msg = f"Timeseries error ({error_type}): {error_detail}"

            return self._handle_interface_error(
                error_type,
                error_msg,
                "timeseries_source",
                "timeseries",
            )

        timeseries = self._retry_request(request_and_parse, error_handler)
        if not timeseries:
            logger.debug(
                "[PV-IF] Timeseries fetch failed after retries - "
                "using last_successful_forecast=%s or defaults",
                "available" if self.last_successful_pv_forecast else "none",
            )
            return self.last_successful_pv_forecast or self.__get_default_pv_forcast(
                fallback_power
            )

        try:
            logger.debug(
                "[PV-IF] Timeseries fetched successfully (%d entries) - parsing...",
                len(timeseries),
            )
            forecast_values = self.__parse_pv_timeseries(
                timeseries, tgt_duration, value_unit=value_unit
            )
            if not forecast_values:
                logger.warning(
                    "[PV-IF] Timeseries parsing returned empty - "
                    "no valid data entries matched target duration"
                )
                return self._handle_interface_error(
                    "processing_error",
                    "Failed to parse PV timeseries data (empty result)",
                    "timeseries_source",
                    "timeseries",
                ) or self.__get_default_pv_forcast(fallback_power)

            logger.debug(
                "[PV-IF] Timeseries parsed successfully: %d values, "
                "range [%.1f - %.1f Wh]",
                len(forecast_values),
                min(forecast_values) if forecast_values else 0,
                max(forecast_values) if forecast_values else 0,
            )
            self.pv_forcast_request_error["error"] = None
            return forecast_values
        except (ValueError, TypeError) as e:
            logger.error(
                "[PV-IF] Timeseries parsing error: %s", str(e)
            )
            return self._handle_interface_error(
                "processing_error",
                f"Error parsing PV timeseries: {e}",
                "timeseries_source",
                "timeseries",
            ) or self.__get_default_pv_forcast(fallback_power)

    def __parse_pv_timeseries(
        self, timeseries, tgt_duration, resolution_seconds=None, value_unit=None
    ):
        """
        Parse and validate a PV forecast timeseries.

        Canonical format: [{start, end, value}, ...]
        - start: ISO8601 string or Unix timestamp (seconds)
        - end: optional, derived from the next entry when absent
        - value: generated power/energy in *value_unit* (non-negative)
        - Supports hourly (48 values) or 15-minute (192 values) resolution

        Mirrors PriceInterface.__parse_price_timeseries, adapted for PV: values
        represent energy-per-slot rather than a rate, so 15-min-to-hourly
        conversion sums instead of averages, and missing trailing slots are
        padded with 0 (no production) rather than the last known value.

        Args:
            value_unit: Unit of the incoming values (see
                timeseries_normalizer.PV_UNITS). ``None`` means the caller already
                supplies canonical Wh-per-slot values and needs no normalization.
        """
        if not timeseries or not isinstance(timeseries, list):
            logger.error("[PV-IF] PV timeseries is not a list")
            return []

        if len(timeseries) == 0:
            logger.error("[PV-IF] PV timeseries is empty")
            return []

        # self.time_zone is a zone *name* here, not a tzinfo — resolve it once for both
        # normalization and the slot alignment further down.
        try:
            tz = pytz.timezone(self.time_zone)
        except (pytz.UnknownTimeZoneError, AttributeError):
            tz = pytz.UTC

        if value_unit is not None:
            # External source: normalize field/timestamp shape first, then detect the
            # source resolution, because a power unit only becomes energy once the
            # slot length is known.
            try:
                timeseries = normalize_entries(timeseries, tz, label="PV-IF")
            except TimeseriesFormatError as exc:
                logger.error("[PV-IF] Invalid PV timeseries: %s", exc)
                return []

            if resolution_seconds is None:
                resolution_seconds = self.__detect_pv_timeseries_resolution(timeseries)
                if resolution_seconds is None:
                    logger.error("[PV-IF] Could not detect PV timeseries resolution")
                    return []

            try:
                convert_pv_values(timeseries, value_unit, resolution_seconds)
            except TimeseriesFormatError as exc:
                logger.error("[PV-IF] Invalid PV timeseries: %s", exc)
                return []

            installed_power_w = sum(
                float(entry.get("power", 0) or 0)
                for entry in (self.config or [])
                if isinstance(entry, dict)
            )
            warning = pv_plausibility_message(
                [entry["value"] for entry in timeseries],
                value_unit,
                resolution_seconds,
                installed_power_w,
            )
            if warning:
                logger.warning("[PV-IF] %s", warning)

            # State the unit once per unit change. The canonical unit moved from
            # Wh-per-slot to W, so a config carried over from an earlier version
            # silently changes meaning on 15-minute data.
            if getattr(self, "_logged_value_unit", None) != value_unit:
                logger.info(
                    "[PV-IF] Timeseries PV values interpreted as '%s' at %ds "
                    "resolution (peak %.0f Wh per slot)",
                    value_unit,
                    resolution_seconds,
                    max((entry["value"] for entry in timeseries), default=0.0),
                )
                self._logged_value_unit = value_unit
        else:
            first = timeseries[0]
            if not isinstance(first, dict) or not all(
                k in first for k in ("start", "value")
            ):
                logger.error(
                    "[PV-IF] Invalid PV timeseries format: missing start or value"
                )
                return []

        if resolution_seconds is None:
            resolution_seconds = self.__detect_pv_timeseries_resolution(timeseries)
            if resolution_seconds is None:
                logger.error("[PV-IF] Could not detect PV timeseries resolution")
                return []

        if resolution_seconds == 900 and self.time_frame_base == 3600:
            logger.debug(
                "[PV-IF] Converting source 15-min to system hourly resolution"
            )
            timeseries = self.__convert_15min_to_hourly_pv_timeseries(timeseries)
        elif resolution_seconds == 3600 and self.time_frame_base == 900:
            logger.error(
                "[PV-IF] Resolution mismatch: data source provides hourly (3600s) "
                "but system configured for 15-min (900s) slots. "
                "Set time_frame_base to 3600 or switch to a 15-minute data source."
            )
            return []
        elif resolution_seconds not in (900, 3600):
            logger.error(
                "[PV-IF] Unsupported resolution: %d seconds (expected 900 or 3600)",
                resolution_seconds,
            )
            return []

        # Align by absolute timestamp to the slot grid starting at local midnight
        # today - NOT positionally. get_ems_data() indexes pv_forcast_array by
        # "slots since midnight" (see eos_connect.py's current_slot calculation),
        # the same convention __get_pv_forecast_evcc_api() already follows. A
        # source whose first entry is "now" (like ours) rather than "midnight"
        # would otherwise land at the wrong array index. (tz resolved above.)

        def parse_ts(ts_val):
            if isinstance(ts_val, datetime):
                # Already normalized (and localized) by timeseries_normalizer.
                return ts_val.astimezone(tz)
            if isinstance(ts_val, (int, float)):
                return datetime.fromtimestamp(ts_val, tz=pytz.UTC).astimezone(tz)
            if isinstance(ts_val, str):
                try:
                    parsed = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                except ValueError:
                    parsed = datetime.fromisoformat(ts_val)
                if parsed.tzinfo is None:
                    parsed = tz.localize(parsed)
                return parsed.astimezone(tz)
            return None

        slot_seconds = 3600 if self.time_frame_base == 3600 else 900
        lookup = {}
        try:
            for item in timeseries:
                ts = parse_ts(item.get("start"))
                if ts is None:
                    continue
                value = float(item.get("value", 0))
                if value < 0:
                    logger.warning("[PV-IF] Negative PV value %.1f clamped to 0", value)
                    value = 0.0

                # Align timestamp to resolution boundary (robust to arbitrary start times)
                # E.g., for 3600s resolution: round to nearest hour
                #       for 900s resolution: round to nearest 15-minute
                ts = ts.replace(second=0, microsecond=0)
                total_seconds = int(ts.timestamp())
                aligned_seconds = (total_seconds // slot_seconds) * slot_seconds
                ts = datetime.fromtimestamp(aligned_seconds, tz=pytz.UTC).astimezone(tz)
                lookup[ts] = value
        except (ValueError, TypeError):
            logger.error("[PV-IF] Failed to extract numeric PV values")
            return []

        now_local = datetime.now(tz)
        midnight_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        expected_count = 48 if self.time_frame_base == 3600 else 192
        slot_keys = [
            midnight_today + timedelta(seconds=slot_seconds * i)
            for i in range(expected_count)
        ]
        values = [round(lookup.get(key, 0.0), 1) for key in slot_keys]

        matched = sum(1 for key in slot_keys if key in lookup)
        if matched == 0 and lookup:
            # Every entry parsed but none landed in the window. Returning 48 zeros
            # here reads as "no sun for two days" and used to pass silently; the most
            # common cause is a timestamp without UTC offset, so name what we saw.
            first_source_ts = min(lookup)
            logger.warning(
                "[PV-IF] None of %d PV timeseries entries fell into the %d-slot "
                "window starting %s. First source timestamp is %s — if the source "
                "renders UTC without an offset, every slot is shifted. See %s",
                len(lookup),
                expected_count,
                midnight_today.isoformat(),
                first_source_ts.isoformat(),
                TEMPLATE_DOCS_ANCHOR,
            )
        elif matched < expected_count:
            logger.debug(
                "[PV-IF] PV timeseries filled %d of %d slots (rest padded with 0)",
                matched,
                expected_count,
            )

        return values

    def __detect_pv_timeseries_resolution(self, timeseries):
        """
        Detect time resolution (900s for 15-min, 3600s for hourly).
        Identical logic to PriceInterface.__detect_price_timeseries_resolution.

        Returns:
            int: Seconds per interval (900 or 3600), or None if cannot detect
        """
        if len(timeseries) < 2:
            return None

        try:
            from datetime import datetime as dt_class
            import pytz

            def parse_ts(ts_str):
                if isinstance(ts_str, dt_class):
                    # Already normalized by timeseries_normalizer.
                    return ts_str
                if isinstance(ts_str, (int, float)):
                    return dt_class.fromtimestamp(ts_str, tz=pytz.UTC)
                if isinstance(ts_str, str):
                    try:
                        return dt_class.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        return dt_class.fromisoformat(ts_str)
                return None

            start1 = parse_ts(timeseries[0].get("start"))
            start2 = parse_ts(timeseries[1].get("start"))

            if start1 is None or start2 is None:
                return None

            delta = int((start2 - start1).total_seconds())

            if delta == 900:
                logger.debug("[PV-IF] Detected 15-minute PV timeseries resolution")
                return 900
            elif delta == 3600:
                logger.debug("[PV-IF] Detected hourly PV timeseries resolution")
                return 3600
            else:
                logger.warning(
                    "[PV-IF] Unexpected PV timeseries resolution delta: %d seconds",
                    delta,
                )
                return None
        except (KeyError, TypeError, ValueError):
            return None

    def __convert_15min_to_hourly_pv_timeseries(self, timeseries):
        """
        Convert 15-minute PV energy values to hourly by summing 4 consecutive
        slots. Unlike PriceInterface's rate-based averaging, PV values are
        energy-per-slot, so summing (not averaging) is the correct aggregation.

        Returns:
            list: Hourly timeseries with summed values
        """
        if len(timeseries) < 4:
            logger.warning("[PV-IF] Not enough 15-min PV data to sum hourly")
            return timeseries

        hourly = []
        for i in range(0, len(timeseries), 4):
            group = timeseries[i : i + 4]
            try:
                total_value = sum(float(item.get("value", 0)) for item in group)
                hourly.append(
                    {
                        "start": group[0].get("start"),
                        "end": group[-1].get("end"),
                        "value": total_value,
                    }
                )
            except (ValueError, TypeError):
                pass

        logger.debug(
            "[PV-IF] Converted %d 15-min PV values to %d hourly values",
            len(timeseries),
            len(hourly),
        )
        return hourly

    def __akkudoktor_hold_remaining(self):
        """Seconds left on an active akkudoktor rate-limit hold; 0 when clear."""
        if self._akkudoktor_hold_until is None:
            return 0
        remaining = (self._akkudoktor_hold_until - datetime.now()).total_seconds()
        if remaining <= 0:
            self._akkudoktor_hold_until = None
            return 0
        return int(remaining)

    def __akkudoktor_hold_response(self, tgt_value, pv_config_entry, hold_seconds):
        """
        Report the rate-limit hold and hand back the cached forecast for *tgt_value*.

        Like the Forecast.Solar equivalent this leaves the failure counters untouched:
        waiting out a quota is not a failed fetch, and counting it as one would burn
        through ``max_failures`` and discard the very cache the hold protects.  An empty
        return means there is no cache yet, which is the caller's cue to fall back to its
        own default - for temperature the 15 degC curve, never a fabricated 0 degC one.
        """
        error_slot = (
            self.temp_forecast_request_error
            if tgt_value == "temperature"
            else self.pv_forcast_request_error
        )
        error_slot.update(
            {
                "error": "rate_limit",
                "timestamp": datetime.now().isoformat(),
                "message": (
                    "akkudoktor.net is being rate limited by the weather provider it"
                    f" queries - no further {tgt_value} requests for {hold_seconds}s."
                    " Nothing is wrong with the configuration; the API returns this as"
                    " an HTTP 500 whose body reads 'Request failed with status code 429'."
                ),
                "config_entry": pv_config_entry,
                "source": "akkudoktor",
            }
        )
        if tgt_value == "temperature":
            return list(self.last_successful_temp_forecast)
        return list(self.last_successful_pv_forecast)

    def __get_pv_forecast_akkudoktor_api(
        self, tgt_value="power", pv_config_entry=None, tgt_duration=48
    ):
        """
        Fetches the PV forecast data from the EOS API and processes it to extract
        power and temperature values for the specified duration starting from the current hour.
        """
        if pv_config_entry is None:
            return self._handle_interface_error(
                "config_error",
                f"No PV config entry provided for target: {tgt_value}",
                {},
                "akkudoktor",
                target=tgt_value,
            )

        hold_remaining = self.__akkudoktor_hold_remaining()
        if hold_remaining > 0:
            logger.warning(
                "[PV-IF] akkudoktor.net rate limit still active - skipping the %s"
                " request for another %d s. Calling again earlier only spends requests"
                " against a quota that is already exhausted.",
                tgt_value,
                hold_remaining,
            )
            return self.__akkudoktor_hold_response(
                tgt_value, pv_config_entry, hold_remaining
            )

        # Temperature is a location-only query; PV needs the real installation.
        forecast_params = (
            self.__create_temperature_request(pv_config_entry)
            if tgt_value == "temperature"
            else self.__create_forecast_request(pv_config_entry)
        )

        def request_func():
            response = requests.get(
                EOS_API_GET_PV_FORECAST, params=forecast_params, timeout=5
            )
            # A relayed 429 has to be recognised before raise_for_status() turns it into
            # an ordinary 500: retrying it is what the provider is asking us not to do.
            if response.status_code >= 400:
                body = _akkudoktor_body_excerpt(response)
                if _akkudoktor_upstream_status(body) == 429:
                    raise _AkkudoktorRateLimit(AKKUDOKTOR_RATE_LIMIT_HOLD_S)
            response.raise_for_status()
            day_values = response.json()
            return day_values["values"]

        # Tracked locally (not via self.pv_forcast_request_error, which can
        # still hold a stale error from an earlier unrelated call) so we know
        # whether day_values below is raw API JSON or an already-final
        # fallback array from _handle_interface_error.
        failure = {"occurred": False}

        def error_handler(error_type, exception):
            failure["occurred"] = True
            return self._handle_interface_error(
                error_type,
                _describe_akkudoktor_error(tgt_value, exception),
                pv_config_entry,
                "akkudoktor",
                target=tgt_value,
            )

        retries, delay = (
            (TEMP_MAX_RETRIES, TEMP_RETRY_DELAY_S)
            if tgt_value == "temperature"
            else (5, 3)
        )
        try:
            day_values = self._retry_request(
                request_func, error_handler, retries, delay
            )
        except _AkkudoktorRateLimit as exc:
            self._akkudoktor_hold_until = datetime.now() + timedelta(
                seconds=exc.hold_seconds
            )
            logger.warning(
                "[PV-IF] akkudoktor.net relayed an upstream 429 (as HTTP 500) -"
                " pausing requests for %d s. The request is not at fault; the weather"
                " provider behind the API is rate limiting it.",
                exc.hold_seconds,
            )
            self._log_error_diagnostics("rate_limit", "akkudoktor", target=tgt_value)
            return self.__akkudoktor_hold_response(
                tgt_value, pv_config_entry, exc.hold_seconds
            )

        if failure["occurred"]:
            # day_values is whatever _handle_interface_error picked as the fallback: a
            # cached forecast, or [] when there is no cache yet.  Neither is raw API
            # JSON, so neither may reach the processing below.  Letting the empty one
            # through used to pad it out to a full-length array of zeros and then log
            # "fetched successfully" - for temperature that is 48 h of 0 degC, cold
            # enough to be wrong by 20 K yet plausible enough to clear every downstream
            # guard, stored as the last *successful* forecast and re-served for an hour.
            return day_values

        # Data processing
        try:
            forecast_values = []
            tz = pytz.timezone(self.time_zone)
            current_time = tz.localize(
                datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            )
            end_time = current_time + timedelta(hours=tgt_duration)

            for forecast_entry in day_values:
                for forecast in forecast_entry:
                    entry_time = datetime.fromisoformat(forecast["datetime"])
                    if entry_time.tzinfo is None:
                        # If datetime is naive, localize it
                        entry_time = pytz.timezone(self.time_zone).localize(entry_time)
                    else:
                        # Convert to configured timezone
                        entry_time = entry_time.astimezone(
                            pytz.timezone(self.time_zone)
                        )
                    if current_time <= entry_time < end_time:
                        value = forecast.get(tgt_value, 0)
                        # if power is negative, set it to 0 (fixing wrong values from api)
                        if tgt_value == "power" and value < 0:
                            value = 0
                        forecast_values.append(value)

            # workaround for wrong time points in the forecast from akkudoktor:
            # the series starts one slot early, so drop the first entry and pad the end.
            # A dropped Watt slot is night-time, so 0 is right there - but a temperature
            # of 0 degrees is a real reading, and padding one in put a bogus cold hour at
            # the end of every curve. Repeat the last value instead, which is what the
            # length correction below already does.
            if forecast_values:
                forecast_values.pop(0)
                forecast_values.append(
                    forecast_values[-1] if tgt_value == "temperature" else 0
                )

            # fix for time changes e.g. western europe then fill or reduce
            # the array to target duration
            if len(forecast_values) > tgt_duration:
                forecast_values = forecast_values[:tgt_duration]
                logger.debug(
                    "[PV-IF][akkudoktor] Day of time change - values reduced to %s for %s",
                    tgt_duration,
                    pv_config_entry.get("name", "unknown"),
                )
            elif len(forecast_values) < tgt_duration:
                if forecast_values:
                    forecast_values.extend(
                        [forecast_values[-1]] * (tgt_duration - len(forecast_values))
                    )
                elif tgt_value == "temperature":
                    # Nothing to extend from.  A zero-filled Watt array is merely a
                    # pessimistic forecast, but a zero-filled temperature array is a
                    # fabricated reading - and one the +-60 degC plausibility guard
                    # cannot reject.  Report the emptiness and let the caller use its
                    # 15 degC default instead.
                    logger.warning(
                        "[PV-IF] Akkudoktor returned no temperature values in the"
                        " forecast window - no curve to serve"
                    )
                    return []
                else:
                    forecast_values = [0] * tgt_duration
                logger.debug(
                    "[PV-IF][akkudoktor] Day of time change - values extended to %s for %s",
                    tgt_duration,
                    pv_config_entry.get("name", "unknown"),
                )

            # Clear any previous errors on success - only for the target that just
            # succeeded, so a good temperature fetch never masks a failing PV fetch.
            if tgt_value == "temperature":
                self.temp_forecast_request_error["error"] = None
            else:
                self.pv_forcast_request_error["error"] = None

            request_type = (
                "PV forecast" if tgt_value == "power" else "Temperature forecast"
            )
            pv_config_name = (
                f"for {pv_config_entry.get('name', 'unknown')}"
                if tgt_value == "power"
                else ""
            )
            logger.debug(
                "[PV-IF] %s fetched successfully %s", request_type, pv_config_name
            )

            if self.time_frame_base == 900 and tgt_value == "power":
                result = self._convert_hourly_to_15min(forecast_values)
            elif self.time_frame_base == 900 and tgt_value == "temperature":
                # all values have to be repeated 4 times for 15min base for temperature
                result = []
                for val in forecast_values:
                    result.extend([val] * 4)
            else:
                result = forecast_values

            if tgt_value == "temperature" and result:
                self.last_successful_temp_forecast = list(result)
                self.consecutive_temp_failures = 0
                self._last_temp_fetch = time.monotonic()

            return result

        except (ValueError, TypeError, AttributeError, KeyError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing {tgt_value} forecast data: {e}",
                pv_config_entry,
                "akkudoktor",
                target=tgt_value,
            )

    def __get_horizon_elevation(self, sun_azimuth, horizon_for_elev):

        if not horizon_for_elev or len(horizon_for_elev) == 0:
            horizon_for_elev = [0] * 36

        # Normalize horizon_for_elev string to a list of integers (handle '50t0.4' as 50)
        if isinstance(horizon_for_elev, str):
            horizon_for_elev = [
                int(float(x.split("t")[0])) if "t" in x else int(float(x))
                for x in horizon_for_elev.split(",")
                if x.strip()
            ]
        else:
            horizon_for_elev = [int(float(x)) for x in horizon_for_elev]
        # Expand horizon_for_elev to 36 values by linear interpolation if needed
        if len(horizon_for_elev) != 36:
            # Interpolate to 36 values (full circle)
            x_old = np.linspace(0, 360, num=len(horizon_for_elev), endpoint=False)
            x_new = np.linspace(0, 360, num=36, endpoint=False)
            horizon_for_elev = np.interp(x_new, x_old, horizon_for_elev).tolist()
        # logger.debug(
        #     "[PV-IF] Horizon elevation values normalized to 36 values: %s",
        #     horizon_for_elev
        # )

        idx = int((sun_azimuth / 10))  # Convert azimuth to index (0-35)
        # logger.debug(
        #     "[PV-IF] azimuth %s° to horizon_for_elev index %s - elevation: %s°",
        #     round(sun_azimuth,2),
        #     idx,
        #     horizon_for_elev[idx]
        # )
        return horizon_for_elev[idx]

    def __get_pv_forecast_openmeteo_api(self, pv_config_entry, hours=48):
        """
        Fetches weather data from Open-Meteo and estimates PV forecast using
        panel tilt and azimuth from pv_config_entry.
        """
        latitude = pv_config_entry["lat"]
        longitude = pv_config_entry["lon"]
        tilt = pv_config_entry.get("tilt", 30)  # degrees
        azimuth = pv_config_entry.get(
            "azimuth", 0
        )  # degrees (0=South - industry standard)
        installed_power_watt = pv_config_entry.get(
            "power", 200
        )  # value in config is in watts
        horizon_openmeteo_api = pv_config_entry.get(
            "horizon", [0] * 36
        )  # default: no shading
        pv_efficiency = pv_config_entry.get("inverterEfficiency", 0.85)
        cloud_factor = 0.3  # factor to adjust radiation based on cloud cover
        timezone = self.time_zone
        logger.debug(
            "[PV-IF] Open-Meteo PV forecast for"
            + " lat: %s, lon: %s, tilt: %s, azimuth: %s, power: %s W - horizon: %s",
            latitude,
            longitude,
            tilt,
            azimuth,
            installed_power_watt,
            horizon_openmeteo_api,
        )

        # Fetch weather data
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={latitude}&longitude={longitude}"
            f"&hourly=shortwave_radiation,cloudcover"
            f"&forecast_days={int(np.ceil(hours/24))}"
            f"&timezone={timezone}"
        )

        def request_func():
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            return response

        def error_handler(error_type, exception):
            return self._handle_interface_error(
                error_type,
                f"Open-Meteo API error for {pv_config_entry['name']}: {exception}",
                pv_config_entry,
                "openmeteo_api",
            )

        response = self._retry_request(request_func, error_handler)

        def json_func():
            return response.json()

        data = self._retry_request(json_func, error_handler)

        radiation = data["hourly"]["shortwave_radiation"][:hours]  # W/m²
        cloudcover = data["hourly"]["cloudcover"][:hours]  # %

        # Prepare time index - create datetime objects instead of pandas DatetimeIndex
        start_time = datetime.fromisoformat(
            data["hourly"]["time"][0].replace("Z", "+00:00")
        )
        times = [start_time + timedelta(hours=i) for i in range(hours)]

        # Get sun position using our custom function
        solpos = self._solar_position(times, latitude, longitude)
        logger.debug(
            "[PV-IF] Open-Meteo solar position calculated - first entry: %s", solpos[0]
        )

        # Calculate PV forecast
        pv_forecast = []
        for i, (rad, cc) in enumerate(zip(radiation, cloudcover)):
            # Calculate angle of incidence (AOI) using our custom function
            aoi = self._angle_of_incidence(
                surface_tilt=tilt,
                surface_azimuth=azimuth,
                solar_zenith=solpos[i]["apparent_zenith"],
                solar_azimuth=solpos[i]["azimuth"],
            )

            sun_az = solpos[i]["azimuth"]
            sun_el = 90 - solpos[i]["apparent_zenith"]

            # Adjust radiation for cloud cover
            eff_rad = rad * (1 - cc / 100) + rad * cloud_factor * (cc / 100)

            # Project radiation onto panel
            projection = max(math.cos(math.radians(aoi)), 0)

            # Adjust for panel efficiency (22,5% is a common value)
            eff_rad_panel = eff_rad * projection * 0.225

            # --- Horizon check ---
            horizon_elev = self.__get_horizon_elevation(sun_az, horizon_openmeteo_api)
            if sun_el < horizon_elev:
                eff_rad_panel = (
                    eff_rad_panel * 0.25
                )  # Sun is behind local horizon - 25% of radiation

            # Estimate PV energy output (Wh)
            energy_wh = (
                eff_rad_panel * pv_efficiency * installed_power_watt / 220
            )  # Assuming 220 W/m² as average panel efficiency for area estimation
            energy_wh = max(0, energy_wh)  # Ensure no negative values

            pv_forecast.append(round(energy_wh, 1))

        pv_forecast = [float(x) for x in pv_forecast]

        # Normalise to exactly 48 hourly slots so DST days never produce
        # a short or long array that would break downstream consumers.
        target_hourly = 48
        if len(pv_forecast) > target_hourly:
            pv_forecast = pv_forecast[:target_hourly]
        elif len(pv_forecast) < target_hourly:
            pad_val = pv_forecast[-1] if pv_forecast else 0.0
            pv_forecast.extend([pad_val] * (target_hourly - len(pv_forecast)))

        logger.debug(
            "[PV-IF] Open-Meteo PV forecast for '%s' (Wh): %s",
            pv_config_entry["name"],
            pv_forecast,
        )

        if self.time_frame_base == 900:
            return self._convert_hourly_to_15min(pv_forecast)

        return pv_forecast

    def __get_pv_forecast_openmeteo_lib(self, pv_config_entry):
        """
        Synchronous wrapper for the async OpenMeteoSolarForecast.
        """
        return asyncio.run(self.__get_pv_forecast_openmeteo_lib_async(pv_config_entry))

    async def __get_pv_forecast_openmeteo_lib_async(self, pv_config_entry):
        """
        Fetches PV forecast from OpenMeteo Solar Forecast library.
        """
        try:
            async with OpenMeteoSolarForecast(
                latitude=pv_config_entry["lat"],
                longitude=pv_config_entry["lon"],
                declination=pv_config_entry.get("tilt", 30),
                azimuth=pv_config_entry.get(
                    "azimuth", 0
                ),  # 0° = South (industry standard)
                dc_kwp=pv_config_entry.get("power", 200) / 1000,  # Convert to kW
                efficiency_factor=pv_config_entry.get("inverterEfficiency", 0.85),
            ) as forecast:
                estimate = await forecast.estimate()

        except (aiohttp.ClientError, ConnectionError) as e:
            return self._handle_interface_error(
                "connection_error",
                f"OpenMeteo Solar Forecast connection error: {e}",
                pv_config_entry,
                "openmeteo_lib",
            )
        except (ValueError, KeyError, AttributeError, TypeError) as e:
            return self._handle_interface_error(
                "api_error",
                f"OpenMeteo Solar Forecast API error: {e}",
                pv_config_entry,
                "openmeteo_lib",
            )

        # Data processing
        try:
            # Build an array of hourly values from now (hour=0) up
            # to tomorrow midnight (48 hours)
            pv_forecast = []
            # Calculate the number of hours remaining until tomorrow midnight
            # Use the current time in the forecast's timezone
            # Always use the start of the current hour in the forecast's timezone
            now = datetime.now(estimate.timezone).replace(
                minute=0, second=0, microsecond=0
            )
            # Find tomorrow's midnight in the forecast's timezone
            tomorrow_midnight = (now + timedelta(days=2)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            hours_until_tomorrow_midnight = int(
                (tomorrow_midnight - now).total_seconds() // 3600
            )
            hours_from_today_midnight = int(
                (
                    now - now.replace(hour=0, minute=0, second=0, microsecond=0)
                ).total_seconds()
                // 3600
            )

            for hour in range(
                -1 * hours_from_today_midnight, hours_until_tomorrow_midnight
            ):
                current_hour_energy = 0
                for minute in range(59):
                    current_hour_energy += estimate.power_production_at_time(
                        now + timedelta(hours=hour, minutes=minute)
                    )
                current_hour_energy = round(current_hour_energy / 60, 1)
                # time_point = now + timedelta(hours=hour, minutes=0)
                # logger.debug("TEST - : %s - %s", current_hour_energy, time_point)
                pv_forecast.append(current_hour_energy)

            # Normalise to exactly 48 hourly slots so DST days never produce
            # a short or long array.  The openmeteo lib computes
            # hours_until_tomorrow_midnight in wall-clock time, which yields
            # 47 on spring-forward and 49 on fall-back days.
            target_hourly = 48
            if len(pv_forecast) > target_hourly:
                pv_forecast = pv_forecast[:target_hourly]
            elif len(pv_forecast) < target_hourly:
                pv_forecast.extend([0.0] * (target_hourly - len(pv_forecast)))

            # Clear any previous errors on success
            self.pv_forcast_request_error["error"] = None

            logger.debug(
                "[PV-IF] OpenMeteo Lib PV forecast (Wh) (length: %s): %s",
                len(pv_forecast),
                pv_forecast,
            )
            if self.time_frame_base == 900:
                return self._convert_hourly_to_15min(pv_forecast)
            return pv_forecast

        except (ValueError, TypeError, AttributeError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing OpenMeteo forecast data: {e}",
                pv_config_entry,
                "openmeteo_lib",
            )

    def __forecast_solar_request_path(self, pv_config_entry):
        """
        Build the keyless part of a Forecast.Solar estimate URL, and a loggable twin.

        Two things must never reach the log here.  The API key, because it is the first
        path segment rather than a header or query parameter
        (https://doc.forecast.solar/api:estimate) - that one the caller prefixes, so it
        is not this function's problem.  And the coordinates, which are the user's home
        address to within metres: the bug reporter offers to paste recent log lines into
        a public GitHub issue, so a debug line carrying them is a real disclosure.

        Returns ``(path, loggable_path)``.  The second is built from the same parameters
        minus latitude and longitude, so everything that helps diagnose a malformed
        request survives and nothing private does.
        """
        latitude = pv_config_entry["lat"]
        longitude = pv_config_entry["lon"]
        tilt = pv_config_entry.get("tilt", 30)
        azimuth = pv_config_entry.get(
            "azimuth", 0
        )  # 0=South (industry standard: 0°=South, 90°=West, 180°=North, -90°=East)
        # Convert to kW for API and round to 4 decimal places
        installed_power_watt = round(pv_config_entry.get("power", 200) / 1000, 4)
        horizon_forecast_solar_api = ""
        if pv_config_entry.get("horizon", None) is not None:
            horizon_forecast_solar_api = pv_config_entry.get("horizon", [0] * 24)
            if isinstance(horizon_forecast_solar_api, str):
                # Convert horizon string to list of floats
                horizon_forecast_solar_api = [
                    float(x.split("t")[0]) if "t" in x else float(x)
                    for x in horizon_forecast_solar_api.split(",")
                    if x.strip()
                ]
            elif isinstance(horizon_forecast_solar_api, list):
                # Use the list directly
                pass
            else:
                # Fallback to default
                horizon_forecast_solar_api = [0] * 24

            # Ensure the list has 24 values, repeating if necessary
            horizon_forecast_solar_api = (
                horizon_forecast_solar_api * (24 // len(horizon_forecast_solar_api) + 1)
            )[:24]

        parameters = (
            f"{tilt}/{azimuth}/{installed_power_watt}"
            f"?horizon={','.join(map(str, horizon_forecast_solar_api))}"
        )
        return (
            f"estimate/{latitude}/{longitude}/{parameters}",
            f"estimate/<lat>/<lon>/{parameters}",
        )

    def __forecast_solar_retry_after_seconds(self, response):
        """
        How long to stay off Forecast.Solar after the 429 it just returned.

        Reads the most specific answer the response offers and falls back to the public
        tier's own period.  Everything is defensive: a malformed 429 body must not raise
        out of the error path, because the whole point is to stop calling.
        """
        candidates = []

        headers = getattr(response, "headers", None) or {}
        for header in ("Retry-After", "X-Ratelimit-Reset"):
            try:
                candidates.append(int(float(headers.get(header))))
            except (TypeError, ValueError):
                pass

        try:
            ratelimit = response.json().get("message", {}).get("ratelimit", {})
        except (ValueError, TypeError, AttributeError):
            ratelimit = {}

        retry_at = ratelimit.get("retry-at") if isinstance(ratelimit, dict) else None
        if retry_at:
            try:
                target = datetime.fromisoformat(str(retry_at).replace("Z", "+00:00"))
                now = datetime.now(target.tzinfo) if target.tzinfo else datetime.now()
                candidates.append(int((target - now).total_seconds()))
            except (ValueError, TypeError):
                pass

        if isinstance(ratelimit, dict):
            try:
                candidates.append(int(float(ratelimit.get("reset"))))
            except (TypeError, ValueError):
                pass

        usable = [c for c in candidates if c > 0]
        hold = usable[0] if usable else FORECAST_SOLAR_DEFAULT_HOLD_S
        return max(FORECAST_SOLAR_MIN_HOLD_S, min(FORECAST_SOLAR_MAX_HOLD_S, hold))

    def __forecast_solar_hold_remaining(self):
        """Seconds left on an active Forecast.Solar rate-limit hold; 0 when clear."""
        if self._forecast_solar_hold_until is None:
            return 0
        remaining = (self._forecast_solar_hold_until - datetime.now()).total_seconds()
        if remaining <= 0:
            self._forecast_solar_hold_until = None
            return 0
        return int(remaining)

    def __forecast_solar_hold_response(self, pv_config_entry, hold_seconds):
        """
        Report the rate-limit hold and hand back the cached forecast.

        Deliberately does not touch ``consecutive_failures``: waiting out a quota we
        were told to wait out is not a failed fetch, and counting it as one would burn
        through ``max_failures`` and discard the very cache the hold exists to protect.
        """
        self.pv_forcast_request_error.update(
            {
                "error": "rate_limit",
                "timestamp": datetime.now().isoformat(),
                "message": (
                    "Forecast.Solar rate limit reached - no further requests for "
                    f"{hold_seconds}s. The public tier allows 12 requests/hour per IP; "
                    "an API key (Settings -> PV Source) raises that quota."
                ),
                "config_entry": pv_config_entry,
                "source": "forecast_solar",
            }
        )
        return list(self.last_successful_pv_forecast)

    def __get_pv_forecast_forecast_solar_api(self, pv_config_entry):
        """
        Fetches PV forecast from Forecast.Solar API.
        """
        hold_remaining = self.__forecast_solar_hold_remaining()
        if hold_remaining > 0:
            logger.warning(
                "[PV-IF] Forecast.Solar rate limit still active - skipping this request"
                " for another %d s. Calling again earlier only renews the block.",
                hold_remaining,
            )
            return self.__forecast_solar_hold_response(pv_config_entry, hold_remaining)

        # The request URL carries both the API key (first path segment) and the user's
        # coordinates, so it is never logged as-is.  ``loggable_path`` is built without
        # the coordinates, and the key is masked here; the two strings are kept separate
        # all the way to the sink so nothing private has a route into the log.
        request_path, loggable_path = self.__forecast_solar_request_path(pv_config_entry)
        api_key = str(self.config_source.get("api_key", "") or "").strip()
        if api_key:
            url = f"https://api.forecast.solar/{api_key}/{request_path}"
            loggable_url = f"https://api.forecast.solar/***/{loggable_path}"
        else:
            url = f"https://api.forecast.solar/{request_path}"
            loggable_url = f"https://api.forecast.solar/{loggable_path}"
        logger.debug(
            "[PV-IF] Fetching PV forecast from Forecast.Solar API for '%s': %s",
            pv_config_entry.get("name", "unnamed"),
            loggable_url,
        )

        def request_func():
            response = requests.get(url, timeout=5)
            if response.status_code == 429:
                raise _ForecastSolarRateLimit(
                    self.__forecast_solar_retry_after_seconds(response)
                )
            if response.status_code in (401, 403):
                raise requests.exceptions.RequestException("auth_error")
            response.raise_for_status()
            return response

        def error_handler(error_type, exception):
            if str(exception) == "auth_error":
                message = (
                    "Forecast.Solar rejected the API key - check api_key in"
                    " Settings -> PV Source, or clear it to use the public tier"
                )
            else:
                message = f"Forecast.Solar API error: {exception}"
            return self._handle_interface_error(
                error_type,
                message,
                pv_config_entry,
                "forecast_solar",
            )

        try:
            response = self._retry_request(request_func, error_handler)
        except _ForecastSolarRateLimit as exc:
            self._forecast_solar_hold_until = datetime.now() + timedelta(
                seconds=exc.hold_seconds
            )
            logger.error(
                "[PV-IF] Forecast.Solar returned 429 - pausing requests for %d s.",
                exc.hold_seconds,
            )
            # Once, as the hold is armed.  The skip path below is silent about the
            # background because it runs every cycle until the hold expires.
            self._log_error_diagnostics("rate_limit", "forecast_solar")
            return self.__forecast_solar_hold_response(pv_config_entry, exc.hold_seconds)

        # _retry_request hands back the error handler's fallback - a forecast list -
        # when every attempt failed.  Without this the list fell through to .json()
        # below, raised AttributeError, and ran the error handler a second time,
        # counting one failed cycle twice against max_failures.
        if isinstance(response, list):
            return response

        def json_func():
            data = response.json()
            watt_hours_period = data.get("result", {}).get("watt_hours_period", {})
            return watt_hours_period

        watt_hours_period = self._retry_request(json_func, error_handler)

        # Data validation
        if not watt_hours_period:
            return self._handle_interface_error(
                "no_valid_data",
                "No valid watt_hours_period data found.",
                pv_config_entry,
                "forecast_solar",
            )

        # Data processing
        try:
            parsed = [
                (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"), v)
                for ts, v in watt_hours_period.items()
            ]
            min_time = min(dt for dt, _ in parsed)
            # Align to midnight of the first day
            midnight = min_time.replace(hour=0, minute=0, second=0, microsecond=0)
            # Build list of 48 hourly timestamps
            hours_list = [midnight + timedelta(hours=i) for i in range(48)]
            # Build a lookup dict for fast access
            lookup = {dt: v for dt, v in parsed}
            # Fill the forecast array
            forecast_values = []
            for h in hours_list:
                # Use value if exact hour exists, else 0
                forecast_values.append(lookup.get(h, 0))

            # Clear any previous errors on success
            self.pv_forcast_request_error["error"] = None

            pv_forecast = forecast_values
            if self.time_frame_base == 900:
                return self._convert_hourly_to_15min(pv_forecast)
            return pv_forecast

        except (ValueError, TypeError, AttributeError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing forecast data: {e}",
                pv_config_entry,
                "forecast_solar",
            )

    def _resolve_evcc_scale_factor(self, solar_forecast_scale):
        """
        Resolve the correction factor EVCC publishes with its solar forecast.

        Returns 1.0 when the user has disabled real-data correction or EVCC reported no
        usable value. A scale below 0.1 means EVCC has barely any measured data yet and
        would otherwise wipe out the forecast, so it is floored at 0.5.
        """
        use_real_data_correction = True
        if isinstance(getattr(self, "config_source", None), dict):
            use_real_data_correction = self.config_source.get("use_real_data_correction", True)

        if not use_real_data_correction:
            logger.debug(
                "[PV-IF] EVCC PV forecast: real data correction disabled - scale factor 1.0"
            )
            return 1.0

        try:
            scale_factor = float(solar_forecast_scale)
        except (TypeError, ValueError):
            return 1.0

        if scale_factor <= 0:
            logger.debug(
                "[PV-IF] EVCC PV forecast scale factor invalid (%s) - using 1.0",
                scale_factor,
            )
            return 1.0
        if scale_factor < 0.1:
            logger.debug(
                "[PV-IF] EVCC PV forecast scale factor too low (< 0.1 - %s) - using 0.5",
                scale_factor,
            )
            return 0.5
        return scale_factor

    def __get_pv_forecast_evcc_api(self, pv_config_entry, hours=48):
        """
        Fetches PV forecast from an EVCC instance.
        """
        if self.config_special.get("url", "") == "":
            logger.error(
                "[PV-IF] No EVCC URL configured for EVCC PV forecast - using default PV forecast"
            )
            return self.__get_default_pv_forcast(pv_config_entry.get("power", 200))

        url = self.config_special.get("url", "").rstrip("/") + "/api/state"
        logger.debug("[PV-IF] Fetching PV forecast from EVCC API: %s", url)

        def request_and_parse():
            """
            Perform the GET request and parse the EVCC JSON payload.
            This keeps request and parsing in the same retried closure so
            _retry_request never returns a non-Response that would later
            be used as if it were a Response object.
            """
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()
            solar_forecast_all = data.get("forecast", {}).get("solar", {})
            solar_forecast = solar_forecast_all.get("timeseries", [])
            solar_forecast_scale = solar_forecast_all.get("scale", "unknown")
            logger.debug(
                "[PV-IF] EVCC API solar forecast received (%d entries, scale: %s)",
                len(solar_forecast),
                solar_forecast_scale,
            )
            return solar_forecast, solar_forecast_scale

        def error_handler(error_type, exception):
            return self._handle_interface_error(
                error_type,
                f"EVCC API error: {exception}",
                pv_config_entry,
                "evcc",
            )


        result = self._retry_request(request_and_parse, error_handler)
        if not result:
            return self._handle_interface_error(
                "no_valid_data",
                "No valid solar forecast data found in EVCC API.",
                pv_config_entry,
                "evcc",
            )
        # On a handled error the error_handler returns a bare list (cache or []), so
        # only unpack when the retried closure actually produced its pair.
        if isinstance(result, tuple):
            solar_forecast, solar_forecast_scale = result
        else:
            solar_forecast, solar_forecast_scale = result, "unknown"

        if not solar_forecast or not isinstance(solar_forecast, list):
            return self._handle_interface_error(
                "no_valid_data",
                "No valid solar forecast data found in EVCC API.",
                pv_config_entry,
                "evcc",
            )

        try:
            # Get timezone-aware current time
            tz = pytz.timezone(self.time_zone)
            current_time = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
            midnight_today = current_time.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            forecast_hours = [midnight_today + timedelta(hours=i) for i in range(hours)]
            pv_forecast = [0.0] * hours  # Initialize with zeros

            # --- AGGREGATE 15-min intervals to hourly Wh if needed ---
            forecast_items = []
            for item in solar_forecast:
                # EVCC <= 0.312 used objects ({ts, val}); newer EVCC
                # versions publish compact [unix_timestamp, value] arrays.
                if isinstance(item, dict):
                    timestamp_value = item.get("ts")
                    power_value = item.get("val", 0)
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    timestamp_value, power_value = item[0], item[1]
                else:
                    logger.warning("[PV-IF] Ignoring invalid EVCC forecast item: %r", item)
                    continue

                try:
                    if isinstance(timestamp_value, (int, float)):
                        # Unix timestamps may be published in seconds or milliseconds.
                        timestamp_seconds = (
                            timestamp_value / 1000.0
                            if abs(timestamp_value) >= 1_000_000_000_000
                            else timestamp_value
                        )
                        ts = datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
                    elif isinstance(timestamp_value, str):
                        ts = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = tz.localize(ts)
                    else:
                        raise ValueError("unsupported timestamp type")

                    ts = ts.astimezone(tz)
                    # EVCC values are power in W for each 15-minute slot.
                    val_wh = float(power_value) * 0.25
                    forecast_items.append((ts, val_wh))
                except (TypeError, ValueError, OverflowError, OSError) as exc:
                    logger.warning(
                        "[PV-IF] Ignoring invalid EVCC forecast item %r: %s",
                        item,
                        exc,
                    )

            if self.time_frame_base == 3600:
                # Group by hour and sum Wh values
                hourly_values = defaultdict(float)
                for ts, val_wh in forecast_items:
                    hour_ts = ts.replace(minute=0, second=0, microsecond=0)
                    hourly_values[hour_ts] += val_wh

                # Fill forecast array for 48 hours from midnight
                for i, hour in enumerate(forecast_hours):
                    pv_forecast[i] = hourly_values.get(hour, 0.0)
            elif self.time_frame_base == 900:
                # Fill forecast array for 192 15-min intervals from midnight
                forecast_15min = [0.0] * 192
                # Build a lookup for fast access
                forecast_lookup = {ts: val for ts, val in forecast_items}
                for i in range(192):
                    interval_time = midnight_today + timedelta(minutes=15 * i)
                    forecast_15min[i] = forecast_lookup.get(interval_time, 0.0)
                pv_forecast = forecast_15min


            # EVCC learns its own correction factor from measured yield and publishes it
            # alongside the forecast. Apply it here, per source. The PV autoscaler then
            # sits on top of the corrected values and learns only the residual bias, so
            # the two corrections compose instead of double-counting.
            scale_factor = self._resolve_evcc_scale_factor(solar_forecast_scale)
            pv_forecast = [round(val * scale_factor, 1) for val in pv_forecast]

            # Clear any previous errors on success
            self.pv_forcast_request_error["error"] = None

            logger.debug(
                "[PV-IF] EVCC PV forecast for given evcc pv config (Wh): %s",
                pv_forecast,
            )
            return pv_forecast

        except (TypeError, ValueError, AttributeError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing forecast values: {e}",
                pv_config_entry,
                "evcc",
            )

    def __get_pv_forecast_solcast_api(self, pv_config_entry, tgt_duration=48):
        """
        Fetches PV forecast from Solcast API using resource ID endpoint.

        For Solcast, the resource_id is stored in pv_forecast_source.resource_id
        (can be comma-separated).
        Each config entry in pv_forecast can represent a single installation if needed.

        Args:
            pv_config_entry (dict): Configuration entry for this PV installation
            (contains name, lat, lon, etc.)
            tgt_duration (int): Target duration in hours (default 48)

        Returns:
            list: PV forecast values in Wh for each hour
        """
        api_key = self.config_source.get("api_key")
        # Get resource_ids from config_source (can be comma-separated)
        resource_ids = str(self.config_source.get("resource_id", "")).strip()

        if not api_key:
            return self._handle_interface_error(
                "config_error",
                "Solcast API key missing from pv_forecast_source configuration",
                pv_config_entry,
                "solcast",
            )

        if not resource_ids:
            return self._handle_interface_error(
                "config_error",
                "Resource ID(s) missing from pv_forecast_source for Solcast",
                pv_config_entry,
                "solcast",
            )

        # For now, use the first resource_id from the comma-separated list
        # If there are multiple IDs, they would need separate API calls and aggregation
        first_resource_id = resource_ids.split(",")[0].strip()

        # Solcast API endpoint for resource-based forecasts (free tier compatible)
        url = f"https://api.solcast.com.au/rooftop_sites/{first_resource_id}/forecasts"

        # Parameters for the API request
        params = {
            "hours": min(tgt_duration, 168),  # Solcast max is 168 hours (7 days)
            "format": "json",
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(
            "[PV-IF] Fetching PV forecast from Solcast API for resource: %s (hours: %d)",
            first_resource_id,
            params["hours"],
        )

        def request_func():
            response = requests.get(url, params=params, headers=headers, timeout=15)
            logger.debug(
                "[PV-IF] Solcast API response status: %d", response.status_code
            )
            if response.status_code == 429:
                raise requests.exceptions.RequestException("rate_limit")
            elif response.status_code == 403:
                raise requests.exceptions.RequestException("auth_error")
            elif response.status_code == 404:
                raise requests.exceptions.RequestException("not_found")
            elif response.status_code == 400:
                raise requests.exceptions.RequestException("bad_request")
            response.raise_for_status()
            return response

        def error_handler(error_type, exception):
            # Map custom error codes to messages
            error_map = {
                "rate_limit": "Solcast API rate limit exceeded",
                "auth_error": "Solcast API authentication failed (403) - check "
                + "API key and resource ID access.",
                "not_found": f"Solcast resource ID '{first_resource_id}' not found"+
                " - check resource ID",
                "bad_request": "Solcast API bad request - check parameters",
            }
            msg = error_map.get(str(exception), f"Solcast API error: {exception}")
            return self._handle_interface_error(
                error_type,
                msg,
                pv_config_entry,
                "solcast",
            )

        response = self._retry_request(request_func, error_handler)

        def json_func():
            return response.json()

        data = self._retry_request(json_func, error_handler)

        # Data processing
        try:
            forecasts = data.get("forecasts", [])
            if not forecasts:
                return self._handle_interface_error(
                    "no_valid_data",
                    "No forecast data received from Solcast API",
                    pv_config_entry,
                    "solcast",
                )

            # Get timezone-aware current time
            tz = pytz.timezone(self.time_zone)
            current_time = datetime.now(tz).replace(minute=0, second=0, microsecond=0)

            # Calculate midnight of today
            midnight_today = current_time.replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            # Create forecast array for target duration starting from midnight today
            forecast_hours = [
                midnight_today + timedelta(hours=i) for i in range(tgt_duration)
            ]
            pv_forecast = [0.0] * tgt_duration  # Initialize with zeros

            # Create hourly aggregation dictionary
            hourly_power = {}

            # Process Solcast data (30-minute intervals)
            for forecast_item in forecasts:
                try:
                    # Parse timestamp from Solcast (ISO format with timezone)
                    period_end = forecast_item.get("period_end", "")
                    if not period_end:
                        continue

                    # Convert to datetime - Solcast uses ISO format
                    if period_end.endswith("Z"):
                        forecast_time = datetime.fromisoformat(
                            period_end.replace("Z", "+00:00")
                        )
                    else:
                        forecast_time = datetime.fromisoformat(period_end)

                    # Convert to configured timezone
                    forecast_time = forecast_time.astimezone(tz)

                    # IMPORTANT: period_end is the END of a 30-minute period
                    # We need to map it to the hour it belongs to
                    # For example: 06:30 period_end belongs to hour 06:00-07:00
                    # So we subtract 30 minutes to get the start of the period
                    period_start = forecast_time - timedelta(minutes=30)

                    # Round down to the hour for aggregation
                    hour_key = period_start.replace(minute=0, second=0, microsecond=0)

                    # Get PV power estimate - Solcast provides kW values for the
                    # system capacity you configured
                    pv_estimate_kw = forecast_item.get("pv_estimate", 0)

                    # Convert kW (average power over 30 minutes) to energy (Wh) for 30-minute period
                    # Energy (Wh) = Power (kW) * Time (h)
                    pv_estimate_wh = pv_estimate_kw * 0.5 * 1000  # kW * h * 1000 = Wh

                    # Aggregate 30-minute values into hourly values
                    if hour_key in hourly_power:
                        hourly_power[hour_key] += pv_estimate_wh
                    else:
                        hourly_power[hour_key] = pv_estimate_wh

                except (ValueError, TypeError, AttributeError) as e:
                    logger.warning(
                        "[PV-IF] Error processing Solcast forecast item: %s", e
                    )
                    continue

            # Fill forecast array with aggregated hourly values
            for i, forecast_hour in enumerate(forecast_hours):
                if forecast_hour in hourly_power:
                    power_wh = hourly_power[forecast_hour]

                    # Apply inverter efficiency if configured
                    inverter_efficiency = pv_config_entry.get("inverterEfficiency", 1.0)
                    power_wh *= inverter_efficiency

                    pv_forecast[i] = round(power_wh, 1)

            # Clear any previous errors on success
            self.pv_forcast_request_error["error"] = None

            # Get inverter efficiency for logging
            inverter_efficiency = pv_config_entry.get("inverterEfficiency", 1.0)

            logger.debug(
                "[PV-IF] Solcast PV forecast for resource '%s' (inverterEfficiency: %s) "
                + "received %d forecast points,"
                + " first 12h (Wh): %s",
                first_resource_id,
                inverter_efficiency,
                len(forecasts),
                pv_forecast[:12],  # Log first 12 hours to avoid spam
            )

            if self.time_frame_base == 900:
                return self._convert_hourly_to_15min(pv_forecast)

            return pv_forecast

        except (ValueError, TypeError, AttributeError, KeyError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing Solcast forecast data: {e}",
                pv_config_entry,
                "solcast",
            )

    def __get_pv_forecast_victron_api(self, pv_config_entry, hours=48):
        """
        Fetches PV forecast from Victron VRM API.

        The Victron VRM API provides hourly solar yield forecasts in Wh.
        This method requires resource_id (VRM installation ID from pv_forecast_source.resource_id)
        and api_key (authentication token) configured in pv_forecast_source section.

        Args:
            pv_config_entry (dict): Configuration entry for PV system
            hours (int): Number of hours to forecast (default 48)

        Returns:
            list: PV forecast values in Wh for each time period (hourly or 15-min)
        """
        # Get VRM ID from pv_forecast_source.resource_id and API key from config
        vrm_id = str(self.config_source.get("resource_id", "")).strip()
        api_key = str(self.config_source.get("api_key", "")).strip()

        if not vrm_id:
            return self._handle_interface_error(
                "config_error",
                "Victron VRM ID (resource_id in pv_forecast_source) missing",
                pv_config_entry,
                "victron",
            )

        if not api_key:
            return self._handle_interface_error(
                "config_error",
                "Victron API key (api_key) missing from pv_forecast_source configuration",
                pv_config_entry,
                "victron",
            )

        # Construct API endpoint
        url = f"https://vrmapi.victronenergy.com/v2/installations/{vrm_id}/stats"

        # Get timezone-aware current time
        tz = pytz.timezone(self.time_zone)
        current_time = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
        midnight_today = current_time.replace(hour=0, minute=0, second=0, microsecond=0)

        # Calculate query start and end times in Unix seconds
        # Start from midnight today
        start_time = midnight_today
        end_time = start_time + timedelta(hours=hours)

        start_unix = int(start_time.timestamp())
        end_unix = int(end_time.timestamp())

        # Query parameters
        params = {
            "start": start_unix,
            "end": end_unix,
            "interval": "hours",
            "type": "forecast",
        }

        # Request headers with authorization
        headers = {
            "X-Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(
            "[PV-IF] Fetching PV forecast from Victron VRM API for installation: %s (hours: %d)",
            vrm_id,
            hours,
        )

        def request_and_parse():
            """
            Perform the GET request and parse the Victron JSON payload.
            """
            response = requests.get(url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Extract solar forecast from Victron response
            # Structure: records.solar_yield_forecast (top-level in API response)
            records = data.get("records", {})
            solar_forecast = records.get("solar_yield_forecast", [])

            logger.debug(
                "[PV-IF] Victron VRM API response received with %d forecast points",
                len(solar_forecast),
            )

            return solar_forecast

        def error_handler(error_type, exception):
            return self._handle_interface_error(
                error_type,
                f"Victron VRM API error: {exception}",
                pv_config_entry,
                "victron",
            )

        # Fetch and parse the response
        solar_forecast = self._retry_request(request_and_parse, error_handler)
        if not solar_forecast:
            return self._handle_interface_error(
                "no_valid_data",
                "No valid solar forecast data found in Victron VRM API.",
                pv_config_entry,
                "victron",
            )

        if not isinstance(solar_forecast, list):
            return self._handle_interface_error(
                "invalid_data",
                "Victron VRM solar forecast is not a list.",
                pv_config_entry,
                "victron",
            )

        try:
            # Initialize forecast array
            pv_forecast = [0.0] * hours

            # Create hour time references for alignment
            forecast_hours = [midnight_today + timedelta(hours=i) for i in range(hours)]

            # Parse Victron forecast data
            # Format: [[unix_timestamp_ms, wh_value], [unix_timestamp_ms, wh_value], ...]
            for forecast_point in solar_forecast:
                if (
                    not isinstance(forecast_point, (list, tuple))
                    or len(forecast_point) < 2
                ):
                    logger.warning(
                        "[PV-IF] Invalid Victron forecast point format: %s",
                        forecast_point,
                    )
                    continue

                try:
                    # Extract timestamp (in milliseconds) and energy value (in Wh)
                    timestamp_ms = forecast_point[0]
                    wh_value = forecast_point[1]

                    # Convert millisecond timestamp to datetime
                    timestamp_seconds = timestamp_ms / 1000
                    forecast_time = datetime.fromtimestamp(
                        timestamp_seconds, tz=pytz.UTC
                    )
                    forecast_time = forecast_time.astimezone(tz)

                    # Find which hour this forecast belongs to
                    hour_index = None
                    for idx, hour_ref in enumerate(forecast_hours):
                        if (
                            forecast_time.year == hour_ref.year
                            and forecast_time.month == hour_ref.month
                            and forecast_time.day == hour_ref.day
                            and forecast_time.hour == hour_ref.hour
                        ):
                            hour_index = idx
                            break

                    if hour_index is not None:
                        # Victron provides Wh values directly for the period
                        pv_forecast[hour_index] = float(wh_value)

                except (ValueError, TypeError, IndexError) as e:
                    logger.warning(
                        "[PV-IF] Error processing Victron forecast point: %s", e
                    )
                    continue

            # Handle 15-min time frame if configured
            if self.time_frame_base == 900:
                # Convert 48 hourly values to 192 15-min values
                pv_forecast = self._convert_hourly_to_15min(pv_forecast)

            # Round values to 1 decimal place
            pv_forecast = [round(val, 1) for val in pv_forecast]

            # Clear any previous errors on success
            self.pv_forcast_request_error["error"] = None

            logger.debug(
                "[PV-IF] Victron VRM PV forecast received with %d values, first 12h (Wh): %s",
                len(pv_forecast),
                pv_forecast[: min(12, len(pv_forecast))],
            )

            return pv_forecast

        except (ValueError, TypeError, AttributeError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing Victron VRM forecast data: {e}",
                pv_config_entry,
                "victron",
            )

    def test_output(self):
        """
        Test method to print the current PV and temperature forecasts.
        """
        self.config_source["source"] = "akkudoktor"
        pv_forcast_array1 = self.get_summarized_pv_forecast()
        # print("[PV-IF] PV forecast (Akkudoktor):", pv_forcast_array1)
        self.config_source["source"] = "openmeteo"
        pv_forcast_array2 = self.get_summarized_pv_forecast()
        # self.config_source["source"] = "forecast_solar"
        # pv_forcast_array3 = self.get_summarized_pv_forecast()

        # print out to csv file - first column is the hour, second column is the value
        # Set start to today at midnight in the configured timezone
        tz = pytz.timezone(self.time_zone)
        start_midnight = datetime.now(tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        df = pd.DataFrame(
            {
                "Hour": pd.date_range(
                    start=start_midnight,
                    periods=48,
                    freq="h",
                ),
                "Akkudoktor": pv_forcast_array1,
                "OpenMeteo": pv_forcast_array2,
                # "ForecastSolar": pv_forcast_array3,
            }
        )
        df.set_index("Hour", inplace=True)
        # Save as HTML with right-aligned numbers and 1px border
        styles = [
            dict(selector="th, td", props=[("text-align", "right")]),
            dict(selector="th.index_name", props=[("text-align", "left")]),
            dict(selector="th.blank", props=[("text-align", "left")]),
            dict(
                selector="table",
                props=[("border-width", "1px"), ("border-style", "solid")],
            ),
        ]
        df.style.format("{:.1f}").set_table_styles(styles).to_html(
            "pv_forecast_test_output_2.html", border=1
        )
        logger.info(
            "[PV-IF] PV forecast test output saved to pv_forecast_test_output_2.csv"
        )

    # Add these helper functions to replace pvlib functionality
    def _solar_position(self, times, latitude, longitude):
        """
        Calculate solar position (zenith and azimuth) for given times and location.
        Simplified version of pvlib.solarposition.get_solarposition
        """
        lat_rad = math.radians(latitude)
        results = []

        for t in times:
            # Convert to Julian day number
            a = (14 - t.month) // 12
            y = t.year - a
            m = t.month + 12 * a - 3
            jdn = (
                t.day
                + (153 * m + 2) // 5
                + 365 * y
                + y // 4
                - y // 100
                + y // 400
                - 32045
            )

            # Add fraction of day
            hour_fraction = (t.hour + t.minute / 60 + t.second / 3600) / 24
            jd = jdn + hour_fraction - 0.5

            # Number of days since J2000.0
            n = jd - 2451545.0

            # Mean longitude of sun
            long_of_sun = (280.460 + 0.9856474 * n) % 360

            # Mean anomaly of sun
            g = math.radians((357.528 + 0.9856003 * n) % 360)

            # Ecliptic longitude of sun
            lambda_sun = math.radians(
                long_of_sun + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)
            )

            # Obliquity of ecliptic
            epsilon = math.radians(23.439 - 0.0000004 * n)

            # Right ascension and declination
            alpha = math.atan2(
                math.cos(epsilon) * math.sin(lambda_sun), math.cos(lambda_sun)
            )
            delta = math.asin(math.sin(epsilon) * math.sin(lambda_sun))

            # Greenwich mean sidereal time
            gmst = (18.697375 + 24.06570982441908 * n) % 24

            # Local sidereal time
            lst = gmst + longitude / 15

            # Hour angle
            h = math.radians(15 * (lst - math.degrees(alpha) / 15))

            # Solar zenith and azimuth
            sin_alt = math.sin(lat_rad) * math.sin(delta) + math.cos(
                lat_rad
            ) * math.cos(delta) * math.cos(h)
            altitude = math.asin(max(-1, min(1, sin_alt)))
            zenith = math.degrees(math.pi / 2 - altitude)

            cos_az = (math.sin(delta) - math.sin(altitude) * math.sin(lat_rad)) / (
                math.cos(altitude) * math.cos(lat_rad)
            )
            azimuth = math.degrees(math.acos(max(-1, min(1, cos_az))))

            if math.sin(h) > 0:
                azimuth = 360 - azimuth

            results.append({"apparent_zenith": zenith, "azimuth": azimuth})

        return results

    def _angle_of_incidence(
        self, surface_tilt, surface_azimuth, solar_zenith, solar_azimuth
    ):
        """
        Calculate angle of incidence between sun and tilted surface.
        Simplified version of pvlib.irradiance.aoi
        """
        # Convert to radians
        surf_tilt_rad = math.radians(surface_tilt)
        surf_az_rad = math.radians(surface_azimuth)
        sun_zen_rad = math.radians(solar_zenith)
        sun_az_rad = math.radians(solar_azimuth)

        # Calculate angle of incidence
        cos_aoi = math.sin(sun_zen_rad) * math.sin(surf_tilt_rad) * math.cos(
            sun_az_rad - surf_az_rad
        ) + math.cos(sun_zen_rad) * math.cos(surf_tilt_rad)

        # Ensure value is within valid range for acos
        cos_aoi = max(-1, min(1, cos_aoi))
        aoi = math.degrees(math.acos(cos_aoi))

        return aoi

    def _retry_request(self, request_func, error_handler, max_retries=3, delay=1):
        """
        Centralized retry logic for API requests.

        Args:
            request_func (callable): Function that performs the request and returns the result.
            error_handler (callable): Function to call on final failure.
            max_retries (int): Number of retries before error handler is called.
            delay (int): Delay in seconds between retries.

        Returns:
            The result of request_func, or error_handler on failure.
        """
        for attempt in range(max_retries):
            try:
                return request_func()
            except requests.exceptions.Timeout as e:
                if attempt == max_retries - 1:
                    return error_handler("timeout", e)
            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    return error_handler("request_failed", e)
            except (ValueError, TypeError) as e:
                if attempt == max_retries - 1:
                    return error_handler("invalid_json", e)
            except (KeyError, AttributeError) as e:
                if attempt == max_retries - 1:
                    return error_handler("parsing_error", e)
            time.sleep(delay)

    def _handle_interface_error(
        self, error_type, message, pv_config_entry, source="unknown", target="power"
    ):
        """
        Centralized error handling for all API errors.
        Uses last successful forecast as fallback if available.
        Similar to PriceInterface.last_successful_prices mechanism.

        `target` selects which cache/counter pair to use ("power" or
        "temperature") so a failed temperature request never falls back to
        the PV power cache (and vice versa).
        """
        # A temperature failure that the cache absorbs is noise, not an incident: the
        # forecast handed to the optimizer is still real degrees.  Escalate only once the
        # cache cannot cover it any more.
        cache_covers_it = (
            target == "temperature"
            and self.consecutive_temp_failures + 1 <= self.max_failures
            and len(self.last_successful_temp_forecast) > 0
        )
        (logger.warning if cache_covers_it else logger.error)("[PV-IF] %s", message)
        error_slot = (
            self.temp_forecast_request_error
            if target == "temperature"
            else self.pv_forcast_request_error
        )
        error_slot.update(
            {
                "error": error_type,
                "timestamp": datetime.now().isoformat(),
                "message": message,
                "config_entry": pv_config_entry,
                "source": source,
            }
        )

        if target == "temperature":
            self.consecutive_temp_failures += 1
            failures = self.consecutive_temp_failures
            last_successful = self.last_successful_temp_forecast
        else:
            self.consecutive_failures += 1
            failures = self.consecutive_failures
            last_successful = self.last_successful_pv_forecast

        # Fallback strategy: Use last successful forecast if available
        # and within failure threshold
        if failures <= self.max_failures and len(last_successful) > 0:
            logger.warning(
                "[PV-IF] No %s forecast retrieved (failure %d/%d)."
                " Using last successful forecast.",
                target,
                failures,
                self.max_failures,
            )
            return last_successful

        # If max failures exceeded or no cache available, return empty array
        # (let caller handle default generation)
        if len(last_successful) == 0:
            logger.warning(
                "[PV-IF] No %s forecast available and no cache - returning empty array",
                target,
            )

        # Log detailed recovery diagnostics for troubleshooting
        self._log_error_diagnostics(error_type, source, target)

        return []

    def _log_error_diagnostics(self, error_type, source, target="power"):
        """
        Log detailed error diagnostics including available sources and recovery hints.
        Helps users troubleshoot and fix configuration issues faster.
        """
        available_sources = [
            "akkudoktor",
            "openmeteo",
            "openmeteo_local",
            "forecast_solar",
            "solcast",
            "victron",
            "evcc",
            "timeseries",
            "default",
        ]
        current_source = self.config_source.get("source", "unknown")

        failures = (
            self.consecutive_temp_failures
            if target == "temperature"
            else self.consecutive_failures
        )
        if failures >= self.max_failures:
            logger.error(
                "[PV-IF] Maximum %s failures reached (%d) - "
                "please check configuration in Settings > PV Source",
                target,
                failures,
            )

        if source == "timeseries":
            data_url = self.config_source.get("data_url", "").strip()
            use_ha = self.config_source.get("use_ha_central_data_source", False)

            if error_type == "config_error" and not data_url and not use_ha:
                logger.error(
                    "[PV-IF] Timeseries requires either data_url or use_ha_central_data_source - "
                    "at least one must be configured"
                )
            elif error_type == "timeout":
                logger.error(
                    "[PV-IF] Timeseries endpoint unreachable: %s - "
                    "check network connectivity and endpoint availability",
                    data_url,
                )
            elif error_type in ("request_failed", "invalid_json", "parsing_error"):
                logger.error(
                    "[PV-IF] Timeseries endpoint returned unexpected data - "
                    "verify data_url and data_path in Settings > PV Source"
                )

        if source == "forecast_solar" and error_type == "rate_limit":
            logger.error(
                "[PV-IF] Forecast.Solar quota is metered per IP address, or per API key"
                " once one is set. The public tier allows 12 requests/hour and EOS"
                " Connect spends one per PV installation per cycle. Add an API key in"
                " Settings > PV Source to raise the quota, or reduce the number of"
                " installations. Requests stay paused until the reported retry time"
                " passes - retrying sooner only restarts the block."
            )

        logger.debug(
            "[PV-IF] Available PV sources: %s (current: %s, consecutive_failures: %d/%d)",
            ", ".join(available_sources),
            current_source,
            self.consecutive_failures,
            self.max_failures,
        )

    def _convert_hourly_to_15min(self, hourly_values):
        """
        Converts a list of hourly Wh values to 15-min interval Wh values by dividing
        each value by 4.

        Args:
            hourly_values (list): List of Wh values at hourly intervals.

        Returns:
            list: List of Wh values at 15-min intervals.
        """
        if not isinstance(hourly_values, list):
            raise TypeError("Input must be a list of hourly values.")
        return [round(value / 4.0, 1) for value in hourly_values for _ in range(4)]
