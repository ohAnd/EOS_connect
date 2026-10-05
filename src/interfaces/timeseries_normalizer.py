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
    - end:   optional; authoritative when present, else derived from the next
      entry's start but capped at the series' nominal spacing (a hole stays a
      hole). A series may also carry only end timestamps ("hour ending").
    - value: prices in EUR/kWh (EVCC `rates`), PV in W (EVCC `forecast.solar`)

Home Assistant is the transport here, not the format. The spellings the common
integrations publish (FIELD_ALIASES: start_time, period_start, end_time,
price_per_kwh, pv_estimate, …) are read directly; for anything else the user
shapes the payload with a template sensor, and a mismatch reports which keys
the payload actually had so that template is easy to write.

The unit conversions live here as plain tables so the price and PV interfaces share one
definition instead of each carrying its own factor-of-1000.
"""

from datetime import datetime, timedelta
import logging

import pytz

try:  # running from src/ as a script — src/ is on sys.path
    from docs_links import current_docs_url
except ImportError:  # imported as src.interfaces (tests)
    from ..docs_links import current_docs_url

logger = logging.getLogger("__main__")

# Section of the configuration guide holding the ready-made HA template snippets.
# Referenced from error messages, so a format mismatch points at the fix. Resolved
# through docs_links so a develop build names the develop copy of the site — the
# messages used to carry a bare "configuration.html#…", which said nothing about
# which of the two published sites to open.
TEMPLATE_DOCS_URL = current_docs_url(
    "user-guide/config-data.html#timeseries-templates"
)

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

# The same headroom for a managed load measured against its rated power. A heat pump
# on a defrost cycle or with a backup heater engaged draws above its nameplate, so the
# band has to allow more than the nameplate itself.
LOAD_PEAK_TOLERANCE = 1.5

# Ceiling for a load peak when no rated power is configured. Nothing in a house draws
# 50 kW continuously, so a series implying more than this is a unit mistake rather
# than a large appliance - but without a declared rating that is all we can say, and a
# tighter band would fire on someone's legitimately large installation.
LOAD_PLAUSIBLE_PEAK_W = 50000.0


class TimeseriesFormatError(ValueError):
    """
    Raised when a payload does not match the canonical format.

    The message is written to be shown to the user: it names the missing field, the
    keys that were actually present, and where to find a template snippet. Callers may
    surface it verbatim — it carries no exception text from lower layers.
    """


def _parse_timestamp(raw, tz, field="start"):
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
            f"'{field}' must be a timestamp, got boolean {raw!r}"
        )

    if isinstance(raw, datetime):
        # Already parsed (an entry coming back through normalize_entries, e.g.
        # the EVCC price branch normalizing before the shared parser does).
        if raw.tzinfo is None:
            if hasattr(tz, "localize"):
                return tz.localize(raw), True
            return raw.replace(tzinfo=tz), True
        return raw.astimezone(tz), False

    if isinstance(raw, (int, float)):
        # 1e11 s is the year 5138 - no plausible seconds timestamp reaches it,
        # but every current epoch-milliseconds value does (a HA template using
        # `as_timestamp() * 1000`). fromtimestamp would raise a bare ValueError
        # ("year must be in 1..9999") that nothing upstream catches.
        if abs(raw) >= 1e11:
            raise TimeseriesFormatError(
                f"'{field}' value {raw!r} looks like epoch milliseconds - send "
                "Unix seconds instead (drop the '* 1000' from the template)"
            )
        try:
            return datetime.fromtimestamp(raw, tz=pytz.UTC).astimezone(tz), False
        except (ValueError, OSError, OverflowError) as exc:
            raise TimeseriesFormatError(
                f"'{field}' value {raw!r} is not a usable Unix timestamp"
            ) from exc

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise TimeseriesFormatError(f"'{field}' is an empty string")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TimeseriesFormatError(
                f"'{field}' value {raw!r} is not a valid ISO8601 timestamp "
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
        f"'{field}' must be an ISO8601 string or Unix timestamp, got {type(raw).__name__}"
    )


def _describe_available_keys(entry):
    """Render the keys an entry actually carries, for an actionable error message."""
    if not isinstance(entry, dict):
        return f"entry is {type(entry).__name__}, not an object"
    if not entry:
        return "entry is an empty object"
    return "available fields: " + ", ".join(repr(k) for k in entry.keys())


#: Accepted spellings per canonical field, canonical name first. These are the
#: names the popular integrations actually publish (discussion #214): EPEX Spot
#: HACS and Tibber HACS use start_time/end_time/price_per_kwh, the Tibber core
#: action start_time/price, Solcast period_start/period_end/pv_estimate,
#: Open-Meteo time. A generic timestamp/time always means the interval START —
#: guessing it could mean the end would shift every slot for a "hour ending"
#: source, silently.
FIELD_ALIASES = {
    "start": ("start", "start_time", "period_start", "timestamp", "time"),
    "end": ("end", "end_time", "period_end"),
    "value": ("value", "price_per_kwh", "price", "pv_estimate"),
}


def resolve_field_names(first):
    """
    Pick the payload's field spellings from its first entry.

    One spelling per field for the whole payload: mixing spellings between
    entries is rejected later like any other malformed entry, because a mix is
    a template bug, not a format.

    Returns:
        dict: canonical name -> the key actually present, for each field found.
    """
    resolved = {}
    if not isinstance(first, dict):
        return resolved
    for canonical, aliases in FIELD_ALIASES.items():
        for name in aliases:
            if name in first:
                resolved[canonical] = name
                break
    return resolved


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

    Field spellings are resolved once from the first entry via FIELD_ALIASES, so
    the integrations from discussion #214 (start_time/end_time/price_per_kwh and
    friends) are read directly, without a renaming template.
    """
    if not isinstance(raw_entries, list):
        raise TimeseriesFormatError(
            f"expected a JSON array of timeseries entries, got "
            f"{type(raw_entries).__name__}"
        )
    if not raw_entries:
        raise TimeseriesFormatError("timeseries is empty")

    names = resolve_field_names(raw_entries[0])
    if "start" not in names and "end" not in names:
        raise TimeseriesFormatError(
            f"missing required field 'start' in the first entry "
            f"({_describe_available_keys(raw_entries[0])}). Accepted names: "
            f"{', '.join(FIELD_ALIASES['start'])} — or, for an hour-ending "
            f"source, end timestamps only ({', '.join(FIELD_ALIASES['end'])}). "
            f"The expected format is {{start, end, value}} — shape the source "
            f"with a Home Assistant template sensor, see {TEMPLATE_DOCS_URL}"
        )
    if "value" not in names:
        raise TimeseriesFormatError(
            f"missing required field 'value' in the first entry "
            f"({_describe_available_keys(raw_entries[0])}). Accepted names: "
            f"{', '.join(FIELD_ALIASES['value'])}. The expected format is "
            f"{{start, end, value}} — shape the source with a Home Assistant "
            f"template sensor, see {TEMPLATE_DOCS_URL}"
        )
    start_key = names.get("start")
    value_key = names["value"]
    # `end` is optional per entry, so its spelling may first appear anywhere in
    # the payload — a hand-written profile declares it only on the one entry
    # that needs a longer span.
    end_key = names.get("end")
    if end_key is None:
        for raw in raw_entries:
            if isinstance(raw, dict):
                end_key = next(
                    (name for name in FIELD_ALIASES["end"] if name in raw), None
                )
                if end_key:
                    break
    aliased = {
        canonical: key
        for canonical, key in (
            ("start", start_key), ("end", end_key), ("value", value_key)
        )
        if key is not None and canonical != key
    }
    if aliased:
        logger.info(
            "[%s] timeseries fields read as %s",
            label,
            ", ".join(f"{c}={k!r}" for c, k in sorted(aliased.items())),
        )

    entries = []
    naive_seen = 0
    for index, raw in enumerate(raw_entries):
        # Every entry has to parse. Dropping the bad ones would be friendlier
        # right up until it corrupts the result: under timestamp placement a
        # dropped entry becomes a hole, and a hole in a price series is filled
        # by policy — a null at 18:00 would silently become the 17:00 price,
        # exactly where the price matters most. Quieter than a shift, not
        # better. Rejecting the payload keeps the fallback chain intact: the
        # caller serves the last good data and the log names the bad entry.
        anchor_key = start_key or end_key
        if not isinstance(raw, dict) or anchor_key not in raw or value_key not in raw:
            raise TimeseriesFormatError(
                f"entry {index} is missing '{anchor_key}' or '{value_key}' "
                f"({_describe_available_keys(raw)})"
            )
        start = None
        if start_key is not None:
            try:
                start, was_naive = _parse_timestamp(raw[start_key], tz)
            except TimeseriesFormatError as exc:
                raise TimeseriesFormatError(f"entry {index}: {exc}") from exc
            if was_naive:
                naive_seen += 1
        end = None
        if end_key is not None and raw.get(end_key) is not None:
            try:
                end, end_was_naive = _parse_timestamp(
                    raw[end_key], tz, field=end_key
                )
            except TimeseriesFormatError as exc:
                raise TimeseriesFormatError(f"entry {index}: {exc}") from exc
            if end_was_naive:
                naive_seen += 1
        if start is not None and end is not None and end <= start:
            raise TimeseriesFormatError(
                f"entry {index} ends at or before it starts "
                f"({raw[end_key]!r} <= {raw[start_key]!r})"
            )
        try:
            value = float(raw[value_key])
        except (TypeError, ValueError) as exc:
            raise TimeseriesFormatError(
                f"entry {index} has a non-numeric '{value_key}': {raw[value_key]!r}"
            ) from exc
        entries.append({"start": start, "end": end, "value": value})

    if naive_seen:
        logger.warning(
            "[%s] %d timeseries timestamp(s) carry no UTC offset and were read as "
            "local time (%s). If the source renders UTC (e.g. a Home Assistant "
            "template using '| timestamp_utc'), every slot is shifted — emit ISO8601 "
            "with offset instead, e.g. '.isoformat()'. See %s",
            label,
            naive_seen,
            getattr(tz, "zone", str(tz)),
            TEMPLATE_DOCS_URL,
        )

    if start_key is None:
        _derive_starts_from_ends(entries)
    entries.sort(key=lambda item: item["start"])
    for earlier, later in zip(entries, entries[1:]):
        if later["start"] == earlier["start"]:
            # A duplicate is always a template bug. The consumers used to
            # disagree on it (last-wins, double-count, reject) — silently
            # keeping either value is how a wrong forecast survives.
            raise TimeseriesFormatError(
                f"two entries share the start timestamp "
                f"{later['start'].isoformat()}"
            )
    _finalize_entry_spans(entries)
    return entries


def _nominal_span_seconds(points):
    """
    The typical spacing of a sorted boundary sequence, in seconds.

    The two grids the optimizer itself runs on are recognised from the first
    delta (mirroring detect_resolution_seconds); any other regular grid falls
    back to the median spacing, which shrugs off isolated holes. None when
    there are not two distinct points.
    """
    deltas = [
        (later - earlier).total_seconds()
        for earlier, later in zip(points, points[1:])
        if later > earlier
    ]
    if not deltas:
        return None
    first = int(deltas[0])
    if first in (900, 3600):
        return first
    return int(_median(deltas))


def _finalize_entry_spans(entries):
    """
    Fill each missing ``end``, capped at the series' nominal spacing, in place.

    A source-provided ``end`` is authoritative — it is how a deliberately long
    entry (one overnight value in a hand-written load profile) is expressed.
    A derived end reaches at most to the next entry's start AND at most one
    nominal spacing past its own start: stretching it to "the next start,
    whatever the distance" would smear the entry before a hole across the
    hole, and a hole the grid placement cannot see can be neither reported,
    nor filled by policy, nor marked as forecast. Derived ends carry an
    ``end_derived`` flag so the probe can say which spans the source declared.

    The last entry gets one nominal spacing (one hour for a single-entry
    series).
    """
    nominal = _nominal_span_seconds([entry["start"] for entry in entries])
    if nominal is None:
        first_end = entries[0].get("end")
        if first_end is not None:
            nominal = (first_end - entries[0]["start"]).total_seconds()
        else:
            nominal = 3600
    span_cap = timedelta(seconds=nominal)

    count = len(entries)
    for index, entry in enumerate(entries):
        if entry.get("end") is not None:
            continue
        candidate = entry["start"] + span_cap
        if index + 1 < count:
            candidate = min(candidate, entries[index + 1]["start"])
        entry["end"] = candidate
        entry["end_derived"] = True


def _derive_starts_from_ends(entries):
    """
    Derive each ``start`` for an end-only ("hour ending") series, in place.

    The mirror of the end derivation: sorted by ``end``, an entry begins at
    the previous end, but never more than one nominal spacing before its own
    end — so a hole between ends stays a hole instead of inflating the entry
    after it. Derived starts carry a ``start_derived`` flag.
    """
    entries.sort(key=lambda item: item["end"])
    for earlier, later in zip(entries, entries[1:]):
        if later["end"] == earlier["end"]:
            raise TimeseriesFormatError(
                f"two entries share the end timestamp {later['end'].isoformat()}"
            )
    nominal = _nominal_span_seconds([entry["end"] for entry in entries]) or 3600
    span_cap = timedelta(seconds=nominal)

    previous_end = None
    for entry in entries:
        candidate = entry["end"] - span_cap
        if previous_end is not None and previous_end > candidate:
            candidate = previous_end
        entry["start"] = candidate
        entry["start_derived"] = True
        previous_end = entry["end"]


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


def convert_energy_values(entries, unit):
    """
    Convert PV or managed-load entries to Wh per entry in place, and return them.

    Power units (W, kW) are integrated over each entry's *own* span rather than
    one resolution for the whole series — a half-hourly grid, one longer entry
    overnight, or a mixed-resolution concatenation would all be silently scaled
    wrong by a single slot length. Energy units pass through scaled.

    Every entry is expected to carry ``end``; `normalize_entries` guarantees that by
    deriving it from the following entry where the source omitted it.

    Args:
        entries: Normalized entries whose ``value`` is in *unit*.
        unit: One of PV_UNITS.

    Raises:
        TimeseriesFormatError: On an unknown unit.
    """
    try:
        spec = PV_UNITS[unit]
    except KeyError as exc:
        raise TimeseriesFormatError(
            f"unknown unit {unit!r}, expected one of {', '.join(sorted(PV_UNITS))}"
        ) from exc

    for entry in entries:
        factor = spec["factor"]
        if spec["is_power"]:
            span = entry.get("end")
            hours = (
                (span - entry["start"]).total_seconds() / 3600.0
                if span is not None else 1.0
            )
            factor *= hours
        entry["value"] = entry["value"] * factor
    return entries


def median_span_seconds(entries):
    """
    The typical length of one entry, for a series whose grid is not 900 or 3600.

    `detect_resolution_seconds` answers only for the two resolutions the optimizer
    itself runs on. A source is free to publish on any grid it likes, so this is what
    tells the caller how long an entry covers when that answer is None.
    """
    spans = [
        (entry["end"] - entry["start"]).total_seconds()
        for entry in entries
        if entry.get("end") is not None
    ]
    if not spans:
        return None
    return int(_median(spans))


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


def load_plausibility_message(values_wh, unit, resolution_seconds, rated_power_w=0):
    """
    Check converted load energies against what the appliance can actually draw.

    The mistake this has to catch is the same one PV has: a source reporting watts
    read as watt-hours per slot, which is a factor of four on quarter-hourly data -
    too small to look like anything but a pessimistic forecast. Comparing the peak
    slot against a declared rated power catches it; without one, only the factor-1000
    mistakes (kW entered as W) are visible.

    Only the high direction is checked. A series that is too *low* is
    indistinguishable from an appliance that is legitimately off, or from a small
    downward correction - both of which are ordinary, and warning about them would
    train the user to ignore the message.

    Args:
        values_wh: Converted energy per slot, in Wh. May contain negatives.
        unit: The configured unit, named in the message.
        resolution_seconds: Slot length the values belong to.
        rated_power_w: The load's configured rated power, or 0 when not declared.

    Returns:
        str: A user-facing warning, or None when the values look plausible.
    """
    if not values_wh or not resolution_seconds:
        return None

    slot_hours = resolution_seconds / 3600.0
    if slot_hours <= 0:
        return None

    # Absolute, because a correction profile is legitimately negative and a unit
    # mistake in one is just as wrong as in a positive one.
    peak_power_w = max(abs(value) for value in values_wh) / slot_hours

    if rated_power_w and rated_power_w > 0:
        ceiling = rated_power_w * LOAD_PEAK_TOLERANCE
        reference = f"but it is rated for {rated_power_w / 1000:.1f} kW"
    else:
        ceiling = LOAD_PLAUSIBLE_PEAK_W
        reference = (
            f"which is above the {LOAD_PLAUSIBLE_PEAK_W / 1000:.0f} kW assumed for a "
            f"household appliance"
        )

    if peak_power_w <= ceiling:
        return None

    return (
        f"implausible load level: peak {peak_power_w / 1000:.1f} kW for unit "
        f"'{unit}', {reference}. If the source reports power rather than energy per "
        f"slot, set the unit to 'W' or 'kW'."
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
