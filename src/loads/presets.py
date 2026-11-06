"""
Appliance types, and what each one means.

A managed load's ``type`` is not a separate implementation - four of the six resolve to
the same `ThermalStorageModel`. What a type actually selects is a set of starting values
and a couple of behavioural switches: where the ambient temperature comes from, whether
a cover and a season are meaningful, how long the thing should run once started.

Keeping that here rather than in the model is what makes "add a sauna" a table entry.
A house is one too: it is a store whose medium is the rooms, so it reuses the same
model and calibration, and what its preset adds is a way to derive the starting values
from the things an owner knows - see `loads.models.building`.
"""

import logging

from .models import building
from .models.external import ExternalPushModel
from .models.thermal import ThermalStorageModel

logger = logging.getLogger("__main__")

TYPE_POOL_HEATPUMP = "pool_heatpump"
TYPE_SAUNA = "sauna"
TYPE_HOT_WATER_TANK = "hot_water_tank"
TYPE_BUFFER_TANK = "buffer_tank"
TYPE_SPACE_HEATING = "space_heating"
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
    TYPE_SPACE_HEATING,
)

# Types the planner and the release gate apply to.
CONTINGENT_TYPES = THERMAL_TYPES + (TYPE_EXTERNAL_CONTINGENT,)

# Types fed from outside rather than from sensors.
EXTERNAL_TYPES = (TYPE_EXTERNAL_CONTINGENT, TYPE_EXTERNAL_PROFILE)

# Where an external load profile gets its array from. Push is the original behaviour -
# an automation hands one over - and stays the default; timeseries names a source EOS
# Connect fetches itself.
PROFILE_SOURCE_PUSH = "push"
PROFILE_SOURCE_TIMESERIES = "timeseries"

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
    TYPE_SPACE_HEATING: {
        "label": "Space heating (heat pump)",
        "model": ThermalStorageModel,
        "ambient": AMBIENT_OUTDOOR,
        "default_ambient_c": 7.0,
        # The house is the store: its rooms are the medium, the outdoor air is what it
        # loses heat to. The physical values are not set here but derived from what
        # the owner knows - living area, construction, heat emitter, building form and
        # insulation - by `building.starting_values`, and calibrated from there.
        "derive": building.starting_values,
        "derived_keys": building.DERIVED_KEYS,
        # What the calibrator may and may not learn for this store. Measured on a real
        # house, both defaults of the pool were wrong for it:
        #
        # - The loss coefficient floor of 1.0 W/(m2 K) sits four times above a
        #   renovated house (0.25), so the fit clamped there and reported no confidence.
        # - Learning the COP from the room temperature does not work when the heat
        #   pump mostly runs while the sun is out, which is exactly what a PV-led plan
        #   makes it do: electrical power and sunshine rise together and the fit cannot
        #   tell their heat apart. It drifted from 3.3 to 0.8 over a week while the
        #   heat meter read 4.0, and the room temperature prediction stayed good the
        #   whole time - so nothing would ever have flagged it. The COP is held at its
        #   starting value instead, and the losses and the sun are learned around it.
        "calibration": {
            "loss_coefficient_min": 0.02,
            "loss_coefficient_max": 5.0,
            "learn_cop": False,
            # A window has to span a real change of the house, not a flicker of one
            # room: 0.2 K, against the three sensor ticks that serve a pool.
            "min_window_change_k": 0.2,
        },
        "defaults": {
            **building.DEFAULT_INPUTS,
            # What the default inputs produce, so the documentation can show a house
            # starting from real numbers. Derived values for the entry's own inputs
            # take precedence over these - see `apply_defaults`.
            **building.starting_values(building.DEFAULT_INPUTS),
            # A modulating heat pump draws well under its nameplate most of the time;
            # this is the typical draw while running, which is what the plan needs.
            "rated_power_w": 1500.0,
            "target_temp": 21.5,
            "deadband_k": 0.5,
            "cover_loss_factor": 1.0,
            # An air-source heat pump for a house is built to run far below zero.
            "min_ambient_temp_c": None,
            # Below this the house is cold enough that price no longer matters.
            "frost_protection_temp_c": 19.0,
            "window_start": None,
            "window_end": None,
            "season_start": None,
            "season_end": None,
            # Floor heating takes an hour to deliver anything a room notices.
            "min_runtime_minutes": 60,
            "max_runtime_hours_per_day": 0,
            "strategy": "combined",
            # Prefer the warm hours by the model's own COP - the point of planning a
            # heat pump rather than just switching it.
            "efficiency_source": "cop",
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
            # Waiting to be handed a profile is what this type has always done, so an
            # entry that predates the fetched source keeps behaving exactly as it did.
            "profile_source": PROFILE_SOURCE_PUSH,
            "use_ha_central_data_source": True,
            "ha_sensor_name": "",
            "data_path": "attributes.data",
            "data_url": "",
            "data_token": "",
            "value_unit": "W",
            "rated_power_w": 0.0,
        },
    },
}

TYPES = tuple(PRESETS.keys())


def presets_as_json():
    """
    The per-type starting values, as plain data.

    What the settings form seeds a new or retyped entry with, and what the
    documentation renders per profile. ``model`` and ``derive`` are code and are
    dropped; ``derived_keys`` names the fields a type computes for itself, which the
    form leaves empty so the runtime can fill them from the entry's own inputs. A
    ``None`` in ``defaults`` is kept, because it means "no limit" rather than "unset".
    """
    return {
        name: {
            "label": preset["label"],
            "ambient": preset["ambient"],
            "default_ambient_c": preset["default_ambient_c"],
            "defaults": dict(preset["defaults"]),
            "derived_keys": list(preset.get("derived_keys", ())),
        }
        for name, preset in PRESETS.items()
    }


def preset_for(type_name):
    """The preset for a type, or None when the type is unknown."""
    return PRESETS.get(str(type_name or "").strip())


def apply_defaults(entry):
    """
    Fill an entry's unset keys from its preset.

    Only keys the user has genuinely not set are filled: an explicit ``None`` in the
    stored config means "no limit" for fields like ``window_start``, and overwriting it
    with the preset would silently reimpose a restriction the user removed.

    A preset with a ``derive`` function computes some of its values from others - a
    house's volume from its living area. Those are filled first, from the entry's own
    inputs, so a 200 m2 house does not start from the 150 m2 the preset documents. A
    derived key left empty counts as unset: unlike a window, it has no "no limit"
    meaning, and an empty volume would only break the model.
    """
    resolved = dict(entry or {})
    preset = preset_for(resolved.get("type"))
    if preset is None:
        return resolved
    derive = preset.get("derive")
    if derive is not None:
        inputs = {**preset["defaults"], **resolved}
        for key, value in derive(inputs).items():
            if resolved.get(key) in (None, ""):
                resolved[key] = value
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
    options = preset.get("calibration")
    if options:
        return preset["model"](entry.get("id"), entry, calibration=options)
    return preset["model"](entry.get("id"), entry)


def pulls_its_profile(entry):
    """
    Whether this entry fetches its own profile instead of waiting to be handed one.

    Reads the resolved entry rather than the type, because the two external types
    differ here: only a profile can be fetched. A budget and a deadline is not a
    timeseries, and nothing would know what to fetch.
    """
    if str(entry.get("type", "")).strip() != TYPE_EXTERNAL_PROFILE:
        return False
    return str(entry.get("profile_source", PROFILE_SOURCE_PUSH)).strip() == (
        PROFILE_SOURCE_TIMESERIES
    )


def uses_outdoor_ambient(type_name):
    """Whether this type reads the outdoor temperature forecast for its ambient."""
    preset = preset_for(type_name)
    return bool(preset and preset["ambient"] == AMBIENT_OUTDOOR)


def fallback_ambient_c(type_name):
    """Ambient to assume when nothing better is configured or forecast."""
    preset = preset_for(type_name)
    return preset["default_ambient_c"] if preset else 18.0
