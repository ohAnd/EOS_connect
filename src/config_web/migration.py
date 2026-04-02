"""
Auto-Migration — Imports existing config.yaml values into the SQLite config store.

On first run (empty database), reads the current config dict from ConfigManager,
flattens all values to dot-notation keys, imports them into the store, and creates
the new ``data_source`` section from the ``load`` section connection values.

When running as an HA addon, a legacy ``/data/options.json`` that contains more
than bootstrap keys is also auto-migrated to SQLite.
"""

import json
import logging
import os
from typing import Any

from .store import ConfigStore
from .schema import ConfigSchema

logger = logging.getLogger("__main__")

# Keys that stay in config.yaml / options.json (bootstrap) and are NOT migrated to SQLite.
# Includes both the config.yaml names and the HA addon options.json names.
_BOOTSTRAP_KEYS = frozenset({
    "eos_connect_web_port",
    "web_port",
    "time_zone",
    "log_level",
    "data_path",
})


def migrate_yaml_to_store(config_dict: dict, store: ConfigStore, schema: ConfigSchema) -> bool:
    """
    Migrate an existing config.yaml dict into the SQLite store.

    This runs only when the store is empty (first launch after upgrade).

    Args:
        config_dict: The current ``config_manager.config`` dict (nested).
        store: An opened ConfigStore instance.
        schema: The ConfigSchema registry.

    Returns:
        True if migration was performed, False if skipped (store already has data).
    """
    if not store.is_empty():
        logger.debug("[Migration] Store already has data — skipping migration")
        return False

    logger.info("[Migration] Empty store detected — migrating config.yaml to SQLite")

    flat = _flatten_config(config_dict)
    migrated_count = 0

    for key, value in flat.items():
        # Skip bootstrap keys
        top_key = key.split(".")[0] if "." in key else key
        if top_key in _BOOTSTRAP_KEYS or key in _BOOTSTRAP_KEYS:
            continue
        # Skip None values
        if value is None:
            continue
        store.set(key, value)
        migrated_count += 1

    # Create unified data_source from load section
    _create_data_source(config_dict, store)

    # Mark migration complete
    store.set("_migrated_from_yaml", True)
    store.set("_wizard_completed", True)  # existing config = not a fresh install

    logger.info(
        "[Migration] Migrated %d settings from config.yaml to SQLite",
        migrated_count,
    )
    return True


def migrate_ha_options_to_store(
    store: ConfigStore, schema: ConfigSchema, options_path: str = "/data/options.json"
) -> bool:
    """
    Migrate a legacy Home Assistant addon ``options.json`` to the SQLite store.

    Older HA addon versions stored the full configuration in ``options.json``
    (all fields, not just bootstrap). This function detects that situation and
    imports the non-bootstrap values into SQLite so users keep their settings
    after upgrading.

    The migration is skipped when:
    - The store already has data (migration already ran).
    - ``options.json`` does not exist.
    - ``options.json`` contains only bootstrap keys (new addon version).

    Args:
        store: An opened ConfigStore instance.
        schema: The ConfigSchema registry.
        options_path: Path to the HA options file (overridable for testing).

    Returns:
        True if migration was performed, False if skipped.
    """
    if not store.is_empty():
        logger.debug("[Migration] Store already has data — skipping HA options migration")
        return False

    if not os.path.exists(options_path):
        return False

    try:
        with open(options_path, "r", encoding="utf-8") as f:
            options = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[Migration] Failed to read %s: %s", options_path, exc)
        return False

    if not isinstance(options, dict):
        return False

    # Check whether options.json has more than just bootstrap keys
    non_bootstrap = {k for k in options if k not in _BOOTSTRAP_KEYS}
    if not non_bootstrap:
        logger.debug("[Migration] options.json contains only bootstrap keys — skipping")
        return False

    logger.info(
        "[Migration] Legacy HA options.json detected (%d non-bootstrap keys) — migrating to SQLite",
        len(non_bootstrap),
    )

    flat = _flatten_config(options)
    migrated_count = 0

    for key, value in flat.items():
        top_key = key.split(".")[0] if "." in key else key
        if top_key in _BOOTSTRAP_KEYS or key in _BOOTSTRAP_KEYS:
            continue
        if value is None:
            continue
        store.set(key, value)
        migrated_count += 1

    # Create unified data_source from load/battery sections
    _create_data_source(options, store)

    store.set("_migrated_from_ha_options", True)
    store.set("_wizard_completed", True)

    logger.info(
        "[Migration] Migrated %d settings from HA options.json to SQLite",
        migrated_count,
    )
    return True


def _flatten_config(config_dict: dict, prefix: str = "") -> dict[str, Any]:
    """
    Flatten a nested config dict to dot-notation keys.

    Lists (like pv_forecast) are stored as a single JSON list value
    under the section key directly, since they have dynamic length.

    Examples:
        {"load": {"source": "ha"}} -> {"load.source": "ha"}
        {"refresh_time": 3}        -> {"refresh_time": 3}
        {"pv_forecast": [...]}     -> {"pv_forecast": [...]}
    """
    result = {}
    for key, value in config_dict.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"

        if isinstance(value, list):
            # Store lists as-is (e.g. pv_forecast array)
            result[full_key if prefix else key] = value
        elif isinstance(value, dict):
            # Recurse into nested dicts
            nested = _flatten_config(value, full_key if prefix else key)
            result.update(nested)
        else:
            result[full_key if prefix else key] = value

    return result


def _create_data_source(config_dict: dict, store: ConfigStore) -> None:
    """
    Create the unified ``data_source`` section from ``load`` section values.

    If load has a real source (homeassistant/openhab), use those values.
    Otherwise try battery section as fallback.
    """
    load = config_dict.get("load", {})
    battery = config_dict.get("battery", {})

    # Prefer load section for data_source creation
    source = load.get("source", "default")
    url = load.get("url", "")
    token = load.get("access_token", "")

    # If load is 'default', try battery
    if source == "default":
        bat_source = battery.get("source", "default")
        if bat_source != "default":
            source = bat_source
            url = battery.get("url", url)
            token = battery.get("access_token", token)

    store.set("data_source.type", source)
    store.set("data_source.url", url)
    store.set("data_source.access_token", token)

    logger.info(
        "[Migration] Created data_source: type=%s, url=%s",
        source,
        url,
    )
