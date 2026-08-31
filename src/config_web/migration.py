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
import shutil
from typing import Any

from ruamel.yaml.error import YAMLError

from .store import ConfigStore
from .schema import ConfigSchema, BOOTSTRAP_KEYS, LEGACY_SENSOR_PLACEHOLDERS

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

    # Ensure feed-in pricing fields are set (backward compat: default to fixed mode)
    if "price.feed_in_source" not in batch:
        batch["price.feed_in_source"] = "fixed"
        logger.debug("[Migration] Added default feed_in_source=fixed for backward compatibility")
    if "price.feed_in_zone" not in batch:
        batch["price.feed_in_zone"] = "DK1"
    if "price.feed_in_static_adder" not in batch:
        batch["price.feed_in_static_adder"] = 0.0
    if "price.feed_in_multiplier" not in batch:
        batch["price.feed_in_multiplier"] = 1.0

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

    # Ensure feed-in pricing fields are set (backward compat: default to fixed mode)
    if "price.feed_in_source" not in batch:
        batch["price.feed_in_source"] = "fixed"
        logger.debug("[Migration] Added default feed_in_source=fixed for backward compatibility")
    if "price.feed_in_zone" not in batch:
        batch["price.feed_in_zone"] = "DK1"
    if "price.feed_in_static_adder" not in batch:
        batch["price.feed_in_static_adder"] = 0.0
    if "price.feed_in_multiplier" not in batch:
        batch["price.feed_in_multiplier"] = 1.0

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


_SENSOR_PLACEHOLDER_MIGRATION_KEY = "_migrated_sensor_placeholders_v1"

# Stored sensor keys that may still hold a value the wizard filled in for the user,
# paired with the placeholder that would have been stored.
_PLACEHOLDER_SENSOR_KEYS = {
    "load.load_sensor": "Load_Power",
    "load.car_charge_load_sensor": "Wallbox_Power",
    "load.additional_load_1_sensor": "additional_load_1_sensor",
    "battery.soc_sensor": "battery_SOC",
}


def migrate_sensor_placeholders_to_empty(store: ConfigStore) -> bool:
    """
    One-time migration: blank the sensor names that were only ever schema hints.

    A click-through wizard run stored ``Load_Power`` and ``battery_SOC`` as though
    they were answers. Connecting Home Assistant afterwards turned them into
    ``/api/states/Load_Power`` — a 404 on every poll, forever, with nothing in the UI
    tying it to a config field. Blanking them lets the interfaces' existing
    "sensor not configured" handling take over, which degrades to the built-in
    profile and says so.

    Overwriting with ``""`` rather than deleting is deliberate: ``_build_section``
    falls back to config.yaml *before* the schema default, and a legacy config.yaml
    may still carry ``load: {load_sensor: Load_Power}`` — a deleted key would come
    straight back on the next start.

    Skipped entirely for openHAB installations: unlike Home Assistant entity ids
    (lowercase ``domain.object_id``), ``Load_Power`` and ``battery_SOC`` are
    plausible real openHAB item names, so they cannot be assumed to be placeholders.

    Args:
        store: An opened ConfigStore instance.

    Returns:
        True if the migration ran (first time), False if it was already done.
    """
    if store.get(_SENSOR_PLACEHOLDER_MIGRATION_KEY, False):
        return False

    if store.get("data_source.type") == "openhab":
        logger.info(
            "[Migration] Skipping sensor placeholder cleanup — openHAB item names "
            "are indistinguishable from the old placeholders"
        )
        store.set(_SENSOR_PLACEHOLDER_MIGRATION_KEY, True)
        return True

    for key, placeholder in _PLACEHOLDER_SENSOR_KEYS.items():
        if store.get(key) == placeholder:
            store.set(key, "")
            logger.info(
                "[Migration] Cleared placeholder %s for %s — it was never a real "
                "entity. Set your own under Settings if you need it.",
                placeholder,
                key,
            )

    store.set(_SENSOR_PLACEHOLDER_MIGRATION_KEY, True)
    return True


PRUNE_BACKUP_SUFFIX = ".migrated.bak"


def prune_migrated_yaml(config_manager, store, persistence_state: str) -> bool:
    """
    Reduce a fully-migrated legacy config.yaml to its bootstrap keys.

    Once a pre-database config.yaml has been imported it is no longer the source of
    truth, but ``migrate_yaml_to_store`` re-imports it verbatim whenever it finds an
    empty store — every image update, for a Docker user whose database is not on a
    volume. Removing the migrated values removes what resurrects them (#287).

    Refuses to act unless the settings are demonstrably safe elsewhere: the migration
    must have imported a real user config rather than bare defaults, the file must
    still hold non-bootstrap keys, and the data directory must not be ``"ephemeral"``
    — pruning there would swap stale-but-working values for an empty wizard on every
    update. The original is copied aside first and every OSError is logged rather
    than raised, so a read-only mount cannot stop startup.

    Args:
        config_manager: The ConfigManager owning config.yaml.
        store: An opened ConfigStore, consulted for the migration marker.
        persistence_state: State from ``ConfigManager.data_dir_persistence()``.

    Returns:
        True if config.yaml was rewritten, False in every other case.
    """
    if config_manager.is_ha_addon:
        return False  # no config.yaml in addon mode; options.json is Supervisor's

    if not store.get("_migrated_from_yaml", False):
        return False

    config_file = config_manager.config_file
    if not os.path.isfile(config_file):
        return False

    try:
        with open(config_file, "r", encoding="utf-8") as handle:
            existing = config_manager.yaml.load(handle)
    except (OSError, YAMLError) as exc:
        logger.debug("[Migration] Cannot inspect %s for pruning: %s", config_file, exc)
        return False

    if not existing:
        return False

    legacy_keys = [key for key in existing if key not in BOOTSTRAP_KEYS]
    if not legacy_keys:
        return False  # already bootstrap-only

    if persistence_state == "ephemeral":
        logger.warning(
            "[Migration] %s still holds pre-database settings (%s) and they will be "
            "re-imported on every container recreate, because %s is not on a volume. "
            "Leaving the file alone so those values remain as a fallback. In "
            'docker-compose.yml add "- ./data:/app/data" under volumes:, or set '
            "EOS_DATA_PATH to a path you already persist. Until then use "
            "Settings > Backup to export your configuration before each update.",
            config_file,
            ", ".join(sorted(legacy_keys)),
            config_manager.data_dir,
        )
        return False

    backup_path = config_file + PRUNE_BACKUP_SUFFIX
    try:
        shutil.copy2(config_file, backup_path)
    except OSError as exc:
        logger.warning(
            "[Migration] Could not back up %s (%s) - leaving it unchanged",
            config_file,
            exc,
        )
        return False

    # Start from the commented defaults so the rewritten file keeps its explanations,
    # then carry over every bootstrap key the file had. Keying off BOOTSTRAP_KEYS and
    # not just the defaults matters: data_path is bootstrap but deliberately absent
    # from create_default_config(), so the defaults alone would delete a hand-authored
    # one. "web_port" is an options.json-only alias and has no place in config.yaml.
    bootstrap_only = config_manager.create_default_config()
    for key, value in existing.items():
        if key in BOOTSTRAP_KEYS and key != "web_port":
            bootstrap_only[key] = value

    try:
        with open(config_file, "w", encoding="utf-8") as handle:
            config_manager.yaml.dump(bootstrap_only, handle)
    except OSError as exc:
        logger.warning(
            "[Migration] Could not rewrite %s (%s) - the backup at %s still holds "
            "the original",
            config_file,
            exc,
            backup_path,
        )
        return False

    logger.info(
        "[Migration] Reduced %s to bootstrap keys; %d migrated setting(s) now live "
        "only in the database. Original saved as %s.",
        config_file,
        len(legacy_keys),
        backup_path,
    )
    return True


_BATTERY_PRICE_UNIT_MIGRATION_KEY = "_migrated_battery_price_unit_v2"


def migrate_battery_price_unit_to_ct_kwh(store: ConfigStore) -> bool:
    """
    One-time migration: ``battery.price_euro_per_wh_accu`` (€/Wh) becomes
    ``battery.price_ct_kwh_accu`` (ct/kWh).

    The old key name no longer matched its unit once the field was switched
    to ct/kWh, so the field itself is renamed. Existing installations with a
    nonzero value configured have it rescaled (×100000, €/Wh → ct/kWh) and
    moved to the new key, so the real-world price they configured is
    preserved across the change. Runs exactly once, guarded by a marker key.

    Args:
        store: An opened ConfigStore instance.

    Returns:
        True if the migration ran (first time), False if it was already done.
    """
    if store.get(_BATTERY_PRICE_UNIT_MIGRATION_KEY, False):
        return False

    old_value = store.get("battery.price_euro_per_wh_accu")
    if old_value:
        new_value = old_value * 100000
        store.set("battery.price_ct_kwh_accu", new_value)
        logger.info(
            "[Migration] Moved battery.price_euro_per_wh_accu (%s €/Wh) to "
            "battery.price_ct_kwh_accu (%s ct/kWh)",
            old_value,
            new_value,
        )
    store.delete("battery.price_euro_per_wh_accu")

    store.set(_BATTERY_PRICE_UNIT_MIGRATION_KEY, True)
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
    placeholders = {"", None} | LEGACY_SENSOR_PLACEHOLDERS
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

    Lists of dicts (like pv_forecast) are expanded into indexed keys
    so that the web UI and export API can consume them directly.
    Plain lists (non-dict items) are stored as-is.

    Examples:
        {"load": {"source": "ha"}}          -> {"load.source": "ha"}
        {"refresh_time": 3}                  -> {"refresh_time": 3}
        {"pv_forecast": [{"name": "Roof"}]} -> {"pv_forecast.0.name": "Roof"}
    """
    result = {}
    for key, value in config_dict.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"

        if isinstance(value, list):
            base = full_key if prefix else key
            if value and isinstance(value[0], dict):
                # Expand list-of-dicts to indexed keys (e.g. pv_forecast)
                for idx, item in enumerate(value):
                    for sub_key, sub_val in item.items():
                        result[f"{base}.{idx}.{sub_key}"] = sub_val
            else:
                # Plain lists stored as-is
                result[base] = value
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
