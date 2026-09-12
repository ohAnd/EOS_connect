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

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from .ambient_bias import IMPLAUSIBLE_GAP_K, AmbientBias
from .contribution import SOURCE_API, LoadContributionRegistry
from .injection import InjectionError, parse_push
from .instance import ManagedLoad
from .models.base import DemandContext
from .presets import EXTERNAL_TYPES, fallback_ambient_c, uses_outdoor_ambient

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


# What a Wh is worth to a load with no price limit set: high enough that the solver
# always prefers running, low enough to stay a number rather than an unbounded reward.
# 10 EUR/kWh is two orders above any tariff anyone has.
UNCAPPED_VALUE_EUR_PER_WH = 0.01


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
        self._warned_bias = set()
        self._warned_no_prices = False
        # Set when the optimizer can place contingent loads itself, which changes what
        # this module does with them: it computes the demand and defers the placing.
        self.external_scheduler = False
        self._last_ctx = None
        self._last_ctx_for = {}
        # Per-hour forecast-versus-sensor offset, one learner per instance.
        self._ambient_bias = {}
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
        """
        return [
            item.power_sensor
            for item in self.instances
            if item.enabled and item.subtract_from_base_load and item.power_sensor
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
            records.append({
                "id": item.id,
                "demand_wh": round(float(demand.total_wh), 1),
                "max_power_w": float(demand.max_power_w),
                "value_eur_per_wh": self._value_of(item),
                "feasible": _padded(demand.feasible, slots),
                "min_runtime_slots": item.min_runtime_slots(ctx) if ctx else 1,
                "urgent_wh": float(demand.total_wh) if demand.urgent else 0.0,
            })
        return records

    def adopt_schedules(self, schedules):
        """
        Take the optimizer's placements and settle each gate on them.

        Loads the optimizer did not answer for keep the plan this module worked out on
        its own - a solver that failed, or was swapped out, must not leave a pool with
        no way to decide anything.
        """
        if not isinstance(schedules, dict):
            return 0
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
            release = item.adopt_schedule(schedule, ctx)
            if release is not None:
                self._publish_release(item, release)
                adopted += 1
        return adopted

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

        source = AMBIENT_CORRECTED if bias.offset(
            (slot % slots_per_day) // slots_per_hour
        ) else AMBIENT_FORECAST
        return corrected, source

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
            logger.warning(
                "[LOADS] no electricity prices available yet - managed loads will not "
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
            if not item.enabled or not item.power_sensor:
                continue
            try:
                details = self.sources.read_sensor_details(item.power_sensor)
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
                logger.warning(
                    "[LOADS] '%s' has power_sensor '%s' reporting %s, which is energy, "
                    "not power. It must report watts - otherwise the appliance looks "
                    "permanently on and its measured efficiency is meaningless. In Home "
                    "Assistant, add a derivative helper and point this at that. "
                    "| Config: #managed-loads | ACTION REQUIRED",
                    item.id,
                    item.power_sensor,
                    details.get("unit") or device_class,
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
                "ambient_measured_c": ctx.ambient_measured_c,
                "ambient_forecast_c": ctx.ambient_forecast_c,
            }
        except (ValueError, IndexError, TypeError):
            return

        if sample["ambient_c"] is None:
            return

        self.stats.samples_recorded += 1
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
