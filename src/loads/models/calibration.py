"""
Learning a store's real loss coefficient and COP from how it actually behaves.

Nobody knows their pool's heat loss coefficient. They know its volume roughly, its
surface area roughly, and that it cools down overnight. The configured values are
therefore a starting point, not an answer, and a forecast built on them alone would be
wrong in a way the user cannot diagnose.

Every consecutive pair of samples is one equation of the same energy balance::

    V·c·dT/dt  =  COP(T_air)·P_el  -  k·A·(T_water - T_air)
      measured         unknown              unknown

and with ``COP(T_air) = a + b·T_air`` that is *linear* in the unknowns a, b and k. So
they are fitted together, by weighted least squares over the recorded history, rather
than one at a time.

The loss coefficient is two unknowns, not one: ``k_open`` while the store is uncovered
and ``k_closed`` while it is covered. Holding the ratio between them at a configured
constant looked harmless and was not. A pool covered five sixths of the time gives a fit
that is overwhelmingly about the covered state, so every error in that constant had
nowhere to go but into ``k_open``, which then drifted upward refit after refit - 15 to
33 W/(m²·K) over a few days on a real pool, inflating the predicted standing losses to
60% of total demand and making the target unreachable. Both states are observed, so both
are identifiable; the ratio is an output now, and a cold start still gets the configured
one through the ridge.

That matters more than it sounds. The obvious approach - measure the losses while the
appliance is off, then use them to measure the COP while it is on - has a circular
dependency, and a real installation walks straight into it: a pool that is held at
target barely changes temperature while heating, so the *assumed* loss term dominates
the COP measurement. An appliance that had never been observed cooling produced a
confident COP of 1.3 and an efficiency that improved as it got colder, which is
backwards. The joint fit has no bootstrap to get wrong, and off-periods contribute
naturally as the rows where ``P_el`` is zero.

Two guards keep it honest where the data is thin. A ridge term pulls the solution
towards the configured values with the weight of a few samples, so an underdetermined
fit degrades to "what you told us" instead of to noise. And the reported confidence
accounts for whether the history actually contains the variety the parameters need -
a week of identical afternoons cannot identify the slope, and says so.
"""

import logging
import math
from collections import deque

from .thermal_physics import (
    COP_MAX,
    COP_MIN,
    COP_REFERENCE_AMBIENT_C,
    WH_PER_M3_PER_K,
)

logger = logging.getLogger("__main__")

# Below this the appliance is standing by, not running. Matches the 50 Wh "did it
# actually run" heuristic already used for the additional-load feature.
IDLE_POWER_W = 50.0

# How long a window may run before it is closed regardless. A pool at target loses about
# 0.05 K/h, so it needs hours to move a tenth of a degree; a sauna moves that in a minute.
# One fixed interval cannot serve both, which is why the window is closed on the
# *temperature* having moved and this is only the backstop.
MIN_SAMPLE_HOURS = 5 / 60.0
MAX_WINDOW_HOURS = 8.0

# What counts as a measurable change, in sensor resolutions.
#
# The reason this exists: a store's temperature is read to some finite precision, and the
# fit's left-hand side is that reading divided by the elapsed time. On an 18 m3 pool
# sampled every five minutes, one 0.1 C tick is 25 kW - against a real loss of about
# 1 kW. Every row was then a rounding artefact twenty-five times the signal, and the fit
# could not tell a loss coefficient of 15 from one of 25. Waiting for three ticks before
# closing a window puts the signal comfortably above the rounding.
RESOLUTION_MULTIPLE = 3.0

# Assumed until the data says otherwise - the coarsest precision worth expecting.
DEFAULT_RESOLUTION_K = 0.1

# A jump larger than this between two samples is a sensor glitch, not a store.
MAX_TEMPERATURE_STEP_K = 20.0

# Plausibility bounds. A covered indoor tank sits near the bottom, an uncovered pool in
# a breeze near the top; anything outside is a bad fit, not a discovery.
LOSS_COEFFICIENT_MIN = 1.0
LOSS_COEFFICIENT_MAX = 200.0

# A linear COP slope beyond this is a fitting artefact, not a compressor characteristic.
AIR_COEFFICIENT_LIMIT = 0.06

# A cover cannot make a store lose heat faster than no cover, and no cover cuts losses
# by more than this. Both ends are physical, not statistical: outside them the fit has
# split the loss between the two states on noise rather than on evidence.
COVER_FACTOR_MIN = 0.05

# How much of the history must have been in each cover state before the two coefficients
# mean anything separately. A pool that was covered every single window says nothing
# about its uncovered losses, and should keep the configured guess rather than invent one.
COVER_SPAN_MIN = 0.05

# How many unknowns the fit carries: COP intercept, COP slope, open loss, covered loss.
UNKNOWNS = 4

# Rows retained for the fit, and what a settled history looks like. Far fewer rows than
# there are samples now that a window spans a measurable change rather than one cycle,
# so the target is lower to match: a store cycling normally produces a handful a day.
SAMPLE_LIMIT = 2000
CONFIDENCE_TARGET_SAMPLES = 30

# Ambient spread the slope needs before it means anything. Below this the ridge holds b
# at its configured value and the fit says the slope was not identified.
COP_FIT_MIN_SPAN_K = 3.0

# How quickly old rows stop counting. A cover left off, or a season turning, should show
# up within a couple of weeks rather than being averaged away forever.
WEIGHT_HALF_LIFE_HOURS = 7 * 24.0

# How much the configured values are worth, in samples. Deliberately weak: swept against
# a simulated store, 5 samples' worth of prior still cost 9% on the loss coefficient and
# a third of the COP slope, while 2 recovers both to within a few percent and a fortnight
# of real rows swamps it entirely. The protection against a wild fit comes from the
# physical bounds and from holding the slope when the ambient never varied, not from
# leaning on the configuration.
RIDGE_SAMPLES = 2.0


def _clamp(value, low, high):
    return max(low, min(high, value))


def _observed_ambient(sample):
    """
    The air temperature this window actually saw.

    The sensor reading where there is one, and only otherwise the value the forecast
    supplied. Both are recorded on every sample, and the fit was reading the wrong one:
    ``ambient_c`` carries the *bias-corrected forecast*, which is the right input for
    predicting a slot nobody has measured yet and the wrong one for learning from a slot
    that has already happened. The correction removes the average error for that hour of
    the day and leaves the rest, so the difference went straight into the temperature
    difference that fixes the loss coefficient - and the windows that fix it hardest are
    the ones overnight, where that correction is largest.
    """
    measured = sample.get("ambient_measured_c")
    if measured is not None:
        value = float(measured)
        if math.isfinite(value):
            return value
    return float(sample["ambient_c"])


def _was_covered(sample):
    """Whether the store was covered, from the recorded state or the older multiplier."""
    covered = sample.get("covered")
    if covered is not None:
        return bool(covered)
    try:
        return float(sample.get("cover_factor", 1.0) or 1.0) < 1.0
    except (TypeError, ValueError):
        return False


def _solve(matrix, rhs):
    """
    Solve a square system by Gauss-Jordan with partial pivoting.

    Hand-rolled rather than pulled from numpy: this package is deliberately free of
    heavy dependencies, and four unknowns is not a reason to acquire one.

    Returns the solution, or None when the system is singular.
    """
    size = len(matrix)
    aug = [list(matrix[i]) + [rhs[i]] for i in range(size)]

    for col in range(size):
        pivot_row = max(range(col, size), key=lambda r, c=col: abs(aug[r][c]))
        if abs(aug[pivot_row][col]) < 1e-12:
            return None
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]

        pivot = aug[col][col]
        for j in range(col, size + 1):
            aug[col][j] /= pivot

        for row in range(size):
            if row == col:
                continue
            factor = aug[row][col]
            if factor == 0.0:
                continue
            for j in range(col, size + 1):
                aug[row][j] -= factor * aug[col][j]

    return [aug[i][size] for i in range(size)]


class ThermalCalibrator:
    """
    Joint estimator for one store's loss coefficient and COP curve.

    Starts from the configured values and moves away from them only as evidence
    accumulates, so a fresh installation behaves sensibly on day one and better by
    week two.
    """

    def __init__(self, volume_m3, surface_m2, loss_coefficient, cop_nominal,
                 air_coefficient=0.0, cover_loss_factor=1.0):
        self.volume_m3 = float(volume_m3 or 0.0)
        self.surface_m2 = float(surface_m2 or 0.0)

        self.configured_loss_coefficient = _clamp(
            float(loss_coefficient or 25.0), LOSS_COEFFICIENT_MIN, LOSS_COEFFICIENT_MAX
        )
        self.configured_cop_nominal = _clamp(float(cop_nominal or 4.0), COP_MIN, COP_MAX)
        self.configured_air_coefficient = _clamp(
            float(air_coefficient or 0.0), -AIR_COEFFICIENT_LIMIT, AIR_COEFFICIENT_LIMIT
        )
        self.configured_cover_loss_factor = _clamp(
            float(cover_loss_factor or 1.0), COVER_FACTOR_MIN, 1.0
        )

        self.loss_coefficient = self.configured_loss_coefficient
        self.cop_nominal = self.configured_cop_nominal
        self.air_coefficient = self.configured_air_coefficient
        self.cover_loss_factor = self.configured_cover_loss_factor

        # Counts of what is *in the fit*, exposed as properties below. They used to be
        # independent counters, which drifted: ``restore`` seeded them from the previous
        # run and the replayed history then added to them, so a card reporting 260
        # periods was describing a fit built from 16.
        self._restored_loss_samples = 0
        self._restored_cop_samples = 0
        self.slope_identified = False
        self.cover_identified = False
        self.residual_w = None
        self.signal_w = None

        self._rows = deque(maxlen=SAMPLE_LIMIT)
        self._latest_timestamp = None
        # Samples since the last closed window, and the finest step the medium sensor
        # has been seen to take. See RESOLUTION_MULTIPLE.
        self._window = []
        self._resolution_k = DEFAULT_RESOLUTION_K
        # What a previous run knew, until this one has rows of its own. Persisted state
        # restores the coefficients but not the history they came from - the samples are
        # replayed separately - and reporting no confidence in the meantime would
        # understate an estimate that is about to be rebuilt.
        self._restored_confidence = None

    # -- ingest -----------------------------------------------------------------------

    def observe(self, sample, refit=True):
        """
        Take one recorded sample. Returns True when it completed a window.

        Samples accumulate until the medium has moved far enough to be measured, the
        appliance switches on or off, or the backstop elapses - then the whole span
        becomes one equation. Pairing consecutive samples instead would divide a
        rounding step by five minutes and call the result a heat flow.

        A completed window re-solves immediately unless *refit* says otherwise, which
        only the bulk replay does. Leaving that out froze the estimate between restarts:
        windows kept accumulating and nothing ever looked at them again, so an install
        running for a week reported the fit it had made in its first minute.
        """
        if not self._usable(sample):
            return False

        closed = False
        if self._window and self._state_changed(self._window[-1], sample):
            # The transition itself describes neither state, so close what came before
            # it and begin again from the new one.
            self._learn_resolution(self._window[-1], sample)
            closed = self._close_window()
            self._window = [sample]
        else:
            if self._window:
                self._learn_resolution(self._window[-1], sample)
            self._window.append(sample)
            if self._window_is_ready():
                closed = self._close_window(keep_last=True)

        if closed and refit:
            self.refit()
        return closed

    def observe_series(self, samples):
        """
        Feed a whole recorded history, oldest first. Returns the windows it produced.

        Fits once at the end rather than after every window: replaying a fortnight is a
        few thousand samples, and the answer only matters after the last one.
        """
        rows_before = len(self._rows)
        self._window = []
        for sample in samples:
            self.observe(sample, refit=False)
        # Whatever is left is still a usable span if it is long enough.
        self._close_window()

        produced = len(self._rows) - rows_before
        if produced:
            self.refit()
        return produced

    # -- windowing ----------------------------------------------------------------------

    @staticmethod
    def _usable(sample):
        """Whether a sample carries the fields a window needs."""
        if not isinstance(sample, dict):
            return False
        try:
            float(sample["medium_c"])
            float(sample["ambient_c"])
            return sample["timestamp"] is not None
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _state_changed(previous, current):
        """Whether the appliance switched on or off between two samples."""
        was_on = float(previous.get("power_w", 0.0) or 0.0) >= IDLE_POWER_W
        is_on = float(current.get("power_w", 0.0) or 0.0) >= IDLE_POWER_W
        return was_on != is_on

    def _learn_resolution(self, previous, current):
        """
        The finest non-zero step the medium sensor takes is its resolution.

        Measured rather than configured: a sensor reporting 29.0 and one reporting
        29.0134 need windows orders of magnitude apart, and only the data knows which
        this is.
        """
        try:
            step = abs(float(current["medium_c"]) - float(previous["medium_c"]))
        except (KeyError, TypeError, ValueError):
            return
        if 0.0 < step < self._resolution_k:
            self._resolution_k = step

    def _window_is_ready(self):
        """Whether the open window has something worth fitting."""
        if len(self._window) < 2:
            return False
        first, last = self._window[0], self._window[-1]
        span = (last["timestamp"] - first["timestamp"]).total_seconds() / 3600.0
        if span < MIN_SAMPLE_HOURS:
            return False
        if span >= MAX_WINDOW_HOURS:
            return True
        moved = abs(float(last["medium_c"]) - float(first["medium_c"]))
        return moved >= RESOLUTION_MULTIPLE * self._resolution_k

    def _close_window(self, keep_last=False):
        """Turn the open window into a row. Returns True when one was produced."""
        if len(self._window) < 2:
            return False

        row = self._row_from(self._window[0], self._window[-1], self._window)
        self._window = [self._window[-1]] if keep_last else []
        if row is None:
            return False

        self._rows.append(row)
        if self._latest_timestamp is None or row["timestamp"] > self._latest_timestamp:
            self._latest_timestamp = row["timestamp"]
        return True

    def _row_from(self, first, last, window):
        """
        One equation, from the whole span rather than from two adjacent readings.

        Power is averaged across the window because that is what actually went in over
        it; the temperatures are the endpoints, because the accumulated heat is the
        difference between them and nothing in between matters.
        """
        try:
            span = (last["timestamp"] - first["timestamp"]).total_seconds() / 3600.0
        except (KeyError, TypeError, AttributeError):
            return None
        if not MIN_SAMPLE_HOURS <= span <= MAX_WINDOW_HOURS * 1.5:
            return None

        try:
            medium_last = float(last["medium_c"])
            medium_first = float(first["medium_c"])
        except (KeyError, TypeError, ValueError):
            return None

        ambients, powers, covers = [], [], []
        for sample in window:
            try:
                ambients.append(_observed_ambient(sample))
                powers.append(float(sample.get("power_w", 0.0) or 0.0))
                covers.append(1.0 if _was_covered(sample) else 0.0)
            except (KeyError, TypeError, ValueError):
                return None
        if not ambients:
            return None

        delta_k = medium_last - medium_first
        if not math.isfinite(delta_k) or abs(delta_k) > MAX_TEMPERATURE_STEP_K:
            return None
        if self.volume_m3 <= 0 or self.surface_m2 <= 0:
            return None

        running = all(power >= IDLE_POWER_W for power in powers)
        idle = all(power < IDLE_POWER_W for power in powers)
        if not running and not idle:
            # A window that spans a switch describes neither state.
            return None

        ambient = sum(ambients) / len(ambients)
        return {
            "timestamp": last["timestamp"],
            # Left-hand side: the rate heat actually accumulated in the store, in watts.
            "stored_w": delta_k / span * WH_PER_M3_PER_K * self.volume_m3,
            "power_w": sum(powers) / len(powers) if running else 0.0,
            "ambient_c": ambient,
            "drive_k": (medium_last + medium_first) / 2.0 - ambient,
            # The share of the window the store spent covered, which is what splits the
            # loss between the two coefficients. Not the multiplier: feeding back a
            # factor this estimator itself produced would close a loop around the fit.
            "covered_share": sum(covers) / len(covers),
            "span_hours": span,
        }

    # -- the fit ------------------------------------------------------------------------

    def _identifiability(self, rows):
        """
        Which unknowns this history can actually speak for, and the covered share.

        The COP slope only means anything if the history spans a range of air
        temperatures. Below that it must be *held* during the solve, not solved for
        and overwritten afterwards: with a constant ambient the power columns are
        collinear, so the fit splits the identifiable sum between the intercept and
        the slope, and discarding the slope afterwards throws that part away. It cost
        a recovered COP of 3.74 where the answer was 4.5.

        The two loss coefficients separate on the same principle. A store that was
        covered through every single window can say what a cover costs and nothing at
        all about going without one, so the coefficient it never observed keeps the
        configured guess rather than absorbing the other one's error.
        """
        on_rows = [row for row in rows if row["power_w"] >= IDLE_POWER_W]
        span = 0.0
        if on_rows:
            temps = [row["ambient_c"] for row in on_rows]
            span = max(temps) - min(temps)
        self.slope_identified = span >= COP_FIT_MIN_SPAN_K

        shares = [row["covered_share"] for row in rows]
        covered_share = sum(shares) / len(shares) if shares else 0.0
        self.cover_identified = COVER_SPAN_MIN <= covered_share <= 1.0 - COVER_SPAN_MIN
        return covered_share

    def _design(self, rows):
        """
        The regressors, one row per window.

        x1 = P_el, x2 = T_air·P_el,
        x3 = -A·(1-covered)·dT  (k while open), x4 = -A·covered·dT  (k while covered)
        """
        design = []
        for row in rows:
            drive = self.surface_m2 * row["drive_k"]
            share = row["covered_share"]
            design.append((
                row["power_w"],
                row["ambient_c"] * row["power_w"],
                -drive * (1.0 - share),
                -drive * share,
            ))
        return design

    def refit(self):
        """
        Re-solve for (a, b, k_open, k_covered) over the retained rows.

        Columns are scaled to comparable magnitudes before solving - power is in
        thousands of watts and the loss term in hundreds, and an unscaled normal-equation
        solve of those is numerically poor.
        """
        rows = list(self._rows)
        if not rows:
            return False

        prior = self._prior()
        weights = self._weights(rows)
        covered_share = self._identifiability(rows)
        design = self._design(rows)
        targets = [row["stored_w"] for row in rows]

        total_weight = sum(weights)
        if total_weight <= 0:
            return False

        # Column scales: root-mean-square, so every column enters the solve at O(1).
        scales = []
        for col in range(UNKNOWNS):
            mean_square = sum(
                w * design[i][col] ** 2 for i, w in enumerate(weights)
            ) / total_weight
            scales.append(math.sqrt(mean_square) if mean_square > 0 else 0.0)

        # A column that is identically zero carries no information about its parameter -
        # no heating at all, or no surface. Hold it at the prior rather than solving for
        # a number the data cannot support. The slope is held on the same footing when
        # the ambient temperature never varied.
        held = [scale <= 0 for scale in scales]
        held[1] = held[1] or not self.slope_identified
        for col in range(UNKNOWNS):
            if scales[col] <= 0:
                scales[col] = 1.0

        ridge = RIDGE_SAMPLES * (total_weight / len(rows))

        normal = [[0.0] * UNKNOWNS for _ in range(UNKNOWNS)]
        rhs = [0.0] * UNKNOWNS
        for i, weight in enumerate(weights):
            scaled = [design[i][col] / scales[col] for col in range(UNKNOWNS)]
            for r in range(UNKNOWNS):
                for c in range(UNKNOWNS):
                    normal[r][c] += weight * scaled[r] * scaled[c]
                rhs[r] += weight * scaled[r] * targets[i]

        # Ridge towards the configured values, in the scaled space. An unidentified
        # coefficient is pinned to its prior the same way the COP slope is.
        prior_scaled = [prior[col] * scales[col] for col in range(UNKNOWNS)]
        pinned = list(held)
        if not self.cover_identified:
            pinned[2 if covered_share > 0.5 else 3] = True
        for col in range(UNKNOWNS):
            strength = ridge * 1e9 if pinned[col] else ridge
            normal[col][col] += strength
            rhs[col] += strength * prior_scaled[col]

        solution = _solve(normal, rhs)
        if solution is None:
            logger.debug("[LOADS] calibration fit is singular - keeping the last estimate")
            return False

        estimate = [solution[col] / scales[col] for col in range(UNKNOWNS)]
        self._apply(estimate, rows, weights, design, targets)
        return True

    def _prior(self):
        """The configured values, as (a, b, k_open, k_covered)."""
        nominal = self.configured_cop_nominal
        slope = nominal * self.configured_air_coefficient
        open_loss = self.configured_loss_coefficient
        return [nominal - slope * COP_REFERENCE_AMBIENT_C, slope,
                open_loss, open_loss * self.configured_cover_loss_factor]

    def _weights(self, rows):
        """Exponential decay by age, so recent behaviour counts for more."""
        if self._latest_timestamp is None:
            return [1.0] * len(rows)
        weights = []
        for row in rows:
            age_hours = (
                self._latest_timestamp - row["timestamp"]
            ).total_seconds() / 3600.0
            weights.append(0.5 ** (max(0.0, age_hours) / WEIGHT_HALF_LIFE_HOURS))
        return weights

    def _apply(self, estimate, rows, weights, design, targets):
        """Bound the solution, convert it to the reported form, and score the fit."""
        intercept, slope, loss, covered_loss = estimate

        loss = _clamp(loss, LOSS_COEFFICIENT_MIN, LOSS_COEFFICIENT_MAX)
        # A cover that made a pool lose heat faster would be a fitting artefact, so the
        # covered coefficient is bounded by the open one rather than trusted past it.
        covered_loss = _clamp(covered_loss, LOSS_COEFFICIENT_MIN, loss)

        nominal = _clamp(
            intercept + slope * COP_REFERENCE_AMBIENT_C, COP_MIN, COP_MAX
        )
        air_coefficient = _clamp(
            slope / nominal if nominal > 0 else 0.0,
            -AIR_COEFFICIENT_LIMIT, AIR_COEFFICIENT_LIMIT,
        )

        self.loss_coefficient = loss
        self.cover_loss_factor = _clamp(covered_loss / loss, COVER_FACTOR_MIN, 1.0)
        self.cop_nominal = nominal
        self.air_coefficient = air_coefficient
        self.residual_w = self._residual(rows, weights, design, targets)
        self.signal_w = self._signal(weights, targets)

    def _residual(self, rows, weights, design, targets):
        """Weighted RMS of what the fitted model fails to explain, in watts."""
        theta = self._theta()
        total_weight = sum(weights)
        if total_weight <= 0:
            return None
        error = 0.0
        for i, weight in enumerate(weights):
            predicted = sum(design[i][col] * theta[col] for col in range(3))
            error += weight * (targets[i] - predicted) ** 2
        return round(math.sqrt(error / total_weight), 1)

    @staticmethod
    def _signal(weights, targets):
        """Weighted RMS of what the model is trying to explain, in watts."""
        total_weight = sum(weights)
        if total_weight <= 0:
            return None
        energy = sum(w * targets[i] ** 2 for i, w in enumerate(weights))
        return round(math.sqrt(energy / total_weight), 1)

    def _theta(self):
        """The current estimate as (a, b, k_open, k_covered)."""
        slope = self.cop_nominal * self.air_coefficient
        return [self.cop_nominal - slope * COP_REFERENCE_AMBIENT_C, slope,
                self.loss_coefficient,
                self.loss_coefficient * self.cover_loss_factor]

    # -- what the fit is built from -----------------------------------------------------

    @property
    def loss_samples(self):
        """Windows in the current fit during which the appliance was idle."""
        if not self._rows:
            return self._restored_loss_samples
        return sum(1 for row in self._rows if row["power_w"] < IDLE_POWER_W)

    @property
    def cop_samples(self):
        """Windows in the current fit during which it was running."""
        if not self._rows:
            return self._restored_cop_samples
        return sum(1 for row in self._rows if row["power_w"] >= IDLE_POWER_W)

    # -- estimates --------------------------------------------------------------------

    def cop_at_reference(self):
        """The fitted nominal COP, at the datasheet reference ambient."""
        return self.cop_nominal

    def confidence(self):
        """
        How much the fit should be trusted, 0 to 1.

        Three things have to be earned:

        - enough rows;
        - enough *variety* among them, because an estimator that has only watched the
          appliance run is solving for the losses and the efficiency from the same
          observations and cannot separate them;
        - and a fit that actually explains the data.

        The third was missing, and it mattered. One installation reported full
        confidence while the residual stood at twelve times the signal it was supposed
        to describe - the number said "trust this" at exactly the moment it should have
        said the opposite.
        """
        rows = len(self._rows)
        if not rows:
            return self._restored_confidence or 0.0
        coverage = min(1.0, rows / CONFIDENCE_TARGET_SAMPLES)
        variety = 1.0 if (self.loss_samples and self.cop_samples) else 0.5
        return round(coverage * variety * self.fit_quality(), 3)

    def fit_quality(self):
        """
        How much of the signal the fit explains, 0 to 1.

        One minus the residual over the signal, floored at zero: a residual as large as
        what it is explaining means the estimate carries no information, however many
        rows produced it.
        """
        if self.residual_w is None or not self.signal_w:
            return 1.0
        return round(max(0.0, 1.0 - self.residual_w / self.signal_w), 3)

    def state(self):
        """Serializable estimator state - persisted, and shown on the API."""
        return {
            "loss_coefficient": round(self.loss_coefficient, 3),
            "cover_loss_factor": round(self.cover_loss_factor, 3),
            "cop_nominal": round(self.cop_nominal, 3),
            "air_coefficient": round(self.air_coefficient, 5),
            "loss_samples": self.loss_samples,
            "cop_samples": self.cop_samples,
            "confidence": self.confidence(),
            "slope_identified": self.slope_identified,
            "cover_identified": self.cover_identified,
            "residual_w": self.residual_w,
            "signal_w": self.signal_w,
            "fit_quality": self.fit_quality(),
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
        cover = state.get("cover_loss_factor")
        if isinstance(cover, (int, float)) and COVER_FACTOR_MIN <= cover <= 1.0:
            self.cover_loss_factor = float(cover)
        air = state.get("air_coefficient")
        if isinstance(air, (int, float)):
            self.air_coefficient = _clamp(
                float(air), -AIR_COEFFICIENT_LIMIT, AIR_COEFFICIENT_LIMIT
            )
        self._restored_loss_samples = max(0, int(state.get("loss_samples", 0) or 0))
        self._restored_cop_samples = max(0, int(state.get("cop_samples", 0) or 0))
        self.slope_identified = bool(state.get("slope_identified", False))
        restored = state.get("confidence")
        if isinstance(restored, (int, float)):
            self._restored_confidence = _clamp(float(restored), 0.0, 1.0)
