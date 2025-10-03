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

from datetime import datetime, timedelta
import threading
import logging
import time
import asyncio
import math
import aiohttp
import pytz
import requests
import pandas as pd
import numpy as np
import sys
from open_meteo_solar_forecast import OpenMeteoSolarForecast

logger = logging.getLogger("__main__")
logger.info("[PV-IF] loading module ")

EOS_API_GET_PV_FORECAST = "https://api.akkudoktor.net/forecast"


class PvInterface:
    """
    Interface for fetching and summarizing PV (photovoltaic) and temperature forecasts.
    Handles configuration validation, periodic updates, and default fallbacks.
    """

    def __init__(
        self,
        config_source,
        config,
        config_special,
        timezone="UTC",
    ):
        self.config = config
        self.time_zone = timezone
        self.config_source = config_source
        self.config_special = config_special
        logger.debug(
            "[PV-IF] Initializing with 1st source: %s",
            self.config_source.get("source", "akkudoktor"),
            # self.config_source.get("second_source", "openmeteo"),
        )

        self.pv_forcast_array = []
        self.pv_forcast_request_error = {
            "error": None,
            "timestamp": None,
            "message": None,
            "config_entry": None,
            "source": None,
        }
        self.temp_forecast_array = [15] * 48

        self._update_thread = None
        self._stop_event = threading.Event()
        # Adjust update interval based on provider
        if self.config_source.get("source") == "solcast":
            self.update_interval = (
                2.5 * 60 * 60
            )  # 2.5 hours (9.6 calls/day - under the 10 limit)
            logger.info("[PV-IF] Using extended update interval for Solcast: 2.5 hours")
        else:
            self.update_interval = 15 * 60  # Standard 15 minutes

        try:
            self.__check_config()  # Validate configuration parameters
            self.configuration_valid = True
            logger.info("[PV-IF] Configuration validation successful")
        except ValueError as e:
            logger.error("[PV-IF] PV Interface configuration error: %s", str(e))
            logger.error("[PV-IF] We have to exit now ...")
            sys.exit(1)  # Exit if configuration is invalid

        logger.info("[PV-IF] Initialized")
        self.__start_update_service()  # Start the background thread for periodic updates

    def __check_config(self):
        """
        Checks the configuration for required parameters.

        This function checks if the necessary parameters are present in the configuration.
        If any required parameter is missing, it raises a ValueError.

        Raises:
            ValueError: If any required parameter is missing from the configuration.
        """
        if not len(self.config) > 0:
            logger.error("[PV-IF] Initialize - No pv entries found")
            return

        logger.debug("[PV-IF] Initialize - pv entries found: %s", len(self.config))

        for config_entry in self.config:
            entry_name = config_entry.get("name", "unnamed")
            logger.debug("[PV-IF] Initialize - config entry name: %s", entry_name)

            # Check for Solcast-specific requirements
            if self.config_source.get("source") == "solcast":
                # Check API key
                if not self.config_source.get("api_key", "").strip():
                    logger.error(
                        "[PV-IF] Solcast API key missing in pv_forecast_source section"
                    )
                    logger.error(
                        '[PV-IF] Please add: api_key: "your_solcast_api_key" in config.yaml'
                    )
                    raise ValueError(
                        "[PV-IF] Solcast API key required - see CONFIG_README.md for setup instructions"
                    )

                # Check resource_id
                if not config_entry.get("resource_id", "").strip():
                    logger.error(
                        "[PV-IF] Resource ID missing for '%s' - required for Solcast",
                        entry_name,
                    )
                    logger.error(
                        '[PV-IF] Please add: resource_id: "your_resource_id" in config.yaml'
                    )
                    raise ValueError(
                        f"[PV-IF] Solcast resource_id required for '{entry_name}' - see CONFIG_README.md for setup instructions"
                    )

                logger.debug("[PV-IF] Solcast config validated for '%s'", entry_name)
            else:
                # Standard parameter validation for other sources
                missing = []
                if config_entry.get("lat") is None:
                    missing.append("lat")
                if config_entry.get("lon") is None:
                    missing.append("lon")
                if config_entry.get("azimuth") is None:
                    missing.append("azimuth")
                if config_entry.get("tilt") is None:
                    missing.append("tilt")

                if missing:
                    logger.error(
                        "[PV-IF] Missing parameters for '%s': %s",
                        entry_name,
                        ", ".join(missing),
                    )
                    raise ValueError(
                        f"[PV-IF] Missing required parameters for '{entry_name}': {', '.join(missing)}"
                    )

            # Common parameters for all sources
            missing_common = []
            if config_entry.get("power") is None:
                missing_common.append("power")
            if config_entry.get("powerInverter") is None:
                missing_common.append("powerInverter")
            if config_entry.get("inverterEfficiency") is None:
                missing_common.append("inverterEfficiency")

            if missing_common:
                logger.error(
                    "[PV-IF] Missing common parameters for '%s': %s",
                    entry_name,
                    ", ".join(missing_common),
                )
                raise ValueError(
                    f"[PV-IF] Missing required parameters for '{entry_name}': {', '.join(missing_common)}"
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
            # Fetch the PV forecast data
            pv_forcast_array = self.get_summarized_pv_forecast(48)
            if not self.pv_forcast_request_error["error"]:
                logger.debug("[PV-IF] PV forecast updated successfully")
                self.pv_forcast_array = pv_forcast_array
            elif self.pv_forcast_array == []:
                # If there was an error and no forecast was fetched, use default values
                logger.warning(
                    "[PV-IF] Using default PV forecast due to previous error: %s",
                    self.pv_forcast_request_error["message"],
                )
                self.pv_forcast_array = self.__get_default_pv_forcast(
                    self.config[0]["power"]
                )
            else:
                # If there was an error but we have a previous forecast, log it
                logger.warning(
                    "[PV-IF] Using previous PV forecast due to error: %s",
                    self.pv_forcast_request_error["message"],
                )
            # special temp forecast if pv config is not given in detail
            if self.config and self.config[0]:
                temp_result = self.__get_pv_forecast_akkudoktor_api(
                    tgt_value="temperature",
                    pv_config_entry=self.config[0],
                    tgt_duration=48,
                )
                if not temp_result:  # If empty array or None due to API error
                    logger.warning(
                        "[PV-IF] Temperature forecast API failed - using default"
                        + " temperature forecast"
                    )
                    self.temp_forecast_array = self.__get_default_temperature_forecast()
                else:
                    self.temp_forecast_array = temp_result
                    # logger.debug(
                    #     "[PV-IF] Temperature forecast updated with %d values",
                    #     len(temp_result),
                    # )
            else:
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

    def get_current_pv_forecast(self):
        """
        Returns the current photovoltaic (PV) forecast array.

        Returns:
            list or np.ndarray: The current PV forecast values stored in pv_forcast_array.
        """
        # logger.debug(
        #     "[PV-IF] Returning current PV forecast: %s", self.pv_forcast_array
        # )
        return self.pv_forcast_array

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
        Creates a forecast request URL for the EOS server.
        """
        horizon_string = ""
        if pv_config_entry["horizon"] != "":
            horizon_string = "&horizont=" + str(pv_config_entry["horizon"])
        return (
            EOS_API_GET_PV_FORECAST
            + "?lat="
            + str(pv_config_entry["lat"])
            + "&lon="
            + str(pv_config_entry["lon"])
            + "&azimuth="
            + str(pv_config_entry["azimuth"])
            + "&tilt="
            + str(pv_config_entry["tilt"])
            + "&power="
            + str(pv_config_entry["power"])
            + "&powerInverter="
            + str(pv_config_entry["powerInverter"])
            + "&inverterEfficiency="
            + str(pv_config_entry["inverterEfficiency"])
            + "&timezone="
            + str(self.time_zone)
            + horizon_string
        )

    def __get_default_pv_forcast(self, pv_power):
        """
        Creates a default PV forecast with fixed values based on max power.
        """
        # Create a 24-hour default forecast
        forecast_24h = [
            pv_power * 0.0,  # 0% at 00:00
            pv_power * 0.0,  # 0% at 01:00
            pv_power * 0.0,  # 0% at 02:00
            pv_power * 0.0,  # 0% at 03:00
            pv_power * 0.0,  # 0% at 04:00
            pv_power * 0.0,  # 0% at 05:00
            pv_power * 0.1,  # 30% at 06:00
            pv_power * 0.2,  # 50% at 07:00
            pv_power * 0.3,  # 60% at 08:00
            pv_power * 0.4,  # 70% at 09:00
            pv_power * 0.5,  # 90% at 10:00
            pv_power * 0.6,  # 80% at 11:00
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
            pv_power * 0.0,  # 0% at 23:00,
        ]
        # Repeat for the next day (48 hours total)
        # logger.debug("[PV-IF] Using default PV forecast with %s W max power", pv_power)
        return forecast_24h * 2

    def __get_default_temperature_forecast(self):
        """
        Creates a default temperature forecast with fixed values.
        The values are set to 20 degrees Celsius for the entire day.
        """
        # Create a 24-hour default temperature forecast
        forecast_24h = [15.0] * 24  # 15 degrees Celsius for each hour
        logger.debug("[PV-IF] Using default temperature forecast with 15 degrees")
        return forecast_24h * 2  # Repeat for the next day (48 hours total)

    def get_pv_forecast(self, config_entry, tgt_duration=24):
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
              "solcast", "default".
            - Logs a warning if the default source is used.
            - Logs an error and falls back to the default forecast if no valid
              source is configured.
        """
        if self.config_source.get("source") == "akkudoktor":
            return self.__get_pv_forecast_akkudoktor_api(
                "power", config_entry, tgt_duration
            )
        elif self.config_source.get("source") == "openmeteo":
            # return self.__get_pv_forecast_openmeteo_api(config_entry, tgt_duration)
            return self.__get_pv_forecast_openmeteo_lib(config_entry)
        elif self.config_source.get("source") == "openmeteo_local":
            return self.__get_pv_forecast_openmeteo_api(config_entry, tgt_duration)
        elif self.config_source.get("source") == "forecast_solar":
            return self.__get_pv_forecast_forecast_solar_api(config_entry)
        elif self.config_source.get("source") == "evcc":
            return self.__get_pv_forecast_evcc_api(config_entry, tgt_duration)
        elif self.config_source.get("source") == "solcast":
            return self.__get_pv_forecast_solcast_api(config_entry, tgt_duration)
        elif self.config_source.get("source") == "default":
            logger.warning("[PV-IF] Using default PV forecast source")
            return self.__get_default_pv_forcast(config_entry["power"])
        else:
            logger.error("[PV-IF] No valid source configured for PV forecast")
            return self.__get_default_pv_forcast(config_entry["power"])

    def get_summarized_pv_forecast(self, tgt_duration=24):
        """
        requesting pv forecast freach config entry and summarize the values
        """
        forecast_values = []
        if self.config_special and self.config_source.get("source") == "evcc":
            logger.debug("[PV-IF] fetching forecast for evcc config")
            forecast = self.get_pv_forecast("evcc_config", tgt_duration)
            forecast_values = forecast
        else:
            for config_entry in self.config:
                logger.debug("[PV-IF] fetching forecast for '%s'", config_entry["name"])
                forecast = self.get_pv_forecast(config_entry, tgt_duration)
                # print("values for " + config_entry+ " -> ")
                # print(forecast)
                if not forecast_values:
                    forecast_values = forecast
                else:
                    forecast_values = [x + y for x, y in zip(forecast_values, forecast)]
        logger.debug("[PV-IF] Summarized PV forecast values: %s", forecast_values)
        return forecast_values

    def __get_pv_forecast_akkudoktor_api(
        self, tgt_value="power", pv_config_entry=None, tgt_duration=24
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
            )

        forecast_request_payload = self.__create_forecast_request(pv_config_entry)

        # Network request handling
        try:
            response = requests.get(forecast_request_payload, timeout=5)
            response.raise_for_status()
            day_values = response.json()
            day_values = day_values["values"]
        except requests.exceptions.Timeout:
            return self._handle_interface_error(
                "timeout",
                f"Akkudoktor API request timed out for {tgt_value}.",
                pv_config_entry,
                "akkudoktor",
            )
        except requests.exceptions.RequestException as e:
            return self._handle_interface_error(
                "request_failed",
                f"Akkudtoktor API request failed for {tgt_value}: {e}",
                pv_config_entry,
                "akkudoktor",
            )
        except (ValueError, TypeError) as e:
            return self._handle_interface_error(
                "invalid_json",
                f"Invalid JSON response for {tgt_value}: {e}",
                pv_config_entry,
                "akkudoktor",
            )
        except (KeyError, AttributeError) as e:
            return self._handle_interface_error(
                "parsing_error",
                f"Error parsing response structure for {tgt_value}: {e}",
                pv_config_entry,
                "akkudoktor",
            )

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

            # workaround for wrong time points in the forecast from akkudoktor
            # remove first entry and append 0 to the end
            if forecast_values:
                forecast_values.pop(0)
                forecast_values.append(0)

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
                else:
                    forecast_values = [0] * tgt_duration
                logger.debug(
                    "[PV-IF][akkudoktor] Day of time change - values extended to %s for %s",
                    tgt_duration,
                    pv_config_entry.get("name", "unknown"),
                )

            # Clear any previous errors on success
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

            return forecast_values

        except (ValueError, TypeError, AttributeError, KeyError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing {tgt_value} forecast data: {e}",
                pv_config_entry,
                "akkudoktor",
            )

    def __get_horizon_elevation(self, sun_azimuth, horizon):

        if not horizon or len(horizon) == 0:
            horizon = [0] * 36

        # Normalize horizon string to a list of integers (handle '50t0.4' as 50)
        if isinstance(horizon, str):
            horizon = [
                int(float(x.split("t")[0])) if "t" in x else int(float(x))
                for x in horizon.split(",")
                if x.strip()
            ]
        else:
            horizon = [int(float(x)) for x in horizon]
        # Expand horizon to 36 values by linear interpolation if needed
        if len(horizon) != 36:
            # Interpolate to 36 values (full circle)
            x_old = np.linspace(0, 360, num=len(horizon), endpoint=False)
            x_new = np.linspace(0, 360, num=36, endpoint=False)
            horizon = np.interp(x_new, x_old, horizon).tolist()
        # logger.debug(
        #     "[PV-IF] Horizon elevation values normalized to 36 values: %s",
        #     horizon
        # )

        idx = int((sun_azimuth / 10))  # Convert azimuth to index (0-35)
        # logger.debug(
        #     "[PV-IF] azimuth %s° to horizon index %s - elevation: %s°",
        #     round(sun_azimuth,2),
        #     idx,
        #     horizon[idx]
        # )
        return horizon[idx]

    def __get_pv_forecast_openmeteo_api(self, pv_config_entry, hours=48):
        """
        Fetches weather data from Open-Meteo and estimates PV forecast using
        panel tilt and azimuth from pv_config_entry.
        """
        latitude = pv_config_entry["lat"]
        longitude = pv_config_entry["lon"]
        tilt = pv_config_entry.get("tilt", 30)  # degrees
        azimuth = pv_config_entry.get("azimuth", 180)  # degrees (180=south)
        installed_power_watt = pv_config_entry.get(
            "power", 200
        )  # value in config is in watts
        horizon = pv_config_entry.get("horizon", [0] * 36)  # default: no shading
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
            horizon,
        )

        # Fetch weather data
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={latitude}&longitude={longitude}"
            f"&hourly=shortwave_radiation,cloudcover"
            f"&forecast_days={int(np.ceil(hours/24))}"
            f"&timezone={timezone}"
        )
        response = requests.get(url, timeout=5)
        data = response.json()

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
            horizon_elev = self.__get_horizon_elevation(sun_az, horizon)
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
        logger.debug(
            "[PV-IF] Open-Meteo PV forecast for '%s' (Wh): %s",
            pv_config_entry["name"],
            pv_forecast,
        )

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
                azimuth=pv_config_entry.get("azimuth", 180),
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

            # Clear any previous errors on success
            self.pv_forcast_request_error["error"] = None

            logger.debug(
                "[PV-IF] OpenMeteo Lib PV forecast (Wh) (length: %s): %s",
                len(pv_forecast),
                pv_forecast,
            )
            return pv_forecast

        except (ValueError, TypeError, AttributeError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing OpenMeteo forecast data: {e}",
                pv_config_entry,
                "openmeteo_lib",
            )

    def __get_pv_forecast_forecast_solar_api(self, pv_config_entry):
        """
        Fetches PV forecast from Forecast.Solar API.
        """
        latitude = pv_config_entry["lat"]
        longitude = pv_config_entry["lon"]
        tilt = pv_config_entry.get("tilt", 30)
        azimuth = pv_config_entry.get("azimuth", 180)
        # Convert to kW for API and round to 4 decimal places
        installed_power_watt = round(pv_config_entry.get("power", 200) / 1000, 4)
        horizon = ""
        if pv_config_entry.get("horizon", None) is not None:
            horizon = pv_config_entry.get("horizon", "")
            if horizon:
                # Convert horizon string to a list of floats
                # Handle entries like '50t0.4' by taking only the value before 't'
                horizon = [
                    float(x.split("t")[0]) if "t" in x else float(x)
                    for x in horizon.split(",")
                    if x.strip()
                ]
                # Ensure the list has 24 values, repeating if necessary
                horizon = (horizon * (24 // len(horizon) + 1))[:24]
            else:
                logger.debug(
                    "[PV-IF] No horizon values provided, using default empty list"
                )

        url = (
            f"https://api.forecast.solar/estimate/"
            f"{latitude}/{longitude}/{tilt}/{azimuth}/{installed_power_watt}"
            f"?horizon={','.join(map(str, horizon))}"
        )
        logger.debug("[PV-IF] Fetching PV forecast from Forecast.Solar API: %s", url)

        # Network request handling
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            return self._handle_interface_error(
                "timeout",
                "Forecast.Solar API request timed out.",
                pv_config_entry,
                "forecast_solar",
            )
        except requests.exceptions.RequestException as e:
            return self._handle_interface_error(
                "request_failed",
                f"Forecast.Solar API request failed: {e}",
                pv_config_entry,
                "forecast_solar",
            )

        # JSON parsing and data extraction
        try:
            data = response.json()
            watt_hours_period = data.get("result", {}).get("watt_hours_period", {})
        except (ValueError, TypeError) as e:
            return self._handle_interface_error(
                "invalid_json",
                f"Invalid JSON response: {e}",
                pv_config_entry,
                "forecast_solar",
            )
        except (KeyError, AttributeError) as e:
            return self._handle_interface_error(
                "parsing_error",
                f"Error parsing forecast data: {e}",
                pv_config_entry,
                "forecast_solar",
            )

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
            forecast_wh = []
            for h in hours_list:
                # Use value if exact hour exists, else 0
                forecast_wh.append(lookup.get(h, 0))

            # Clear any previous errors on success
            self.pv_forcast_request_error["error"] = None

            pv_forecast = forecast_wh
            return pv_forecast

        except (ValueError, TypeError, AttributeError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing forecast data: {e}",
                pv_config_entry,
                "forecast_solar",
            )

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

        # Network request handling
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
        except requests.exceptions.Timeout:
            return self._handle_interface_error(
                "timeout", "EVCC API request timed out.", pv_config_entry, "evcc"
            )
        except requests.exceptions.RequestException as e:
            return self._handle_interface_error(
                "request_failed", f"EVCC API request failed: {e}", pv_config_entry
            )

        # JSON parsing and data extraction
        try:
            data = response.json()
            solar_forecast_all = data.get("forecast", {}).get("solar", {})
            solar_forecast_scale = solar_forecast_all.get("scale", "unknown")
            solar_forecast = solar_forecast_all.get("timeseries", [])

            logger.debug(
                "[PV-IF] EVCC API solar forecast received with scale: %s",
                solar_forecast_scale,
            )

        except (ValueError, TypeError) as e:
            return self._handle_interface_error(
                "invalid_json", f"Invalid JSON response: {e}", pv_config_entry
            )
        except (KeyError, AttributeError) as e:
            return self._handle_interface_error(
                "parsing_error", f"Error parsing forecast data: {e}", pv_config_entry
            )

        # Data validation and processing
        if not solar_forecast or not isinstance(solar_forecast, list):
            return self._handle_interface_error(
                "no_valid_data",
                "No valid solar forecast data found in EVCC API.",
                pv_config_entry,
            )

        try:
            # Get timezone-aware current time
            tz = pytz.timezone(self.time_zone)
            current_time = datetime.now(tz).replace(minute=0, second=0, microsecond=0)

            # Calculate midnight of today
            midnight_today = current_time.replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            # Create forecast array for 48 hours starting from midnight today
            forecast_hours = [midnight_today + timedelta(hours=i) for i in range(hours)]
            pv_forecast = [0.0] * hours  # Initialize with zeros

            # Create lookup dictionary from API data
            forecast_lookup = {}
            for item in solar_forecast:
                try:
                    # Parse timestamp from API
                    ts_str = item.get("ts", "")
                    if ts_str:
                        # Parse ISO format timestamp with timezone
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        # Convert to configured timezone
                        ts = ts.astimezone(tz)
                        # Round down to hour
                        ts = ts.replace(minute=0, second=0, microsecond=0)
                        forecast_lookup[ts] = item.get("val", 0)
                except (ValueError, TypeError) as e:
                    logger.warning("[PV-IF] Error parsing timestamp %s: %s", ts_str, e)
                    continue

            # Fill forecast array with values from API or keep zeros for missing hours
            for i, hour in enumerate(forecast_hours):
                if hour in forecast_lookup:
                    pv_forecast[i] = forecast_lookup[hour]
                # else: keep the initialized zero value

            # Apply scaling factor
            try:
                scale_factor = float(solar_forecast_scale)
            except (TypeError, ValueError):
                scale_factor = 1.0

            if scale_factor <= 0:
                logger.debug(
                    "[PV-IF] EVCC PV forecast scale factor invalid (%s) - using 1.0",
                    scale_factor,
                )
                scale_factor = 1.0

            pv_forecast = [val * scale_factor for val in pv_forecast]

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
            )

    def __get_pv_forecast_solcast_api(self, pv_config_entry, tgt_duration=48):
        """
        Fetches PV forecast from Solcast API using resource ID endpoint.

        Args:
            pv_config_entry (dict): Configuration entry containing resource_id
            tgt_duration (int): Target duration in hours (default 48)

        Returns:
            list: PV forecast values in Wh for each hour
        """
        api_key = self.config_source.get("api_key")
        resource_id = pv_config_entry.get("resource_id")

        if not api_key:
            return self._handle_interface_error(
                "config_error",
                "Solcast API key missing from pv_forecast_source configuration",
                pv_config_entry,
                "solcast",
            )

        if not resource_id:
            return self._handle_interface_error(
                "config_error",
                "Resource ID missing from PV configuration for Solcast",
                pv_config_entry,
                "solcast",
            )

        # Solcast API endpoint for resource-based forecasts (free tier compatible)
        url = f"https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts"

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
            resource_id,
            params["hours"],
        )

        # Network request handling
        try:
            response = requests.get(url, params=params, headers=headers, timeout=15)

            # Enhanced error logging for debugging
            logger.debug(
                "[PV-IF] Solcast API response status: %d", response.status_code
            )

            # Check for API-specific error responses
            if response.status_code == 429:
                return self._handle_interface_error(
                    "rate_limit",
                    "Solcast API rate limit exceeded",
                    pv_config_entry,
                    "solcast",
                )
            elif response.status_code == 403:
                logger.error(
                    "[PV-IF] Solcast API 403 Forbidden - Response: %s",
                    response.text[:200],
                )
                return self._handle_interface_error(
                    "auth_error",
                    "Solcast API authentication failed (403) - check API key and "
                    + "resource ID access. Response: {response.text[:100]}",
                    pv_config_entry,
                    "solcast",
                )
            elif response.status_code == 404:
                return self._handle_interface_error(
                    "not_found",
                    f"Solcast resource ID '{resource_id}' not found - check resource ID",
                    pv_config_entry,
                    "solcast",
                )
            elif response.status_code == 400:
                return self._handle_interface_error(
                    "bad_request",
                    "Solcast API bad request - check parameters",
                    pv_config_entry,
                    "solcast",
                )

            response.raise_for_status()
            data = response.json()

        except requests.exceptions.Timeout:
            return self._handle_interface_error(
                "timeout",
                "Solcast API request timed out.",
                pv_config_entry,
                "solcast",
            )
        except requests.exceptions.RequestException as e:
            return self._handle_interface_error(
                "request_failed",
                f"Solcast API request failed: {e}",
                pv_config_entry,
                "solcast",
            )
        except (ValueError, TypeError) as e:
            return self._handle_interface_error(
                "invalid_json",
                f"Invalid JSON response from Solcast: {e}",
                pv_config_entry,
                "solcast",
            )

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

                    # Convert kW to Wh for 30-minute period
                    # kW * 0.5 hours = kWh, then * 1000 to get Wh
                    pv_estimate_wh = (
                        pv_estimate_kw * 500
                    )  # kW * 0.5h * 1000W/kW = Wh for 30min

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

            logger.debug(
                "[PV-IF] Solcast PV forecast for resource '%s' received %d forecast points,"
                + " first 12h (Wh): %s",
                resource_id,
                len(forecasts),
                pv_forecast[:12],  # Log first 12 hours to avoid spam
            )

            return pv_forecast

        except (ValueError, TypeError, AttributeError, KeyError) as e:
            return self._handle_interface_error(
                "processing_error",
                f"Error processing Solcast forecast data: {e}",
                pv_config_entry,
                "solcast",
            )

    def _handle_interface_error(
        self, error_type, message, pv_config_entry, source="unknown"
    ):
        """
        Centralized error handling for all API errors.
        """
        logger.error("[PV-IF] %s", message)
        self.pv_forcast_request_error.update(
            {
                "error": error_type,
                "timestamp": datetime.now().isoformat(),
                "message": message,
                "config_entry": pv_config_entry,
                "source": source,
            }
        )
        return []

    def test_output(self):
        """
        Test method to print the current PV and temperature forecasts.
        """
        self.config_source["source"] = "akkudoktor"
        pv_forcast_array1 = self.get_summarized_pv_forecast(48)
        # print("[PV-IF] PV forecast (Akkudoktor):", pv_forcast_array1)
        self.config_source["source"] = "openmeteo"
        pv_forcast_array2 = self.get_summarized_pv_forecast(48)
        # self.config_source["source"] = "forecast_solar"
        # pv_forcast_array3 = self.get_summarized_pv_forecast(48)

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

        for time in times:
            # Convert to Julian day number
            a = (14 - time.month) // 12
            y = time.year - a
            m = time.month + 12 * a - 3
            jdn = (
                time.day
                + (153 * m + 2) // 5
                + 365 * y
                + y // 4
                - y // 100
                + y // 400
                - 32045
            )

            # Add fraction of day
            hour_fraction = (time.hour + time.minute / 60 + time.second / 3600) / 24
            jd = jdn + hour_fraction - 0.5

            # Number of days since J2000.0
            n = jd - 2451545.0

            # Mean longitude of sun
            L = (280.460 + 0.9856474 * n) % 360

            # Mean anomaly of sun
            g = math.radians((357.528 + 0.9856003 * n) % 360)

            # Ecliptic longitude of sun
            lambda_sun = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))

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
