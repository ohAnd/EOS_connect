"""
Learning how far a site sits from the model that forecasts it.

One installation's weather station read 4 K below every model tried -- ICON-D2, ICON-EU,
ECMWF and GFS all agreed with each other and none with the garden, which is what cold
air draining into a hollow overnight looks like. The important part is that such a bias
is *not* constant through the day, so one averaged number is wrong at both ends of it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.loads.ambient_bias import (
    IMPLAUSIBLE_GAP_K,
    MAX_OFFSET_K,
    MIN_GLOBAL_WEIGHT,
    AmbientBias,
)

T0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def _observe_hour(bias, hour, gap, times=5, start=T0):
    """Record the same disagreement at one hour of the day, on consecutive days."""
    moment = start.replace(hour=hour)
    for _ in range(times):
        bias.observe(moment, 20.0 + gap, 20.0)
        moment += timedelta(days=1)


def test_nothing_learned_means_no_correction():
    assert AmbientBias().offset(3) == 0.0


def test_one_observation_is_not_a_bias():
    bias = AmbientBias()
    bias.observe(T0.replace(hour=3), 16.0, 20.0)
    assert bias.offset(3) == 0.0


def test_a_repeated_disagreement_becomes_the_offset():
    bias = AmbientBias()
    _observe_hour(bias, hour=3, gap=-4.0)
    assert bias.offset(3) == pytest.approx(-4.0, abs=0.1)


def test_each_hour_keeps_its_own_offset():
    """
    The whole reason for this: cold air pools at night and burns off by mid-morning, so
    a single number splits the difference and is wrong at both ends.

    Recorded day by day and in order, as an installation actually would -- feeding one
    hour's whole history before the other's decays the first while the second is being
    laid down.
    """
    bias = AmbientBias()
    day = T0
    for _ in range(5):
        bias.observe(day.replace(hour=3), 20.0 - 4.5, 20.0)    # cold before dawn
        bias.observe(day.replace(hour=15), 20.0 - 0.5, 20.0)   # close to it by afternoon
        day += timedelta(days=1)

    assert bias.offset(3) == pytest.approx(-4.5, abs=0.2)
    assert bias.offset(15) == pytest.approx(-0.5, abs=0.2)
    # And the average of the two would have been wrong for both.
    assert abs(bias.offset(3) - bias.offset(15)) > 3.0


def test_an_unseen_hour_falls_back_to_the_all_day_average():
    """Better than pretending the site matches the model, once there is enough history."""
    bias = AmbientBias()
    for hour in range(0, 24, 2):
        _observe_hour(bias, hour=hour, gap=-3.0, times=3)

    assert bias.offset(3) == 0.0 or bias.offset(3) == pytest.approx(-3.0, abs=0.3)
    # An odd hour was never observed, but the site average is well established.
    assert bias.offset(7) == pytest.approx(-3.0, abs=0.3)


def test_an_unseen_hour_with_little_history_is_left_alone():
    bias = AmbientBias()
    _observe_hour(bias, hour=3, gap=-4.0, times=4)
    assert sum(bias._weight) < MIN_GLOBAL_WEIGHT
    assert bias.offset(15) == 0.0


def test_recent_days_count_for_more():
    bias = AmbientBias()
    _observe_hour(bias, hour=3, gap=-5.0, times=8)
    assert bias.offset(3) == pytest.approx(-5.0, abs=0.3)

    _observe_hour(bias, hour=3, gap=0.0, times=8, start=T0 + timedelta(days=8))
    assert abs(bias.offset(3)) < 2.0


def test_the_offset_is_bounded():
    """A sensor reporting in the wrong unit must not run away with the horizon."""
    bias = AmbientBias()
    _observe_hour(bias, hour=3, gap=-18.0, times=10)
    assert bias.offset(3) == pytest.approx(-MAX_OFFSET_K)
    assert 3 in bias.saturated_hours()


def test_an_impossible_gap_is_rejected_rather_than_bounded():
    """Beyond a point the two are not describing the same air at all."""
    bias = AmbientBias()
    for _ in range(10):
        assert bias.observe(T0, 20.0, 20.0 + IMPLAUSIBLE_GAP_K + 5) is False
    assert bias.offset(0) == 0.0


@pytest.mark.parametrize("args", [
    (None, 20.0, 16.0),
    (T0, None, 16.0),
    (T0, 20.0, None),
    (T0, "warm", 16.0),
])
def test_unusable_observations_are_ignored(args):
    assert AmbientBias().observe(*args) is False


def test_the_summary_reports_what_is_known():
    bias = AmbientBias()
    _observe_hour(bias, hour=3, gap=-4.0)
    state = bias.state()

    assert state["hours_known"] == 1
    assert state["mean_offset_k"] == pytest.approx(-4.0, abs=0.1)
    assert state["saturated_hours"] == []


def test_a_reset_forgets_everything():
    bias = AmbientBias()
    _observe_hour(bias, hour=3, gap=-4.0)
    bias.reset()
    assert bias.offset(3) == 0.0
    assert bias.state()["mean_offset_k"] is None


def test_the_offsets_are_reported_hour_by_hour():
    """
    A correction averaging -5 K across a horizon looks identical from outside to one
    that is -8 K overnight and zero at noon, and only the second is right. Without the
    per-hour figures there is no way to tell which you have.
    """
    bias = AmbientBias()
    day = T0
    for _ in range(5):
        bias.observe(day.replace(hour=3), 12.0, 20.0)     # -8 before dawn
        bias.observe(day.replace(hour=14), 20.0, 20.0)    # spot on by afternoon
        day += timedelta(days=1)

    by_hour = bias.state()["offset_by_hour"]
    assert len(by_hour) == 24
    assert by_hour[3] == pytest.approx(-8.0, abs=0.3)
    assert by_hour[14] == pytest.approx(0.0, abs=0.3)
    # An hour with no history of its own reports nothing rather than the average.
    assert by_hour[9] is None
