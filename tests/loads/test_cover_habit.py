"""
Learning when the cover is usually on.

A pool cover changes the heat loss by roughly two thirds and goes on and off daily. The
model read the switch once and assumed that state for two days, so planning at nine in
the evening with the cover just pulled over predicted a covered pool through the whole
of the next afternoon.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.loads.models.cover_habit import HALF_LIFE_DAYS, CoverHabit

T0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)


def _nights(habit, days, covered_hours=range(22, 24), start=T0):
    """Record a household that covers the pool over the given hours, every day."""
    moment = start
    for _ in range(days):
        for hour in range(24):
            habit.observe(moment.replace(hour=hour), hour in covered_hours)
        moment += timedelta(days=1)


def test_an_hour_with_no_history_says_so():
    """None is a real answer: the caller defers to the switch rather than guessing."""
    assert CoverHabit().probability(3) is None


def test_one_day_is_not_yet_a_habit():
    habit = CoverHabit()
    _nights(habit, days=1)
    assert habit.probability(22) is None


def test_two_days_of_agreement_is():
    habit = CoverHabit()
    _nights(habit, days=3)
    assert habit.probability(22) == pytest.approx(1.0)
    assert habit.probability(12) == pytest.approx(0.0)


def test_a_partial_habit_is_reported_as_a_fraction():
    """Four nights in five is worth four fifths of a cover, not a rounded yes or no."""
    habit = CoverHabit()
    moment = T0
    for day in range(10):
        habit.observe(moment.replace(hour=23), day % 5 != 0)
        moment += timedelta(days=1)

    assert 0.6 < habit.probability(23) < 0.95


def test_recent_days_count_for_more():
    """A cover that starts being left off should show up within a week."""
    habit = CoverHabit()
    _nights(habit, days=10, covered_hours=range(22, 24))
    covered_before = habit.probability(23)

    _nights(habit, days=10, covered_hours=(), start=T0 + timedelta(days=10))

    assert covered_before == pytest.approx(1.0)
    # Not erased -- ten days of the old habit still carry some weight -- but clearly
    # outvoted by the recent behaviour.
    assert habit.probability(23) < 0.35


def test_the_half_life_is_what_decays_it():
    habit = CoverHabit(half_life_days=HALF_LIFE_DAYS)
    habit.observe(T0.replace(hour=5), True)
    habit.observe(T0.replace(hour=5), True)
    assert habit.probability(5) == pytest.approx(1.0)

    # A fortnight later, one contrary observation outweighs the decayed pair.
    habit.observe(T0 + timedelta(days=28, hours=5), False)
    assert habit.probability(5) is None or habit.probability(5) < 0.5


def test_hours_known_counts_only_hours_with_evidence():
    habit = CoverHabit()
    moment = T0
    for _ in range(3):
        for hour in (22, 23):
            habit.observe(moment.replace(hour=hour), True)
        moment += timedelta(days=1)

    assert habit.hours_known() == 2


def test_the_summary_names_the_covered_hours():
    habit = CoverHabit()
    _nights(habit, days=3, covered_hours=(22, 23))
    assert habit.state()["covered_hours"] == [22, 23]


def test_a_reset_forgets_the_pattern():
    habit = CoverHabit()
    _nights(habit, days=5)
    habit.reset()
    assert habit.probability(22) is None
    assert habit.hours_known() == 0


def test_a_missing_timestamp_is_ignored():
    assert CoverHabit().observe(None, True) is False


def test_an_hour_seen_once_a_day_becomes_known_on_the_third():
    """
    Two observations do not quite reach the threshold, because by the time the second
    arrives the first has already aged. Worth pinning: it is the difference between
    deferring to the switch and speaking for a whole hour of the plan.
    """
    habit = CoverHabit()
    for day in range(2):
        habit.observe(T0 + timedelta(days=day, hours=9), True)
    assert habit.probability(9) is None

    habit.observe(T0 + timedelta(days=2, hours=9), True)
    assert habit.probability(9) == pytest.approx(1.0)
