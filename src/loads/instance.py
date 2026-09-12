"""
One configured managed load, from sensors to a release signal.

`ManagedLoad` is where the pieces meet: it asks its model what is needed, has the planner
place it, lets the gate decide whether that means "run now", and publishes the result as
a `LoadContribution`. Everything appliance-specific lives in the model; everything here
is the same for a pool, a sauna and a pushed heating profile.

The single most important thing this class does is keep the forecast and the control
signal derived from **one** plan. If the array handed to the optimizer said the pump runs
at 13:00 while the gate released it at 16:00, the optimizer would be planning the battery
around a load that never appears - which is precisely the failure mode that makes people
give up on `additional_load_1` in issues #34 and #201.
"""

import logging
from datetime import timedelta

from .contribution import (
    KIND_PROFILE,
    SOURCE_API,
    SOURCE_INTERNAL,
    SOURCE_MQTT,
    LoadContribution,
)
from .gate import ReleaseGate
from .planner import (
    NON_BLOCKING_REASONS,
    plan_price,
    SLOT_DEADLINE,
    SLOT_PAST,
    SLOT_PLANNED,
    PlanOptions,
    plan_contingent,
)
from .presets import CONTINGENT_TYPES, EXTERNAL_TYPES, apply_defaults, build_model

logger = logging.getLogger("__main__")


def _as_ct_per_kwh(eur_per_wh):
    """Back to the unit the user set the limit in, or None when nothing was placed."""
    if eur_per_wh is None:
        return None
    return round(eur_per_wh * 100.0 * 1000.0, 2)

# How long a contribution computed internally stays valid. Long enough to survive a
# skipped cycle, short enough that a wedged poll thread stops inflating the forecast
# instead of holding a stale demand there for a day.
_INTERNAL_TTL_CYCLES = 3
_INTERNAL_TTL_MIN_MINUTES = 15


class ManagedLoad:
    """A single entry from the ``managed_loads`` config section."""

    def __init__(self, entry, cycle_seconds=300):
        resolved = apply_defaults(entry)
        self.config = resolved
        self.id = str(resolved.get("id", "")).strip()
        self.type = str(resolved.get("type", "")).strip()
        self.enabled = bool(resolved.get("enabled", True))
        self.priority = int(resolved.get("priority", 100) or 100)
        self.subtract_from_base_load = bool(resolved.get("subtract_from_base_load", True))
        self.power_sensor = str(resolved.get("power_sensor", "") or "").strip()

        self.cycle_seconds = max(30, int(cycle_seconds or 300))
        self.model = build_model(resolved)
        self.gates_release = self.type in CONTINGENT_TYPES
        self.gate = (
            ReleaseGate(self.id, resolved.get("min_runtime_minutes", 0))
            if self.gates_release
            else None
        )
        self.max_price_eur_per_wh = self._price_cap(resolved, "max_price_ct_kwh")

        self.last_plan = []
        self.last_slot_capacity_wh = 0.0
        # When a schedule last arrived from outside, and when this load first started
        # waiting for one. A gate that is only ever settled by someone else freezes the
        # moment they stop answering - but a load that has never been scheduled is not
        # stale, it is new, and falling back on its first cycle would put it into the
        # household forecast *and* offer it to the solver at the same time.
        self.last_schedule_at = None
        self.waiting_for_schedule_since = None
        # Whether the reported price was measured against a run without this load, or
        # is only the tariff of the hours it occupies.
        self.last_cost_is_measured = False
        self.last_cost_is_shared = False
        self.last_demand = None
        self.last_release = None
        self.last_error = None

    def _price_cap(self, entry, key):
        """ct/kWh is what a user can reason about; the planner wants EUR/Wh."""
        raw = entry.get(key)
        if raw in (None, "", 0):
            return None
        try:
            return float(raw) / 100.0 / 1000.0
        except (TypeError, ValueError):
            logger.warning(
                "[LOADS] '%s' has an unreadable %s %r - ignoring that limit",
                self.id, key, raw,
            )
            return None

    def schedule_is_stale(self, now, max_age_seconds):
        """
        Whether nobody has scheduled this load recently enough to be trusted.

        The release signal is only refreshed when a schedule arrives, so an optimizer
        that stops answering would otherwise leave the appliance latched on whatever it
        was last told - a pump held released indefinitely, with nothing logging why.
        """
        since = self.last_schedule_at or self.waiting_for_schedule_since
        if since is None:
            return False
        return (now - since).total_seconds() > max_age_seconds

    def kind_is_contingent(self):
        """Whether this load's timing is ours to choose, rather than merely reported."""
        return self.type in CONTINGENT_TYPES

    def min_runtime_slots(self, ctx):
        """The configured minimum run, in slots at the running resolution."""
        minutes = float(self.config.get("min_runtime_minutes", 0) or 0)
        return max(1, int(round(minutes * 60 / ctx.time_frame_base)))

    def _resolved_options(self, ctx):
        """Turn the minute- and hour-based config into slot counts for this resolution."""
        slots_per_hour = ctx.slots_per_hour()
        min_runtime_slots = self.min_runtime_slots(ctx)
        max_hours = float(self.config.get("max_runtime_hours_per_day", 0) or 0)
        return PlanOptions(
            strategy=self.config.get("strategy"),
            min_runtime_slots=min_runtime_slots,
            max_slots_per_day=int(max_hours * slots_per_hour),
            max_price_eur_per_wh=self.max_price_eur_per_wh,
        )

    def reconfigure(self, entry):
        """
        Apply a changed configuration in place.

        The model keeps whatever it has learned - a target temperature change should not
        cost a fortnight of calibration - and the gate keeps its current release, so a
        running compressor is not interrupted by someone adjusting an unrelated field.
        Restart-required settings (id, type, sensors, physical dimensions) never reach
        here; the schema labels them and the UI shows the restart banner instead.
        """
        resolved = apply_defaults(entry)
        self.config = resolved
        self.enabled = bool(resolved.get("enabled", True))
        self.priority = int(resolved.get("priority", 100) or 100)
        self.max_price_eur_per_wh = self._price_cap(resolved, "max_price_ct_kwh")

        if self.gate is not None:
            self.gate.min_runtime_minutes = max(
                0, int(resolved.get("min_runtime_minutes", 0) or 0)
            )
        if self.model is not None:
            self.model.reconfigure(resolved)

    # -- the cycle ------------------------------------------------------------------------

    def evaluate(self, ctx, budget_wh=None, defer_gate=False):
        """
        Run one planning cycle.

        Returns ``(contribution, release)``. *release* is None for profile-kind loads,
        which have nothing to gate. Exceptions from a model are caught and reported on
        the instance rather than propagated: this runs on the path that builds the
        optimizer request, and one misconfigured sauna must not stop the house being
        optimized.
        """
        if not self.enabled or self.model is None:
            return None, None

        try:
            demand = self.model.demand(ctx)
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            self.last_error = str(exc)
            logger.exception("[LOADS] '%s' failed to compute its demand", self.id)
            return None, None

        self.last_error = None
        self.last_demand = demand

        if demand.kind == KIND_PROFILE:
            plan = list(demand.profile_wh or [0.0] * ctx.slot_count)
            release = None
        elif defer_gate:
            # Somebody else is doing the placing. Plan anyway - it is what the load
            # falls back to if no schedule arrives - but do not touch the gate: it
            # remembers when it last released, so settling it against a plan that is
            # about to be replaced would start a minimum-runtime hold on a decision
            # nobody made.
            self._mask_current_slot(demand, ctx)
            plan = self._plan_only(demand, ctx, budget_wh)
            # The gate keeps whatever it last decided until a schedule arrives. Nulling
            # it left the card reporting a bare gate state - no energy figure, no next
            # start - for every cycle that landed between two optimizer runs.
            release = self.last_release
        else:
            plan, release = self._plan_and_gate(demand, ctx, budget_wh)

        self.last_plan = plan
        self.last_release = release

        if not any(plan):
            return None, release

        return self._contribution(plan, ctx, demand), release

    def adopt_schedule(self, schedule, ctx, cost=None):
        """
        Take a schedule somebody else produced, and gate on that instead.

        The optimizer places this load against the household and the battery together,
        which is a better answer than this module can reach on its own. What does not
        change is that one series drives both the release signal and whatever is
        reported - the two cannot disagree, because there is only ever one plan.
        """
        if self.last_demand is None or self.gate is None:
            return None
        plan = [max(0.0, float(value or 0.0)) for value in schedule]
        if len(plan) < ctx.slot_count:
            plan += [0.0] * (ctx.slot_count - len(plan))
        self.last_plan = plan[: ctx.slot_count]
        self.last_schedule_at = ctx.now
        # What the household actually pays extra, where the optimizer measured it. The
        # tariff of the occupied hours is the fallback, and only a rough guide: it
        # cannot see that the energy came from the roof, so a sunny day reads dearer
        # than a dark one.
        measured = (cost or {}).get("eur_per_wh")
        self.last_demand.plan_price_eur_per_wh = (
            float(measured) if measured is not None
            else plan_price(self.last_plan, list(ctx.price_eur_per_wh or []))
        )
        self.last_cost_is_measured = measured is not None
        self.last_cost_is_shared = bool((cost or {}).get("shared"))
        self.last_release = self._gate_on(self.last_demand, ctx, self.last_plan)
        return self.last_release

    def _plan_only(self, demand, ctx, budget_wh):
        """The placement, without settling the gate on it."""
        options = self._resolved_options(ctx)
        self.last_slot_capacity_wh = demand.max_power_w * ctx.hours_per_slot()
        return plan_contingent(demand, ctx, options, budget_wh=budget_wh)

    def _gate_on(self, demand, ctx, plan):
        """Turn a placement into a release decision."""
        active = bool(demand.detail.get("active", demand.total_wh > 0))
        planned_now = (
            0 <= ctx.current_slot < len(plan) and plan[ctx.current_slot] > 0
        )
        release = self.gate.evaluate(
            planned=planned_now,
            urgent=demand.urgent,
            now=ctx.now,
            has_demand=active,
        )
        release["next_release_start"] = self._next_release_start(plan, ctx)
        release["energy_needed_wh"] = round(demand.total_wh, 1)
        return release

    def _mask_current_slot(self, demand, ctx):
        """
        A load that must not start right now must not be *forecast* to start now either.

        Marking the current slot infeasible before anything is placed is what keeps the
        array the optimizer works from and the signal sent to the appliance in
        agreement. It belongs here rather than inside the planner because the same mask
        is handed out when something else does the placing.
        """
        active = bool(demand.detail.get("active", demand.total_wh > 0))
        feasible = list(demand.feasible) or [True] * ctx.slot_count
        if not active and 0 <= ctx.current_slot < len(feasible):
            feasible[ctx.current_slot] = False
        demand.feasible = feasible

    def _plan_and_gate(self, demand, ctx, budget_wh):
        """Place the contingent, then decide whether that means "run now"."""
        self._mask_current_slot(demand, ctx)
        plan = self._plan_only(demand, ctx, budget_wh)
        return plan, self._gate_on(demand, ctx, plan)

    def _next_release_start(self, plan, ctx):
        """When the appliance is next expected to run, as an ISO timestamp."""
        for index in range(max(0, ctx.current_slot), len(plan)):
            if plan[index] > 0:
                start = ctx.anchor + timedelta(seconds=index * ctx.time_frame_base)
                return start.isoformat()
        return None

    def _contribution(self, plan, ctx, demand):
        ttl_minutes = max(
            _INTERNAL_TTL_MIN_MINUTES,
            _INTERNAL_TTL_CYCLES * self.cycle_seconds // 60,
        )
        source = SOURCE_INTERNAL
        if self.type in EXTERNAL_TYPES:
            source = getattr(self.model, "last_source", SOURCE_API)
        return LoadContribution(
            id=self.id,
            slots_wh=plan,
            anchor=ctx.anchor,
            time_frame_base=ctx.time_frame_base,
            valid_until=ctx.now + timedelta(minutes=ttl_minutes),
            kind=demand.kind,
            source=source,
        )

    # -- external control -----------------------------------------------------------------

    def accept_push(self, parsed, anchor, time_frame_base, source=SOURCE_API):
        """Hand a parsed push to the model. Only external types accept one."""
        if not hasattr(self.model, "accept"):
            raise ValueError(
                f"managed load '{self.id}' is of type '{self.type}' and computes its "
                "own demand - it does not accept pushed data"
            )
        self.model.accept(parsed, anchor, time_frame_base)
        self.model.last_source = SOURCE_MQTT if source == SOURCE_MQTT else SOURCE_API

    def clear_push(self):
        """Drop the stored push. Only external types have one to drop."""
        if hasattr(self.model, "clear"):
            self.model.clear()
            return True
        return False

    def set_override(self, mode, minutes, now):
        """Force this load released or blocked. Profile-kind loads have no gate."""
        if self.gate is None:
            raise ValueError(
                f"managed load '{self.id}' has no release signal to override"
            )
        self.gate.set_override(mode, minutes, now)

    def observe(self, sample):
        """Feed one recorded sample to the model's calibration, if it has any."""
        if self.model is None:
            return False
        return bool(self.model.observe(sample))

    # -- reporting ------------------------------------------------------------------------

    def plan_summary(self):
        """
        What became of every slot, and which setting is holding the load back.

        A shortfall used to be reported as "widen its window or raise the daily cap",
        which is wrong advice whenever something else did the excluding - a price cap
        can rule out four fifths of a horizon while the window stands wide open.
        """
        reasons = (self.last_demand.slot_reasons if self.last_demand else None) or []
        if not reasons:
            return None

        counts = {}
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1

        # Only a shortfall has something holding it back. A slot can be skipped for the
        # daily cap and the demand still be met the next day, and reporting that as a
        # limit would send the user to loosen a setting that cost them nothing.
        needed = self.last_demand.total_wh
        covered = sum(self.last_plan or []) >= needed - 1.0

        blocking = [] if covered else [
            (count, reason) for reason, count in counts.items()
            if reason not in NON_BLOCKING_REASONS and count
        ]
        blocking.sort(reverse=True)

        # What the appliance could deliver if every limit were lifted at once - every
        # slot still ahead of it, running flat out. When the demand is above even that,
        # naming the setting that excluded the most slots is true and useless: the user
        # raises it, and the load still cannot keep up. On a real pool the standing
        # losses grew until 36 kWh was wanted from a horizon that could carry 29, and
        # the card sent its owner to the price cap.
        reachable = sum(
            1 for reason in reasons if reason not in (SLOT_PAST, SLOT_DEADLINE)
        )
        reachable_wh = reachable * self.last_slot_capacity_wh
        over_committed = bool(blocking) and needed > reachable_wh + 1.0

        return {
            "slots": len(reasons),
            "planned": counts.get(SLOT_PLANNED, 0),
            "counts": counts,
            # The one worth naming to the user; None when nothing was in the way.
            "limited_by": blocking[0][1] if blocking else None,
            "limited_slots": blocking[0][0] if blocking else 0,
            # What the plan came to per kWh. Under a budget the pump can legitimately
            # run in the day's dearest hour, which looks wrong until this number is
            # next to it.
            "avg_price_ct_kwh": _as_ct_per_kwh(
                self.last_demand.plan_price_eur_per_wh if self.last_demand else None
            ),
            # What that figure is: what the household pays extra for this load, or
            # merely the tariff of the hours it runs in. They differ most on the sunny
            # days people care about, so the card must not present one as the other.
            "price_is_measured": self.last_cost_is_measured,
            "price_is_shared": self.last_cost_is_shared,
            # Set when no setting can close the gap, so the card stops naming one.
            "over_committed": over_committed,
            "reachable_wh": round(reachable_wh, 1),
            "shortfall_wh": round(max(0.0, needed - sum(self.last_plan or [])), 1),
        }

    def status(self):
        """Everything the REST API and the dashboard show for this instance."""
        demand = self.last_demand
        payload = {
            "id": self.id,
            "type": self.type,
            "enabled": self.enabled,
            "priority": self.priority,
            "kind": demand.kind if demand else None,
            "reason": demand.reason if demand else None,
            "energy_needed_wh": round(demand.total_wh, 1) if demand else 0.0,
            "planned_wh": round(sum(self.last_plan), 1) if self.last_plan else 0.0,
            "plan": [round(value, 1) for value in self.last_plan],
            "error": self.last_error,
            "model": self.model.status() if self.model else {},
        }
        if demand:
            payload["detail"] = demand.detail
            payload["plan_reasons"] = list(demand.slot_reasons or [])
        summary = self.plan_summary()
        if summary:
            payload["plan_summary"] = summary
        if self.gate is not None:
            payload["release"] = self.last_release or self.gate.status()
        return payload
