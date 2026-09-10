"""
Placing an energy contingent into the cheapest slots it is allowed to occupy.

The optimizer decides how to *source* energy - grid, battery, PV. It does not decide
when a pool heat pump should run, and for the local EVopt backends it cannot: neither
supports even the single schedulable appliance the EOS server does. So a managed load
brings its own placement, and this module is it.

The rule is deliberately simple and explainable, because the user has to be able to look
at the dashboard and understand why the pump is running at 13:00: rank the slots the
load may use by what the energy costs there, and fill the cheapest ones until the
demand is covered. What makes it more than a sort is the constraint set - a minimum
runtime so the compressor is not cycled once per slot, a daily cap, a per-slot power
budget shared with the other managed loads, and an urgent path that ignores price
entirely when the water is about to freeze.
"""

import logging

logger = logging.getLogger("__main__")

STRATEGY_CHEAPEST = "cheapest_slots"
STRATEGY_PV_SURPLUS = "pv_surplus"
STRATEGY_COMBINED = "combined"

STRATEGIES = (STRATEGY_CHEAPEST, STRATEGY_PV_SURPLUS, STRATEGY_COMBINED)

# Why a slot carries no energy. Recorded per slot so the page can say which setting is
# actually holding a load back: "covers 20% of it, widen the window" is bad advice when
# the window is wide open and the price cap is doing the excluding.
SLOT_PLANNED = "planned"
SLOT_PAST = "past"
SLOT_DEADLINE = "after deadline"
SLOT_INFEASIBLE = "not allowed"
SLOT_PRICE = "above price cap"
SLOT_BUDGET = "shared power budget"
SLOT_DAILY_CAP = "daily runtime cap"
SLOT_NOT_NEEDED = "not needed"

# Reasons that are nobody's fault: the slot has gone, or the demand was already met.
# Everything else represents a setting standing in the way, and a load short of energy
# is limited by whichever of those excluded the most slots.
#
# Defined as the exception rather than the rule on purpose. Listing the blocking reasons
# instead meant the model's own, more specific ones - "outside allowed hours" in place of
# a generic "not allowed" - were not on the list, so the very cases worth naming were the
# ones that came back as nothing at all.
NON_BLOCKING_REASONS = (SLOT_PLANNED, SLOT_PAST, SLOT_NOT_NEEDED)

# Ranking cost for a slot with no price information. Above any realistic tariff, so
# priced slots always win, but finite so a missing price series still yields a plan
# rather than no heating at all.
_UNKNOWN_PRICE = 10.0


class PlanOptions:
    """
    The constraints a contingent is placed under.

    Attributes:
        strategy: One of `STRATEGIES`.
        min_runtime_slots: Shortest contiguous run the load may be released for.
        max_slots_per_day: Cap on released slots per local day, 0 for none.
        max_price_eur_per_wh: Refuse slots above this, unless the demand cannot
            otherwise be met. None disables the cap.
    """

    def __init__(self, strategy=STRATEGY_COMBINED, min_runtime_slots=1,
                 max_slots_per_day=0, max_price_eur_per_wh=None):
        self.strategy = strategy if strategy in STRATEGIES else STRATEGY_COMBINED
        self.min_runtime_slots = max(1, int(min_runtime_slots or 1))
        self.max_slots_per_day = max(0, int(max_slots_per_day or 0))
        self.max_price_eur_per_wh = max_price_eur_per_wh


def _series(values, length, fill=0.0):
    """Pad or truncate a forecast series to the horizon length."""
    if not values:
        return [fill] * length
    out = list(values[:length])
    while len(out) < length:
        out.append(out[-1] if out else fill)
    return out


def _slot_costs(ctx, options, caps):
    """
    Cost per Wh of running in each slot, under the chosen strategy.

    PV surplus is valued at the *feed-in* tariff rather than at zero: the energy is not
    free, it is worth what the grid would have paid for it. That is what makes a slot
    with surplus beat a cheap grid slot without inventing a preference.
    """
    length = ctx.slot_count
    price = _series(ctx.price_eur_per_wh, length, _UNKNOWN_PRICE)
    feed_in = _series(ctx.feed_in_eur_per_wh, length, 0.0)
    surplus = _series(ctx.pv_surplus_wh, length, 0.0)

    costs = []
    for index in range(length):
        cap = caps[index]
        if cap <= 0:
            costs.append(float("inf"))
            continue

        grid_price = price[index]
        if options.strategy == STRATEGY_CHEAPEST:
            costs.append(grid_price)
            continue

        covered = max(0.0, min(cap, surplus[index]))
        remainder = cap - covered
        if options.strategy == STRATEGY_PV_SURPLUS:
            # Surplus first, and among equally sunny slots the cheaper one. Slots with
            # no surplus stay available so a target temperature is still reached on a
            # week of rain - they just rank last.
            costs.append((-covered, grid_price))
            continue

        blended = (covered * feed_in[index] + remainder * grid_price) / cap
        costs.append(blended)
    return costs


def _candidates(demand, ctx, caps, costs, options, reasons):
    """Slots the load may actually occupy, and why each of the others is out."""
    start = max(0, ctx.current_slot)
    end = ctx.slot_count - 1
    if demand.deadline_slot is not None:
        end = min(end, int(demand.deadline_slot))

    feasible = _series([bool(v) for v in demand.feasible], ctx.slot_count, True)
    detail = _series(list(demand.feasible_reason or []), ctx.slot_count, None)

    for index in range(ctx.slot_count):
        if index < start:
            reasons[index] = SLOT_PAST
        elif index > end:
            reasons[index] = SLOT_DEADLINE

    usable = []
    priced_out = 0
    for index in range(start, end + 1):
        if not feasible[index]:
            reasons[index] = detail[index] or SLOT_INFEASIBLE
            continue
        if caps[index] <= 0:
            reasons[index] = SLOT_BUDGET
            continue
        cost = costs[index]
        if isinstance(cost, float) and cost == float("inf"):
            reasons[index] = SLOT_BUDGET
            continue
        if (
            options.max_price_eur_per_wh is not None
            and not demand.urgent
            and options.strategy != STRATEGY_PV_SURPLUS
            and cost > options.max_price_eur_per_wh
        ):
            priced_out += 1
            reasons[index] = SLOT_PRICE
            continue
        usable.append(index)

    if priced_out:
        logger.debug(
            "[LOADS] %d slots skipped for exceeding the configured price cap", priced_out
        )
    return usable, priced_out


def _blocks(candidates, length):
    """Contiguous runs of exactly *length* candidate slots."""
    if length <= 1:
        return [[index] for index in candidates]
    available = set(candidates)
    runs = []
    for index in candidates:
        run = list(range(index, index + length))
        if all(slot in available for slot in run):
            runs.append(run)
    return runs


def plan_contingent(demand, ctx, options, budget_wh=None):
    """
    Place ``demand.total_wh`` into slots, returning a per-slot Wh series.

    *budget_wh* is the residual per-slot energy left by higher-priority managed loads;
    None means unlimited. Slots are filled to at most the load's own power draw and at
    most that residual, which is what stops a pool pump and a sauna from stacking into
    the same cheap slot and forecasting a peak the house cannot draw.
    """
    length = ctx.slot_count
    plan = [0.0] * length
    # Filled in as slots are ruled out, so the page can name the setting that is
    # actually holding the load back rather than guessing.
    reasons = [SLOT_NOT_NEEDED] * length
    demand.slot_reasons = reasons
    remaining = float(demand.total_wh)
    if remaining <= 0:
        return plan

    slot_capacity = demand.max_power_w * ctx.hours_per_slot()
    if slot_capacity <= 0:
        logger.warning(
            "[LOADS] managed load has demand but no power rating - cannot place it"
        )
        return plan

    caps = []
    for index in range(length):
        cap = slot_capacity
        if budget_wh is not None:
            cap = min(cap, max(0.0, budget_wh[index]))
        caps.append(cap)

    costs = _slot_costs(ctx, options, caps)
    candidates, _ = _candidates(demand, ctx, caps, costs, options, reasons)

    if not candidates:
        logger.info(
            "[LOADS] no feasible slot for %.0f Wh of demand - check the allowed window, "
            "the ambient temperature limit and the price cap",
            remaining,
        )
        return plan

    if demand.urgent:
        # Cheapness is worthless if the pool freezes: take the next slots available.
        ordered = sorted(candidates)
        return _fill(plan, ordered, caps, remaining, options, ctx, reasons)

    ordered_blocks = _blocks(candidates, options.min_runtime_slots)
    if not ordered_blocks:
        # The window is shorter than the minimum runtime. Running for less is better
        # than not running at all, so fall back to single slots and say so.
        logger.info(
            "[LOADS] no run of %d contiguous slots available - placing the demand in "
            "single slots instead",
            options.min_runtime_slots,
        )
        ordered_blocks = [[index] for index in candidates]

    ordered_blocks.sort(key=lambda run: (_block_cost(run, costs), run[0]))

    order = []
    seen = set()
    for run in ordered_blocks:
        if remaining <= 0:
            break
        fresh = [slot for slot in run if slot not in seen]
        if not fresh:
            continue
        for slot in run:
            if slot not in seen:
                seen.add(slot)
                order.append(slot)
        remaining -= sum(caps[slot] for slot in fresh)

    # Anything the blocks could not cover falls back to the cheapest leftover slots.
    leftovers = [index for index in candidates if index not in seen]
    leftovers.sort(key=lambda index: (costs[index], index))
    order.extend(leftovers)

    return _fill(plan, order, caps, float(demand.total_wh), options, ctx, reasons)


def _block_cost(run, costs):
    """Mean cost of a candidate run, so long runs are not penalised for being long."""
    values = [costs[slot] for slot in run]
    if values and isinstance(values[0], tuple):
        surplus = sum(item[0] for item in values) / len(values)
        price = sum(item[1] for item in values) / len(values)
        return (surplus, price)
    return sum(values) / len(values)


def _fill(plan, order, caps, remaining, options, ctx, reasons):
    """Pour *remaining* Wh into *order* until it is gone, honouring the daily cap."""
    slots_per_day = max(1, 86400 // ctx.time_frame_base)
    used_per_day = {}

    for slot in order:
        if remaining <= 1e-6:
            break
        day = slot // slots_per_day
        if options.max_slots_per_day and used_per_day.get(day, 0) >= options.max_slots_per_day:
            reasons[slot] = SLOT_DAILY_CAP
            continue
        take = min(caps[slot], remaining)
        if take <= 0:
            reasons[slot] = SLOT_BUDGET
            continue
        plan[slot] = take
        reasons[slot] = SLOT_PLANNED
        remaining -= take
        used_per_day[day] = used_per_day.get(day, 0) + 1

    if remaining > 1e-6:
        logger.info(
            "[LOADS] %.0f Wh of demand could not be placed in the horizon - it will be "
            "carried into the next planning run",
            remaining,
        )
    return plan
