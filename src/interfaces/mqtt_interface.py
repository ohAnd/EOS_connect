"""
parts of my contributiuon to from
https://github.com/muexxl/batcontrol/blob/main/src/batcontrol/mqtt_api.py

This module provides an MQTT interface for Home Assistant Auto Discovery and MQTT communication.
It includes functionalities for connecting to an MQTT broker, subscribing to topics, publishing
messages, and handling Home Assistant MQTT Auto Discovery configuration.
"""

import logging
import json
from typing import Optional
from typing import Any, Dict
from pathlib import Path
import sys
import paho.mqtt.client as mqtt

sys.path.append(str(Path(__file__).resolve().parent.parent))
from version import __version__


logger = logging.getLogger("__main__")
logger.info("[MQTT] Loading module")


class MqttInterface:
    """
    MQTT Interface for Home Assistant Auto Discovery and MQTT communication.
    This class handles the connection to the MQTT broker, subscribes to topics,
    and publishes messages.
    """

    def __init__(self, config_mqtt: Dict[str, Any], on_mqtt_command=None):
        """
        Initialize the MQTT client.

        :param broker: MQTT broker address
        :param port: MQTT broker port (default: 1883)
        :param username: Username for authentication (optional)
        :param password: Password for authentication (optional)
        :param tls: Use TLS for secure connection (default: False)
        :param will_topic: Topic for the Last Will and Testament (optional)
        :param will_message: Message for the Last Will and Testament (default: "offline")
        """
        self.enable_mqtt = config_mqtt.get("enabled", False)
        self.mqtt_config_enabled = self.enable_mqtt  # Store original config value
        self.mqtt_connection_failed = False  # Track connection failures
        if not self.enable_mqtt:
            logger.info("[MQTT] MQTT is disabled, skipping initialization.")
            return

        self.client = mqtt.Client()
        self.broker = config_mqtt.get("broker", "localhost")
        self.port = config_mqtt.get("port", 1883)
        username = config_mqtt.get("user", "")
        password = config_mqtt.get("password", "")
        self.tls = config_mqtt.get("tls", False)
        self.base_topic = "eos_connect"
        self.ha_auto_discovery = config_mqtt.get("ha_mqtt_auto_discovery", True)
        self.auto_discover_topic = config_mqtt.get(
            "ha_mqtt_auto_discovery_prefix", "homeassistant"
        )

        # Set authentication if provided
        if username and password:
            self.client.username_pw_set(username, password)

        # Set TLS if enabled
        if self.tls:
            self.client.tls_set()

        self.topics_publish = {
            "status": {
                "value": "offline",
                "name": "Status",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "sensor",
                "device_class": None,
                "icon": "mdi:state-machine",
                "entity_category": "diagnostic",
            },
            "control/overall_state": {
                "value": None,
                "set_value": None,
                "command_topic": "control/overall_state/set",
                "name": "Current State",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "select",
                "device_class": None,
                "icon": "mdi:state-machine",
                "value_template": (
                    "{% set v = value|int %}"
                    "{% if v == -2 %}Auto"
                    "{% elif v == -1 %}StartUp"
                    "{% elif v == 0 %}Charge from Grid"
                    "{% elif v == 1 %}Avoid Discharge"
                    "{% elif v == 2 %}Discharge Allowed"
                    "{% elif v == 3 %}Avoid Discharge EVCC FAST"
                    "{% elif v == 4 %}Avoid Discharge EVCC PV"
                    "{% elif v == 5 %}Avoid Discharge EVCC MIN+PV"
                    "{% elif v == 6 %}Charge from Grid EVCC FAST"
                    "{% else %}Unknown{% endif %}"
                ),
                "command_template": (
                    "{% set labels = {"
                    "'Auto': -2, "
                    "'StartUp': -2, "
                    "'Charge from Grid': 0, "
                    "'Avoid Discharge': 1, "
                    "'Discharge Allowed': 2, "
                    "'Avoid Discharge EVCC FAST': -2, "
                    "'Avoid Discharge EVCC PV': -2, "
                    "'Avoid Discharge EVCC MIN+PV': -2, "
                    "'Charge from Grid EVCC FAST': -2"
                    "} %}"
                    "{% if value is not none and (value|int(0)|string) == (value|string) %}{{ value|int(0) }}{% elif value in labels %}{{ labels[value] }}{% else %}-2{% endif %}"
                ),
                "options": [
                    "Auto",
                    "StartUp",
                    "Charge from Grid",
                    "Avoid Discharge",
                    "Discharge Allowed",
                    "Avoid Discharge EVCC FAST",
                    "Avoid Discharge EVCC PV",
                    "Avoid Discharge EVCC MIN+PV",
                    "Charge from Grid EVCC FAST",
                ],
            },
            "control/eos_ac_charge_demand": {
                "value": None,
                "name": "EOS AC Charge Demand",
                "qos": 0,
                "retain": True,
                "unit": "W",
                "type": "sensor",
                "device_class": "power",
                "icon": "mdi:state-machine",
                "entity_category": "diagnostic",
            },
            "control/eos_dc_charge_demand": {
                "value": None,
                "name": "EOS DC Charge Demand",
                "qos": 0,
                "retain": True,
                "unit": "W",
                "type": "sensor",
                "device_class": "power",
                "icon": "mdi:state-machine",
                "entity_category": "diagnostic",
            },
            "control/eos_discharge_allowed": {
                "value": None,
                "name": "EOS Discharge Allowed",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "binary_sensor",
                "device_class": None,
                "value_template": "{{ 'OFF' if 'False' in value else 'ON'}}",
                "icon": "mdi:state-machine",
                "entity_category": "diagnostic",
            },
            "control/eos_homeappliance_released": {
                "value": None,
                "name": "EOS Home Appliance Released",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "binary_sensor",
                "device_class": None,
                "value_template": "{{ 'OFF' if 'False' in value else 'ON'}}",
                "icon": "mdi:heat-pump-outline",
            },
            "control/eos_homeappliance_start_hour": {
                "value": None,
                "name": "EOS Home Appliance Start Hour",
                "qos": 0,
                "retain": True,
                "unit": "hour",
                "type": "sensor",
                "device_class": None,
                "icon": "mdi:clock-start",
            },
            "control/override_remain_time": {
                "value": None,
                "set_value": None,
                "command_topic": "control/override_remain_time/set",
                "name": "Override Remain Time (HH:MM)",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "select",
                "device_class": None,
                "icon": "mdi:clock-end",
                "options": [
                    "00:30",
                    "01:00",
                    "01:30",
                    "02:00",
                    "02:30",
                    "03:00",
                    "03:30",
                    "04:00",
                    "04:30",
                    "05:00",
                    "05:30",
                    "06:00",
                    "06:30",
                    "07:00",
                    "07:30",
                    "08:00",
                    "08:30",
                    "09:00",
                    "09:30",
                    "10:00",
                    "10:30",
                    "11:00",
                    "11:30",
                    "12:00",
                ],
            },
            "control/override_charge_power": {
                "value": None,
                "set_value": None,
                "command_topic": "control/override_charge_power/set",
                "name": "Override Charge Power",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "number",
                "min": 0,
                "max": 10000,
                "step": 100,
                "device_class": "power",
                "icon": "mdi:power-plug-battery-outline",
                "value_template": "{{ value|int }}",  # Ensures integer display in HA
            },
            "control/override_active": {
                "value": None,
                "name": "Override Active",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "binary_sensor",
                "device_class": None,
                "value_template": "{{ 'OFF' if 'False' in value else 'ON'}}",
                "icon": "mdi:autorenew-off",
            },
            "control/override_end_time": {
                "value": None,
                "name": "Override End Time",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "sensor",
                "device_class": "timestamp",
                "icon": "mdi:clock-end",
            },
            "optimization/last_run": {
                "value": None,
                "name": "Last Run",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "sensor",
                "device_class": "timestamp",
                "icon": "mdi:timer-check-outline",
            },
            "optimization/next_run": {
                "value": None,
                "name": "Next Run",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "sensor",
                "device_class": "timestamp",
                "icon": "mdi:timer-play-outline",
            },
            "optimization/state": {
                "value": None,
                "name": "Optimization State",
                "qos": 0,
                "retain": True,
                "unit": None,
                "type": "sensor",
                "device_class": None,
                "icon": "mdi:state-machine",
            },
            "inverter/special/temperature_inverter": {
                "value": None,
                "name": "Inverter Temperature",
                "qos": 0,
                "retain": True,
                "unit": "°C",
                "type": "sensor",
                "device_class": "temperature",
                "icon": "mdi:thermometer",
                "entity_category": "diagnostic",
            },
            "inverter/special/temperature_ac_module": {
                "value": None,
                "name": "AC Module Temperature",
                "qos": 0,
                "retain": True,
                "unit": "°C",
                "type": "sensor",
                "device_class": "temperature",
                "icon": "mdi:thermometer",
                "entity_category": "diagnostic",
            },
            "inverter/special/temperature_dc_module": {
                "value": None,
                "name": "DC Module Temperature",
                "qos": 0,
                "retain": True,
                "unit": "°C",
                "type": "sensor",
                "device_class": "temperature",
                "icon": "mdi:thermometer",
                "entity_category": "diagnostic",
            },
            "inverter/special/temperature_battery_module": {
                "value": None,
                "name": "Battery Module Temperature",
                "qos": 0,
                "retain": True,
                "unit": "°C",
                "type": "sensor",
                "device_class": "temperature",
                "icon": "mdi:thermometer",
                "entity_category": "diagnostic",
            },
            "inverter/special/fan_control_01": {
                "value": None,
                "name": "Inverter Fan Control 01",
                "qos": 0,
                "retain": True,
                "unit": "%",
                "type": "sensor",
                "device_class": None,
                "icon": "mdi:fan",
                "entity_category": "diagnostic",
            },
            "inverter/special/fan_control_02": {
                "value": None,
                "name": "Inverter Fan Control 02",
                "qos": 0,
                "retain": True,
                "unit": "%",
                "type": "sensor",
                "device_class": None,
                "icon": "mdi:fan",
                "entity_category": "diagnostic",
            },
            "battery/soc": {
                "value": None,
                "name": "State of Charge",
                "qos": 0,
                "retain": True,
                "unit": "%",
                "type": "sensor",
                "device_class": "battery",
                "icon": "mdi:battery",
            },
            "battery/remaining_energy": {
                "value": None,
                "name": "Remaining Energy",
                "qos": 0,
                "retain": True,
                "unit": "Wh",
                "type": "sensor",
                "device_class": "energy",
                "icon": "mdi:home-battery-outline",
            },
            "battery/dyn_max_charge_power": {
                "value": None,
                "name": "Dyn Max Charge Power",
                "qos": 0,
                "retain": True,
                "unit": "W",
                "type": "sensor",
                "device_class": "power",
                "icon": None,
                "entity_category": "diagnostic",
            },
            "battery/soc_min": {
                "value": None,
                "set_value": None,
                "command_topic": "battery/soc_min/set",
                "name": "Battery min SOC",
                "qos": 0,
                "retain": True,
                "unit": "%",
                "type": "number",
                "min": 0,
                "max": 100,
                "step": 1,
                "device_class": None,
                "icon": "mdi:battery-minus-outline",
                "entity_category": "diagnostic",
            },
            "battery/soc_max": {
                "value": None,
                "set_value": None,
                "command_topic": "battery/soc_max/set",
                "name": "Battery max SOC",
                "qos": 0,
                "retain": True,
                "unit": "%",
                "type": "number",
                "min": 0,
                "max": 100,
                "step": 1,
                "device_class": None,
                "icon": "mdi:battery-plus-outline",
                "entity_category": "diagnostic",
            },
        }

        self.topics_publish_last = {
            key: value.copy() if isinstance(value, dict) else value
            for key, value in self.topics_publish.items()
        }

        # Set Last Will and Testament (LWT)
        self.client.will_set(self.base_topic + "/status", "offline", qos=1, retain=True)

        # Attach event callbacks
        self.on_mqtt_command = on_mqtt_command  # Store the callback
        self.client.on_connect = self.__on_connect
        self.client.on_message = self.__on_message
        self.client.on_disconnect = self.__on_disconnect
        self.client.on_subscribe = self.__on_subscribe

        # start the client loop
        if not self.__connect():
            logger.error("[MQTT] Failed to connect to MQTT broker, disabling MQTT.")
            self.enable_mqtt = False
            self.mqtt_connection_failed = True  # Mark as connection failure
            return
        self.client.loop_start()
        logger.info("[MQTT] Successfully connected to MQTT broker.")

    def __subscribe_needed_topics(self):
        """
        Subscribe to the necessary topics for the MQTT client.
        """
        if self.base_topic:
            for topic, value in self.topics_publish.items():
                if "command_topic" in value and value["command_topic"]:
                    self.__subscribe(self.base_topic + "/" + value["command_topic"])

    def __on_connect(self, client, userdata, flags, rc):
        """
        Callback for when the client connects to the broker.
        """
        if rc == 0:
            logger.info(
                "[MQTT] Connected to MQTT broker: %s:%d", self.broker, self.port
            )
            self.__subscribe_needed_topics()
            if self.ha_auto_discovery:
                logger.info(
                    "[MQTT] Home Assistant Auto Discovery enabled, sending discovery messages."
                )
                # Publish all offered mqtt discovery config messages
                self.__send_mqtt_discovery_messages()
        else:
            # Handle specific return codes
            if rc == 1:
                logger.error("[MQTT] Connection refused: Incorrect protocol version.")
            elif rc == 2:
                logger.error("[MQTT] Connection refused: Invalid client identifier.")
            elif rc == 3:
                logger.error("[MQTT] Connection refused: Server unavailable.")
            elif rc == 4:
                logger.error("[MQTT] Connection refused: Bad username or password.")
            elif rc == 5:
                logger.error(
                    "[MQTT] Connection refused: Not authorized."
                    " Check username/password or permissions."
                )
            else:
                logger.error(
                    "[MQTT] Connection refused: Unknown error (return code: %d).", rc
                )

    def __on_disconnect(self, client, userdata, rc):
        """
        Callback for when the client disconnects from the broker.
        """
        if rc == 0:
            logger.info("[MQTT] Disconnected from MQTT broker.")
        else:
            logger.warning(
                "[MQTT] Unexpected disconnection from MQTT broker, return code: %d", rc
            )

    def __on_subscribe(self, client, userdata, mid, granted_qos):
        """
        Callback method triggered when the client successfully subscribes to a topic.
        """
        logger.debug("[MQTT] Subscribed to topic with QoS: %s", granted_qos)

    def __on_message(self, client, userdata, msg):
        """
        Callback for when a message is received on a subscribed topic.
        """
        logger.debug(
            "[MQTT] Received message on topic '%s': %s", msg.topic, msg.payload.decode()
        )
        topic = msg.topic.replace(self.base_topic + "/", "", 1).removesuffix("/set")
        if topic in self.topics_publish:
            try:
                self.topics_publish[topic]["set_value"] = msg.payload.decode()
                logger.info(
                    "[MQTT] message received - set value for topic '%s': %s",
                    topic,
                    self.topics_publish[topic]["set_value"],
                )
            except KeyError as e:
                logger.error(
                    "[MQTT] KeyError while updating publish topic => %s: %s",
                    topic,
                    e,
                )
            except (TypeError, ValueError) as e:
                logger.error("[MQTT] Error while updating publish topics: %s", e)
            # Map topics to command keys for simplified handling
            topic_command_map = {
                "control/overall_state": "mode",
                "control/override_remain_time": "duration",
                "control/override_charge_power": "charge_power",
                "battery/soc_min": "soc_min",
                "battery/soc_max": "soc_max",
            }
            if self.on_mqtt_command and topic in topic_command_map:
                self.on_mqtt_command(
                    {topic_command_map[topic]: self.topics_publish[topic]["set_value"]}
                )

    def __connect(self):
        """
        Connect to the MQTT broker.
        """
        try:
            self.client.connect(self.broker, self.port)
            return True
        except mqtt.socket.error as e:
            logger.error(
                "[MQTT] Network error while" + " connecting to MQTT broker %s:%d - %s",
                self.broker,
                self.port,
                e,
            )
            self.enable_mqtt = False
            self.mqtt_connection_failed = True  # Mark as connection failure
        except mqtt.ssl.SSLError as e:
            logger.error(
                "[MQTT] SSL error while" + " connecting to MQTT broker %s:%d - %s",
                self.broker,
                self.port,
                e,
            )
            self.enable_mqtt = False
            self.mqtt_connection_failed = True  # Mark as connection failure
        except Exception as e:
            logger.error(
                "[MQTT] Failed to connect" + " to MQTT broker %s:%d - %s",
                self.broker,
                self.port,
                e,
            )
            self.enable_mqtt = False
            self.mqtt_connection_failed = True  # Mark as connection failure
        return False

    def __publish(self, topic, payload, qos=0, retain=False):
        """
        Publish a message to a topic.

        :param topic: Topic to publish to
        :param payload: Message payload
        :param qos: Quality of Service level (default: 0)
        :param retain: Retain the message (default: False)
        """
        # logger.debug("[MQTT] Publishing message to topic '%s': %s", topic, payload)
        self.client.publish(topic, payload, qos=qos, retain=retain)

    def __subscribe(self, topic, qos=0):
        """
        Subscribe to a topic.

        :param topic: Topic to subscribe to
        :param qos: Quality of Service level (default: 0)
        """
        logger.info("[MQTT] Subscribing to topic '%s' with QoS %d", topic, qos)
        self.client.subscribe(topic, qos=qos)

    def loop_forever(self):
        """
        Start the MQTT client loop to process network traffic and dispatch callbacks.
        """
        logger.info("[MQTT] Starting MQTT client loop.")
        self.client.loop_forever()

    def loop_start(self):
        """
        Start the MQTT client loop in a separate thread.
        """
        logger.info("[MQTT] Starting MQTT client loop in a separate thread.")
        self.client.loop_start()

    def shutdown(self):
        """
        Stop the MQTT client loop.
        """
        logger.info("[MQTT] Stopping MQTT client loop.")
        self.client.loop_stop()

    def __publish_topics_on_change(self):
        """
        Publish topics if they have changed since the last publish.
        """
        for topic, value in self.topics_publish.items():
            # Check if the topic is in the last published topics and if the value has changed
            if self.topics_publish_last[topic]["value"] != value["value"]:
                # logger.debug("[MQTT] Topic '%s' has changed, publishing new value: %s",
                # topic, value["value"])
                self.__publish(
                    self.base_topic + "/" + topic,
                    value["value"],
                    value["qos"],
                    value["retain"],
                )
                self.topics_publish_last[topic]["value"] = value["value"]

    def update_publish_topics(self, topics):
        """
        Update the publish topics with new values.

        :param topics: Dictionary of topics and their new values
        """
        if not self.enable_mqtt:
            if not self.mqtt_config_enabled:
                logger.debug(
                    "[MQTT] MQTT is disabled in configuration, skipping publish."
                )
            elif self.mqtt_connection_failed:
                logger.warning(
                    "[MQTT] MQTT connection to broker %s:%d failed during initialization,"
                    + " skipping publish. Check broker availability and credentials.",
                    self.broker,
                    self.port,
                )
            return
        for topic, value in topics.items():
            if topic in self.topics_publish:
                try:
                    self.topics_publish[topic]["value"] = value["value"]
                except KeyError as e:
                    logger.error(
                        "[MQTT] KeyError while updating publish topic => %s: %s",
                        topic,
                        e,
                    )
                except (TypeError, ValueError) as e:
                    logger.error("[MQTT] Error while updating publish topics: %s", e)
        self.__publish_topics_on_change()

    def __send_mqtt_discovery_messages(self) -> None:
        """Publish all offered mqtt discovery config messages"""
        for topic, value in self.topics_publish.items():
            self.__publish_mqtt_discovery_message(
                value["name"],
                "eos_connect_" + topic.replace("/", "_"),
                value["type"],
                value["device_class"],
                value["unit"],
                self.base_topic + "/" + topic,
                icon=value.get("icon") and value["icon"],
                command_topic=value.get("command_topic")
                and self.base_topic + "/" + value["command_topic"],
                entity_category=value.get("entity_category")
                and value["entity_category"],
                min_value=value.get("min") and value["min"],
                max_value=value.get("max") and value["max"],
                step_value=value.get("step") and value["step"],
                value_template=value.get("value_template") and value["value_template"],
                command_template=value.get("command_template")
                and value["command_template"],
                options=value.get("options") and value["options"],
            )

    def __publish_mqtt_discovery_message(
        self,
        name: str,
        unique_id: str,
        item_type: str,
        device_class: str,
        unit_of_measurement: str,
        state_topic: str,
        icon: Optional[str] = None,
        command_topic: Optional[str] = None,
        entity_category: Optional[str] = None,
        min_value=None,
        max_value=None,
        step_value=None,
        initial_value=None,
        options: Optional[str] = None,
        value_template: Optional[str] = None,
        command_template: Optional[str] = None,
    ) -> None:
        """
        Publish Home Assistant MQTT Auto Discovery message

        Home Assistant MQTT Auto Discovery
        https://www.home-assistant.io/docs/mqtt/discovery/
        item_type = sensor, switch, binary_sensor, select
        device_class = battery, power, energy, temperature, humidity,
                        timestamp, signal_strength, problem, connectivity

        """
        if self.client.is_connected():
            payload = {}
            payload["name"] = name
            payload["unique_id"] = unique_id
            payload["state_topic"] = state_topic
            if value_template:
                payload["value_template"] = value_template
            if command_topic:
                payload["command_topic"] = command_topic
            if command_template:
                payload["command_template"] = command_template
            if device_class:
                payload["device_class"] = device_class
            if unit_of_measurement:
                payload["unit_of_measurement"] = unit_of_measurement
            if icon:
                payload["icon"] = icon
            if item_type == "number":
                payload["min"] = min_value
                payload["max"] = max_value
                if step_value:
                    payload["step"] = step_value
                payload["mode"] = "box"
            if entity_category:
                payload["entity_category"] = entity_category
            if initial_value:
                payload["initial"] = initial_value
            if options:
                payload["options"] = options
            device = {
                "identifiers": "EOS_connect",
                "name": "EOS Connect",
                "manufacturer": "ohAnd",
                "model": "EOS_connect",
                "sw_version": __version__,
                "configuration_url": "https://github.com/ohAnd/EOS_connect",
            }
            payload["device"] = device
            logger.debug(
                "[MQTT] Sending HA AD config message for %s",
                self.auto_discover_topic
                + "/"
                + item_type
                + "/"
                + unique_id
                + "/config",
            )
            self.client.publish(
                self.auto_discover_topic
                + "/"
                + item_type
                + "/eos_connect/"
                + unique_id
                + "/config",
                json.dumps(payload),
                retain=True,
            )
