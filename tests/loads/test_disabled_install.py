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


# ---------------------------------------------------------------------------
# A setting that is configured but not applied
# ---------------------------------------------------------------------------

def test_a_daily_cap_the_optimizer_cannot_apply_says_so(make_manager, installation, caplog):
    """
    `max_runtime_hours_per_day` reaches the fallback planner and not the solver, so
    under the built-in optimizer it is inert - and the pool preset ships it set to 12.
    A user on defaults would believe in a limit that is not there, and nothing on the
    card or in the log said otherwise.
    """
    from tests.loads.test_instance import POOL, _run

    manager = make_manager([dict(POOL, max_runtime_hours_per_day=12)])
    manager.external_scheduler = True
    with caplog.at_level("WARNING"):
        _run(manager, installation, water_c=24.0)

    assert "not being applied" in caplog.text
    assert "12 h/day" in caplog.text

    # Once, not once a cycle.
    caplog.clear()
    with caplog.at_level("WARNING"):
        _run(manager, installation, water_c=24.0)
    assert "not being applied" not in caplog.text


def test_no_cap_configured_says_nothing(make_manager, installation, caplog):
    from tests.loads.test_instance import POOL, _run

    manager = make_manager([dict(POOL, max_runtime_hours_per_day=0)])
    manager.external_scheduler = True
    with caplog.at_level("WARNING"):
        _run(manager, installation, water_c=24.0)
    assert "not being applied" not in caplog.text


def test_the_fallback_planner_applies_it_so_stays_quiet(make_manager, installation, caplog):
    """Without an optimizer to defer to, the cap is enforced and there is nothing to say."""
    from tests.loads.test_instance import POOL, _run

    manager = make_manager([dict(POOL, max_runtime_hours_per_day=12)])
    manager.external_scheduler = False
    with caplog.at_level("WARNING"):
        _run(manager, installation, water_c=24.0)
    assert "not being applied" not in caplog.text
