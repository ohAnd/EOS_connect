"""Placement of an energy contingent: cost ranking and every constraint around it."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.loads.models.base import DemandContext, EnergyDemand
from src.loads.planner import (
    STRATEGY_CHEAPEST,
    STRATEGY_COMBINED,
    STRATEGY_PV_SURPLUS,
    PlanOptions,
    plan_contingent,
)

BERLIN = ZoneInfo("Europe/Berlin")


def _ctx(slot_count=48, base=3600, current_slot=0, price=None, feed_in=None, surplus=None):
    anchor = datetime(2026, 6, 1, 0, 0, tzinfo=BERLIN)
    return DemandContext(
        now=anchor,
        anchor=anchor,
        slot_count=slot_count,
        time_frame_base=base,
        current_slot=current_slot,
        price_eur_per_wh=price if price is not None else [0.0003] * slot_count,
        feed_in_eur_per_wh=feed_in if feed_in is not None else [0.00008] * slot_count,
        pv_surplus_wh=surplus if surplus is not None else [0.0] * slot_count,
    )


def _demand(total_wh=3600.0, power_w=1800.0, feasible=None, slot_count=48, **kwargs):
    return EnergyDemand(
        total_wh=total_wh,
        max_power_w=power_w,
        feasible=feasible if feasible is not None else [True] * slot_count,
        **kwargs,
    )


def _used(plan):
    return [index for index, value in enumerate(plan) if value > 0]


def test_the_cheapest_slots_are_chosen():
    price = [0.0004] * 48
    price[10] = 0.0001
    price[20] = 0.0002
    plan = plan_contingent(
        _demand(total_wh=3600.0),
        _ctx(price=price),
        PlanOptions(strategy=STRATEGY_CHEAPEST),
    )
    assert _used(plan) == [10, 20]
    assert sum(plan) == pytest.approx(3600.0)


def test_energy_is_conserved_and_capped_per_slot():
    """A 1800 W load cannot put more than 1800 Wh into an hourly slot."""
    plan = plan_contingent(
        _demand(total_wh=5000.0, power_w=1800.0),
        _ctx(),
        PlanOptions(strategy=STRATEGY_CHEAPEST),
    )
    assert max(plan) <= 1800.0
    assert sum(plan) == pytest.approx(5000.0)


def test_slots_before_now_are_never_used():
    plan = plan_contingent(
        _demand(total_wh=1800.0), _ctx(current_slot=12), PlanOptions()
    )
    assert min(_used(plan)) >= 12


def test_infeasible_slots_are_skipped():
    """Outside the allowed window, below the ambient limit, out of season."""
    feasible = [False] * 48
    feasible[30] = True
    feasible[31] = True
    plan = plan_contingent(
        _demand(total_wh=1800.0, feasible=feasible), _ctx(), PlanOptions()
    )
    assert _used(plan) == [30]


def test_deadline_bounds_the_search():
    price = [0.0004] * 48
    price[40] = 0.00001  # cheapest, but after the deadline
    plan = plan_contingent(
        _demand(total_wh=1800.0, deadline_slot=12),
        _ctx(price=price),
        PlanOptions(strategy=STRATEGY_CHEAPEST),
    )
    assert max(_used(plan)) <= 12


def test_minimum_runtime_forces_contiguous_blocks():
    """Cheap isolated slots must lose to a slightly worse but contiguous run."""
    price = [0.0004] * 48
    price[5] = 0.0001   # cheapest, but its neighbours are expensive
    price[20] = 0.00015
    price[21] = 0.00015
    price[22] = 0.00015
    plan = plan_contingent(
        _demand(total_wh=5400.0),
        _ctx(price=price),
        PlanOptions(strategy=STRATEGY_CHEAPEST, min_runtime_slots=3),
    )
    assert _used(plan) == [20, 21, 22]


def test_minimum_runtime_falls_back_to_single_slots_when_impossible(caplog):
    """Running for less than the minimum beats not reaching the target at all."""
    feasible = [False] * 48
    feasible[10] = True
    with caplog.at_level("INFO", logger="__main__"):
        plan = plan_contingent(
            _demand(total_wh=1800.0, feasible=feasible),
            _ctx(),
            PlanOptions(min_runtime_slots=4),
        )
    assert _used(plan) == [10]
    assert any("contiguous" in r.getMessage() for r in caplog.records)


def test_daily_cap_limits_released_slots_per_day():
    plan = plan_contingent(
        _demand(total_wh=18000.0),
        _ctx(),
        PlanOptions(strategy=STRATEGY_CHEAPEST, max_slots_per_day=2),
    )
    day_one = [i for i in _used(plan) if i < 24]
    day_two = [i for i in _used(plan) if i >= 24]
    assert len(day_one) == 2
    assert len(day_two) == 2


def test_urgent_demand_runs_immediately_and_ignores_price():
    price = [0.001] * 48
    price[40] = 0.00001
    plan = plan_contingent(
        _demand(total_wh=3600.0, urgent=True),
        _ctx(current_slot=6, price=price),
        PlanOptions(strategy=STRATEGY_CHEAPEST),
    )
    assert _used(plan) == [6, 7]


def test_price_cap_excludes_expensive_slots():
    price = [0.0009] * 48
    price[15] = 0.0001
    plan = plan_contingent(
        _demand(total_wh=1800.0),
        _ctx(price=price),
        PlanOptions(strategy=STRATEGY_CHEAPEST, max_price_eur_per_wh=0.0002),
    )
    assert _used(plan) == [15]


def test_urgent_demand_ignores_the_price_cap():
    price = [0.0009] * 48
    plan = plan_contingent(
        _demand(total_wh=1800.0, urgent=True),
        _ctx(price=price),
        PlanOptions(strategy=STRATEGY_CHEAPEST, max_price_eur_per_wh=0.0002),
    )
    assert plan[0] == pytest.approx(1800.0)


def test_combined_prefers_pv_surplus_over_a_cheap_grid_slot():
    """
    Surplus is valued at the feed-in tariff, not at zero.

    The sunny slot wins because exporting would only have earned 8 ct/kWh, while the
    cheap grid slot still costs 20 ct/kWh.
    """
    price = [0.0004] * 48
    price[8] = 0.0002        # cheap grid, no sun
    surplus = [0.0] * 48
    surplus[13] = 2000.0     # full surplus, grid price is the default 0.0004
    plan = plan_contingent(
        _demand(total_wh=1800.0),
        _ctx(price=price, surplus=surplus),
        PlanOptions(strategy=STRATEGY_COMBINED),
    )
    assert _used(plan) == [13]


def test_pv_surplus_strategy_still_reaches_the_target_without_sun():
    """A week of rain must not mean a cold pool."""
    plan = plan_contingent(
        _demand(total_wh=1800.0),
        _ctx(surplus=[0.0] * 48),
        PlanOptions(strategy=STRATEGY_PV_SURPLUS),
    )
    assert sum(plan) == pytest.approx(1800.0)


def test_pv_surplus_strategy_ranks_sunnier_slots_first():
    surplus = [0.0] * 48
    surplus[11] = 500.0
    surplus[12] = 1800.0
    plan = plan_contingent(
        _demand(total_wh=1800.0),
        _ctx(surplus=surplus),
        PlanOptions(strategy=STRATEGY_PV_SURPLUS),
    )
    assert _used(plan) == [12]


def test_a_shared_budget_stops_two_loads_stacking_in_one_slot():
    """The residual left by a higher-priority load bounds what this one may take."""
    price = [0.0004] * 48
    price[9] = 0.0001
    budget = [3000.0] * 48
    budget[9] = 200.0  # the sauna already took almost all of the cheapest slot
    plan = plan_contingent(
        _demand(total_wh=1800.0),
        _ctx(price=price),
        PlanOptions(strategy=STRATEGY_CHEAPEST),
        budget_wh=budget,
    )
    assert plan[9] == pytest.approx(200.0)
    assert sum(plan) == pytest.approx(1800.0)
    assert len(_used(plan)) > 1


def test_no_feasible_slot_yields_an_empty_plan(caplog):
    with caplog.at_level("INFO", logger="__main__"):
        plan = plan_contingent(
            _demand(total_wh=1800.0, feasible=[False] * 48), _ctx(), PlanOptions()
        )
    assert plan == [0.0] * 48
    assert any("no feasible slot" in r.getMessage() for r in caplog.records)


def test_zero_demand_yields_an_empty_plan():
    assert plan_contingent(_demand(total_wh=0.0), _ctx(), PlanOptions()) == [0.0] * 48


def test_a_load_without_a_power_rating_cannot_be_placed(caplog):
    with caplog.at_level("WARNING", logger="__main__"):
        plan = plan_contingent(_demand(power_w=0.0), _ctx(), PlanOptions())
    assert plan == [0.0] * 48
    assert any("power rating" in r.getMessage() for r in caplog.records)


def test_unplaceable_energy_is_reported(caplog):
    """Carrying demand forward silently is how a pool never reaches target."""
    feasible = [False] * 48
    feasible[3] = True
    with caplog.at_level("INFO", logger="__main__"):
        plan_contingent(
            _demand(total_wh=20000.0, feasible=feasible), _ctx(), PlanOptions()
        )
    assert any("could not be placed" in r.getMessage() for r in caplog.records)


def test_quarter_hour_slots_halve_the_per_slot_capacity():
    plan = plan_contingent(
        _demand(total_wh=1800.0, power_w=1800.0, slot_count=192),
        _ctx(slot_count=192, base=900),
        PlanOptions(strategy=STRATEGY_CHEAPEST),
    )
    assert max(plan) == pytest.approx(450.0)
    assert sum(plan) == pytest.approx(1800.0)
