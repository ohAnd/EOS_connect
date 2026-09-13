"""
Turning a pushed payload into a slot-aligned energy series.

This is the hand-over channel issue #55 asked for: an external system that already knows
something EOS Connect cannot - a heat pump's cooling mode, a heating curve against the
outdoor temperature forecast, a washing day - pushes the resulting energy over HTTP or
MQTT, and it is added to the household load forecast.

Callers should not have to know how EOS Connect is configured, so the parser accepts
whatever shape is natural at the sending end and normalises it here:

- ``{"value_wh": 1500}`` - a constant hourly average, the form proposed on the issue.
- ``{"values": [...]}`` - 24 or 48 hourly values, or 96 or 192 quarter-hourly ones. The
  resolution is inferred from the length and resampled to whatever the optimizer is
  currently running on, so a pusher does not break when ``eos.time_frame`` changes.
- ``{"total_wh": 8000, "deadline_hours": 24}`` - an energy contingent to be placed by the
  planner rather than a fixed profile.

A load can also be pointed at a source EOS Connect fetches itself, which arrives here as
already-normalised timestamped entries - see `profile_from_entries`. Both paths end in
the same `PushedProfile`, so everything downstream treats a pulled forecast and a pushed
one identically.

Everything is validated before it can reach the optimizer: the payload arrives from the
network and a malformed array must produce a 400, never a corrupted ``gesamtlast``.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime

from .contribution import MAX_SLOT_WH, ttl_from_minutes

logger = logging.getLogger("__main__")

# Series lengths we can interpret without being told, mapped to the seconds per slot
# they imply. 24/48 are the hourly day and two-day forms, 96/192 their quarter-hourly
# equivalents - the "24 h or 96-slot array" the issue thread converged on.
_INFERRED_RESOLUTION = {
    24: 3600,
    48: 3600,
    96: 900,
    192: 900,
}

# Explicit ``resolution`` values, for series whose length is not self-describing.
_NAMED_RESOLUTION = {
    "hourly": 3600,
    "60min": 3600,
    "3600": 3600,
    "15min": 900,
    "quarter": 900,
    "900": 900,
}

_UNITS = {"wh": "Wh", "w": "W"}

# A pushed array longer than this is a mistake, not a very long forecast.
_MAX_SERIES_LENGTH = 2000


class InjectionError(ValueError):
    """A pushed payload could not be interpreted. The message is user-facing."""


@dataclass
class PushedProfile:
    """A fixed per-slot series, ready to become a `LoadContribution`."""

    slots_wh: list
    valid_until: datetime
    source_resolution_s: int
    source_length: int


@dataclass
class PushedContingent:
    """An energy budget for the planner to place."""

    total_wh: float
    deadline_slot: object
    valid_until: datetime


def _as_number(value, label):
    """Coerce one scalar, rejecting the values that would poison the forecast."""
    if isinstance(value, bool) or value is None:
        raise InjectionError(f"'{label}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InjectionError(f"'{label}' must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise InjectionError(f"'{label}' must be finite, got {value!r}")
    return number


def resample(values, source_base, target_base):
    """
    Convert an energy series between slot resolutions, conserving total energy.

    Splitting divides each value evenly - a pusher that knows only the hourly total has
    no better information about its distribution inside the hour, and pretending
    otherwise would invent a shape. Aggregating sums, which is exact.
    """
    if source_base == target_base:
        return list(values)

    if source_base > target_base:
        if source_base % target_base:
            raise InjectionError(
                f"cannot split {source_base}s slots into {target_base}s slots"
            )
        factor = source_base // target_base
        out = []
        for value in values:
            out.extend([value / factor] * factor)
        return out

    if target_base % source_base:
        raise InjectionError(
            f"cannot combine {source_base}s slots into {target_base}s slots"
        )
    factor = target_base // source_base
    return [sum(values[i:i + factor]) for i in range(0, len(values), factor)]


def _resolve_resolution(payload, length):
    """Pick the source slot length, from the explicit hint or from the series length."""
    raw = str(payload.get("resolution", "auto")).strip().lower()
    if raw and raw != "auto":
        if raw not in _NAMED_RESOLUTION:
            raise InjectionError(
                f"unknown resolution {raw!r} - use one of: auto, hourly, 15min"
            )
        return _NAMED_RESOLUTION[raw]

    if length in _INFERRED_RESOLUTION:
        return _INFERRED_RESOLUTION[length]
    raise InjectionError(
        f"cannot infer the resolution of a {length}-value series - send 24, 48, 96 or "
        "192 values, or set 'resolution' explicitly"
    )


def _resolve_unit(payload):
    raw = str(payload.get("unit", "Wh")).strip().lower()
    if raw not in _UNITS:
        raise InjectionError(f"unknown unit {payload.get('unit')!r} - use 'Wh' or 'W'")
    return _UNITS[raw]


def _resolve_start_slot(payload, anchor, current_slot, time_frame_base):
    """
    Where the caller's first value belongs, as a slot index relative to *anchor*.

    ``today`` is the default because it matches how ``gesamtlast`` itself is indexed;
    ``now`` is what a caller sending "the next 24 hours" means; an ISO timestamp covers
    everything else.
    """
    raw = payload.get("start", "today")
    if raw is None:
        return 0
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("", "today", "midnight"):
            return 0
        if text == "now":
            return current_slot
        try:
            moment = datetime.fromisoformat(raw.strip())
        except ValueError as exc:
            raise InjectionError(
                f"'start' must be 'today', 'now' or an ISO timestamp, got {raw!r}"
            ) from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=anchor.tzinfo)
        offset = (moment - anchor).total_seconds() / time_frame_base
        return int(round(offset))
    raise InjectionError(f"'start' must be a string, got {raw!r}")


def _place(values, start_slot, slot_count):
    """
    Lay a series into a ``slot_count`` window at *start_slot*, zero elsewhere.

    Values falling outside the window are dropped and counted, so the caller can be told
    that half of what they sent will not be used - silently discarding it is how an
    alignment bug survives for weeks.
    """
    placed = [0.0] * slot_count
    dropped = 0
    for index, value in enumerate(values):
        target = start_slot + index
        if 0 <= target < slot_count:
            placed[target] += value
        else:
            dropped += 1
    return placed, dropped


def parse_push(payload, anchor, slot_count, time_frame_base, current_slot,
               default_ttl_minutes=1440, now=None):
    """
    Interpret a pushed payload.

    Returns a `PushedProfile` or a `PushedContingent`. Raises `InjectionError` with a
    message meant for the caller - it is rendered straight into the HTTP 400 body and
    logged for MQTT pushes, which have nowhere to return an error to.
    """
    if payload is None:
        raise InjectionError("empty payload")

    # A bare number over MQTT is the most convenient thing to publish from a rule or an
    # automation, so accept it as the hourly-average form.
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        payload = {"value_wh": payload}

    if not isinstance(payload, dict):
        raise InjectionError(f"expected a JSON object or a number, got {type(payload).__name__}")

    valid_until = ttl_from_minutes(payload.get("ttl_minutes", default_ttl_minutes), now=now)

    if "total_wh" in payload:
        return _parse_contingent(payload, slot_count, time_frame_base, current_slot, valid_until)

    return _parse_profile(
        payload, anchor, slot_count, time_frame_base, current_slot, valid_until
    )


def _parse_contingent(payload, slot_count, time_frame_base, current_slot, valid_until):
    """The "I need X Wh by T" form - the planner decides where it goes."""
    total = _as_number(payload["total_wh"], "total_wh")
    if total < 0:
        raise InjectionError("'total_wh' cannot be negative")

    deadline_slot = None
    if payload.get("deadline_hours") is not None:
        hours = _as_number(payload["deadline_hours"], "deadline_hours")
        if hours <= 0:
            raise InjectionError("'deadline_hours' must be positive")
        deadline_slot = min(
            slot_count - 1,
            current_slot + int(round(hours * 3600 / time_frame_base)),
        )

    return PushedContingent(
        total_wh=total, deadline_slot=deadline_slot, valid_until=valid_until
    )


def _parse_profile(payload, anchor, slot_count, time_frame_base, current_slot, valid_until):
    """The fixed-series form, including the constant shorthands."""
    unit = _resolve_unit(payload)

    if "values" in payload:
        raw = payload["values"]
        if not isinstance(raw, (list, tuple)):
            raise InjectionError("'values' must be an array")
        if not raw:
            raise InjectionError("'values' must not be empty")
        if len(raw) > _MAX_SERIES_LENGTH:
            raise InjectionError(
                f"'values' has {len(raw)} entries - at most {_MAX_SERIES_LENGTH} accepted"
            )
        numbers = [_as_number(v, f"values[{i}]") for i, v in enumerate(raw)]
        source_base = _resolve_resolution(payload, len(numbers))

    elif "value_wh" in payload or "value_w" in payload:
        # The constant form covers the whole horizon: "for this day (and next day)",
        # as proposed on the issue.
        key = "value_wh" if "value_wh" in payload else "value_w"
        constant = _as_number(payload[key], key)
        source_base = 3600
        hours = math.ceil(slot_count * time_frame_base / 3600)
        numbers = [constant] * hours
        unit = "Wh" if key == "value_wh" else "W"

    else:
        raise InjectionError(
            "payload must contain 'values', 'value_wh', 'value_w' or 'total_wh'"
        )

    # Average power over a slot becomes energy in that slot before any resampling -
    # doing it afterwards would scale by the wrong slot length.
    if unit == "W":
        numbers = [value * source_base / 3600.0 for value in numbers]

    for value in numbers:
        if abs(value) > MAX_SLOT_WH:
            raise InjectionError(
                f"a slot value of {value:.0f} Wh is outside the plausible range "
                f"(+/-{MAX_SLOT_WH:.0f} Wh) - check the unit"
            )

    resampled = resample(numbers, source_base, time_frame_base)
    start_slot = _resolve_start_slot(payload, anchor, current_slot, time_frame_base)
    placed, dropped = _place(resampled, start_slot, slot_count)

    if dropped:
        logger.warning(
            "[LOADS] %d of %d pushed slots fall outside the %d-slot optimization "
            "horizon and were ignored - check 'start' and the series length",
            dropped,
            len(resampled),
            slot_count,
        )

    return PushedProfile(
        slots_wh=placed,
        valid_until=valid_until,
        source_resolution_s=source_base,
        source_length=len(numbers),
    )


def profile_from_entries(entries, resolution_seconds, anchor, slot_count,
                        time_frame_base, valid_until):
    """
    Lay a timestamped series onto the optimizer's slot grid.

    This is the pull counterpart to `parse_push`. Where a push sends a bare array and
    says where it starts, a pulled series carries a real timestamp per entry - which is
    what makes it safe across midnight and across a DST change, where "the 24th value"
    and "the hour at 02:00" stop meaning the same thing.

    Each entry's energy is spread over the target slots its span actually covers,
    weighted by overlap. That gets both directions right without a separate resampling
    step: a finer source simply lands several entries in one slot and they sum, and a
    coarser one is divided evenly across the slots it spans - the same rule `resample`
    applies to a push, because a source that knows only the hourly total has no better
    information about the shape inside that hour either way.

    Args:
        entries: Normalized entries, oldest first, each ``{"start": datetime, "value":
            float}`` with an optional ``"end"``. Values are energy in Wh per entry.
        resolution_seconds: Span of one entry, used when ``end`` is absent.
        anchor: Local midnight that slot 0 starts at, timezone-aware.
        slot_count: Length of the series to produce.
        time_frame_base: Seconds per target slot.
        valid_until: When the result stops counting.

    Returns:
        PushedProfile: ready to be handed to `ExternalPushModel.accept`.

    Raises:
        InjectionError: with a message written for the user.
    """
    if not entries:
        raise InjectionError("the source returned no values")

    placed = [0.0] * slot_count
    horizon_seconds = slot_count * time_frame_base
    dropped = 0

    for index, entry in enumerate(entries):
        start = entry.get("start")
        if start is None:
            raise InjectionError(f"entry {index} has no 'start'")
        value = _as_number(entry.get("value"), f"entries[{index}].value")

        begin = (start - anchor).total_seconds()
        end_stamp = entry.get("end")
        finish = (
            (end_stamp - anchor).total_seconds() if end_stamp is not None
            else begin + resolution_seconds
        )
        span = finish - begin
        if span <= 0:
            raise InjectionError(
                f"entry {index} ends at or before it starts - check the source's "
                "timestamps"
            )

        if finish <= 0 or begin >= horizon_seconds:
            dropped += 1
            continue

        _spread(placed, value, begin, finish, span, time_frame_base, slot_count)

    if dropped:
        # The same accounting a push gets. A source publishing yesterday as well as
        # today is normal and its past entries are meant to fall away - but so is a
        # source stuck a day behind, and only the count tells those apart.
        logger.info(
            "[LOADS] %d of %d pulled entries fall outside the %d-slot horizon and were "
            "ignored", dropped, len(entries), slot_count,
        )

    for value in placed:
        if abs(value) > MAX_SLOT_WH:
            raise InjectionError(
                f"a slot value of {value:.0f} Wh is outside the plausible range "
                f"(+/-{MAX_SLOT_WH:.0f} Wh) - check the unit"
            )

    return PushedProfile(
        slots_wh=placed,
        valid_until=valid_until,
        source_resolution_s=int(resolution_seconds),
        source_length=len(entries),
    )


def _spread(placed, value, begin, finish, span, time_frame_base, slot_count):
    """
    Add one entry's energy to every slot its span touches, weighted by overlap.

    Splitting by overlap rather than by slot index is what keeps a source whose
    timestamps do not line up with the slot boundaries from silently losing energy at
    each edge.
    """
    first = max(0, int(begin // time_frame_base))
    last = min(slot_count - 1, int((finish - 1e-9) // time_frame_base))

    for slot in range(first, last + 1):
        slot_begin = slot * time_frame_base
        slot_end = slot_begin + time_frame_base
        overlap = min(finish, slot_end) - max(begin, slot_begin)
        if overlap > 0:
            placed[slot] += value * (overlap / span)


def describe(profile, time_frame_base):
    """Echo shown back to the caller so they can verify alignment without guessing."""
    return {
        "slots": len(profile.slots_wh),
        "time_frame_base": time_frame_base,
        "source_resolution_s": profile.source_resolution_s,
        "source_length": profile.source_length,
        "total_wh": round(sum(profile.slots_wh), 1),
        "valid_until": profile.valid_until.isoformat(),
        "values": [round(value, 2) for value in profile.slots_wh],
    }
