"""
The names an EVCC data source can read, and what they mean.

EVCC is addressed by *virtual entity* names that stand where a Home Assistant
entity id would: ``group:home`` for a whole metered group, ``group:consumer:Sauna``
for one series inside it, ``tariff:grid`` for the persisted price slots, and bare
keys like ``battery.soc`` or ``pvEnergy`` for its live state.

Four modules had to answer questions about those names - the load interface
before fetching, the merger before deciding a stored name is stale, the API
before choosing which connection to probe through, and the probe itself - and
each grew its own version. They drifted: two validated the group name, one
checked only the prefix, one additionally knew about ``sensor.``. This module is
the single answer, kept deliberately free of heavy imports so every one of them
can take it without pulling the interfaces' import graph along.

Two predicates, because two different questions are being asked:

``is_evcc_reference`` — "is this name EVCC's at all?" Shape only. A typo in the
group name still counts: the user clearly meant an EVCC series, and replacing
their typo with a default would hide it rather than fix it.

``is_evcc_history_entity`` — "can the history endpoint actually answer this?"
Validates the group and tariff names, which is what turns a typo into a message.
"""

from datetime import timezone

# Groups EVCC's energy history endpoint can answer with, verified against
# evcc-io/evcc core/metrics/collector.go. Each becomes a virtual entity
# "group:<name>" (whole group) or "group:<name>:<title>" (one series, e.g. one
# wallbox named by its loadpoint title).
EVCC_HISTORY_GROUPS = (
    "home",
    "grid",
    "pv",
    "battery",
    "loadpoint",
    "consumer",
    "meter",
    "forecast",
)

# Persisted tariff slots, "tariff:<name>". EVCC stores what its tariff source
# produced at the time - the price history the battery price calculation works
# from. Names are the usages EVCC persists (grid, feedin) per core/metrics/tariffs.go.
EVCC_TARIFF_NAMES = ("grid", "feedin")

_PREFIXES = ("group:", "tariff:")


def is_evcc_reference(name) -> bool:
    """Whether *name* is shaped like one of EVCC's own series.

    Prefix only - an unknown group name still answers True. The caller that
    fetches validates it properly; the callers that only need to tell an EVCC
    name from a Home Assistant entity id must not silently "correct" a typo.
    """
    if not name or not isinstance(name, str):
        return False
    return name.strip().startswith(_PREFIXES)


def is_evcc_history_entity(name) -> bool:
    """Whether *name* identifies a series the history endpoints can answer.

    ``group:<known>``, ``group:<known>:<title>`` or ``tariff:<known>``. Empty and
    None are the caller's business, handled the way a missing sensor name is.
    """
    if not name or not isinstance(name, str):
        return False
    parts = name.strip().split(":")
    if parts[0] == "tariff":
        return len(parts) == 2 and parts[1] in EVCC_TARIFF_NAMES
    if parts[0] != "group" or len(parts) not in (2, 3):
        return False
    return parts[1] in EVCC_HISTORY_GROUPS


def evcc_entity_parts(name):
    """Split a history reference into ``(group, title_or_None)``, or None.

    ``tariff:grid`` comes back as ``("tariff:grid", None)`` - the tariff slots
    are a separate endpoint, and carrying the whole prefix keeps the caller from
    having to re-derive which one it is.
    """
    if not is_evcc_history_entity(name):
        return None
    parts = str(name).strip().split(":")
    if parts[0] == "tariff":
        return f"tariff:{parts[1]}", None
    return parts[1], (parts[2] if len(parts) == 3 else None)


def is_evcc_state_key(name) -> bool:
    """Whether *name* addresses EVCC's live state document.

    ``battery.soc`` is dotted, ``pvEnergy`` is not - both are keys in the same
    document, so the dot cannot be the test. What it is *not* is a Home Assistant
    entity id, and ``sensor.`` is what makes one unmistakable.
    """
    if not name or not isinstance(name, str):
        return False
    stripped = name.strip()
    return bool(stripped) and ":" not in stripped and not stripped.startswith("sensor.")


def evcc_history_params(entity, start, end, aggregate="15m"):
    """Query parameters for one ``/api/history/energy`` request.

    Shared, because the entity probe and the load interface have to ask EVCC the
    *same* question. The probe exists to answer "will the interface read this?",
    and two hand-written parameter sets can agree today and drift tomorrow -
    they already had: the probe asked per entity and summed the series itself
    while the interface asked EVCC to sum them.

    ``grouped`` has to be sent explicitly for a whole group: EVCC's handler
    reads ``grouped := q.Get("grouped") == "true"``, so leaving it out asks for
    per-entity series instead of their sum.

    Args:
        entity: ``(group, title_or_None)`` as ``evcc_entity_parts`` returns it.
        start, end: timezone-aware bounds; sent as RFC3339 UTC.
        aggregate: EVCC bucket size. ``15m`` is its native slot.
    """
    group, title = entity
    params = {
        "from": start.astimezone(timezone.utc).isoformat(),
        "to": end.astimezone(timezone.utc).isoformat(),
        "aggregate": aggregate,
        # Narrows the query in EVCC instead of pulling every meter and
        # discarding most of it.
        "group": group,
    }
    if title:
        params["title"] = title
    else:
        params["grouped"] = "true"
    return params
