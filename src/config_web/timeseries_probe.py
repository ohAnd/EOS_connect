"""
Pre-flight probe for the unified ``timeseries`` data source.

The timeseries source deliberately accepts exactly one format (see
``interfaces/timeseries_normalizer``). Strictness is only workable if the user can see
*why* a source was rejected — and, just as important, see the numbers a source that was
*accepted* actually produces. A payload can match the format perfectly and still be
wrong by a factor of 1000 if the unit is misconfigured; the only way to catch that is to
show the converted value in the unit the user reads elsewhere in the UI.

The work is split in two halves: ``probe`` fetches (and owns every transport
message), ``analyse`` judges an already-fetched payload. The offline doctor runs
``analyse`` directly on a pasted file, so the CLI and the "Test connection"
button can never disagree about a payload — same code, same message constants.
"""

import logging
from dataclasses import dataclass

import pytz
import requests

try:  # running from src/ as a script — src/ is on sys.path
    from timeseries_grid import (
        KIND_INTENSIVE,
        TimeseriesGridError,
        horizon_slots,
        local_midnight,
        place_on_grid,
    )
except ImportError:  # imported as src.config_web (tests)
    from ..timeseries_grid import (
        KIND_INTENSIVE,
        TimeseriesGridError,
        horizon_slots,
        local_midnight,
        place_on_grid,
    )

try:  # running from src/ as a script — src/ is on sys.path
    from interfaces.timeseries_normalizer import (
        PRICE_UNIT_TO_EUR_PER_WH,
        PV_UNITS,
        TEMPLATE_DOCS_URL,
        TimeseriesFormatError,
        convert_price_values,
        convert_energy_values,
        median_span_seconds,
        detect_resolution_seconds,
        extract_json_path,
        normalize_entries,
        price_plausibility_message,
        pv_plausibility_message,
        resolve_field_names,
    )
except ImportError:  # imported as src.config_web (tests)
    from ..interfaces.timeseries_normalizer import (
        PRICE_UNIT_TO_EUR_PER_WH,
        PV_UNITS,
        TEMPLATE_DOCS_URL,
        TimeseriesFormatError,
        convert_price_values,
        convert_energy_values,
        median_span_seconds,
        detect_resolution_seconds,
        extract_json_path,
        normalize_entries,
        price_plausibility_message,
        pv_plausibility_message,
        resolve_field_names,
    )

try:  # running from src/ as a script — src/ is on sys.path
    from interfaces.http_fetch import DEFAULT_TIMEOUT_SECONDS, bearer_request_kwargs
except ImportError:  # imported as src.config_web (tests)
    from ..interfaces.http_fetch import DEFAULT_TIMEOUT_SECONDS, bearer_request_kwargs

logger = logging.getLogger("__main__")

REQUEST_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS

# How many normalized slots to hand back for display.
SAMPLE_SLOT_COUNT = 3

# Analysis messages, as constants so the offline doctor renders the exact same
# words the "Test connection" button does.
MSG_UNKNOWN_DOMAIN = "unknown probe domain '{domain}'"
MSG_UNKNOWN_UNIT = "Unknown unit '{unit}'. Expected one of: {expected}."
WARN_UNUSUAL_RESOLUTION = (
    "Unusual slot resolution ({resolution}s between entries) — values are placed "
    "on the grid by their timestamps: a finer entry averages (prices) or sums "
    "(PV energy) into each slot, a coarser one covers every slot it spans."
)
WARN_HOURLY_ON_QUARTER = (
    "Source is hourly on a 15-minute system: each price holds for its four "
    "15-minute slots (PV energy is split across them)."
)
WARN_COARSE_SPAN = (
    "Each entry spans {slots} slots and covers all of them — check whether this "
    "is really a per-slot series and not a daily average."
)
MSG_WINDOW_MISS = (
    "None of the {count} entries fall into the {slots}-slot optimization window "
    "starting {anchor}. The running interface would keep its last good data — "
    "the series is stale, or entirely beyond the horizon."
)
MSG_SENSOR_UNAVAILABLE = (
    "'{label}' is currently '{state}' in Home Assistant — the sensor exists but "
    "carries no data right now. Check the integration providing it."
)
WARN_PARTIAL_WINDOW = (
    "The series covers slots {first}-{last} of {slots} ({covered} covered). "
    "Uncovered price slots are filled by policy and hatched as forecast in the "
    "chart; PV treats them as 0 Wh."
)
WARN_NAIVE_TIMESTAMPS = (
    "Timestamps carry no UTC offset and were read as local time ({zone}). If the "
    "source renders UTC, every slot is shifted — see {docs}."
)


@dataclass(frozen=True)
class ProbeDomain:
    """
    Everything that distinguishes one probed domain from another.

    A new domain (feed-in, later temperature or base load) is one entry in
    DOMAINS instead of another branch in every function that used to ask
    ``if domain == "price"``.
    """

    units: dict  # accepted value_unit spellings -> conversion spec
    convert: callable  # (entries, unit) -> entries, in place
    plausibility: callable  # (values, unit, resolution_s, installed_power_w) -> str|None
    display_factor: float  # internal unit -> the unit the UI shows
    display_round: int
    display_unit: str
    unit_field: str  # config keys errors should point at
    url_field: str
    path_field: str


DOMAINS = {
    "price": ProbeDomain(
        units=PRICE_UNIT_TO_EUR_PER_WH,
        convert=convert_price_values,
        plausibility=lambda values, unit, resolution, power: (
            price_plausibility_message(values, unit)
        ),
        # Internal unit is EUR/Wh; the schedule table shows ct/kWh.
        display_factor=100_000.0,
        display_round=2,
        display_unit="ct/kWh",
        unit_field="price.value_unit",
        url_field="price.data_url",
        path_field="price.data_path",
    ),
    "pv": ProbeDomain(
        units=PV_UNITS,
        convert=convert_energy_values,
        plausibility=pv_plausibility_message,
        display_factor=1.0,
        display_round=1,
        display_unit="Wh",
        unit_field="pv_forecast_source.value_unit",
        url_field="pv_forecast_source.data_url",
        path_field="pv_forecast_source.data_path",
    ),
    # Feed-in prices are prices: same units, conversion and plausibility — only
    # the config keys differ (they live in the price section, feed_in_-prefixed).
    "feed_in": ProbeDomain(
        units=PRICE_UNIT_TO_EUR_PER_WH,
        convert=convert_price_values,
        plausibility=lambda values, unit, resolution, power: (
            price_plausibility_message(values, unit)
        ),
        display_factor=100_000.0,
        display_round=2,
        display_unit="ct/kWh",
        unit_field="price.feed_in_value_unit",
        url_field="price.feed_in_data_url",
        path_field="price.feed_in_data_path",
    ),
}


def analyse(
    raw_entries,
    domain,
    value_unit,
    time_zone,
    time_frame_base=3600,
    installed_power_w=0,
    anchor=None,
):
    """
    Judge an already-fetched timeseries payload.

    The transport-free half of the probe: the offline doctor feeds it a pasted
    file, the probe feeds it what it fetched — both get the same verdicts and
    the same warning texts.

    Args:
        raw_entries: The decoded array (what data_path points at).
        domain: A DOMAINS key — "price" or "pv".
        value_unit: Configured unit of the "value" field.
        time_zone: Zone name or tzinfo used to resolve timestamps.
        time_frame_base: The system's slot length (900 or 3600). A coarser
            source is accepted — values land on the grid by overlap — and the
            applied rule is reported as a warning so the test says what will
            happen, not just that something will.
        installed_power_w: Total configured array power, used only to
            sanity-check PV magnitudes. Zero disables that check.
        anchor: Slot 0 of the optimization window — local midnight today when
            None. The doctor's --today passes a different day through here.

    Returns:
        dict: ``{"ok": bool, ...}``. On success: entry_count,
        resolution_seconds, value_unit, first_slot/last_slot/covered_slots
        (where the series lands on the window), slots (start/end/value/unit)
        and warnings. A series that never touches the window is a failure —
        the running interface would fall back, and a green test would hide
        that. On failure: a curated ``error`` string safe to show the user,
        plus ``field`` naming the config key most likely at fault.

    Never raises: every failure path is reported as a result.
    """
    spec = DOMAINS.get(domain)
    if spec is None:
        return {"ok": False, "error": MSG_UNKNOWN_DOMAIN.format(domain=domain)}

    if value_unit not in spec.units:
        return {
            "ok": False,
            "field": spec.unit_field,
            "error": MSG_UNKNOWN_UNIT.format(
                unit=value_unit, expected=", ".join(sorted(spec.units))
            ),
        }

    try:
        tz = pytz.timezone(time_zone) if isinstance(time_zone, str) else time_zone
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    try:
        entries = normalize_entries(raw_entries, tz, label="Probe")
    except TimeseriesFormatError as exc:
        return {"ok": False, "field": spec.path_field, "error": str(exc)}

    warnings = []
    resolution_seconds = detect_resolution_seconds(entries)
    if resolution_seconds is None:
        # The entry spans themselves (a single entry included — the normalizer
        # always derives an end). The interface accepts exactly this, so the
        # probe must not be stricter; the coverage warning below carries the
        # quality signal.
        resolution_seconds = median_span_seconds(entries)
        if len(entries) >= 2:
            warnings.append(
                WARN_UNUSUAL_RESOLUTION.format(resolution=resolution_seconds)
            )

    if resolution_seconds == 3600 and time_frame_base == 900:
        warnings.append(WARN_HOURLY_ON_QUARTER)
    elif resolution_seconds and resolution_seconds > 4 * time_frame_base:
        warnings.append(
            WARN_COARSE_SPAN.format(slots=resolution_seconds // time_frame_base)
        )

    try:
        spec.convert(entries, value_unit)
    except TimeseriesFormatError as exc:
        return {"ok": False, "field": spec.unit_field, "error": str(exc)}

    # Where does the series actually land on the optimization window? The same
    # placement the interfaces run — probe green must mean the interface
    # serves a non-fallback array (and probe red that it will not).
    slot_count = horizon_slots(time_frame_base)
    if anchor is None:
        anchor = local_midnight(tz)
    try:
        placement = place_on_grid(
            entries, anchor, slot_count, time_frame_base, KIND_INTENSIVE
        )
    except TimeseriesGridError as exc:
        return {"ok": False, "field": spec.path_field, "error": str(exc)}

    covered_indices = [
        index for index, fraction in enumerate(placement.coverage) if fraction > 0
    ]
    if not covered_indices:
        return {
            "ok": False,
            "field": spec.url_field,
            "error": MSG_WINDOW_MISS.format(
                count=len(entries), slots=slot_count, anchor=anchor.isoformat()
            ),
        }
    first_slot = covered_indices[0]
    last_slot = covered_indices[-1]
    covered_slots = len(covered_indices)
    if covered_slots < slot_count:
        warnings.append(
            WARN_PARTIAL_WINDOW.format(
                first=first_slot,
                last=last_slot,
                slots=slot_count,
                covered=covered_slots,
            )
        )

    plausibility = spec.plausibility(
        [entry["value"] for entry in entries],
        value_unit,
        resolution_seconds,
        installed_power_w,
    )
    if plausibility:
        warnings.append(plausibility)

    if _has_naive_source_timestamps(raw_entries):
        warnings.append(
            WARN_NAIVE_TIMESTAMPS.format(
                zone=getattr(tz, "zone", str(tz)), docs=TEMPLATE_DOCS_URL
            )
        )

    slots = []
    for entry in entries[:SAMPLE_SLOT_COUNT]:
        slots.append(
            {
                "start": entry["start"].isoformat(),
                "end": entry["end"].isoformat() if entry["end"] else None,
                "value": round(
                    entry["value"] * spec.display_factor, spec.display_round
                ),
                "unit": spec.display_unit,
            }
        )

    return {
        "ok": True,
        "entry_count": len(entries),
        "resolution_seconds": resolution_seconds,
        "value_unit": value_unit,
        "first_slot": first_slot,
        "last_slot": last_slot,
        "covered_slots": covered_slots,
        "slots": slots,
        "warnings": warnings,
    }


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
    anchor=None,
):
    """
    Fetch a timeseries endpoint and `analyse` what it returns.

    This half owns the transport: URL/token handling and every curated message
    about reachability, HTTP status and JSON shape. Everything about the
    payload itself is judged by `analyse`, which the offline doctor shares.

    Args: as `analyse`, plus
        data_url: Endpoint to fetch.
        data_path: Dot-notation path to the timeseries array.
        data_token: Optional bearer token.
        resource_label: What to call the thing being fetched in messages — the
            HA sensor entity when the URL was derived from one, else the URL.

    Returns:
        dict: the `analyse` contract; transport failures additionally carry
        ``"transport": True``.

    Never raises: every failure path is reported as a result so the caller can
    render it next to the field.
    """
    spec = DOMAINS.get(domain)
    if spec is None:
        return {"ok": False, "error": MSG_UNKNOWN_DOMAIN.format(domain=domain)}

    if not data_url:
        return {
            "ok": False,
            "field": spec.url_field,
            "error": "No data URL configured.",
            "transport": True,
        }

    if value_unit not in spec.units:
        return {
            "ok": False,
            "field": spec.unit_field,
            "error": MSG_UNKNOWN_UNIT.format(
                unit=value_unit, expected=", ".join(sorted(spec.units))
            ),
        }

    label = resource_label or data_url

    try:
        response = requests.get(data_url, **bearer_request_kwargs(data_token))
    except requests.exceptions.Timeout:
        logger.warning("[Probe] Timeseries probe timed out for %s", data_url)
        return {
            "ok": False,
            "field": spec.url_field,
            "error": f"No response within {REQUEST_TIMEOUT_SECONDS}s. Check the URL and network.",
            "transport": True,
        }
    except requests.exceptions.RequestException as exc:
        # An HTTPError may carry the response that caused it (raise_for_status, or a
        # hook raising early). Report that status rather than a generic failure.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            status_error = _status_error(status, spec.url_field, label)
            if status_error:
                return status_error
        # Curated message only — the exception text can carry internal detail.
        logger.warning("[Probe] Timeseries probe request failed: %s", exc, exc_info=True)
        return {
            "ok": False,
            "field": spec.url_field,
            "error": "Could not reach the endpoint. Check the URL, port and TLS settings.",
            "transport": True,
        }

    status_error = _status_error(response.status_code, spec.url_field, label)
    if status_error:
        return status_error

    try:
        payload = response.json()
    except ValueError:
        return {
            "ok": False,
            "field": spec.url_field,
            "error": "Response is not valid JSON.",
        }

    if isinstance(payload, dict):
        # The most likely thing a user hits in the wild: the HA entity exists
        # but its integration is down, so the state says so and the attributes
        # are stale or gone. Name that instead of a path error.
        state = str(payload.get("state", "")).lower()
        if state in ("unavailable", "unknown"):
            return {
                "ok": False,
                "field": spec.url_field,
                "error": MSG_SENSOR_UNAVAILABLE.format(label=label, state=state),
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
            "field": spec.path_field,
            "error": (
                f"Path '{data_path}' does not exist in the response "
                f"(top level: {available})."
            ),
        }

    return analyse(
        raw_entries,
        domain,
        value_unit,
        time_zone,
        time_frame_base=time_frame_base,
        installed_power_w=installed_power_w,
        anchor=anchor,
    )


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


def _has_naive_source_timestamps(raw_entries):
    """
    True when the source's own start strings omit a UTC offset.

    Checked against the raw payload rather than the normalized entries, because
    normalization has already localized them by then. The start field is looked
    up through the alias table, so a start_time/period_start payload gets the
    same warning a canonical one does.
    """
    if not isinstance(raw_entries, list) or not raw_entries:
        return False
    start_key = resolve_field_names(raw_entries[0]).get("start")
    if start_key is None:
        return False
    for raw in raw_entries[:1]:
        if not isinstance(raw, dict):
            continue
        start = raw.get(start_key)
        if not isinstance(start, str):
            continue
        text = start.strip()
        if text.endswith("Z"):
            return False
        # An offset appears as +HH:MM / -HH:MM at the tail, after the date part.
        tail = text[10:]
        return "+" not in tail and "-" not in tail
    return False
