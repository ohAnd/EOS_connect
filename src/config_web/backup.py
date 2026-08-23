"""
Backup / Restore Blueprint — whole-install backup over HTTP.

Prefix: ``/api/backup``

Separate from ``/api/config`` on purpose.  That pair stays config-scoped and
byte-identical to what earlier versions produced, so files written by — and for — older
builds keep working; this one carries everything the install holds.

The file itself stays **flat**: settings sit at the top level next to the metadata and
the dataset keys, rather than inside a ``{"settings": {...}}`` envelope.  A nested file
handed to an older build would resolve zero top-level keys against its schema, import
nothing, and report success — a silent no-op after a Docker rollback.  Flat, that same
build restores every setting it recognises and skips one unknown key.
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request as flask_request

from .api import apply_settings, export_settings_dict, plan_settings

logger = logging.getLogger("__main__")

backup_bp = Blueprint("backup", __name__, url_prefix="/api/backup")

_FORMAT = "eos-connect-backup"
_VERSION = 1

# Local artifacts with no meaning on another install.  The meter counter is excluded for
# a stronger reason — see PvYieldStore.plan_import.
_ROW_FIELDS_NOT_EXPORTED = ("id", "created_at", "real_counter_kwh")

SETTINGS = "settings"
PV_YIELD_HISTORY = "pv_yield_history"
_DATASETS = (SETTINGS, PV_YIELD_HISTORY)

_store = None
_schema = None
_module = None


def init_backup(store, schema, module):
    """Wire the store, schema, and module references into the blueprint."""
    global _store, _schema, _module  # pylint: disable=global-statement
    _store = store
    _schema = schema
    _module = module


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _pv_store():
    """
    The PvYieldStore, or None when there is none.

    It is None when the table's schema init failed, and absent entirely on hosts that
    never wired one up, so every caller degrades instead of raising.
    """
    return getattr(_module, "pv_yield_store", None)


def _setting(key, fallback):
    """A stored setting, falling back to the schema default and then to *fallback*."""
    field_def = _schema.get(key) if _schema else None
    default = field_def.default if field_def is not None else fallback
    value = _store.get(key, default)
    return fallback if value is None else value


def _retention_days() -> int:
    """How many days of yield history the autoscaler currently keeps."""
    try:
        return int(_setting("pv_autoscaling.retention_days", 7))
    except (TypeError, ValueError):
        return 7


def _tz_name():
    """The configured local timezone, used to place 'today' when seeding."""
    return _setting("time_zone", None)


def _requested_datasets():
    """
    Parse ``?include=a,b`` into the datasets to act on.

    Absent means everything, so a plain URL still produces a full backup.  Unknown names
    are dropped rather than rejected: a newer file naming a dataset this build does not
    have should degrade, not fail.
    """
    raw = flask_request.args.get("include")
    if not raw:
        return list(_DATASETS)
    wanted = {name.strip() for name in raw.split(",") if name.strip()}
    return [name for name in _DATASETS if name in wanted]


def _flag(name: str) -> bool:
    """Read a boolean query flag."""
    return flask_request.args.get(name, "").lower() in ("1", "true", "yes")


def _export_rows():
    """Every stored yield row, stripped of fields that do not travel."""
    pv_store = _pv_store()
    if pv_store is None:
        return []
    return [
        {k: v for k, v in row.items() if k not in _ROW_FIELDS_NOT_EXPORTED}
        for row in pv_store.get_all_history()
    ]


# ------------------------------------------------------------------
# Info
# ------------------------------------------------------------------


@backup_bp.route("/info", methods=["GET"])
def backup_info():
    """Summarise what a backup taken now would contain."""
    rows = _export_rows()
    timestamps = sorted(row["timestamp"] for row in rows if row.get("timestamp"))
    return jsonify(
        {
            "datasets": list(_DATASETS),
            SETTINGS: {"available": True, "count": len(export_settings_dict())},
            PV_YIELD_HISTORY: {
                "available": _pv_store() is not None,
                "count": len(rows),
                "oldest": timestamps[0] if timestamps else None,
                "newest": timestamps[-1] if timestamps else None,
            },
            "retention_days": _retention_days(),
            # The export is never masked: a redacted backup could not restore.  Said
            # here so the UI can warn before the file reaches the user's disk.
            "contains_secrets": True,
        }
    )


# ------------------------------------------------------------------
# Export
# ------------------------------------------------------------------


@backup_bp.route("/export", methods=["GET"])
def export_backup():
    """Export the selected datasets as one flat JSON document."""
    include = _requested_datasets()
    payload = {}
    if SETTINGS in include:
        payload.update(export_settings_dict())

    payload["_format"] = _FORMAT
    payload["_version"] = _VERSION
    payload["_exported_at"] = datetime.now(timezone.utc).isoformat()
    payload["_datasets"] = include

    if PV_YIELD_HISTORY in include:
        payload[PV_YIELD_HISTORY] = _export_rows()

    logger.info("[Backup] Exported datasets: %s", ", ".join(include) or "none")
    return jsonify(payload)


# ------------------------------------------------------------------
# Import
# ------------------------------------------------------------------


@backup_bp.route("/import", methods=["POST"])
def import_backup():
    """
    Restore a backup file, or preview what restoring it would do.

    Query parameters:
        include: Comma-separated datasets to restore.  Default: all.
        mode: ``replace`` (default) makes the stored settings match the file exactly,
            removing those it does not contain.  ``merge`` keeps them.
        history_mode: ``as_is`` (default) restores yield rows at their own timestamps;
            ``seed`` shifts them into the retention window.
        dry_run: When set, nothing is written and the response describes what would
            happen.  The UI always previews before it commits — a whole-install restore
            is not something to discover the consequences of afterwards.
    """
    data = flask_request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    include = _requested_datasets()
    mode = "merge" if flask_request.args.get("mode") == "merge" else "replace"
    history_mode = "seed" if flask_request.args.get("history_mode") == "seed" else "as_is"
    dry_run = _flag("dry_run")

    result = {
        "dry_run": dry_run,
        "datasets": include,
        "mode": mode,
        "history_mode": history_mode,
        "format": data.get("_format"),
        "version": data.get("_version"),
        "exported_at": data.get("_exported_at"),
    }

    if SETTINGS in include:
        runner = plan_settings if dry_run else apply_settings
        result[SETTINGS] = runner(data, mode=mode)

    if PV_YIELD_HISTORY in include:
        result[PV_YIELD_HISTORY] = _restore_history(data, history_mode, dry_run)

    if not dry_run:
        logger.info(
            "[Backup] Restored %s (mode=%s, history_mode=%s)",
            ", ".join(include) or "nothing",
            mode,
            history_mode,
        )
    return jsonify(result)


def _restore_history(data: dict, history_mode: str, dry_run: bool) -> dict:
    """Restore (or preview) the yield rows a backup carries."""
    pv_store = _pv_store()
    rows = data.get(PV_YIELD_HISTORY)
    if pv_store is None:
        return {
            "available": False,
            "imported": 0,
            "present_in_file": len(rows) if isinstance(rows, list) else 0,
        }

    retention_days = _retention_days()
    kwargs = {
        "retention_days": retention_days,
        "tz_name": _tz_name(),
        "mode": history_mode,
    }
    plan = (
        pv_store.plan_import(rows, **kwargs)
        if dry_run
        else pv_store.import_rows(rows, **kwargs)
    )
    plan.pop("rows", None)
    return {**plan, "available": True, "retention_days": retention_days}
