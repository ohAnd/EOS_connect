"""
REST API Blueprint — Flask Blueprint with all configuration endpoints.

Prefix: ``/api/config``

All routes are registered via a Flask Blueprint so that the main app
only needs one line: ``app.register_blueprint(config_bp)``.
"""

import logging
import re
from flask import Blueprint, jsonify, request as flask_request

logger = logging.getLogger("__main__")

config_bp = Blueprint("config", __name__, url_prefix="/api/config")

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
    """Return the full config schema as JSON, including section metadata."""
    return jsonify({
        "fields": _schema.to_json(),
        "sections": _schema.section_meta(),
    })


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
    """
    data = flask_request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    errors = _validate_updates(data)
    if errors:
        return jsonify({"errors": errors}), 422

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

    # Persist restart-required fields for banner across reloads
    if restart_required:
        existing = _store.get("_restart_pending", []) or []
        merged = list(set(existing + restart_required))
        _store.set("_restart_pending", merged)

    return jsonify({
        "updated": changed_keys,
        "restart_required": restart_required,
        "hot_reloaded": hot_reloaded,
    })


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
        k: v for k, v in all_settings.items()
        if not k.startswith("_") and _resolve_schema_key(k) is not None
    }
    return jsonify(filtered)


@config_bp.route("/import", methods=["POST"])
def import_config():
    """Import a flat JSON dict of settings (from backup)."""
    data = flask_request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # Filter to known keys only, skip internal keys
    valid_data = {}
    skipped = 0
    for key, value in data.items():
        if key.startswith("_"):
            skipped += 1
            continue
        field_def = _resolve_schema_key(key)
        if field_def is None:
            skipped += 1
            continue
        valid_data[key] = _coerce_value(field_def, value)

    count = _store.import_dict(valid_data) if valid_data else 0
    _module.rebuild_config()
    return jsonify({"imported": count, "skipped": skipped})


# ------------------------------------------------------------------
# Wizard status
# ------------------------------------------------------------------

@config_bp.route("/wizard-status", methods=["GET"])
def wizard_status():
    """Return wizard completion state."""
    completed = _store.get("_wizard_completed", False)
    migrated = _store.get("_migrated_from_yaml", False)
    return jsonify({
        "pending": not completed and not migrated,
        "completed": bool(completed),
        "migrated": bool(migrated),
    })


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
    m = re.match(r'^(\w+)\.\d+\.(.+)$', key)
    if m:
        template_key = f"{m.group(1)}.{m.group(2)}"
        return _schema.get(template_key)
    return None


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
    # str, select, password, sensor — keep as-is
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
