"""
Pre-flight probe for a single sensor field, whatever it names.

The sensor fields are free-text: nothing stops a typo, and nothing used to catch one.
Home Assistant is especially unhelpful here — the history endpoint the load interface
reads answers an unknown entity with ``200 []`` rather than a 404, so a wrong name
produces no error anywhere and simply degrades to the built-in profile. The only way
for the user to tell the difference is to ask before saving.

Three shapes are probed, named by the value the field holds:
- an EVCC virtual entity (``group:home``, ``group:loadpoint:Carport``,
  ``tariff:grid``) — answered from the history endpoints;
- a dotted path into EVCC's live state (``battery.soc``);
- an ordinary Home Assistant entity or openHAB item, on whichever connection the
  field resolves to.

This reuses the interfaces' own readers, so "the test passed" means the interface
will read the same value.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

try:  # running from src/ as a script — src/ is on sys.path
    from interfaces.state_source import SUPPORTED_SOURCES, fetch_remote_state
    from interfaces.evcc_entities import (
        EVCC_HISTORY_GROUPS,
        is_evcc_history_entity,
        is_evcc_reference,
        is_evcc_state_key,
    )
except ImportError:  # imported as src.config_web (tests)
    from ..interfaces.state_source import SUPPORTED_SOURCES, fetch_remote_state
    from ..interfaces.evcc_entities import (
        EVCC_HISTORY_GROUPS,
        is_evcc_history_entity,
        is_evcc_reference,
        is_evcc_state_key,
    )

logger = logging.getLogger("__main__")

REQUEST_TIMEOUT_SECONDS = 30


def _probe_virtual_entity(sensor, url, request_timeout):
    """Answer an EVCC group/tariff reference from the history endpoints.

    A virtual entity has no /api/states entry - the load interface reads it from
    /api/history/energy (or /api/history/tariffs), so that is what gets asked.
    Reporting the slots the series actually holds turns "does this exist?" into
    something the user can act on: a battery title mistyped reads as zero slots,
    not as a server error.
    """
    if not is_evcc_history_entity(sensor):
        return {
            "ok": False,
            "error": (
                f"'{sensor}' is not a series EVCC keeps. Use group:<name> or "
                f"group:<name>:<title> with one of {', '.join(EVCC_HISTORY_GROUPS)}, "
                "or tariff:grid / tariff:feedin for the price history."
            ),
        }

    parts = sensor.strip().split(":")
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=24)
    try:
        if parts[0] == "tariff":
            response = requests.get(
                f"{url}/api/history/tariffs",
                params={
                    "from": start.isoformat(),
                    "to": now.isoformat(),
                    "aggregate": "15m",
                },
                timeout=request_timeout,
            )
            slots = response.json() or []
            column = parts[1]
            filled = [
                slot
                for slot in slots
                if isinstance(slot, dict) and slot.get(column) is not None
            ]
            if not filled:
                return {
                    "ok": False,
                    "error": (
                        f"EVCC has no persisted '{column}' tariff slots in the "
                        "last 24 h. The tariff source EVCC uses has to answer "
                        "for the history to hold this column."
                    ),
                }
            latest = filled[-1]
            stamp = latest.get("start", "")
            return {
                "ok": True,
                "error": "",
                "value": latest.get(column),
                "detail": (
                    f"{len(filled)} tariff slots in the last 24 h "
                    f"(latest {stamp}, {latest.get(column)} currency/kWh)"
                ),
            }

        params = {
            "from": start.isoformat(),
            "to": now.isoformat(),
            "aggregate": "15m",
            "grouped": "false",
        }
        group, title = (parts[1], parts[2] if len(parts) == 3 else None)
        if title:
            params["title"] = title
        response = requests.get(
            f"{url}/api/history/energy", params=params, timeout=request_timeout
        )
        series = response.json() or []
    except requests.exceptions.RequestException as exc:
        logger.warning(
            "[EntityProbe] EVCC history for %r failed: %s",
            sensor,
            exc,
            exc_info=True,
        )
        return {
            "ok": False,
            "error": "Could not reach EVCC. Check the URL, port and TLS settings.",
        }
    except ValueError:
        return {"ok": False, "error": "EVCC answered with invalid JSON."}

    matching = [
        entry
        for entry in series
        if isinstance(entry, dict)
        and entry.get("group") == group
        and (title is None or entry.get("title") == title)
    ]
    slots = sum(len(entry.get("data") or []) for entry in matching)
    if not slots:
        if title and any(
            isinstance(entry, dict) and entry.get("group") == group for entry in series
        ):
            return {
                "ok": False,
                "error": (
                    f"EVCC meters group '{group}' but has no series titled "
                    f"'{title}'. Titles of a group are listed on EVCC's own "
                    "energy page — the title filter is case-sensitive."
                ),
            }
        return {
            "ok": False,
            "error": (
                f"EVCC history holds no '{group}' data in the last 24 h. "
                "The meter has to be configured in EVCC, with its database "
                "enabled, for this series to exist."
            ),
        }

    # The last slot is still being filled at request time (its energy covers
    # only the elapsed fraction), so it under-reads as a "constant" whatever
    # the moment happens to be. Closed slots only, and the *net* Wh each.
    closed = [slot for entry in matching for slot in (entry.get("data") or [])[:-1]]
    nets = [
        (slot.get("energy") or 0.0) - (slot.get("returnEnergy") or 0.0)
        for slot in closed
    ]

    # A whole-group reference sums every series EVCC puts into the group —
    # the same thing EVCC's own grouped answers do. loadpoint group carries
    # both the heat pump and the wallbox; taking one series' value would
    # misreport the other's car as connected.
    if title is None and len(matching) > 1:
        total = 0.0
        for entry in matching:
            data = entry.get("data") or []
            # The same still-filling rule: last *closed* slot each.
            last_closed = data[-2] if len(data) >= 2 else None
            if last_closed is not None:
                total += (last_closed.get("energy") or 0.0) - (
                    last_closed.get("returnEnergy") or 0.0
                )
        titles = [entry.get("title") or "(untitled)" for entry in matching]
        return {
            "ok": True,
            "error": "",
            # kWh per 15-min slot to average W: ×1000 (kWh→Wh) ×4 (15 min).
            "value": round(total * 1000.0 * 4.0, 1),
            "detail": (
                f"{slots} slots in the last 24 h; summed series: "
                f"{', '.join(titles)} — average W over the last closed slot"
            ),
        }

    # Single series (or a group that only has one): report the last closed
    # slot's average power. kWh per 15-min slot × 1000 × 4 = W, the unit the
    # field is named for and what EVCC's live power readings compare against.
    value = round(nets[-1] * 4000.0, 1) if nets else None
    latest_slot = closed[-1].get("start") if closed else None
    return {
        "ok": True,
        "error": "",
        "value": value,
        "detail": (
            f"{slots} slots in the last 24 h, average over the last closed "
            f"slot ({latest_slot})"
        ),
    }


def _probe_evcc_state_path(sensor, url, request_timeout):
    """Answer a dotted path into EVCC's live state (battery.soc)."""
    try:
        response = requests.get(f"{url}/api/state", timeout=request_timeout)
        payload = response.json()
    except requests.exceptions.RequestException as exc:
        logger.warning(
            "[EntityProbe] EVCC state for %r failed: %s", sensor, exc, exc_info=True
        )
        return {
            "ok": False,
            "error": "Could not reach EVCC. Check the URL, port and TLS settings.",
        }
    except ValueError:
        return {"ok": False, "error": "EVCC answered with invalid JSON."}
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]

    current = payload
    for part in str(sensor).split("."):
        if not isinstance(current, dict) or part not in current:
            return {
                "ok": False,
                "error": (
                    f"EVCC's live state has no '{sensor}'. Keys it offers are its "
                    "own — e.g. battery.soc, battery.power."
                ),
            }
        current = current[part]
    if current is None:
        return {
            "ok": False,
            "error": f"EVCC's live state reports nothing under '{sensor}'.",
        }
    return {"ok": True, "error": "", "value": current}


# pylint: disable=too-many-return-statements
def probe_entity(source, sensor, url, access_token="", ssl_ignore=False):
    """
    Read one entity/item and report whether it is usable.

    Args:
        source: "homeassistant" or "openhab".
        sensor: Entity id or item name to look up.
        url: Base URL of the instance.
        access_token: Bearer token; Home Assistant only.
        ssl_ignore: Skip TLS certificate verification.

    Returns:
        ``{"ok": bool, "error": str}`` and, when ok, ``"value"`` with the state as the
        source reports it (for a virtual entity: the latest slot's net Wh), plus
        ``"detail"`` with human context where the probe knows more than a value.
        Never raises: an unreachable source is a finding to show the
        user, not a server error.
    """
    sensor = str(sensor or "").strip()
    if source not in SUPPORTED_SOURCES:
        return {
            "ok": False,
            "error": ("Set the data source before testing an entity."),
        }
    if not sensor:
        return {"ok": False, "error": "Enter an entity/item name to test."}
    if not str(url or "").strip():
        return {"ok": False, "error": "The data source URL is not configured."}

    # Shape first, validity second: a "group:" name that does not exist is still
    # an EVCC reference, and saying so beats reporting it as a missing entity.
    if is_evcc_reference(sensor):
        return _probe_virtual_entity(sensor, url, REQUEST_TIMEOUT_SECONDS)
    if source == "evcc" and is_evcc_state_key(sensor):
        return _probe_evcc_state_path(sensor, url, REQUEST_TIMEOUT_SECONDS)

    label = "Entity" if source == "homeassistant" else "Item"

    try:
        state = fetch_remote_state(
            source,
            str(sensor).strip(),
            url=url,
            access_token=access_token,
            request_timeout=REQUEST_TIMEOUT_SECONDS,
            ssl_ignore=ssl_ignore,
        )
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            return {
                "ok": False,
                "error": (
                    f"{label} '{sensor}' not found (404). Check the name — Home "
                    "Assistant entity ids look like sensor.house_power."
                ),
            }
        if status in (401, 403):
            return {
                "ok": False,
                "error": (
                    f"The data source refused the request ({status}). Check the access "
                    "token."
                ),
            }
        # Curated message only — the exception text can carry internal detail.
        logger.warning(
            "[EntityProbe] Request for %r failed with HTTP %s",
            sensor,
            status,
            exc_info=True,
        )
        return {"ok": False, "error": f"The data source returned HTTP {status}."}
    except requests.exceptions.RequestException as exc:
        # A requests exception names the resolved host, port and any proxy in the
        # chain. That belongs in the log, not in a response served over HTTP.
        logger.warning(
            "[EntityProbe] Could not reach the data source for %r: %s",
            sensor,
            exc,
            exc_info=True,
        )
        return {
            "ok": False,
            "error": "Could not reach the data source. Check the URL, port and TLS settings.",
        }
    except ValueError as exc:
        logger.warning("[EntityProbe] Cannot probe %r: %s", sensor, exc)
        return {
            "ok": False,
            "error": "Cannot read this entity with the current data source.",
        }

    if state == "":
        return {
            "ok": False,
            "error": (
                f"{label} '{sensor}' exists but reports no value. Check that it is "
                "available and not 'unknown'."
            ),
        }
    if state.lower() in ("unknown", "unavailable"):
        return {
            "ok": False,
            "error": f"{label} '{sensor}' currently reports '{state}'.",
        }
    result = {"ok": True, "error": "", "value": state}
    if source == "evcc":
        # A dotted state path carries no unit of its own; the raw number is the
        # answer. Naming the source keeps the "what am I actually testing"
        # question answerable when the field no longer carries an entity id.
        result["detail"] = "read from EVCC's live state"
    return result
