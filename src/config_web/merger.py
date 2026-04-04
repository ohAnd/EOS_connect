"""
Merged Config Builder — Produces a config dict in the EXACT same shape as
``config_manager.config`` so that all existing interfaces work without changes.

Reads bootstrap values from config.yaml (via ConfigManager) and all other values
from the SQLite store. Resolves the unified ``data_source`` into per-section
``source``/``url``/``access_token`` fields so interfaces receive the same dict shape.
"""

import logging
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
    result["pv_forecast"] = _build_pv_forecast(all_settings, bootstrap_config, defaults)

    # Resolve data_source -> load/battery connection fields
    _apply_data_source_inheritance(result, all_settings)

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
) -> list:
    """
    Rebuild the pv_forecast list.

    The migration stores the entire list under the key ``pv_forecast``.
    If not found in the store, fall back to the bootstrap config.
    """
    if "pv_forecast" in all_settings:
        stored = all_settings["pv_forecast"]
        if isinstance(stored, list):
            return stored

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
        # If section has its own source set, keep it (Expert override)
