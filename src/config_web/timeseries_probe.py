"""
Pre-flight probe for the unified ``timeseries`` data source.

The timeseries source deliberately accepts exactly one format (see
``interfaces/timeseries_normalizer``). Strictness is only workable if the user can see
*why* a source was rejected — and, just as important, see the numbers a source that was
*accepted* actually produces. A payload can match the format perfectly and still be
wrong by a factor of 1000 if the unit is misconfigured; the only way to catch that is to
show the converted value in the unit the user reads elsewhere in the UI.

This module therefore performs the same fetch → extract → normalize → convert chain the
running interfaces perform, and reports the outcome. It reuses the interfaces' own
normalizer and path extractor so that "the test passed" means the interface will
succeed too.
"""

import logging

import pytz
import requests

try:  # running from src/ as a script — src/ is on sys.path
    from interfaces.timeseries_normalizer import (
        PRICE_UNIT_TO_EUR_PER_WH,
        PV_UNITS,
        TEMPLATE_DOCS_ANCHOR,
        TimeseriesFormatError,
        convert_price_values,
        convert_pv_values,
        detect_resolution_seconds,
        extract_json_path,
        normalize_entries,
        price_plausibility_message,
        pv_plausibility_message,
    )
except ImportError:  # imported as src.config_web (tests)
    from ..interfaces.timeseries_normalizer import (
        PRICE_UNIT_TO_EUR_PER_WH,
        PV_UNITS,
        TEMPLATE_DOCS_ANCHOR,
        TimeseriesFormatError,
        convert_price_values,
        convert_pv_values,
        detect_resolution_seconds,
        extract_json_path,
        normalize_entries,
        price_plausibility_message,
        pv_plausibility_message,
    )

logger = logging.getLogger("__main__")

REQUEST_TIMEOUT_SECONDS = 10

# How many normalized slots to hand back for display.
SAMPLE_SLOT_COUNT = 3


def _display(domain, value):
    """Render one converted value in the unit the rest of the UI shows."""
    if domain == "price":
        # Internal unit is EUR/Wh; the schedule table shows ct/kWh.
        return {"value": round(value * 100_000, 2), "unit": "ct/kWh"}
    return {"value": round(value, 1), "unit": "Wh"}


def _status_error(status, url_field, resource_label):
    """Map an HTTP status onto a curated, actionable message (or None if it is fine)."""
    if status in (401, 403):
        return {
            "ok": False,
            "field": url_field,
            "error": (
                f"Endpoint refused the request ({status}). Check the access token."
            ),
        }
    if status == 404:
        return {
            "ok": False,
            "field": url_field,
            "error": f"'{resource_label}' not found (404). Check the URL / sensor entity name.",
        }
    if status >= 400:
        return {
            "ok": False,
            "field": url_field,
            "error": f"Endpoint returned HTTP {status}.",
        }
    return None


def probe(
    domain,
    data_url,
    data_path,
    data_token,
    value_unit,
    time_zone,
    resource_label=None,
    time_frame_base=3600,
    installed_power_w=0,
):
    """
    Fetch and validate a timeseries endpoint without touching any running interface.

    Args:
        domain: "price" or "pv" — selects the unit table and the display unit.
        data_url: Endpoint to fetch.
        data_path: Dot-notation path to the timeseries array.
        data_token: Optional bearer token.
        value_unit: Configured unit of the "value" field.
        time_zone: Zone name or tzinfo used to resolve timestamps.
        resource_label: What to call the thing being fetched in messages — the HA
            sensor entity when the URL was derived from one, else the URL itself.
        time_frame_base: The system's slot length (900 or 3600). Reported as a failure
            when the source cannot satisfy it, because the interfaces reject that
            combination outright and would otherwise fall back silently.
        installed_power_w: Total configured array power, used only to sanity-check PV
            magnitudes. Zero disables that check.

    Returns:
        dict: ``{"ok": bool, ...}``. On success: entry_count, resolution_seconds,
        value_unit, slots (start/end/value/unit) and warnings. On failure: a curated
        ``error`` string safe to show the user, plus ``field`` naming the config key
        most likely at fault.

    Never raises: every failure path is reported as a result so the caller can render
    it next to the field.
    """
    if domain not in ("price", "pv"):
        return {"ok": False, "error": f"unknown probe domain '{domain}'"}

    units = PRICE_UNIT_TO_EUR_PER_WH if domain == "price" else PV_UNITS
    unit_field = "price.value_unit" if domain == "price" else "pv_forecast_source.value_unit"
    url_field = "price.data_url" if domain == "price" else "pv_forecast_source.data_url"
    path_field = "price.data_path" if domain == "price" else "pv_forecast_source.data_path"

    if not data_url:
        return {
            "ok": False,
            "field": url_field,
            "error": "No data URL configured.",
            "transport": True,
        }

    if value_unit not in units:
        return {
            "ok": False,
            "field": unit_field,
            "error": (
                f"Unknown unit '{value_unit}'. Expected one of: "
                f"{', '.join(sorted(units))}."
            ),
        }

    try:
        tz = pytz.timezone(time_zone) if isinstance(time_zone, str) else time_zone
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    headers = {"Content-Type": "application/json"}
    if data_token:
        headers["Authorization"] = f"Bearer {data_token}"

    label = resource_label or data_url

    try:
        response = requests.get(
            data_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.exceptions.Timeout:
        logger.warning("[Probe] Timeseries probe timed out for %s", data_url)
        return {
            "ok": False,
            "field": url_field,
            "error": f"No response within {REQUEST_TIMEOUT_SECONDS}s. Check the URL and network.",
            "transport": True,
        }
    except requests.exceptions.RequestException as exc:
        # An HTTPError may carry the response that caused it (raise_for_status, or a
        # hook raising early). Report that status rather than a generic failure.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            status_error = _status_error(status, url_field, label)
            if status_error:
                return status_error
        # Curated message only — the exception text can carry internal detail.
        logger.warning("[Probe] Timeseries probe request failed: %s", exc, exc_info=True)
        return {
            "ok": False,
            "field": url_field,
            "error": "Could not reach the endpoint. Check the URL, port and TLS settings.",
            "transport": True,
        }

    status_error = _status_error(response.status_code, url_field, label)
    if status_error:
        return status_error

    try:
        payload = response.json()
    except ValueError:
        return {
            "ok": False,
            "field": url_field,
            "error": "Response is not valid JSON.",
        }

    raw_entries = extract_json_path(payload, data_path, label="Probe")
    if raw_entries is None:
        available = (
            ", ".join(repr(k) for k in payload.keys())
            if isinstance(payload, dict)
            else f"response is a {type(payload).__name__}"
        )
        return {
            "ok": False,
            "field": path_field,
            "error": (
                f"Path '{data_path}' does not exist in the response "
                f"(top level: {available})."
            ),
        }

    try:
        entries = normalize_entries(raw_entries, tz, label="Probe")
    except TimeseriesFormatError as exc:
        return {"ok": False, "field": path_field, "error": str(exc)}

    warnings = []
    resolution_seconds = detect_resolution_seconds(entries)
    if resolution_seconds is None:
        if len(entries) < 2:
            return {
                "ok": False,
                "field": path_field,
                "error": (
                    "Only one entry found — at least two are needed to determine the "
                    "slot resolution."
                ),
            }
        delta = int((entries[1]["start"] - entries[0]["start"]).total_seconds())
        return {
            "ok": False,
            "field": path_field,
            "error": (
                f"Unsupported slot resolution: {delta}s between the first two entries. "
                "Only 900s (15 min) and 3600s (hourly) are supported."
            ),
        }

    if resolution_seconds == 3600 and time_frame_base == 900:
        # PriceInterface / PvInterface refuse this combination and fall back, so a
        # green test here would be a lie.
        return {
            "ok": False,
            "field": path_field,
            "error": (
                "Source provides hourly data, but the system is configured for "
                "15-minute slots (eos.time_frame = 900). Use a 15-minute source or "
                "set the time frame to 3600."
            ),
        }

    try:
        if domain == "price":
            convert_price_values(entries, value_unit)
        else:
            convert_pv_values(entries, value_unit, resolution_seconds)
    except TimeseriesFormatError as exc:
        return {"ok": False, "field": unit_field, "error": str(exc)}

    if domain == "price":
        plausibility = price_plausibility_message(
            [entry["value"] for entry in entries], value_unit
        )
    else:
        plausibility = pv_plausibility_message(
            [entry["value"] for entry in entries],
            value_unit,
            resolution_seconds,
            installed_power_w,
        )
    if plausibility:
        warnings.append(plausibility)

    naive_hint = (
        f"Timestamps carry no UTC offset and were read as local time "
        f"({getattr(tz, 'zone', str(tz))}). If the source renders UTC, every slot is "
        f"shifted — see {TEMPLATE_DOCS_ANCHOR}."
    )
    if _has_naive_source_timestamps(raw_entries):
        warnings.append(naive_hint)

    slots = []
    for entry in entries[:SAMPLE_SLOT_COUNT]:
        rendered = _display(domain, entry["value"])
        slots.append(
            {
                "start": entry["start"].isoformat(),
                "end": entry["end"].isoformat() if entry["end"] else None,
                "value": rendered["value"],
                "unit": rendered["unit"],
            }
        )

    return {
        "ok": True,
        "entry_count": len(entries),
        "resolution_seconds": resolution_seconds,
        "value_unit": value_unit,
        "slots": slots,
        "warnings": warnings,
    }


def _has_naive_source_timestamps(raw_entries):
    """
    True when the source's own ``start`` strings omit a UTC offset.

    Checked against the raw payload rather than the normalized entries, because
    normalization has already localized them by then.
    """
    if not isinstance(raw_entries, list):
        return False
    for raw in raw_entries[:1]:
        if not isinstance(raw, dict):
            continue
        start = raw.get("start")
        if not isinstance(start, str):
            continue
        text = start.strip()
        if text.endswith("Z"):
            return False
        # An offset appears as +HH:MM / -HH:MM at the tail, after the date part.
        tail = text[10:]
        return "+" not in tail and "-" not in tail
    return False
