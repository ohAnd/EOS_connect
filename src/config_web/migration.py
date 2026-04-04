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
from .schema import ConfigSchema, BOOTSTRAP_KEYS

logger = logging.getLogger("__main__")


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
    batch = {}

    for key, value in flat.items():
        # Skip bootstrap keys
        top_key = key.split(".")[0] if "." in key else key
        if top_key in BOOTSTRAP_KEYS or key in BOOTSTRAP_KEYS:
            continue
        # Skip None values
        if value is None:
            continue
        # Coerce value to match schema type (e.g. "enabled" → True for bool)
        value = _coerce_migrated_value(schema, key, value)
        batch[key] = value

    # Create unified data_source from load section
    ds_batch = _create_data_source_batch(config_dict)
    batch.update(ds_batch)

    # Detect whether this is a real user config or just ConfigManager defaults.
    # A real config has at least one source field set to a non-default value.
    is_real_config = _has_user_configured_values(config_dict)
    if is_real_config:
        batch["_migrated_from_yaml"] = True
        batch["_wizard_completed"] = True

    # Atomic write — all or nothing
    try:
        migrated_count = store.set_batch(batch)
    except Exception:
        logger.exception("[Migration] Failed to write settings to SQLite — migration aborted")
        return False

    if is_real_config:
        logger.info(
            "[Migration] Migrated %d settings from config.yaml to SQLite",
            migrated_count,
        )
    else:
        # Fresh install with only defaults — don't mark as migrated so wizard shows
        logger.info(
            "[Migration] Stored %d defaults from config.yaml — wizard will appear for initial setup",
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
    non_bootstrap = {k for k in options if k not in BOOTSTRAP_KEYS}
    if not non_bootstrap:
        logger.debug("[Migration] options.json contains only bootstrap keys — skipping")
        return False

    logger.info(
        "[Migration] Legacy HA options.json detected (%d non-bootstrap keys) — migrating to SQLite",
        len(non_bootstrap),
    )

    flat = _flatten_config(options)
    batch = {}

    for key, value in flat.items():
        top_key = key.split(".")[0] if "." in key else key
        if top_key in BOOTSTRAP_KEYS or key in BOOTSTRAP_KEYS:
            continue
        if value is None:
            continue
        batch[key] = value

    # Create unified data_source from load/battery sections
    ds_batch = _create_data_source_batch(options)
    batch.update(ds_batch)

    batch["_migrated_from_ha_options"] = True
    batch["_wizard_completed"] = True

    # Atomic write — all or nothing
    try:
        migrated_count = store.set_batch(batch)
    except Exception:
        logger.exception("[Migration] Failed to write HA options to SQLite — migration aborted")
        return False

    logger.info(
        "[Migration] Migrated %d settings from HA options.json to SQLite",
        migrated_count,
    )
    return True


def _has_user_configured_values(config_dict: dict) -> bool:
    """
    Detect whether a config dict contains real user-configured values or just
    ConfigManager defaults.

    Checks sentinel fields that users must configure for a working setup.
    If all source fields are still ``"default"`` and no real sensors are set,
    the config is considered a fresh install (not a real migration).
    """
    # Check various source fields — a configured system has at least one non-default
    source_checks = [
        config_dict.get("load", {}).get("source", "default"),
        config_dict.get("battery", {}).get("source", "default"),
        config_dict.get("price", {}).get("source", "default"),
        config_dict.get("eos", {}).get("source", "default"),
    ]
    if any(s not in ("default", "", None) for s in source_checks):
        return True

    # Check if any sensor values differ from obvious placeholders
    sensor_checks = [
        config_dict.get("load", {}).get("load_sensor", ""),
        config_dict.get("battery", {}).get("soc_sensor", ""),
    ]
    placeholders = {"", "Load_Power", "battery_SOC", None}
    if any(s not in placeholders for s in sensor_checks):
        return True

    return False


def _coerce_migrated_value(schema: ConfigSchema, key: str, value: Any) -> Any:
    """
    Coerce a migrated value to match the schema field type.

    Handles legacy YAML values like "enabled"/"disabled" for bool fields.
    Also validates against schema choices, falling back to the schema default
    when the migrated value is not a valid choice (e.g. ConfigManager's
    ``"default"`` placeholder vs schema's ``"eos_server"``).
    """
    field_def = schema.get(key)
    if field_def is None:
        return value

    ft = field_def.field_type
    if ft == "bool" and isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "enabled")
    if ft == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if ft == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return value

    # Validate against choices — replace invalid values with schema default
    choices = field_def.validation.get("choices") if field_def.validation else None
    if choices and value not in choices:
        logger.warning(
            "[Migration] Value %r for %s is not in choices %s — using schema default %r",
            value, key, choices, field_def.default,
        )
        return field_def.default

    return value


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


def _create_data_source_batch(config_dict: dict) -> dict[str, Any]:
    """
    Build the unified ``data_source`` section from ``load`` section values.

    Returns a dict of key/value pairs to include in the migration batch.
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

    logger.info(
        "[Migration] Created data_source: type=%s, url=%s",
        source,
        url,
    )
    return {
        "data_source.type": source,
        "data_source.url": url,
        "data_source.access_token": token,
    }


def _create_data_source(config_dict: dict, store: ConfigStore) -> None:
    """
    Create the unified ``data_source`` section from ``load`` section values.

    Legacy wrapper — writes directly to store. Kept for backward compatibility
    with existing tests.
    """
    batch = _create_data_source_batch(config_dict)
    for key, value in batch.items():
        store.set(key, value)
