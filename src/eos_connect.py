"""
This module fetches energy data from OpenHAB, processes it, and creates a load profile.
"""

import os
import sys
from datetime import datetime
import time
import logging
import json
import threading
import pytz
import requests
from flask import Flask, Response, render_template_string, request, send_from_directory
from version import __version__
from config import ConfigManager
from log_handler import MemoryLogHandler
from interfaces.base_control import BaseControl
from interfaces.load_interface import LoadInterface
from interfaces.battery_interface import BatteryInterface
from interfaces.inverter_fronius import FroniusWR
from interfaces.inverter_fronius_v2 import FroniusWRV2
from interfaces.evcc_interface import EvccInterface
from interfaces.eos_interface import EosInterface
from interfaces.price_interface import PriceInterface
from interfaces.mqtt_interface import MqttInterface
from interfaces.pv_interface import PvInterface
from interfaces.port_interface import PortInterface

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
    max_records=10000,  # All log entries (mixed levels)
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
# initialize eos interface
eos_interface = EosInterface(
    eos_server=config_manager.config["eos"]["server"],
    eos_port=config_manager.config["eos"]["port"],
    timezone=time_zone,
)
# initialize base control
base_control = BaseControl(config_manager.config, time_zone)
# initialize the inverter interface
inverter_interface = None

# Handle backward compatibility for old interface names
inverter_type = config_manager.config["inverter"]["type"]
if inverter_type == "fronius_gen24_v2":
    logger.warning(
        "[Config] Interface name 'fronius_gen24_v2' is deprecated. "
        "Please update your config.yaml to use 'fronius_gen24' instead. "
        "Using enhanced interface for compatibility."
    )
    inverter_type = "fronius_gen24"  # Auto-migrate to new name

if inverter_type == "fronius_gen24":
    # Enhanced V2 interface (default for existing users)
    logger.info(
        "[Inverter] Using enhanced Fronius GEN24 interface with firmware-based authentication"
    )
    inverter_config = {
        "address": config_manager.config["inverter"]["address"],
        "max_grid_charge_rate": config_manager.config["inverter"][
            "max_grid_charge_rate"
        ],
        "max_pv_charge_rate": config_manager.config["inverter"]["max_pv_charge_rate"],
        "user": config_manager.config["inverter"]["user"],
        "password": config_manager.config["inverter"]["password"],
    }
    inverter_interface = FroniusWRV2(inverter_config)
elif inverter_type == "fronius_gen24_legacy":
    # Legacy V1 interface (for corner cases)
    logger.info(
        "[Inverter] Using legacy Fronius GEN24 interface (V1) for compatibility"
    )
    inverter_config = {
        "address": config_manager.config["inverter"]["address"],
        "max_grid_charge_rate": config_manager.config["inverter"][
            "max_grid_charge_rate"
        ],
        "max_pv_charge_rate": config_manager.config["inverter"]["max_pv_charge_rate"],
        "user": config_manager.config["inverter"]["user"],
        "password": config_manager.config["inverter"]["password"],
    }
    inverter_interface = FroniusWR(inverter_config)
elif inverter_type == "evcc":
    logger.info(
        "[Inverter] Inverter type %s - using the universal evcc external battery control.",
        inverter_type,
    )
else:
    logger.info(
        "[Inverter] Inverter type %s - no external connection."
        + " Changing to show only mode.",
        config_manager.config["inverter"]["type"],
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
def mqtt_control_callback(command):
    """
    Handles MQTT control commands by parsing the command dictionary and updating the system's state.

    Args:
        command (dict): Contains "duration" (str, "HH:MM"), "mode" (str/int),
        and "grid_charge_power" (str/int).

    Side Effects:
        - Updates base control mode override.
        - Publishes updated control topics to MQTT.
        - Logs the event and triggers a control state change.
    """
    # Default to "02:00" if empty or None
    duration_string = command.get("duration", "02:00") or "02:00"
    duration_hh = duration_string.split(":")[0]
    duration_mm = duration_string.split(":")[1]
    duration = int(duration_hh) * 60 + int(duration_mm)
    # Default to 0 if empty or None
    charge_power = command.get("charge_power", 0) or 0
    charge_power = int(charge_power) / 1000  # convert to kW
    # update the base control with the new charging state
    base_control.set_mode_override(int(command["mode"]), duration, charge_power)
    mqtt_interface.update_publish_topics(
        {
            "control/override_charge_power": {"value": charge_power * 1000},
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
    logger.info("[MAIN] MQTT Event - control command to: %s", command["mode"])
    change_control_state()


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
    time_zone,
)

battery_interface = BatteryInterface(
    config_manager.config["battery"],
    on_bat_max_changed=None,
)

price_interface = PriceInterface(config_manager.config["price"], time_zone)

pv_interface = PvInterface(
    config_manager.config["pv_forecast_source"],
    config_manager.config["pv_forecast"],
    config_manager.config.get("evcc", {}),
    config_manager.config.get("time_zone", "UTC"),
)

# wait for the interfaces to initialize - depend on entries for pv_forecast
init_time = 3 + 1 * len(config_manager.config["pv_forecast"])
logger.info("[Main] Waiting %s seconds for interfaces to initialize", init_time)
time.sleep(init_time)

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

    def get_ems_data():
        return {
            "pv_prognose_wh": pv_interface.get_current_pv_forecast(),
            "strompreis_euro_pro_wh": price_interface.get_current_prices(),
            "einspeiseverguetung_euro_pro_wh": price_interface.get_current_feedin_prices(),
            "preis_euro_pro_wh_akku": config_manager.config["battery"][
                "price_euro_per_wh_accu"
            ],
            "gesamtlast": load_interface.get_load_profile(EOS_TGT_DURATION),
        }

    def get_pv_akku_data():
        akku_object = {
            "capacity_wh": config_manager.config["battery"]["capacity_wh"],
            "charging_efficiency": config_manager.config["battery"][
                "charge_efficiency"
            ],
            "discharging_efficiency": config_manager.config["battery"][
                "discharge_efficiency"
            ],
            "max_charge_power_w": config_manager.config["battery"][
                "max_charge_power_w"
            ],
            "initial_soc_percentage": round(battery_interface.get_current_soc()),
            "min_soc_percentage": config_manager.config["battery"][
                "min_soc_percentage"
            ],
            "max_soc_percentage": config_manager.config["battery"][
                "max_soc_percentage"
            ],
        }
        if eos_interface.get_eos_version() == ">=2025-04-09":
            akku_object = {"device_id": "battery1", **akku_object}
        return akku_object

    def get_wechselrichter_data():
        wechselrichter_object = {
            "max_power_wh": config_manager.config["inverter"]["max_pv_charge_rate"],
        }
        if eos_interface.get_eos_version() == ">=2025-04-09":
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
        if eos_interface.get_eos_version() == ">=2025-04-09":
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
        if eos_interface.get_eos_version() == ">=2025-04-09":
            dishwasher_object = {"device_id": "additional_load_1", **dishwasher_object}
        return dishwasher_object

    payload = {
        "ems": get_ems_data(),
        "pv_akku": get_pv_akku_data(),
        "inverter": get_wechselrichter_data(),
        "eauto": get_eauto_data(),
        "dishwasher": get_dishwasher_data(),
        "temperature_forecast": pv_interface.get_current_temp_forecast(),
        "start_solution": eos_interface.get_last_start_solution(),
    }
    logger.debug(
        "[Main] optimize request payload - startsolution: %s", payload["start_solution"]
    )
    return payload


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

    if current_soc >= max_soc and ac_charge_demand_rel > 0:
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
    mqtt_interface.update_publish_topics(
        {
            "control/eos_ac_charge_demand": {
                "value": base_control.get_current_ac_charge_demand()
            },
            "control/eos_dc_charge_demand": {
                "value": base_control.get_current_dc_charge_demand()
            },
            "control/eos_discharge_allowed": {
                "value": base_control.get_current_discharge_allowed()
            },
        }
    )
    # set the current battery state of charge
    base_control.set_current_battery_soc(battery_interface.get_current_soc())
    # getting the current charging state from evcc
    base_control.set_current_evcc_charging_state(evcc_interface.get_charging_state())
    base_control.set_current_evcc_charging_mode(evcc_interface.get_charging_mode())


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
        optimized_response, avg_runtime = eos_interface.eos_set_optimize_request(
            json_optimize_input, config_manager.config["eos"]["timeout"]
        )
        # Store the runtime for use in sleep calculation
        self._last_avg_runtime = avg_runtime

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
        ac_charge_demand, dc_charge_demand, discharge_allowed, error = (
            eos_interface.examine_response_to_control_data(optimized_response)
        )
        if error is not True:
            setting_control_data(ac_charge_demand, dc_charge_demand, discharge_allowed)
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
            #     "[Main] Optimization fast control loop - current state: %s (Num: %s) -> ac_charge_demand: %s, dc_charge_demand: %s, discharge_allowed: %s",
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
        if inverter_type in ["fronius_gen24", "fronius_gen24_legacy"]:
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
    if inverter_type in ["fronius_gen24", "fronius_gen24_legacy"]:
        inverter_fronius_en = True
    elif config_manager.config["inverter"]["type"] == "evcc":
        inverter_evcc_en = True

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
            "status": {"value": "online"},
        }
    )

    # get the current ac/dc charge demand and for setting to inverter according
    # to the max dynamic charge power of the battery based on SOC
    tgt_ac_charge_power = min(
        base_control.get_current_ac_charge_demand(),
        round(battery_interface.get_max_charge_power()),
    )
    tgt_dc_charge_power = min(
        base_control.get_current_dc_charge_demand(),
        round(battery_interface.get_max_charge_power()),
    )

    base_control.set_current_bat_charge_max(
        max(tgt_ac_charge_power, tgt_dc_charge_power)
    )

    # Check if the overall state of the inverter was changed recently
    if base_control.was_overall_state_changed_recently():
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
                inverter_interface.set_mode_avoid_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("avoid_discharge")
            logger.info(
                "[Main] Inverter mode set to %s (_____-----_____)",
                current_overall_state_text,
            )
        # MODE_DISCHARGE_ALLOWED
        elif current_overall_state == 2:
            if inverter_fronius_en:
                inverter_interface.api_set_max_pv_charge_rate(tgt_dc_charge_power)
                inverter_interface.set_mode_allow_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("discharge_allowed")
            logger.info(
                "[Main] Inverter mode set to %s (_____+++++_____)",
                current_overall_state_text,
            )
        # MODE_AVOID_DISCHARGE_EVCC_FAST
        elif current_overall_state == 3:
            if inverter_fronius_en:
                inverter_interface.set_mode_avoid_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("avoid_discharge")
            logger.info(
                "[Main] Inverter mode set to %s (_____+---+_____)",
                current_overall_state_text,
            )
        # MODE_DISCHARGE_ALLOWED_EVCC_PV
        elif current_overall_state == 4:
            if inverter_fronius_en:
                inverter_interface.api_set_max_pv_charge_rate(tgt_dc_charge_power)
                inverter_interface.set_mode_allow_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("discharge_allowed")
            logger.info(
                "[Main] Inverter mode set to %s (_____-+++-_____)",
                current_overall_state_text,
            )
        # MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV
        elif current_overall_state == 5:
            if inverter_fronius_en:
                inverter_interface.api_set_max_pv_charge_rate(tgt_dc_charge_power)
                inverter_interface.set_mode_allow_discharge()
            elif inverter_evcc_en:
                evcc_interface.set_external_battery_mode("discharge_allowed")
            logger.info(
                "[Main] Inverter mode set to %s (_____+-+-+_____)",
                current_overall_state_text,
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


# legacy web site support
@app.route("/index_legacy.html", methods=["GET"])
def main_page_legacy():
    """
    Renders the main page of the web application.

    This function reads the content of the 'index.html' file located in the 'web' directory
    and returns it as a rendered template string.
    """
    with open(base_path + "/web/index_legacy.html", "r", encoding="utf-8") as html_file:
        return render_template_string(html_file.read())


# new web site support


@app.route("/", methods=["GET"])
def main_page():
    """
    Renders the main page of the web application.

    This function reads the content of the 'index.html' file located in the 'web' directory
    and returns it as a rendered template string.
    """
    with open(base_path + "/web/index.html", "r", encoding="utf-8") as html_file:
        return render_template_string(html_file.read())


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
            js_directory, filename, mimetype="application/javascript"
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
        return send_from_directory(web_directory, filename, mimetype="text/css")

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
    current_discharge_allowed = base_control.get_current_discharge_allowed()
    current_battery_soc = battery_interface.get_current_soc()
    base_control.set_current_battery_soc(current_battery_soc)
    current_inverter_mode = base_control.get_current_overall_state()
    current_inverter_mode_num = base_control.get_current_overall_state_number()

    response_data = {
        "current_states": {
            "current_ac_charge_demand": current_ac_charge_demand,
            "current_dc_charge_demand": current_dc_charge_demand,
            "current_discharge_allowed": current_discharge_allowed,
            "inverter_mode": current_inverter_mode,
            "inverter_mode_num": current_inverter_mode_num,
            "override_active": base_control.get_override_active_and_endtime()[0],
            "override_end_time": base_control.get_override_active_and_endtime()[1],
        },
        "evcc": {
            "charging_state": base_control.get_current_evcc_charging_state(),
            "charging_mode": base_control.get_current_evcc_charging_mode(),
            "current_sessions": evcc_interface.get_current_detail_data(),
        },
        "battery": {
            "soc": current_battery_soc,
            "usable_capacity": battery_interface.get_current_usable_capacity(),
            "max_charge_power_dyn": battery_interface.get_max_charge_power(),
            "max_grid_charge_rate": config_manager.config["inverter"][
                "max_grid_charge_rate"
            ],
        },
        "inverter": {
            "inverter_special_data": (
                inverter_interface.get_inverter_current_data()
                if inverter_type in ["fronius_gen24", "fronius_gen24_legacy"]
                and inverter_interface is not None
                else None
            )
        },
        "state": optimization_scheduler.get_current_state(),
        "eos_connect_version": __version__,
        "timestamp": datetime.now(time_zone).isoformat(),
        "api_version": "0.0.2",
    }
    return Response(
        json.dumps(response_data, indent=4), content_type="application/json"
    )


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
        base_control.set_mode_override(mode, duration, grid_charge_power)
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


if __name__ == "__main__":
    http_server = None
    try:
        # Create web server with port checking
        HOST = "0.0.0.0"
        desired_port = config_manager.config["eos_connect_web_port"]

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

        # restore the old config
        if (
            config_manager.config["inverter"]["type"]
            in ["fronius_gen24", "fronius_gen24_v2"]
            and inverter_interface is not None
        ):
            inverter_interface.shutdown()
        pv_interface.shutdown()
        price_interface.shutdown()
        mqtt_interface.shutdown()
        evcc_interface.shutdown()
        battery_interface.shutdown()
        logger.info("[Main] Server stopped gracefully")
    finally:
        logging.shutdown()  # This will call close() on all handlers
        logger.info("[Main] Cleanup complete. Goodbye!")
        sys.exit(0)
