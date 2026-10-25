"""
Shared reader for entity state from Home Assistant, openHAB or EVCC.

Several interfaces poll a single sensor value over HTTP with the same shape: an
openHAB item endpoint, a Home Assistant states endpoint with a bearer token, or a
dotted path into EVCC's /api/state document. Keeping one implementation means a fix to
authentication, timeouts or TLS handling reaches all of them rather than only the
interface it was noticed in.
"""

import requests

SUPPORTED_SOURCES = ("homeassistant", "openhab", "evcc")


def fetch_evcc_state(url, request_timeout=10, ssl_ignore=False):
    """One EVCC ``/api/state`` document, with the legacy envelope unwrapped.

    Older EVCC versions wrapped every response in ``{"result": {...}}``; current
    ones answer with the state document directly. This is the one place that
    knows it, so a change to EVCC's envelope is a change here - it used to be
    repeated in ``evcc_parameters`` as well, where it quietly drifted out of
    reach of the tests covering this one.

    (``EvccInterface`` still carries its own copy for the version probe and its
    polling loop; that one answers a different question - it needs the raw
    response for error reporting - and is left alone deliberately.)

    Raises:
        requests.exceptions.RequestException: Propagated for the caller.
    """
    base = str(url or "").rstrip("/")
    response = requests.get(
        f"{base}/api/state", timeout=request_timeout, verify=not ssl_ignore
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if not isinstance(payload, dict):
        payload = {}
    return payload


def _evcc_path(payload, path):
    """The value at a dotted path like ``battery.soc``, or None when absent.

    Only plain dict traversal: a list index would be nice for ``pv.0.power`` but
    no configured field needs it, and silently indexing the wrong thing beats
    loudly failing the one this exists for.
    """
    current = payload
    for part in str(path or "").split("."):
        if not part:
            return None
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def needs_access_token(source) -> bool:
    """Whether *source* authenticates reads with a bearer token.

    Home Assistant does. openHAB's REST items are read without one, and EVCC's
    API carries no authentication at all - which is why its PV counter is read
    correctly with the token field left empty.

    Kept beside ``fetch_remote_state``, the function that decides it per source,
    so a caller warning about a missing token asks here rather than assuming.
    """
    return str(source or "").strip() == "homeassistant"


def fetch_remote_state(
    source, sensor, url, access_token="", request_timeout=10, ssl_ignore=False
):
    """
    Fetch the raw state string for one entity/item.

    Args:
        source: "homeassistant" or "openhab".
        sensor: Entity id (Home Assistant) or item name (openHAB).
        url: Base URL of the instance.
        access_token: Bearer token; Home Assistant only.
        request_timeout: Per-request timeout in seconds.
        ssl_ignore: Skip TLS certificate verification.

    Returns:
        The state as a trimmed string. Units are left in place; callers that expect a
        number take the first whitespace-separated token.

    Raises:
        ValueError: No sensor given, or an unsupported source.
        requests.exceptions.RequestException: Propagated for the caller to handle.
    """
    if not sensor:
        raise ValueError("Sensor/item identifier must be provided")

    base = str(url or "").rstrip("/")
    if source == "openhab":
        endpoint = f"{base}/rest/items/{sensor}"
        headers = None
    elif source == "homeassistant":
        endpoint = f"{base}/api/states/{sensor}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
    elif source == "evcc":
        payload = fetch_evcc_state(base, request_timeout, ssl_ignore)
        value = _evcc_path(payload, sensor)
        return "" if value is None else str(value).strip()
    else:
        raise ValueError(f"Unknown source: {source}")

    response = requests.get(
        endpoint, headers=headers, timeout=request_timeout, verify=not ssl_ignore
    )
    response.raise_for_status()
    return _details_from(response.json())


def fetch_remote_state_details(
    source, sensor, url, access_token="", request_timeout=10, ssl_ignore=False
):
    """
    Fetch one entity/item's state together with what it is measured in.

    Same request as `fetch_remote_state`, but the unit and device class come back too.
    Callers that need to know whether a sensor reports power or energy cannot tell from
    the number alone - 1234 W and 1234 kWh look identical - and reading one as the other
    is a silent wrong answer rather than a visible failure.

    Returns:
        ``{"state": str, "unit": str, "device_class": str}``. The last two are empty
        strings when the source does not report them, which openHAB and EVCC never do.

    Raises:
        Same as `fetch_remote_state`.
    """
    if not sensor:
        raise ValueError("Sensor/item identifier must be provided")

    base = str(url or "").rstrip("/")
    if source == "openhab":
        endpoint = f"{base}/rest/items/{sensor}"
        headers = None
    elif source == "homeassistant":
        endpoint = f"{base}/api/states/{sensor}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
    elif source == "evcc":
        payload = fetch_evcc_state(base, request_timeout, ssl_ignore)
        value = _evcc_path(payload, sensor)
        return {
            "state": "" if value is None else str(value).strip(),
            "unit": "",
            "device_class": "",
        }
    else:
        raise ValueError(f"Unknown source: {source}")

    response = requests.get(
        endpoint, headers=headers, timeout=request_timeout, verify=not ssl_ignore
    )
    response.raise_for_status()
    payload = response.json()
    attributes = payload.get("attributes") or {}
    return {
        "state": _details_from(payload),
        "unit": str(attributes.get("unit_of_measurement", "") or "").strip(),
        "device_class": str(attributes.get("device_class", "") or "").strip(),
    }


def _details_from(payload):
    """The state as a trimmed string, however the source wrapped it."""
    return str(payload.get("state", "")).strip()
