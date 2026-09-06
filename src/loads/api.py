"""
HTTP surface for managed loads.

This is the endpoint issue #55 asked for: somewhere an external system can hand EOS
Connect a load forecast it has no way of computing itself. It also exposes what each
managed load is currently doing, and a manual override for the moments when the user
knows something the model does not.

The blueprint is the only place in this package that imports Flask. Everything it does
is a thin translation between HTTP and the manager - deliberately, so the behaviour
being tested lives in the manager and not behind a request context.
"""

import logging

from flask import Blueprint, jsonify, request as flask_request

from .contribution import SOURCE_API
from .gate import OVERRIDE_BLOCK, OVERRIDE_RELEASE
from .injection import InjectionError, PushedProfile, describe

logger = logging.getLogger("__main__")

loads_bp = Blueprint("managed_loads", __name__, url_prefix="/api/managed_loads")

# Set by init_api() before any request is served.
_manager = None


def init_api(manager):
    """Wire the manager into the blueprint."""
    global _manager  # pylint: disable=global-statement
    _manager = manager


def _unavailable():
    return jsonify({"error": "Managed loads are not configured"}), 404


@loads_bp.route("/", methods=["GET"])
@loads_bp.route("", methods=["GET"])
def list_loads():
    """Every managed load, its plan, its release state and its calibration."""
    if _manager is None:
        return _unavailable()
    return jsonify(_manager.status())


@loads_bp.route("/<load_id>", methods=["GET"])
def get_load(load_id):
    """One managed load."""
    if _manager is None:
        return _unavailable()
    item = _manager.instance(load_id)
    if item is None:
        return jsonify({"error": f"No managed load with id '{load_id}'"}), 404
    return jsonify(item.status())


@loads_bp.route("/<load_id>/push", methods=["POST"])
def push(load_id):
    """
    Hand this load a forecast or an energy budget.

    Accepts a JSON object or a bare number. The normalised series is echoed back so the
    caller can verify the alignment rather than guess at it - getting ``start`` wrong is
    the mistake a first integration makes, and it is invisible from the sending end.
    """
    if _manager is None:
        return _unavailable()

    payload = flask_request.get_json(silent=True)
    if payload is None:
        # A bare number is valid, and `silent=True` also swallows a malformed body, so
        # distinguish the two rather than reporting "empty payload" for broken JSON.
        raw = (flask_request.get_data(as_text=True) or "").strip()
        if raw:
            return jsonify({"error": "Request body is not valid JSON"}), 400

    try:
        parsed = _manager.push(load_id, payload, source=SOURCE_API)
    except InjectionError as exc:
        # These messages are written for the caller, not copied from an exception we
        # did not author, so echoing them leaks nothing.
        return jsonify({"error": str(exc)}), 400

    if isinstance(parsed, PushedProfile):
        body = describe(parsed, _manager.time_frame_base)
        body["kind"] = "profile"
    else:
        body = {
            "kind": "contingent",
            "total_wh": parsed.total_wh,
            "deadline_slot": parsed.deadline_slot,
            "valid_until": parsed.valid_until.isoformat(),
        }
    body["id"] = load_id
    body["accepted"] = True
    return jsonify(body)


@loads_bp.route("/<load_id>/push", methods=["DELETE"])
def clear_push(load_id):
    """Drop whatever was pushed, so the load stops contributing immediately."""
    if _manager is None:
        return _unavailable()
    try:
        cleared = _manager.clear_push(load_id)
    except InjectionError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"id": load_id, "cleared": bool(cleared)})


@loads_bp.route("/<load_id>/calibration/reset", methods=["POST"])
def reset_calibration(load_id):
    """
    Throw away what this load has learned and start again from its configuration.

    Worth doing when the inputs it was fitted against turn out to have been wrong: the
    recorded samples carry those inputs, so they keep dragging the fit until they age
    out of the retention window on their own.
    """
    if _manager is None:
        return _unavailable()
    try:
        state = _manager.reset_calibration(load_id)
    except InjectionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"id": load_id, "calibration": state})


@loads_bp.route("/<load_id>/override", methods=["POST"])
def override(load_id):
    """
    Force a load released or blocked for a while.

    ``{"mode": "release" | "block" | "clear", "minutes": 60}``. An override outranks the
    plan, the minimum runtime and frost protection alike: it exists for the case where
    the user knows something the model does not, and one the model can veto is not an
    override.
    """
    if _manager is None:
        return _unavailable()

    payload = flask_request.get_json(silent=True) or {}
    mode = str(payload.get("mode", "")).strip().lower()
    if mode in ("clear", "none", ""):
        mode = None
    elif mode not in (OVERRIDE_RELEASE, OVERRIDE_BLOCK):
        return jsonify({
            "error": "'mode' must be 'release', 'block' or 'clear'"
        }), 400

    minutes = payload.get("minutes", 60)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return jsonify({"error": "'minutes' must be a whole number"}), 400
    if mode is not None and not 1 <= minutes <= 1440:
        return jsonify({"error": "'minutes' must be between 1 and 1440"}), 400

    try:
        state = _manager.set_override(load_id, mode, minutes)
    except InjectionError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"id": load_id, "override": state})
