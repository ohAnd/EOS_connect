"""
Plan stability: a small price on changing your mind.

The optimizer is re-run every cycle from scratch, and it has no idea what it decided
fifteen minutes ago. Where two plans cost almost the same - a flat price curve with a
full battery, a heat pump that could run at 11:00 or at 12:00 for half a cent of
difference, a 1 % optimality gap that is wider than that half cent - it picks one of
them more or less at random, and the next run may pick the other. Nothing is wrong
with either plan; the trouble is that the decision flips between them. A heat pump
starts, stops half an hour later and starts again; the "next start" an automation
reads jumps between 10:00 and 12:00 every cycle.

This module gives every run a memory of the previous one and charges for departing
from it: each Wh a series moves away from what the last plan said for the same slot
costs a fraction of the mean import price. A real change - a new day-ahead price, a
forecast that moved a lot - still wins, because it is worth more than that fraction.
Noise does not.

Generic on purpose. A series is anything the solver decides per slot, named by a key:
``ml:<id>`` for a managed load, ``c:<n>`` and ``d:<n>`` for battery charging and
discharging. The optimizer adds the penalty for whichever keys it is handed and has
variables for; adding another consumer is naming its variables, not writing another
mechanism.

The price is not the same everywhere in the plan. Moving a run costs twice its energy
in deviation - off where it was, on where it goes - so a weight w means a run only moves
for a saving of more than 2w of the mean price. Over the next two hours, where a change
switches something physical, that should be hard: a quarter of the price, so 50 %. Further
out, where a change only moves what the plan shows, it should be easy, or the first plan
of the morning would be carried into the afternoon against a better one: a fifth of
that, so 10 %.

Measured on one day of 15-minute replanning with noisy PV forecasts (heat pump, 16 kWh
battery, 1 h minimum runtime), three seeds each:

    penalty                         starts  flips  next start jumps >= 1 h
    none                             3.0     4.3          19
    0.25 over the whole plan         2.3     1.0           3
    0.25 for 2 h, 0.05 after (this)  2.0     0.3           3.7
    0.10 for 2 h, 0.05 after         2.3     2.0           7.7

Narrowing the optimality gap from 1 % to 0.1 % instead did nothing for the flips and
took up to 13 s per solve.
"""

import re
from datetime import timedelta

import pulp

# Default weight: a quarter of the mean import price per Wh moved. Large enough to
# outweigh the optimality gap and forecast noise, small enough that a price difference
# of a few cents per kWh still moves the plan.
DEFAULT_WEIGHT = 0.25

# How far ahead the full weight applies, and what is left of it after that.
NEAR_HOURS = 2.0
FAR_FACTOR = 0.2

# What the penalty applies to unless configured otherwise. Managed loads first: they
# are what switches something physical on and off. Battery series are available but
# opt-in, because a battery that follows the old plan a little longer costs money
# without anything audible to show for it.
SCOPE_OFF = "off"
SCOPE_MANAGED_LOADS = "managed_loads"
SCOPE_ALL = "managed_loads_and_battery"
SCOPES = (SCOPE_OFF, SCOPE_MANAGED_LOADS, SCOPE_ALL)

# A plan older than this says nothing useful about the current one - after a restart,
# a long solver outage, or a clock jump.
MAX_AGE = timedelta(hours=6)


def key_managed_load(load_id):
    return f"ml:{load_id}"


def key_battery_charge(index):
    return f"c:{index}"


def key_battery_discharge(index):
    return f"d:{index}"


def scope_includes(scope, key):
    """Whether a series with this key is held to the previous plan under *scope*."""
    if scope == SCOPE_OFF or not key:
        return False
    if key.startswith("ml:"):
        return scope in (SCOPE_MANAGED_LOADS, SCOPE_ALL)
    return scope == SCOPE_ALL


class PlanMemory:
    """
    The last solved series, by key, anchored to the wall-clock start of its slot 0.

    The anchor is what makes it reusable across runs: the next run starts a slot or two
    later, so its slot 0 is the previous plan's slot 1 or 2. `previous` lines the two
    up and leaves None where the old plan has nothing to say - the slots it never
    covered, such as tomorrow before the day-ahead prices arrived.
    """

    def __init__(self, slot_seconds):
        self.slot_seconds = int(slot_seconds)
        self._start = None
        self._series = {}

    def remember(self, start, series):
        """Store *series* ({key: [value per slot]}) as solved for a plan from *start*."""
        self._start = start
        self._series = {
            key: [float(v or 0.0) for v in values]
            for key, values in (series or {}).items()
            if values is not None
        }

    def forget(self):
        self._start = None
        self._series = {}

    def previous(self, start, length):
        """
        The remembered series re-indexed to a plan starting at *start*, *length* long.

        Returns {key: [value or None per slot]}, or {} when nothing usable is stored.
        """
        if self._start is None or not self._series or length <= 0:
            return {}
        elapsed = (start - self._start).total_seconds()
        if elapsed < 0 or elapsed > MAX_AGE.total_seconds():
            return {}
        offset = int(round(elapsed / self.slot_seconds))
        shifted = {}
        for key, values in self._series.items():
            row = []
            for t in range(length):
                source = t + offset
                row.append(values[source] if source < len(values) else None)
            shifted[key] = row
        return shifted


def slot_weights(weight, slot_seconds):
    """Per-slot penalty: *weight* for the next `NEAR_HOURS`, `FAR_FACTOR` of it after."""
    weights = []
    elapsed = 0.0
    for seconds in slot_seconds:
        weights.append(weight if elapsed < NEAR_HOURS * 3600 else weight * FAR_FACTOR)
        elapsed += float(seconds or 0)
    return weights


def add_change_penalty(problem, new_var, objective, name, variables, previous, weight):
    """
    Charge *weight* per unit that *variables* move away from *previous*.

    *weight* is a number for every slot alike, or a list with one per slot.

    Adds one non-negative deviation variable per slot that has a previous value, with
    the two constraints that make it the absolute difference, and returns the
    objective with the penalty subtracted (the problem maximises). Slots where
    *previous* is None are left free. Works for any per-slot decision variables.
    """
    weights = weight if isinstance(weight, (list, tuple)) else [weight] * len(variables)
    if not variables or not previous or max(weights, default=0) <= 0:
        return objective
    terms = []
    for t, var in enumerate(variables):
        if (
            t >= len(previous)
            or previous[t] is None
            or t >= len(weights)
            or weights[t] <= 0
        ):
            continue
        deviation = new_var(f"chg_{_safe(name)}_{t}", lowBound=0)
        problem += deviation >= var - previous[t]
        problem += deviation >= previous[t] - var
        terms.append(weights[t] * deviation)
    if not terms:
        return objective
    return objective - pulp.lpSum(terms)


def _safe(name):
    """A key as a solver variable name: CBC's LP format takes no colons or spaces."""
    return re.sub(r"\W", "_", str(name))
