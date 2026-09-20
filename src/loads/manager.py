"""
Owner of every managed load, and the one thing the optimizer path talks to.

The manager runs its own poll thread: it reads sensors, records samples for calibration,
asks each instance for a plan and publishes the results. The optimizer request builder
only ever calls `apply`, which reads the finished contributions out of the registry. That
split matters - the request builder runs on a schedule the user can make tight, and it
must not end up waiting on a Home Assistant history query.

Everything this package needs from the rest of EOS Connect arrives as a callable in
`ManagedLoadSources`. That is what keeps `loads` free of imports from `interfaces`: the
manager asks for "the price series" and does not care that a `PriceInterface` produced it,
which also makes the whole thing testable with four lambdas.
"""

import hashlib
import logging
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime

from .ambient_bias import IMPLAUSIBLE_GAP_K, MAX_OFFSET_K, AmbientBias
from .contribution import (
    SOURCE_API, SOURCE_PULL, LoadContributionRegistry, ttl_from_minutes,
)
from .injection import InjectionError, parse_push, profile_from_entries
from .instance import ManagedLoad
from .models.base import DemandContext
from .presets import (
    EXTERNAL_TYPES, fallback_ambient_c, pulls_its_profile, uses_outdoor_ambient,
)

logger = logging.getLogger("__main__")

# Horizon the optimizer plans over, in hours. Mirrors EOS_TGT_DURATION in eos_connect.py;
# passed in rather than imported so this package stays standalone.
DEFAULT_HORIZON_HOURS = 48


def _noop(*_args, **_kwargs):
    return None


# Units that mean the sensor is counting energy, not reporting power. A managed load's
# power sensor has to be watts: 1234 W and 1234 kWh are the same number, so reading one
# as the other is a silent wrong answer - the appliance looks permanently on and the
# measured efficiency is nonsense.
ENERGY_UNITS = ("wh", "kwh", "mwh", "j", "kj", "mj")

# Where an ambient series came from, best first. Reported alongside the numbers, because
# a prediction standing on a guessed constant looks exactly like one standing on a
# forecast until you are told otherwise.
AMBIENT_FORECAST = "forecast"
AMBIENT_SENSOR = "sensor"
AMBIENT_FALLBACK = "fallback"
# A forecast shifted to agree with the site's own thermometer.
AMBIENT_CORRECTED = "forecast_corrected"

# How fast today's departure from the typical day fades across the horizon.
#
# The per-hour offset is a climatology: it describes a *typical* hour, averaged over
# days. On an untypical day it is wrong for the whole day, not only for now. Live, a
# site whose sensor catches the morning sun had learned +4 K for 09:00; on a cold
# clear morning it read 8.9 C where the typical-day estimate said 13.9 - a 5 K
# anomaly - and the planner went on sizing energy into morning slots that would be
# below the 12 C cut-off by the time they arrived.
#
# Six hours keeps the rest of today anchored to what the thermometer actually says
# and lets tomorrow revert to the climatology, which is the only thing that can speak
# for it. Today's weather is not evidence about tomorrow's.
NOWCAST_HALF_LIFE_HOURS = 6.0

# How much of a single reading the departure takes on, as a half-life in minutes.
#
# The gate keys off the corrected curve, not off the thermometer, and that is
# deliberate: a raw reading wandering across the cut-off would switch the appliance on
# and off with it. So the curve has to be *accurate* rather than *live* - within a
# fraction of a kelvin, where it used to be more than three out. Smoothing the
# departure buys that: sensor noise averages away, while a real change works through
# in a few cycles. Ten minutes lags a 2 K/h ramp by about half a kelvin, which is the
# error budget; shorter starts chasing noise, longer starts missing the evening drop.
RESIDUAL_HALF_LIFE_MIN = 10.0

# Store temperatures kept in memory for the card. Two days at the shortest sensible
# poll is well under this; the cap only stops an unbounded list on a long uptime.
MEDIUM_HISTORY_MAX = 2000

# Who made a release decision, recorded in the journal. The distinction is the first
# thing you want when reviewing a day of toggling: an optimizer that keeps changing its
# mind and this module falling back on its own plan look identical from the appliance.
ORIGIN_OPTIMIZER = "optimizer"
ORIGIN_SELF = "self"

# A regional forecast and a garden thermometer disagree, and each is right about
# something the other cannot know: the forecast has the shape - that tonight drops to
# 11 and tomorrow reaches 22 - and the sensor has the level, because it is standing at
# the site. Shifting the forecast onto the sensor keeps both. See `loads.ambient_bias`
# for why the shift is learned per hour of the day rather than as one number.


@dataclass
class ManagedLoadSources:
    """
    Everything the manager needs from the rest of the application.

    Every entry is optional and defaults to "nothing available", so a partially
    configured system degrades to a smaller feature rather than to a traceback.
    """

    read_sensor: object = _noop           # (sensor_name) -> raw state or None
    read_sensor_details: object = _noop   # (sensor_name) -> {state, unit, device_class}
    read_history: object = _noop          # (sensor, start, end) -> [{state, last_updated}]
    price: object = _noop                 # () -> [EUR/Wh per slot]
    feed_in_price: object = _noop         # () -> [EUR/Wh per slot]
    pv_forecast: object = _noop           # () -> [Wh per slot]
    base_load: object = _noop             # () -> [Wh per slot]
    temperature_forecast: object = _noop  # () -> [degrees C, hourly]
    # () -> cumulative PV generation in kWh, or None. Differenced across a calibration
    # window it gives the exact mean irradiance over exactly that window, which is what
    # the solar term in `loads.models.calibration` regresses against. The PV forecast
    # stands in when there is no counter; neither leaves the term at zero.
    pv_counter_kwh: object = _noop
    # (entry_config) -> {"entries": [{"start": datetime, "value": Wh}, ...],
    #                    "resolution_seconds": int} | None
    #
    # Fetches a load profile for one managed load. Returns None when nothing is
    # configured to fetch, and raises ValueError with a user-facing message when the
    # fetch or the format fails. Timestamps must be aware and values already converted
    # to Wh per entry - normalising a timeseries needs `interfaces`, which this package
    # deliberately cannot import.
    read_profile: object = _noop


# What a Wh is worth to a load with no price limit set: high enough that the solver
# always prefers running, low enough to stay a number rather than an unbounded reward.
# 10 EUR/kWh is two orders above any tariff anyone has.
UNCAPPED_VALUE_EUR_PER_WH = 0.01

# What one start is worth avoiding, as a fraction of a slot's energy value.
#
# Without it the solver is indifferent between a contiguous run and the same slots
# scattered across the day - with near-flat prices every arrangement costs the same -
# so it returns whichever the search reaches first and the appliance cycles. Measured
# on fragmenting cases it collapsed five starts to one and three to one while placing
# exactly the same energy, so it buys contiguity for nothing. Half a slot says: take
# another start only if it saves more than half a slot's worth of running.
START_COST_SLOTS = 0.5


def _padded(mask, length):
    """A feasibility mask at exactly the horizon length, missing slots allowed."""
    values = [bool(value) for value in (mask or [])][:length]
    return values + [True] * (length - len(values))


@dataclass
class ManagerStats:
    """Counters shown on the status endpoint, for answering "is this thing running"."""

    cycles: int = 0
    last_cycle: object = None
    last_error: object = None
    samples_recorded: int = 0
    calibration_updates: int = 0
    extras: dict = field(default_factory=dict)


class ManagedLoadManager:
    """Builds, schedules and reports every configured managed load."""

    def __init__(self, entries, time_frame_base, time_zone, sources=None, store=None,
                 cycle_seconds=300, max_power_w=0, horizon_hours=DEFAULT_HORIZON_HOURS,
                 on_release_change=None, clock=None):
        self.time_frame_base = int(time_frame_base or 3600)
        self.time_zone = time_zone
        self.sources = sources or ManagedLoadSources()
        self.store = store
        self.cycle_seconds = max(30, int(cycle_seconds or 300))
        self.max_power_w = max(0.0, float(max_power_w or 0.0))
        self.horizon_hours = int(horizon_hours or DEFAULT_HORIZON_HOURS)
        self.on_release_change = on_release_change
        # Injectable so tests can pin "now". Every planning decision depends on which
        # slot the current moment falls in, so a suite that reads the wall clock passes
        # in the morning and fails after lunch.
        self._now = clock or self._wall_clock

        self.registry = LoadContributionRegistry()
        self.stats = ManagerStats()

        self._instances = {}
        self._published_release = {}
        self._warned_ambient = set()
        # (id, kind) pairs already reported as unfetchable, so a source that is down
        # for a day writes one line rather than one per cycle.
        self._warned_pull = set()
        self._warned_bias = set()
        self._warned_no_prices = False
        self._hinted_pv_counter = False
        # Set when the optimizer can place contingent loads itself, which changes what
        # this module does with them: it computes the demand and defers the placing.
        self.external_scheduler = False
        self._last_ctx = None
        self._last_ctx_for = {}
        # Per-hour forecast-versus-sensor offset, one learner per instance.
        self._ambient_bias = {}
        # The raw forecast beside the one actually used, kept per load so the card can
        # show what the correction did. Only outdoor loads ever populate it.
        self._ambient_trace = {}
        # How far today is running from the typical day, smoothed: id -> (when, kelvin).
        self._ambient_residual = {}
        # Recent store temperatures, so the card can draw where the water has been as
        # well as where the plan takes it: id -> deque of (when, celsius).
        self._medium_history = {}
        self._stop = threading.Event()
        self._thread = None

        self._build(entries or [])

    # -- construction ---------------------------------------------------------------------

    def _build(self, entries):
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id", "")).strip()
            if not entry_id:
                logger.error("[LOADS] a managed load entry has no id - skipping it")
                continue
            if entry_id in seen:
                logger.error(
                    "[LOADS] duplicate managed load id '%s' - only the first is used",
                    entry_id,
                )
                continue
            seen.add(entry_id)

            instance = ManagedLoad(entry, cycle_seconds=self.cycle_seconds)
            if instance.model is None:
                continue
            self._instances[entry_id] = instance
            logger.info(
                "[LOADS] managed load '%s' (%s) configured, priority %d, %s",
                entry_id, instance.type, instance.priority,
                "enabled" if instance.enabled else "disabled",
            )

        if self._instances:
            logger.info("[LOADS] %d managed load(s) active", len(self._instances))

    @property
    def instances(self):
        """Instances in the order they are planned: priority first, then id."""
        return sorted(
            self._instances.values(), key=lambda item: (item.priority, item.id)
        )

    def instance(self, entry_id):
        """The instance with this id, or None."""
        return self._instances.get(str(entry_id or "").strip())

    def subtract_sensors(self):
        """
        Power sensors whose history must come out of the household base load.

        An instance's measured consumption is already inside the weekday average the load
        profile is built from. Injecting its prediction on top without removing the
        history would count the same appliance twice - the exact trap described on issue
        #55 for `additional_load_1`.

        Which field names that meter depends on the type, so the instance resolves it;
        see `ManagedLoad.subtracted_sensor`. An externally fed load that names none is
        saying its forecast is an addition to the base load rather than a replacement
        for part of it, and that is the ordinary case.
        """
        return [
            item.subtracted_sensor
            for item in self.instances
            if item.enabled and item.subtracted_sensor
        ]

    def enabled_ids(self):
        """Ids of the instances that are actually running."""
        return [item.id for item in self.instances if item.enabled]

    def needs_outdoor_temperature(self):
        """
        Whether any managed load reads the outdoor temperature forecast.

        A pool sits outside: both what it loses and how efficiently the pump replaces it
        depend on the air temperature over the next two days. Without the forecast the
        model falls back to a flat default, and the prediction stops following the
        weather - which is the one thing it exists to do.
        """
        return any(
            item.enabled and uses_outdoor_ambient(item.type) for item in self.instances
        )

    # -- the optimizer-facing call ---------------------------------------------------------

    def apply(self, gesamtlast, time_frame_base=None):
        """
        Add every active contribution to a household load profile.

        Contingent loads are in here only when nothing downstream can place them. Where
        the optimizer schedules them itself they were dropped from the registry as the
        cycle ran, so this adds the profile loads and nothing else - a load cannot be
        both a fixed part of the forecast and a thing the solver is choosing when to run.

        The input list is never modified - both series here are backed by a cache on the
        interface that produced them, and mutating one in place is a bug this codebase
        has already paid for once (see the comment in `get_ems_data`).
        """
        base = list(gesamtlast or [])
        if not base or not self._instances:
            return base

        resolution = int(time_frame_base or self.time_frame_base)
        anchor, now, _ = self._clock()
        extra = self.registry.total(anchor, len(base), resolution, now=now)
        if not any(extra):
            return base

        combined = [max(0.0, value + bonus) for value, bonus in zip(base, extra)]
        logger.debug(
            "[LOADS] added %.0f Wh across %d slots to the load forecast",
            sum(combined) - sum(base), len(base),
        )
        return combined

    def contribution_total_wh(self):
        """Summed contribution over the horizon, for MQTT and the dashboard."""
        anchor, now, slot_count = self._clock()
        return round(
            sum(self.registry.total(anchor, slot_count, self.time_frame_base, now=now)), 1
        )

    # -- the cycle -------------------------------------------------------------------------

    def start(self):
        """Start the poll thread. Does nothing when no managed load is configured."""
        if not self._instances or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="managed-loads", daemon=True
        )
        self._thread.start()
        logger.info("[LOADS] poll thread started, cycle %ds", self.cycle_seconds)

    def shutdown(self):
        """Stop the poll thread and wait briefly for it to finish."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self):
        # Run once immediately so the first optimizer request already has contributions,
        # rather than optimizing a house without its pool for the first five minutes.
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception:  # pylint: disable=broad-except
                # The poll thread must outlive any single bad cycle: a sensor that
                # returns nonsense today should not silently end managed loads until
                # the next restart.
                self.stats.last_error = "unhandled error - see the log"
                logger.exception("[LOADS] managed load cycle failed")
            self._stop.wait(self.cycle_seconds)

    def run_cycle(self):
        """One full pass: read, learn, plan, publish. Also the unit-test entry point."""
        ctx_base = self._context()
        budget = self._initial_budget(ctx_base)
        self._last_ctx = ctx_base

        for item in self.instances:
            if not item.enabled:
                # Not just skipped: a load switched off has to stop contributing now,
                # not when its contribution happens to expire. Until this dropped, the
                # optimizer kept planning around an appliance the user had disabled.
                self.registry.drop(item.id)
                continue
            ctx = self._context_for(item, ctx_base)
            self._last_ctx_for[item.id] = ctx
            self._pull_profile(item, ctx)
            if uses_outdoor_ambient(item.type) and not self.has_pv_counter():
                self._hint_pv_counter(item)
            self._record_sample(item, ctx)
            defer = self.external_scheduler and item.kind_is_contingent()
            if defer:
                if item.waiting_for_schedule_since is None:
                    item.waiting_for_schedule_since = ctx_base.now
                if item.schedule_is_stale(ctx_base.now, self._schedule_max_age()):
                    defer = False
                    logger.warning(
                        "[LOADS] '%s' has had no schedule from the optimizer for over "
                        "%d s - deciding from its own plan until one arrives",
                        item.id, self._schedule_max_age(),
                    )
            contribution, release = item.evaluate(
                ctx, budget_wh=budget, defer_gate=defer
            )
            if defer:
                # Its energy reaches the optimizer as something to place, not as part
                # of the household profile. Registering it here as well would have the
                # solver schedule around a load it is also being asked to schedule.
                self.registry.drop(item.id)
            else:
                self._store_result(item, contribution, release)
                if item.kind_is_contingent():
                    self._record_decision(item, ctx, release, origin=ORIGIN_SELF)
            self._consume_budget(budget, item.last_plan)

        self.stats.cycles += 1
        self.stats.last_cycle = ctx_base.now.isoformat()
        return self.stats.cycles

    # -- handing the placing to something that can do it better -----------------------------

    def _schedule_max_age(self):
        """
        How long a load may go unscheduled before it falls back to its own plan.

        Two cycles: one missed run is a slow solve, two is something wrong. Short
        enough that a dead optimizer does not leave a pump latched on for an hour,
        long enough not to flap on a single late answer.
        """
        return max(120, self.cycle_seconds * 2)

    def schedulable(self):
        """
        The contingent loads, as records for an optimizer that can place them itself.

        Nothing in a record names an appliance: a sauna and a hot-water tank produce
        the same shape from the same code, because what the energy is *for* lives in
        the demand model that worked the numbers out.
        """
        records = []
        for item in self.instances:
            if not item.enabled or not item.kind_is_contingent():
                continue
            demand = item.last_demand
            if demand is None or demand.total_wh <= 0 or demand.max_power_w <= 0:
                continue
            ctx = self._last_ctx_for.get(item.id)
            slots = ctx.slot_count if ctx else len(demand.feasible or [])
            # Read from *now*, not from whenever the cycle last ran: how far into a run
            # or a rest the appliance is decides what the next plan may change.
            committed_on, committed_off = (
                item.commitment(self._at_now(ctx, self._clock()[1])) if ctx else (0, 0)
            )
            records.append({
                "id": item.id,
                "demand_wh": round(float(demand.total_wh), 1),
                "max_power_w": float(demand.max_power_w),
                "value_eur_per_wh": self._value_of(item),
                "feasible": _padded(demand.feasible, slots),
                "min_runtime_slots": item.min_runtime_slots(ctx) if ctx else 1,
                "urgent_wh": float(demand.total_wh) if demand.urgent else 0.0,
                "committed_on_slots": committed_on,
                "committed_off_slots": committed_off,
                "already_running": item.is_running(),
                "max_slots_per_day": item.max_slots_per_day(ctx) if ctx else 0,
                # A label per slot rather than a boundary index: it rides the same
                # rotation into solver space as the feasibility mask, so the solver
                # groups by integers and never has to reason about the clock.
                "day_index": (
                    [index // ctx.slots_per_day() for index in range(slots)]
                    if ctx else []
                ),
                "start_cost_eur": (
                    START_COST_SLOTS * self._value_of(item)
                    * demand.max_power_w * (ctx.hours_per_slot() if ctx else 1.0)
                ),
            })
        return records

    def adopt_schedules(self, schedules, cost=None):
        """
        Take the optimizer's placements and settle each gate on them.

        Loads the optimizer did not answer for keep the plan this module worked out on
        its own - a solver that failed, or was swapped out, must not leave a pool with
        no way to decide anything.

        *cost* is what the household pays extra for them, measured by the optimizer
        against a run without them. It is the figure the price limit promises, so it is
        what gets reported rather than the tariff of the hours they happen to occupy.
        """
        if not isinstance(schedules, dict):
            return 0

        # The optimizer answers far more often than this module polls - every two
        # minutes against a five-minute cycle on a live install - so the context from
        # the last cycle is stale by up to a whole cycle. Its sensor readings and its
        # demand are fine: those are only refreshed on a cycle by design. Its *clock*
        # is not. Settling the gate against a stale `current_slot` releases the load
        # for a slot that has already ended, and a minimum runtime then holds the
        # appliance on for half an hour that nothing ever planned.
        _, now, _ = self._clock()

        adopted = 0
        for item in self.instances:
            if not item.enabled or not item.kind_is_contingent():
                continue
            schedule = schedules.get(item.id)
            if schedule is None:
                continue
            ctx = self._last_ctx_for.get(item.id)
            if ctx is None:
                continue
            ctx = self._at_now(ctx, now)
            release = item.adopt_schedule(schedule, ctx, cost=cost)
            if release is not None:
                self._publish_release(item, release)
                self._record_decision(item, ctx, release, origin=ORIGIN_OPTIMIZER)
                adopted += 1
        return adopted

    def _at_now(self, ctx, now):
        """
        The same context, read from the current moment.

        The slot index stays relative to ``ctx.anchor`` rather than to today's midnight,
        because the plan it will be compared against is indexed from that anchor. Past
        midnight this simply counts on beyond a day, which is what the 192-slot horizon
        already expects.
        """
        current_slot = int((now - ctx.anchor).total_seconds() // self.time_frame_base)
        return replace(ctx, now=now, current_slot=current_slot)

    def _value_of(self, item):
        """
        What a Wh delivered to this load is worth, which is also its price limit.

        With no limit set the figure has to be high enough that the solver always
        prefers running to not running, so the demand is met whatever it costs - but
        finite, so an unreachable target still degrades to a short plan rather than to
        an unbounded objective.
        """
        if item.max_price_eur_per_wh is not None:
            return float(item.max_price_eur_per_wh)
        prices = self._series(self.sources.price, self.horizon_hours) or [0.0]
        return max(max(prices) * 10.0, UNCAPPED_VALUE_EUR_PER_WH)

    def refresh(self, entry_id):
        """
        Re-plan one instance right now, without waiting for the next cycle.

        A pushed forecast has to take effect immediately: the whole point of the
        endpoint is an external system saying "this is what the heating will draw", and
        an optimizer run landing in the five minutes before the next poll would plan the
        battery around a load that is already known to be wrong.

        The shared budget is approximated from what the *other* instances planned last
        cycle, which is exact whenever they have not changed - and they have not, since
        only this one was pushed to. The next full cycle re-derives it either way.
        """
        item = self.instance(entry_id)
        if item is None:
            return False
        if not item.enabled:
            self.registry.drop(item.id)
            return False

        ctx_base = self._context()
        budget = self._initial_budget(ctx_base)
        if budget is not None:
            for other in self.instances:
                if other.id != item.id:
                    self._consume_budget(budget, other.last_plan)

        ctx = self._context_for(item, ctx_base)
        contribution, release = item.evaluate(ctx, budget_wh=budget)
        self._store_result(item, contribution, release)
        return True

    def _store_result(self, item, contribution, release):
        """Publish one instance's result to the registry and the outside world."""
        if contribution is not None:
            self.registry.set(contribution)
        else:
            self.registry.drop(item.id)
        self._publish_release(item, release)

    def _record_decision(self, item, ctx, release, origin):
        """
        Journal one release decision and the plan behind it.

        Purely diagnostic - nothing reads this back to run the house. It exists because
        a toggling appliance is almost impossible to explain after the fact from a
        release signal alone: the signal says *what* happened and never *which plan*
        said so, and the plan that said so has been replaced by the time anyone looks.
        """
        if self.store is None or release is None:
            return

        plan = item.last_plan or []
        slots = [index for index, value in enumerate(plan) if value]
        demand = item.last_demand
        record = {
            "origin": origin,
            "released": bool(release.get("released")),
            "reason": release.get("reason", ""),
            "current_slot": ctx.current_slot,
            "planned_now": bool(
                0 <= ctx.current_slot < len(plan) and plan[ctx.current_slot] > 0
            ),
            # A short digest of *which* slots, so churn is one GROUP BY rather than a
            # walk over a week of arrays.
            "plan_hash": hashlib.md5(
                str(slots).encode("utf-8")
            ).hexdigest()[:8] if slots else "",
            "anchor": ctx.anchor.isoformat(),
            "planned_slots": slots,
            "planned_wh": round(sum(plan), 1),
            "demand_wh": round(float(demand.total_wh), 1) if demand else 0.0,
            "demand_reason": demand.reason if demand else "",
            "next_release_start": release.get("next_release_start"),
            "price_eur_per_wh": getattr(demand, "plan_price_eur_per_wh", None)
            if demand else None,
        }
        try:
            self.store.record_decision(item.id, ctx.now, record)
        except Exception:  # pylint: disable=broad-except
            # A journal that cannot be written must never stop the house being run.
            logger.exception("[LOADS] could not journal the decision for '%s'", item.id)

    # -- context ---------------------------------------------------------------------------

    def _wall_clock(self):
        return datetime.now(self.time_zone) if self.time_zone else datetime.now()

    def _clock(self):
        now = self._now()
        anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)
        slot_count = self.horizon_hours * 3600 // self.time_frame_base
        return anchor, now, slot_count

    def _context(self):
        anchor, now, slot_count = self._clock()
        seconds = (now - anchor).total_seconds()
        current_slot = int(seconds // self.time_frame_base)

        return DemandContext(
            now=now,
            anchor=anchor,
            slot_count=slot_count,
            time_frame_base=self.time_frame_base,
            current_slot=current_slot,
            price_eur_per_wh=self._prices(slot_count),
            feed_in_eur_per_wh=self._series(self.sources.feed_in_price, slot_count),
            pv_surplus_wh=self._surplus(slot_count),
            solar_wh=self._series(self.sources.pv_forecast, slot_count),
            ambient_temp_c=[],
            readings={},
        )

    def _context_for(self, item, ctx_base):
        """Per-instance context: its own sensors and its own ambient series."""
        readings = self._read_sensors(item)
        measured, forecast_now = self._ambient_inputs(item, ctx_base, readings)
        ambient, ambient_source = self._ambient_series(item, ctx_base, readings)
        return DemandContext(
            now=ctx_base.now,
            anchor=ctx_base.anchor,
            slot_count=ctx_base.slot_count,
            time_frame_base=ctx_base.time_frame_base,
            current_slot=ctx_base.current_slot,
            price_eur_per_wh=ctx_base.price_eur_per_wh,
            feed_in_eur_per_wh=ctx_base.feed_in_eur_per_wh,
            pv_surplus_wh=ctx_base.pv_surplus_wh,
            solar_wh=ctx_base.solar_wh,
            ambient_temp_c=ambient,
            ambient_source=ambient_source,
            ambient_measured_c=measured,
            ambient_forecast_c=forecast_now,
            readings=readings,
        )

    def _read_sensors(self, item):
        readings = {}
        for key in getattr(item.model, "sensor_keys", ()):
            sensor = str(item.config.get(key, "") or "").strip()
            if not sensor:
                continue
            try:
                readings[key] = self.sources.read_sensor(sensor)
            except Exception:  # pylint: disable=broad-except
                # A sensor that is briefly unavailable is normal; the model treats a
                # missing reading as "no demand" and the next cycle tries again.
                logger.debug(
                    "[LOADS] '%s' could not read %s (%s)", item.id, key, sensor,
                    exc_info=True,
                )
        return readings

    def _ambient_inputs(self, item, ctx, readings):
        """The raw sensor reading and the raw forecast for the current slot."""
        measured = self._sensor_ambient(readings)
        forecast = None
        if uses_outdoor_ambient(item.type):
            hourly = self.sources.temperature_forecast() or []
            if hourly:
                raw = self._expand_hourly(hourly, ctx.slot_count)
                slot = min(max(0, ctx.current_slot), len(raw) - 1)
                forecast = raw[slot]
        return measured, forecast

    def _ambient_series(self, item, ctx, readings):
        """
        The ambient temperature this instance sees, one value per slot.

        Returns ``(series, source)`` - the values, and which of the three they came
        from, so the page can say what the prediction is standing on.

        Three sources, best first:

        1. a real outdoor forecast, which is the only one that can see into tomorrow -
           and the horizon is two days long;
        2. the instance's own ambient sensor, held flat. It cannot see ahead, but it is
           a measurement of this actual site;
        3. the preset's constant, which is a guess and is treated as one.

        The provider must return nothing rather than a placeholder for step 1 to fall
        through correctly. It did not, at first: the static 15 degree default that
        stands in for an unfetched forecast is indistinguishable from a real one, so a
        pool with a perfectly good outdoor sensor configured was modelled against a
        fiction - both its losses and its efficiency.
        """
        measured = self._sensor_ambient(readings)

        if uses_outdoor_ambient(item.type):
            hourly = self.sources.temperature_forecast() or []
            if hourly:
                raw = self._expand_hourly(hourly, ctx.slot_count)
                return self._corrected_forecast(item, raw, measured, ctx)

        if measured is not None:
            return [measured] * ctx.slot_count, AMBIENT_SENSOR

        if uses_outdoor_ambient(item.type):
            self._warn_missing_ambient(item)
        return [fallback_ambient_c(item.type)] * ctx.slot_count, AMBIENT_FALLBACK

    @staticmethod
    def _sensor_ambient(readings):
        """The instance's own outdoor reading, or None."""
        raw = readings.get("ambient_temp_sensor")
        if raw is None:
            return None
        try:
            return float(str(raw).strip().split()[0])
        except (ValueError, IndexError):
            return None

    def _corrected_forecast(self, item, raw, measured, ctx):
        """
        The forecast shifted onto this site, hour by hour.

        Each slot takes the offset learned for its own hour of the day. One number for
        the whole horizon cannot describe a site that runs 4 K cold overnight and close
        to the model by mid-afternoon - it splits the difference and is wrong at both
        ends, and at the current cap it applied a night-time correction to tomorrow's
        afternoon.
        """
        bias = self._ambient_bias.get(item.id)
        if bias is None:
            bias = self._ambient_bias[item.id] = AmbientBias()

        slot = min(max(0, ctx.current_slot), len(raw) - 1)
        forecast_now = raw[slot]
        if measured is not None:
            bias.observe(ctx.now, measured, forecast_now)
            self._warn_if_implausible(item, measured - forecast_now)

        slots_per_day = max(1, 86400 // ctx.time_frame_base)
        slots_per_hour = max(1, ctx.slots_per_hour())
        corrected = []
        for index, value in enumerate(raw):
            hour = (index % slots_per_day) // slots_per_hour
            corrected.append(value + bias.offset(hour))

        # What the thermometer says now, and how far that carries.
        #
        # The slot happening now needs no predicting at all - it takes the reading. The
        # rest of today is shifted by the same departure, fading with distance, because
        # an anomaly is a property of the day and not of the instant: it is what stops
        # the planner sizing energy into hours that will be too cold to use it.
        #
        # The projection is clamped where the reading itself is not - the current slot
        # is a measurement and stands, but a sensor reporting nonsense must not drag a
        # two-day horizon with it.
        if measured is not None and 0 <= slot < len(corrected):
            residual = self._smoothed_residual(item, ctx, measured - corrected[slot])
            projected = max(-MAX_OFFSET_K, min(MAX_OFFSET_K, residual))
            hours_per_slot = ctx.time_frame_base / 3600.0
            for index in range(slot, len(corrected)):
                ahead = (index - slot) * hours_per_slot
                corrected[index] += projected * 0.5 ** (ahead / NOWCAST_HALF_LIFE_HOURS)

        self._ambient_trace[item.id] = (list(raw), list(corrected))

        source = AMBIENT_CORRECTED if bias.offset(
            (slot % slots_per_day) // slots_per_hour
        ) else AMBIENT_FORECAST
        return corrected, source

    def _remember_medium(self, entry_id, when, medium_c):
        """Keep the recent store temperature, bounded, for the card to draw."""
        if when is None or medium_c is None:
            return
        try:
            value = float(medium_c)
        except (TypeError, ValueError):
            return
        seen = self._medium_history.get(entry_id)
        if seen is None:
            seen = self._medium_history[entry_id] = deque(maxlen=MEDIUM_HISTORY_MAX)
        seen.append((when, value))

    def medium_series_state(self, item, ctx):
        """
        Where the store's temperature has been, and where the plan takes it.

        The pair is the answer to "why does it want so much energy?" - the climb to
        target and the losses on the way are one number in the demand and two quite
        different shapes here.
        """
        if ctx is None or not hasattr(item.model, "project_medium"):
            return None
        current = item.last_demand.detail.get("temperature_c") if item.last_demand else None
        if current is None:
            return None

        history = [None] * ctx.slot_count
        for when, value in self._medium_history.get(item.id, ()):
            index = ctx.slot_of(when) if hasattr(ctx, "slot_of") else None
            if index is None:
                offset = (when - ctx.anchor).total_seconds()
                index = int(offset // ctx.time_frame_base)
            if 0 <= index < ctx.slot_count:
                history[index] = round(float(value), 2)

        projected = item.model.project_medium(ctx, item.last_plan or [], current)
        target = item.last_demand.detail.get("target_temperature_c")
        if not projected:
            return None
        return {
            "history_c": history,
            "projected_c": projected,
            "target_c": None if target is None else round(float(target), 2),
        }

    def _smoothed_residual(self, item, ctx, gap):
        """
        Today's departure from the typical day, filtered.

        The first reading is taken whole - there is nothing to average it against, and
        after a restart the old value means nothing. Afterwards each one moves the
        estimate by however much of a half-life has passed, so the filter follows wall
        clock rather than cycle count and a change of poll interval cannot retune it.

        A cycle that arrives no later than the last one still has to count for
        something, or a fixed clock would freeze the estimate forever; one poll
        interval is the floor.
        """
        previous = self._ambient_residual.get(item.id)
        if previous is None:
            smoothed = gap
        else:
            seen_at, value = previous
            minutes = max(
                (ctx.now - seen_at).total_seconds() / 60.0,
                self.cycle_seconds / 60.0,
            )
            keep = 0.5 ** (minutes / RESIDUAL_HALF_LIFE_MIN)
            smoothed = value * keep + gap * (1.0 - keep)
        self._ambient_residual[item.id] = (ctx.now, smoothed)
        return smoothed

    def ambient_series_state(self, item):
        """
        The forecast as retrieved and as used, for the card to draw.

        Two readings of the same quantity, so the card separates them by line style
        rather than by colour - there is no third categorical hue to spend beside the
        three the slot strip already uses, and a reader comparing "what the model was
        told" with "what it believes" is not comparing two different things.
        """
        trace = self._ambient_trace.get(item.id)
        if not trace:
            return None
        raw, used = trace
        cut_off = item.config.get("min_ambient_temp_c")
        return {
            "forecast_c": [round(float(v), 1) for v in raw],
            "adapted_c": [round(float(v), 1) for v in used],
            "min_ambient_c": None if cut_off is None else float(cut_off),
        }

    def ambient_bias_state(self, entry_id):
        """What has been learned about this site's offset, for the API."""
        bias = self._ambient_bias.get(entry_id)
        return bias.state() if bias else None

    def _warn_if_implausible(self, item, gap):
        """Say once when the sensor and the forecast cannot be describing the same air."""
        if abs(gap) <= IMPLAUSIBLE_GAP_K or item.id in self._warned_bias:
            return
        self._warned_bias.add(item.id)
        logger.warning(
            "[LOADS] '%s' has an ambient sensor reading %.1f C away from the outdoor "
            "forecast. One of them is not measuring outdoor air at this site, so the "
            "reading is being ignored rather than corrected for. "
            "| Config: #managed-loads",
            item.id, gap,
        )

    def _warn_missing_ambient(self, item):
        """
        Say once that an outdoor load is running on a guessed air temperature.

        Silently assuming a constant is the worst outcome available: the forecast looks
        like it is working, and every number it produces is wrong in a way nothing on
        the page reveals.
        """
        if item.id in self._warned_ambient:
            return
        self._warned_ambient.add(item.id)
        logger.warning(
            "[LOADS] '%s' sits outdoors but has no outdoor temperature to work from, so "
            "it is running on a fixed %.0f C and its energy prediction cannot follow the "
            "weather. Either set an ambient_temp_sensor on the load, or give the "
            "installation a Latitude and Longitude under System so the temperature "
            "forecast can be fetched. | Config: #managed-loads | ACTION REQUIRED",
            item.id,
            fallback_ambient_c(item.type),
        )

    def _expand_hourly(self, hourly, slot_count):
        """
        Bring a temperature series onto the running slot grid.

        The provider already publishes it at the optimizer's resolution, so the length
        is the thing to read rather than an assumption to make. Expanding an array that
        was already 15-minute would have described the first twelve hours of the
        forecast as if they were the whole two days - invisible at hourly resolution,
        and wrong at quarter-hourly.
        """
        if not hourly:
            return [15.0] * slot_count
        if len(hourly) >= slot_count:
            return self._fit(list(hourly), slot_count, fallback=15.0)

        factor = max(1, round(slot_count / len(hourly)))
        expanded = []
        for value in hourly:
            expanded.extend([value] * factor)
        return self._fit(expanded, slot_count, fallback=15.0)

    def _prices(self, slot_count):
        """
        The grid price series, or nothing at all when it has not arrived yet.

        An all-zero series is not a tariff, it is an interface that has not fetched
        anything - and it reads to the planner as free electricity, so the price cap
        stops binding and every slot in the horizon gets planned at 0 ct. Handing back
        an empty list instead lets the planner substitute its "unknown price" value,
        which is above any tariff, so an unpriced cycle places nothing rather than
        everything. The next cycle has real numbers.
        """
        prices = self._series(self.sources.price, slot_count)
        if prices and any(value for value in prices):
            return prices
        if not self._warned_no_prices:
            self._warned_no_prices = True
            # Expected once at startup: this thread and the price interface come up
            # together and either may win. It only deserves a warning if it is still
            # true after that, because then something is actually wrong.
            if self.stats.cycles == 0:
                logger.info(
                    "[LOADS] electricity prices have not arrived yet - waiting for "
                    "them before placing anything"
                )
            else:
                logger.warning(
                    "[LOADS] no electricity prices available - managed loads will not "
                    "be placed until they arrive"
                )
        return []

    def _series(self, provider, slot_count, fallback=0.0):
        try:
            values = provider() or []
        except Exception:  # pylint: disable=broad-except
            logger.debug("[LOADS] a forecast provider failed", exc_info=True)
            values = []
        return self._fit(list(values), slot_count, fallback)

    @staticmethod
    def _fit(values, slot_count, fallback):
        """Pad or truncate a provider's series to the horizon."""
        if not values:
            return [fallback] * slot_count
        out = list(values[:slot_count])
        while len(out) < slot_count:
            out.append(out[-1])
        return out

    def has_pv_counter(self):
        """Whether a cumulative PV meter is readable, for the card to say so."""
        return self._pv_counter() is not None

    def _hint_pv_counter(self, item):
        """
        Say once that a PV meter would sharpen the model, without nagging.

        Not a warning: the PV forecast stands in perfectly well, and the calibration
        works either way. But a meter differenced across a window measures exactly that
        window, where a forecast is a guess about a whole slot, so a store that sits in
        the sun learns what the sun does to it rather more precisely.
        """
        if self._hinted_pv_counter:
            return
        self._hinted_pv_counter = True
        logger.info(
            "[LOADS] '%s' sits outdoors and is learning what the sun adds to it from "
            "the PV *forecast*. Setting a cumulative PV generation counter under PV "
            "Auto-Scaling would measure that from the meter instead, which is more "
            "precise. Nothing is wrong without it. | Config: #pv-autoscaling",
            item.id,
        )

    def _pv_counter(self):
        """The cumulative PV meter, or None when the site has not got one."""
        try:
            value = self.sources.pv_counter_kwh()
        except Exception:  # pylint: disable=broad-except
            logger.debug("[LOADS] PV counter unreadable", exc_info=True)
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _forecast_solar_w(self, ctx):
        """
        The forecast's irradiance proxy for this slot, as a fallback for the counter.

        Weaker - a forecast, and a single point on it - but a site with no PV meter can
        still tell a bright window from a dark one, which is all the fit needs.
        """
        series = ctx.solar_wh or []
        if not series or not 0 <= ctx.current_slot < len(series):
            return 0.0
        return max(0.0, float(series[ctx.current_slot])) / max(
            1e-9, ctx.hours_per_slot()
        )

    def _surplus(self, slot_count):
        """PV generation left after the household base load, per slot."""
        pv = self._series(self.sources.pv_forecast, slot_count)
        base = self._series(self.sources.base_load, slot_count)
        return [max(0.0, generated - consumed) for generated, consumed in zip(pv, base)]

    # -- budget ----------------------------------------------------------------------------

    def _initial_budget(self, ctx):
        """
        Per-slot energy every managed load shares, or None when unlimited.

        Without it two planners both pick the single cheapest slot and stack a sauna on
        top of a pool pump, forecasting a peak the house cannot draw and telling the
        optimizer to size the battery for it.
        """
        if self.max_power_w <= 0:
            return None
        per_slot = self.max_power_w * ctx.time_frame_base / 3600.0
        return [per_slot] * ctx.slot_count

    @staticmethod
    def _consume_budget(budget, plan):
        if budget is None or not plan:
            return
        for index, value in enumerate(plan):
            if index < len(budget):
                budget[index] = max(0.0, budget[index] - value)

    def check_power_sensors(self):
        """
        Warn once about a power sensor that is actually counting energy.

        Nothing downstream can detect this from the value: an energy counter reads as a
        large, slowly rising wattage, so the appliance appears to be running
        permanently and every efficiency sample it produces is wrong. The number alone
        cannot say which it is, so the unit is what gets checked.

        The "| Config:" and "| ACTION REQUIRED" suffixes are what the alerts panel turns
        into a deep link to the setting - see ``parseAlertMeta`` in web/js/main.js.
        """
        for item in self.instances:
            sensor = item.power_sensor or item.replaces_sensor
            if not item.enabled or not sensor:
                continue
            try:
                details = self.sources.read_sensor_details(sensor)
            except Exception:  # pylint: disable=broad-except
                # A sensor that is briefly unreachable is not a configuration problem.
                logger.debug(
                    "[LOADS] could not inspect the power sensor for '%s'", item.id,
                    exc_info=True,
                )
                continue

            if not isinstance(details, dict):
                continue

            unit = str(details.get("unit", "")).strip().lower()
            device_class = str(details.get("device_class", "")).strip().lower()
            if unit in ENERGY_UNITS or device_class == "energy":
                # An externally fed load measures no efficiency, so naming one would be
                # nonsense - but the reading still has to be watts, because it is the
                # history that leaves the household base load.
                consequence = (
                    "otherwise too much is taken out of the household base load and the "
                    "forecast comes out far too low"
                    if item.replaces_sensor and not item.power_sensor
                    else "otherwise the appliance looks permanently on and its measured "
                         "efficiency is meaningless"
                )
                logger.warning(
                    "[LOADS] '%s' has %s '%s' reporting %s, which is energy, "
                    "not power. It must report watts - %s. In Home "
                    "Assistant, add a derivative helper and point this at that. "
                    "| Config: #managed-loads | ACTION REQUIRED",
                    item.id,
                    "replaces_sensor" if item.replaces_sensor and not item.power_sensor
                    else "power_sensor",
                    sensor,
                    details.get("unit") or device_class,
                    consequence,
                )

    # -- sampling and calibration ------------------------------------------------------------

    def _record_sample(self, item, ctx):
        """Record one observation and let the model learn from it."""
        temperature = ctx.readings.get("temp_sensor")
        if temperature is None:
            return

        ambient = ctx.ambient_temp_c[ctx.current_slot] if ctx.ambient_temp_c else None
        try:
            sample = {
                "timestamp": ctx.now,
                "medium_c": float(str(temperature).strip().split()[0]),
                "ambient_c": float(ambient) if ambient is not None else None,
                "power_w": self._reading_as_float(ctx.readings.get("power_sensor"), 0.0),
                "cover_factor": item.model.cover_factor(ctx.readings)
                if hasattr(item.model, "cover_factor") else 1.0,
                # The state, not the multiplier. The multiplier is now an output of the
                # calibration, and feeding it back in as an input would close a loop.
                "covered": bool(item.model.is_covered(ctx.readings))
                if hasattr(item.model, "is_covered") else False,
                # Kept alongside the value actually used, so the site-versus-model
                # offset can be relearned after a restart instead of starting over.
                # The counter, not a power: differencing two readings across a window
                # averages exactly the window, where a point sample of a passing cloud
                # would speak for the whole of it.
                "pv_counter_kwh": self._pv_counter(),
                "solar_w": self._forecast_solar_w(ctx),
                "ambient_measured_c": ctx.ambient_measured_c,
                "ambient_forecast_c": ctx.ambient_forecast_c,
            }
        except (ValueError, IndexError, TypeError):
            return

        if sample["ambient_c"] is None:
            return

        self.stats.samples_recorded += 1
        self._remember_medium(item.id, sample.get("timestamp"), sample.get("medium_c"))
        if item.observe(sample):
            self.stats.calibration_updates += 1

        if self.store is not None:
            try:
                self.store.record_sample(item.id, sample)
                self.store.save_model_state(item.id, item.model.status())
            except Exception:  # pylint: disable=broad-except
                logger.debug("[LOADS] could not persist a sample", exc_info=True)

    @staticmethod
    def _reading_as_float(value, default=0.0):
        if value is None:
            return default
        try:
            return float(str(value).strip().split()[0])
        except (ValueError, IndexError):
            return default

    def backfill(self, days=14):
        """
        Replay recorded history into the calibrators at startup.

        Without this a fresh container starts from the configured guesses again every
        restart, and the estimate never settles on a system that gets updated weekly.
        """
        if self.store is None:
            return 0
        restored = 0
        for item in self.instances:
            state = self.store.load_model_state(item.id)
            if state and hasattr(item.model, "calibrator"):
                item.model.calibrator.restore(state)
            samples = self.store.load_samples(item.id, days=days)
            if samples and hasattr(item.model, "observe_history"):
                restored += item.model.observe_history(samples)
            self._replay_ambient_bias(item, samples)
            for sample in samples:
                self._remember_medium(
                    item.id, sample.get("timestamp"), sample.get("medium_c")
                )
        if restored:
            logger.info(
                "[LOADS] calibration seeded from %d recorded sample pairs", restored
            )
        return restored

    # -- external control --------------------------------------------------------------------

    def _replay_ambient_bias(self, item, samples):
        """
        Rebuild this site's per-hour offset from recorded history.

        Samples written before the measurement was stored simply carry neither field and
        are skipped, so an upgraded install relearns over a day rather than failing.
        """
        if not samples or not uses_outdoor_ambient(item.type):
            return
        bias = self._ambient_bias.get(item.id)
        if bias is None:
            bias = self._ambient_bias[item.id] = AmbientBias()
        for sample in samples:
            bias.observe(
                sample.get("timestamp"),
                sample.get("ambient_measured_c"),
                sample.get("ambient_forecast_c"),
            )

    def push(self, entry_id, payload, source=SOURCE_API):
        """
        Accept a pushed profile or contingent for one instance.

        Raises `InjectionError` with a user-facing message; the REST layer turns it into
        a 400 and the MQTT layer logs it, because a published message has nowhere to
        return an error to.
        """
        item = self.instance(entry_id)
        if item is None:
            raise InjectionError(f"no managed load with id '{entry_id}'")
        if item.type not in EXTERNAL_TYPES:
            raise InjectionError(
                f"managed load '{entry_id}' is of type '{item.type}' and computes its "
                "own demand - only external types accept pushed data"
            )
        if pulls_its_profile(item.config):
            # Accepting it would work for exactly one cycle and then be overwritten by
            # the next fetch, which looks like the push was lost rather than refused.
            raise InjectionError(
                f"managed load '{entry_id}' fetches its profile from the source "
                "configured for it - set its profile source back to 'push' to hand it "
                "one instead"
            )

        anchor, now, slot_count = self._clock()
        current_slot = int((now - anchor).total_seconds() // self.time_frame_base)
        parsed = parse_push(
            payload,
            anchor=anchor,
            slot_count=slot_count,
            time_frame_base=self.time_frame_base,
            current_slot=current_slot,
            default_ttl_minutes=item.config.get("ttl_minutes", 1440),
            now=now,
        )
        item.accept_push(parsed, anchor, self.time_frame_base, source=source)
        logger.info("[LOADS] '%s' accepted a push via %s", entry_id, source)
        self.refresh(entry_id)
        return parsed

    def _pull_profile(self, item, ctx):
        """
        Fetch one load's profile from the source the user named.

        A failed fetch keeps whatever was last fetched rather than clearing it. Home
        Assistant restarts, and blanking the forecast for the minutes that takes would
        swing the battery plan on nothing more than a reboot. The staleness bound is
        ``ttl_minutes``, which the model enforces on its own: if the source stays gone
        long enough, the profile expires and the load simply stops contributing.
        """
        if not pulls_its_profile(item.config):
            return

        try:
            fetched = self.sources.read_profile(item.config)
        except (ValueError, TypeError, KeyError) as exc:
            self._warn_once(item.id, "pull", str(exc))
            return
        except Exception:  # pylint: disable=broad-except
            # A network layer can raise anything at all, and one unreachable sensor
            # must not stop the other managed loads - or the house - being planned.
            logger.exception("[LOADS] '%s' could not fetch its profile", item.id)
            return

        if not fetched:
            return

        try:
            profile = profile_from_entries(
                fetched.get("entries") or [],
                resolution_seconds=fetched.get("resolution_seconds") or 3600,
                anchor=ctx.anchor,
                slot_count=ctx.slot_count,
                time_frame_base=ctx.time_frame_base,
                valid_until=ttl_from_minutes(
                    item.config.get("ttl_minutes", 1440), now=ctx.now
                ),
            )
        except InjectionError as exc:
            self._warn_once(item.id, "format", str(exc))
            return

        self._warned_pull.discard((item.id, "pull"))
        self._warned_pull.discard((item.id, "format"))
        item.accept_push(profile, ctx.anchor, self.time_frame_base, source=SOURCE_PULL)
        logger.debug(
            "[LOADS] '%s' fetched %d entries, %.0f Wh over the horizon",
            item.id, profile.source_length, sum(profile.slots_wh),
        )

    def _warn_once(self, entry_id, kind, message):
        """
        Report a failing fetch the first time and then stay quiet about it.

        The cycle runs every few minutes; a source that is down for a day would
        otherwise write the same line three hundred times and bury everything else.
        """
        if (entry_id, kind) in self._warned_pull:
            logger.debug("[LOADS] '%s' still cannot fetch its profile: %s",
                         entry_id, message)
            return
        self._warned_pull.add((entry_id, kind))
        logger.warning(
            "[LOADS] '%s' could not fetch its profile: %s. The last one stays in the "
            "forecast until it expires. | Config: #managed-loads",
            entry_id, message,
        )

    def clear_push(self, entry_id):
        """Drop an instance's pushed data, so it stops contributing immediately."""
        item = self.instance(entry_id)
        if item is None:
            raise InjectionError(f"no managed load with id '{entry_id}'")
        cleared = item.clear_push()
        if cleared:
            self.registry.drop(entry_id)
            self.refresh(entry_id)
        return cleared

    def reset_calibration(self, entry_id):
        """
        Throw away what a load has learned and start again from its configuration.

        Needed when the inputs the calibration was fitted against turn out to have been
        wrong - a store that was learning against a placeholder ambient temperature has
        recorded samples that will drag the fit for as long as they are retained, and no
        amount of good data arriving later undoes that quickly.
        """
        item = self.instance(entry_id)
        if item is None:
            raise InjectionError(f"no managed load with id '{entry_id}'")
        if not hasattr(item.model, "reset_calibration"):
            raise InjectionError(
                f"managed load '{entry_id}' is of type '{item.type}' and learns nothing"
            )

        if self.store is not None:
            try:
                self.store.forget(entry_id)
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "[LOADS] could not clear the recorded samples for '%s'", entry_id
                )

        item.model.reset_calibration()
        logger.info("[LOADS] '%s' calibration reset to its configured values", entry_id)
        self.refresh(entry_id)
        return item.model.status()

    def set_override(self, entry_id, mode, minutes):
        """Force one instance released or blocked for *minutes*, or clear it."""
        item = self.instance(entry_id)
        if item is None:
            raise InjectionError(f"no managed load with id '{entry_id}'")
        _, now, _ = self._clock()
        item.set_override(mode, minutes, now)
        # An override that only reaches the appliance at the next poll is not much of an
        # override - the user pressed the button because they want it now.
        self.refresh(entry_id)
        return item.gate.status()

    # -- reporting ------------------------------------------------------------------------------

    def _publish_release(self, item, release):
        """Notify the outside world, but only when the decision actually changed."""
        if release is None or self.on_release_change is None:
            return
        previous = self._published_release.get(item.id)
        signature = (release.get("released"), release.get("reason"),
                     release.get("next_release_start"))
        if previous == signature:
            return
        self._published_release[item.id] = signature
        try:
            self.on_release_change(item.id, release)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[LOADS] publishing the release state for '%s' failed", item.id)

    def _load_status(self, item):
        """One load's status, plus what the manager knows about its site."""
        status = item.status()
        bias = self.ambient_bias_state(item.id)
        if bias is not None:
            status["ambient_bias"] = bias
        series = self.ambient_series_state(item)
        if series is not None:
            status["ambient_series"] = series
        medium = self.medium_series_state(item, self._last_ctx_for.get(item.id))
        if medium is not None:
            status["water_series"] = medium
        return status

    def status(self):
        """The whole subsystem, for `GET /api/managed_loads` and the dashboard."""
        return {
            "enabled": bool(self._instances),
            # Which rule the price limit is under. The optimizer places these loads
            # against the battery and the household together and runs them whenever the
            # energy genuinely costs less; without it the load falls back to skipping
            # any hour above the figure, which is blunter and worth saying out loud.
            "scheduled_by_optimizer": bool(self.external_scheduler),
            # Which irradiance proxy the calibration is learning the sun from.
            "pv_counter_available": self.has_pv_counter(),
            "time_frame_base": self.time_frame_base,
            "cycle_seconds": self.cycle_seconds,
            "max_power_w": self.max_power_w,
            "contribution_total_wh": self.contribution_total_wh(),
            "cycles": self.stats.cycles,
            "last_cycle": self.stats.last_cycle,
            "last_error": self.stats.last_error,
            "samples_recorded": self.stats.samples_recorded,
            "calibration_updates": self.stats.calibration_updates,
            "contributions": self.registry.snapshot(),
            "loads": [self._load_status(item) for item in self.instances],
        }
