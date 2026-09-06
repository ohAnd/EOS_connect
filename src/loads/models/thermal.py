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

from .base import KIND_CONTINGENT, BaseDemandModel, EnergyDemand
from .calibration import IDLE_POWER_W, ThermalCalibrator
from .thermal_physics import (
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
                   "ambient_temp_sensor", "cover_sensor")

    def __init__(self, entry_id, config):
        super().__init__(entry_id, config)
        self._apply_config(self.config)

        self.calibrator = ThermalCalibrator(
            volume_m3=self.volume_m3,
            surface_m2=self.surface_m2,
            loss_coefficient=self.config.get("heat_loss_w_per_m2_k", 25.0),
            cop_nominal=self.config.get("cop_nominal", 4.5),
            air_coefficient=self.config.get("cop_air_coeff", 0.0),
        )
        self._last_sample = None

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

        self.min_ambient_temp_c = config.get("min_ambient_temp_c")
        self.frost_protection_temp_c = config.get("frost_protection_temp_c")
        self.window_start = config.get("window_start")
        self.window_end = config.get("window_end")
        self.deadline_hours = config.get("deadline_hours")

        self.season_start = _parse_month_day(config.get("season_start"))
        self.season_end = _parse_month_day(config.get("season_end"))

    def reconfigure(self, config):
        """Apply changed settings, keeping the calibrator and everything it learned."""
        super().reconfigure(config)
        self._apply_config(self.config)

    def reset_calibration(self):
        """Discard what has been learned and start again from the configured values."""
        self.calibrator = ThermalCalibrator(
            volume_m3=self.volume_m3,
            surface_m2=self.surface_m2,
            loss_coefficient=self.config.get("heat_loss_w_per_m2_k", 25.0),
            cop_nominal=self.config.get("cop_nominal", 4.5),
            air_coefficient=self.config.get("cop_air_coeff", 0.0),
        )
        self._last_sample = None

    # -- configuration helpers ----------------------------------------------------------

    def target_temperature(self, readings):
        """The setpoint, from its sensor when one is configured, else from config."""
        from_sensor = _as_float(readings.get("target_temp_sensor"))
        if from_sensor is not None:
            return from_sensor
        return _as_float(self.config.get("target_temp"), 0.0)

    def cover_factor(self, readings):
        """A closed cover cuts the losses; anything unreadable means "open"."""
        raw = readings.get("cover_sensor")
        if raw is None:
            return 1.0
        text = str(raw).strip().lower()
        closed = text in ("on", "true", "closed", "1", "yes", "home")
        return self.cover_loss_factor if closed else 1.0

    def is_running(self, readings):
        """Whether the appliance is drawing more than standby power right now."""
        power = _as_float(readings.get("power_sensor"), 0.0)
        return power is not None and power >= IDLE_POWER_W

    # -- feasibility --------------------------------------------------------------------

    def feasibility(self, ctx):
        """
        Per-slot mask of when the appliance may run at all.

        Hours are derived from the slot index because slot 0 is local midnight by
        construction. On the two DST days of the year that drifts by an hour late in the
        second day of the horizon, which is not worth a calendar walk per slot.
        """
        slots_per_day = max(1, 86400 // ctx.time_frame_base)
        slots_per_hour = max(1, ctx.slots_per_hour())
        mask = []
        for index in range(ctx.slot_count):
            hour = (index % slots_per_day) // slots_per_hour
            day = (ctx.anchor + timedelta(days=index // slots_per_day)).date()

            allowed = _in_window(hour, self.window_start, self.window_end)
            if allowed and not _in_season((day.month, day.day), self.season_start,
                                          self.season_end):
                allowed = False
            if allowed and self.min_ambient_temp_c is not None:
                ambient = self._ambient_at(ctx, index)
                if ambient is not None and ambient < float(self.min_ambient_temp_c):
                    allowed = False
            mask.append(allowed)
        return mask

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
        cover = self.cover_factor(ctx.readings)
        feasible = self.feasibility(ctx)

        urgent = (
            self.frost_protection_temp_c is not None
            and current <= float(self.frost_protection_temp_c)
        )

        heat_up_wh = energy_to_raise_wh(self.volume_m3, target - current)
        losses_wh, mean_cop = self._horizon_losses(ctx, target, cover, feasible)

        thermal_wh = max(0.0, heat_up_wh + losses_wh)
        total_wh = thermal_to_electrical_wh(thermal_wh, mean_cop)

        active, reason = self._control_state(current, target, ctx.readings, urgent)
        if not any(feasible):
            reason = REASON_OUT_OF_SEASON

        deadline_slot = None
        if self.deadline_hours:
            deadline_slot = min(
                ctx.slot_count - 1,
                ctx.current_slot
                + int(round(float(self.deadline_hours) * 3600 / ctx.time_frame_base)),
            )

        return EnergyDemand(
            kind=self.kind,
            total_wh=total_wh,
            feasible=feasible,
            max_power_w=self.rated_power_w,
            deadline_slot=deadline_slot,
            urgent=urgent and active,
            reason=reason,
            detail={
                "active": active,
                "temperature_c": round(current, 2),
                "target_temperature_c": round(target, 2),
                "heat_up_wh_thermal": round(heat_up_wh, 1),
                "standing_losses_wh_thermal": round(losses_wh, 1),
                "mean_cop": round(mean_cop, 2),
                "cover_factor": cover,
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
                "ambient_measured_c": _round_or_none(
                    _as_float(ctx.readings.get("ambient_temp_sensor"))
                ),
                "horizon_hours": round(
                    max(0, ctx.slot_count - ctx.current_slot) * ctx.hours_per_slot(), 1
                ),
                "electrical_power_w": self.rated_power_w,
                "thermal_power_w": round(self.rated_power_w * mean_cop),
            },
        )

    def _horizon_losses(self, ctx, target, cover, feasible):
        """
        Thermal energy lost between now and the end of the horizon, and the COP to use.

        Losses are evaluated at the *target* temperature: that is the state the plan is
        working to hold, and using the current temperature would understate the demand of
        a store that is still cold. The mean COP is taken over the slots the appliance may
        actually run in, because those are the ambient temperatures it will see.
        """
        hours = ctx.hours_per_slot()
        losses = 0.0
        cops = []
        for index in range(max(0, ctx.current_slot), ctx.slot_count):
            ambient = self._ambient_at(ctx, index)
            if ambient is None:
                continue
            losses += loss_power_w(
                self.calibrator.loss_coefficient, self.surface_m2, target, ambient, cover
            ) * hours
            if feasible[index]:
                cops.append(
                    cop_at(ambient, self.calibrator.cop_nominal,
                           self.calibrator.air_coefficient)
                )

        mean_cop = sum(cops) / len(cops) if cops else self.calibrator.cop_nominal
        return losses, mean_cop

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
        """Pair each sample with its predecessor and let the calibrator learn."""
        if not isinstance(sample, dict) or "timestamp" not in sample:
            return False
        previous, self._last_sample = self._last_sample, sample
        if previous is None:
            return False
        return self.calibrator.observe_pair(previous, sample)

    def observe_history(self, samples):
        """Replay a recorded history at startup so day one is not a cold start."""
        used = self.calibrator.observe_series(list(samples))
        if samples:
            self._last_sample = samples[-1]
        return used

    def status(self):
        state = self.calibrator.state()
        state.update({
            "volume_m3": self.volume_m3,
            "surface_m2": self.surface_m2,
            "rated_power_w": self.rated_power_w,
            "deadband_k": self.deadband_k,
        })
        return state
