"""
An install with no managed loads must behave as though the feature were not there.

The feature is opt-in and most installs will never configure a load, so every entry
point has to be a no-op rather than a cheap one: the household forecast untouched,
nothing offered to the optimizer, and the subsystem reporting itself off so the
dashboard does not grow a tile for it.

Worth its own file because the cost of getting this wrong is paid by users who never
asked for the feature, and none of the feature's own tests would notice.
"""

from zoneinfo import ZoneInfo

from src.loads.manager import ManagedLoadManager, ManagedLoadSources

BERLIN = ZoneInfo("Europe/Berlin")


def _empty():
    return ManagedLoadManager(
        [], time_frame_base=3600, time_zone=BERLIN,
        sources=ManagedLoadSources(read_sensor=lambda name: None),
    )


def test_the_household_forecast_comes_back_untouched():
    manager = _empty()
    base = [1234.0] * 48
    result = manager.apply(base, 3600)

    assert result == base
    # A copy, not the caller's list: both series are backed by a cache on the
    # interface that produced them, and mutating one in place is a bug this
    # codebase has already paid for once.
    assert result is not base


def test_the_optimizer_is_offered_nothing():
    assert _empty().schedulable() == []


def test_the_subsystem_reports_itself_off():
    status = _empty().status()
    assert status["enabled"] is False
    assert status["loads"] == []


def test_a_cycle_does_nothing_and_raises_nothing():
    manager = _empty()
    manager.run_cycle()
    assert manager.status()["loads"] == []

