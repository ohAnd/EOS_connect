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

from .hot_reload import _coerce_bool
from .migration import _flatten_config
from .schema import (
    DATA_SOURCE_REQUIRED_SENSORS,
    LEGACY_SENSOR_PLACEHOLDERS,
    LOCATION_BASED_PV_SOURCES,
    REMOTE_DATA_SOURCE_TYPES,
)
from .entity_probe import probe_entity
from .timeseries_probe import probe

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
        # Which PV sources need a pv_forecast installation. The frontend decides on
        # this in two places and used to keep its own copies, which drifted — one of
        # them listed "default" and the other did not, so the wizard rendered the
        # installation fields for a source it then refused to save them for.
        "location_based_pv_sources": list(LOCATION_BASED_PV_SOURCES),
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
    - If unmet *blocking* dependencies: status 200 with ``success: false`` and
      "unmet_dependencies"; nothing is written
    - If success: status 200 with "updated", "restart_required", "hot_reloaded", and
      "warnings" — advisory dependencies describing a configuration that saved fine
      but will run degraded
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

    # Cross-field dependencies are checked before anything is written. They used to be
    # checked afterwards, so a request refused with success:false had already stored
    # every value in it — the caller was told the save did not happen while the database
    # said otherwise. _check_dependencies reads the incoming values in preference to the
    # stored ones, so it sees the same picture either way.
    # Only the blocking ones refuse the write. Advisory entries describe a
    # configuration that runs in a degraded mode worth mentioning, and ride along with
    # the successful response instead.
    unmet_deps = _check_dependencies(data)
    blocking_deps = [d for d in unmet_deps if d.get("blocking", True)]
    advisories = [d for d in unmet_deps if not d.get("blocking", True)]
    if blocking_deps:
        return jsonify({
            "success": False,
            "unmet_dependencies": blocking_deps,
            "message": "Cannot save: required dependencies not configured"
        }), 200

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

    return jsonify(
        {
            "success": True,
            "updated": changed_keys,
            "restart_required": restart_required,
            "hot_reloaded": hot_reloaded,
            "warnings": advisories,
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


def export_settings_dict() -> dict:
    """
    Every stored setting worth writing to a backup file, flat and dot-notated.

    Excludes internal keys (prefixed with ``_``) and raw array keys that are redundant
    with their indexed children (the ``pv_forecast`` list is already present as
    ``pv_forecast.0.azimuth`` and friends).  Shared with ``/api/backup`` so both files
    carry exactly the same settings, and so replace mode can be the precise inverse of
    what this writes.
    """
    return {
        k: v
        for k, v in _store.export_dict().items()
        if not k.startswith("_") and _resolve_schema_key(k) is not None
    }


@config_bp.route("/export", methods=["GET"])
def export_config():
    """Export current config as a flat JSON dict (for backup)."""
    return jsonify(export_settings_dict())


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


def _classify_settings(data: dict) -> tuple[dict, list[dict], int]:
    """
    Split an incoming payload into writable values, rejects and unknown keys.

    A single out-of-range value in an old backup must not abort the restore, so
    offenders are reported and skipped individually rather than failing the request.

    Returns ``(valid_data, invalid, skipped)``.
    """
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
            logger.warning("[ConfigWeb] Rejected imported value for '%s': %s", key, exc)
            invalid.append({"key": key, "error": _type_error_message(field_def)})
    return valid_data, invalid, skipped


def plan_settings(data: dict, mode: str = "merge") -> dict:
    """
    Report what ``apply_settings`` would do, without touching the store.

    Restoring a whole install is not something to discover the consequences of
    afterwards, so the restore dialog previews this first — above all the list of
    settings replace mode would remove.  Shares its classification with the real thing
    so the preview and the write cannot disagree.
    """
    before = _store.get_all()
    valid_data, invalid, skipped = _classify_settings(data)
    removed = (
        _stale_keys(before, valid_data) if mode == "replace" else []
    )
    changed = [k for k, v in valid_data.items() if k not in before or before[k] != v]

    restart_required = []
    hot_reloaded = []
    for key in changed + removed:
        field_def = _resolve_schema_key(key)
        if field_def is None:
            continue
        if "restart_required" in field_def.labels:
            restart_required.append(key)
        elif field_def.hot_reload:
            hot_reloaded.append(key)

    return {
        "imported": len(valid_data),
        "changed": len(changed),
        "removed": removed,
        "skipped": skipped,
        "invalid": invalid,
        "restart_required": restart_required,
        "hot_reloaded": hot_reloaded,
        "warnings": _check_dependencies(valid_data) if valid_data else [],
    }


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
    valid_data, invalid, skipped = _classify_settings(data)

    removed = _remove_stale_keys(before, valid_data) if mode == "replace" else []

    count = _store.set_batch(valid_data) if valid_data else 0
    _module.rebuild_config()

    restart_required, hot_reloaded, changed = _replay_changes(before, valid_data, removed)

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
        "changed": changed,
        "removed": removed,
        "skipped": skipped,
        "invalid": invalid,
        "restart_required": restart_required,
        "hot_reloaded": hot_reloaded,
        "warnings": warnings,
    }


def _stale_keys(before: dict, valid_data: dict) -> list[str]:
    """
    Stored settings that the imported file does not contain.

    Only keys ``export_config()`` would have written are eligible, which makes replace
    the exact inverse of export.  Two kinds of key are therefore left alone:

    - ``_``-prefixed internals.  Wiping ``_wizard_completed`` would pop the setup
      wizard after every restore, and ``_restart_pending`` carries the banner state.
    - Keys with no schema definition, such as the raw ``pv_forecast`` list kept as a
      migration fallback by the merger.  The indexed ``pv_forecast.0.*`` keys take
      precedence over it, so it is inert rather than stale.
    """
    return [
        key
        for key in before
        if not key.startswith("_")
        and key not in valid_data
        and _resolve_schema_key(key) is not None
    ]


def _remove_stale_keys(before: dict, valid_data: dict) -> list[str]:
    """Delete the stale keys and return them."""
    stale = _stale_keys(before, valid_data)
    for key in stale:
        _store.delete(key)
    return stale


def _replay_changes(
    before: dict, valid_data: dict, removed: list[str]
) -> tuple[list[str], list[str], int]:
    """
    Fire hot-reload callbacks for everything the import actually changed.

    ``set_batch()`` and ``delete()`` skip the store's own change callbacks so the write
    stays a single transaction; without this replay the restored values would never
    reach the running interfaces.  Returns the keys needing a restart, those applied
    live — classified exactly as ``update_config()`` does — and how many values changed
    in total.
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

    return restart_required, hot_reloaded, len(changes)


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


# The URL the schema ships as a hint. It is not a configured EVCC instance, so every
# check that requires one has to reject it.
_EVCC_PLACEHOLDER_URL = "http://yourEVCCserver:7070"


def _evcc_url_unconfigured(url) -> bool:
    """Whether *url* is empty, blank, or still the schema's placeholder."""
    if not url or not isinstance(url, str):
        return True
    stripped = url.strip()
    return not stripped or stripped == _EVCC_PLACEHOLDER_URL


def _sensor_unconfigured(value) -> bool:
    """Whether *value* is empty, blank, or still one of the schema's old placeholders."""
    if not value or not isinstance(value, str):
        return True
    stripped = value.strip()
    return not stripped or stripped in LEGACY_SENSOR_PLACEHOLDERS


def _pending_pv_installation_count(data: dict, current_config: dict) -> int:
    """
    How many PV installations there will be once *data* is applied.

    Installations are stored as indexed keys (``pv_forecast.0.lat``) and only become a
    list once the merger rebuilds the config. This counts them without that rebuild, so
    the dependency check can run before anything is written rather than after.

    Indices from the request and from the store are unioned: a partial update that only
    touches installation 0 must not read as "there is one installation" when the store
    already holds two.
    """
    def _indices(keys):
        found = set()
        for key in keys:
            parts = key.split(".")
            if len(parts) >= 3 and parts[0] == "pv_forecast" and parts[1].isdigit():
                found.add(parts[1])
        return found

    indexed = _indices(data) | _indices(_store.get_all())
    if indexed:
        return len(indexed)

    # A whole list can also arrive under the bare key, and the merged config always
    # exposes one.
    listed = data["pv_forecast"] if "pv_forecast" in data else current_config.get("pv_forecast")
    return len(listed or [])


def _check_dependencies(data: dict) -> list[dict]:
    """
    Cross-field dependencies that *this request* would leave unsatisfied.

    Each check is gated on the request actually touching one of the fields involved.
    Judging the whole resulting config instead would refuse an unrelated save whenever
    the stored configuration was already incomplete — and a fresh install always is,
    because the default PV source is location-based and has no installations yet. That
    made every single save through this endpoint impossible until the PV step was done,
    in whatever order the user happened to work.

    Each entry is ``{"field", "reason", "requires", "blocking"}``. ``blocking`` false
    means "save it, but tell the user" — a configuration that runs, in a degraded mode
    they should know about, rather than one that cannot be written.
    """
    dependencies = []
    current_config = _module.get_config()

    def get_value(key):
        """The value this request would end up with: its own, else what is stored."""
        if key in data:
            return data[key]
        parts = key.split(".")
        val = current_config
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return None
        return val

    def touches(*keys):
        """Whether the request sets any of *keys* — including indexed pv_forecast."""
        for key in keys:
            if key in data:
                return True
            if key == "pv_forecast" and any(
                k.startswith("pv_forecast.") for k in data
            ):
                return True
        return False

    # EVCC is the data source for three different things, and each needs a real URL.
    # The placeholder counts as unconfigured for all of them — it used to pass for the
    # PV source only, so that one save went through and then failed at runtime.
    for field, label in (
        ("pv_forecast_source.source", "PV source"),
        ("inverter.type", "inverter controller"),
        ("price.source", "price source"),
    ):
        if get_value(field) == "evcc" and touches(field, "evcc.url"):
            if _evcc_url_unconfigured(get_value("evcc.url")):
                dependencies.append({
                    "field": field,
                    "reason": f"EVCC selected as {label} but EVCC URL is not configured",
                    "requires": "evcc.url",
                    "blocking": True,
                })

    pv_source = get_value("pv_forecast_source.source")

    # Solcast and Victron identify the installation by id rather than coordinates.
    if pv_source in ("solcast", "victron") and touches(
        "pv_forecast_source.source", "pv_forecast_source.resource_id"
    ):
        resource_id = get_value("pv_forecast_source.resource_id")
        if not resource_id or (isinstance(resource_id, str) and not resource_id.strip()):
            dependencies.append({
                "field": "pv_forecast_source.resource_id",
                "reason": (
                    f"{pv_source.capitalize()} selected as PV source but "
                    "Resource ID/Installation ID is not configured"
                ),
                "requires": "pv_forecast_source.resource_id",
                "blocking": True,
            })

    # Location-based sources forecast from coordinates, so they need an installation.
    if pv_source in LOCATION_BASED_PV_SOURCES and touches(
        "pv_forecast_source.source", "pv_forecast"
    ):
        if _pending_pv_installation_count(data, current_config) == 0:
            dependencies.append({
                "field": "pv_forecast",
                "reason": "Location-based PV source selected but no PV installations configured",
                "requires": "pv_forecast.0.lat",  # Indicate at least one entry needed
                "blocking": True,
            })

    # A remote data source with nothing to read from it. Deliberately advisory: an
    # empty sensor is a *supported* state — the interfaces fall back to the built-in
    # profile — so refusing the write would be wrong on the merits. It would also
    # deadlock the user, because these sensor fields only become visible once
    # data_source.type is set to a remote source, which is the very save being judged.
    if get_value("data_source.type") in REMOTE_DATA_SOURCE_TYPES and touches(
        "data_source.type", "data_source.url", "data_source.access_token"
    ):
        for key, label in DATA_SOURCE_REQUIRED_SENSORS.items():
            # A key the request answers is an answer, whatever it says. The wizard
            # posts both sensors alongside data_source.type, and warning about one it
            # just set would be noise the user cannot act on.
            if key in data:
                continue
            if _sensor_unconfigured(get_value(key)):
                dependencies.append({
                    "field": key,
                    "reason": (
                        f"{get_value('data_source.type')} selected as data source, but "
                        f"the {label.lower()} sensor is not set — that data stays on "
                        f"the built-in fallback until you set it under Settings > {label}"
                    ),
                    "requires": key,
                    "blocking": False,
                })

    return dependencies


# Config keys that make a timeseries probe worthwhile. Saving anything else must not
# trigger a network round-trip, so an unrelated setting change stays fast and cannot be
# blocked by a momentarily unreachable endpoint.
_TIMESERIES_PROBE_TRIGGERS = {
    "price": {
        "price.source",
        "price.data_url",
        "price.data_path",
        "price.data_token",
        "price.value_unit",
        "price.use_ha_central_data_source",
        "price.ha_sensor_name",
    },
    "pv": {
        "pv_forecast_source.source",
        "pv_forecast_source.data_url",
        "pv_forecast_source.data_path",
        "pv_forecast_source.data_token",
        "pv_forecast_source.value_unit",
        "pv_forecast_source.use_ha_central_data_source",
        "pv_forecast_source.ha_sensor_name",
    },
}

# The shared data source only feeds a domain that opted into central mode, so it may
# only trigger that domain's probe. Listing it unconditionally would let an unreachable
# PV endpoint block an unrelated edit of the Home Assistant URL.
_CENTRAL_DATA_SOURCE_TRIGGERS = {"data_source.url", "data_source.access_token"}

_TIMESERIES_DOMAIN_SECTIONS = {
    "price": ("price", "sensor.grid_prices", "EUR/kWh"),
    "pv": ("pv_forecast_source", "sensor.pv_forecast", "W"),
}


def _effective_value_getter(data: dict):
    """
    Build a resolver for "value after this update would be applied".

    Resolution order is update body → store → merged config. The store has to come
    before the merged config: ``merger._apply_central_ha_data_source`` overwrites
    ``<section>.data_url`` / ``data_token`` with values derived from the central data
    source, so reading the merged config would hand back a derived HA URL even for a
    request that is switching central mode off. The store still holds what the user
    actually entered. The merged config remains the last resort so schema defaults are
    picked up for keys never written.
    """
    current_config = _module.get_config()

    def get_value(key):
        if key in data:
            return data[key]
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

    return get_value


def _raw_value_getter(data: dict):
    """
    Resolve a key without consulting the merged config.

    ``merger._apply_central_ha_data_source`` overwrites ``<section>.data_url`` and
    ``data_token`` with values derived from the shared data source. For a request that
    turns central mode off, the merged config would therefore hand back the derived HA
    URL rather than the user's own — and the probe would happily green-light a config
    that 401s at runtime. Falling back to the schema default instead keeps this
    resolution honest for exactly those two keys.
    """
    def get_raw(key):
        if key in data:
            return data[key]
        store_val = _store.get(key)
        if store_val is not None:
            return store_val
        field_def = _schema.get(key)
        return field_def.default if field_def else None

    return get_raw


def _resolve_timeseries_config(domain: str, data: dict, get_value) -> dict:
    """
    Resolve the connection details a timeseries probe needs for one domain.

    Mirrors merger._apply_central_ha_data_source: in central mode the URL is built
    from the shared data_source plus the sensor entity, so the probe tests exactly
    what the interface will later fetch.
    """
    section, default_sensor, default_unit = _TIMESERIES_DOMAIN_SECTIONS[domain]
    get_raw = _raw_value_getter(data)

    def merger_style(key, default):
        """Default only on absence, as the merger's dict.get(key, default) does."""
        value = get_value(key)
        return default if value is None else value

    # Coerced, not truthiness-tested: pre-flight runs before _coerce_value, so a REST
    # client sending the string "false" would otherwise take the central-HA branch.
    if _coerce_bool(get_value(f"{section}.use_ha_central_data_source")):
        ds_url = get_value("data_source.url") or ""
        sensor = merger_style(f"{section}.ha_sensor_name", default_sensor)
        # Built exactly as merger._apply_central_ha_data_source builds it — including
        # the absence of rstrip and the empty-string passthrough — so the probe can
        # never pass on a URL the running interface would fetch differently.
        data_url = f"{ds_url}/api/states/{sensor}" if ds_url else ""
        data_token = get_value("data_source.access_token") or ""
        sensor_field = f"{section}.ha_sensor_name"
        resource_label = sensor or f"{section}.ha_sensor_name"
    else:
        data_url = get_raw(f"{section}.data_url") or ""
        data_token = get_raw(f"{section}.data_token") or ""
        sensor_field = f"{section}.data_url"
        resource_label = data_url

    return {
        "data_url": data_url,
        "data_path": get_value(f"{section}.data_path") or "attributes.data",
        "data_token": data_token,
        "value_unit": get_value(f"{section}.value_unit") or default_unit,
        "sensor_field": sensor_field,
        "resource_label": resource_label,
    }


def _effective_time_frame_base(get_value) -> int:
    """
    The slot length the interfaces will actually run at.

    Mirrors the coercion in eos_connect.py: 15-minute slots are only honoured for the
    EVopt backends and otherwise fall back to hourly. Using the stored value verbatim
    would make the probe reject an hourly source for a system that in fact runs
    hourly.
    """
    try:
        time_frame_base = int(get_value("eos.time_frame") or 3600)
    except (TypeError, ValueError):
        return 3600

    if time_frame_base not in (900, 3600):
        return 3600
    if time_frame_base == 900 and get_value("eos.source") not in (
        "evopt",
        "local_evopt",
    ):
        return 3600
    return time_frame_base


def _installed_pv_power_w(config: dict) -> float:
    """Total configured array power, or 0 when no PV installations are defined."""
    total = 0.0
    for entry in config.get("pv_forecast") or []:
        if isinstance(entry, dict):
            try:
                total += float(entry.get("power", 0) or 0)
            except (TypeError, ValueError):
                continue
    return total


def _probe_timeseries_domain(domain: str, data: dict, get_value) -> dict:
    """Run the probe for one domain using the effective (post-update) config."""
    resolved = _resolve_timeseries_config(domain, data, get_value)
    config = _module.get_config() or {}
    time_zone = config.get("time_zone", "UTC")
    time_frame_base = _effective_time_frame_base(get_value)
    result = probe(
        domain,
        resolved["data_url"],
        resolved["data_path"],
        resolved["data_token"],
        resolved["value_unit"],
        time_zone,
        resource_label=resolved["resource_label"],
        time_frame_base=time_frame_base,
        installed_power_w=_installed_pv_power_w(config) if domain == "pv" else 0,
    )
    # In central-HA mode the actionable field is the sensor name, not the derived URL.
    if not result.get("ok") and result.get("field", "").endswith(".data_url"):
        result["field"] = resolved["sensor_field"]
    return result


def _check_timeseries_preflight(data: dict) -> list[dict]:
    """
    Validate timeseries sources before saving.

    Covers price and PV, and both connection modes. Beyond reachability this also
    resolves the configured data_path and parses the payload, so a format or unit
    mistake is reported against the field that caused it instead of surfacing hours
    later as an implausible schedule. Warnings (e.g. an odd price level) never block
    the save — only hard failures do.
    """
    errors = []
    get_value = _effective_value_getter(data)

    for domain, (section, _sensor, _unit) in _TIMESERIES_DOMAIN_SECTIONS.items():
        if get_value(f"{section}.source") != "timeseries":
            continue

        own_keys_touched = bool(_TIMESERIES_PROBE_TRIGGERS[domain] & data.keys())
        central_touched = bool(
            _CENTRAL_DATA_SOURCE_TRIGGERS & data.keys()
        ) and _coerce_bool(get_value(f"{section}.use_ha_central_data_source"))
        if not (own_keys_touched or central_touched):
            continue

        result = _probe_timeseries_domain(domain, data, get_value)
        if result.get("ok"):
            continue

        if not own_keys_touched:
            # Only the shared data source changed. The user is not editing this
            # domain, so a failure here is collateral — a deleted PV sensor must not
            # make it impossible to rotate the Home Assistant token.
            logger.info(
                "[ConfigAPI] Timeseries pre-flight for the %s source failed while "
                "the shared data source changed: %s",
                domain,
                result.get("error"),
            )
            continue

        if result.get("transport"):
            # The endpoint did not answer at all. That is not necessarily a bad
            # configuration — it is also what a source being briefly down looks like,
            # and what switching `source` to timeseries before filling in the URL
            # looks like. Blocking the whole PUT on it would hold every other key in
            # the request hostage to a 10s timeout, so record it and let the save
            # proceed; the running interface reports it on the next cycle.
            logger.info(
                "[ConfigAPI] Timeseries pre-flight could not reach the %s source: %s",
                domain,
                result.get("error"),
            )
            continue

        errors.append(
            {
                "key": result.get("field", f"{section}.data_url"),
                "error": result.get("error", "Timeseries check failed."),
            }
        )

    return errors


@config_bp.route("/test-timeseries", methods=["POST"])
def test_timeseries():
    """
    Probe a timeseries data source and report what it yields.

    Body: optional flat dot-notation keys (as for PUT /), so the UI can test values
    the user has typed but not yet saved. Anything absent falls back to the stored
    config. ``domain`` selects "price" (default) or "pv".

    Returns 200 with the probe result in both the success and the failure case — a
    source that does not answer is a finding, not a server error.
    """
    data = flask_request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    domain = data.pop("domain", "price")
    if domain not in _TIMESERIES_DOMAIN_SECTIONS:
        return jsonify({"error": "domain must be 'price' or 'pv'"}), 400

    get_value = _effective_value_getter(data)
    return jsonify(_probe_timeseries_domain(domain, data, get_value)), 200


@config_bp.route("/test-entity", methods=["POST"])
def test_entity():
    """
    Read one sensor entity/item and report whether it is usable.

    Body: optional flat dot-notation keys (as for PUT /), so the UI can test a name
    the user has typed but not yet saved, plus ``key`` naming the sensor field to
    test. Anything absent falls back to the stored config.

    Returns 200 in both the success and the failure case — an entity that does not
    answer is a finding to show the user, not a server error.
    """
    data = flask_request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    key = data.pop("key", "")
    if not key or _resolve_schema_key(key) is None:
        return jsonify({"error": "key must name a sensor field"}), 400

    get_value = _effective_value_getter(data)
    source, url, token, ssl_ignore = _entity_probe_connection(key, get_value)
    result = probe_entity(
        source,
        get_value(key),
        url,
        access_token=token,
        ssl_ignore=ssl_ignore,
    )
    return jsonify(result), 200


def _entity_probe_connection(key: str, get_value):
    """
    The connection the interface behind *key* actually reads through.

    Load and battery always inherit the central data source (``merger.
    _apply_data_source_inheritance``), but ``pv_autoscaling`` can opt out of it and
    carry its own host and token. Probing ``data_source.*`` for that one would test a
    connection it never uses, and report a working entity as missing — or the reverse.

    Returns ``(source, url, access_token, ssl_ignore)``.
    """
    section = key.split(".")[0]

    if section == "pv_autoscaling" and not get_value(
        "pv_autoscaling.use_ha_central_data_source"
    ):
        return (
            get_value("pv_autoscaling.src"),
            get_value("pv_autoscaling.url"),
            get_value("pv_autoscaling.access_token"),
            bool(get_value("pv_autoscaling.ssl_ignore")),
        )

    return (
        get_value("data_source.type"),
        get_value("data_source.url"),
        get_value("data_source.access_token"),
        bool(get_value("data_source.ssl_ignore")),
    )


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
            except ValueError as exc:
                logger.debug("[ConfigWeb] fixed_24h_array is not parseable: %s", exc)
                errors.append({
                    "key": "price.fixed_24h_array",
                    "error": "Array must contain numeric values only"
                })

    return errors


def _dependency_met(field_def, data: dict, current_config: dict) -> bool:
    """
    Whether *field_def* applies at all, given the values this request would leave.

    Mirrors ``_isDependencyMet`` in the frontend: a ``depends_on`` entry is satisfied
    when the governing key holds one of the listed values, or — for ``"!empty"`` — any
    value at all.
    """
    if not field_def.depends_on:
        return True

    for dep_key, allowed in field_def.depends_on.items():
        if dep_key in data:
            current = data[dep_key]
        else:
            current = current_config
            for part in dep_key.split("."):
                current = current.get(part) if isinstance(current, dict) else None

        if allowed == "!empty":
            if not current:
                return False
        elif isinstance(allowed, list):
            if not any(str(a) == str(current) for a in allowed):
                return False
        elif str(allowed) != str(current):
            return False

    return True


def _validate_updates(data: dict) -> list[dict]:
    """Validate a dict of {key: value} against the schema. Returns list of error dicts."""
    errors = []
    current_config = _module.get_config()

    for key, value in data.items():
        field_def = _resolve_schema_key(key)
        if field_def is None:
            errors.append({"key": key, "error": "Unknown configuration key"})
            continue

        # A field the configuration does not reach cannot be missing a value. Marking
        # one required, or giving it a pattern no empty string matches, otherwise
        # rejects the whole request over a field the user was never shown — which is
        # how a fresh install could not complete the setup wizard: an empty
        # data_source.url that only applies to Home Assistant and OpenHAB, and
        # pv_autoscaling.sensor_entity_id, required but only when auto-scaling is on.
        if _is_empty(value) and not _dependency_met(field_def, data, current_config):
            continue

        err = _validate_single(field_def, value)
        if err:
            errors.append({"key": key, "error": err})

    return errors


def _is_empty(value) -> bool:
    """Whether *value* carries no content — ``None``, or a blank string."""
    return value is None or (isinstance(value, str) and not value.strip())


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
    except (TypeError, ValueError) as exc:
        logger.debug("[ConfigWeb] Value for '%s' will not coerce: %s", field_def.key, exc)
        return _type_error_message(field_def)

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


def _type_error_message(field_def) -> str:
    """
    A stable message for a value that will not coerce to the field's type.

    Deliberately excludes the exception text.  ``int("abc")`` puts the offending value
    into its own message, and interpolating that into an HTTP response is information
    exposure through an exception — CodeQL flags it, and it reads as developer noise to
    the person who has to act on it.  The detail is logged instead.
    """
    expected = {
        "int": "a whole number",
        "float": "a number",
        "bool": "true or false",
        "select": "one of the allowed choices",
    }.get(field_def.field_type, f"a valid {field_def.field_type} value")
    return f"Expected {expected}"


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
