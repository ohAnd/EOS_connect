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
    """Return the full config schema as JSON."""
    return jsonify(_schema.to_json())


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
    return jsonify(_mask_passwords_flat(section_data))


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

    for key, value in data.items():
        field_def = _schema.get(key)
        if field_def is None:
            continue

        value = _coerce_value(field_def, value)
        _store.set(key, value)
        changed_keys.append(key)

        if "restart_required" in field_def.labels:
            restart_required.append(key)

    # Rebuild merged config so get_config() reflects changes
    _module.rebuild_config()

    return jsonify({
        "updated": changed_keys,
        "restart_required": restart_required,
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
    # Track restart-required changes in session — for now return empty
    return jsonify({"fields": []})


# ------------------------------------------------------------------
# Export / Import
# ------------------------------------------------------------------

@config_bp.route("/export", methods=["GET"])
def export_config():
    """Export current config as a flat JSON dict (for backup)."""
    all_settings = _store.export_dict()
    return jsonify(all_settings)


@config_bp.route("/import", methods=["POST"])
def import_config():
    """Import a flat JSON dict of settings (from backup)."""
    data = flask_request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    count = _store.import_dict(data)
    _module.rebuild_config()
    return jsonify({"imported": count})


# ------------------------------------------------------------------
# Wizard status
# ------------------------------------------------------------------

@config_bp.route("/wizard-status", methods=["GET"])
def wizard_status():
    """Return wizard completion state."""
    completed = _store.get("_wizard_completed", False)
    return jsonify({
        "pending": not completed,
        "completed": completed,
    })


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _validate_updates(data: dict) -> list[dict]:
    """Validate a dict of {key: value} against the schema. Returns list of error dicts."""
    errors = []
    for key, value in data.items():
        field_def = _schema.get(key)
        if field_def is None:
            errors.append({"key": key, "error": "Unknown configuration key"})
            continue

        err = _validate_single(field_def, value)
        if err:
            errors.append({"key": key, "error": err})

    return errors


def _validate_single(field_def, value) -> str:
    """Validate a single value against its field definition. Returns error string or ''."""
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


def _mask_passwords(config: dict) -> dict:
    """Recursively mask password fields in a nested config dict."""
    result = {}
    for key, value in config.items():
        if isinstance(value, dict):
            result[key] = _mask_passwords(value)
        elif isinstance(value, list):
            result[key] = value
        else:
            # Check if any schema field matching this key is a password
            result[key] = value
    return result


def _mask_passwords_flat(data: dict) -> dict:
    """Mask password fields in a flat dict."""
    result = {}
    for key, value in data.items():
        field_def = _schema.get(key) if "." in key else None
        if field_def and field_def.field_type == "password":
            result[key] = "********" if value else ""
        else:
            result[key] = value
    return result
