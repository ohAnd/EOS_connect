"""
Appliance types, and what each one means.

A managed load's ``type`` is not a separate implementation - four of the six resolve to
the same `ThermalStorageModel`. What a type actually selects is a set of starting values
and a couple of behavioural switches: where the ambient temperature comes from, whether
a cover and a season are meaningful, how long the thing should run once started.

Keeping that here rather than in the model is what makes "add a sauna" a table entry.
Adding a space-heating type later is one more entry plus a profile-kind model; nothing
in the framework has to learn about it.
"""

import logging

from .models.external import ExternalPushModel
from .models.thermal import ThermalStorageModel

logger = logging.getLogger("__main__")

TYPE_POOL_HEATPUMP = "pool_heatpump"
TYPE_SAUNA = "sauna"
TYPE_HOT_WATER_TANK = "hot_water_tank"
TYPE_BUFFER_TANK = "buffer_tank"
TYPE_EXTERNAL_CONTINGENT = "external_contingent"
TYPE_EXTERNAL_PROFILE = "external_profile"

# Where a type reads its ambient temperature from.
AMBIENT_OUTDOOR = "outdoor"   # the PV interface's outdoor temperature forecast
AMBIENT_INDOOR = "indoor"     # a configured sensor, or a fixed fallback

# Types whose demand comes from stored heat, so the thermal fields apply to them. Kept
# as a tuple because the config schema serialises it into ``depends_on`` for the UI.
THERMAL_TYPES = (
    TYPE_POOL_HEATPUMP,
    TYPE_SAUNA,
    TYPE_HOT_WATER_TANK,
    TYPE_BUFFER_TANK,
)

# Types the planner and the release gate apply to.
CONTINGENT_TYPES = THERMAL_TYPES + (TYPE_EXTERNAL_CONTINGENT,)

# Types fed by a push rather than by sensors.
EXTERNAL_TYPES = (TYPE_EXTERNAL_CONTINGENT, TYPE_EXTERNAL_PROFILE)

# Types for which a cover, and a season, mean anything.
COVER_TYPES = (TYPE_POOL_HEATPUMP,)
SEASON_TYPES = (TYPE_POOL_HEATPUMP,)


PRESETS = {
    TYPE_POOL_HEATPUMP: {
        "label": "Pool heat pump",
        "model": ThermalStorageModel,
        "ambient": AMBIENT_OUTDOOR,
        "default_ambient_c": 15.0,
        "defaults": {
            "volume_m3": 30.0,
            "surface_m2": 32.0,
            "heat_loss_w_per_m2_k": 25.0,
            "rated_power_w": 2500.0,
            "cop_nominal": 5.0,
            "cop_air_coeff": 0.03,
            "target_temp": 28.0,
            "deadband_k": 0.5,
            "cover_loss_factor": 0.35,
            # An air-to-water pool pump barely works below about 12 C and a plate heat
            # exchanger can freeze, so both a floor and a frost trip are set by default.
            "min_ambient_temp_c": 12.0,
            "frost_protection_temp_c": 4.0,
            # Daylight hours: the COP is far better warm, and nobody wants the pump
            # audible at 03:00 for the sake of two cents.
            "window_start": 8,
            "window_end": 20,
            "season_start": "04-15",
            "season_end": "09-30",
            "min_runtime_minutes": 30,
            "max_runtime_hours_per_day": 12,
            "strategy": "combined",
        },
    },
    TYPE_SAUNA: {
        "label": "Sauna",
        "model": ThermalStorageModel,
        "ambient": AMBIENT_INDOOR,
        "default_ambient_c": 20.0,
        "defaults": {
            # A sauna heats air and stones, not water. The volume is a water-equivalent
            # fitted to a typical heat-up time; the calibrator corrects it from the
            # first real session onwards.
            "volume_m3": 0.08,
            "surface_m2": 12.0,
            "heat_loss_w_per_m2_k": 5.0,
            "rated_power_w": 6000.0,
            "cop_nominal": 1.0,
            "cop_air_coeff": 0.0,
            "target_temp": 90.0,
            "deadband_k": 3.0,
            "cover_loss_factor": 1.0,
            "min_ambient_temp_c": None,
            "frost_protection_temp_c": None,
            "window_start": None,
            "window_end": None,
            "season_start": None,
            "season_end": None,
            # A sauna is wanted at a time, not merely cheaply; the deadline carries that.
            "min_runtime_minutes": 15,
            "max_runtime_hours_per_day": 0,
            "deadline_hours": 6,
            "strategy": "cheapest_slots",
        },
    },
    TYPE_HOT_WATER_TANK: {
        "label": "Hot water tank",
        "model": ThermalStorageModel,
        "ambient": AMBIENT_INDOOR,
        "default_ambient_c": 18.0,
        "defaults": {
            "volume_m3": 0.3,
            "surface_m2": 3.5,
            "heat_loss_w_per_m2_k": 1.5,
            "rated_power_w": 2000.0,
            "cop_nominal": 3.0,
            "cop_air_coeff": 0.01,
            "target_temp": 55.0,
            "deadband_k": 3.0,
            "cover_loss_factor": 1.0,
            "min_ambient_temp_c": None,
            "frost_protection_temp_c": None,
            "window_start": None,
            "window_end": None,
            "season_start": None,
            "season_end": None,
            "min_runtime_minutes": 20,
            "max_runtime_hours_per_day": 0,
            "strategy": "combined",
        },
    },
    TYPE_BUFFER_TANK: {
        "label": "Buffer tank",
        "model": ThermalStorageModel,
        "ambient": AMBIENT_INDOOR,
        "default_ambient_c": 18.0,
        "defaults": {
            "volume_m3": 0.8,
            "surface_m2": 5.0,
            "heat_loss_w_per_m2_k": 1.2,
            "rated_power_w": 3000.0,
            "cop_nominal": 3.5,
            "cop_air_coeff": 0.02,
            "target_temp": 45.0,
            "deadband_k": 2.0,
            "cover_loss_factor": 1.0,
            "min_ambient_temp_c": None,
            "frost_protection_temp_c": None,
            "window_start": None,
            "window_end": None,
            "season_start": None,
            "season_end": None,
            "min_runtime_minutes": 20,
            "max_runtime_hours_per_day": 0,
            "strategy": "combined",
        },
    },
    TYPE_EXTERNAL_CONTINGENT: {
        "label": "External energy budget",
        "model": ExternalPushModel,
        "ambient": AMBIENT_INDOOR,
        "default_ambient_c": 18.0,
        "defaults": {
            "rated_power_w": 2000.0,
            "min_runtime_minutes": 0,
            "max_runtime_hours_per_day": 0,
            "strategy": "combined",
            "ttl_minutes": 1440,
        },
    },
    TYPE_EXTERNAL_PROFILE: {
        "label": "External load profile",
        "model": ExternalPushModel,
        "ambient": AMBIENT_INDOOR,
        "default_ambient_c": 18.0,
        "defaults": {
            "ttl_minutes": 1440,
        },
    },
}

TYPES = tuple(PRESETS.keys())


def preset_for(type_name):
    """The preset for a type, or None when the type is unknown."""
    return PRESETS.get(str(type_name or "").strip())


def apply_defaults(entry):
    """
    Fill an entry's unset keys from its preset.

    Only keys the user has genuinely not set are filled: an explicit ``None`` in the
    stored config means "no limit" for fields like ``window_start``, and overwriting it
    with the preset would silently reimpose a restriction the user removed.
    """
    resolved = dict(entry or {})
    preset = preset_for(resolved.get("type"))
    if preset is None:
        return resolved
    for key, value in preset["defaults"].items():
        if key not in resolved:
            resolved[key] = value
    return resolved


def build_model(entry):
    """
    Construct the demand model for a config entry.

    Returns None for an unknown type rather than raising: a typo in one entry must not
    stop the other managed loads - or the optimizer - from running.
    """
    preset = preset_for(entry.get("type"))
    if preset is None:
        logger.error(
            "[LOADS] '%s' has unknown type %r - known types are: %s",
            entry.get("id", "?"), entry.get("type"), ", ".join(TYPES),
        )
        return None
    return preset["model"](entry.get("id"), entry)


def is_thermal(type_name):
    """Whether this type's demand comes from stored heat."""
    return type_name in THERMAL_TYPES


def is_external(type_name):
    """Whether this type is fed by a push rather than by sensors."""
    return type_name in EXTERNAL_TYPES


def uses_outdoor_ambient(type_name):
    """Whether this type reads the outdoor temperature forecast for its ambient."""
    preset = preset_for(type_name)
    return bool(preset and preset["ambient"] == AMBIENT_OUTDOOR)


def fallback_ambient_c(type_name):
    """Ambient to assume when nothing better is configured or forecast."""
    preset = preset_for(type_name)
    return preset["default_ambient_c"] if preset else 18.0
