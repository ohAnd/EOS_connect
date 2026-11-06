"""
Plan stability: the previous plan is remembered, lined up with the next run, and a
departure from it costs a little - enough to break ties, not enough to block a change
that is actually worth something.
"""

from datetime import datetime, timedelta

import pytest

pulp = pytest.importorskip("pulp")

from src.interfaces.optimization_backends.local_evopt import (
    plan_stability,
)  # noqa: E402
from src.interfaces.optimization_backends.local_evopt.optimizer import (  # noqa: E402
    BatteryConfig,
    GridConfig,
    ManagedLoadConfig,
    Optimizer,
    OptimizationStrategy,
    OptimizerSettings,
    TimeSeriesData,
)

T = 12
DT = 3600
PRICE = 0.00030
START = datetime(2026, 11, 4, 9, 0)


def _optimizer(
    prices,
    previous=None,
    weight=0.0,
    scope=plan_stability.SCOPE_MANAGED_LOADS,
    battery=False,
):
    batteries = (
        [
            BatteryConfig(
                s_min=500,
                s_max=10000,
                s_initial=5000,
                c_max=3000,
                d_max=3000,
                p_a=PRICE,
                charge_from_grid=True,
                s_capacity=10000,
            )
        ]
        if battery
        else []
    )
    load = ManagedLoadConfig(
        id="heat",
        demand_wh=3000.0,
        value_eur_per_wh=0.001,
        max_power_w=1000.0,
        min_runtime_slots=3,
    )
    return Optimizer(
        strategy=OptimizationStrategy(),
        grid=GridConfig(p_max_imp=20000, p_max_exp=20000),
        batteries=batteries,
        time_series=TimeSeriesData(
            dt=[DT] * T,
            gt=[300.0] * T,
            ft=[0.0] * T,
            p_N=list(prices),
            p_E=[0.00008] * T,
        ),
        optimizer_settings=OptimizerSettings(time_limit=30, gapRel=0.0),
        M=60000,
        managed_loads=[load],
        previous_plan=previous,
        plan_stability_weight=weight,
        plan_stability_scope=scope,
    )


def _runs(result):
    energy = result["managed_loads"][0]["energy"]
    return [t for t, wh in enumerate(energy) if wh > 1]


# -- the memory -------------------------------------------------------------------------


def test_the_previous_plan_is_shifted_by_the_slots_that_have_passed():
    memory = plan_stability.PlanMemory(DT)
    memory.remember(START, {"ml:heat": [0, 1, 2, 3, 4]})
    shifted = memory.previous(START + timedelta(hours=2), 5)
    assert shifted == {"ml:heat": [2.0, 3.0, 4.0, None, None]}


def test_slots_the_old_plan_never_covered_are_left_free():
    memory = plan_stability.PlanMemory(DT)
    memory.remember(START, {"ml:heat": [1, 1]})
    assert memory.previous(START, 4)["ml:heat"] == [1.0, 1.0, None, None]


@pytest.mark.parametrize("offset", [timedelta(hours=-1), timedelta(hours=7)])
def test_a_plan_from_the_future_or_from_long_ago_is_ignored(offset):
    memory = plan_stability.PlanMemory(DT)
    memory.remember(START, {"ml:heat": [1, 1, 1]})
    assert memory.previous(START + offset, 3) == {}


def test_nothing_remembered_means_nothing_to_hold_to():
    assert plan_stability.PlanMemory(DT).previous(START, 4) == {}


@pytest.mark.parametrize(
    "scope, key, held",
    [
        ("off", "ml:heat", False),
        ("managed_loads", "ml:heat", True),
        ("managed_loads", "c:0", False),
        ("managed_loads_and_battery", "c:0", True),
        ("managed_loads_and_battery", "d:0", True),
    ],
)
def test_the_scope_decides_which_series_are_held(scope, key, held):
    assert plan_stability.scope_includes(scope, key) is held


# -- the solver -------------------------------------------------------------------------


def test_a_tie_goes_to_the_previous_plan():
    # Two equally cheap windows. Whatever the solver would pick on its own, a previous
    # plan in the later one keeps it there.
    prices = [PRICE] * T
    previous = {"ml:heat": [0.0] * 6 + [1000.0] * 3 + [0.0] * 3}
    result = _optimizer(prices, previous, weight=0.25).solve()
    assert _runs(result) == [6, 7, 8]


def test_a_real_saving_still_moves_the_plan():
    # The previous plan ran in slots 6-8, beyond the near two hours; now slots 1-3 are
    # a third cheaper. That is far more than the 10 % a run out there needs to move.
    prices = [PRICE] * T
    for t in (1, 2, 3):
        prices[t] = PRICE * 0.66
    previous = {"ml:heat": [0.0] * 6 + [1000.0] * 3 + [0.0] * 3}
    result = _optimizer(prices, previous, weight=0.25).solve()
    assert _runs(result) == [1, 2, 3]


def test_a_saving_smaller_than_the_penalty_does_not():
    prices = [PRICE] * T
    for t in (1, 2, 3):
        prices[t] = PRICE * 0.98
    previous = {"ml:heat": [0.0] * 6 + [1000.0] * 3 + [0.0] * 3}
    result = _optimizer(prices, previous, weight=0.25).solve()
    assert _runs(result) == [6, 7, 8]


def _start_cheaper(at, by):
    prices = [PRICE] * T
    for t in at:
        prices[t] = PRICE * (1 - by)
    return prices


def test_what_is_about_to_run_takes_a_big_saving_to_move():
    # Planned to start now. 20 % cheaper later is not enough to call it off...
    previous = {"ml:heat": [1000.0] * 3 + [0.0] * 9}
    result = _optimizer(_start_cheaper((8, 9, 10), 0.20), previous, weight=0.25).solve()
    assert _runs(result) == [0, 1, 2]


def test_what_is_hours_away_moves_for_a_modest_one():
    # ...while the same 20 % moves a run that was only planned for later.
    previous = {"ml:heat": [0.0] * 4 + [1000.0] * 3 + [0.0] * 5}
    result = _optimizer(_start_cheaper((8, 9, 10), 0.20), previous, weight=0.25).solve()
    assert _runs(result) == [8, 9, 10]


def test_the_weight_is_full_for_two_hours_and_a_fifth_after():
    weights = plan_stability.slot_weights(1.0, [900] * 12)
    assert weights[:8] == [1.0] * 8
    assert weights[8:] == [pytest.approx(0.2)] * 4


def test_weight_zero_is_the_old_behaviour():
    prices = [PRICE] * T
    for t in (1, 2, 3):
        prices[t] = PRICE * 0.98
    previous = {"ml:heat": [0.0] * 6 + [1000.0] * 3 + [0.0] * 3}
    result = _optimizer(prices, previous, weight=0.0).solve()
    assert _runs(result) == [1, 2, 3]


def test_the_penalty_is_not_part_of_the_reported_cost():
    prices = [PRICE] * T
    previous = {"ml:heat": [0.0] * 6 + [1000.0] * 3 + [0.0] * 3}
    held = _optimizer(prices, previous, weight=0.25).solve()
    free = _optimizer(prices).solve()
    assert held["objective_value"] == pytest.approx(free["objective_value"], abs=1e-6)


def test_the_battery_is_held_only_when_the_scope_says_so():
    prices = [PRICE] * T
    previous = {"c:0": [3000.0] + [0.0] * (T - 1), "d:0": [0.0] * T}
    loads_only = _optimizer(prices, previous, weight=0.25, battery=True)
    loads_only.create_model()
    with_battery = _optimizer(
        prices, previous, weight=0.25, battery=True, scope=plan_stability.SCOPE_ALL
    )
    with_battery.create_model()

    def penalties(opt):
        return [v for v in opt.problem.variables() if v.name.startswith("chg_c_0")]

    assert not penalties(loads_only)
    assert len(penalties(with_battery)) == T


def test_the_solved_series_come_back_under_the_same_keys():
    opt = _optimizer([PRICE] * T, battery=True)
    opt.solve()
    series = opt.plan_series()
    assert set(series) == {"ml:heat", "c:0", "d:0"}
    assert len(series["ml:heat"]) == T


# -- the backend ------------------------------------------------------------------------


def _backend(**kwargs):
    import pytz
    from src.interfaces.optimization_backends.optimization_backend_local_evopt import (
        LocalEVOptBackend,
    )

    return LocalEVOptBackend(
        time_frame_base=900, time_zone=pytz.timezone("Europe/Berlin"), **kwargs
    )


def test_the_backend_hands_the_last_plan_to_the_next_run():
    backend = _backend()
    start = backend._plan_start()
    backend.plan_memory.remember(
        start - timedelta(minutes=15), {"ml:heat": [5.0, 6.0, 7.0]}
    )
    opt = _optimizer([PRICE] * T)
    backend._attach_previous_plan(opt, start)
    assert opt.plan_stability_weight == pytest.approx(0.25)
    assert opt.plan_stability_scope == plan_stability.SCOPE_MANAGED_LOADS
    assert opt.previous_plan["ml:heat"][:3] == [6.0, 7.0, None]


def test_scope_off_hands_over_nothing():
    backend = _backend(plan_stability_scope="off")
    start = backend._plan_start()
    backend.plan_memory.remember(start, {"ml:heat": [5.0]})
    opt = _optimizer([PRICE] * T)
    backend._attach_previous_plan(opt, start)
    assert opt.plan_stability_weight == 0.0
    assert opt.previous_plan == {}


def test_an_unknown_scope_falls_back_to_the_default():
    backend = _backend(plan_stability_scope="everything")
    opt = _optimizer([PRICE] * T)
    backend._attach_previous_plan(opt, backend._plan_start())
    assert opt.plan_stability_scope == plan_stability.SCOPE_MANAGED_LOADS
