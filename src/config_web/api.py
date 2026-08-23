"""
REST API Blueprint — Flask Blueprint with all configuration endpoints.

Prefix: ``/api/config``

All routes are registered via a Flask Blueprint so that the main app
only needs one line: ``app.register_blueprint(config_bp)``.
"""

import json
import logging
import re
from flask import Blueprint, jsonify, request as flask_request, Response

from .migration import _flatten_config

logger = logging.getLogger("__main__")

config_bp = Blueprint("config", __name__, url_prefix="/api/config")

# Top-level keys a full backup file uses for non-setting data.  They sit alongside the
# settings rather than in a nested envelope so that an older build, which resolves every
# top-level key against the schema and skips what it does not know, still restores the
# settings instead of silently importing nothing.  Listed here so the settings importer
# can tell "not a setting" apart from "unknown key".
_DATASET_KEYS = ("pv_yield_history",)

# These are set by ConfigWebModule.start() before any request is served
_store = None
_schema = None
_module = None  # back-reference to ConfigWebModule for get_config()


def init_api(store, schema, module):
    """Wire the store, schema, and module references into the blueprint."""
    global _store, _schema, _module  # pylint: disable=global-statement
    _store = store
    _schema = schema
    _module = module


# ------------------------------------------------------------------
# Schema
# ------------------------------------------------------------------


@config_bp.route("/schema", methods=["GET"])
def get_schema():
    """Return the full config schema as JSON, including section metadata and descriptions."""
    # Get current config to resolve dynamic descriptions
    current_config = _module.get_config()
    # Flatten config to dot-notation for description resolution
    flat_config = _flatten_config(current_config)

    sections_dict = _schema.section_meta()

    # Build fields with resolved descriptions and description_map for frontend
    fields = []
    for f in _schema.all_fields():
        resolved_desc = _schema.get_resolved_description(f.key, flat_config)
        field_obj = {
            "key": f.key,
            "type": f.field_type,
            "default": f.default,
            "section": f.section,
            "level": f.level,
            "description": resolved_desc,
            "labels": f.labels,
            "help_url": f.help_url,
            "validation": f.validation,
            "depends_on": f.depends_on,
            "hot_reload": f.hot_reload,
            "display_group": f.display_group,
        }
        # Include description_map for fields that have dynamic descriptions
        if f.description_map:
            field_obj["description_map"] = f.description_map
        fields.append(field_obj)

    data = {
        "fields": fields,
        "sections": sections_dict,
        "section_order": list(sections_dict.keys()),  # Explicit order as array
    }
    # Use json.dumps with sort_keys=False to preserve SECTION_META insertion order
    response = Response(
        json.dumps(data, sort_keys=False, ensure_ascii=False),
        mimetype="application/json"
    )
    return response


# ------------------------------------------------------------------
# Read config
# ------------------------------------------------------------------


@config_bp.route("/", methods=["GET"])
def get_config():
    """Return all current config values (passwords masked)."""
    config = _module.get_config()
    masked = _mask_passwords(config)
    return jsonify(masked)


@config_bp.route("/section/<section>", methods=["GET"])
def get_section(section):
    """Return config values for a single section."""
    valid_sections = _schema.sections()
    if section not in valid_sections:
        return jsonify({"error": f"Unknown section: {section}"}), 404

    config = _module.get_config()
    if section == "system":
        # System keys are top-level
        system_fields = _schema.get_section("system")
        result = {}
        for f in system_fields:
            result[f.key] = config.get(f.key, f.default)
        return jsonify(_mask_passwords_flat(result))

    section_data = config.get(section, {})
    return jsonify(_mask_passwords_flat(section_data, section=section))


# ------------------------------------------------------------------
# Update config
# ------------------------------------------------------------------


@config_bp.route("/", methods=["PUT"])
def update_config():
    """
    Partial update — accepts a flat dict of dot-notation keys + values.

    Example body: ``{"price.feed_in_price": 0.08, "battery.min_soc_percentage": 10}``

    Returns:
    - If validation errors: status 422 with "errors"
    - If unmet dependencies: status 200 with "unmet_dependencies" + no save
    - If success: status 200 with "updated", "restart_required", "hot_reloaded"
    """
    data = flask_request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # Validate values
    errors = _validate_updates(data)
    if errors:
        return jsonify({"errors": errors}), 422

    # Check for timeseries configuration changes and run pre-flight validation if needed
    preflight_errors = _check_timeseries_preflight(data)
    if preflight_errors:
        return jsonify({"errors": preflight_errors}), 422

    # Validate price array configurations (fixed_24h_array format and counts)
    array_errors = _validate_price_arrays(data)
    if array_errors:
        return jsonify({"errors": array_errors}), 422

    changed_keys = []
    restart_required = []
    hot_reloaded = []

    for key, value in data.items():
        field_def = _resolve_schema_key(key)
        if field_def is None:
            continue

        value = _coerce_value(field_def, value)
        _store.set(key, value)
        changed_keys.append(key)

        if "restart_required" in field_def.labels:
            restart_required.append(key)
        elif field_def.hot_reload:
            hot_reloaded.append(key)

    # Rebuild merged config so get_config() reflects changes
    _module.rebuild_config()

    # Check cross-field dependencies after the new config has been merged.
    # This ensures array updates like pv_forecast.0.* are visible to dependency logic.
    unmet_deps = _check_dependencies(data)
    if unmet_deps:
        return jsonify({
            "success": False,
            "unmet_dependencies": unmet_deps,
            "message": "Cannot save: required dependencies not configured"
        }), 200

    # Persist restart-required fields for banner across reloads
    if restart_required:
        existing = _store.get("_restart_pending", []) or []
        merged = list(set(existing + restart_required))
        _store.set("_restart_pending", merged)

    return jsonify(
        {
            "success": True,
            "updated": changed_keys,
            "restart_required": restart_required,
            "hot_reloaded": hot_reloaded,
        }
    )


# ------------------------------------------------------------------
# Validate
# ------------------------------------------------------------------


@config_bp.route("/validate", methods=["POST"])
def validate_config():
    """Validate values without saving. Returns errors if any."""
    data = flask_request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    errors = _validate_updates(data)
    if errors:
        return jsonify({"valid": False, "errors": errors}), 422
    return jsonify({"valid": True, "errors": []})


# ------------------------------------------------------------------
# Restart-required status
# ------------------------------------------------------------------


@config_bp.route("/restart-required", methods=["GET"])
def get_restart_required():
    """Return list of fields that have been changed and require a restart."""
    fields = _store.get("_restart_pending", []) or []
    return jsonify({"fields": fields})


# ------------------------------------------------------------------
# Export / Import
# ------------------------------------------------------------------


@config_bp.route("/export", methods=["GET"])
def export_config():
    """Export current config as a flat JSON dict (for backup)."""
    all_settings = _store.export_dict()
    # Exclude internal keys (prefixed with _) and raw array keys that are
    # redundant with their indexed children (e.g. "pv_forecast" array is
    # already present as "pv_forecast.0.azimuth" etc.)
    filtered = {
        k: v
        for k, v in all_settings.items()
        if not k.startswith("_") and _resolve_schema_key(k) is not None
    }
    return jsonify(filtered)


@config_bp.route("/import", methods=["POST"])
def import_config():
    """Import a flat JSON dict of settings (from backup).

    Config-scoped: any non-setting payload a full backup carries (e.g.
    ``pv_yield_history``) is reported back but not restored here — that is what
    ``/api/backup/import`` is for.
    """
    data = flask_request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    result = apply_settings(data, mode="merge")
    result["ignored_datasets"] = [k for k in _DATASET_KEYS if k in data]
    return jsonify(result)


def apply_settings(data: dict, mode: str = "merge") -> dict:
    """
    Write a flat dict of settings to the store the same way a normal save does.

    ``ConfigStore.import_dict`` writes SQL directly, which skips validation, the
    hot-reload callbacks and the restart-required bookkeeping that ``PUT /api/config/``
    performs.  An import that bypasses those leaves the database updated while the
    running interfaces keep their old values, with no restart banner to say so.  Both
    import endpoints go through here instead.

    Args:
        data: Flat dot-notation key/value pairs.  Unknown and ``_``-prefixed keys are
            ignored; a whole-backup payload can be passed unfiltered.
        mode: ``"merge"`` keeps settings absent from *data*.  ``"replace"`` removes
            them, so the stored config ends up matching the file exactly.

    Returns:
        Counts and per-key detail: ``imported``, ``removed``, ``skipped``, ``invalid``,
        ``restart_required``, ``hot_reloaded``, ``warnings``.
    """
    before = _store.get_all()

    # Validate before writing anything.  A single out-of-range value in an old backup
    # must not abort the restore, so offenders are reported and skipped individually.
    valid_data = {}
    invalid = []
    skipped = 0
    for key, value in data.items():
        # Internal bookkeeping keys and file metadata are not settings, and are not
        # "unknown keys" either — counting them as skipped makes re-importing your own
        # export look like it lost something.
        if key.startswith("_") or key in _DATASET_KEYS:
            continue
        field_def = _resolve_schema_key(key)
        if field_def is None:
            skipped += 1
            continue
        err = _validate_single(field_def, value)
        if err:
            invalid.append({"key": key, "error": err})
            continue
        try:
            valid_data[key] = _coerce_value(field_def, value)
        except (TypeError, ValueError) as exc:
            invalid.append({"key": key, "error": f"Invalid type: {exc}"})

    removed = _remove_stale_keys(before, valid_data) if mode == "replace" else []

    count = _store.set_batch(valid_data) if valid_data else 0
    _module.rebuild_config()

    restart_required, hot_reloaded = _replay_changes(before, valid_data, removed)

    # Persist restart-required fields so the banner survives a page reload, matching
    # what update_config() does after a normal save.
    if restart_required:
        existing = _store.get("_restart_pending", []) or []
        _store.set("_restart_pending", list(set(existing + restart_required)))

    # Reported, never blocking: a restore is applied as a whole, and refusing it
    # because one cross-field dependency is unmet would leave the user with nothing.
    warnings = _check_dependencies(valid_data) if valid_data else []

    logger.info(
        "[ConfigWeb] Imported %d setting(s), removed %d, skipped %d, invalid %d (mode=%s)",
        count,
        len(removed),
        skipped,
        len(invalid),
        mode,
    )

    return {
        "imported": count,
        "removed": removed,
        "skipped": skipped,
        "invalid": invalid,
        "restart_required": restart_required,
        "hot_reloaded": hot_reloaded,
        "warnings": warnings,
    }


def _remove_stale_keys(before: dict, valid_data: dict) -> list[str]:
    """
    Delete stored settings that the imported file does not contain.

    Only keys ``export_config()`` would have written are eligible, which makes replace
    the exact inverse of export.  Two kinds of key are therefore left alone:

    - ``_``-prefixed internals.  Wiping ``_wizard_completed`` would pop the setup
      wizard after every restore, and ``_restart_pending`` carries the banner state.
    - Keys with no schema definition, such as the raw ``pv_forecast`` list kept as a
      migration fallback by the merger.  The indexed ``pv_forecast.0.*`` keys take
      precedence over it, so it is inert rather than stale.
    """
    stale = [
        key
        for key in before
        if not key.startswith("_")
        and key not in valid_data
        and _resolve_schema_key(key) is not None
    ]
    for key in stale:
        _store.delete(key)
    return stale


def _replay_changes(
    before: dict, valid_data: dict, removed: list[str]
) -> tuple[list[str], list[str]]:
    """
    Fire hot-reload callbacks for everything the import actually changed.

    ``set_batch()`` and ``delete()`` skip the store's own change callbacks so the write
    stays a single transaction; without this replay the restored values would never
    reach the running interfaces.  Returns the changed keys split into those needing a
    restart and those applied live, classified exactly as ``update_config()`` does.
    """
    changes = [
        (key, before.get(key), value)
        for key, value in valid_data.items()
        if key not in before or before[key] != value
    ]
    # A removed key falls back to its schema default, which is the value the interfaces
    # now need to see.
    for key in removed:
        field_def = _resolve_schema_key(key)
        changes.append((key, before.get(key), field_def.default if field_def else None))

    restart_required = []
    hot_reloaded = []
    notify = getattr(_module, "notify_config_changed", None)
    for key, old_value, new_value in changes:
        field_def = _resolve_schema_key(key)
        if field_def is not None:
            if "restart_required" in field_def.labels:
                restart_required.append(key)
            elif field_def.hot_reload:
                hot_reloaded.append(key)
        if notify is not None:
            notify(key, old_value, new_value)

    return restart_required, hot_reloaded


# ------------------------------------------------------------------
# Wizard status
# ------------------------------------------------------------------


@config_bp.route("/wizard-status", methods=["GET"])
def wizard_status():
    """Return wizard completion state."""
    completed = _store.get("_wizard_completed", False)
    migrated = _store.get("_migrated_from_yaml", False)
    return jsonify(
        {
            "pending": not completed and not migrated,
            "completed": bool(completed),
            "migrated": bool(migrated),
        }
    )


@config_bp.route("/wizard-complete", methods=["POST"])
def wizard_complete():
    """Mark the wizard as completed."""
    _store.set("_wizard_completed", True)
    return jsonify({"completed": True})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _resolve_schema_key(key: str):
    """Resolve a key to its schema definition, handling PV array indexed keys.

    PV forecast keys are stored as ``pv_forecast.0.name``, ``pv_forecast.1.azimuth``,
    etc., but the schema defines them as ``pv_forecast.name``, ``pv_forecast.azimuth``.
    """
    field_def = _schema.get(key)
    if field_def is not None:
        return field_def
    # Try stripping array index: pv_forecast.0.name → pv_forecast.name
    m = re.match(r"^(\w+)\.\d+\.(.+)$", key)
    if m:
        template_key = f"{m.group(1)}.{m.group(2)}"
        return _schema.get(template_key)
    return None


def _check_dependencies(data: dict) -> list[dict]:
    """
    Check cross-field dependencies. Returns list of unmet dependency objects.

    Examples:
    - If pv_forecast_source.source="evcc", then evcc.url must be populated
    - If mqtt.enabled=True, then mqtt.broker must be populated

    Each dependency object has: {"field": "...", "reason": "...", "requires": "..."
    """
    dependencies = []

    # Get current config for fields not in the update
    current_config = _module.get_config()

    # Helper: get effective value (from update data or current config)
    def get_value(key):
        if key in data:
            return data[key]
        # Navigate nested key in current config
        parts = key.split(".")
        val = current_config
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return None
        return val

    # PV Source: if "evcc" selected, EVCC URL must be configured
    pv_source = get_value("pv_forecast_source.source")
    if pv_source == "evcc":
        evcc_url = get_value("evcc.url")
        if not evcc_url or evcc_url.strip() == "":
            dependencies.append({
                "field": "pv_forecast_source.source",
                "reason": "EVCC selected as PV source but EVCC URL is not configured",
                "requires": "evcc.url",
                "blocking": True,
            })

    # Inverter: if "evcc" selected, EVCC URL must be configured
    inverter_type = get_value("inverter.type")
    if inverter_type == "evcc":
        evcc_url = get_value("evcc.url")
        if not evcc_url or evcc_url.strip() == "" or evcc_url == "http://yourEVCCserver:7070":
            dependencies.append({
                "field": "inverter.type",
                "reason": "EVCC selected as inverter controller but EVCC URL is not configured",
                "requires": "evcc.url",
                "blocking": True,
            })

    # Price Source: if "evcc" selected, EVCC URL must be configured
    price_source = get_value("price.source")
    if price_source == "evcc":
        evcc_url = get_value("evcc.url")
        if not evcc_url or evcc_url.strip() == "" or evcc_url == "http://yourEVCCserver:7070":
            dependencies.append({
                "field": "price.source",
                "reason": "EVCC selected as price source but EVCC URL is not configured",
                "requires": "evcc.url",
                "blocking": True,
            })

    # PV Source: validation for Solcast and Victron
    pv_source = get_value("pv_forecast_source.source")
    if pv_source in ["solcast", "victron"]:
        resource_id = get_value("pv_forecast_source.resource_id")
        if not resource_id or (isinstance(resource_id, str) and resource_id.strip() == ""):
            dependencies.append({
                "field": "pv_forecast_source.resource_id",
                "reason": (
                    f"{pv_source.capitalize()} selected as PV source but "
                    "Resource ID/Installation ID is not configured"
                ),
                "requires": "pv_forecast_source.resource_id",
                "blocking": True,
            })

    # PV Source: validation for location-based sources (must have at least 1 installation)
    location_based_sources = ["akkudoktor", "openmeteo", "openmeteo_local", "forecast_solar"]
    if pv_source in location_based_sources:
        # Get PV installations from current config
        pv_forecast_data = (
            data.get("pv_forecast")
            if "pv_forecast" in data
            else current_config.get("pv_forecast", [])
        )
        if not pv_forecast_data or len(pv_forecast_data) == 0:
            dependencies.append({
                "field": "pv_forecast",
                "reason": "Location-based PV source selected but no PV installations configured",
                "requires": "pv_forecast.0.lat",  # Indicate at least one entry needed
                "blocking": True,
            })

    return dependencies


def _check_timeseries_preflight(data: dict) -> list[dict]:
    """
    Check if we're modifying a timeseries config and validate the sensor exists.
    Returns list of error dicts if validation fails.
    """
    errors = []
    current_config = _module.get_config()

    # Helper: get effective value (from update data or current config)
    def get_value(key):
        if key in data:
            return data[key]
        # For data_source keys, check store directly
        # (data_source is excluded from merged config)
        if key.startswith("data_source."):
            store_val = _store.get(key)
            if store_val is not None:
                return store_val
        parts = key.split(".")
        val = current_config
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return None
        return val

    # Check if we're modifying price timeseries config
    price_source = get_value("price.source")
    if price_source == "timeseries":
        use_ha_central = get_value("price.use_ha_central_data_source")
        if use_ha_central:
            # Central HA mode: sensor name + data_source config
            ha_sensor_name = get_value("price.ha_sensor_name")
            data_source_url = get_value("data_source.url")
            data_source_token = get_value("data_source.access_token")

            if (
                ha_sensor_name and data_source_url and data_source_token
            ):
                # Try to fetch the sensor from Home Assistant
                ha_url = (
                    f"{data_source_url.rstrip('/')}/api/states/{ha_sensor_name}"
                )
                try:
                    import requests
                    response = requests.get(
                        ha_url,
                        headers={"Authorization": f"Bearer {data_source_token}"},
                        timeout=5
                    )
                    if response.status_code == 404:
                        errors.append({
                            "key": "price.ha_sensor_name",
                            "error": (
                                f"Sensor '{ha_sensor_name}' not found in "
                                "Home Assistant"
                            )
                        })
                    elif response.status_code != 200:
                        errors.append({
                            "key": "price.ha_sensor_name",
                            "error": (
                                f"Home Assistant error {response.status_code}: "
                                f"{response.reason}"
                            )
                        })
                except requests.exceptions.HTTPError as e:
                    if hasattr(e, 'response') and e.response is not None:
                        if e.response.status_code == 404:
                            errors.append({
                                "key": "price.ha_sensor_name",
                                "error": (
                                    f"Sensor '{ha_sensor_name}' not found in "
                                    "Home Assistant"
                                )
                            })
                        else:
                            errors.append({
                                "key": "price.ha_sensor_name",
                                "error": (
                                    f"Home Assistant error "
                                    f"{e.response.status_code}: "
                                    f"{e.response.reason}"
                                )
                            })
                except Exception as e:
                    # Log detailed error server-side only (not exposed to client)
                    logger.error("Home Assistant connection error: %s", str(e), exc_info=True)
                    errors.append({
                        "key": "price.ha_sensor_name",
                        "error": "Failed to connect to Home Assistant. Check configuration and logs."
                    })

    return errors


def _validate_price_arrays(data: dict) -> list[dict]:
    """
    Validate price array configurations at save time.

    Checks:
    - price.fixed_24h_array: must contain exactly 24 comma-separated numeric values
      (only validated if price.source="fixed_24h")
    - price.source="fixed_24h" requires fixed_24h_array to be populated

    Args:
        data: Configuration update dict

    Returns:
        list: Error dicts if validation fails, empty list otherwise
    """
    errors = []
    current_config = _module.get_config()

    # Determine effective price.source
    price_source = (
        data.get("price.source")
        if "price.source" in data
        else current_config.get("price", {}).get("source")
    )

    # Only validate fixed_24h_array if the source is or will be "fixed_24h"
    if price_source == "fixed_24h":
        # Check price.fixed_24h_array if being updated or already set
        fixed_24h_array = (
            data.get("price.fixed_24h_array")
            if "price.fixed_24h_array" in data
            else current_config.get("price", {}).get("fixed_24h_array")
        )

        # If not provided or empty, that's an error
        if not fixed_24h_array or (isinstance(fixed_24h_array, str) and
                                   not fixed_24h_array.strip()):
            errors.append({
                "key": "price.fixed_24h_array",
                "error": (
                    "fixed_24h_array is required when price source is 'fixed_24h'"
                )
            })
            return errors  # Return early to avoid redundant errors

        # Validate format only if we have data
        if isinstance(fixed_24h_array, str) and fixed_24h_array.strip():
            try:
                # Split by comma and filter out empty strings
                values = [v.strip() for v in fixed_24h_array.split(",") if v.strip()]
                # Try to convert each value to float to catch non-numeric entries
                for v in values:
                    float(v)
                # Check count
                if len(values) != 24:
                    errors.append({
                        "key": "price.fixed_24h_array",
                        "error": (
                            f"Must contain exactly 24 comma-separated values, "
                            f"got {len(values)}"
                        )
                    })
            except ValueError as e:
                errors.append({
                    "key": "price.fixed_24h_array",
                    "error": f"Array must contain numeric values: {e}"
                })

    return errors


def _validate_updates(data: dict) -> list[dict]:
    """Validate a dict of {key: value} against the schema. Returns list of error dicts."""
    errors = []
    for key, value in data.items():
        field_def = _resolve_schema_key(key)
        if field_def is None:
            errors.append({"key": key, "error": "Unknown configuration key"})
            continue

        err = _validate_single(field_def, value)
        if err:
            errors.append({"key": key, "error": err})

    return errors


def _validate_single(field_def, value) -> str:
    """Validate a single value against its field definition. Returns error string or ''."""
    # --- Global checks applied before schema-specific validation ---

    # Max string length (defence against absurdly long values)
    if isinstance(value, str) and len(value) > 2000:
        return f"Value too long ({len(value)} chars, max 2000)"

    # HTML/script injection (str and sensor fields only — passwords may contain symbols)
    if field_def.field_type in ("str", "sensor") and isinstance(value, str):
        if re.search(r"<[a-zA-Z/!]", value):
            return "HTML tags are not allowed in this field"

    # Password/token fields must be ASCII-safe for use in HTTP headers
    if field_def.field_type == "password" and isinstance(value, str) and value:
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            return "Token/password must contain only ASCII characters (HTTP header restriction)"

    v = field_def.validation
    if not v:
        return ""

    # Type coercion for comparison
    try:
        value = _coerce_value(field_def, value)
    except (TypeError, ValueError) as e:
        return f"Invalid type: {e}"

    # Choices
    if "choices" in v:
        if value not in v["choices"]:
            return f"Must be one of: {v['choices']}"

    # Min/Max
    if "min" in v and isinstance(value, (int, float)):
        if value < v["min"]:
            return f"Must be >= {v['min']}"
    if "max" in v and isinstance(value, (int, float)):
        if value > v["max"]:
            return f"Must be <= {v['max']}"

    # Pattern
    if "pattern" in v and isinstance(value, str):
        if not re.match(v["pattern"], value):
            return f"Must match pattern: {v['pattern']}"

    # Required (empty check for fields marked required)
    if v.get("required") and (value is None or value == ""):
        return "This field is required"

    return ""


def _coerce_value(field_def, value):
    """Coerce a value to the expected type based on field definition."""
    if value is None:
        return value

    ft = field_def.field_type
    if ft == "int":
        return int(value)
    if ft == "float":
        return float(value)
    if ft == "bool":
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "enabled")
        return bool(value)
    if ft == "select" and isinstance(value, str):
        # The browser always sends strings; coerce to match the type of the
        # choices list so that e.g. "3600" matches int choice 3600.
        choices = (field_def.validation or {}).get("choices", [])
        if choices and isinstance(choices[0], int):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
        if choices and isinstance(choices[0], float):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    # str, password, sensor — keep as-is
    return value


def _mask_passwords(config: dict, prefix: str = "") -> dict:
    """Recursively mask password fields in a nested config dict."""
    result = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result[key] = _mask_passwords(value, full_key)
        elif isinstance(value, list):
            result[key] = value
        else:
            field_def = _schema.get(full_key)
            if field_def and field_def.field_type == "password":
                result[key] = "********" if value else ""
            else:
                result[key] = value
    return result


def _mask_passwords_flat(data: dict, section: str = "") -> dict:
    """Mask password fields in a flat dict.

    Args:
        data: Flat dict of config values.
        section: Optional section prefix for schema lookup when keys
            lack dot-notation (e.g. section='inverter' + key='password'
            looks up 'inverter.password' in schema).
    """
    result = {}
    for key, value in data.items():
        lookup_key = key if "." in key else f"{section}.{key}" if section else key
        field_def = _schema.get(lookup_key)
        if field_def and field_def.field_type == "password":
            result[key] = "********" if value else ""
        else:
            result[key] = value
    return result
