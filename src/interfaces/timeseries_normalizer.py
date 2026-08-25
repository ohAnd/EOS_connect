# -*- coding: utf-8 -*-
"""
Normalization of external timeseries payloads into the canonical EOS Connect format.

The canonical format is the one EVCC publishes, because that is the format users
already have on hand — either straight from an EVCC endpoint, or from a Home Assistant
template sensor written to feed EVCC (see discussion #214):

    {"start": "2026-08-23T00:00:00+02:00",
     "end":   "2026-08-23T00:15:00+02:00",
     "value": 0.3811}

    - start: ISO8601 (offset strongly recommended) or Unix seconds
    - end:   optional; derived from the next entry's start when absent
    - value: prices in EUR/kWh (EVCC `rates`), PV in W (EVCC `forecast.solar`)

Home Assistant is the transport here, not the format: whatever a given integration
names its attributes, the user shapes it into the above with a template sensor. This
module therefore does not guess field names — it validates them and, on mismatch,
reports which keys the payload actually had so the user can write that template.

The unit conversions live here as plain tables so the price and PV interfaces share one
definition instead of each carrying its own factor-of-1000.
"""

from datetime import datetime, timedelta
import logging

import pytz

logger = logging.getLogger("__main__")

# Anchor in docs/user-guide/configuration.html holding the ready-made HA template
# snippets. Referenced from error messages, so a format mismatch points at the fix.
TEMPLATE_DOCS_ANCHOR = "configuration.html#timeseries-templates"

# Price units → EUR/Wh, the unit the optimizer and the web UI work in internally.
PRICE_UNIT_TO_EUR_PER_WH = {
    "EUR/kWh": 1e-3,
    "ct/kWh": 1e-5,
    "EUR/Wh": 1.0,
}

# PV units → Wh per slot. Power units additionally need the slot length, so each entry
# carries whether it is a power reading.
PV_UNITS = {
    "W": {"factor": 1.0, "is_power": True},
    "kW": {"factor": 1000.0, "is_power": True},
    "Wh": {"factor": 1.0, "is_power": False},
    "kWh": {"factor": 1000.0, "is_power": False},
}

# Plausibility band for grid prices, in ct/kWh. Wide enough to pass any real tariff
# (including negative spot hours, which are filtered out by taking the median) and
# narrow enough that a factor-1000 unit error falls outside it in either direction.
PRICE_PLAUSIBLE_MIN_CT_KWH = 0.5
PRICE_PLAUSIBLE_MAX_CT_KWH = 150.0

# How far a PV peak may exceed installed capacity before it is called implausible.
# Headroom covers optimistic forecasts and arrays oversized against their inverter;
# the unit mistake it has to catch is a factor of four.
PV_PEAK_TOLERANCE = 1.5


class TimeseriesFormatError(ValueError):
    """
    Raised when a payload does not match the canonical format.

    The message is written to be shown to the user: it names the missing field, the
    keys that were actually present, and where to find a template snippet. Callers may
    surface it verbatim — it carries no exception text from lower layers.
    """


def _parse_timestamp(raw, tz):
    """
    Parse one timestamp into a timezone-aware datetime in *tz*.

    Returns (datetime, was_naive). A naive input is localized to *tz* rather than
    rejected, but the flag lets the caller warn once: a HA template using
    ``| timestamp_utc`` emits UTC wall-clock without an offset, which would otherwise
    be silently read as local time and shift every slot.
    """
    if isinstance(raw, bool):
        # bool is an int subclass; treating True as epoch 1 would be nonsense.
        raise TimeseriesFormatError(
            f"'start' must be a timestamp, got boolean {raw!r}"
        )

    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=pytz.UTC).astimezone(tz), False

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise TimeseriesFormatError("'start' is an empty string")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TimeseriesFormatError(
                f"'start' value {raw!r} is not a valid ISO8601 timestamp "
                f"or Unix timestamp"
            ) from exc
        if parsed.tzinfo is None:
            # localize() is pytz-specific; fall back for plain tzinfo objects.
            if hasattr(tz, "localize"):
                parsed = tz.localize(parsed)
            else:
                parsed = parsed.replace(tzinfo=tz)
            return parsed, True
        return parsed.astimezone(tz), False

    raise TimeseriesFormatError(
        f"'start' must be an ISO8601 string or Unix timestamp, got {type(raw).__name__}"
    )


def _describe_available_keys(entry):
    """Render the keys an entry actually carries, for an actionable error message."""
    if not isinstance(entry, dict):
        return f"entry is {type(entry).__name__}, not an object"
    if not entry:
        return "entry is an empty object"
    return "available fields: " + ", ".join(repr(k) for k in entry.keys())


def normalize_entries(raw_entries, tz, label="timeseries"):
    """
    Validate and normalize a raw payload into the canonical format.

    Args:
        raw_entries: Decoded JSON list from the configured data_path.
        tz: Target timezone (pytz timezone or tzinfo) for all timestamps.
        label: Prefix used in log messages, e.g. "PRICE-IF" or "PV-IF".

    Returns:
        list: ``[{"start": datetime (aware), "end": datetime (aware), "value": float}]``
        sorted by start, with ``end`` derived where the source omitted it.

    Raises:
        TimeseriesFormatError: With a user-facing message naming the offending field
            and the keys that were present instead.
    """
    if not isinstance(raw_entries, list):
        raise TimeseriesFormatError(
            f"expected a JSON array of timeseries entries, got "
            f"{type(raw_entries).__name__}"
        )
    if not raw_entries:
        raise TimeseriesFormatError("timeseries is empty")

    first = raw_entries[0]
    for field in ("start", "value"):
        if not isinstance(first, dict) or field not in first:
            raise TimeseriesFormatError(
                f"missing required field '{field}' in the first entry "
                f"({_describe_available_keys(first)}). The expected format is "
                f"{{start, end, value}} — shape the source with a Home Assistant "
                f"template sensor, see {TEMPLATE_DOCS_ANCHOR}"
            )

    entries = []
    naive_seen = 0
    for index, raw in enumerate(raw_entries):
        # Every entry has to parse. Dropping the bad ones would be friendlier right
        # up until it corrupts the result: the hourly price path maps this list to
        # slots by position, so a single missing entry shifts every later price an
        # hour early — silently, and in the direction that makes power look cheaper.
        # Rejecting the payload instead lets the caller fall back to cached data.
        if not isinstance(raw, dict) or "start" not in raw or "value" not in raw:
            raise TimeseriesFormatError(
                f"entry {index} is missing 'start' or 'value' "
                f"({_describe_available_keys(raw)})"
            )
        try:
            start, was_naive = _parse_timestamp(raw["start"], tz)
        except TimeseriesFormatError as exc:
            raise TimeseriesFormatError(f"entry {index}: {exc}") from exc
        try:
            value = float(raw["value"])
        except (TypeError, ValueError) as exc:
            raise TimeseriesFormatError(
                f"entry {index} has a non-numeric 'value': {raw['value']!r}"
            ) from exc
        if was_naive:
            naive_seen += 1
        entries.append({"start": start, "end": None, "value": value})

    if naive_seen:
        logger.warning(
            "[%s] %d timeseries timestamp(s) carry no UTC offset and were read as "
            "local time (%s). If the source renders UTC (e.g. a Home Assistant "
            "template using '| timestamp_utc'), every slot is shifted — emit ISO8601 "
            "with offset instead, e.g. '.isoformat()'. See %s",
            label,
            naive_seen,
            getattr(tz, "zone", str(tz)),
            TEMPLATE_DOCS_ANCHOR,
        )

    entries.sort(key=lambda item: item["start"])
    _derive_end_timestamps(entries)
    return entries


def _derive_end_timestamps(entries):
    """
    Fill each entry's ``end`` from the following entry's ``start``, in place.

    ``end`` is part of the documented format but is never read for any calculation;
    deriving it means a source that omits it (Tibber/EPEX HA integrations) is accepted
    rather than rejected over a field we do not use. The last entry reuses the
    preceding delta, falling back to one hour for a single-entry series.
    """
    count = len(entries)
    for index in range(count - 1):
        entries[index]["end"] = entries[index + 1]["start"]

    if count >= 2:
        last_delta = entries[-1]["start"] - entries[-2]["start"]
    else:
        last_delta = timedelta(hours=1)
    entries[-1]["end"] = entries[-1]["start"] + last_delta


def detect_resolution_seconds(entries):
    """
    Detect the slot length from the first two normalized entries.

    Returns:
        int: 900 or 3600, or None when it is neither or cannot be determined.
    """
    if len(entries) < 2:
        return None
    delta = int((entries[1]["start"] - entries[0]["start"]).total_seconds())
    if delta in (900, 3600):
        return delta
    return None


def convert_price_values(entries, unit):
    """
    Convert price entries to EUR/Wh in place and return them.

    Args:
        entries: Normalized entries whose ``value`` is in *unit*.
        unit: One of PRICE_UNIT_TO_EUR_PER_WH.

    Raises:
        TimeseriesFormatError: On an unknown unit.
    """
    try:
        factor = PRICE_UNIT_TO_EUR_PER_WH[unit]
    except KeyError as exc:
        raise TimeseriesFormatError(
            f"unknown price unit {unit!r}, expected one of "
            f"{', '.join(sorted(PRICE_UNIT_TO_EUR_PER_WH))}"
        ) from exc

    for entry in entries:
        entry["value"] = entry["value"] * factor
    return entries


def convert_pv_values(entries, unit, resolution_seconds):
    """
    Convert PV entries to Wh per slot in place and return them.

    Power units (W, kW) are integrated over the slot length, matching how the EVCC PV
    adapter treats EVCC's own forecast values. Energy units pass through scaled.

    Args:
        entries: Normalized entries whose ``value`` is in *unit*.
        unit: One of PV_UNITS.
        resolution_seconds: Source slot length in seconds (900 or 3600).

    Raises:
        TimeseriesFormatError: On an unknown unit.
    """
    try:
        spec = PV_UNITS[unit]
    except KeyError as exc:
        raise TimeseriesFormatError(
            f"unknown PV unit {unit!r}, expected one of {', '.join(sorted(PV_UNITS))}"
        ) from exc

    factor = spec["factor"]
    if spec["is_power"]:
        factor *= resolution_seconds / 3600.0

    for entry in entries:
        entry["value"] = entry["value"] * factor
    return entries


def _median(values):
    """Median of a non-empty sequence, without pulling in statistics for one call."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def pv_plausibility_message(values_wh, unit, resolution_seconds, installed_power_w):
    """
    Check converted PV energies against the installed generator.

    The PV unit mistake (power read as energy, or the reverse) is a factor of four on
    15-minute data — small enough to look like a bad forecast rather than a
    misconfiguration. Comparing the peak slot against installed capacity catches it,
    since no array can exceed its own rating.

    Args:
        values_wh: Converted energy per slot, in Wh.
        unit: The configured unit, named in the message.
        resolution_seconds: Slot length the values belong to.
        installed_power_w: Sum of the configured array power, or 0 when unknown.

    Returns:
        str: A user-facing warning, or None when nothing looks wrong. Returns None
        whenever installed power is unknown — a timeseries source does not require PV
        installations to be configured, and guessing without a reference would produce
        noise.
    """
    if not values_wh or not installed_power_w or installed_power_w <= 0:
        return None

    slot_hours = resolution_seconds / 3600.0
    if slot_hours <= 0:
        return None

    peak_power_w = max(values_wh) / slot_hours
    if peak_power_w <= installed_power_w * PV_PEAK_TOLERANCE:
        return None

    return (
        f"implausible PV level: peak {peak_power_w / 1000:.1f} kW for unit '{unit}', "
        f"but only {installed_power_w / 1000:.1f} kW is installed. If the source "
        f"reports power rather than energy per slot, set the unit to 'W' or 'kW'."
    )


def price_plausibility_message(values_eur_per_wh, unit):
    """
    Check converted prices against a plausible tariff band.

    Uses the median so that a handful of negative or zero spot hours cannot trip the
    check, while a wholesale unit mistake — which shifts every value — does.

    Args:
        values_eur_per_wh: Converted price values.
        unit: The configured unit, named in the message.

    Returns:
        str: A user-facing warning, or None when the prices look plausible.
    """
    if not values_eur_per_wh:
        return None

    median_ct_kwh = _median([abs(v) for v in values_eur_per_wh]) * 100_000
    if PRICE_PLAUSIBLE_MIN_CT_KWH <= median_ct_kwh <= PRICE_PLAUSIBLE_MAX_CT_KWH:
        return None

    if median_ct_kwh > PRICE_PLAUSIBLE_MAX_CT_KWH:
        hint = (
            f"values look about 1000x too high for unit '{unit}' — the source is "
            f"probably already in EUR/Wh, or in ct/kWh"
        )
    else:
        hint = (
            f"values look about 1000x too low for unit '{unit}' — the source is "
            f"probably in EUR/kWh"
        )

    return (
        f"implausible price level: median {median_ct_kwh:.1f} ct/kWh, expected "
        f"{PRICE_PLAUSIBLE_MIN_CT_KWH}-{PRICE_PLAUSIBLE_MAX_CT_KWH} ct/kWh. {hint}"
    )


def extract_json_path(obj, path, label="timeseries"):
    """
    Extract a nested value from a decoded JSON payload using dot notation.

    Examples:
    - 'attributes.data' -> obj['attributes']['data']
    - 'data'            -> obj['data']
    - 'prices[0].data'  -> obj['prices'][0]['data']

    Shared by the price interface, the PV interface and the config-web probe so the
    pre-flight test resolves exactly the same path the running interface will.

    Args:
        obj: JSON object (dict or list)
        path: Dot-notation path string
        label: Prefix used in log messages, e.g. "PRICE-IF" or "PV-IF".

    Returns:
        Extracted value, or None if the path does not resolve.
    """
    try:
        current = obj
        for part in path.split("."):
            if "[" in part:
                # Handle array index notation (e.g., "prices[0]")
                key, index_str = part.split("[")
                index = int(index_str.rstrip("]"))
                if key:
                    current = current[key][index]
                else:
                    current = current[index]
            else:
                current = current[part]
        return current
    except (KeyError, IndexError, TypeError, ValueError):
        logger.warning("[%s] Could not extract path '%s' from JSON response", label, path)
        return None
