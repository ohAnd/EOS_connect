"""
A contingent load the solver places itself, rather than being handed as fixed demand.

The promise these tests defend is the one the setting makes to the user: energy bought
for the load comes in under `value_eur_per_wh`. It is not enforced by a constraint - it
falls out of the solver only adding a Wh when supplying it costs less than the Wh is
worth - so it is worth checking against the solved numbers rather than trusting the
argument.
"""

import pytest

pulp = pytest.importorskip("pulp")

from src.interfaces.optimization_backends.local_evopt.optimizer import (  # noqa: E402
    BatteryConfig,
    GridConfig,
    ManagedLoadConfig,
    Optimizer,
    OptimizationStrategy,
    OptimizerSettings,
    TimeSeriesData,
)

T = 24
DT = 3600
CHEAP = 0.00010
DEAR = 0.00045
FEED_IN = 0.00008


def _optimizer(loads=None, prices=None, pv=None, house=None, battery=True):
    """A day of hourly slots, deliberately small so the tests stay readable."""
    grid = GridConfig(p_max_imp=20000, p_max_exp=20000)
    batteries = [
        BatteryConfig(
            s_min=500, s_max=10000, s_initial=5000, c_max=5000, d_max=5000,
            p_a=CHEAP, charge_from_grid=True, s_capacity=10000,
        )
    ] if battery else []
    series = TimeSeriesData(
        dt=[DT] * T,
        gt=list(house) if house else [300.0] * T,
        ft=list(pv) if pv else [0.0] * T,
        p_N=list(prices) if prices else [CHEAP] * T,
        p_E=[FEED_IN] * T,
    )
    return Optimizer(
        strategy=OptimizationStrategy(), grid=grid, batteries=batteries,
        time_series=series, eta_c=0.95, eta_d=0.95,
        optimizer_settings=OptimizerSettings(time_limit=60), M=60000,
        managed_loads=loads or [],
    )


def _load(**kwargs):
    base = {
        "id": "pool", "demand_wh": 6000.0, "value_eur_per_wh": 0.00025,
        "max_power_w": 1500.0, "min_runtime_slots": 1,
    }
    base.update(kwargs)
    return ManagedLoadConfig(**base)


def _placed(result, load_id="pool"):
    for entry in result["managed_loads"]:
        if entry["id"] == load_id:
            return entry["energy"]
    raise AssertionError(f"no schedule returned for {load_id}")


def _grid_cost(optimizer, result):
    return sum(
        energy * optimizer.time_series.p_N[t]
        for t, energy in enumerate(result["grid_import"])
    )


# --- the load is placed at all ----------------------------------------------------------

def test_a_load_worth_more_than_the_power_costs_is_placed():
    opt = _optimizer([_load()])
    result = opt.solve()
    assert result["status"] == "Optimal"
    assert sum(_placed(result)) == pytest.approx(6000.0, rel=0.01)


def test_a_load_worth_less_than_the_power_costs_is_not():
    """Nothing forces it to run. Worth less than the energy, so it stays off."""
    opt = _optimizer([_load(value_eur_per_wh=CHEAP / 2)])
    result = opt.solve()
    assert sum(_placed(result)) == pytest.approx(0.0, abs=1.0)


def test_the_demand_is_a_ceiling_not_a_target():
    """Cheap power everywhere, but it never takes more than the water needs."""
    opt = _optimizer([_load(demand_wh=2000.0, value_eur_per_wh=DEAR * 2)])
    result = opt.solve()
    assert sum(_placed(result)) == pytest.approx(2000.0, rel=0.01)


def test_a_load_with_no_room_left_yields_a_short_plan_not_an_infeasible_model():
    """
    The reason demand is a ceiling. An equality here would make the whole problem
    infeasible and take the household's schedule down with a pool that cannot be filled.
    """
    mask = [False] * T
    mask[0] = True
    opt = _optimizer([_load(demand_wh=90000.0, feasible=mask, value_eur_per_wh=DEAR * 2)])
    result = opt.solve()
    assert result["status"] == "Optimal"
    assert sum(_placed(result)) == pytest.approx(1500.0, rel=0.01)


# --- where it is placed -----------------------------------------------------------------

def test_it_runs_in_the_cheap_hours():
    prices = [DEAR] * T
    for hour in (3, 4, 5, 6):
        prices[hour] = CHEAP
    opt = _optimizer([_load(demand_wh=6000.0)], prices=prices)
    energy = _placed(opt.solve())
    assert sum(energy[3:7]) == pytest.approx(6000.0, rel=0.02)
    assert sum(energy) == pytest.approx(sum(energy[3:7]), rel=0.02)


def test_the_mask_is_obeyed():
    mask = [False] * T
    for hour in (10, 11, 12, 13):
        mask[hour] = True
    opt = _optimizer([_load(feasible=mask, value_eur_per_wh=DEAR * 2)])
    energy = _placed(opt.solve())
    assert all(value < 1.0 for t, value in enumerate(energy) if not mask[t])
    assert sum(energy) > 1000.0


def test_free_sun_is_used_before_the_grid():
    pv = [0.0] * T
    pv[12] = 5000.0
    opt = _optimizer([_load(demand_wh=1500.0)], prices=[DEAR] * T, pv=pv)
    energy = _placed(opt.solve())
    assert energy[12] == pytest.approx(1500.0, rel=0.02)


# --- the promise ------------------------------------------------------------------------

@pytest.mark.parametrize("value_ct", [15, 20, 25, 35])
def test_what_is_paid_per_kwh_stays_under_what_it_was_worth(value_ct):
    """
    The property the setting promises, measured rather than argued.

    Incremental cost is the household's grid bill with the load minus the bill without
    it, corrected for any battery left behind - energy not bought is not a cost.
    """
    prices = [DEAR if hour % 3 else CHEAP for hour in range(T)]
    value = value_ct / 100.0 / 1000.0

    base_opt = _optimizer([], prices=prices)
    base = base_opt.solve()
    base_cost = _grid_cost(base_opt, base)
    base_soc = base["batteries"][0]["state_of_charge"][-1]

    opt = _optimizer([_load(demand_wh=6000.0, value_eur_per_wh=value)], prices=prices)
    result = opt.solve()
    placed = sum(_placed(result))
    if placed < 1.0:
        return

    soc = result["batteries"][0]["state_of_charge"][-1]
    extra = (_grid_cost(opt, result) - base_cost) - (soc - base_soc) * CHEAP
    assert extra / placed <= value + 1e-9


def test_raising_the_value_never_places_less():
    """Monotone, which is what makes the setting predictable to turn up."""
    prices = [DEAR if hour % 3 else CHEAP for hour in range(T)]
    placed = []
    for value_ct in (10, 18, 25, 40):
        opt = _optimizer(
            [_load(demand_wh=9000.0, value_eur_per_wh=value_ct / 100.0 / 1000.0)],
            prices=prices,
        )
        placed.append(sum(_placed(opt.solve())))
    assert placed == sorted(placed)


def test_the_battery_is_charged_to_serve_the_load():
    """
    The trade that only something seeing both can make: buy cheap at night, run the
    pump through a dear evening. It is the reason this moved into the optimizer.
    """
    prices = [CHEAP] * 6 + [DEAR] * 18
    mask = [False] * 6 + [True] * 18          # may only run when power is dear
    opt = _optimizer(
        [_load(demand_wh=4000.0, feasible=mask, value_eur_per_wh=DEAR)],
        prices=prices, house=[100.0] * T,
    )
    result = opt.solve()
    assert sum(_placed(result)) > 1000.0
    charged_cheap = sum(result["batteries"][0]["charging_power"][:6])
    assert charged_cheap > 1000.0, "did not stock up while power was cheap"


# --- minimum runtime --------------------------------------------------------------------

def test_a_minimum_run_is_not_broken_into_single_slots():
    prices = [DEAR] * T
    for hour in (2, 8, 14, 20):
        prices[hour] = CHEAP            # four isolated cheap hours
    opt = _optimizer(
        [_load(demand_wh=3000.0, min_runtime_slots=3, value_eur_per_wh=DEAR)],
        prices=prices,
    )
    energy = _placed(opt.solve())
    running = [value > 1.0 for value in energy]

    runs = []
    for index, on in enumerate(running):
        if on and runs and runs[-1][-1] == index - 1:
            runs[-1].append(index)
        elif on:
            runs.append([index])

    assert runs, "nothing was placed at all"
    for run in runs:
        # A run the horizon cuts short is not a violated minimum: the appliance keeps
        # going past the end of the window, we simply cannot see it. Refusing those
        # would mean never starting in the last hours of any horizon.
        if run[-1] == T - 1:
            continue
        assert len(run) >= 3, f"isolated run of {len(run)} at slot {run[0]}"


def test_no_minimum_run_means_no_binaries():
    """They cost solve time, so they are only created when something asks for them."""
    opt = _optimizer([_load(min_runtime_slots=1)])
    opt.create_model()
    assert opt.variables["ml_on"][0] is None


# --- urgent -----------------------------------------------------------------------------

def test_urgent_energy_is_placed_however_dear_it_is():
    """Frost protection is not a price decision."""
    opt = _optimizer(
        [_load(demand_wh=3000.0, value_eur_per_wh=0.0, urgent_wh=1500.0)],
        prices=[DEAR] * T,
    )
    assert sum(_placed(opt.solve())) == pytest.approx(1500.0, rel=0.02)


def test_urgent_beyond_the_demand_does_not_make_the_model_infeasible():
    opt = _optimizer(
        [_load(demand_wh=1000.0, value_eur_per_wh=0.0, urgent_wh=99000.0)],
        prices=[DEAR] * T,
    )
    result = opt.solve()
    assert result["status"] == "Optimal"
    assert sum(_placed(result)) == pytest.approx(1000.0, rel=0.02)


# --- several at once --------------------------------------------------------------------

def test_loads_keep_their_own_limits_and_their_own_schedules():
    """
    Nothing here is pool-shaped. A sauna that will pay more than a pool gets the dear
    hours the pool refuses, and each is answered separately.
    """
    prices = [DEAR] * T
    for hour in (4, 5):
        prices[hour] = CHEAP
    # Both want more than the two cheap hours can supply, so what each does with the
    # dear hours is what separates them.
    loads = [
        _load(id="pool", demand_wh=9000.0, value_eur_per_wh=0.00015),
        _load(id="sauna", demand_wh=9000.0, value_eur_per_wh=DEAR * 1.5),
    ]
    result = _optimizer(loads, prices=prices).solve()

    pool = _placed(result, "pool")
    sauna = _placed(result, "sauna")
    assert sum(sauna) == pytest.approx(9000.0, rel=0.02)
    assert sum(pool) < sum(sauna)
    # The pool took the cheap hours and stopped near there. Not exactly there: it also
    # picks up whatever the battery can give it under its own limit, which is the
    # behaviour worth having rather than an artefact to assert away.
    assert sum(pool[4:6]) >= 0.7 * sum(pool)


def test_every_load_appears_in_the_result_even_when_it_runs_for_nothing():
    loads = [
        _load(id="pool", value_eur_per_wh=0.0),
        _load(id="sauna", value_eur_per_wh=DEAR),
    ]
    result = _optimizer(loads).solve()
    assert {entry["id"] for entry in result["managed_loads"]} == {"pool", "sauna"}


def test_no_managed_loads_leaves_the_model_as_it_was():
    result = _optimizer([]).solve()
    assert result["status"] == "Optimal"
    assert result["managed_loads"] == []
