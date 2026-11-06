"""
How much a Wh is worth in each slot, relative to the best one.

A heat pump needs less electricity for the same heat when it is warmer outside. With a
fixed amount of energy to place and nothing else to tell the slots apart, a solver sees
every feasible slot as equally good - and on a day of flat prices, or with a battery
that makes every slot cost the same, it picks one arbitrarily. On a real house that
released the heating at 07:09 at 2 C while the afternoon reached 13 C, and the planned
start jumped between 08:30, 11:00 and 13:45 from one run to the next.

So a contingent may carry an **efficiency** per slot: 1.0 where a Wh does the most good,
less where the same Wh does less. The solver counts energy at its efficiency - a slot at
0.9 needs 11 % more electricity for the same result - and weighs that against what the
electricity costs there. Relative values rather than COPs, so one rule serves a heat pump,
an air conditioner and anything pushed from outside, and so the demand can be stated in
the units of the best slot.

Where the series comes from is one setting per load:

- ``off`` - every slot is 1.0, which is what every load did before this existed;
- ``cop`` - the thermal model's own COP for each slot's air temperature, over the best
  feasible one. Learned or measured wherever the model learns it;
- ``ambient`` - from the outdoor temperature alone, for a load with no model of its own:
  ``efficiency_per_k`` lost per kelvin below the warmest slot (``warmest``, for heating)
  or above the coolest (``coolest``, for cooling);
- ``pushed`` - whatever the external system sent alongside its energy.

A monotonic rule rather than bands on purpose. Bands - "within 2 K of the best is as good
as the best" - put a plateau where the solver needs a gradient, and inside a plateau it is
back to choosing arbitrarily.

Pure functions, no I/O.
"""

import math

SOURCE_OFF = "off"
SOURCE_COP = "cop"
SOURCE_AMBIENT = "ambient"
SOURCE_PUSHED = "pushed"
SOURCES = (SOURCE_OFF, SOURCE_COP, SOURCE_AMBIENT, SOURCE_PUSHED)

REFERENCE_WARMEST = "warmest"
REFERENCE_COOLEST = "coolest"
REFERENCES = (REFERENCE_WARMEST, REFERENCE_COOLEST)

# Below this a slot is not "less efficient", it is pointless - and an efficiency of a
# few percent would let the solver buy twenty times the energy to deliver the same
# result. Anything that low is excluded instead, through the feasibility mask.
MIN_EFFICIENCY = 0.2

# About what an air-source heat pump loses per kelvin colder, as a share of its COP:
# measured 2.0 % on a floor-heated house (3.42 at 0 C, 3.98 at 7 C).
DEFAULT_PER_K = 0.02


def source_of(config):
    """The configured source, or ``off`` for anything unknown."""
    value = str((config or {}).get("efficiency_source") or SOURCE_OFF).strip().lower()
    return value if value in SOURCES else SOURCE_OFF


def flat(slot_count):
    """The series a load without an efficiency has."""
    return [1.0] * max(0, int(slot_count))


def normalise(values, slot_count, start=0, feasible=None):
    """
    Scale a positive per-slot series so the best usable slot is 1.0.

    *start* and *feasible* say which slots count as usable: the best slot is the best
    one the load could actually run in, not one in the past or outside its window. A
    slot whose value is missing or not positive gets the floor.
    """
    series = []
    for index in range(max(0, int(slot_count))):
        try:
            value = float(values[index]) if index < len(values) else None
        except (TypeError, ValueError):
            value = None
        series.append(value if value is not None and value > 0 else 0.0)

    usable = [
        series[index]
        for index in range(max(0, int(start)), len(series))
        if feasible is None or (index < len(feasible) and feasible[index])
    ]
    best = max(usable) if usable else max(series, default=0.0)
    if best <= 0:
        return flat(slot_count)
    return [clamp(value / best) for value in series]


def clamp(value):
    """Keep an efficiency between the floor and 1.0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    if math.isnan(number):
        return 1.0
    return max(MIN_EFFICIENCY, min(1.0, number))


def from_ambient(
    ambient_c,
    slot_count,
    start=0,
    feasible=None,
    per_k=DEFAULT_PER_K,
    reference=REFERENCE_WARMEST,
):
    """
    Efficiency from the outdoor temperature alone.

    Linear in the distance from the best usable slot, ``per_k`` per kelvin: for heating
    the warmest slot is the best, for cooling the coolest.
    """
    temps = []
    for index in range(max(0, int(slot_count))):
        if ambient_c and index < len(ambient_c) and ambient_c[index] is not None:
            temps.append(float(ambient_c[index]))
        elif temps:
            temps.append(temps[-1])
        else:
            temps.append(None)
    known = [
        temps[index]
        for index in range(max(0, int(start)), len(temps))
        if temps[index] is not None
        and (feasible is None or (index < len(feasible) and feasible[index]))
    ]
    if not known:
        return flat(slot_count)
    cooling = str(reference).strip().lower() == REFERENCE_COOLEST
    best = min(known) if cooling else max(known)
    rate = max(0.0, float(per_k or 0.0))
    series = []
    for temp in temps:
        if temp is None:
            series.append(1.0)
            continue
        distance = (temp - best) if cooling else (best - temp)
        series.append(clamp(1.0 - rate * max(0.0, distance)))
    return series


def effective_wh(plan, efficiency):
    """What a plan delivers in units of the best slot."""
    total = 0.0
    for index, value in enumerate(plan or ()):
        weight = efficiency[index] if efficiency and index < len(efficiency) else 1.0
        total += float(value or 0.0) * weight
    return total
