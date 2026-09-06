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


@dataclass
class ManagedLoadSources:
    """
    Everything the manager needs from the rest of the application.

    Every entry is optional and defaults to "nothing available", so a partially
    configured system degrades to a smaller feature rather than to a traceback.
    """

    read_sensor: object = _noop           # (sensor_name) -> raw state or None
    read_history: object = _noop          # (sensor, start, end) -> [{state, last_updated}]
    price: object = _noop                 # () -> [EUR/Wh per slot]
    feed_in_price: object = _noop         # () -> [EUR/Wh per slot]
    pv_forecast: object = _noop           # () -> [Wh per slot]
    base_load: object = _noop             # () -> [Wh per slot]
    temperature_forecast: object = _noop  # () -> [degrees C, hourly]


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

    # -- the optimizer-facing call ---------------------------------------------------------

    def apply(self, gesamtlast, time_frame_base=None):
        """
        Add every active contribution to a household load profile.

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

        for item in self.instances:
            if not item.enabled:
                # Not just skipped: a load switched off has to stop contributing now,
                # not when its contribution happens to expire. Until this dropped, the
                # optimizer kept planning around an appliance the user had disabled.
                self.registry.drop(item.id)
                continue
            ctx = self._context_for(item, ctx_base)
            self._record_sample(item, ctx)
            contribution, release = item.evaluate(ctx, budget_wh=budget)
            self._store_result(item, contribution, release)
            self._consume_budget(budget, item.last_plan)

        self.stats.cycles += 1
        self.stats.last_cycle = ctx_base.now.isoformat()
        return self.stats.cycles

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
            price_eur_per_wh=self._series(self.sources.price, slot_count),
            feed_in_eur_per_wh=self._series(self.sources.feed_in_price, slot_count),
            pv_surplus_wh=self._surplus(slot_count),
            ambient_temp_c=[],
            readings={},
        )

    def _context_for(self, item, ctx_base):
        """Per-instance context: its own sensors and its own ambient series."""
        readings = self._read_sensors(item)
        ambient = self._ambient_series(item, ctx_base, readings)
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

    def _ambient_series(self, item, ctx, readings):
        """
        The ambient temperature this instance sees, one value per slot.

        A pool sits outdoors, so it uses the outdoor forecast already fetched for the
        optimizer - the same data j4rvisstant pointed out is the missing input. A tank
        in a cellar does not care about the weather, so it uses its own sensor, or a
        constant when it has none.
        """
        if uses_outdoor_ambient(item.type):
            hourly = self.sources.temperature_forecast() or []
            if hourly:
                return self._expand_hourly(hourly, ctx.slot_count)

        from_sensor = readings.get("ambient_temp_sensor")
        if from_sensor is not None:
            try:
                value = float(str(from_sensor).strip().split()[0])
                return [value] * ctx.slot_count
            except (ValueError, IndexError):
                pass

        return [fallback_ambient_c(item.type)] * ctx.slot_count

    def _expand_hourly(self, hourly, slot_count):
        """Stretch an hourly forecast over the running slot resolution."""
        per_hour = max(1, 3600 // self.time_frame_base)
        expanded = []
        for value in hourly:
            expanded.extend([value] * per_hour)
        return self._fit(expanded, slot_count, fallback=15.0)

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
        if restored:
            logger.info(
                "[LOADS] calibration seeded from %d recorded sample pairs", restored
            )
        return restored

    # -- external control --------------------------------------------------------------------

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

    def status(self):
        """The whole subsystem, for `GET /api/managed_loads` and the dashboard."""
        return {
            "enabled": bool(self._instances),
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
            "loads": [item.status() for item in self.instances],
        }
