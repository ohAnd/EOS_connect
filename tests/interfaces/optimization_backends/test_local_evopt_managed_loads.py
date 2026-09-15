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


# --- not cycling -------------------------------------------------------------------------

def _shape(result, load_id="pool"):
    """The run/stop pattern, as a string: # is running, . is idle."""
    return "".join("#" if value > 1 else "." for value in _placed(result, load_id))


def _starts(shape):
    return sum(1 for t, mark in enumerate(shape)
               if mark == "#" and (t == 0 or shape[t - 1] == "."))


def test_equal_cost_slots_are_taken_together_not_scattered():
    """
    With near-flat prices every arrangement of the same number of slots costs the same,
    so nothing in the objective preferred a contiguous one and the solver returned
    whichever the search reached first. On a real pool that read as an hour of running,
    a fifteen-minute stop, then another three and a half hours.
    """
    prices = [CHEAP] * T
    prices[8] = DEAR
    opt = _optimizer(
        [_load(demand_wh=12000.0, max_power_w=1000.0, min_runtime_slots=2,
               value_eur_per_wh=0.00030, start_cost_eur=0.5 * 0.00030 * 1000.0)],
        prices=prices,
    )
    shape = _shape(opt.solve())
    assert _starts(shape) == 1, shape


def test_pricing_starts_costs_no_energy():
    """It breaks ties; it does not buy contiguity by running less."""
    prices = [CHEAP if hour % 4 else DEAR for hour in range(T)]
    placed = []
    for start_cost in (0.0, 0.5 * 0.00030 * 1000.0):
        opt = _optimizer(
            [_load(demand_wh=12000.0, max_power_w=1000.0, min_runtime_slots=2,
                   value_eur_per_wh=0.00030, start_cost_eur=start_cost)],
            prices=prices,
        )
        placed.append(sum(_placed(opt.solve())))
    assert placed[1] == pytest.approx(placed[0], rel=0.01)


def test_a_priced_start_is_still_taken_when_it_pays():
    """
    Half a slot's worth, not a veto. A long dear stretch is still worth stopping for.

    No battery here on purpose: with one the solver charges cheaply and runs straight
    through the expensive hours, which is the right answer and the wrong test - it
    never has to choose between stopping and overpaying.
    """
    prices = [CHEAP] * 6 + [DEAR * 4] * 8 + [CHEAP] * 10
    opt = _optimizer(
        [_load(demand_wh=12000.0, max_power_w=1000.0, min_runtime_slots=2,
               value_eur_per_wh=0.00030, start_cost_eur=0.5 * 0.00030 * 1000.0)],
        prices=prices, battery=False,
    )
    shape = _shape(opt.solve())
    assert _starts(shape) == 2, shape
    assert "#" not in shape[6:14], shape


def test_a_short_stop_between_two_runs_is_not_allowed():
    """The minimum runtime bounds the runs; this bounds the rests between them."""
    prices = [CHEAP] * T
    prices[10] = DEAR * 3
    opt = _optimizer(
        [_load(demand_wh=18000.0, max_power_w=1000.0, min_runtime_slots=3,
               value_eur_per_wh=0.00030)],
        prices=prices,
    )
    shape = _shape(opt.solve())
    gaps = [len(gap) for gap in shape.strip(".").split("#") if gap]
    assert all(gap >= 3 for gap in gaps), shape


def test_no_start_cost_creates_no_binary():
    """Priced starts cost a binary per slot; unpriced ones must not."""
    opt = _optimizer([_load(min_runtime_slots=1, start_cost_eur=0.0)])
    opt.create_model()
    assert opt.variables["ml_start"][0] is None
    assert opt.variables["ml_on"][0] is None


def test_pricing_starts_creates_the_switch_it_needs():
    """The start variable is defined off the on/off state, so it has to exist."""
    opt = _optimizer([_load(min_runtime_slots=1, start_cost_eur=0.01)])
    opt.create_model()
    assert opt.variables["ml_start"][0] is not None
    assert opt.variables["ml_on"][0] is not None


# --- what the appliance is already doing -------------------------------------------------

def test_a_run_already_under_way_is_not_stopped():
    """
    The seam between two plans is where it cycled. Each plan kept its own runs long
    enough, but a fresh one knew nothing of the last, so a pump three minutes into a
    thirty-minute run could be stopped by the next solve.
    """
    prices = [DEAR] * T                 # every slot too dear to choose freely
    opt = _optimizer(
        [_load(demand_wh=6000.0, max_power_w=1000.0, value_eur_per_wh=CHEAP,
               min_runtime_slots=2, committed_on_slots=2)],
        prices=prices, battery=False,
    )
    energy = _placed(opt.solve())
    assert energy[0] > 0 and energy[1] > 0, energy[:4]


def test_a_rest_already_under_way_is_not_interrupted():
    prices = [CHEAP] * T                # every slot worth taking
    opt = _optimizer(
        [_load(demand_wh=12000.0, max_power_w=1000.0, value_eur_per_wh=DEAR,
               min_runtime_slots=2, committed_off_slots=3)],
        prices=prices,
    )
    energy = _placed(opt.solve())
    assert all(value < 1.0 for value in energy[:3]), energy[:5]
    assert sum(energy) > 1000.0, "it should still run once the rest is served"


def test_a_commitment_never_outlives_the_rules_that_bound_the_load():
    """
    If the window closes while it is running, the window wins. Holding it on through a
    slot it may not use would make the model infeasible and take the household's whole
    schedule down with it.
    """
    mask = [False] * T
    for hour in range(4, T):
        mask[hour] = True
    opt = _optimizer(
        [_load(demand_wh=6000.0, max_power_w=1000.0, feasible=mask,
               min_runtime_slots=2, committed_on_slots=4)],
        prices=[CHEAP] * T,
    )
    result = opt.solve()
    assert result["status"] == "Optimal"
    assert all(value < 1.0 for value in _placed(result)[:4])


def test_a_commitment_never_asks_for_more_than_the_load_wants():
    """A nearly satisfied store must not be forced past its demand to serve a run."""
    opt = _optimizer(
        [_load(demand_wh=1000.0, max_power_w=1000.0, min_runtime_slots=4,
               committed_on_slots=4)],
        prices=[CHEAP] * T,
    )
    result = opt.solve()
    assert result["status"] == "Optimal"
    assert sum(_placed(result)) == pytest.approx(1000.0, rel=0.02)


def test_no_commitment_leaves_the_solver_free():
    opt = _optimizer(
        [_load(demand_wh=6000.0, max_power_w=1000.0, value_eur_per_wh=CHEAP / 2,
               min_runtime_slots=2)],
        prices=[DEAR] * T, battery=False,
    )
    assert sum(_placed(opt.solve())) == pytest.approx(0.0, abs=1.0)
