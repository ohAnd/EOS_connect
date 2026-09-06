"""
Calibration: does a simulated store with known coefficients read back as itself.

The simulator below is the physics run forwards; the calibrator is the same physics run
backwards. If the two agree the estimator is recovering real parameters rather than
fitting noise.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.loads.models.calibration import (
    AIR_COEFFICIENT_LIMIT,
    IDLE_POWER_W,
    ThermalCalibrator,
)
from src.loads.models.thermal_physics import WH_PER_M3_PER_K, cop_at

VOLUME = 30.0
SURFACE = 32.0
TRUE_K = 25.0
TRUE_COP = 4.5
TRUE_AIR_COEFF = 0.03
T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _simulate(hours, step_minutes, medium_c, ambient_c, power_w, cover_factor=1.0,
              start=T0):
    """
    Roll the store forward and emit the samples a real installation would record.

    Returns a list of ``{timestamp, medium_c, ambient_c, power_w, cover_factor}``.
    """
    samples = []
    step_h = step_minutes / 60.0
    moment = start
    temp = medium_c
    for _ in range(int(hours / step_h) + 1):
        samples.append({
            "timestamp": moment,
            "medium_c": temp,
            "ambient_c": ambient_c,
            "power_w": power_w,
            "cover_factor": cover_factor,
        })
        loss_w = TRUE_K * SURFACE * (temp - ambient_c) * cover_factor
        thermal_w = 0.0
        if power_w >= IDLE_POWER_W:
            thermal_w = power_w * cop_at(ambient_c, TRUE_COP, TRUE_AIR_COEFF)
        temp += (thermal_w - loss_w) * step_h / (WH_PER_M3_PER_K * VOLUME)
        moment += timedelta(minutes=step_minutes)
    return samples


def _calibrator(loss=60.0, cop=3.0):
    """Deliberately wrong starting values, as a real user's guess would be."""
    return ThermalCalibrator(VOLUME, SURFACE, loss, cop)


def test_the_loss_coefficient_converges_on_the_true_value():
    cal = _calibrator(loss=60.0)
    samples = _simulate(hours=60, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    used = cal.observe_series(samples)

    assert used > 100
    assert cal.loss_coefficient == pytest.approx(TRUE_K, rel=0.05)


def test_a_cover_is_accounted_for_rather_than_read_as_a_better_pool():
    """A covered pool loses less; the coefficient must stay the same."""
    cal = _calibrator(loss=60.0)
    samples = _simulate(
        hours=60, step_minutes=15, medium_c=28.0, ambient_c=16.0,
        power_w=0.0, cover_factor=0.3,
    )
    cal.observe_series(samples)
    assert cal.loss_coefficient == pytest.approx(TRUE_K, rel=0.05)


def test_the_cop_converges_on_the_true_value():
    cal = _calibrator(cop=3.0)
    cal.loss_coefficient = TRUE_K  # isolate the COP fit from the loss fit
    samples = _simulate(
        hours=30, step_minutes=15, medium_c=22.0, ambient_c=26.0, power_w=2000.0
    )
    cal.observe_series(samples)
    assert cal.cop_at_reference() == pytest.approx(TRUE_COP, rel=0.05)


def test_the_cop_slope_is_recovered_from_a_range_of_ambient_temperatures():
    """
    Fitting needs the samples to span a temperature range, which a real season provides.
    """
    cal = _calibrator(cop=3.0)
    cal.loss_coefficient = TRUE_K
    start = T0
    for ambient in (8.0, 14.0, 20.0, 26.0, 30.0):
        samples = _simulate(
            hours=8, step_minutes=15, medium_c=22.0, ambient_c=ambient,
            power_w=2000.0, start=start,
        )
        cal.observe_series(samples)
        start += timedelta(days=1)

    assert cal.air_coefficient == pytest.approx(TRUE_AIR_COEFF, abs=0.01)
    assert cal.cop_at_reference() == pytest.approx(TRUE_COP, rel=0.05)


def test_a_single_ambient_temperature_produces_no_slope():
    """A week of identical afternoons must not yield a confident gradient."""
    cal = _calibrator(cop=3.0)
    cal.loss_coefficient = TRUE_K
    samples = _simulate(
        hours=30, step_minutes=15, medium_c=22.0, ambient_c=26.0, power_w=2000.0
    )
    cal.observe_series(samples)
    assert cal.air_coefficient == pytest.approx(0.0, abs=1e-9)


def test_confidence_rises_with_evidence_and_needs_both_halves():
    """Watching the pump run says nothing about how the pool cools."""
    cal = _calibrator()
    assert cal.confidence() == 0.0

    cal.observe_series(
        _simulate(hours=30, step_minutes=15, medium_c=22.0, ambient_c=26.0, power_w=2000.0)
    )
    heating_only = cal.confidence()
    assert 0.0 < heating_only <= 0.5

    cal.observe_series(
        _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0,
                  start=T0 + timedelta(days=5))
    )
    assert cal.confidence() > heating_only


def test_pairs_spanning_a_state_change_are_ignored():
    """Half an interval heating and half cooling is neither measurement."""
    cal = _calibrator()
    pair = [
        {"timestamp": T0, "medium_c": 25.0, "ambient_c": 18.0, "power_w": 0.0},
        {"timestamp": T0 + timedelta(minutes=15), "medium_c": 25.2,
         "ambient_c": 18.0, "power_w": 2000.0},
    ]
    assert cal.observe_pair(pair[0], pair[1]) is False
    assert cal.loss_samples == 0
    assert cal.cop_samples == 0


@pytest.mark.parametrize("minutes", [1, 240])
def test_pairs_too_close_together_or_too_far_apart_are_ignored(minutes):
    cal = _calibrator()
    first = {"timestamp": T0, "medium_c": 28.0, "ambient_c": 16.0, "power_w": 0.0}
    second = {
        "timestamp": T0 + timedelta(minutes=minutes),
        "medium_c": 27.9, "ambient_c": 16.0, "power_w": 0.0,
    }
    assert cal.observe_pair(first, second) is False


def test_malformed_samples_do_not_raise():
    cal = _calibrator()
    first = {"timestamp": T0, "medium_c": "warm", "ambient_c": 16.0, "power_w": 0.0}
    second = {"timestamp": T0 + timedelta(minutes=15), "medium_c": 27.9,
              "ambient_c": 16.0, "power_w": 0.0}
    assert cal.observe_pair(first, second) is False
    assert cal.observe_pair({}, {}) is False


def test_state_survives_a_restart():
    cal = _calibrator()
    cal.observe_series(
        _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )
    saved = cal.state()

    restored = _calibrator()
    restored.restore(saved)
    assert restored.loss_coefficient == pytest.approx(saved["loss_coefficient"])
    assert restored.loss_samples == saved["loss_samples"]
    assert restored.confidence() == saved["confidence"]


@pytest.mark.parametrize("bad", [
    {"loss_coefficient": -5},
    {"loss_coefficient": 100000},
    {"cop_nominal": 42},
    None,
    "not a dict",
])
def test_restoring_implausible_state_keeps_the_configured_values(bad):
    cal = _calibrator(loss=60.0, cop=3.0)
    cal.restore(bad)
    assert cal.loss_coefficient == 60.0
    assert cal.cop_nominal == 3.0


def test_the_air_coefficient_is_bounded():
    cal = _calibrator()
    cal.restore({"air_coefficient": 99.0})
    assert cal.air_coefficient == AIR_COEFFICIENT_LIMIT
