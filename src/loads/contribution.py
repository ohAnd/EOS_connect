"""
The registry every managed load publishes into, and the record it publishes.

A contribution is a per-slot energy series aligned to the same grid as ``gesamtlast``:
one value per ``time_frame_base`` seconds, index 0 at local midnight of ``anchor``.
Keeping the anchor with the data - rather than assuming "index 0 is today" - is what
lets a contribution survive midnight: at 00:05 the next day the stored array is simply
read from an offset, and the slots that ran off the end are reported as zero instead of
silently reusing yesterday's numbers.
"""

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("__main__")
logger.info("[LOADS] loading module ")

# Per-slot sanity ceiling. `LoadInterface.get_load_profile_for_day` rejects historic
# samples above 100 kWh in a slot for the same reason: a sensor glitch or a unit mix-up
# (kW pushed into a W field) must not reach the optimizer as a real demand.
MAX_SLOT_WH = 100000.0

# Kinds of contribution. See the package docstring.
KIND_CONTINGENT = "contingent"
KIND_PROFILE = "profile"

# Where a contribution came from, for display and debugging only.
SOURCE_INTERNAL = "internal"
SOURCE_API = "api"
SOURCE_MQTT = "mqtt"


def _utcnow():
    """Aware UTC now, in one place so tests can reason about it."""
    return datetime.now(timezone.utc)


def _sanitize(values):
    """
    Coerce a raw series into finite floats within the sanity ceiling.

    Non-numeric, NaN and infinite entries become 0.0 rather than raising: a single bad
    slot in a 192-value push should not throw away the other 191. Values are clamped
    symmetrically because a contribution may legitimately be negative - that is how a
    caller corrects the base load *downwards*, which is exactly issue #55's
    cooling-versus-heating delta.
    """
    cleaned = []
    for raw in values:
        try:
            number = float(raw)
        except (TypeError, ValueError):
            cleaned.append(0.0)
            continue
        if not math.isfinite(number):
            cleaned.append(0.0)
            continue
        cleaned.append(max(-MAX_SLOT_WH, min(MAX_SLOT_WH, number)))
    return cleaned


def align_series(values, from_anchor, to_anchor, time_frame_base, slot_count):
    """
    Read a slot series indexed from *from_anchor* as if it were indexed from *to_anchor*.

    Returns ``slot_count`` values. When *to_anchor* is later than *from_anchor* - the
    normal case once the day rolls over - the series is read from the corresponding
    offset and short-padded with zeros. An earlier anchor, which only happens if the
    clock moves backwards, is left-padded the same way. Nothing is extrapolated: a
    horizon we have no data for contributes nothing, which is the honest answer, and it
    is what makes a 48 h push degrade gracefully into its second day rather than
    silently repeating its first.
    """
    offset = int(round((to_anchor - from_anchor).total_seconds() / time_frame_base))
    window = []
    for index in range(offset, offset + slot_count):
        if 0 <= index < len(values):
            window.append(values[index])
        else:
            window.append(0.0)
    return window


@dataclass
class LoadContribution:
    """
    One named contributor's energy series over the optimization horizon.

    Attributes:
        id: Instance id. Slug-safe - it becomes an MQTT topic segment and a URL path.
        slots_wh: Energy per slot in Wh. May contain negative values.
        anchor: Local midnight that ``slots_wh[0]`` starts at, timezone-aware.
        time_frame_base: Seconds per slot the series was built for (900 or 3600). A
            contribution built for a different resolution is ignored rather than
            silently misread - the two are not interchangeable without resampling.
        valid_until: After this instant the contribution stops being counted.
        kind: ``contingent`` or ``profile``; carried for display, not for arithmetic.
        source: ``internal``, ``api`` or ``mqtt``.
        updated_at: When this record was last written.
    """

    id: str
    slots_wh: list
    anchor: datetime
    time_frame_base: int
    valid_until: datetime
    kind: str = KIND_PROFILE
    source: str = SOURCE_INTERNAL
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self):
        self.slots_wh = _sanitize(self.slots_wh)
        if self.anchor.tzinfo is None:
            raise ValueError("LoadContribution.anchor must be timezone-aware")
        if self.valid_until.tzinfo is None:
            raise ValueError("LoadContribution.valid_until must be timezone-aware")
        if self.time_frame_base <= 0:
            raise ValueError("LoadContribution.time_frame_base must be positive")

    def is_valid(self, now=None):
        """True while the contribution has not expired."""
        return (now or _utcnow()) < self.valid_until

    def total_wh(self):
        """Sum of the whole series - what the dashboard and MQTT report."""
        return sum(self.slots_wh)

    def aligned_to(self, anchor, slot_count):
        """Re-read the series against a (possibly newer) anchor. See `align_series`."""
        return align_series(
            self.slots_wh, self.anchor, anchor, self.time_frame_base, slot_count
        )


class LoadContributionRegistry:
    """
    Thread-safe store of the current contribution per id.

    Writers are the managed-load poll thread, the Flask request thread and the MQTT
    network thread; the reader is the optimizer request builder. Every one of those is
    a different thread, so the lock is not optional.
    """

    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()
        # Ids already reported as expired, so a stale contribution logs once rather
        # than on every optimizer run.
        self._expiry_logged = set()

    def set(self, contribution):
        """Insert or replace the contribution for ``contribution.id``."""
        with self._lock:
            self._items[contribution.id] = contribution
            self._expiry_logged.discard(contribution.id)
        logger.debug(
            "[LOADS] contribution '%s' updated: %d slots, %.0f Wh total, valid until %s",
            contribution.id,
            len(contribution.slots_wh),
            contribution.total_wh(),
            contribution.valid_until.isoformat(),
        )

    def drop(self, contribution_id):
        """Remove one contribution. Returns True when something was removed."""
        with self._lock:
            removed = self._items.pop(contribution_id, None) is not None
            self._expiry_logged.discard(contribution_id)
        if removed:
            logger.debug("[LOADS] contribution '%s' dropped", contribution_id)
        return removed

    def get(self, contribution_id):
        """The stored contribution for an id, expired or not, or None."""
        with self._lock:
            return self._items.get(contribution_id)

    def active(self, now=None):
        """
        Every unexpired contribution, newest-expiring last.

        Expiry is reported once per id at INFO: a contribution that quietly stopped
        being counted is exactly the kind of thing a user needs told, because the
        optimizer result changes without any visible configuration change.
        """
        moment = now or _utcnow()
        with self._lock:
            live = []
            for key, item in self._items.items():
                if item.is_valid(moment):
                    live.append(item)
                elif key not in self._expiry_logged:
                    self._expiry_logged.add(key)
                    logger.info(
                        "[LOADS] contribution '%s' expired at %s and is no longer "
                        "added to the load forecast",
                        key,
                        item.valid_until.isoformat(),
                    )
        return sorted(live, key=lambda item: item.id)

    def total(self, anchor, slot_count, time_frame_base, now=None):
        """
        Sum every active contribution into one ``slot_count``-long series.

        Contributions built for a different slot resolution are skipped with a warning
        rather than reinterpreted: reading a 15-minute series as hourly would inflate
        the forecast fourfold, and the caller that pushed it can simply push again.
        """
        summed = [0.0] * slot_count
        for item in self.active(now):
            if item.time_frame_base != time_frame_base:
                logger.warning(
                    "[LOADS] contribution '%s' was built for %ds slots but the "
                    "optimizer now runs on %ds slots - ignoring it. Push it again to "
                    "have it counted.",
                    item.id,
                    item.time_frame_base,
                    time_frame_base,
                )
                continue
            for index, value in enumerate(item.aligned_to(anchor, slot_count)):
                summed[index] += value
        return summed

    def snapshot(self, now=None):
        """Serializable view for the REST API and the dashboard."""
        moment = now or _utcnow()
        with self._lock:
            items = list(self._items.values())
        return [
            {
                "id": item.id,
                "kind": item.kind,
                "source": item.source,
                "slots": len(item.slots_wh),
                "time_frame_base": item.time_frame_base,
                "total_wh": round(item.total_wh(), 1),
                "anchor": item.anchor.isoformat(),
                "valid_until": item.valid_until.isoformat(),
                "updated_at": item.updated_at.isoformat(),
                "active": item.is_valid(moment),
            }
            for item in sorted(items, key=lambda entry: entry.id)
        ]


def ttl_from_minutes(minutes, now=None):
    """Absolute expiry instant from a relative TTL, clamped to something sane."""
    moment = now or _utcnow()
    try:
        span = int(minutes)
    except (TypeError, ValueError):
        span = 0
    # A zero or negative TTL would expire the contribution before it is ever read.
    span = max(1, min(span, 7 * 24 * 60))
    return moment + timedelta(minutes=span)
