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
import aiohttp
import pytz
import requests
import pvlib
import pandas as pd
import numpy as np
from open_meteo_solar_forecast import OpenMeteoSolarForecast
from collections import defaultdict

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
            "[PV-IF] Initializing with 1st source: %s"
            # + " and 2nd source: %s"
            ,
            self.config_source.get("source", "akkudoktor"),
            # self.config_source.get("second_source", "openmeteo"),
        )
        self.__check_config()  # Validate configuration parameters

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
        self.update_interval = 15 * 60  # Update 15 minutes (in seconds)
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
        else:
            logger.debug("[PV-IF] Initialize - pv entries found: %s", len(self.config))
            for config_entry in self.config:
                # check for each entries the mandatory params
                if config_entry.get("name", ""):
                    logger.debug(
                        "[PV-IF] Initialize - config entry name: %s",
                        config_entry.get("name", ""),
                    )
                else:
                    logger.debug("[PV-IF] Init - config entry name not found")
                if config_entry.get("lat", None) is None:
                    raise ValueError("[PV-IF] Init - lat not found in config entry")
                if config_entry.get("lon", None) is None:
                    raise ValueError("[PV-IF] Init - lon not found in config entry")
                if config_entry.get("azimuth", None) is None:
                    raise ValueError("[PV-IF] Init - azimuth not found in config entry")
                if config_entry.get("tilt", None) is None:
                    raise ValueError("[PV-IF] Init - tilt not found in config entry")
                if config_entry.get("power", None) is None:
                    raise ValueError("[PV-IF] Init - power not found in config entry")
                if config_entry.get("powerInverter", None) is None:
                    raise ValueError(
                        "[PV-IF] Init - powerInverter not found in config entry"
                    )
                if config_entry.get("inverterEfficiency", None) is None:
                    raise ValueError(
                        "[PV-IF] Init - inverterEfficiency not found in config entry"
                    )
                # if config_entry.get("horizon", None) is None:
                #     config_entry["horizon"] = ""
                #     logger.debug("[PV-IF] Init - horizon not found in config entry,"+
                # "using default empty value")

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
                self.temp_forecast_array = self.__get_pv_forecast_akkudoktor_api(
                    tgt_value="temperature", pv_config_entry=self.config[0], tgt_duration=48
                )
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
              "default".
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
            return self.__get_pv_forecast_openmeteo_lib(config_entry, tgt_duration)
        elif self.config_source.get("source") == "openmeteo_local":
            return self.__get_pv_forecast_openmeteo_api(config_entry, tgt_duration)
        elif self.config_source.get("source") == "forecast_solar":
            return self.__get_pv_forecast_forecast_solar_api(config_entry, tgt_duration)
        elif self.config_source.get("source") == "evcc":
            return self.__get_pv_forecast_evcc_api(config_entry, tgt_duration)
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
            logger.error(
                "[PV-IF][akkudoktor] No PV config entry provided for target: %s",
                tgt_value,
            )
            return []
        forecast_request_payload = self.__create_forecast_request(pv_config_entry)
        # print(forecast_request_payload)
        recv_error = False
        try:
            response = requests.get(forecast_request_payload, timeout=5)
            response.raise_for_status()
            day_values = response.json()
            day_values = day_values["values"]
        except requests.exceptions.Timeout:
            logger.error(
                "[PV-IF][akkudoktor] Request timed out while fetching PV forecast. (%s)",
                tgt_value,
            )
            recv_error = True
        except requests.exceptions.RequestException as e:
            logger.error(
                "[PV-IF][akkudoktor] Request failed while fetching PV forecast (%s): %s",
                tgt_value,
                e,
            )
            recv_error = True
        if recv_error:
            if tgt_value == "power":
                logger.info(
                    "[PV-IF][akkudoktor] Using default PV forecast with max %s W for %s",
                    pv_config_entry["power"],
                    pv_config_entry["name"],
                )
                # return a default forecast with 0% at night and 100% at noon
                return self.__get_default_pv_forcast(pv_config_entry["power"])
            else:
                logger.info(
                    "[PV-IF][akkudoktor] Using default temperature forecast for %s",
                    pv_config_entry["name"],
                )
                # return a default temperature forecast with 0% at night and 100% at noon
                return self.__get_default_temperature_forecast()

        forecast_values = []
        tz = pytz.timezone(self.time_zone)
        current_time = tz.localize(
            datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        )
        end_time = current_time + timedelta(hours=tgt_duration)
        # logger.debug(
        #     "[PV-IF] Fetching %s forecast for %s from %s to %s",
        #     tgt_value,
        #     pv_config_entry["name"],
        #     current_time.isoformat(),
        #     end_time.isoformat(),
        # )

        for forecast_entry in day_values:
            for forecast in forecast_entry:
                entry_time = datetime.fromisoformat(forecast["datetime"])
                if entry_time.tzinfo is None:
                    # If datetime is naive, localize it
                    entry_time = pytz.timezone(self.time_zone).localize(entry_time)
                else:
                    # Convert to configured timezone
                    entry_time = entry_time.astimezone(pytz.timezone(self.time_zone))
                if current_time <= entry_time < end_time:
                    value = forecast.get(tgt_value, 0)

                    # logger.debug(
                    #     "[PV-IF] Processing forecast entry at %s (%s) for value:  %s",
                    #     entry_time.isoformat(), forecast["datetime"],
                    #     value,
                    # )
                    # if power is negative, set it to 0 (fixing wrong values form api)
                    if tgt_value == "power" and value < 0:
                        value = 0
                    forecast_values.append(value)
        # workaround for wrong time points in the forecast from akkudoktor
        # remove first entry and append 0 to the end
        forecast_values.pop(0)
        forecast_values.append(0)

        request_type = "PV forecast"
        pv_config_name = "for " + pv_config_entry["name"]
        if tgt_value == "temperature":
            request_type = "Temperature forecast"
            pv_config_name = ""
        logger.debug(
            "[PV-IF] %s fetched successfully %s",
            request_type,
            pv_config_name,
        )
        # fix for time changes e.g. western europe then fill or reduce the array to 48 values
        if len(forecast_values) > tgt_duration:
            forecast_values = forecast_values[:tgt_duration]
            logger.debug(
                "[PV-IF][akkudoktor] Day of time change %s values reduced to %s for %s",
                request_type,
                tgt_duration,
                pv_config_name,
            )
        elif len(forecast_values) < tgt_duration:
            forecast_values.extend(
                [forecast_values[-1]] * (tgt_duration - len(forecast_values))
            )
            logger.debug(
                "[PV-IF][akkudoktor] Day of time change %s values extended to %s for %s",
                request_type,
                tgt_duration,
                pv_config_name,
            )
        return forecast_values

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

        # logger.debug(
        #     "[PV-IF] Open-Meteo radiation: %s", radiation
        # )

        # Prepare time index for pvlib
        times = pd.date_range(
            start=data["hourly"]["time"][0], periods=hours, freq="h", tz=timezone
        )

        # Get sun position
        solpos = pvlib.solarposition.get_solarposition(times, latitude, longitude)
        logger.debug("[PV-IF] Open-Meteo solar position calculated solpos - %s", solpos)

        # Calculate angle of incidence (AOI)
        aoi = pvlib.irradiance.aoi(
            surface_tilt=tilt,
            surface_azimuth=azimuth,
            solar_zenith=solpos["apparent_zenith"],
            solar_azimuth=solpos["azimuth"],
        )

        # Calculate PV forecast
        pv_forecast = []
        for rad, cc, angle, sun_az, sun_el in zip(
            radiation,
            cloudcover,
            aoi,
            solpos["azimuth"],
            90 - solpos["apparent_zenith"],
        ):
            # Adjust radiation for cloud cover
            eff_rad = rad * (1 - cc / 100) + rad * cloud_factor * (cc / 100)

            # Project radiation onto panel
            projection = max(np.cos(np.radians(angle)), 0)

            # Adjust for panel efficiency (22,5% is a common value)
            eff_rad_panel = eff_rad * projection * 0.225

            # --- Horizon check ---
            horizon_elev = self.__get_horizon_elevation(sun_az, horizon)
            if sun_el < horizon_elev:
                # logger.debug(
                #     "[PV-IF] Sun elevation %s° is below horizon elevation %s° at azimuth %s°",
                #     sun_el,
                #     horizon_elev,
                #     sun_az,
                # )
                eff_rad_panel = (
                    eff_rad_panel * 0.25
                )  # Sun is behind local horizon - 25% of radiation

            # Estimate PV energy output (Wh)
            energy_wh = (
                eff_rad_panel * pv_efficiency * installed_power_watt / 220
            )  # Assuming 220 W/m² as average panel efficiency for area estimation
            energy_wh = max(0, energy_wh)  # Ensure no negative values

            # logger.debug(
            #     "[PV-IF] Radiation: %s W/m², Cloud cover: %s%%, AOI: %s°, "
            #     "Sun azimuth: %s°, Sun elevation: %s° -> Energy output: %s Wh",
            #     round(rad, 2), round(cc, 2), round(angle, 2),
            #     round(sun_az, 2), round(sun_el, 2), round(energy_wh, 2)
            # )
            pv_forecast.append(round(energy_wh, 1))

        pv_forecast = [float(x) for x in pv_forecast]
        logger.debug(
            "[PV-IF] Open-Meteo PV forecast for '%s' (Wh): %s",
            pv_config_entry["name"],
            pv_forecast,
        )

        return pv_forecast

    def __get_pv_forecast_openmeteo_lib(self, pv_config_entry, hours=48):
        """
        Synchronous wrapper for the async OpenMeteoSolarForecast.
        """
        return asyncio.run(
            self.__get_pv_forecast_openmeteo_lib_async(pv_config_entry, hours)
        )

    async def __get_pv_forecast_openmeteo_lib_async(self, pv_config_entry, hours=48):
        """
        Fetches PV forecast from Forecast.Solar LIB.
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

                logger.debug(
                    "[PV-IF] Openmeteo Lib PV forecast (Wh) (length: %s): %s",
                    len(pv_forecast),
                    pv_forecast,
                )
                return pv_forecast
        except (aiohttp.ClientError, ConnectionError) as e:
            logger.error("[PV-IF] OpenMeteoLib SolarForecast connection error: %s", e)
            # Return a default or empty forecast to avoid crashing the thread
            return self.__get_default_pv_forcast(pv_config_entry.get("power", 200))
        except (ValueError, KeyError, AttributeError, TypeError) as e:
            logger.error(
                "[PV-IF] Unexpected error in OpenMeteoLib SolarForecast: %s", e
            )
            return self.__get_default_pv_forcast(pv_config_entry.get("power", 200))

    def __get_pv_forecast_forecast_solar_api(self, pv_config_entry, hours=48):
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
                # logger.debug("[PV-IF] Horizon values: %s", horizon)
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
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            self.pv_forcast_request_error["error"] = None
        except requests.exceptions.Timeout:
            logger.error("[PV-IF] Forecast.Solar API request timed out.")
            self.pv_forcast_request_error["error"] = "timeout"
            self.pv_forcast_request_error["timestamp"] = datetime.now().isoformat()
            self.pv_forcast_request_error["message"] = (
                "Forecast.Solar API request timed out."
            )
            self.pv_forcast_request_error["config_entry"] = pv_config_entry
            self.pv_forcast_request_error["source"] = "forecast_solar"
            return []
        except requests.exceptions.RequestException as e:
            logger.error("[PV-IF] Forecast.Solar API request failed: %s", e)
            # logger.error("[PV-IF] Forecast.Solar API error response: %s", response.json())
            self.pv_forcast_request_error["error"] = "request_failed"
            self.pv_forcast_request_error["timestamp"] = datetime.now().isoformat()
            self.pv_forcast_request_error["message"] = (
                f"Forecast.Solar API request failed: {e}"
            )
            self.pv_forcast_request_error["config_entry"] = pv_config_entry
            self.pv_forcast_request_error["source"] = "forecast_solar"
            return []
        data = response.json()
        # logger.debug("[PV-IF] Forecast.Solar API response: %s", data)
        watt_hours_period = data.get("result", {}).get("watt_hours_period", {})

        if not watt_hours_period:
            logger.error("[PV-IF] No valid watt_hours_period data found.")
            self.pv_forcast_request_error["error"] = "no_valid_data"
            self.pv_forcast_request_error["timestamp"] = datetime.now().isoformat()
            self.pv_forcast_request_error["message"] = (
                "No valid watt_hours_period data found."
            )
            self.pv_forcast_request_error["config_entry"] = pv_config_entry
            self.pv_forcast_request_error["source"] = "forecast_solar"
            return []

        parsed = [
            (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"), v)
            for ts, v in watt_hours_period.items()
        ]
        min_time = min(dt for dt, _ in parsed)
        # Align to midnight of the first day
        midnight = min_time.replace(hour=0, minute=0, second=0, microsecond=0)
        # Build list of 48 hourly timestamps
        hours = [midnight + timedelta(hours=i) for i in range(48)]
        # Build a lookup dict for fast access
        lookup = {dt: v for dt, v in parsed}
        # Fill the forecast array
        forecast_wh = []
        for h in hours:
            # Use value if exact hour exists, else 0
            forecast_wh.append(lookup.get(h, 0))

        pv_forecast = forecast_wh
        # logger.debug("[PV-IF] Forecast.Solar PV forecast (Wh): %s", pv_forecast)
        return pv_forecast

    def __get_pv_forecast_evcc_api(self, pv_config_entry, hours=48):
        """
        Fetches PV forecast from an EVCC instance.
        """
        if self.config_special.get("url", "") == "":
            logger.error(
                "[PV-IF] No EVCC URL configured for EVCC PV forecast - using default"
            )
            return self.__get_default_pv_forcast(pv_config_entry.get("power", 200))

        url = self.config_special.get("url", "").rstrip("/") + "/api/state"
        logger.debug("[PV-IF] Fetching PV forecast from EVCC API: %s", url)
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            self.pv_forcast_request_error["error"] = None
        except requests.exceptions.Timeout:
            logger.error("[PV-IF] EVCC API request timed out.")
            self.pv_forcast_request_error["error"] = "timeout"
            self.pv_forcast_request_error["timestamp"] = datetime.now().isoformat()
            self.pv_forcast_request_error["message"] = "EVCC API request timed out."
            self.pv_forcast_request_error["config_entry"] = pv_config_entry
            self.pv_forcast_request_error["source"] = "evcc"
            return []
        except requests.exceptions.RequestException as e:
            logger.error("[PV-IF] EVCC API request failed: %s", e)
            self.pv_forcast_request_error["error"] = "request_failed"
            self.pv_forcast_request_error["timestamp"] = datetime.now().isoformat()
            self.pv_forcast_request_error["message"] = f"EVCC API request failed: {e}"
            self.pv_forcast_request_error["config_entry"] = pv_config_entry
            self.pv_forcast_request_error["source"] = "evcc"
            return []
        data = response.json()
        # print("raw evcc api data: %s", data)
        solar_forecast_all = data.get("forecast", []).get("solar", [])
        solar_forecast_scale = solar_forecast_all.get("scale", "unknown")
        logger.debug(
            "[PV-IF] EVCC API solar forecast received with scale: %s",
            solar_forecast_scale,
        )
        solar_forecast = solar_forecast_all.get("timeseries", [])


        if solar_forecast and isinstance(solar_forecast, list):
            # Build a mapping from hour (datetime) to list of values
            import dateutil.parser

            tz = pytz.timezone(self.time_zone)
            # Start at midnight today in configured timezone
            midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            hour_bins = [midnight + timedelta(hours=i) for i in range(hours)]
            hour_map = {h: [] for h in hour_bins}

            for item in solar_forecast:
                ts = dateutil.parser.isoparse(item.get("ts"))
                # Align to the start of the hour in configured timezone
                ts = ts.astimezone(tz)
                hour_start = ts.replace(minute=0, second=0, microsecond=0)
                if hour_start in hour_map:
                    hour_map[hour_start].append(item.get("val", 0.0))

            pv_forecast = [0] * hours
            for idx, h in enumerate(hour_bins):
                vals = hour_map[h]
                if vals:
                    pv_forecast[idx] = sum(vals) / len(vals)
                else:
                    pv_forecast[idx] = 0.0

            # Scale each value according to the solar_forecast_scale (e.g., 0.5 or 0.75)
            try:
                scale_factor = float(solar_forecast_scale)
            except (TypeError, ValueError):
                scale_factor = 1.0
            pv_forecast = [val * scale_factor for val in pv_forecast]
            logger.debug(
                "[PV-IF] EVCC PV forecast for given evcc pv config (Wh): %s",
                pv_forecast,
            )
            return pv_forecast

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
