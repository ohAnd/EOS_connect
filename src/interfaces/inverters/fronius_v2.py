"""Fronius GEN24 V2 inverter interface implementation."""

import time
import os
import logging
import json
import hashlib
import requests


from ..inverter_base import BaseInverter  # pylint: disable=relative-beyond-top-level

logger = logging.getLogger("__main__").getChild("FroniusV2")
logger.setLevel(logging.INFO)
logger.info("[Inverter] Loading Fronius GEN24 V2 interface")


def hash_utf8_md5(x):
    """Hash a string or bytes object with MD5 (legacy support)."""
    if isinstance(x, str):
        x = x.encode("utf-8")
    return hashlib.md5(x).hexdigest()


def hash_utf8_sha256(x):
    """Hash a string or bytes object with SHA256 (new firmware)."""
    if isinstance(x, str):
        x = x.encode("utf-8")
    return hashlib.sha256(x).hexdigest()


def strip_dict(original):
    """Strip all keys starting with '_' from a dictionary."""
    # return unmodified original if its not a dict
    if not isinstance(original, dict):
        return original
    stripped_copy = {}
    for key in original.keys():
        if not key.startswith("_"):
            stripped_copy[key] = original[key]
    return stripped_copy


# Config files are stored in interfaces/config, one level up from inverters/
base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TIMEOFUSE_CONFIG_FILENAME = os.path.join(base_path, "config", "timeofuse_config.json")
BATTERY_CONFIG_FILENAME = os.path.join(base_path, "config", "battery_config.json")


class FroniusV2(BaseInverter):

    def __init__(self, config):
        """Initialize the Fronius V2 interface."""
        super().__init__(config)

        # --- Configuration values ---
        self.address = config["address"]
        self.user = config.get("user", "customer").lower()  # Always lowercase
        self.password = config.get("password", "your_password")
        self.capacity = -1
        self.max_soc = 100
        self.min_soc = 5

        # --- Auth status ---
        self.subsequent_login = False
        self.ncvalue_num = 1
        self.cnonce = "NaN"
        self.login_attempts = 0
        self.nonce = 0
        self.algorithm = "SHA256"  # Will be determined by firmware version

        # --- SW version loaded in initialize() ---
        self.inverter_sw_revision = {"major": 0, "minor": 0, "patch": 0, "build": 0}
        self.api_praefix = ""

        # --- Internal inverter data (stays in __init__) ---
        self.inverter_current_data = {
            "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32": 0,
            "MODULE_TEMPERATURE_MEAN_01_F32": 0,
            "MODULE_TEMPERATURE_MEAN_03_F32": 0,
            "MODULE_TEMPERATURE_MEAN_04_F32": 0,
            "FANCONTROL_PERCENT_01_F32": 0,
            "FANCONTROL_PERCENT_02_F32": 0,
        }

        # Placeholder, filled in initialize()
        self.previous_battery_config = {}
        self.previous_backup_power_config = None
        self.backup_power_mode = 0

        # Energy Management default initialization (overwritten later)
        self.em_mode = 0
        self.em_power = 0

    def initialize(self):
        """Heavy initialization that performs API calls and loads full configuration."""

        # 1) Load firmware version + set API prefix
        self.__get_current_inverter_sw_version()
        self.__set_api_praefix()

        # 2) Load battery config
        self.previous_battery_config = self.get_battery_config()
        if not self.previous_battery_config:
            raise RuntimeError(
                f"[Inverter] Failed to load Battery config from Inverter at {self.address}"
            )

        # 3) Take over EM mode + power
        self.em_mode = self.previous_battery_config["HYB_EM_MODE"]
        self.em_power = self.previous_battery_config["HYB_EM_POWER"]

        # 4) Activate Solar API
        self.set_solar_api_active(True)

        # 5) Try PowerUnit config
        try:
            self.previous_backup_power_config = self.get_powerunit_config()
        except RuntimeError:
            logger.error("[Inverter] failed to load latest PowerUnit config.")

        # Fallback to older API 1.2
        if not self.previous_backup_power_config:
            try:
                self.previous_backup_power_config = self.get_powerunit_config("1.2")
                logger.info("[Inverter] Loaded PowerUnit config from 1.2 API.")
            except RuntimeError:
                logger.error("[Inverter] Failed to load PowerUnit config (1.2).")
                self.previous_backup_power_config = None

        # 6) Determine backup power mode
        if self.previous_backup_power_config:
            self.backup_power_mode = self.previous_backup_power_config["backuppower"][
                "DEVICE_MODE_BACKUPMODE_TYPE_U16"
            ]
        else:
            logger.error("[Inverter] Setting backup power mode to 0 as fallback.")
            self.backup_power_mode = 0

        # 7) Configure min/max SOC
        if self.backup_power_mode == 0:
            self.min_soc = self.previous_battery_config["BAT_M0_SOC_MIN"]
        else:
            self.min_soc = max(
                self.previous_battery_config["BAT_M0_SOC_MIN"],
                self.previous_battery_config["HYB_BACKUP_RESERVED"],
            )
        self.max_soc = self.previous_battery_config["BAT_M0_SOC_MAX"]

        # 8) Load time-of-use
        self.get_time_of_use()

        # 9) Activate grid charging
        self.set_allow_grid_charging(True)

        logger.info("[Inverter] Initialization completed.")

    def connect_inverter(self):
        return super().connect_inverter()

    def disconnect_inverter(self):
        return super().disconnect_inverter()

    def get_battery_info(self):
        return super().get_battery_info()

    def set_battery_mode(self, mode):
        return super().set_battery_mode(mode)

    def get_SOC(self):
        """
        Retrieves the State of Charge (SOC) from the inverter.

        This method sends a request to the inverter's API to fetch the real-time
        power flow data and extracts the SOC value. If the request fails or the
        response is invalid, it logs an error and returns a default SOC value of 99.0.

        Returns:
            float: The State of Charge (SOC) as a percentage. Defaults to 99.0 if
            the request fails or the response is invalid.
        """
        path = "/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
        response = self.send_request(path)
        if not response:
            logger.error(
                "[Inverter] Failed to get SOC. Returning default value of 99.0"
            )
            return 99.0
        result = json.loads(response.text)
        soc = result["Body"]["Data"]["Inverters"]["1"]["SOC"]
        return soc

    def get_battery_config(self):
        """Get battery configuration from inverter and keep a backup."""
        path = self.api_praefix + "/config/batteries"
        response = self.send_request(path, auth=True)
        if not response:
            logger.error(
                "[Inverter] Failed to get battery configuration. Returning empty dict"
            )
            return {}

        result = json.loads(response.text)
        # only write file if it does not exist
        if not os.path.exists(BATTERY_CONFIG_FILENAME):
            with open(BATTERY_CONFIG_FILENAME, "w", encoding="utf-8") as f:
                f.write(response.text)
        else:
            logger.warning(
                "[Inverter] Battery config file already exists. Not writing to %s",
                BATTERY_CONFIG_FILENAME,
            )

        return result

    def get_powerunit_config(self, path_version="latest"):
        """Get additional PowerUnit configuration for backup power.

        Parameters:
            path_version (optional):
                'latest' (default) - get via '/config/powerunit'
                '1.2'              - get via '/config/setup/powerunit'

        Returns: dict with backup power configuration
        """
        if path_version == "latest":
            path = self.api_praefix + "/config/powerunit"
        else:
            path = "/config/setup/powerunit"

        response = self.send_request(path, auth=True)
        if not response:
            logger.error(
                "[Inverter] Failed to get power unit configuration. Returning empty dict"
            )
            return {}
        result = json.loads(response.text)
        return result

    def restore_battery_config(self):
        """Restore the previous battery config from a backup file."""
        settings_to_restore = [
            "BAT_M0_SOC_MAX",
            "BAT_M0_SOC_MIN",
            "BAT_M0_SOC_MODE",
            "HYB_BM_CHARGEFROMAC",
            "HYB_EM_MODE",
            "HYB_EM_POWER",
            "HYB_EVU_CHARGEFROMGRID",
        ]
        settings = {}
        for key in settings_to_restore:
            if key in self.previous_battery_config.keys():
                settings[key] = self.previous_battery_config[key]
            else:
                raise RuntimeError(
                    f"Unable to restore settings. Parameter {key} is missing"
                )
        path = self.api_praefix + "/config/batteries"
        payload = json.dumps(settings)
        logger.info("[Inverter] Restoring previous battery configuration: %s ", payload)
        response = self.send_request(path, method="POST", payload=payload, auth=True)
        if not response:
            raise RuntimeError("failed to restore battery config")

        response_dict = json.loads(response.text)
        expected_write_successes = settings_to_restore
        for expected_write_success in expected_write_successes:
            if not expected_write_success in response_dict["writeSuccess"]:
                raise RuntimeError(f"failed to set {expected_write_success}")
        # Remove after successful restore
        try:
            os.remove(BATTERY_CONFIG_FILENAME)
        except OSError:
            logger.error(
                "[Inverter] could not remove battery config file %s",
                BATTERY_CONFIG_FILENAME,
            )
        return response

    def set_allow_grid_charging(self, value: bool):
        """Switches grid charging on (true) or off."""
        if value:
            payload = '{"HYB_EVU_CHARGEFROMGRID": true}'
        else:
            payload = '{"HYB_EVU_CHARGEFROMGRID": false}'
        path = self.api_praefix + "/config/batteries"
        response = self.send_request(path, method="POST", payload=payload, auth=True)
        response_dict = json.loads(response.text)
        expected_write_successes = ["HYB_EVU_CHARGEFROMGRID"]
        for expected_write_success in expected_write_successes:
            if not expected_write_success in response_dict["writeSuccess"]:
                raise RuntimeError(f"failed to set {expected_write_success}")
        return response

    def set_solar_api_active(self, value: bool):
        """Switches Solar.API on (true) or off. Solar.API is required to get SOC values."""
        if value:
            payload = '{"SolarAPIv1Enabled": true}'
        else:
            payload = '{"SolarAPIv1Enabled": false}'
        path = self.api_praefix + "/config/solar_api"
        response = self.send_request(path, method="POST", payload=payload, auth=True)
        response_dict = json.loads(response.text)
        expected_write_successes = ["SolarAPIv1Enabled"]
        for expected_write_success in expected_write_successes:
            if not expected_write_success in response_dict["writeSuccess"]:
                raise RuntimeError(f"failed to set {expected_write_success}")
        return response

    def set_wr_parameters(self, minsoc, maxsoc, allow_grid_charging, grid_power):
        """set power at grid-connection point negative values for Feed-In"""
        path = self.api_praefix + "/config/batteries"
        if not isinstance(allow_grid_charging, bool):
            raise RuntimeError(
                f"Expected type: bool actual type: {type(allow_grid_charging)}"
            )

        grid_power = int(grid_power)
        minsoc = int(minsoc)
        maxsoc = int(maxsoc)

        if not 0 <= grid_power <= self.max_grid_charge_rate:
            raise RuntimeError(f"gridpower out of allowed limits {grid_power}")

        if minsoc > maxsoc:
            raise RuntimeError("Min SOC needs to be higher than Max SOC")

        if minsoc < self.min_soc:
            raise RuntimeError(f"Min SOC not allowed below {self.min_soc}")

        if maxsoc > self.max_soc:
            raise RuntimeError(f"Max SOC not allowed above {self.max_soc}")

        parameters = {
            "HYB_EVU_CHARGEFROMGRID": allow_grid_charging,
            "HYB_EM_POWER": grid_power,
            "HYB_EM_MODE": 1,
            "BAT_M0_SOC_MIN": minsoc,
            "BAT_M0_SOC_MAX": maxsoc,
            "BAT_M0_SOC_MODE": "manual",
        }

        payload = json.dumps(parameters)
        logger.info("[Inverter] Setting battery parameters: %s", payload)

        response = self.send_request(path, method="POST", payload=payload, auth=True)
        if not response:
            logger.error("[Inverter] Failed to set parameters. No response from server")
            return response
        response_dict = json.loads(response.text)
        for expected_write_success in parameters.keys():
            if not expected_write_success in response_dict["writeSuccess"]:
                raise RuntimeError(f"failed to set {expected_write_success}")
        return response

    def get_time_of_use(self):
        """Get time of use configuration from inverter and keep a backup."""
        response = self.send_request(self.api_praefix + "/config/timeofuse", auth=True)
        if not response:
            return None

        result = json.loads(response.text)["timeofuse"]
        # only write file if it does not exist
        if not os.path.exists(TIMEOFUSE_CONFIG_FILENAME):
            with open(TIMEOFUSE_CONFIG_FILENAME, "w", encoding="utf-8") as f:
                f.write(json.dumps(result))
        else:
            logger.warning(
                "[Inverter] Time of use config file already exists. Not writing to %s",
                TIMEOFUSE_CONFIG_FILENAME,
            )

        return result

    def fetch_inverter_data(self):
        """Get inverter data for monitoring (temperatures, fan control, etc.)."""
        try:
            response = self.send_request(
                self.api_praefix + "/components/inverter/readable", auth=True
            )

            if not response:
                logger.debug("[Inverter] Inverter monitoring endpoint not available")
                return None

            if response.status_code == 404:
                logger.debug(
                    "[Inverter] Inverter monitoring not supported by this firmware"
                )
                return None

            if response.status_code != 200:
                logger.debug(
                    f"[Inverter] Inverter monitoring returned {response.status_code}"
                )
                return None

            data = json.loads(response.text)
            body_data = data.get("Body", {}).get("Data", {})

            # Find first device that has channel data (device id is dynamic)
            channels = {}
            for device_id, device_data in body_data.items():
                if isinstance(device_data, dict) and "channels" in device_data:
                    channels = device_data.get("channels", {}) or {}
                    logger.debug(
                        f"[Inverter] Found inverter data for device: {device_id}"
                    )
                    break

            # If no channels found, still return a zeroed dict (do not return None on malformed 200)
            if not channels:
                logger.warning("[Inverter] No channel data found in inverter response")
                self.inverter_current_data = {
                    "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32": 0.0,
                    "MODULE_TEMPERATURE_MEAN_01_F32": 0.0,
                    "MODULE_TEMPERATURE_MEAN_03_F32": 0.0,
                    "MODULE_TEMPERATURE_MEAN_04_F32": 0.0,
                    "FANCONTROL_PERCENT_01_F32": 0.0,
                    "FANCONTROL_PERCENT_02_F32": 0.0,
                }
                return self.inverter_current_data

            # Build normalized inverter_current_data with consistent keys
            ambient_raw = channels.get("DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32", 0)
            self.inverter_current_data = {
                # Canonical keys used by V2/tests
                "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32": round(ambient_raw, 2),
                "MODULE_TEMPERATURE_MEAN_01_F32": round(
                    channels.get("MODULE_TEMPERATURE_MEAN_01_F32", 0), 2
                ),
                "MODULE_TEMPERATURE_MEAN_03_F32": round(
                    channels.get("MODULE_TEMPERATURE_MEAN_03_F32", 0), 2
                ),
                "MODULE_TEMPERATURE_MEAN_04_F32": round(
                    channels.get("MODULE_TEMPERATURE_MEAN_04_F32", 0), 2
                ),
                "FANCONTROL_PERCENT_01_F32": round(
                    channels.get("FANCONTROL_PERCENT_01_F32", 0), 2
                ),
                "FANCONTROL_PERCENT_02_F32": round(
                    channels.get("FANCONTROL_PERCENT_02_F32", 0), 2
                ),
            }

            logger.debug("[Inverter] Inverter data: %s", self.inverter_current_data)
            return self.inverter_current_data

        except (KeyError, ValueError, AttributeError) as e:
            logger.warning(f"[Inverter] Error parsing inverter data: {e}")
            return None

    def get_inverter_current_data(self):
        """Get the current inverter data."""
        return self.inverter_current_data

    def supports_extended_monitoring(self) -> bool:
        """Fronius V2 supports extended monitoring (temperature, fan control)."""
        return True

    def set_mode_avoid_discharge(self):
        """Set the inverter to avoid discharging the battery."""
        timeofuselist = [
            {
                "Active": True,
                "Power": int(0),
                "ScheduleType": "DISCHARGE_MAX",
                "TimeTable": {"Start": "00:00", "End": "23:59"},
                "Weekdays": {
                    "Mon": True,
                    "Tue": True,
                    "Wed": True,
                    "Thu": True,
                    "Fri": True,
                    "Sat": True,
                    "Sun": True,
                },
            }
        ]
        return self.set_time_of_use(timeofuselist)

    def set_mode_allow_discharge(self):
        """Set the inverter to discharge the battery."""
        timeofuselist = []
        if self.max_pv_charge_rate > 0:
            timeofuselist = [
                {
                    "Active": True,
                    "Power": int(self.max_pv_charge_rate),
                    "ScheduleType": "CHARGE_MAX",
                    "TimeTable": {"Start": "00:00", "End": "23:59"},
                    "Weekdays": {
                        "Mon": True,
                        "Tue": True,
                        "Wed": True,
                        "Thu": True,
                        "Fri": True,
                        "Sat": True,
                        "Sun": True,
                    },
                }
            ]
        response = self.set_time_of_use(timeofuselist)

        return response

    def set_mode_force_charge(self, chargerate=500):
        """Set the inverter to charge the battery with a specific power from GRID."""
        # activate timeofuse rules
        chargerate = min(chargerate, self.max_grid_charge_rate)
        timeofuselist = [
            {
                "Active": True,
                "Power": int(chargerate),
                "ScheduleType": "CHARGE_MIN",
                "TimeTable": {"Start": "00:00", "End": "23:59"},
                "Weekdays": {
                    "Mon": True,
                    "Tue": True,
                    "Wed": True,
                    "Thu": True,
                    "Fri": True,
                    "Sat": True,
                    "Sun": True,
                },
            }
        ]
        return self.set_time_of_use(timeofuselist)

    def restore_time_of_use_config(self):
        """Restore the previous time of use config from a backup file."""
        try:
            with open(TIMEOFUSE_CONFIG_FILENAME, "r", encoding="utf-8") as f:
                time_of_use_config_json = f.read()
        except OSError:
            logger.error("[Inverter] could not restore timeofuse config")
            return

        try:
            time_of_use_config = json.loads(time_of_use_config_json)
        except:  # pylint: disable=bare-except
            logger.error(
                "[Inverter] could not parse timeofuse config from %s",
                TIMEOFUSE_CONFIG_FILENAME,
            )
            return

        stripped_time_of_use_config = []
        for listitem in time_of_use_config:
            new_item = {}
            new_item["Active"] = listitem["Active"]
            new_item["Power"] = listitem["Power"]
            new_item["ScheduleType"] = listitem["ScheduleType"]
            new_item["TimeTable"] = {
                "Start": listitem["TimeTable"]["Start"],
                "End": listitem["TimeTable"]["End"],
            }
            weekdays = {}
            for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
                weekdays[day] = listitem["Weekdays"][day]
            new_item["Weekdays"] = weekdays
            stripped_time_of_use_config.append(new_item)

        self.set_time_of_use(stripped_time_of_use_config)
        # After restoring the time of use config, delete the backup
        try:
            os.remove(TIMEOFUSE_CONFIG_FILENAME)
        except OSError:
            logger.error(
                "[Inverter] could not remove timeofuse config file %s",
                TIMEOFUSE_CONFIG_FILENAME,
            )

    def set_time_of_use(self, timeofuselist):
        """Get the planned battery charge/discharge schedule."""
        config = {"timeofuse": timeofuselist}
        payload = json.dumps(config)
        response = self.send_request(
            self.api_praefix + "/config/timeofuse",
            method="POST",
            payload=payload,
            auth=True,
        )
        response_dict = json.loads(response.text)
        expected_write_successes = ["timeofuse"]
        for expected_write_success in expected_write_successes:
            if not expected_write_success in response_dict["writeSuccess"]:
                raise RuntimeError(f"failed to set {expected_write_success}")
        return response

    def get_capacity(self):
        """Get the full and raw capacity of the battery in Wh."""
        if self.capacity >= 0:
            return self.capacity

        response = self.send_request("/solar_api/v1/GetStorageRealtimeData.cgi")
        if not response:
            logger.warning(
                "[Inverter] capacity request failed. Returning default value"
            )
            return 1000
        result = json.loads(response.text)
        capacity = result["Body"]["Data"]["0"]["Controller"]["DesignedCapacity"]
        self.capacity = capacity
        return capacity

    def send_request(
        self, path, method="GET", payload="", params=None, headers=None, auth=False
    ):
        """Send a HTTP REST request to the inverter.

        auth = This request needs to be run with authentication.
        is_login = This request is a login request. Do not retry on 401.
        """
        logger.debug("[Inverter] Sending request to %s", path)
        if not headers:
            headers = {}
        for i in range(2):
            # Try tp send the request, if it fails, try to login and resend
            response = self.__send_one_http_request(
                path, method, payload, params, headers, auth
            )
            if response.status_code == 200:
                if auth:
                    self.__retrieve_auth_from_response(response)
                return response
            # 401 - unauthorized , relogin
            # 403 - is forbidden, what happens at 01.00 in the night
            if response.status_code in (401, 403):
                self.__retrieve_auth_from_response(response)
                self.login()
            else:
                raise RuntimeError(
                    f"[Inverter] Request {i} failed with {response.status_code}-"
                    f"{response.reason}. \n"
                    f"\t path:{path}, \n\tparams:{params} \n\theaders {headers} \n"
                    f"\tnonce {self.nonce} \n"
                    f"\tpayload {payload}"
                )
        return None

    def __send_one_http_request(
        self, path, method="GET", payload="", params=None, headers=None, auth=False
    ):
        """Send one HTTP Request to the backend.
        This method does not handle application errors, only connection errors.
        """
        if not headers:
            headers = {}
        url = "http://" + self.address + path
        fullpath = path
        if params:
            fullpath += "?" + "&".join(
                [f"{k + '=' + str(params[k])}" for k in params.keys()]
            )
        if auth:
            headers["Authorization"] = self.get_auth_header(
                method=method, path=fullpath
            )

        for i in range(3):
            # 3 retries if connection can't be established
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    data=payload,
                    timeout=30,
                )
                return response
            except requests.exceptions.ConnectionError as err:
                logger.error(
                    "[Inverter] Connection to Inverter failed on %s. (%d) "
                    "Retrying in 60 seconds, Error %s",
                    self.address,
                    i,
                    err,
                )
                time.sleep(60)

        logger.error("[Inverter] Request failed without response.")
        raise RuntimeError(
            f"\turl:{url}, \n\tparams:{params} \n\theaders {headers} \n"
            f"\tnonce {self.nonce} \n"
            f"\tpayload {payload}"
        )

    def login(self):
        """Login to Fronius API"""
        logger.debug("[Inverter] Logging in")
        path = self.api_praefix + "/commands/Login"
        self.cnonce = "NaN"
        self.ncvalue_num = 1
        self.login_attempts = 0
        for i in range(3):
            self.login_attempts += 1
            response = self.__send_one_http_request(path, auth=True)
            if response.status_code == 200:
                self.subsequent_login = True
                logger.info("[Inverter] Login successful %s", response)
                logger.debug("[Inverter] Response: %s", response.headers)
                self.__retrieve_auth_from_response(response)
                self.login_attempts = 0
                return

            logger.error("[Inverter] Login -%d- failed, Response: %s", i, response)
            logger.error("[Inverter] Response-raw: %s", response.raw)
            if self.subsequent_login:
                logger.info("[Inverter] Retrying login in 10 seconds")
                time.sleep(10)
        if self.login_attempts >= 3:
            logger.info("[Inverter] Login failed 3 times .. aborting")
            raise RuntimeError(
                "[Inverter] Login failed repeatedly .. wrong credentials?"
            )

    def logout(self):
        """Logout from Fronius API"""
        path = self.api_praefix + "/commands/Logout"
        response = self.send_request(path, auth=True)
        if not response:
            logger.warning("[Inverter] Logout failed. No response from server")
        if response.status_code == 200:
            logger.info("[Inverter] Logout successful")
        else:
            logger.info("[Inverter] Logout failed")
        return response

    def __retrieve_auth_from_response(self, response):
        """Get & store the authentication parts from response auth header.
        - nc
        - cnonce
        - nonce
        """
        auth_dict = self.__split_response_auth_header(response)
        if auth_dict.get("nc"):
            self.ncvalue_num = int(auth_dict["nc"]) + 1
        else:
            self.ncvalue_num = 1
        if auth_dict.get("cnonce"):
            self.cnonce = auth_dict["cnonce"]
        else:
            self.cnonce = "NaN"
        if auth_dict.get("nonce"):
            self.nonce = auth_dict["nonce"]

    def __split_response_auth_header(self, response):
        """Split the response header into a dictionary."""
        auth_dict = {}
        # stupid API bug: nonce headers with different capitalization at different end points
        if "X-WWW-Authenticate" in response.headers:
            auth_string = response.headers["X-WWW-Authenticate"]
        elif "X-Www-Authenticate" in response.headers:
            auth_string = response.headers["X-Www-Authenticate"]
        elif "Authentication-Info" in response.headers:
            auth_string = response.headers["Authentication-Info"]
        else:
            # Return an empty dict to work with Fronius below 1.35.4-1
            logger.debug("[Inverter] No authentication header found in response")
            return auth_dict

        auth_list = auth_string.replace(" ", "").replace('"', "").split(",")
        logger.debug("[Inverter] Authentication header: %s", auth_list)
        auth_dict = {}
        for item in auth_list:
            key, value = item.split("=")
            auth_dict[key] = value
            logger.debug("[Inverter] %s: %s", key, value)
        return auth_dict

    def get_auth_header(self, method, path) -> str:
        """Create the Authorization header with SHA256/MD5 support based on firmware."""
        nonce = self.nonce
        realm = "Webinterface area"
        ncvalue = f"{self.ncvalue_num:08d}"
        cnonce = self.cnonce
        user = self.user
        password = self.password

        if len(self.user) < 4:
            raise RuntimeError("User needed for Authorization")
        if len(self.password) < 4:
            raise RuntimeError("Password needed for Authorization")

        # Choose hash function based on algorithm
        if self.algorithm == "SHA256":
            hash_func = hash_utf8_sha256
            algorithm_header = "SHA256"
        else:
            hash_func = hash_utf8_md5
            algorithm_header = "MD5"

        a1 = f"{user}:{realm}:{password}"
        a2 = f"{method}:{path}"
        ha1 = hash_func(a1)
        ha2 = hash_func(a2)
        noncebit = f"{nonce}:{ncvalue}:{cnonce}:auth:{ha2}"
        respdig = hash_func(f"{ha1}:{noncebit}")

        auth_header = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{path}", '
        auth_header += f'algorithm="{algorithm_header}", qop=auth, nc={ncvalue}, cnonce="{cnonce}", '
        auth_header += f'response="{respdig}"'
        return auth_header

    def __set_em(self, mode=None, power=None):
        """Change Energy Management"""
        settings = {}
        settings = {"HYB_EM_MODE": self.em_mode, "HYB_EM_POWER": self.em_power}

        if mode is not None:
            settings["HYB_EM_MODE"] = mode
        if power is not None:
            settings["HYB_EM_POWER"] = power

        path = self.api_praefix + "/config/batteries"
        payload = json.dumps(settings)
        logger.info("[Inverter] Setting EM mode %s , power %s", mode, power)
        response = self.send_request(path, method="POST", payload=payload, auth=True)
        if not response:
            raise RuntimeError("Failed to set EM")

    # def set_em_power(self, power):
    #     """ Change Energy Manangement Power
    #         positive = get from grid
    #         negative = feed to grid
    #     """
    #     self.__set_em(power=power)
    #     self.em_power = power
    #     if self.mqtt_api:
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'em_power', power)

    # def set_em_mode(self, mode):
    #     """ Change Energy Manangement mode."""
    #     self.__set_em(mode=mode)
    #     self.em_mode = mode
    #     if self.mqtt_api:
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'em_mode', mode)

    def shutdown(self):
        """Change back EOS_connect changes."""
        logger.info("[Inverter] Reverting EOS_connect created config changes")
        self.restore_battery_config()
        self.restore_time_of_use_config()
        self.logout()

    def __get_current_inverter_sw_version(self):
        """Get the current version of the inverter."""
        path = "/status/version"
        response = self.send_request(path)
        if not response:
            logger.error(
                "[Inverter] Failed to get current version. Returning default value"
            )
            return 99.0
        result = json.loads(response.text)
        version_string = result.get("swrevisions").get("GEN24")
        version_parts = version_string.split("-")[0].split(".")
        self.inverter_sw_revision = {
            "major": int(version_parts[0]),
            "minor": int(version_parts[1]),
            "patch": int(version_parts[2]),
            "build": int(version_string.split("-")[1]),
        }
        logger.info("[Inverter] Current sw revision: %s", self.inverter_sw_revision)
        return True

    def __set_api_praefix(self):
        """Set API prefix and authentication algorithm based on firmware version."""
        version_tuple = (
            self.inverter_sw_revision["major"],
            self.inverter_sw_revision["minor"],
            self.inverter_sw_revision["patch"],
            self.inverter_sw_revision["build"],
        )

        if version_tuple < (1, 36, 5, 1):
            # Old firmware: no /api prefix, MD5 only
            self.api_praefix = ""
            self.algorithm = "MD5"
            logger.info(
                "[Inverter] Old firmware (<1.36.5-1): Using '' base with MD5 auth"
            )
        elif version_tuple < (1, 38, 6, 1):
            # Middle firmware: /api prefix, MD5 only
            self.api_praefix = "/api"
            self.algorithm = "MD5"
            logger.info(
                "[Inverter] Middle firmware (1.36.5-1 to 1.38.5-x): Using '/api' base with MD5 auth"
            )
        else:
            # New firmware: /api prefix, SHA256
            self.api_praefix = "/api"
            self.algorithm = "SHA256"
            logger.info(
                "[Inverter] New firmware (>=1.38.6-1): Using '/api' base with SHA256 auth"
            )
        return True

    # def activate_mqtt(self, api_mqtt_api):
    #     """
    #     Activates MQTT for the inverter.

    #     This function starts the API functions and publishes all internal values via MQTT.
    #     The MQTT topic is: base_topic + '/inverters/0/'

    #     Parameters that can be set via MQTT:
    #     - max_grid_charge_rate (int): Maximum power in W that can be
    #                                       used to load the battery from the grid.
    #     - max_pv_charge_rate (int): Maximum power in W that can be
    #                                       used to load the battery from the PV.

    #     Args:
    #         api_mqtt_api: The MQTT API instance to be used for registering callbacks.

    #     """
    #     self.mqtt_api = api_mqtt_api
    #     # /set is appended to the topic
    #     self.mqtt_api.register_set_callback(self.__get_mqtt_topic(
    #     ) + 'max_grid_charge_rate', self.api_set_max_grid_charge_rate, int)
    #     self.mqtt_api.register_set_callback(self.__get_mqtt_topic(
    #     ) + 'max_pv_charge_rate', self.api_set_max_pv_charge_rate, int)
    #     self.mqtt_api.register_set_callback(self.__get_mqtt_topic(
    #     ) + 'em_mode', self.api_set_em_mode, int)
    #     self.mqtt_api.register_set_callback(self.__get_mqtt_topic(
    #     ) + 'em_power', self.api_set_em_power, int)

    # def refresh_api_values(self):
    #     """ Publishes all values to mqtt."""
    #     if self.mqtt_api:
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'SOC', self.get_SOC())
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'stored_energy', self.get_stored_energy())
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'free_capacity', self.get_free_capacity())
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'max_capacity', self.get_max_capacity())
    #         self.mqtt_api.generic_publish(self.__get_mqtt_topic(
    #         ) + 'usable_capacity', self.get_usable_capacity())
    #         self.mqtt_api.generic_publish(self.__get_mqtt_topic(
    #         ) + 'max_grid_charge_rate', self.max_grid_charge_rate)
    #         self.mqtt_api.generic_publish(self.__get_mqtt_topic(
    #         ) + 'max_pv_charge_rate', self.max_pv_charge_rate)
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'min_soc', self.min_soc)
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'max_soc', self.max_soc)
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'capacity', self.get_capacity())
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'em_mode' , self.em_mode)
    #         self.mqtt_api.generic_publish(
    #             self.__get_mqtt_topic() + 'em_power' , self.em_power)

    def api_set_max_grid_charge_rate(self, max_grid_charge_rate: int):
        """Set the maximum power in W that can be used to load the battery from the grid."""
        if max_grid_charge_rate < 0:
            logger.warning(
                "[Inverter] API: Invalid max_grid_charge_rate %sW", max_grid_charge_rate
            )
            return
        logger.info(
            "[Inverter] API: Setting max_grid_charge_rate: %.1fW", max_grid_charge_rate
        )
        self.max_grid_charge_rate = max_grid_charge_rate

    def api_set_max_pv_charge_rate(self, max_pv_charge_rate: int):
        """Set the maximum power in W that can be used to load the battery from the PV."""
        if max_pv_charge_rate < 0:
            logger.warning(
                "[Inverter] API: Invalid max_pv_charge_rate %s", max_pv_charge_rate
            )
            return
        logger.info(
            "[Inverter] API: Setting max_pv_charge_rate: %.1fW", max_pv_charge_rate
        )
        self.max_pv_charge_rate = max_pv_charge_rate

    # def api_set_em_mode(self, em_mode: int):
    #     """ Set the Energy Management Mode."""
    #     if not isinstance(em_mode , int):
    #         logger.warning(
    #             '[Inverter] API: Invalid type em_mode %s',
    #             em_mode
    #         )
    #         return
    #     if em_mode < 0 or em_mode > 2:
    #         logger.warning(
    #             '[Inverter] API: Invalid em_mode %s',
    #             em_mode
    #         )
    #         return
    #     logger.info(
    #         '[Inverter] API: Setting em_mode: %s',
    #         em_mode
    #     )
    #     self.set_em_mode(em_mode)

    # def api_set_em_power(self, em_power: int):
    #     """ Change EnergeManagement Offset
    #         positive = get from grid
    #         negative = feed to grid
    #     """
    #     if not isinstance(em_power , int):
    #         logger.warning(
    #             '[Inverter] API: Invalid type em_power %s',
    #             em_power
    #         )
    #         return
    #     logger.info(
    #         '[Inverter] API: Setting em_power: %s',
    #         em_power
    #     )
    #     self.set_em_power(em_power)

    # def __get_mqtt_topic(self) -> str:
    #     """ Used to implement the mqtt basic topic."""
    #     return f'inverters/{self.inverter_num}/'
