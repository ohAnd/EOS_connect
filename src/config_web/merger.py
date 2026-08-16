"""
Merged Config Builder — Produces a config dict in the EXACT same shape as
``config_manager.config`` so that all existing interfaces work without changes.

Reads bootstrap values from config.yaml (via ConfigManager) and all other values
from the SQLite store. Resolves the unified ``data_source`` into per-section
``source``/``url``/``access_token`` fields so interfaces receive the same dict shape.
"""

import logging
import re
from typing import Any

from .store import ConfigStore
from .schema import ConfigSchema, BOOTSTRAP_KEYS

logger = logging.getLogger("__main__")

# Sections that receive data_source inheritance
_DATA_SOURCE_SECTIONS = ("load", "battery")


def build_merged_config(
    bootstrap_config: dict,
    store: ConfigStore,
    schema: ConfigSchema,
) -> dict:
    """
    Build a merged config dict that has the EXACT same shape as ``config_manager.config``.

    Priority order for each value:
    1. SQLite store (user-edited via web UI)
    2. Bootstrap config.yaml (for bootstrap-only keys)
    3. Schema defaults (fallback)

    Args:
        bootstrap_config: The ``config_manager.config`` dict (for bootstrap keys).
        store: An opened ConfigStore with migrated/edited values.
        schema: The ConfigSchema registry.

    Returns:
        A nested dict identical in shape to the original config dict.
    """
    all_settings = store.get_all()
    defaults = schema.defaults_dict()
    result = {}

    # Build sections from schema
    for section in schema.sections():
        if section == "data_source":
            # data_source is internal — not passed to interfaces
            continue

        if section == "system":
            # System-level keys are top-level (no nesting)
            continue

        if section == "pv_forecast":
            # pv_forecast is a list — handle specially
            continue

        result[section] = _build_section(section, all_settings, bootstrap_config, defaults)

    # Top-level (system) keys
    for field_def in schema.get_section("system"):
        key = field_def.key
        if key in BOOTSTRAP_KEYS:
            result[key] = bootstrap_config.get(key, field_def.default)
        elif key in all_settings:
            result[key] = all_settings[key]
        else:
            result[key] = bootstrap_config.get(key, field_def.default)

    # pv_forecast — list of installations
    result["pv_forecast"] = _build_pv_forecast(all_settings, bootstrap_config, defaults, schema)

    # Resolve data_source -> load/battery connection fields
    _apply_data_source_inheritance(result, all_settings)

    # Inject data_source credentials to inverter when type is homeassistant
    _apply_inverter_data_source_injection(result, all_settings)

    # Apply central HA data source for price and PV sources
    _apply_central_ha_data_source(result, all_settings)

    return result


def _build_section(
    section: str,
    all_settings: dict[str, Any],
    bootstrap_config: dict,
    defaults: dict,
) -> dict:
    """Build one config section dict from store values, falling back to defaults."""
    section_dict = {}
    section_defaults = defaults.get(section, {})
    bootstrap_section = bootstrap_config.get(section, {})

    # Collect all keys for this section from store
    prefix = f"{section}."
    for key, value in all_settings.items():
        if key.startswith(prefix):
            subkey = key[len(prefix):]
            section_dict[subkey] = value

    # Fill in missing keys from bootstrap config, then defaults
    if isinstance(section_defaults, dict):
        for subkey, default_val in section_defaults.items():
            if subkey not in section_dict:
                # Try bootstrap config
                if isinstance(bootstrap_section, dict) and subkey in bootstrap_section:
                    section_dict[subkey] = bootstrap_section[subkey]
                else:
                    section_dict[subkey] = default_val

    return section_dict


def _build_pv_forecast(
    all_settings: dict[str, Any],
    bootstrap_config: dict,
    defaults: dict,
    schema: ConfigSchema,
) -> list:
    """
    Rebuild the pv_forecast list from store values.

    Two storage formats are supported:
    1. Web UI format (preferred): indexed keys like ``pv_forecast.0.name``,
       ``pv_forecast.1.lat``, etc.
    2. Migration format (fallback): entire list under key ``"pv_forecast"``

    Missing fields in entries are filled with schema defaults.
    Falls back to bootstrap config if neither is found.
    """
    # Try format 1: indexed keys (from web UI) — preferred, checked first
    result_dict = {}
    for key, value in all_settings.items():
        if key.startswith("pv_forecast."):
            # Extract index and subkey: pv_forecast.0.name -> (0, "name")
            parts = key.split(".")
            if len(parts) >= 3 and parts[1].isdigit():
                idx = int(parts[1])
                subkey = ".".join(parts[2:])
                if idx not in result_dict:
                    result_dict[idx] = {}
                result_dict[idx][subkey] = value

    if result_dict:
        # Rebuild list from dict, sorted by index, filling in missing fields from schema
        result_list = []
        for i in sorted(result_dict.keys()):
            entry = result_dict[i].copy()
            # Fill in missing fields with schema defaults
            for field_def in schema.get_section("pv_forecast"):
                # Extract the field name (e.g., "pv_forecast.name" -> "name")
                field_name = field_def.key.split(".")[-1]
                if field_name not in entry:
                    entry[field_name] = field_def.default
            result_list.append(entry)
        return result_list

    # Try format 1.5: unindexed template keys like pv_forecast.name, pv_forecast.lat
    template_entry = {}
    for key, value in all_settings.items():
        if key.startswith("pv_forecast.") and not re.match(r"^pv_forecast\.\d+\.", key):
            parts = key.split(".")
            if len(parts) == 2:
                template_entry[parts[1]] = value

    if template_entry:
        logger.warning(
            "[Merger] Found unindexed pv_forecast template keys in the store; synthesizing one installation"
        )
        entry = template_entry.copy()
        for field_def in schema.get_section("pv_forecast"):
            field_name = field_def.key.split(".")[-1]
            if field_name not in entry:
                entry[field_name] = field_def.default
        return [entry]

    # Try format 2: single key (from migration) — fallback
    if "pv_forecast" in all_settings:
        stored = all_settings["pv_forecast"]
        if isinstance(stored, list):
            return stored

    # Last resort: fallback to bootstrap config or defaults
    return bootstrap_config.get(
        "pv_forecast",
        defaults.get("pv_forecast", []),
    )


def _apply_data_source_inheritance(result: dict, all_settings: dict[str, Any]) -> None:
    """
    Resolve ``data_source.*`` into ``load`` and ``battery`` connection fields.

    Per-section overrides (Expert level) take precedence. If a section already has
    its own ``source``/``url``/``access_token`` with non-default values, those win.
    Otherwise the global ``data_source`` values are injected.
    """
    ds_type = all_settings.get("data_source.type", "default")
    ds_url = all_settings.get("data_source.url", "")
    ds_token = all_settings.get("data_source.access_token", "")
    ds_ssl_ignore = all_settings.get("data_source.ssl_ignore", False)

    for section in _DATA_SOURCE_SECTIONS:
        if section not in result:
            continue

        sec = result[section]

        # Only inherit if section doesn't have its own override
        current_source = sec.get("source", "default")
        if current_source in ("default", "", None):
            sec["source"] = ds_type
            sec["url"] = ds_url
            sec["access_token"] = ds_token
        # Inject ssl_ignore regardless of source override
        sec["ssl_ignore"] = ds_ssl_ignore
        # If section has its own source set, keep it (Expert override)


def _apply_inverter_data_source_injection(result: dict, all_settings: dict[str, Any]) -> None:
    """
    Inject data_source URL and token to inverter when type is homeassistant.

    When inverter.type is "homeassistant", inject the data_source.url and
    data_source.access_token as inverter.url and inverter.token respectively.
    This ensures the inverter uses the same HA credentials as the load/battery interfaces.
    """
    if "inverter" not in result:
        return

    inverter = result["inverter"]
    if inverter.get("type") != "homeassistant":
        return

    ds_url = all_settings.get("data_source.url", "")
    ds_token = all_settings.get("data_source.access_token", "")
    ds_ssl_ignore = all_settings.get("data_source.ssl_ignore", False)

    # Inject credentials from data_source
    inverter["url"] = ds_url
    inverter["token"] = ds_token
    inverter["ssl_ignore"] = ds_ssl_ignore


def _apply_central_ha_data_source(result: dict, all_settings: dict[str, Any]) -> None:
    """
    Apply central Home Assistant data source to price, pv_forecast_source, and pv_autoscaling.

    When price.use_ha_central_data_source, pv_forecast_source.use_ha_central_data_source,
    or pv_autoscaling.use_ha_central_data_source is true, construct the connection details
    from the centrally configured data_source (url and access_token), avoiding repetition
    for end users.

    Args:
        result: The merged config dict to modify in-place.
        all_settings: All settings from the store.
    """
    ds_type = all_settings.get("data_source.type", "homeassistant")
    ds_url = all_settings.get("data_source.url", "")
    ds_token = all_settings.get("data_source.access_token", "")
    ds_ssl_ignore = all_settings.get("data_source.ssl_ignore", False)

    # Apply to price section
    if "price" in result:
        price = result["price"]
        if price.get("use_ha_central_data_source"):
            sensor_name = price.get("ha_sensor_name", "sensor.grid_prices")
            # Construct HA API URL from sensor entity
            price["data_url"] = f"{ds_url}/api/states/{sensor_name}"
            price["data_token"] = ds_token

    # Apply to pv_forecast_source section
    if "pv_forecast_source" in result:
        pv_source = result["pv_forecast_source"]
        if pv_source.get("use_ha_central_data_source"):
            sensor_name = pv_source.get("ha_sensor_name", "sensor.pv_forecast")
            # Construct HA API URL from sensor entity
            pv_source["data_url"] = f"{ds_url}/api/states/{sensor_name}"
            pv_source["data_token"] = ds_token

    # Apply to pv_autoscaling section
    if "pv_autoscaling" in result:
        pv_auto = result["pv_autoscaling"]
        if pv_auto.get("use_ha_central_data_source"):
            # For autoscaler, apply src, url, and token from central data_source
            pv_auto["src"] = ds_type
            pv_auto["url"] = ds_url
            pv_auto["access_token"] = ds_token
            pv_auto["ssl_ignore"] = ds_ssl_ignore
