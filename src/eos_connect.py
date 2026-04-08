"""
This module fetches energy data from OpenHAB, processes it, and creates a load profile.
"""

import os
import sys
from datetime import datetime, timedelta
import time
import logging
import json
import threading
import pytz
import requests
from flask import (
    Flask,
    Response,
    make_response,
    render_template_string,
    request,
    send_from_directory,
)
from version import __version__
from config import ConfigManager
from log_handler import MemoryLogHandler
from constants import CURRENCY_SYMBOL_MAP, CURRENCY_MINOR_UNIT_MAP
from interfaces.base_control import BaseControl
from interfaces.load_interface import LoadInterface
from interfaces.battery_interface import BatteryInterface
from interfaces.evcc_interface import EvccInterface
from interfaces.optimization_interface import OptimizationInterface
from interfaces.price_interface import PriceInterface
from interfaces.mqtt_interface import MqttInterface
from interfaces.pv_interface import PvInterface
from interfaces.port_interface import PortInterface
from interfaces.update_checker import UpdateChecker
from interfaces.inverters import create_inverter
from interfaces.inverters.null_inverter import NullInverter
from interfaces.inverters.evcc_inverter import EvccInverter
from config_web import ConfigWebModule

# Check Python version early
if sys.version_info < (3, 11):
    sys.stderr.write(
        f"ERROR: Python 3.11 or higher is required. "
        f"You are running Python {sys.version_info.major}.{sys.version_info.minor}\n"
    )
    sys.stderr.write("Please upgrade your Python installation.\n")
    sys.exit(1)

EOS_TGT_DURATION = 48


###################################################################################################
# Custom formatter to use the configured timezone
class TimezoneFormatter(logging.Formatter):
    """
    A custom logging formatter that formats log timestamps according to a specified timezone.
    """

    def __init__(self, fmt=None, datefmt=None, tz=None):
        super().__init__(fmt, datefmt)
        self.tz = tz

    def formatTime(self, record, datefmt=None):
        # Convert the record's timestamp to the configured timezone
        record_time = datetime.fromtimestamp(record.created, self.tz)
        return record_time.strftime(datefmt or self.default_time_format)


##################################################################################################
LOGLEVEL = logging.DEBUG  # start before reading the config file
logger = logging.getLogger(__name__)

# Basic formatter for startup logging (before config/timezone is available)
basic_formatter = logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
)
streamhandler = logging.StreamHandler(sys.stdout)
streamhandler.setFormatter(basic_formatter)
logger.addHandler(streamhandler)
logger.setLevel(LOGLEVEL)
logger.info("[Main] Starting eos_connect - version: %s", __version__)

###################################################################################################
base_path = os.path.dirname(os.path.abspath(__file__))
# get param to set a specific path
if len(sys.argv) > 1:
    current_dir = sys.argv[1]
else:
    current_dir = base_path

###################################################################################################
config_manager = ConfigManager(current_dir)
time_zone = pytz.timezone(config_manager.config["time_zone"])

LOGLEVEL = config_manager.config["log_level"].upper()
logger.setLevel(LOGLEVEL)

# Now upgrade to timezone-aware formatter after config is loaded
timezone_formatter = TimezoneFormatter(
    "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S", tz=time_zone
)
streamhandler.setFormatter(timezone_formatter)

memory_handler = MemoryLogHandler(
    max_records=50000,  # All log entries (mixed levels)
    max_alerts=2000,  # Dedicated alert buffer (WARNING/ERROR/CRITICAL only)
)
memory_handler.setFormatter(timezone_formatter)  # Use timezone formatter for web logs
logger.addHandler(memory_handler)
logger.debug("[Main] Memory log handler initialized successfully")

logger.info(
    "[Main] set user defined time zone to %s and loglevel to %s",
    config_manager.config["time_zone"],
    LOGLEVEL,
)

# Phase 1: open the config DB and deep-update config_manager.config with any
# values the user changed via the web UI.  All interfaces constructed below
# will therefore receive the correct, authoritative values directly — no
# post-init re-sync is needed.
config_web = ConfigWebModule(config_manager)
try:
    config_web.start_db()
except Exception:
    logger.exception(
        "[Main] Config database startup failed — continuing with config.yaml values. "
        "Check data directory permissions and disk space."
    )

# Set global time frame base AFTER DB merge (so DB values are respected)
# with validation and fallback
time_frame_base = config_manager.config.get("eos", {}).get("time_frame", 3600)
eos_source = config_manager.config.get("eos", {}).get("source", "eos_server")

try:
    time_frame_base = int(time_frame_base)
except (TypeError, ValueError):
    logger.warning(
        "[Config] Invalid time_frame type (%r); defaulting to 3600", time_frame_base
    )
    time_frame_base = 3600

if time_frame_base not in (900, 3600):
    logger.warning(
        "[Config] Invalid time_frame (%s); defaulting to 3600", time_frame_base
    )
    time_frame_base = 3600
elif time_frame_base == 900 and eos_source != "evopt":
    logger.warning(
        "[Config] 15-min time_frame only supported with EVopt source; defaulting to 3600"
    )
    time_frame_base = 3600

# initialize eos interface
eos_interface = OptimizationInterface(
    config=config_manager.config["eos"],
    time_frame_base=time_frame_base,
    timezone=time_zone,
)

# initialize base control
base_control = BaseControl(config_manager.config, time_zone, time_frame_base)
# initialize the inverter interface
inverter_interface = None

# Call factory via config dict
inverter_interface = create_inverter(config_manager.config["inverter"])
if inverter_interface is not None:
    inverter_interface.initialize()
else:
    logger.error(
        "[Main] Failed to initialize inverter interface - check inverter configuration"
    )


# callback function for evcc interface
def charging_state_callback(new_state):
    """
    Callback function that gets triggered when the charging state changes.
    """
    # update the base control with the new charging state
    base_control.set_current_evcc_charging_state(evcc_interface.get_charging_state())
    base_control.set_current_evcc_charging_mode(evcc_interface.get_charging_mode())
    logger.info("[MAIN] EVCC Event - Charging state changed to: %s", new_state)
    change_control_state()


# callback function for battery interface
def battery_state_callback():
    """
    Callback function that gets triggered when the battery state changes.
    """
    logger.debug(
        "[MAIN] Battery Event - State of charge changed to: %s",
        battery_interface.get_current_soc(),
    )
    # update the base control with the new battery state of charge
    change_control_state()


# callback function for mqtt interface
def mqtt_control_callback(mqtt_cmd):
    """
    Handles MQTT control commands by parsing the command dictionary and updating the system's state.

    Args:
        mqtt_cmd (dict): Contains "duration" (str, "HH:MM"), "mode" (str/int),
        and "grid_charge_power" (str/int).

    Side Effects:
        - Updates base control mode override.
        - Publishes updated control topics to MQTT.
        - Logs the event and triggers a control state change.
    """
    logger.info("[MAIN] MQTT Event - control command received: %s", mqtt_cmd)

    if "charge_power" in mqtt_cmd:
        # Default to 0 if empty or None
        charge_power = mqtt_cmd.get("charge_power", 0) or 0
        charge_power = int(charge_power) / 1000  # convert to kW
        base_control.set_override_charge_rate(charge_power)
        # update mqtt topics
        mqtt_interface.update_publish_topics(
            {
                "control/override_charge_power": {"value": charge_power * 1000},
            }
        )
        logger.info(
            "[MAIN] MQTT Event - charge_power command to: %s", mqtt_cmd["charge_power"]
        )

    if "duration" in mqtt_cmd:
        # Default to "02:00" if empty or None
        duration_string = mqtt_cmd.get("duration", "02:00") or "02:00"
        duration_hh = duration_string.split(":")[0]
        duration_mm = duration_string.split(":")[1]
        duration = int(duration_hh) * 60 + int(duration_mm)

        # update the base control with the new charging state
        base_control.set_override_duration(duration)
        # update mqtt topics
        mqtt_interface.update_publish_topics(
            {
                "control/override_end_time": {
                    "value": (
                        datetime.fromtimestamp(
                            base_control.get_override_active_and_endtime()[1], time_zone
                        )
                    ).isoformat()
                },
            }
        )
        logger.info("[MAIN] MQTT Event - duration command to: %s", mqtt_cmd["duration"])

    if "mode" in mqtt_cmd:
        # mode
        mode_value = mqtt_cmd.get("mode")
        if mode_value is None:
            mode_value = base_control.get_current_overall_state_number()
        # update the base control with the new charging state
        base_control.set_mode_override(int(mode_value))
        # update mqtt topics
        mqtt_interface.update_publish_topics(
            {
                "control/override_charge_power": {
                    "value": base_control.get_override_charge_rate() * 1000
                },
                "control/override_active": {
                    "value": base_control.get_override_active_and_endtime()[0]
                },
                "control/override_end_time": {
                    "value": (
                        datetime.fromtimestamp(
                            base_control.get_override_active_and_endtime()[1], time_zone
                        )
                    ).isoformat()
                },
            }
        )
        logger.info("[MAIN] MQTT Event - control command to: %s", mqtt_cmd["mode"])
        change_control_state()
    # Check for battery SOC limit keys
    if "soc_min" in mqtt_cmd:
        soc_min = int(mqtt_cmd.get("soc_min", battery_interface.get_min_soc()))
        battery_interface.set_min_soc(soc_min)

        mqtt_interface.update_publish_topics(
            {
                "battery/soc_min": {"value": battery_interface.get_min_soc()},
            }
        )
        logger.info("[MAIN] MQTT Event - battery soc limit command: %s", mqtt_cmd)
    if "soc_max" in mqtt_cmd:
        soc_max = int(mqtt_cmd.get("soc_max", battery_interface.get_max_soc()))
        battery_interface.set_max_soc(soc_max)

        mqtt_interface.update_publish_topics(
            {
                "battery/soc_max": {"value": battery_interface.get_max_soc()},
            }
        )
        logger.info("[MAIN] MQTT Event - battery soc limit command: %s", mqtt_cmd)


mqtt_interface = MqttInterface(
    config_mqtt=config_manager.config["mqtt"], on_mqtt_command=None
)

evcc_interface = EvccInterface(
    url=config_manager.config.get("evcc", {}).get("url", ""),
    ext_bat_mode=config_manager.config["inverter"]["type"] == "evcc",
    update_interval=10,
    on_charging_state_change=None,
)

# intialize the load interface
load_interface = LoadInterface(
    config_manager.config.get("load", {}),
    time_frame_base,
    time_zone,
    request_timeout=config_manager.config.get("request_timeout", 10),
)

battery_config = dict(config_manager.config["battery"])
battery_config["feed_in_price"] = config_manager.config.get("price", {}).get(
    "feed_in_price", 0.0
)
battery_interface = BatteryInterface(
    battery_config,
    on_bat_max_changed=None,
    load_interface=load_interface,
    timezone=time_zone,
    base_control=base_control,
    request_timeout=config_manager.config.get("request_timeout", 10),
)

price_interface = PriceInterface(
    config_manager.config["price"], time_frame_base, time_zone
)

pv_interface = PvInterface(
    config_manager.config["pv_forecast_source"],
    config_manager.config["pv_forecast"],
    time_frame_base,
    config_manager.config.get("evcc", {}),
    (
        True
        if config_manager.config["eos"].get("source", "eos_server") == "eos_server"
        else False
    ),
    config_manager.config.get("time_zone", "UTC"),
)

# wait for the interfaces to initialize - depend on entries for pv_forecast
init_time = 3 + 1 * len(config_manager.config["pv_forecast"])
logger.info("[Main] Waiting %s seconds for interfaces to initialize", init_time)
time.sleep(init_time)

# Perform initial battery price calculation if enabled (blocking, synchronous)
# This ensures the first optimization run has the correct battery price
battery_interface.perform_initial_price_calculation()


# Callback for update status changes (publishes to MQTT)
def on_update_status_change(status):
    """Called when update status changes (update becomes available or unavailable)."""
    try:
        mqtt_interface.update_publish_topics(
            {
                "system/update_available": {
                    "value": str(status["update_available"]).lower()
                },
                "system/current_version": {"value": status["current_version"]},
                "system/latest_version": {"value": status.get("latest_version") or ""},
                "system/update_check_enabled": {
                    "value": str(status["enabled"]).lower()
                },
            }
        )
        logger.info("[UPDATE-CHECK] Published status change to MQTT")
    except (KeyError, TypeError, ValueError) as e:
        logger.error("[UPDATE-CHECK] Error publishing to MQTT: %s", e)


# Initialize update checker (checks for Docker image updates)
# Automatically disabled for Home Assistant Add-on users
update_checker = UpdateChecker(
    current_version=__version__,
    check_interval=43200,
    on_status_change=on_update_status_change,
)

# Publish initial update status to MQTT
try:
    initial_status = update_checker.get_update_status()
    on_update_status_change(initial_status)
except (KeyError, TypeError, ValueError) as e:
    logger.error("[UPDATE-CHECK] Error publishing initial status to MQTT: %s", e)

# pv_interface.test_output()
# sys.exit(0)  # exit if the interfaces are not initialized correctly


# summarize all date
def create_optimize_request():
    """
    Creates an optimization request payload for energy management systems.

    Args:
        api_version (str): The API version to use for the request. Defaults to "new".

    Returns:
        dict: A dictionary containing the payload for the optimization request.
    """

    def get_dst_change_in_next_48(tz, start_dt=None):
        """
        Returns:
            0 if no DST change in next 48 hours,
            +N if DST fallback (extra hour) at Nth hour from now,
            -N if DST spring forward (missing hour) at Nth hour from now.
        """
        if start_dt is None:
            start_dt = datetime.now(tz)
        if start_dt.tzinfo is None:
            start_dt = tz.localize(start_dt)
        prev_offset = start_dt.utcoffset()
        for i in range(1, 49):
            check_dt = tz.normalize(start_dt + timedelta(hours=i))
            offset = check_dt.utcoffset()
            if offset != prev_offset:
                # DST change detected
                if offset > prev_offset:
                    logger.debug("[DST] Spring forward detected at hour %s: -%s", i, i)
                    return -i  # hour lost
                logger.debug("[DST] Fall back detected at hour %s: +%s", i, i)
                return i  # hour gained
            prev_offset = offset
        logger.debug("[DST] No DST change detected in next 48 hours (0)")
        return 0

    # def adjust_forecast_array_for_dst(data_array, dst_change_detected):
    #     """
    #     Adjusts the forecast array for Daylight Saving Time (DST) changes.

    #     Args:
    #         data_array (list): The original forecast array.
    #         dst_change_detected (int): The DST change detected (positive for fall back,
    #                                     negative for spring forward).
    #     Returns:
    #         list: The adjusted forecast array.
    #     """
    #     arr = list(data_array)  # Make a copy so the original is not modified
    #     if dst_change_detected != 0:
    #         hour_index = abs(dst_change_detected) - 1

    #         # Validate computed index to avoid IndexError
    #         if hour_index < 0 or hour_index >= len(arr):
    #             logger.warning(
    #                 "[DST] Computed hour index %s out of range for array length %s"
    #                 + " - skipping DST adjustment",
    #                 hour_index,
    #                 len(arr),
    #             )
    #             return arr

    #         if dst_change_detected > 0:
    #             # Fall back - repeat hour
    #             arr.insert(hour_index, arr[hour_index])  # duplicate hour
    #             logger.debug(
    #                 "[DST] Adjusted forecast for fall back at hour %s",
    #                 hour_index + 1,
    #             )
    #         else:
    #             # Spring forward - remove hour
    #             removed_value = arr.pop(hour_index)
    #             logger.debug(
    #                 "[DST] Adjusted forecast for spring forward at hour %s (removed %s Wh)",
    #                 hour_index + 1,
    #                 removed_value,
    #             )
    #     return arr

    def get_ems_data(dst_change_detected):

        pv_prognose_wh = pv_interface.get_current_pv_forecast()
        strompreis_euro_pro_wh = price_interface.get_current_prices()
        einspeiseverguetung_euro_pro_wh = price_interface.get_current_feedin_prices()
        gesamtlast = load_interface.get_load_profile(EOS_TGT_DURATION)

        if config_manager.config.get("eos", {}).get("source", "eos_server") == "evopt":
            now = datetime.now(time_zone)
            seconds_since_midnight = now.hour * 3600 + now.minute * 60 + now.second
            scale_factor = (
                time_frame_base - (seconds_since_midnight % time_frame_base)
            ) / time_frame_base

            if time_frame_base == 3600:
                current_slot = now.hour
            elif time_frame_base == 900:
                current_slot = now.hour * 4 + now.minute // 15
            else:
                current_slot = seconds_since_midnight // time_frame_base

            for ts in (pv_prognose_wh, gesamtlast):
                if ts and len(ts) > current_slot:
                    ts[current_slot] *= scale_factor
                    logger.debug(
                        "[EOS_Request] Adjusted forecast for slot %d to %.2f Wh "
                        + "due to partial slot",
                        current_slot + 1,
                        ts[current_slot],
                    )

        # if dst_change_detected != 0:
        #     pv_prognose_wh = adjust_forecast_array_for_dst(
        #         pv_prognose_wh, dst_change_detected
        #     )
        #     strompreis_euro_pro_wh = adjust_forecast_array_for_dst(
        #         strompreis_euro_pro_wh, dst_change_detected
        #     )
        #     einspeiseverguetung_euro_pro_wh = adjust_forecast_array_for_dst(
        #         einspeiseverguetung_euro_pro_wh, dst_change_detected
        #     )
        #     gesamtlast = adjust_forecast_array_for_dst(gesamtlast, dst_change_detected)

        return {
            "pv_prognose_wh": pv_prognose_wh,
            "strompreis_euro_pro_wh": strompreis_euro_pro_wh,
            "einspeiseverguetung_euro_pro_wh": einspeiseverguetung_euro_pro_wh,
            "preis_euro_pro_wh_akku": battery_interface.get_price_euro_per_wh(),
            "gesamtlast": gesamtlast,
        }

    def get_pv_akku_data():
        # Use dynamic max charge power if charging curve is enabled, otherwise use fixed value
        # This ensures EVopt receives realistic charging limits based on current SOC
        current_dynamic_max = battery_interface.get_max_charge_power()
        max_charge_power = (
            current_dynamic_max
            if config_manager.config["battery"].get("charging_curve_enabled", True)
            else config_manager.config["battery"]["max_charge_power_w"]
        )

        # Debug logging for charge demand tracking
        is_dynamic = config_manager.config["battery"].get(
            "charging_curve_enabled", True
        )
        logger.info(
            "[CHARGE_DEMAND] Optimizer request preparation: max_charge_power=%s W "
            "(source=%s, dynamic_max=%s, config_fixed=%s, charging_curve_enabled=%s)",
            max_charge_power,
            "dynamic" if is_dynamic else "fixed",
            current_dynamic_max,
            config_manager.config["battery"]["max_charge_power_w"],
            is_dynamic,
        )

        # Store this value in base_control so it can use the same value when
        # converting relative charge demands back to absolute values
        # This prevents sawtooth patterns caused by mismatched max_charge_power values
        base_control.optimization_max_charge_power_w = max_charge_power

        akku_object = {
            "capacity_wh": config_manager.config["battery"]["capacity_wh"],
            "charging_efficiency": config_manager.config["battery"][
                "charge_efficiency"
            ],
            "discharging_efficiency": config_manager.config["battery"][
                "discharge_efficiency"
            ],
            "max_charge_power_w": max_charge_power,
            "initial_soc_percentage": round(battery_interface.get_current_soc()),
            "min_soc_percentage": battery_interface.get_min_soc(),
            "max_soc_percentage": battery_interface.get_max_soc(),
        }
        if eos_interface.is_eos_version_at_least("0.0.2"):
            akku_object = {"device_id": "battery1", **akku_object}
        return akku_object

    def get_wechselrichter_data():
        wechselrichter_object = {
            "max_power_wh": config_manager.config["inverter"]["max_pv_charge_rate"],
        }
        if eos_interface.is_eos_version_at_least("0.0.2"):
            wechselrichter_object = {
                "device_id": "inverter1",
                **wechselrichter_object,
            }  # at top
            wechselrichter_object["battery_id"] = "battery1"  # at the bottom
        return wechselrichter_object

    def get_eauto_data():
        eauto_object = {
            "capacity_wh": 27000,
            "charging_efficiency": 0.90,
            "discharging_efficiency": 0.95,
            "max_charge_power_w": 7360,
            "initial_soc_percentage": 50,
            "min_soc_percentage": 5,
            "max_soc_percentage": 100,
        }
        if eos_interface.is_eos_version_at_least("0.0.2"):
            eauto_object = {"device_id": "ev1", **eauto_object}
        return eauto_object

    def get_dishwasher_data():
        consumption_wh = config_manager.config["load"].get(
            "additional_load_1_consumption", 1
        )
        if not consumption_wh or consumption_wh == 0:
            consumption_wh = 1
        duration_h = config_manager.config["load"].get("additional_load_1_runtime", 1)
        if not duration_h or duration_h == 0:
            duration_h = 1
        dishwasher_object = {
            "consumption_wh": consumption_wh,
            "duration_h": duration_h,
        }
        if eos_interface.is_eos_version_at_least("0.0.2"):
            dishwasher_object = {"device_id": "additional_load_1", **dishwasher_object}
        # if eos_interface.get_eos_version() == "0.1.0+dev":
        #     time_windows = [{"duration": "2", "start_time": "10:00"}]
        #     dishwasher_object = {"time_windows": time_windows, **dishwasher_object}
        return dishwasher_object

    dst_change_detected = get_dst_change_in_next_48(time_zone)

    temperature_forecast = pv_interface.get_current_temp_forecast()
    if dst_change_detected != 0:
        logger.info(
            "[Main] DST change detected: in %s hours there will be a shift with %s - please check"
            + " https://github.com/ohAnd/EOS_connect/issues/130#issuecomment-3444749335"
            + " for details.",
            abs(dst_change_detected),
            "1 hour plus" if dst_change_detected > 0 else "1 hour minus",
        )
    #     temperature_forecast = adjust_forecast_array_for_dst(
    #         temperature_forecast, dst_change_detected
    #     )

    payload = {
        "ems": get_ems_data(dst_change_detected),
        "pv_akku": get_pv_akku_data(),
        "inverter": get_wechselrichter_data(),
        "eauto": get_eauto_data(),
        "dishwasher": get_dishwasher_data(),
        "temperature_forecast": temperature_forecast,
        "start_solution": eos_interface.get_last_start_solution(),
    }
    logger.debug(
        "[Main] optimize request payload - startsolution: %s", payload["start_solution"]
    )
    return payload


last_control_data = {
    "current_soc": None,
    "ac_charge_demand": None,
    "dc_charge_demand": None,
    "discharge_allowed": None,
}


def setting_control_data(ac_charge_demand_rel, dc_charge_demand_rel, discharge_allowed):
    """
    Process the optimized response from EOS and update the load interface.

    Args:
        ac_charge_demand_rel (float): The relative AC charge demand.
        dc_charge_demand_rel (float): The relative DC charge demand.
        discharge_allowed (bool): Whether discharge is allowed (True/False).
    """
    # Safety check: Prevent AC charging if battery SoC exceeds maximum
    current_soc = battery_interface.get_current_soc()
    max_soc = config_manager.config["battery"]["max_soc_percentage"]

    if (
        last_control_data["current_soc"] is not None
        and last_control_data["ac_charge_demand"] is not None
    ):
        if (
            current_soc >= max_soc
            and ac_charge_demand_rel > 0
            and last_control_data["current_soc"] != current_soc
            and last_control_data["ac_charge_demand"] != ac_charge_demand_rel
        ):
            logger.warning(
                "[Main] EOS requested AC charging (%s) but battery SoC (%s%%)"
                + " at/above maximum (%s%%) - overriding to 0",
                ac_charge_demand_rel,
                current_soc,
                max_soc,
            )
            ac_charge_demand_rel = 0  # Override EOS decision for safety

    base_control.set_current_ac_charge_demand(ac_charge_demand_rel)
    base_control.set_current_dc_charge_demand(dc_charge_demand_rel)
    base_control.set_current_discharge_allowed(bool(discharge_allowed))

    # Set the dynamic override discharge allowed active flag from latest optimization data
    dyn_override_active = eos_interface.get_last_control_data()[0].get(
        "dyn_override_active", False
    )
    base_control.set_dyn_override_discharge_allowed_active(dyn_override_active)

    # set the current battery state of charge
    base_control.set_current_battery_soc(battery_interface.get_current_soc())
    # getting the current charging state from evcc
    base_control.set_current_evcc_charging_state(evcc_interface.get_charging_state())
    base_control.set_current_evcc_charging_mode(evcc_interface.get_charging_mode())

    # Publish MQTT after all states are set to reflect the final combined state
    ac_power_for_mqtt = base_control.get_needed_ac_charge_power()
    # Only log MQTT publish on change (logging happens in get_needed_ac_charge_power)
    mqtt_interface.update_publish_topics(
        {
            "control/eos_ac_charge_demand": {"value": ac_power_for_mqtt},
            "control/eos_dc_charge_demand": {
                "value": base_control.get_current_dc_charge_demand()
            },
            "control/eos_discharge_allowed": {
                "value": base_control.get_effective_discharge_allowed()
            },
        }
    )

    last_control_data["current_soc"] = current_soc
    last_control_data["ac_charge_demand"] = ac_charge_demand_rel
    last_control_data["dc_charge_demand"] = dc_charge_demand_rel
    last_control_data["discharge_allowed"] = discharge_allowed


class OptimizationScheduler:
    """
    A scheduler class that manages the periodic execution of an optimization process
    in a background thread. The class is responsible for starting, stopping, and
    managing the lifecycle of the optimization service.
    Attributes:
        update_interval (int): The interval in seconds between optimization runs.
        _update_thread_optimization_loop (threading.Thread): The background thread
            running the optimization loop.
        _stop_event (threading.Event): An event used to signal the thread to stop.
    Methods:
        __start_update_service_optimization_loop():
        shutdown():
        _update_state_loop():
        __run_optimization_loop():
    """

    def __init__(self, update_interval):
        self.update_interval = update_interval
        self.last_request_response = {
            "request": json.dumps(
                {
                    "status": "Awaiting first optimization run",
                },
                indent=4,
            ),
            "response": json.dumps(
                {
                    "status": "starting up",
                    "message": (
                        "The first request has been sent to EOS and is now waiting for "
                        "the completion of the first optimization run."
                    ),
                },
                indent=4,
            ),
        }
        self.current_state = {
            "request_state": None,
            "last_request_timestamp": None,
            # initialize with startup time stamp to avoid confusion in gui
            "last_response_timestamp": datetime.now(time_zone).isoformat(),
            "next_run": None,
        }
        self._update_thread_optimization_loop = None
        self._stop_event = threading.Event()
        self._last_avg_runtime = 120  # Initialize with a default value
        self._last_dyn_override_array = []  # Initialize override array for chart
        self.__start_update_service_optimization_loop()
        self._update_thread_control_loop = None
        self._stop_event_control_loop = threading.Event()
        self.__start_update_service_control_loop()
        self._update_thread_data_loop = None
        self._stop_event_data_loop = threading.Event()
        self.__start_update_service_data_loop()

    def get_last_request_response(self):
        """
        Returns the last request response.
        """
        return self.last_request_response

    def get_current_state(self):
        """
        Returns the current state of the optimization scheduler.
        """
        return self.current_state

    def get_last_dyn_override_array(self):
        """
        Returns the last dynamic override array for all time slots.
        """
        return self._last_dyn_override_array

    def __set_state_request(self):
        """
        Sets the current state of the optimization scheduler.
        """
        self.current_state["request_state"] = "request send"
        self.current_state["last_request_timestamp"] = datetime.now(
            time_zone
        ).isoformat()

    def __set_state_response(self):
        """
        Sets the current state of the optimization scheduler.
        """
        self.current_state["request_state"] = "response received"
        self.current_state["last_response_timestamp"] = datetime.now(
            time_zone
        ).isoformat()

    def __set_state_next_run(self, next_run_time):
        """
        Sets the current state of the optimization scheduler.
        """
        self.current_state["next_run"] = next_run_time

    def __start_update_service_optimization_loop(self):
        """
        Starts the background thread to periodically update the state.
        """
        if (
            self._update_thread_optimization_loop is None
            or not self._update_thread_optimization_loop.is_alive()
        ):
            self._stop_event.clear()
            self._update_thread_optimization_loop = threading.Thread(
                target=self.__update_state_optimization_loop, daemon=True
            )
            self._update_thread_optimization_loop.start()
            logger.info("[OPTIMIZATION] Update service Optimization Run started.")

    def __update_state_optimization_loop(self):
        """
        The loop that runs in the background thread to update the state.
        """
        while not self._stop_event.is_set():
            try:
                self.__run_optimization_loop()

                # Calculate actual sleep time based on smart scheduling
                loop_now = datetime.now(time_zone)
                next_eval = eos_interface.calculate_next_run_time(
                    loop_now,
                    getattr(self, "_last_avg_runtime", 120),  # Use last known runtime
                    self.update_interval,
                )
                actual_sleep_interval = max(10, (next_eval - loop_now).total_seconds())
                self.__set_state_next_run(next_eval.astimezone(time_zone).isoformat())
                mqtt_interface.update_publish_topics(
                    {
                        "optimization/last_run": {
                            "value": self.get_current_state()["last_response_timestamp"]
                        },
                        "optimization/next_run": {
                            "value": self.get_current_state()["next_run"]
                        },
                    }
                )
                minutes, seconds = divmod(actual_sleep_interval, 60)
                logger.info(
                    "[Main] Next optimization at %s (based on average runtime of %.0f seconds)."
                    + " Sleeping for %d min %.0f seconds\n",
                    next_eval.strftime("%H:%M:%S"),
                    getattr(self, "_last_avg_runtime", 120),
                    minutes,
                    seconds,
                )

            except (requests.exceptions.RequestException, ValueError, KeyError) as e:
                logger.error("[OPTIMIZATION] Error while updating state: %s", e)
                actual_sleep_interval = self.update_interval  # Fallback on error

            # Use the calculated sleep interval instead of fixed interval
            while actual_sleep_interval > 0:
                if self._stop_event.is_set():
                    return  # Exit immediately if stop event is set
                time.sleep(min(1, actual_sleep_interval))  # Sleep in 1-second chunks
                actual_sleep_interval -= 1

        # self.__start_update_service_optimization_loop()

    def __run_optimization_loop(self):
        """
        Executes the optimization process by creating an optimization request,
        sending it to the EOS interface, processing the response, and scheduling
        the next optimization run.
        The method performs the following steps:
        1. Logs the start of a new optimization run.
        2. Creates an optimization request in JSON format and saves it to a file.
        3. Sends the optimization request to the EOS interface and retrieves the response.
        4. Adds a timestamp to the response and saves it to a file.
        5. Extracts control data from the response and, if no error is detected,
           applies the control settings and updates the control state.
        6. Calculates the time for the next optimization run and logs the sleep duration.
        Raises:
            Any exceptions raised during file operations, JSON serialization,
            or EOS interface communication will propagate to the caller.
        Notes:
            - The method assumes the presence of global variables or objects such as
              `logger`, `base_path`, `eos_interface`, `config_manager`, and `time_zone`.
            - The `config_manager.config` dictionary is expected to contain the
              necessary configuration values for "eos.timeout" and "refresh_time".
        """
        logger.info("[Main] start new run")
        # update prices
        # price_interface.update_prices(
        #     EOS_TGT_DURATION,
        #     datetime.now(time_zone).replace(hour=0, minute=0, second=0, microsecond=0),
        # )
        # create optimize request
        json_optimize_input = create_optimize_request()
        self.__set_state_request()

        with open(
            base_path + "/json/optimize_request.json", "w", encoding="utf-8"
        ) as file:
            json.dump(json_optimize_input, file, indent=4)

        mqtt_interface.update_publish_topics(
            {"optimization/state": {"value": self.get_current_state()["request_state"]}}
        )
        optimized_response, avg_runtime = eos_interface.optimize(json_optimize_input)
        # Store the runtime for use in sleep calculation (defensive against None)
        try:
            if avg_runtime is None:
                # keep previous value or default if not present
                self._last_avg_runtime = getattr(self, "_last_avg_runtime", 120)
                logger.warning(
                    "[Main] optimize() returned no avg_runtime; keeping previous value: %s",
                    self._last_avg_runtime,
                )
            else:
                self._last_avg_runtime = avg_runtime
        except (TypeError, AttributeError) as e:
            # fallback to a sensible default and log the specific error
            logger.warning(
                "[Main] Error processing avg_runtime (%s): %s. Falling back to default.",
                type(avg_runtime).__name__ if "avg_runtime" in locals() else "Unknown",
                e,
            )
            self._last_avg_runtime = 120

        json_optimize_input["timestamp"] = datetime.now(time_zone).isoformat()
        self.last_request_response["request"] = json.dumps(
            json_optimize_input, indent=4
        )
        optimized_response["timestamp"] = datetime.now(time_zone).isoformat()
        self.last_request_response["response"] = json.dumps(
            optimized_response, indent=4
        )
        self.__set_state_response()

        with open(
            base_path + "/json/optimize_response.json", "w", encoding="utf-8"
        ) as file:
            json.dump(optimized_response, file, indent=4)
        # +++++++++
        (
            ac_charge_demand,
            dc_charge_demand,
            discharge_allowed,
            error,
            dyn_override_array,
        ) = eos_interface.examine_response_to_control_data(optimized_response)
        if error is not True:
            setting_control_data(ac_charge_demand, dc_charge_demand, discharge_allowed)
            # Store the override array for API response
            self._last_dyn_override_array = dyn_override_array
            # get recent evcc states
            base_control.set_current_evcc_charging_state(
                evcc_interface.get_charging_state()
            )
            base_control.set_current_evcc_charging_mode(
                evcc_interface.get_charging_mode()
            )
            # change_control_state() # -> moved to __run_control_loop

    def __start_update_service_control_loop(self):
        """
        Starts the background thread to periodically update the state.
        """
        if (
            self._update_thread_control_loop is None
            or not self._update_thread_control_loop.is_alive()
        ):
            self._stop_event_control_loop.clear()
            self._update_thread_control_loop = threading.Thread(
                target=self.__update_state_loop_control_loop, daemon=True
            )
            self._update_thread_control_loop.start()
            logger.info("[OPTIMIZATION] Update service Control started.")

    def __update_state_loop_control_loop(self):
        """
        The loop that runs in the background thread to update the state.
        """
        while not self._stop_event_control_loop.is_set():
            try:
                self.__run_control_loop()
            except (requests.exceptions.RequestException, ValueError, KeyError) as e:
                logger.error("[OPTIMIZATION] Error while running control loop: %s", e)
                # Break the sleep interval into smaller chunks to allow immediate shutdown
            sleep_interval = 1
            while sleep_interval > 0:
                if self._stop_event_control_loop.is_set():
                    return  # Exit immediately if stop event is set
                time.sleep(min(1, sleep_interval))  # Sleep in 1-second chunks
                sleep_interval -= 1
        self.__start_update_service_control_loop()

    def __run_control_loop(self):
        current_hour = datetime.now(time_zone).hour
        last_control_selected_entry = 0
        if current_hour == eos_interface.get_last_control_data()[1]["hour"]:
            last_control_selected_entry = 1
        elif -1 == eos_interface.get_last_control_data()[0]["hour"]:
            # logger.debug("[Main] check current tgt ctrl - still in startup - skip")
            return
        elif (
            current_hour != eos_interface.get_last_control_data()[0]["hour"]
            and current_hour != eos_interface.get_last_control_data()[1]["hour"]
        ):
            logger.warning(
                "[Main] check current tgt ctrl - wrong hour data for fast control - skip"
            )
            return

        ac_charge_demand = eos_interface.get_last_control_data()[
            last_control_selected_entry
        ]["ac_charge_demand"]
        dc_charge_demand = eos_interface.get_last_control_data()[
            last_control_selected_entry
        ]["dc_charge_demand"]
        discharge_allowed = eos_interface.get_last_control_data()[
            last_control_selected_entry
        ]["discharge_allowed"]
        error = eos_interface.get_last_control_data()[last_control_selected_entry][
            "error"
        ]

        if (
            ac_charge_demand is None
            or dc_charge_demand is None
            or discharge_allowed is None
        ):
            logger.warning(
                "[Main] check current tgt ctrl - missing data for fast control - skip"
            )
            return

        if ac_charge_demand < 0 or dc_charge_demand < 0:
            logger.warning(
                "[Main] check current tgt ctrl - invalid data for fast control - skip"
            )
            return

        if error is not True:
            # logger.debug(
            #     "[Main] Optimization fast control loop - current state: %s (Num: %s) "+
            #     "-> ac_charge_demand: %s, dc_charge_demand: %s, discharge_allowed: %s",
            #     base_control.get_current_overall_state(),
            #     base_control.get_current_overall_state_number(),
            #     ac_charge_demand,
            #     dc_charge_demand,
            #     discharge_allowed,
            # )
            setting_control_data(ac_charge_demand, dc_charge_demand, discharge_allowed)
            # get recent evcc states
            base_control.set_current_evcc_charging_state(
                evcc_interface.get_charging_state()
            )
            base_control.set_current_evcc_charging_mode(
                evcc_interface.get_charging_mode()
            )
            change_control_state()
        # logger.debug(
        #     "[Main] Optimization control loop - secondly check - current state: %s (Num: %s)",
        #     base_control.get_current_overall_state(),
        #     base_control.get_current_overall_state_number(),
        # )

    def __start_update_service_data_loop(self):
        """
        Starts the background thread to periodically update the state.
        """
        if (
            self._update_thread_data_loop is None
            or not self._update_thread_data_loop.is_alive()
        ):
            self._stop_event_data_loop.clear()
            self._update_thread_data_loop = threading.Thread(
                target=self.__update_state_loop_data_loop, daemon=True
            )
            self._update_thread_data_loop.start()
            logger.info("[OPTIMIZATION] Update service Data started.")

    def __update_state_loop_data_loop(self):
        """
        The loop that runs in the background thread to update the state.
        """
        while not self._stop_event_data_loop.is_set():
            try:
                self.__run_data_loop()
            except (requests.exceptions.RequestException, ValueError, KeyError) as e:
                logger.error(
                    "[OPTIMIZATION] Error while running data control loop: %s", e
                )
                # Break the sleep interval into smaller chunks to allow immediate shutdown
            sleep_interval = 15
            while sleep_interval > 0:
                if self._stop_event_data_loop.is_set():
                    return  # Exit immediately if stop event is set
                time.sleep(min(1, sleep_interval))  # Sleep in 1-second chunks
                sleep_interval -= 1
        self.__start_update_service_data_loop()

    def __run_data_loop(self):
        if (
            inverter_interface is not None
            and inverter_interface.supports_extended_monitoring
        ):
            inverter_interface.fetch_inverter_data()
            mqtt_interface.update_publish_topics(
                {
                    "inverter/special/temperature_inverter": {
                        "value": inverter_interface.get_inverter_current_data()[
                            "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"
                        ]
                    },
                    "inverter/special/temperature_ac_module": {
                        "value": inverter_interface.get_inverter_current_data()[
                            "MODULE_TEMPERATURE_MEAN_01_F32"
                        ]
                    },
                    "inverter/special/temperature_dc_module": {
                        "value": inverter_interface.get_inverter_current_data()[
                            "MODULE_TEMPERATURE_MEAN_03_F32"
                        ]
                    },
                    "inverter/special/temperature_battery_module": {
                        "value": inverter_interface.get_inverter_current_data()[
                            "MODULE_TEMPERATURE_MEAN_04_F32"
                        ]
                    },
                    "inverter/special/fan_control_01": {
                        "value": inverter_interface.get_inverter_current_data()[
                            "FANCONTROL_PERCENT_01_F32"
                        ]
                    },
                    "inverter/special/fan_control_02": {
                        "value": inverter_interface.get_inverter_current_data()[
                            "FANCONTROL_PERCENT_02_F32"
                        ]
                    },
                }
            )
            # logger.debug(
            #     "[Main] Inverter data fetched - %s",
            #     inverter_interface.get_inverter_current_data(),
            # )

    def shutdown(self):
        """
        Stops the background thread and shuts down the update service.
        """
        if (
            self._update_thread_optimization_loop
            and self._update_thread_optimization_loop.is_alive()
        ):
            self._stop_event.set()
            self._update_thread_optimization_loop.join()
            logger.info("[OPTIMIZATION] Update service Optimization Loop stopped.")
        if (
            self._update_thread_control_loop
            and self._update_thread_control_loop.is_alive()
        ):
            self._stop_event_control_loop.set()
            self._update_thread_control_loop.join()
            logger.info("[OPTIMIZATION] Update service Control Loop stopped.")
        if self._update_thread_data_loop and self._update_thread_data_loop.is_alive():
            self._stop_event_data_loop.set()
            self._update_thread_data_loop.join()
            logger.info("[OPTIMIZATION] Update service Data Loop stopped.")


optimization_scheduler = OptimizationScheduler(
    config_manager.config["refresh_time"] * 60  # convert to seconds
)


def change_control_state():
    """
    Adjusts the control state of the inverter based on the current overall state.

    This function checks the current overall state of the inverter and performs
    the corresponding action. The possible states and their actions are:
    - MODE_CHARGE_FROM_GRID (state 0): Sets the inverter to charge from the grid
      with the specified AC charge demand.
    - MODE_AVOID_DISCHARGE (state 1): Sets the inverter to avoid discharge.
    - MODE_DISCHARGE_ALLOWED (state 2): Sets the inverter to allow discharge.
    - Uninitialized state (state < 0): Logs a warning indicating that the inverter
      mode is not initialized yet.

    Returns:
        bool: True if the state was changed recently and an action was performed,
              False otherwise.
    """
    inverter_fronius_en = False
    inverter_evcc_en = False
    # Check if we have an active inverter (Fronius) or if EVCC/display-only mode is enabled
    if inverter_interface is not None:
        if isinstance(inverter_interface, EvccInverter):
            inverter_evcc_en = True
        elif isinstance(inverter_interface, NullInverter):
            inverter_evcc_en = True
        else:
            # Real inverter (Fronius, Victron, etc.)
            inverter_fronius_en = True

    current_overall_state = base_control.get_current_overall_state_number()
    current_overall_state_text = base_control.get_current_overall_state()

    mqtt_interface.update_publish_topics(
        {
            "control/overall_state": {
                "value": base_control.get_current_overall_state_number()
            },
            "optimization/state": {
                "value": optimization_scheduler.get_current_state()["request_state"]
            },
            # "control/override_remain_time": {"value": "01:00"},
            # "control/override_charge_power": {
            #     "value": base_control.get_current_ac_charge_demand()
            # },
            "control/override_active": {
                "value": base_control.get_override_active_and_endtime()[0]
            },
            "control/override_end_time": {
                "value": (
                    datetime.fromtimestamp(
                        base_control.get_override_active_and_endtime()[1], time_zone
                    )
                ).isoformat()
            },
            "control/eos_homeappliance_released": {
                "value": eos_interface.get_home_appliance_released()
            },
            "control/eos_homeappliance_start_hour": {
                "value": eos_interface.get_home_appliance_start_hour()
            },
            "battery/soc": {"value": battery_interface.get_current_soc()},
            "battery/remaining_energy": {
                "value": battery_interface.get_current_usable_capacity()
            },
            "battery/dyn_max_charge_power": {
                "value": battery_interface.get_max_charge_power()
            },
            "battery/soc_min": {"value": battery_interface.get_min_soc()},
            "battery/soc_max": {"value": battery_interface.get_max_soc()},
            "status": {"value": "online"},
        }
    )

    # get the current ac/dc charge demand and for setting to inverter according
    # to the max dynamic charge power of the battery based on SOC
    tgt_ac_charge_power = min(
        base_control.get_needed_ac_charge_power(),
        round(battery_interface.get_max_charge_power()),
        config_manager.config["inverter"]["max_grid_charge_rate"],
    )
    # When pv_battery_charge_control_enabled is False, ignore the optimizer's dc_charge
    # signal and always allow full PV charging (other inverters don't enforce it anyway).
    _pv_charge_ctrl_enabled = eos_interface.pv_battery_charge_control_enabled
    if _pv_charge_ctrl_enabled:
        tgt_dc_charge_power = min(
            base_control.get_current_dc_charge_demand(),
            round(battery_interface.get_max_charge_power()),
            config_manager.config["inverter"]["max_pv_charge_rate"],
        )
    else:
        tgt_dc_charge_power = config_manager.config["inverter"]["max_pv_charge_rate"]

    # Update current battery max to actual capability (after SOC/temp derating)
    # This allows get_needed_ac_charge_power() to properly cap calculated demand
    base_control.set_current_bat_charge_max(
        round(battery_interface.get_max_charge_power())
    )

    # Check if the overall state of the inverter was changed recently and consume the event
    if base_control.was_overall_state_changed_recently(consume=True):
        logger.debug("[Main] Overall state changed recently")
        # MODE_CHARGE_FROM_GRID
        if current_overall_state == 0:
            if inverter_fronius_en:
                inverter_interface.set_mode_force_charge(tgt_ac_charge_power)
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("force_charge")
            logger.info(
                "[Main] Inverter mode set to %s with %s W (_____|||||_____)",
                current_overall_state_text,
                tgt_ac_charge_power,
            )
        # MODE_AVOID_DISCHARGE
        elif current_overall_state == 1:
            if inverter_fronius_en:
                inverter_interface.api_set_max_pv_charge_rate(tgt_dc_charge_power)
                inverter_interface.set_mode_avoid_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("avoid_discharge")
            logger.info(
                "[Main] Inverter mode set to %s, PV charge rate: %s W (_____-----_____)",
                current_overall_state_text,
                tgt_dc_charge_power,
            )
        # MODE_DISCHARGE_ALLOWED
        elif current_overall_state == 2:
            if inverter_fronius_en:
                inverter_interface.api_set_max_pv_charge_rate(tgt_dc_charge_power)
                inverter_interface.set_mode_allow_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("discharge_allowed")
            logger.info(
                "[Main] Inverter mode set to %s, PV charge rate: %s W (_____+++++_____)",
                current_overall_state_text,
                tgt_dc_charge_power,
            )
        # MODE_AVOID_DISCHARGE_EVCC_FAST
        elif current_overall_state == 3:
            if inverter_fronius_en:
                inverter_interface.api_set_max_pv_charge_rate(tgt_dc_charge_power)
                inverter_interface.set_mode_avoid_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("avoid_discharge")
            logger.info(
                "[Main] Inverter mode set to %s, PV charge rate: %s W (_____+---+_____)",
                current_overall_state_text,
                tgt_dc_charge_power,
            )
        # MODE_DISCHARGE_ALLOWED_EVCC_PV
        elif current_overall_state == 4:
            if inverter_fronius_en:
                inverter_interface.api_set_max_pv_charge_rate(tgt_dc_charge_power)
                inverter_interface.set_mode_allow_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("discharge_allowed")
            logger.info(
                "[Main] Inverter mode set to %s, PV charge rate: %s W (_____-+++-_____)",
                current_overall_state_text,
                tgt_dc_charge_power,
            )
        # MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV
        elif current_overall_state == 5:
            if inverter_fronius_en:
                inverter_interface.api_set_max_pv_charge_rate(tgt_dc_charge_power)
                inverter_interface.set_mode_allow_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("discharge_allowed")
            logger.info(
                "[Main] Inverter mode set to %s, PV charge rate: %s W (_____+-+-+_____)",
                current_overall_state_text,
                tgt_dc_charge_power,
            )
        # MODE_CHARGE_FROM_GRID_EVCC_FAST
        elif current_overall_state == 6:
            if inverter_fronius_en:
                inverter_interface.set_mode_force_charge(tgt_ac_charge_power)
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("force_charge")
            logger.info(
                "[Main] Inverter mode set to %s with %s W (_____|---|_____)",
                current_overall_state_text,
                tgt_ac_charge_power,
            )
        elif current_overall_state < 0:
            logger.warning("[Main] Inverter mode not initialized yet")
        return True

    # Log the current state if no recent changes were made
    if datetime.now().minute % 5 == 0 and datetime.now().second == 0:
        logger.info(
            "[Main] Overall state not changed recently"
            + " - remaining in current state: %s  (_____OOOOO_____)",
            current_overall_state_text,
        )
    return False


# setting the callbacks for the interfaces
battery_interface.on_bat_max_changed = battery_state_callback
evcc_interface.on_charging_state_change = charging_state_callback
mqtt_interface.on_mqtt_command = mqtt_control_callback

# web server
app = Flask(__name__)

# Ensure JSON responses preserve dict insertion order (SECTION_META order)
app.config['JSON_SORT_KEYS'] = False

# Phase 2: register the Flask REST API now that app exists.
try:
    config_web.start_api(app)
except Exception:
    logger.exception("[Main] Config web API registration failed — config UI unavailable")

# Register hot-reload: live config changes are applied without restart
from config_web.hot_reload import HotReloadAdapter  # pylint: disable=wrong-import-position

hot_reload_adapter = HotReloadAdapter(
    price_interface=price_interface,
    battery_interface=battery_interface,
    pv_interface=pv_interface,
    optimization_interface=eos_interface,
    config_provider=config_web.get_config,
)
config_web.register_hot_reload_callback(hot_reload_adapter.on_config_changed)

ASSET_CACHE_MAX_AGE_SECONDS = 31536000


# legacy web site support
@app.route("/index_legacy.html", methods=["GET"])
def main_page_legacy():
    """
    Renders the main page of the web application.

    This function reads the content of the 'index.html' file located in the 'web' directory
    and returns it as a rendered template string.
    """
    with open(base_path + "/web/index_legacy.html", "r", encoding="utf-8") as html_file:
        rendered_html = render_template_string(
            html_file.read(), asset_version=__version__
        )
    response = make_response(rendered_html)
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# new web site support


@app.route("/", methods=["GET"])
def main_page():
    """
    Renders the main page of the web application.

    This function reads the content of the 'index.html' file located in the 'web' directory
    and returns it as a rendered template string.
    """
    with open(base_path + "/web/index.html", "r", encoding="utf-8") as html_file:
        rendered_html = render_template_string(
            html_file.read(), asset_version=__version__
        )
    response = make_response(rendered_html)
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/js/<filename>")
def serve_js_files(filename):
    """
    Dynamically serve JavaScript files from the js directory.
    This allows adding new JS modules without modifying the server code.
    """
    try:
        js_directory = os.path.join(os.path.dirname(__file__), "web", "js")

        # Security check: only allow .js files
        if not filename.endswith(".js"):
            logger.warning("[Web] Blocked attempt to serve non-JS file: %s", filename)
            return "Not Found", 404

        # Check if file exists
        file_path = os.path.join(js_directory, filename)
        if not os.path.exists(file_path):
            logger.warning("[Web] JavaScript file not found: %s", filename)
            return "Not Found", 404

        # logger.debug("[Web] Serving JavaScript file: %s", filename)
        return send_from_directory(
            js_directory,
            filename,
            mimetype="application/javascript",
            max_age=ASSET_CACHE_MAX_AGE_SECONDS,
        )

    except (OSError, IOError, ValueError) as e:
        logger.error("[Web] Error serving JavaScript file %s: %s", filename, e)
        return "Server Error", 500


# Also add CSS file serving for completeness
@app.route("/css/<filename>")
def serve_css_files(filename):
    """
    Dynamically serve CSS files from the web directory.
    """
    try:
        web_directory = os.path.join(os.path.dirname(__file__), "web", "css")

        # Security check: only allow .css files
        if not filename.endswith(".css"):
            logger.warning("[Web] Blocked attempt to serve non-CSS file: %s", filename)
            return "Not Found", 404

        # Check if file exists
        file_path = os.path.join(web_directory, filename)
        if not os.path.exists(file_path):
            logger.warning("[Web] CSS file not found: %s", filename)
            return "Not Found", 404

        # logger.debug("[Web] Serving CSS file: %s", filename)
        return send_from_directory(
            web_directory,
            filename,
            mimetype="text/css",
            max_age=ASSET_CACHE_MAX_AGE_SECONDS,
        )

    except (OSError, IOError, ValueError) as e:
        logger.error("[Web] Error serving CSS file %s: %s", filename, e)
        return "Server Error", 500


@app.route("/json/optimize_request.json", methods=["GET"])
def get_optimize_request():
    """
    Retrieves the last optimization request and returns it as a JSON response.
    """
    return Response(
        optimization_scheduler.get_last_request_response()["request"],
        content_type="application/json",
    )


@app.route("/json/optimize_response.json", methods=["GET"])
def get_optimize_response():
    """
    Retrieves the last optimization response and returns it as a JSON response.
    """
    return Response(
        optimization_scheduler.get_last_request_response()["response"],
        content_type="application/json",
    )


@app.route("/json/optimize_request.test.json", methods=["GET"])
def get_optimize_request_test():
    """
    Retrieves the last optimization request and returns it as a JSON response.
    """
    with open(
        base_path + "/json/optimize_request.test.json", "r", encoding="utf-8"
    ) as file:
        return Response(
            file.read(),
            content_type="application/json",
        )


@app.route("/json/optimize_response.test.json", methods=["GET"])
def get_optimize_response_test():
    """
    Retrieves the last optimization response and returns it as a JSON response.
    """
    with open(
        base_path + "/json/optimize_response.test.json", "r", encoding="utf-8"
    ) as file:
        return Response(
            file.read(),
            content_type="application/json",
        )


@app.route("/json/current_controls.json", methods=["GET"])
def get_controls():
    """
    Returns the current demands for AC and DC charging as a JSON response.
    """
    current_ac_charge_demand = base_control.get_current_ac_charge_demand()
    current_dc_charge_demand = base_control.get_current_dc_charge_demand()
    # Use effective discharge allowed state (reflects final state after EVCC/manual overrides)
    current_discharge_allowed = base_control.get_effective_discharge_allowed()
    current_battery_soc = battery_interface.get_current_soc()
    base_control.set_current_battery_soc(current_battery_soc)
    current_inverter_mode = base_control.get_current_overall_state()
    current_inverter_mode_num = base_control.get_current_overall_state_number()

    # Get current actual power (logging handled in get_needed_ac_charge_power with change tracking)
    actual_power = base_control.get_needed_ac_charge_power()

    currency = price_interface.get_price_currency()
    currency_symbol = CURRENCY_SYMBOL_MAP.get(currency, currency)
    currency_minor_unit = CURRENCY_MINOR_UNIT_MAP.get(currency, f"{currency}")

    response_data = {
        "current_states": {
            "current_ac_charge_demand": current_ac_charge_demand,
            "current_ac_charge_power": actual_power,  # Power in W, not energy
            "current_dc_charge_demand": current_dc_charge_demand,
            "current_discharge_allowed": current_discharge_allowed,
            "inverter_mode": current_inverter_mode,
            "inverter_mode_num": current_inverter_mode_num,
            "override_active": base_control.get_override_active_and_endtime()[0],
            "override_end_time": base_control.get_override_active_and_endtime()[1],
            "dyn_override_discharge_allowed_enabled": eos_interface.dyn_override_discharge_allowed,
            "pv_battery_charge_control_enabled": eos_interface.pv_battery_charge_control_enabled,
            "dyn_override_discharge_allowed_active": eos_interface.get_last_control_data()[
                0
            ].get(
                "dyn_override_active", False
            ),
            "dyn_override_discharge_allowed_array": optimization_scheduler.get_last_dyn_override_array(),
        },
        "evcc": {
            "charging_state": base_control.get_current_evcc_charging_state(),
            "charging_mode": base_control.get_current_evcc_charging_mode(),
            "current_sessions": evcc_interface.get_current_detail_data(),
        },
        "battery": {
            "soc": current_battery_soc,
            "capacity_wh": config_manager.config["battery"].get("capacity_wh", 0),
            "usable_capacity": battery_interface.get_current_usable_capacity(),
            "max_charge_power_dyn": battery_interface.get_max_charge_power(),
            "max_charge_power_fix": config_manager.config["battery"].get(
                "max_charge_power_w", 0
            ),
            "charging_curve_enabled": config_manager.config["battery"].get(
                "charging_curve_enabled", True
            ),
            "temperature": battery_interface.current_temp,
            "max_grid_charge_rate": config_manager.config["inverter"][
                "max_grid_charge_rate"
            ],
            "stored_energy": battery_interface.get_stored_energy_info(),
        },
        "inverter": {
            "inverter_special_data": (
                inverter_interface.get_inverter_current_data()
                if inverter_interface is not None
                and inverter_interface.supports_extended_monitoring
                else None
            )
        },
        "localization": {
            "currency": currency,
            "currency_symbol": currency_symbol,
            "currency_minor_unit": currency_minor_unit,
        },
        "state": optimization_scheduler.get_current_state(),
        "used_optimization_source": config_manager.config.get("eos", {}).get(
            "source", "eos_server"
        ),
        "used_time_frame_base": time_frame_base,
        "eos_connect_version": __version__,
        "timestamp": datetime.now(time_zone).isoformat(),
        "api_version": "0.0.4",
    }
    return Response(
        json.dumps(response_data, indent=4), content_type="application/json"
    )


@app.route("/json/price_info.json", methods=["GET"])
def get_price_info():
    """
    Returns price forecast metadata for UI visualization.

    Provides information about whether prices are real data, simple repetition,
    or smart forecasted, to help distinguish in charts and tables.
    """
    forecast_metadata = price_interface.get_forecast_metadata()

    response_data = {
        "forecast_start_index": forecast_metadata.get("forecast_start_index"),
        "forecast_type": forecast_metadata.get("forecast_type"),
        "forecast_source": forecast_metadata.get("forecast_source"),
        "timestamp": datetime.now(time_zone).isoformat(),
        "api_version": "0.0.1",
    }
    response = Response(
        json.dumps(response_data, indent=4), content_type="application/json"
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/json/test/<filename>")
def serve_test_json_files(filename):
    """
    Dynamically serve test JSON files from the json directory.
    This allows adding new test JSON files without modifying the server code.
    Supports all test files like current_controls.test.json, optimize_request.test.json, etc.
    """
    try:
        # Test files are in the json/test/ subdirectory
        json_test_directory = os.path.join(os.path.dirname(__file__), "json", "test")

        # Security check: only allow .json files
        if not filename.endswith(".json"):
            logger.warning("[Web] Blocked attempt to serve non-JSON file: %s", filename)
            return Response(
                '{"error": "Invalid file type"}',
                status=400,
                content_type="application/json",
            )

        # Additional security: only allow files with .test.json ending
        # (all test files must follow this naming convention)
        if not filename.endswith(".test.json"):
            logger.warning(
                "[Web] Blocked attempt to serve non-test JSON file: %s", filename
            )
            return Response(
                '{"error": "Access denied - not a test file"}',
                status=403,
                content_type="application/json",
            )

        # Check if file exists in test directory
        file_path = os.path.join(json_test_directory, filename)
        if not os.path.exists(file_path):
            logger.warning("[Web] Test JSON file not found: %s", filename)
            logger.debug("[Web] Looked in directory: %s", json_test_directory)
            return Response(
                '{"error": "Test file not found"}',
                status=404,
                content_type="application/json",
            )

        # logger.info("[Web] Serving test JSON file: %s from %s", filename, json_test_directory)
        return send_from_directory(
            json_test_directory, filename, mimetype="application/json"
        )

    except (OSError, IOError, ValueError) as e:
        logger.error("[Web] Error serving test JSON file %s: %s", filename, e)
        return Response(
            '{"error": "Server error"}', status=500, content_type="application/json"
        )


@app.route("/controls/mode_override", methods=["POST"])
def handle_mode_override():
    """
    Handles a POST request to override the inverter mode.

    Expects a JSON payload with the following structure:
    {
        "mode": <int>,  # The mode to override (0, 1, 2, etc.)
        "duration": <int>  # Duration in minutes for the override
    }

    Returns:
        A JSON response indicating success or failure.
    """
    try:
        data = request.get_json()
        if (
            not data
            or "mode" not in data
            or "duration" not in data
            or "grid_charge_power" not in data
        ):
            return Response(
                json.dumps({"error": "Invalid payload"}),
                status=400,
                content_type="application/json",
            )

        mode = int(data["mode"])
        duration_string = data["duration"]  # 00:00, 00:30, 01:00 ...
        duration_hh = duration_string.split(":")[0]
        duration_mm = duration_string.split(":")[1]
        duration = int(duration_hh) * 60 + int(duration_mm)
        grid_charge_power = float(data["grid_charge_power"])

        # Validate mode and duration
        if mode < -2 or mode > 2:
            return Response(
                json.dumps({"error": "Invalid mode value"}),
                status=400,
                content_type="application/json",
            )
        if duration <= 0 and duration <= 12 * 60:
            return Response(
                json.dumps(
                    {
                        "error": "Duration must be greater than 0 and less/ equal than 12 hours"
                    }
                ),
                status=400,
                content_type="application/json",
            )
        if (
            grid_charge_power < 0.5
            and grid_charge_power
            <= config_manager.config["inverter"]["max_grid_charge_rate"] / 1000
        ):
            return Response(
                json.dumps(
                    {
                        "error": "Grid charge power must be greater than 0"
                        + " and less / equal than max grid charge rate"
                    }
                ),
                status=400,
                content_type="application/json",
            )

        # Apply the override
        base_control.set_override_charge_rate(grid_charge_power)
        base_control.set_override_duration(duration)
        base_control.set_mode_override(mode)
        change_control_state()
        if mode == -1:
            logger.info("[Main] Mode override deactivated")
        else:
            logger.info(
                "[Main] Mode override applied: mode=%s, duration=%d minutes",
                base_control.get_state_mapping(mode),
                duration,
            )

        return Response(
            json.dumps({"status": "success", "message": "Mode override applied"}),
            content_type="application/json",
        )
    except ValueError as e:
        logger.error("[Main] Value error in mode override: %s", e)
        return Response(
            json.dumps({"error": "Invalid input"}),
            status=400,
            content_type="application/json",
        )
    except TypeError as e:
        logger.error("[Main] Type error in mode override: %s", e)
        return Response(
            json.dumps({"error": "Invalid data type"}),
            status=400,
            content_type="application/json",
        )
    except KeyError as e:
        logger.error("[Main] Key error in mode override: %s", e)
        return Response(
            json.dumps({"error": "Missing or invalid key in input data"}),
            status=400,
            content_type="application/json",
        )


@app.route("/logs", methods=["GET"])
def get_logs():
    """
    Retrieve application logs with optional filtering.

    Query parameters:
    - level: Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    - limit: Maximum number of records to return (default: 100)
    - since: ISO timestamp to get logs since that time
    """
    try:
        level_filter = request.args.get("level")
        limit = int(request.args.get("limit", 100))
        since = request.args.get("since")

        logs = memory_handler.get_logs(
            level_filter=level_filter, limit=limit, since=since
        )

        response_data = {
            "logs": logs,
            "total_count": len(logs),
            "timestamp": datetime.now(time_zone).isoformat(),
            "filters_applied": {"level": level_filter, "limit": limit, "since": since},
        }

        return Response(
            json.dumps(response_data, indent=2), content_type="application/json"
        )

    except (ValueError, TypeError, KeyError) as e:
        logger.error("[Web] Error retrieving logs: %s", e)
        return Response(
            json.dumps({"error": "Failed to retrieve logs"}),
            status=500,
            content_type="application/json",
        )


@app.route("/logs/alerts", methods=["GET"])
def get_alerts():
    """
    Retrieve warning and error logs for alert system.
    """
    try:
        alerts = memory_handler.get_alerts()

        # Group alerts by level for easier processing
        grouped_alerts = {
            "WARNING": [a for a in alerts if a["level"] == "WARNING"],
            "ERROR": [a for a in alerts if a["level"] == "ERROR"],
            "CRITICAL": [a for a in alerts if a["level"] == "CRITICAL"],
        }

        response_data = {
            "alerts": alerts,
            "grouped_alerts": grouped_alerts,
            "alert_counts": {
                level: len(items) for level, items in grouped_alerts.items()
            },
            "timestamp": datetime.now(time_zone).isoformat(),
        }

        return Response(
            json.dumps(response_data, indent=2), content_type="application/json"
        )

    except (ValueError, TypeError, KeyError) as e:
        logger.error("[Web] Error retrieving alerts: %s", e)
        return Response(
            json.dumps({"error": "Failed to retrieve alerts"}),
            status=500,
            content_type="application/json",
        )


@app.route("/logs/clear", methods=["POST"])
def clear_logs():
    """
    Clear all stored logs from memory (file logs remain intact).
    """
    try:
        memory_handler.clear_logs()
        logger.info("[Web] Memory logs cleared via web API")

        return Response(
            json.dumps({"status": "success", "message": "Logs cleared"}),
            content_type="application/json",
        )

    except (RuntimeError, ValueError, TypeError, KeyError) as e:
        logger.error("[Web] Error clearing logs: %s", e)
        return Response(
            json.dumps({"error": "Failed to clear logs"}),
            status=500,
            content_type="application/json",
        )


@app.route("/logs/alerts/clear", methods=["POST"])
def clear_alerts_only():
    """
    Clear only alert logs from memory, keeping regular logs intact.
    """
    try:
        memory_handler.clear_alerts_only()
        logger.info("[Web] Alert logs cleared via web API")

        return Response(
            json.dumps({"status": "success", "message": "Alert logs cleared"}),
            content_type="application/json",
        )

    except (RuntimeError, ValueError, TypeError, KeyError) as e:
        logger.error("[Web] Error clearing alert logs: %s", e)
        return Response(
            json.dumps({"error": "Failed to clear alert logs"}),
            status=500,
            content_type="application/json",
        )


@app.route("/logs/stats", methods=["GET"])
def get_log_stats():
    """
    Get buffer usage statistics.
    """
    try:
        stats = memory_handler.get_buffer_stats()

        response_data = {
            "buffer_stats": stats,
            "timestamp": datetime.now(time_zone).isoformat(),
        }

        return Response(
            json.dumps(response_data, indent=2), content_type="application/json"
        )

    except (ValueError, TypeError, KeyError) as e:
        logger.error("[Web] Error retrieving buffer stats: %s", e)
        return Response(
            json.dumps({"error": "Failed to retrieve buffer stats"}),
            status=500,
            content_type="application/json",
        )


@app.route("/api/update/status", methods=["GET"])
def get_update_status():
    """
    Get update checker status.

    Returns information about available updates for Docker image users.
    For Home Assistant Add-on users, this will indicate that update checking
    is handled by Home Assistant.

    Returns:
        JSON response with update status including:
        - enabled: Whether update checking is active
        - is_ha_addon: Whether running as HA Add-on
        - current_version: Current EOS Connect version
        - is_develop_branch: Whether on develop or stable branch
        - update_available: Whether an update is available
        - latest_version: Latest available version (if any)
        - last_check_time: Unix timestamp of last check
        - last_check_success: Whether last check succeeded
        - last_error: Error message from last check (if any)
        - next_check_in_seconds: Seconds until next automatic check
    """
    try:
        status = update_checker.get_update_status()

        response_data = {
            "update_status": status,
            "timestamp": datetime.now(time_zone).isoformat(),
            "api_version": "0.0.1",
        }

        response = Response(
            json.dumps(response_data, indent=2), content_type="application/json"
        )
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    except (ValueError, TypeError, KeyError, AttributeError) as e:
        logger.error("[Web] Error retrieving update status: %s", e)
        return Response(
            json.dumps(
                {"error": "Failed to retrieve update status", "message": str(e)}
            ),
            status=500,
            content_type="application/json",
        )


if __name__ == "__main__":
    http_server = None
    try:
        # Create web server with port checking
        HOST = "0.0.0.0"
        # In HA addon mode, port is always 8081 (mapped via ports: config)
        # In local/Docker mode, use the configured port
        desired_port = config_manager.config.get("eos_connect_web_port", 8081)

        logger.info("[Main] Initializing EOS Connect web server...")
        http_server, actual_port = PortInterface.create_web_server_with_port_check(
            HOST, desired_port, app, logger
        )

        logger.info(
            "[Main] EOS Connect web server successfully created on %s:%s",
            HOST,
            actual_port,
        )
        logger.info(
            "[Main] Web interface available at: http://localhost:%s", actual_port
        )

        # Start serving
        logger.info("[Main] Starting EOS Connect web server...")
        http_server.serve_forever()

    except RuntimeError as e:
        # PortInterface already provides detailed error messages and solutions
        logger.error("[Main] %s", str(e))
        logger.error("[Main] EOS Connect cannot start without its web interface.")
        sys.exit(1)

    except (OSError, ImportError) as e:
        # Only handle truly unexpected errors (not port-related)
        logger.error("[Main] Unexpected error: %s", str(e))
        logger.error("[Main] EOS Connect cannot start. Please check the logs.")
        sys.exit(1)

    except KeyboardInterrupt:
        logger.info("[Main] Shutting down EOS Connect (user requested)")
        optimization_scheduler.shutdown()
        base_control.shutdown()
        if http_server:
            http_server.stop()
            logger.info("[Main] HTTP server stopped")

        # Shutdown real inverter if it exists (not NullInverter/display-only or EvccInverter)
        if inverter_interface is not None and not isinstance(
            inverter_interface, (NullInverter, EvccInverter)
        ):
            inverter_interface.shutdown()
        pv_interface.shutdown()
        price_interface.shutdown()
        mqtt_interface.shutdown()
        evcc_interface.shutdown()
        battery_interface.shutdown()
        update_checker.shutdown()
        config_web.stop()
        logger.info("[Main] Server stopped gracefully")
    finally:
        logging.shutdown()  # This will call close() on all handlers
        logger.info("[Main] Cleanup complete. Goodbye!")
        sys.exit(0)
