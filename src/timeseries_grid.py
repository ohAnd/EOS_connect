"""
Placing timestamped series onto the optimizer's slot grid.

Five consumers used to lay the same payload format onto the forecast grid with
three different hand-written procedures, and the positional ones are how a
series that starts yesterday ends up served as today (discussion #214). This
module is the one placement they share; only the meaning of a value stays
domain-specific, expressed as the *kind*:

- ``KIND_EXTENSIVE`` — energy per entry (Wh): a coarse entry is divided across
  the slots it spans, overlapping contributions add up. Energy is conserved in
  both directions.
- ``KIND_INTENSIVE`` — a rate (EUR/Wh, °C): slots average what covers them,
  weighted by overlap, so a coarse entry simply holds its value across every
  sub-slot.

The module lives at ``src/`` level because ``src/loads/`` must not import from
``src/interfaces/`` — placement is the one piece both sides share. It never
calls ``datetime.now()``: the anchor comes from the caller, which is what makes
a DST day or a fetch running across midnight well-defined, and what lets tests
pass an explicit anchor instead of patching the clock.

Gap policy deliberately does NOT live here. The callers genuinely differ — PV
and load want 0 for an uncovered slot, prices fill by rule and must report the
first filled slot to the UI — so `place_on_grid` reports per-slot ``coverage``
and decides nothing.
"""

import math
from dataclasses import dataclass
from datetime import datetime

KIND_EXTENSIVE = "extensive"
KIND_INTENSIVE = "intensive"

#: The optimization horizon in hours. 48 h of 3600 s slots — the EVopt backend,
#: the EOS request builder and the chart all tile exactly this window.
HORIZON_HOURS = 48


class TimeseriesGridError(ValueError):
    """
    An entry that cannot be placed. The message is written for the user and
    names the entry by index; callers may re-wrap it in their own error type
    but should keep the text.
    """


@dataclass
class GridPlacement:
    """What landed where — and how much of each slot the source actually covered."""

    values: list  # slot_count floats; uncovered slots are 0.0
    coverage: list  # slot_count fractions in [0.0, 1.0]
    dropped: int  # entries entirely outside the horizon
    source_count: int  # entries offered by the source


def horizon_slots(time_frame_base, hours=HORIZON_HOURS):
    """Slots in the optimization horizon: 48 hourly, 192 quarter-hourly."""
    return int(hours * 3600) // int(time_frame_base)


def local_midnight(tz, now=None):
    """
    True local midnight of *now*'s day in *tz* — slot 0 of the forecast grid.

    ``now.replace(hour=0, ...)`` is NOT this: on a pytz datetime it keeps
    *now's* UTC offset, so on a DST day the "midnight" it produces is an hour
    off, and ``(now - anchor)`` collapses back to the wall clock. Localizing
    the date fresh attaches midnight's own offset.
    """
    if now is None:
        now = datetime.now(tz)
    day = now.astimezone(tz)
    naive = datetime(day.year, day.month, day.day)
    if hasattr(tz, "localize"):
        return tz.localize(naive)
    return naive.replace(tzinfo=tz)


def place_on_grid(entries, anchor, slot_count, slot_seconds, kind):
    """
    Lay timestamped entries onto the slot grid by overlap.

    Args:
        entries: ``[{"start": datetime, "end": datetime, "value": number}]``,
            timezone-aware, ``end`` present on every entry (the normalizer
            guarantees that; `profile_from_entries` fills it from the declared
            resolution).
        anchor: The aware datetime slot 0 starts at — local midnight today.
        slot_count: Slots to produce.
        slot_seconds: Length of one slot.
        kind: KIND_EXTENSIVE or KIND_INTENSIVE.

    Returns:
        GridPlacement. ``coverage[i]`` is the fraction of slot i covered by
        source entries — the first uncovered slot is what a price caller
        reports as ``forecast_start_index``, so this function must never paper
        over a hole.

    Raises:
        TimeseriesGridError: An entry without timestamps, a non-numeric or
            non-finite value, or a span of zero or less.
    """
    if kind not in (KIND_EXTENSIVE, KIND_INTENSIVE):
        raise ValueError(f"unknown placement kind {kind!r}")

    values = [0.0] * slot_count
    covered_seconds = [0.0] * slot_count
    horizon_seconds = slot_count * slot_seconds
    dropped = 0

    for index, entry in enumerate(entries):
        start = entry.get("start")
        if start is None:
            raise TimeseriesGridError(f"entry {index} has no 'start'")
        end = entry.get("end")
        if end is None:
            raise TimeseriesGridError(f"entry {index} has no 'end'")
        value = _as_number(entry.get("value"), f"entries[{index}].value")

        begin = (start - anchor).total_seconds()
        finish = (end - anchor).total_seconds()
        span = finish - begin
        if span <= 0:
            raise TimeseriesGridError(
                f"entry {index} ends at or before it starts - check the source's "
                "timestamps"
            )

        if finish <= 0 or begin >= horizon_seconds:
            dropped += 1
            continue

        first = max(0, int(begin // slot_seconds))
        last = min(slot_count - 1, int((finish - 1e-9) // slot_seconds))
        for slot in range(first, last + 1):
            slot_begin = slot * slot_seconds
            overlap = min(finish, slot_begin + slot_seconds) - max(begin, slot_begin)
            if overlap <= 0:
                continue
            covered_seconds[slot] += overlap
            if kind == KIND_EXTENSIVE:
                values[slot] += value * (overlap / span)
            else:
                values[slot] += value * overlap

    if kind == KIND_INTENSIVE:
        values = [
            total / seconds if seconds > 0 else 0.0
            for total, seconds in zip(values, covered_seconds)
        ]

    coverage = [
        min(1.0, seconds / slot_seconds) for seconds in covered_seconds
    ]
    return GridPlacement(
        values=values,
        coverage=coverage,
        dropped=dropped,
        source_count=len(entries),
    )


def clamp_report(values, lower, upper):
    """
    Clamp a series into [lower, upper] and report the damage in one piece.

    One aggregated warning instead of one log line per slot: a payload with a
    wrong unit produces 192 out-of-range values, and 192 WARNING records evict
    real alerts from the UI's bounded history.

    Returns:
        (clamped_values, clamped_count, worst_offender) — worst_offender is the
        original value farthest outside the band, or None when nothing clamped.
    """
    clamped = []
    count = 0
    worst = None

    def _distance(number):
        if number < lower:
            return lower - number
        return number - upper

    for value in values:
        if value < lower or value > upper:
            count += 1
            if worst is None or _distance(value) > _distance(worst):
                worst = value
            value = max(lower, min(upper, value))
        clamped.append(value)
    return clamped, count, worst


def _as_number(value, label):
    """Coerce one scalar, rejecting the values that would poison the forecast."""
    if isinstance(value, bool) or value is None:
        raise TimeseriesGridError(f"'{label}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TimeseriesGridError(f"'{label}' must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise TimeseriesGridError(f"'{label}' must be finite, got {value!r}")
    return number
