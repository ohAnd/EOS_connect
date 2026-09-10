"""
Calibration: does a simulated store with known coefficients read back as itself.

The simulator below is the physics run forwards; the calibrator is the same physics run
backwards. If the two agree the estimator is recovering real parameters rather than
fitting noise.
"""

import math
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
    windows = cal.observe_series(samples)

    # Windows, not samples: each one spans a measurable change rather than one cycle.
    assert windows > 20
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

    The appliance also has to be seen both running and idle at those temperatures, which
    is what a plan actually produces. With the pump on continuously a colder day raises
    the losses and lowers the efficiency together, so the two cannot be told apart from
    the data - see ``test_a_confounded_history_is_reported_as_such``.
    """
    cal = _calibrator(cop=3.0)
    start, temperature = T0, 24.0
    for ambient in (8.0, 14.0, 20.0, 26.0, 30.0):
        for power in (2000.0, 0.0, 2000.0, 0.0):
            samples = _simulate(
                hours=4, step_minutes=15, medium_c=temperature, ambient_c=ambient,
                power_w=power, start=start,
            )
            cal.observe_series(samples)
            temperature = samples[-1]["medium_c"]
            start += timedelta(hours=4)
        start += timedelta(hours=8)

    assert cal.slope_identified is True
    assert cal.air_coefficient == pytest.approx(TRUE_AIR_COEFF, abs=0.01)
    assert cal.cop_at_reference() == pytest.approx(TRUE_COP, rel=0.05)
    # And the loss coefficient comes out of the same fit, without a separate pass.
    assert cal.loss_coefficient == pytest.approx(TRUE_K, rel=0.15)


def test_a_confounded_history_is_reported_as_such():
    """
    An appliance never seen idle cannot have its losses separated from its efficiency:
    both scale with the same temperature difference. The fit leans on the configured
    values rather than inventing a split, and says the history lacked the variety.
    """
    cal = _calibrator(cop=3.0)
    start, temperature = T0, 24.0
    for ambient in (8.0, 14.0, 20.0, 26.0, 30.0):
        samples = _simulate(
            hours=8, step_minutes=15, medium_c=temperature, ambient_c=ambient,
            power_w=2000.0, start=start,
        )
        cal.observe_series(samples)
        temperature = samples[-1]["medium_c"]
        start += timedelta(days=1)

    assert cal.loss_samples == 0
    # Half marks at most: it has never watched the store cool.
    assert cal.confidence() <= 0.5


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


def test_a_window_never_spans_a_switch():
    """Half of it heating and half cooling describes neither state."""
    cal = _calibrator()
    cal.observe({"timestamp": T0, "medium_c": 25.0, "ambient_c": 18.0, "power_w": 0.0})
    cal.observe({"timestamp": T0 + timedelta(minutes=15), "medium_c": 25.0,
                 "ambient_c": 18.0, "power_w": 0.0})
    # The pump comes on: the window so far is closed as an idle one, and the heating
    # period starts a fresh window rather than being mixed into it.
    cal.observe({"timestamp": T0 + timedelta(minutes=30), "medium_c": 25.2,
                 "ambient_c": 18.0, "power_w": 2000.0})

    assert cal.loss_samples == 1
    assert cal.cop_samples == 0


def test_a_window_shorter_than_the_minimum_is_not_closed():
    """Below it the elapsed time is too small to divide a temperature change by."""
    cal = _calibrator()
    cal.observe({"timestamp": T0, "medium_c": 28.0, "ambient_c": 16.0, "power_w": 0.0})
    closed = cal.observe({"timestamp": T0 + timedelta(minutes=1), "medium_c": 27.5,
                          "ambient_c": 16.0, "power_w": 0.0})
    assert closed is False
    assert len(cal._rows) == 0


def test_a_window_closes_on_the_backstop_even_if_nothing_moved():
    """
    A store that barely changes over eight hours is not uninformative - that is a tight
    bound on its losses, and the most valuable row a well-insulated one produces.
    """
    cal = _calibrator()
    cal.observe({"timestamp": T0, "medium_c": 28.0, "ambient_c": 16.0, "power_w": 0.0})
    closed = cal.observe({"timestamp": T0 + timedelta(hours=9), "medium_c": 28.0,
                          "ambient_c": 16.0, "power_w": 0.0})
    assert closed is True
    assert cal.loss_samples == 1


def test_malformed_samples_do_not_raise():
    cal = _calibrator()
    assert cal.observe({"timestamp": T0, "medium_c": "warm", "ambient_c": 16.0,
                        "power_w": 0.0}) is False
    assert cal.observe({}) is False
    assert cal.observe(None) is False


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


# ── The joint fit ───────────────────────────────────────────────────────────────
#
# The old scheme measured the losses while the appliance was off, then used them to
# measure the COP while it was on. That has a circular dependency, and a real pool walks
# into it: held at target it barely changes temperature while heating, so the *assumed*
# loss term dominated the COP measurement. One installation reported a COP of 1.3 and an
# efficiency that improved as it got colder.


def test_the_loss_coefficient_is_learned_while_the_appliance_is_running():
    """
    The property the old scheme could not have: no off-period is *required*, because
    both parameters come out of the same equations.
    """
    cal = _calibrator(loss=60.0, cop=3.0)
    start, temperature = T0, 24.0
    for ambient in (10.0, 18.0, 26.0):
        samples = _simulate(
            hours=6, step_minutes=15, medium_c=temperature, ambient_c=ambient,
            power_w=2000.0, start=start,
        )
        cal.observe_series(samples)
        temperature = samples[-1]["medium_c"]
        start += timedelta(days=1)

    assert cal.loss_samples == 0          # never once seen idle
    assert cal.loss_coefficient < 45.0    # yet moved a long way off the configured 60


def test_a_wrong_configured_loss_does_not_poison_the_cop():
    """
    The failure that prompted this. Starting from a badly wrong loss coefficient, the
    COP must still come out right rather than absorbing the error.
    """
    cal = _calibrator(loss=90.0, cop=3.0)
    start, temperature = T0, 24.0
    for ambient in (12.0, 20.0, 28.0):
        for power in (2000.0, 0.0):
            samples = _simulate(
                hours=5, step_minutes=15, medium_c=temperature, ambient_c=ambient,
                power_w=power, start=start,
            )
            cal.observe_series(samples)
            temperature = samples[-1]["medium_c"]
            start += timedelta(hours=5)

    assert cal.cop_at_reference() == pytest.approx(TRUE_COP, rel=0.1)
    assert cal.loss_coefficient == pytest.approx(TRUE_K, rel=0.25)


def test_recent_behaviour_counts_for_more_than_old():
    """A cover left off, or a season turning, has to show up inside a fortnight."""
    cal = _calibrator(loss=25.0, cop=4.5)

    # A fortnight ago the store lost heat at the configured rate.
    cal.observe_series(
        _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0,
                  power_w=0.0, start=T0)
    )
    old_estimate = cal.loss_coefficient

    # Since then it has been losing far more. The recent rows should dominate.
    global TRUE_K  # pylint: disable=global-statement
    previous, TRUE_K = TRUE_K, 60.0
    try:
        cal.observe_series(
            _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0,
                      power_w=0.0, start=T0 + timedelta(days=13))
        )
    finally:
        TRUE_K = previous

    assert cal.loss_coefficient > old_estimate * 1.5


def test_the_fit_reports_what_it_could_not_determine():
    cal = _calibrator()
    cal.observe_series(
        _simulate(hours=20, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )
    state = cal.state()

    assert state["slope_identified"] is False     # never ran, so no COP evidence at all
    assert state["residual_w"] is not None        # and how well the fit explains the data
    assert 0.0 <= state["confidence"] <= 1.0


def test_a_singular_history_keeps_the_previous_estimate():
    """Two identical rows determine nothing; the estimate must not become a NaN."""
    cal = _calibrator(loss=30.0, cop=4.0)
    sample = {
        "timestamp": T0, "medium_c": 25.0, "ambient_c": 25.0,
        "power_w": 0.0, "cover_factor": 1.0,
    }
    cal.observe(sample)
    cal.observe(dict(sample, timestamp=T0 + timedelta(hours=9)))

    assert math.isfinite(cal.loss_coefficient)
    assert math.isfinite(cal.cop_nominal)


def test_a_restored_estimate_reports_the_confidence_it_was_saved_with():
    """
    The samples are replayed separately at start-up, so between restoring the
    coefficients and replaying the history there are no rows to score.
    """
    cal = _calibrator()
    cal.restore({"loss_coefficient": 22.0, "cop_nominal": 4.2, "confidence": 0.75})
    assert cal.confidence() == 0.75

    cal.observe_series(
        _simulate(hours=20, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )
    # Once there is real history it scores that instead.
    assert cal.confidence() != 0.75


# ── Windows sized to the sensor, not to the cycle ───────────────────────────────
#
# A store's temperature is read to some finite precision, and the fit divides that
# reading by the elapsed time. On an 18 m3 pool sampled every five minutes, one 0.1 C
# tick is 25 kW against a real loss of about 1 kW -- so every row was a rounding
# artefact twenty-five times the signal, and the fit could not tell a loss coefficient
# of 15 from one of 25. It reported full confidence while doing so.


def _quantised(hours, step_minutes, medium_c, ambient_c, power_w, start=T0,
               resolution=0.1, true_k=None):
    """Like `_simulate`, but reporting the medium only to *resolution*."""
    global TRUE_K  # pylint: disable=global-statement
    previous = TRUE_K
    if true_k is not None:
        TRUE_K = true_k
    try:
        samples = _simulate(
            hours=hours, step_minutes=step_minutes, medium_c=medium_c,
            ambient_c=ambient_c, power_w=power_w, start=start,
        )
    finally:
        TRUE_K = previous
    for sample in samples:
        steps = round(sample["medium_c"] / resolution)
        sample["medium_c"] = round(steps * resolution, 6)
    return samples


def test_the_sensor_resolution_is_measured_rather_than_assumed():
    """A sensor reporting 29.0 and one reporting 29.0134 need very different windows."""
    coarse = _calibrator()
    coarse.observe_series(_quantised(20, 5, 28.0, 16.0, 0.0, resolution=0.1))
    assert coarse._resolution_k == pytest.approx(0.1, abs=1e-6)

    fine = _calibrator()
    fine.observe_series(
        _simulate(hours=20, step_minutes=5, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )
    assert fine._resolution_k < 0.05


def test_a_coarse_sensor_produces_fewer_but_longer_windows():
    coarse = _calibrator()
    coarse.observe_series(_quantised(40, 5, 28.0, 16.0, 0.0, resolution=0.1))

    fine = _calibrator()
    fine.observe_series(
        _simulate(hours=40, step_minutes=5, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )

    assert len(coarse._rows) < len(fine._rows)
    spans = [row["span_hours"] for row in coarse._rows]
    assert min(spans) > 5 / 60.0


def test_a_coarse_sensor_still_recovers_the_loss_coefficient():
    """The point of the exercise: 0.1 C readings must not defeat the fit."""
    cal = _calibrator(loss=60.0)
    cal.observe_series(_quantised(60, 5, 28.0, 16.0, 0.0, resolution=0.1))

    assert cal.loss_coefficient == pytest.approx(TRUE_K, rel=0.15)


def test_quantisation_no_longer_swamps_the_signal():
    """Pairing consecutive samples made the residual many times the signal."""
    cal = _calibrator(loss=60.0)
    cal.observe_series(_quantised(60, 5, 28.0, 16.0, 0.0, resolution=0.1))

    assert cal.residual_w < cal.signal_w


# ── Confidence has to notice a bad fit ──────────────────────────────────────────


def test_a_fit_that_explains_nothing_is_not_confident():
    """
    The failure this exists for: full confidence reported while the residual stood at
    twelve times the signal.
    """
    cal = _calibrator()
    cal.observe_series(
        _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )
    cal.observe_series(
        _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0,
                  power_w=2000.0, start=T0 + timedelta(days=2))
    )
    settled = cal.confidence()

    # Now make the fit look hopeless without touching the row count.
    cal.residual_w = (cal.signal_w or 1000.0) * 12.0
    assert cal.fit_quality() == 0.0
    assert cal.confidence() == 0.0
    assert cal.confidence() < settled


def test_fit_quality_is_reported_alongside_the_numbers():
    cal = _calibrator()
    cal.observe_series(
        _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )
    state = cal.state()

    assert 0.0 <= state["fit_quality"] <= 1.0
    assert state["signal_w"] is not None
    assert state["confidence"] <= state["fit_quality"] + 1e-9


def test_the_reported_period_counts_describe_the_current_fit():
    """
    They used to be independent counters that ``restore`` seeded and the replayed
    history then added to, so a card reporting 260 observed periods was describing a fit
    built from 16 of them.
    """
    cal = _calibrator()
    cal.restore({"loss_coefficient": 22.0, "loss_samples": 202, "cop_samples": 58})
    # With no rows yet, the persisted figures are all there is to report.
    assert cal.loss_samples == 202

    cal.observe_series(
        _simulate(hours=30, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )

    assert cal.loss_samples == len(cal._rows)
    assert cal.cop_samples == 0
    assert cal.loss_samples < 202


def test_a_window_closed_while_running_updates_the_fit():
    """
    The estimate used to be re-solved only by the bulk replay, which runs at start-up
    and nowhere else. Windows kept accumulating and nothing looked at them again, so an
    install running for a week reported the fit it had made in its first minute -- rows
    rising, every fitted value bit-identical.
    """
    cal = _calibrator(loss=60.0)
    samples = _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0,
                        power_w=0.0)

    # Feed them the way the poll thread does: one at a time, no bulk replay.
    for sample in samples:
        cal.observe(sample)

    assert len(cal._rows) > 1
    assert cal.loss_coefficient == pytest.approx(TRUE_K, rel=0.2)
    assert cal.residual_w is not None


def test_the_estimate_keeps_moving_as_windows_arrive():
    cal = _calibrator(loss=60.0)
    first = _simulate(hours=12, step_minutes=15, medium_c=28.0, ambient_c=16.0,
                      power_w=0.0)
    for sample in first:
        cal.observe(sample)
    after_first = cal.loss_coefficient

    second = _simulate(hours=12, step_minutes=15, medium_c=first[-1]["medium_c"],
                       ambient_c=16.0, power_w=0.0,
                       start=first[-1]["timestamp"] + timedelta(minutes=15))
    for sample in second:
        cal.observe(sample)

    assert cal.loss_coefficient != after_first
    assert cal.loss_coefficient == pytest.approx(TRUE_K, rel=0.2)


def test_the_bulk_replay_still_solves_only_once(monkeypatch):
    """Replaying a fortnight should not re-solve a few hundred times over."""
    cal = _calibrator()
    calls = {"n": 0}
    real = cal.refit

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(cal, "refit", counting)
    cal.observe_series(
        _simulate(hours=40, step_minutes=15, medium_c=28.0, ambient_c=16.0, power_w=0.0)
    )
    assert calls["n"] == 1
