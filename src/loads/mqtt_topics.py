"""
MQTT topics for managed loads.

Every managed load gets its own set of entities, named after the id the user chose. This
module only *describes* them - it builds plain dicts in the shape
`MqttInterface.register_topics` expects and reads values out of the manager. Nothing
here imports the MQTT interface, so the whole mapping is testable without a broker.

The release flag is the point of the exercise: an appliance that cannot be told "run
now" or "wait" is only half-managed, and a binary sensor discovered by Home Assistant is
the shortest path from EOS Connect's plan to a relay.
"""

import json
import logging

from .gate import OVERRIDE_BLOCK, OVERRIDE_RELEASE
from .injection import InjectionError
from .presets import EXTERNAL_TYPES, THERMAL_TYPES

logger = logging.getLogger("__main__")

# Topic prefix under the interface's base topic (``eos_connect``).
PREFIX = "managed_load"

# The summed contribution, useful on its own for spotting a runaway push.
TOTAL_TOPIC = "load/contribution_total_wh"

_BOOL_TEMPLATE = "{{ 'ON' if value == 'true' else 'OFF' }}"


def _bool(value):
    """Booleans go out as the lower-case strings the built-in topics already use."""
    return "true" if value else "false"


def build_topics(manager):
    """
    Describe every topic for the configured managed loads.

    Returns ``{topic: definition}``, ready for `MqttInterface.register_topics`.
    """
    topics = {
        TOTAL_TOPIC: {
            "name": "Managed Loads Contribution",
            "unit": "Wh",
            "type": "sensor",
            "device_class": "energy",
            "icon": "mdi:transmission-tower-import",
        },
    }

    for item in manager.instances:
        topics.update(_topics_for(manager, item))
    return topics


def _topics_for(manager, item):
    """Every topic for one instance."""
    base = f"{PREFIX}/{item.id}"
    label = item.id.replace("_", " ").title()
    topics = {}

    if item.gates_release:
        topics[f"{base}/released"] = {
            "name": f"{label} Released",
            "type": "binary_sensor",
            "device_class": "running",
            "icon": "mdi:play-circle-outline",
            "value_template": _BOOL_TEMPLATE,
        }
        topics[f"{base}/state"] = {
            "name": f"{label} State",
            "type": "sensor",
            "icon": "mdi:state-machine",
        }
        topics[f"{base}/reason"] = {
            "name": f"{label} Reason",
            "type": "sensor",
            "icon": "mdi:comment-question-outline",
            "entity_category": "diagnostic",
        }
        topics[f"{base}/next_release_start"] = {
            "name": f"{label} Next Start",
            "type": "sensor",
            "device_class": "timestamp",
            "icon": "mdi:clock-start",
        }
        topics[f"{base}/override"] = {
            "name": f"{label} Override",
            "type": "sensor",
            "icon": "mdi:hand-back-right-outline",
            "entity_category": "diagnostic",
            "command_topic": f"{base}/override/set",
            "on_command": _override_handler(manager, item.id),
        }

    topics[f"{base}/energy_needed_wh"] = {
        "name": f"{label} Energy Needed",
        "unit": "Wh",
        "type": "sensor",
        "device_class": "energy",
        "icon": "mdi:lightning-bolt-outline",
    }
    topics[f"{base}/planned_wh"] = {
        "name": f"{label} Energy Planned",
        "unit": "Wh",
        "type": "sensor",
        "device_class": "energy",
        "icon": "mdi:calendar-clock",
    }

    if item.type in THERMAL_TYPES:
        topics[f"{base}/temperature"] = {
            "name": f"{label} Temperature",
            "unit": "°C",
            "type": "sensor",
            "device_class": "temperature",
            "icon": "mdi:thermometer",
        }
        topics[f"{base}/target_temperature"] = {
            "name": f"{label} Target Temperature",
            "unit": "°C",
            "type": "sensor",
            "device_class": "temperature",
            "icon": "mdi:thermometer-check",
        }
        topics[f"{base}/calibration_confidence"] = {
            "name": f"{label} Calibration Confidence",
            "unit": "%",
            "type": "sensor",
            "icon": "mdi:progress-check",
            "entity_category": "diagnostic",
        }

    if item.type in EXTERNAL_TYPES:
        topics[base] = {
            "name": f"{label} Pushed Energy",
            "unit": "Wh",
            "type": "sensor",
            "device_class": "energy",
            "icon": "mdi:import",
            "command_topic": f"{base}/set",
            # A pushed forecast is not a config value: losing it on reconnect would
            # leave the optimizer planning without it until the sender next publishes,
            # which may be hours away.
            "accept_retained": True,
            "on_command": _push_handler(manager, item.id),
        }

    return topics


def _push_handler(manager, load_id):
    """Handler for ``eos_connect/managed_load/<id>/set``."""

    def handle(payload):
        if payload is None:
            return
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            # A bare number published from an automation is the easiest thing to send,
            # and json.loads already accepts it - anything else is a genuine mistake.
            logger.error(
                "[LOADS] push to '%s' is not valid JSON or a number: %s",
                load_id, str(payload)[:200],
            )
            return
        try:
            manager.push(load_id, parsed, source="mqtt")
        except InjectionError as exc:
            # A published message has nowhere to return an error to, so the log is the
            # only channel the sender has.
            logger.error("[LOADS] push to '%s' refused: %s", load_id, exc)

    return handle


def _override_handler(manager, load_id):
    """Handler for ``eos_connect/managed_load/<id>/override/set``."""

    def handle(payload):
        if payload is None:
            return
        mode, minutes = _parse_override(payload)
        if mode is False:
            logger.error(
                "[LOADS] override for '%s' not understood: %s - expected "
                "'release', 'block', 'clear', or {\"mode\": ..., \"minutes\": ...}",
                load_id, str(payload)[:200],
            )
            return
        try:
            manager.set_override(load_id, mode, minutes)
        except (InjectionError, ValueError) as exc:
            logger.error("[LOADS] override for '%s' refused: %s", load_id, exc)

    return handle


def _parse_override(payload):
    """``"release"`` or ``{"mode": "block", "minutes": 30}`` to ``(mode, minutes)``."""
    text = str(payload).strip()
    minutes = 60
    mode = text.lower()

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return False, 0
        if not isinstance(data, dict):
            return False, 0
        mode = str(data.get("mode", "")).strip().lower()
        try:
            minutes = int(data.get("minutes", 60))
        except (TypeError, ValueError):
            return False, 0

    if mode in ("clear", "none", "off", ""):
        return None, minutes
    if mode in (OVERRIDE_RELEASE, OVERRIDE_BLOCK):
        return mode, max(1, min(minutes, 1440))
    return False, 0


def build_values(manager):
    """
    Current values for every registered topic, for `update_publish_topics`.

    Only topics that exist are filled: a load whose cycle has not run yet publishes
    nothing rather than a misleading zero.
    """
    values = {TOTAL_TOPIC: {"value": manager.contribution_total_wh()}}

    for item in manager.instances:
        base = f"{PREFIX}/{item.id}"
        demand = item.last_demand
        release = item.last_release

        if release is not None:
            values[f"{base}/released"] = {"value": _bool(release.get("released"))}
            values[f"{base}/state"] = {"value": release.get("state")}
            values[f"{base}/reason"] = {"value": release.get("reason")}
            values[f"{base}/next_release_start"] = {
                "value": release.get("next_release_start")
            }
            values[f"{base}/override"] = {"value": release.get("override") or "none"}

        if demand is not None:
            values[f"{base}/energy_needed_wh"] = {"value": round(demand.total_wh, 1)}
            values[f"{base}/planned_wh"] = {"value": round(sum(item.last_plan), 1)}

            detail = demand.detail or {}
            if item.type in THERMAL_TYPES:
                if "temperature_c" in detail:
                    values[f"{base}/temperature"] = {"value": detail["temperature_c"]}
                if "target_temperature_c" in detail:
                    values[f"{base}/target_temperature"] = {
                        "value": detail["target_temperature_c"]
                    }
                confidence = (item.model.status() or {}).get("confidence")
                if confidence is not None:
                    values[f"{base}/calibration_confidence"] = {
                        "value": round(confidence * 100, 1)
                    }

        if item.type in EXTERNAL_TYPES:
            values[base] = {"value": round(sum(item.last_plan), 1) if item.last_plan else 0}

    return values
