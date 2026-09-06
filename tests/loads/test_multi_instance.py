"""
Several managed loads at once - the question that shaped the design.

A user adds a pool heat pump, then a sauna, then a heating profile pushed from Home
Assistant. All of their energy needs must land in one `gesamtlast`, none of them may be
counted twice, and they must not all pile into the same cheap hour.
"""

import pytest

from src.loads.presets import (
    TYPE_EXTERNAL_PROFILE,
    TYPE_POOL_HEATPUMP,
    TYPE_SAUNA,
)

OPEN = {
    "window_start": None, "window_end": None,
    "season_start": None, "season_end": None,
    "min_ambient_temp_c": None, "min_runtime_minutes": 0,
}

POOL = dict(
    OPEN, id="pool", type=TYPE_POOL_HEATPUMP, temp_sensor="sensor.pool",
    power_sensor="sensor.pool_power", target_temp=28.0, rated_power_w=1800.0,
    priority=10,
)
SAUNA = dict(
    OPEN, id="sauna", type=TYPE_SAUNA, temp_sensor="sensor.sauna",
    power_sensor="sensor.sauna_power", target_temp=90.0, rated_power_w=2400.0,
    priority=20, deadline_hours=None,
)


def _cold(installation):
    installation.sensors.update({
        "sensor.pool": 24.0, "sensor.pool_power": 0.0,
        "sensor.sauna": 20.0, "sensor.sauna_power": 0.0,
    })


def test_every_contribution_lands_in_one_load_forecast(make_manager, installation):
    manager = make_manager([POOL, SAUNA, {"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    _cold(installation)
    manager.push("heating", {"value_wh": 900})
    manager.run_cycle()

    base = [400.0] * 48
    combined = manager.apply(base)

    assert len(manager.registry.snapshot()) == 3
    assert sum(combined) > sum(base)
    # Every slot carries at least the base plus the pushed heating profile.
    assert min(combined) >= 1300.0 - 1e-6


def test_the_base_profile_is_never_mutated(make_manager, installation):
    """Both series are backed by an interface cache; this bug has been paid for once."""
    manager = make_manager([POOL])
    _cold(installation)
    manager.run_cycle()

    base = [400.0] * 48
    manager.apply(base)
    assert base == [400.0] * 48


def test_the_combined_forecast_is_never_negative(make_manager):
    """A downward correction may exceed the base load in a slot."""
    manager = make_manager([{"id": "correction", "type": TYPE_EXTERNAL_PROFILE}])
    manager.push("correction", {"values": [-5000.0] * 24})
    manager.run_cycle()

    combined = manager.apply([400.0] * 48)
    assert min(combined) == 0.0


def test_a_shared_power_budget_spreads_two_loads_across_slots(make_manager, installation):
    """
    Without the budget both planners pick the single cheapest hour and stack 4.2 kW
    into it, telling the optimizer to size the battery for a peak the house cannot draw.
    """
    prices = [0.0009] * 48
    prices[12] = 0.0001
    installation.prices = prices
    _cold(installation)

    manager = make_manager([POOL, SAUNA], max_power_w=2000.0)
    manager.run_cycle()

    pool_plan = manager.instance("pool").last_plan
    sauna_plan = manager.instance("sauna").last_plan
    assert pool_plan[12] + sauna_plan[12] <= 2000.0 + 1e-6
    assert sum(1 for value in sauna_plan if value > 0) >= 1


def test_priority_decides_who_gets_the_cheapest_slot(make_manager, installation):
    prices = [0.0009] * 48
    prices[12] = 0.0001
    installation.prices = prices
    _cold(installation)

    manager = make_manager(
        [dict(POOL, priority=10), dict(SAUNA, priority=20)], max_power_w=1800.0
    )
    manager.run_cycle()

    assert manager.instance("pool").last_plan[12] == pytest.approx(1800.0)
    assert manager.instance("sauna").last_plan[12] == 0.0


def test_reversing_the_priority_reverses_the_outcome(make_manager, installation):
    prices = [0.0009] * 48
    prices[12] = 0.0001
    installation.prices = prices
    _cold(installation)

    manager = make_manager(
        [dict(POOL, priority=30), dict(SAUNA, priority=5)], max_power_w=1800.0
    )
    manager.run_cycle()

    assert manager.instance("sauna").last_plan[12] > 0
    assert manager.instance("pool").last_plan[12] == 0.0


def test_no_budget_means_no_limit(make_manager, installation):
    prices = [0.0009] * 48
    prices[12] = 0.0001
    installation.prices = prices
    _cold(installation)

    manager = make_manager([POOL, SAUNA], max_power_w=0)
    manager.run_cycle()

    assert manager.instance("pool").last_plan[12] > 0
    assert manager.instance("sauna").last_plan[12] > 0


def test_an_urgent_load_still_runs_when_the_budget_is_spent(make_manager, installation):
    """Frost protection outranks a tidy peak."""
    _cold(installation)
    installation.sensors["sensor.pool"] = 3.0

    manager = make_manager(
        [dict(POOL, priority=90, frost_protection_temp_c=4.0), dict(SAUNA, priority=1)],
        max_power_w=2400.0,
    )
    manager.run_cycle()

    assert manager.instance("pool").last_release["released"] is True


def test_subtract_sensors_lists_what_must_leave_the_base_load(make_manager, installation):
    manager = make_manager([
        POOL,
        dict(SAUNA, subtract_from_base_load=False),
        {"id": "heating", "type": TYPE_EXTERNAL_PROFILE},
    ])
    _cold(installation)

    assert manager.subtract_sensors() == ["sensor.pool_power"]


def test_a_disabled_load_is_not_subtracted(make_manager):
    manager = make_manager([dict(POOL, enabled=False)])
    assert manager.subtract_sensors() == []


def test_release_changes_are_published_once_per_change(make_manager, installation):
    seen = []
    manager = make_manager([POOL], on_release_change=lambda i, r: seen.append((i, r["released"])))
    _cold(installation)

    manager.run_cycle()
    manager.run_cycle()
    assert seen == [("pool", True)]

    installation.sensors["sensor.pool"] = 30.0
    manager.run_cycle()
    assert seen[-1] == ("pool", False)


def test_a_failing_publisher_does_not_break_the_cycle(make_manager, installation):
    def boom(_id, _release):
        raise RuntimeError("mqtt is down")

    manager = make_manager([POOL], on_release_change=boom)
    _cold(installation)
    manager.run_cycle()
    assert manager.stats.cycles == 1
