"""
Learning a store's real loss coefficient and COP from how it actually behaves.

Nobody knows their pool's heat loss coefficient. They know its volume roughly, its
surface area roughly, and that it cools down overnight. The configured values are
therefore a starting point, not an answer, and a forecast built on them alone would be
wrong in a way the user cannot diagnose.

The physics gives two free measurements, every day, for nothing:

- while the pump is **off**, the water cools at a rate that depends only on the loss
  coefficient and the temperature difference to ambient;
- while the pump is **on**, the water warms at a rate that, once the simultaneous losses
  are added back, is the heat pump's thermal output - and dividing by the measured
  electrical draw gives the COP at that ambient temperature.

Both are noisy, so both are smoothed, bounded, and reported with a confidence the user
can see. This is also the honest answer to the "there are dozens of variables" objection
raised on issue #201: the model is deliberately lumped, and the fit absorbs what it does
not name.
"""

import logging
import math
from collections import deque

from .thermal_physics import (
    COP_MAX,
    COP_MIN,
    COP_REFERENCE_AMBIENT_C,
    observed_cop,
    observed_loss_coefficient,
)

logger = logging.getLogger("__main__")

# Below this the appliance is standing by, not running. Matches the 50 Wh "did it
# actually run" heuristic already used for the additional-load feature.
IDLE_POWER_W = 50.0

# Sample pairs outside this spacing say nothing: too short and the temperature sensor's
# resolution dominates, too long and the pump state changed in between.
MIN_SAMPLE_HOURS = 5 / 60.0
MAX_SAMPLE_HOURS = 3.0

# Plausibility bounds. A covered indoor tank sits near the bottom, an uncovered pool in
# a breeze near the top; anything outside is a bad sample, not a discovery.
LOSS_COEFFICIENT_MIN = 1.0
LOSS_COEFFICIENT_MAX = 200.0

# How fast the estimates follow new evidence. Slow enough that one odd afternoon does
# not move the plan, fast enough to track a pool cover being left off for a week.
SMOOTHING = 0.1

# Samples retained for the COP-versus-ambient fit, and what a settled fit looks like.
COP_SAMPLE_LIMIT = 400
COP_FIT_MIN_SAMPLES = 8
COP_FIT_MIN_SPAN_K = 3.0
CONFIDENCE_TARGET_SAMPLES = 60

# A linear COP slope beyond this is a fitting artefact, not a compressor characteristic.
AIR_COEFFICIENT_LIMIT = 0.06


def _clamp(value, low, high):
    return max(low, min(high, value))


class ThermalCalibrator:
    """
    Online estimator for one store's loss coefficient and COP curve.

    Starts from the configured values and moves away from them only as evidence
    accumulates, so a fresh installation behaves sensibly on day one and better by
    week two.
    """

    def __init__(self, volume_m3, surface_m2, loss_coefficient, cop_nominal,
                 air_coefficient=0.0):
        self.volume_m3 = float(volume_m3 or 0.0)
        self.surface_m2 = float(surface_m2 or 0.0)

        self.configured_loss_coefficient = float(loss_coefficient or 0.0)
        self.configured_cop_nominal = _clamp(float(cop_nominal or 4.0), COP_MIN, COP_MAX)

        self.loss_coefficient = self.configured_loss_coefficient
        self.cop_nominal = self.configured_cop_nominal
        self.air_coefficient = _clamp(
            float(air_coefficient or 0.0), -AIR_COEFFICIENT_LIMIT, AIR_COEFFICIENT_LIMIT
        )

        self.loss_samples = 0
        self.cop_samples = 0
        self._cop_points = deque(maxlen=COP_SAMPLE_LIMIT)

    # -- ingest -----------------------------------------------------------------------

    def observe_pair(self, previous, current):
        """
        Take two consecutive samples and learn what they allow.

        A sample is a dict with ``timestamp`` (aware datetime), ``medium_c``,
        ``ambient_c``, ``power_w`` and optional ``cover_factor``. Pairs that cannot say
        anything are dropped silently - most pairs are, and logging each one would bury
        the log.
        """
        hours = self._pair_hours(previous, current)
        if hours is None:
            return False

        try:
            medium_now = float(current["medium_c"])
            medium_before = float(previous["medium_c"])
            ambient = (float(current["ambient_c"]) + float(previous["ambient_c"])) / 2.0
            power_now = float(current.get("power_w", 0.0))
            power_before = float(previous.get("power_w", 0.0))
        except (KeyError, TypeError, ValueError):
            return False

        if not all(math.isfinite(v) for v in (medium_now, medium_before, ambient)):
            return False

        delta_k = medium_now - medium_before
        cover_factor = float(current.get("cover_factor", 1.0) or 1.0)
        medium_mean = (medium_now + medium_before) / 2.0

        running = power_now >= IDLE_POWER_W and power_before >= IDLE_POWER_W
        idle = power_now < IDLE_POWER_W and power_before < IDLE_POWER_W

        if idle:
            return self._learn_loss(delta_k, hours, medium_mean, ambient, cover_factor)
        if running:
            return self._learn_cop(
                delta_k, hours, medium_mean, ambient, cover_factor,
                (power_now + power_before) / 2.0,
            )
        # The pump started or stopped inside the interval: neither measurement is clean.
        return False

    def observe_series(self, samples):
        """Feed a whole recorded history, oldest first. Returns pairs actually used."""
        used = 0
        for previous, current in zip(samples, samples[1:]):
            if self.observe_pair(previous, current):
                used += 1
        return used

    # -- estimates --------------------------------------------------------------------

    def cop_at_reference(self):
        """The fitted nominal COP, at the datasheet reference ambient."""
        return self.cop_nominal

    def confidence(self):
        """
        How much the fit should be trusted, 0 to 1.

        Both halves have to be earned: an estimator that has watched the pump run but
        never watched it cool down knows the COP and is still guessing at the losses.
        """
        loss_part = min(1.0, self.loss_samples / CONFIDENCE_TARGET_SAMPLES)
        cop_part = min(1.0, self.cop_samples / CONFIDENCE_TARGET_SAMPLES)
        return round((loss_part + cop_part) / 2.0, 3)

    def state(self):
        """Serializable estimator state - persisted, and shown on the API."""
        return {
            "loss_coefficient": round(self.loss_coefficient, 3),
            "cop_nominal": round(self.cop_nominal, 3),
            "air_coefficient": round(self.air_coefficient, 5),
            "loss_samples": self.loss_samples,
            "cop_samples": self.cop_samples,
            "confidence": self.confidence(),
        }

    def restore(self, state):
        """Reload a persisted estimate, ignoring anything implausible."""
        if not isinstance(state, dict):
            return
        loss = state.get("loss_coefficient")
        if isinstance(loss, (int, float)) and LOSS_COEFFICIENT_MIN <= loss <= LOSS_COEFFICIENT_MAX:
            self.loss_coefficient = float(loss)
        cop = state.get("cop_nominal")
        if isinstance(cop, (int, float)) and COP_MIN <= cop <= COP_MAX:
            self.cop_nominal = float(cop)
        air = state.get("air_coefficient")
        if isinstance(air, (int, float)):
            self.air_coefficient = _clamp(
                float(air), -AIR_COEFFICIENT_LIMIT, AIR_COEFFICIENT_LIMIT
            )
        self.loss_samples = max(0, int(state.get("loss_samples", 0) or 0))
        self.cop_samples = max(0, int(state.get("cop_samples", 0) or 0))

    # -- internals --------------------------------------------------------------------

    @staticmethod
    def _pair_hours(previous, current):
        try:
            span = (current["timestamp"] - previous["timestamp"]).total_seconds() / 3600.0
        except (KeyError, TypeError, AttributeError):
            return None
        if not MIN_SAMPLE_HOURS <= span <= MAX_SAMPLE_HOURS:
            return None
        return span

    def _learn_loss(self, delta_k, hours, medium_c, ambient_c, cover_factor):
        estimate = observed_loss_coefficient(
            self.volume_m3, self.surface_m2, delta_k, hours,
            medium_c, ambient_c, cover_factor,
        )
        if estimate is None:
            return False
        if not LOSS_COEFFICIENT_MIN <= estimate <= LOSS_COEFFICIENT_MAX:
            return False

        self.loss_coefficient = (
            (1 - SMOOTHING) * self.loss_coefficient + SMOOTHING * estimate
        )
        self.loss_samples += 1
        return True

    def _learn_cop(self, delta_k, hours, medium_c, ambient_c, cover_factor, power_w):
        loss_w = 0.0
        if self.loss_coefficient > 0 and self.surface_m2 > 0:
            loss_w = (
                self.loss_coefficient * self.surface_m2
                * (medium_c - ambient_c) * cover_factor
            )

        estimate = observed_cop(self.volume_m3, delta_k, hours, power_w, loss_w)
        if estimate is None or not COP_MIN <= estimate <= COP_MAX:
            return False

        self._cop_points.append((ambient_c, estimate))
        self.cop_samples += 1
        self._refit_cop()
        return True

    def _refit_cop(self):
        """
        Least squares of COP against ambient, falling back to a plain average.

        The fit only runs once the samples actually span a temperature range: fitting a
        slope through a week of identical afternoons produces a confident nonsense
        gradient that would then be extrapolated to a cold morning.
        """
        points = list(self._cop_points)
        if len(points) < COP_FIT_MIN_SAMPLES:
            mean = sum(value for _, value in points) / len(points)
            self.cop_nominal = _clamp(
                (1 - SMOOTHING) * self.cop_nominal + SMOOTHING * mean, COP_MIN, COP_MAX
            )
            return

        temps = [temp for temp, _ in points]
        span = max(temps) - min(temps)
        mean_temp = sum(temps) / len(temps)
        mean_cop = sum(value for _, value in points) / len(points)

        slope = 0.0
        if span >= COP_FIT_MIN_SPAN_K:
            variance = sum((temp - mean_temp) ** 2 for temp in temps)
            if variance > 0:
                covariance = sum(
                    (temp - mean_temp) * (value - mean_cop) for temp, value in points
                )
                slope = covariance / variance

        cop_at_reference = mean_cop + slope * (COP_REFERENCE_AMBIENT_C - mean_temp)
        cop_at_reference = _clamp(cop_at_reference, COP_MIN, COP_MAX)

        air_coefficient = 0.0
        if cop_at_reference > 0:
            air_coefficient = _clamp(
                slope / cop_at_reference, -AIR_COEFFICIENT_LIMIT, AIR_COEFFICIENT_LIMIT
            )

        self.cop_nominal = cop_at_reference
        self.air_coefficient = air_coefficient
