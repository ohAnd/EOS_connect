"""
The demand model behind every stored-heat appliance.

A pool heat pump, a sauna, a hot water tank and a buffer tank pose the same question -
how much electrical energy does this store need over the next two days, and when is it
allowed to take it - and differ only in the numbers. So there is one model, and the
appliance types are presets over it.

Two answers come out, and keeping them apart is what makes the feature behave:

- ``total_wh`` is the **forecast**: the energy the store will need across the whole
  horizon, including the losses it will suffer while sitting at target. It stays honest
  even when the store is momentarily satisfied, because the optimizer is planning two
  days ahead and a pool that is at temperature right now will still need topping up
  tonight.
- ``detail["active"]`` is the **control** flag: whether the appliance should be running
  at all at this moment, with a deadband so it is not switched on for the last tenth of
  a degree.

Confusing the two produces either a forecast that collapses to zero whenever the pump
is off, or a pump that hunts around its setpoint.
"""

import logging
from datetime import timedelta

from .. import efficiency as slot_efficiency
from .base import KIND_CONTINGENT, BaseDemandModel, EnergyDemand
from .calibration import IDLE_POWER_W, ThermalCalibrator
from .cover_habit import OBSERVED_HOURS, CoverHabit
from .thermal_physics import (
    WH_PER_M3_PER_K,
    cop_at,
    energy_to_raise_wh,
    loss_power_w,
    thermal_to_electrical_wh,
)

logger = logging.getLogger("__main__")

# Reasons reported alongside the demand, shown on MQTT and the dashboard.
REASON_NO_TEMPERATURE = "temperature sensor unavailable"
REASON_AT_TARGET = "at target temperature"
REASON_IN_DEADBAND = "within deadband - not starting"
REASON_FROST = "frost protection"
REASON_HEATING = "below target temperature"
REASON_HOLDING = "covering standing losses"
REASON_OUT_OF_SEASON = "out of season"

# How a store is planned. By energy, the original way: an amount over the horizon,
# placed by price. By temperature: the store itself, handed to a solver that can follow
# it slot by slot - see `store`.
PLAN_BY_ENERGY = "energy"
PLAN_BY_TEMPERATURE = "temperature"
PLANNING_MODES = (PLAN_BY_ENERGY, PLAN_BY_TEMPERATURE)

# How far above target a store planned by temperature may be heated to bank cheap or
# free energy, unless configured.
DEFAULT_STORAGE_HEADROOM_K = 1.0

# Per-slot exclusions, reported so a shortfall can name the setting behind it.
REASON_SLOT_WINDOW = "outside allowed hours"
REASON_SLOT_SEASON = "out of season"
REASON_SLOT_AMBIENT = "too cold to run"


def _as_float(value, default=None):
    """Sensor readings arrive as strings with units; take the number or give up."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(str(value).strip().split()[0])
    except (ValueError, IndexError, AttributeError):
        return default


def _round_or_none(value, digits=1):
    """Round a value that may not be there."""
    return None if value is None else round(value, digits)


def _parse_month_day(value):
    """``"04-15"`` to ``(4, 15)``, or None when unset or unparseable."""
    if not value:
        return None
    try:
        month, day = str(value).strip().split("-")[:2]
        return (int(month), int(day))
    except (ValueError, TypeError):
        logger.warning("[LOADS] cannot read %r as a MM-DD date - ignoring it", value)
        return None


def _in_season(day, start, end):
    """Whether a (month, day) falls in a season that may wrap the new year."""
    if start is None or end is None:
        return True
    if start <= end:
        return start <= day <= end
    return day >= start or day <= end


def _in_window(hour, start, end):
    """Whether an hour falls in an allowed window that may wrap midnight."""
    if start is None or end is None or start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class ThermalStorageModel(BaseDemandModel):
    """Stored-heat demand with an online-calibrated loss coefficient and COP."""

    kind = KIND_CONTINGENT
    sensor_keys = ("temp_sensor", "target_temp_sensor", "power_sensor",
                   "ambient_temp_sensor", "cover_sensor", "heat_sensor")

    def __init__(self, entry_id, config, calibration=None):
        super().__init__(entry_id, config)
        self._apply_config(self.config)
        # What the calibrator may learn for this kind of store - see the preset. Kept so
        # a reset starts again under the same rules rather than the pool's.
        self.calibration_options = dict(calibration or {})
        self.calibrator = self._new_calibrator()
        self.cover_habit = CoverHabit()

    def _new_calibrator(self):
        return ThermalCalibrator(
            volume_m3=self.volume_m3,
            surface_m2=self.surface_m2,
            loss_coefficient=self.config.get("heat_loss_w_per_m2_k", 25.0),
            cop_nominal=self.config.get("cop_nominal", 4.5),
            air_coefficient=self.config.get("cop_air_coeff", 0.0),
            cover_loss_factor=self.cover_loss_factor,
            idle_power_w=self.standby_threshold_w,
            **self.calibration_options,
        )

    def _apply_config(self, config):
        """
        Read the tunables out of a config dict.

        Shared by construction and hot reload. The physical dimensions are read here too
        because construction needs them, but they are restart-required in the schema:
        changing the volume of a store invalidates a calibration anchored to it, and
        quietly carrying the old coefficients forward would be worse than a restart.
        """
        self.volume_m3 = float(config.get("volume_m3", 0) or 0)
        self.surface_m2 = float(config.get("surface_m2", 0) or 0)
        self.rated_power_w = float(config.get("rated_power_w", 0) or 0)
        self.deadband_k = max(0.0, float(config.get("deadband_k", 0.5) or 0.0))
        self.cover_loss_factor = float(config.get("cover_loss_factor", 1.0) or 1.0)
        # The permanent draw of the appliance's own systems - circulation pumps,
        # controls, an oil heating circuit - under which the meter never falls. It is
        # the on/off threshold, not a contribution: how the measured idling enters the
        # household forecast is decided where the plan is published.
        self.standby_threshold_w = max(
            0.0, float(config.get("standby_threshold_w", 0) or 0)
        )

        self.min_ambient_temp_c = config.get("min_ambient_temp_c")
        self.frost_protection_temp_c = config.get("frost_protection_temp_c")
        self.window_start = config.get("window_start")
        self.window_end = config.get("window_end")
        self.deadline_hours = config.get("deadline_hours")

        self.season_start = _parse_month_day(config.get("season_start"))
        self.season_end = _parse_month_day(config.get("season_end"))

        mode = str(config.get("planning_mode") or PLAN_BY_ENERGY).strip().lower()
        self.planning_mode = mode if mode in PLANNING_MODES else PLAN_BY_ENERGY
        try:
            headroom = float(config.get("storage_headroom_k", DEFAULT_STORAGE_HEADROOM_K))
        except (TypeError, ValueError):
            headroom = DEFAULT_STORAGE_HEADROOM_K
        self.storage_headroom_k = max(0.0, headroom)

    def reconfigure(self, config):
        """Apply changed settings, keeping the calibrator and everything it learned."""
        super().reconfigure(config)
        self._apply_config(self.config)
        # The threshold is a pure classification change: no window, row or coefficient
        # is invalidated by it, so the fit carries over.
        self.calibrator.set_idle_threshold(self.standby_threshold_w)

    def reset_calibration(self):
        """Discard what has been learned and start again from the configured values."""
        self.calibrator = self._new_calibrator()
        self.cover_habit.reset()

    # -- configuration helpers ----------------------------------------------------------

    def target_temperature(self, readings):
        """The setpoint, from its sensor when one is configured, else from config."""
        from_sensor = _as_float(readings.get("target_temp_sensor"))
        if from_sensor is not None:
            return from_sensor
        return _as_float(self.config.get("target_temp"), 0.0)

    def is_covered(self, readings):
        """Whether the cover reads closed; anything unreadable means "open"."""
        raw = readings.get("cover_sensor")
        if raw is None:
            return False
        text = str(raw).strip().lower()
        return text in ("on", "true", "closed", "1", "yes", "home")

    def effective_cover_factor(self):
        """
        How much a closed cover actually cuts the losses.

        The configured value is the cold start; what the calibrator has since measured
        replaces it. A cover is the one part of this model the user has no way to look
        up - the number on a supplier's data sheet is for still air over new material -
        so guessing it once and holding it forever put its whole error into the loss
        coefficient instead.
        """
        if self.cover_loss_factor >= 1.0:
            return 1.0
        return self.calibrator.cover_loss_factor

    def standby_power_now(self, readings):
        """
        The permanent draw this meter is reading while the appliance is off, in watts.

        The threshold decides where standby ends; what is under it is measured, not
        configured, because the real draw - the pump's own figure is 80-120 W - moves
        about and the household forecast should carry what the meter actually says.
        None when there is no threshold, no readable power, or the appliance is above
        the threshold: there the standby is inside the running draw and is the
        schedule's business, not a separate household term.
        """
        if self.standby_threshold_w <= 0:
            return None
        power = _as_float(readings.get("power_sensor"), 0.0)
        if power is None or power <= 0 or power >= self.standby_threshold_w:
            return None
        return power

    def cover_factor(self, readings):
        """A closed cover cuts the losses; anything unreadable means "open"."""
        return self.effective_cover_factor() if self.is_covered(readings) else 1.0

    def is_running(self, readings):
        """
        Whether the appliance itself is drawing, above its permanent systems.

        The configured threshold says where the standby draw on this meter ends and the
        appliance begins; the 50 W default is the old guess for a circuit that does
        drop to nearly nothing. A heat pump idling at 100 W of circulation pumps and
        controls read as "running" around the clock under the bare guess, which held
        the release gate open inside the deadband and fed the calibration compressor
        windows that were only standby.
        """
        power = _as_float(readings.get("power_sensor"), 0.0)
        if power is None:
            return False
        # A configured threshold is authoritative, however low - the user knows the
        # circuit. Only the unset zero stands down to the bare noise-floor guess.
        return power >= (self.standby_threshold_w or IDLE_POWER_W)

    # -- feasibility --------------------------------------------------------------------

    def feasibility(self, ctx):
        """
        Per-slot mask of when the appliance may run at all, and why not when it may not.

        Each slot's wall clock comes from the instant it starts, not from its index.
        Dividing the index gives the right answer on 364 days a year and is an hour
        out on the other two: a 25-hour day has 25 hourly slots, so from the
        transition onwards index // slots_per_hour names the hour before the real
        one — and a run window of 22:00–06:00 would open and close an hour late. One
        astimezone() per slot (192 at most) settles it exactly.
        """
        mask, reasons = [], []
        for index in range(ctx.slot_count):
            # astimezone() re-normalizes the pytz offset across a transition.
            slot_start = (
                ctx.anchor + timedelta(seconds=index * ctx.time_frame_base)
            ).astimezone(ctx.anchor.tzinfo)
            hour = slot_start.hour
            day = slot_start.date()

            reason = None
            if not _in_window(hour, self.window_start, self.window_end):
                reason = REASON_SLOT_WINDOW
            elif not _in_season((day.month, day.day), self.season_start, self.season_end):
                reason = REASON_SLOT_SEASON
            elif self.min_ambient_temp_c is not None:
                ambient = self._ambient_at(ctx, index)
                if ambient is not None and ambient < float(self.min_ambient_temp_c):
                    reason = REASON_SLOT_AMBIENT

            mask.append(reason is None)
            reasons.append(reason)
        return mask, reasons

    @staticmethod
    def _ambient_at(ctx, index):
        if not ctx.ambient_temp_c:
            return None
        if index < len(ctx.ambient_temp_c):
            return ctx.ambient_temp_c[index]
        return ctx.ambient_temp_c[-1]

    # -- demand ---------------------------------------------------------------------------

    def demand(self, ctx):
        """Energy needed over the horizon, and whether to run right now."""
        current = _as_float(ctx.readings.get("temp_sensor"))
        if current is None:
            logger.debug(
                "[LOADS] '%s' has no readable temperature - reporting no demand", self.id
            )
            return EnergyDemand(
                kind=self.kind, total_wh=0.0, feasible=[False] * ctx.slot_count,
                max_power_w=self.rated_power_w, reason=REASON_NO_TEMPERATURE,
            )

        target = self.target_temperature(ctx.readings)
        cover_now = self.cover_factor(ctx.readings)
        cover = self.cover_series(ctx, cover_now)
        feasible, feasible_reason = self.feasibility(ctx)

        urgent = (
            self.frost_protection_temp_c is not None
            and current <= float(self.frost_protection_temp_c)
        )

        deadline_slot = None
        if self.deadline_hours:
            deadline_slot = min(
                ctx.slot_count - 1,
                ctx.current_slot
                + int(round(float(self.deadline_hours) * 3600 / ctx.time_frame_base)),
            )

        heat_up_wh = energy_to_raise_wh(self.volume_m3, target - current)
        losses_wh, mean_cop = self._horizon_losses(
            ctx, target, cover, feasible, until=deadline_slot
        )

        thermal_wh = max(0.0, heat_up_wh + losses_wh)
        total_wh = thermal_to_electrical_wh(thermal_wh, mean_cop)

        # With a COP per slot the solver can prefer the warm hours, and the demand is
        # then the electricity the heat would take at the *best* of them: each slot's
        # Wh counts at its own COP over that one, so the total is a statement about
        # heat rather than about any particular placement.
        efficiency, reference_cop = [], None
        if slot_efficiency.source_of(self.config) == slot_efficiency.SOURCE_COP:
            cops = self.cop_series(ctx)
            usable = [
                cops[index] for index in range(max(0, ctx.current_slot), ctx.slot_count)
                if feasible[index]
            ]
            if usable:
                reference_cop = max(usable)
                efficiency = slot_efficiency.normalise(
                    cops, ctx.slot_count, start=ctx.current_slot, feasible=feasible
                )
                total_wh = thermal_to_electrical_wh(thermal_wh, reference_cop)

        active, reason = self._control_state(current, target, ctx.readings, urgent)
        if not any(feasible):
            reason = REASON_OUT_OF_SEASON

        store = None
        if self.planning_mode == PLAN_BY_TEMPERATURE and self.volume_m3 > 0:
            store = self.thermal_store(ctx, current, target, cover)

        return EnergyDemand(
            kind=self.kind,
            total_wh=total_wh,
            feasible=feasible,
            feasible_reason=feasible_reason,
            max_power_w=self.rated_power_w,
            efficiency=efficiency,
            store=store,
            deadline_slot=deadline_slot,
            urgent=urgent and active,
            reason=reason,
            detail={
                "active": active,
                # Whether a solver following the temperature may run it now: anywhere
                # below the storage ceiling, because inside the band the plan - not a
                # deadband - decides when heat is worth putting in.
                "storage_active": bool(
                    urgent or current < target + self.storage_headroom_k
                ),
                "planning_mode": self.planning_mode,
                "efficiency_source": slot_efficiency.source_of(self.config),
                "reference_cop": _round_or_none(reference_cop, 2),
                "temperature_c": round(current, 2),
                "target_temperature_c": round(target, 2),
                "heat_up_wh_thermal": round(heat_up_wh, 1),
                "standing_losses_wh_thermal": round(losses_wh, 1),
                "mean_cop": round(mean_cop, 2),
                "cover_factor": cover_now,
                "cover_habit_hours": self.cover_habit.hours_known(),
                "running": self.is_running(ctx.readings),
                # The inputs, so a wrong answer can be diagnosed from the page. The
                # placeholder-ambient bug produced entirely plausible outputs and was
                # invisible precisely because none of this was reported.
                "ambient_now_c": (
                    round(self._ambient_at(ctx, ctx.current_slot), 1)
                    if self._ambient_at(ctx, ctx.current_slot) is not None else None
                ),
                "ambient_source": ctx.ambient_source,
                # What the site's own thermometer says, alongside what the model used.
                # "Outside now" promised a measurement and showed a forecast.
                "ambient_measured_c": _round_or_none(ctx.ambient_measured_c),
                "ambient_forecast_c": _round_or_none(ctx.ambient_forecast_c),
                "ambient_offset_k": _round_or_none(
                    None if (ctx.ambient_measured_c is None
                             or ctx.ambient_forecast_c is None)
                    else self._ambient_at(ctx, ctx.current_slot) - ctx.ambient_forecast_c
                ),
                "horizon_hours": round(
                    max(0, ctx.slot_count - ctx.current_slot) * ctx.hours_per_slot(), 1
                ),
                "electrical_power_w": self.rated_power_w,
                "thermal_power_w": round(self.rated_power_w * mean_cop),
            },
        )

    def thermal_store(self, ctx, current, target, cover):
        """
        The store as a solver needs it to follow the temperature slot by slot.

        The same physics as the demand and the projection - losses against the air
        through the calibrated coefficient and the cover, heat in through the COP at
        each slot's air temperature, the sun through the fitted gain - laid out per
        slot instead of summed, so the three cannot disagree. The band is the deadband
        below target and the storage headroom above it: comfort at the bottom, what may
        be banked at the top. Lists are in EOS slot space, from local midnight.
        """
        solar = ctx.solar_wh or []
        gain = self.calibrator.solar_gain
        loss, ambient, gains = [], [], []
        for index in range(ctx.slot_count):
            air = self._ambient_at(ctx, index)
            ambient.append(air if air is not None else 0.0)
            factor = cover[index] if isinstance(cover, list) else cover
            loss.append(self.calibrator.loss_coefficient * self.surface_m2 * factor)
            gains.append(
                gain * max(0.0, float(solar[index]))
                if gain > 0 and index < len(solar) else 0.0
            )
        return {
            "capacity_wh_per_k": WH_PER_M3_PER_K * self.volume_m3,
            "loss_w_per_k": loss,
            "ambient_c": ambient,
            "cop": self.cop_series(ctx),
            "gain_wh": gains,
            "t_initial": float(current),
            "t_min": float(target) - self.deadband_k,
            "t_target": float(target),
            "t_max": float(target) + self.storage_headroom_k,
        }

    def cop_series(self, ctx):
        """The COP the appliance would run at in every slot, from its air temperature."""
        series = []
        for index in range(ctx.slot_count):
            ambient = self._ambient_at(ctx, index)
            series.append(
                cop_at(ambient, self.calibrator.cop_nominal, self.calibrator.air_coefficient)
                if ambient is not None else self.calibrator.cop_nominal
            )
        return series

    def cover_series(self, ctx, measured):
        """
        Effective cover factor for every slot of the horizon.

        The next couple of hours take the switch as it reads now; beyond that the
        learned habit, blended rather than switched - an hour that is covered four
        nights in five is worth four fifths of a cover, and saying so is more honest
        than rounding it either way. An hour with too little history defers to the
        switch.
        """
        if self.cover_loss_factor >= 1.0:
            return [1.0] * ctx.slot_count

        observed_until = ctx.current_slot + round(
            OBSERVED_HOURS * 3600 / ctx.time_frame_base
        )

        series = []
        for index in range(ctx.slot_count):
            if index <= observed_until:
                series.append(measured)
                continue
            # The hour this slot actually starts at - see _slot_mask: index
            # arithmetic is an hour out after a DST transition, which would
            # read the cover habit for the wrong hour of the evening.
            hour = (
                ctx.anchor + timedelta(seconds=index * ctx.time_frame_base)
            ).astimezone(ctx.anchor.tzinfo).hour
            probability = self.cover_habit.probability(hour)
            if probability is None:
                series.append(measured)
            else:
                series.append(1.0 - probability * (1.0 - self.effective_cover_factor()))
        return series

    def _horizon_losses(self, ctx, target, cover, feasible, until=None):
        """
        Thermal energy lost between now and the end of the horizon, and the COP to use.

        Losses are evaluated at the *target* temperature: that is the state the plan is
        working to hold, and using the current temperature would understate the demand of
        a store that is still cold. The mean COP is taken over the slots the appliance may
        actually run in, because those are the ambient temperatures it will see.

        Sunshine is netted off. A store that gains heat from the sun needs less from the
        appliance, and a model that measured that gain without ever spending it would
        book the saving and then ask for the energy anyway. The gain is zero until the
        calibration has both bright and dark windows to separate it from everything else.
        """
        hours = ctx.hours_per_slot()
        solar = ctx.solar_wh or []
        gain = self.calibrator.solar_gain
        # A store with a deadline is asked to be at target *by* then, not held there
        # for two days. Integrating to the end of the horizon regardless asked a sauna
        # for 177 kWh of standing loss - 29 hours of running in a 42 hour horizon -
        # when what it wanted was to be hot in six.
        last = ctx.slot_count if until is None else min(ctx.slot_count, until + 1)

        losses = 0.0
        cops = []
        for index in range(max(0, ctx.current_slot), last):
            ambient = self._ambient_at(ctx, index)
            if ambient is None:
                continue
            losses += loss_power_w(
                self.calibrator.loss_coefficient, self.surface_m2, target, ambient,
                cover[index] if isinstance(cover, list) else cover,
            ) * hours
            if gain > 0 and index < len(solar):
                # solar_wh is energy per slot; the gain was fitted against power.
                losses -= gain * max(0.0, float(solar[index]))
            if feasible[index]:
                cops.append(
                    cop_at(ambient, self.calibrator.cop_nominal,
                           self.calibrator.air_coefficient)
                )

        mean_cop = sum(cops) / len(cops) if cops else self.calibrator.cop_nominal
        return losses, mean_cop

    def project_medium(self, ctx, plan, start_c, target=None):
        """
        Where the store's own temperature goes, slot by slot, under a given plan.

        The card needs this to answer "why does it want so much energy?". The demand
        figure is a single number and says nothing about the shape behind it: a store
        that has to climb four kelvin *and* hold there against a cold night is asking
        for two different things at once, and only a curve separates them.

        It is the same physics the demand is built from, integrated rather than
        summed - heat in from whatever the plan runs, heat out to the air it is
        standing in, sun netted off - so the line and the number cannot disagree.
        Slots before now are the past and stay empty.
        """
        if self.volume_m3 <= 0:
            return []
        if target is None:
            target = self.target_temperature(ctx.readings)
            # A store planned by temperature is meant to go above target when heat is
            # cheap; stopping the line there would draw a plan that is not the one made.
            if self.planning_mode == PLAN_BY_TEMPERATURE:
                target += self.storage_headroom_k
        hours = ctx.hours_per_slot()
        cover = self.cover_series(ctx, self.cover_factor(ctx.readings))
        solar = ctx.solar_wh or []
        gain = self.calibrator.solar_gain
        capacity_wh_per_k = WH_PER_M3_PER_K * self.volume_m3

        temperature = float(start_c)
        projected = []
        for index in range(ctx.slot_count):
            if index < ctx.current_slot:
                projected.append(None)
                continue
            ambient = self._ambient_at(ctx, index)
            if ambient is None:
                projected.append(round(temperature, 2))
                continue

            electrical_wh = float(plan[index]) if index < len(plan) else 0.0
            gained = electrical_wh * cop_at(
                ambient, self.calibrator.cop_nominal, self.calibrator.air_coefficient
            )
            gained -= loss_power_w(
                self.calibrator.loss_coefficient, self.surface_m2, temperature, ambient,
                cover[index] if isinstance(cover, list) else cover,
            ) * hours
            if gain > 0 and index < len(solar):
                gained += gain * max(0.0, float(solar[index]))

            temperature += gained / capacity_wh_per_k
            # The appliance stops at target; a projection that does not says a sauna
            # will reach 120 C against a 90 C setting. Invisible on a pool at four
            # hundredths of a kelvin per slot, plain on a 6 kW heater in 80 litres.
            if target is not None and temperature > target:
                temperature = float(target)
            projected.append(round(temperature, 2))
        return projected

    def _control_state(self, current, target, readings, urgent):
        """The hysteresis: when may the appliance start, and when must it stop."""
        if urgent:
            return True, REASON_FROST
        if current >= target:
            return False, REASON_AT_TARGET
        if current < target - self.deadband_k:
            return True, REASON_HEATING
        # Inside the deadband: keep going if already running, do not start otherwise.
        if self.is_running(readings):
            return True, REASON_HOLDING
        return False, REASON_IN_DEADBAND

    # -- calibration ----------------------------------------------------------------------

    def observe(self, sample):
        """Hand one recorded sample to the calibrator, and to the cover habit."""
        if not isinstance(sample, dict) or "timestamp" not in sample:
            return False
        self._observe_cover(sample)
        return self.calibrator.observe(sample)

    def _observe_cover(self, sample):
        """Note whether the store was covered, for the per-hour pattern."""
        if self.cover_loss_factor >= 1.0:
            return
        covered = sample.get("covered")
        if covered is None:
            factor = sample.get("cover_factor")
            if factor is None:
                return
            try:
                covered = float(factor) < 1.0
            except (TypeError, ValueError):
                return
        self.cover_habit.observe(sample["timestamp"], bool(covered))

    def observe_history(self, samples):
        """Replay a recorded history at startup so day one is not a cold start."""
        rows = list(samples)
        for sample in rows:
            if isinstance(sample, dict):
                self._observe_cover(sample)
        return self.calibrator.observe_series(rows)

    def status(self):
        state = self.calibrator.state()
        state.update({
            "cover_habit": self.cover_habit.state(),
            "volume_m3": self.volume_m3,
            "surface_m2": self.surface_m2,
            "rated_power_w": self.rated_power_w,
            "deadband_k": self.deadband_k,
            "standby_threshold_w": self.standby_threshold_w,
        })
        return state
