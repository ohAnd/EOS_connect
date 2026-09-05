"""
Pre-flight probe for a single Home Assistant entity or openHAB item.

The sensor fields are free-text: nothing stops a typo, and nothing used to catch one.
Home Assistant is especially unhelpful here — the history endpoint the load interface
reads answers an unknown entity with ``200 []`` rather than a 404, so a wrong name
produces no error anywhere and simply degrades to the built-in profile. The only way
for the user to tell the difference is to ask before saving.

This reuses the interfaces' own ``fetch_remote_state``, so "the test passed" means the
interface will read the same value.
"""

import logging

import requests

try:  # running from src/ as a script — src/ is on sys.path
    from interfaces.state_source import SUPPORTED_SOURCES, fetch_remote_state
except ImportError:  # imported as src.config_web (tests)
    from ..interfaces.state_source import SUPPORTED_SOURCES, fetch_remote_state

logger = logging.getLogger("__main__")

REQUEST_TIMEOUT_SECONDS = 10


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
        source reports it. Never raises: an unreachable source is a finding to show the
        user, not a server error.
    """
    if source not in SUPPORTED_SOURCES:
        return {
            "ok": False,
            "error": (
                "Set the data source to Home Assistant or openHAB before testing an "
                "entity."
            ),
        }
    if not str(sensor or "").strip():
        return {"ok": False, "error": "Enter an entity/item name to test."}
    if not str(url or "").strip():
        return {"ok": False, "error": "The data source URL is not configured."}

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
            "[EntityProbe] Request for %r failed with HTTP %s", sensor, status,
            exc_info=True,
        )
        return {"ok": False, "error": f"The data source returned HTTP {status}."}
    except requests.exceptions.RequestException as exc:
        # A requests exception names the resolved host, port and any proxy in the
        # chain. That belongs in the log, not in a response served over HTTP.
        logger.warning(
            "[EntityProbe] Could not reach the data source for %r: %s", sensor, exc,
            exc_info=True,
        )
        return {
            "ok": False,
            "error": "Could not reach the data source. Check the URL, port and TLS settings.",
        }
    except ValueError as exc:
        logger.warning("[EntityProbe] Cannot probe %r: %s", sensor, exc)
        return {"ok": False, "error": "Cannot read this entity with the current data source."}

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

    return {"ok": True, "error": "", "value": state}
