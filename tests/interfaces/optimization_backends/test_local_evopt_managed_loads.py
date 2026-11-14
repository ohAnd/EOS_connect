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


def _optimizer(loads=None, prices=None, pv=None, house=None, battery=True,
               budget_w=0.0):
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
        managed_loads=loads or [], managed_load_budget_w=budget_w,
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

# ── The shared power budget ────────────────────────────────────────────────────

def _two_loads(**kwargs):
    return [_load(id="pool", demand_wh=12000.0, max_power_w=1500.0,
                  value_eur_per_wh=DEAR, min_runtime_slots=1, **kwargs),
            _load(id="sauna", demand_wh=12000.0, max_power_w=1500.0,
                  value_eur_per_wh=DEAR, min_runtime_slots=1, **kwargs)]


def _cheap_window():
    prices = [DEAR] * T
    for hour in (3, 4, 5, 6):
        prices[hour] = CHEAP
    return prices


def _peak(result):
    series = {e["id"]: e["energy"] for e in result["managed_loads"]}
    return max(sum(series[k][t] for k in series) for t in range(T))


def test_two_loads_do_not_stack_past_the_site_budget():
    """
    Without it both take the single cheapest hour and stack a sauna on top of a pool
    pump - 3000 W where the house allows 1500, a peak the optimizer then sizes the
    battery for. The budget belongs to the installation, not to a load, and it is not
    the grid import limit: sun can cover a draw the grid never sees.
    """
    opt = _optimizer(_two_loads(), prices=_cheap_window(), battery=False,
                     budget_w=1500.0)
    assert _peak(opt.solve()) <= 1500.0 + 1e-6


def test_no_budget_leaves_them_free():
    opt = _optimizer(_two_loads(), prices=_cheap_window(), battery=False)
    assert _peak(opt.solve()) > 1500.0


def test_a_budget_below_one_appliance_does_not_silence_it():
    """
    A budget smaller than a single load is a misconfiguration. Read literally it would
    stop that appliance ever running rather than sharing anything out, so the floor is
    the largest load.
    """
    opt = _optimizer(_two_loads(), prices=_cheap_window(), battery=False,
                     budget_w=200.0)
    result = opt.solve()
    assert result["status"] == "Optimal"
    total = sum(sum(e["energy"]) for e in result["managed_loads"])
    assert total > 1000.0, "the loads must still be able to run"
    assert _peak(result) <= 1500.0 + 1e-6, "but not two at once"


def test_one_load_is_never_constrained_by_the_shared_budget():
    """Nothing to share out, and its own rating already bounds it."""
    opt = _optimizer(
        [_load(demand_wh=12000.0, max_power_w=1500.0, value_eur_per_wh=DEAR,
               min_runtime_slots=1)],
        prices=_cheap_window(), battery=False, budget_w=100.0,
    )
    assert sum(_placed(opt.solve())) > 1000.0


# ── The daily runtime cap ───────────────────────────────────────────────────────

# T is 24 hourly slots, so a "day" here is twelve slots and the horizon holds two.
_DAYS = [0] * 12 + [1] * 12


def test_a_daily_cap_holds_the_load_to_its_hours():
    """
    The cap reached the fallback planner and not the solver, so under this backend it
    was silently inert - and the pool preset ships it set to twelve, which means a
    user on defaults believed in a limit that was not there.
    """
    opt = _optimizer(
        [_load(demand_wh=20000.0, max_power_w=1000.0, value_eur_per_wh=DEAR,
               min_runtime_slots=1, max_slots_per_day=4, day_index=_DAYS)],
        prices=[CHEAP] * T, battery=False,
    )
    energy = _placed(opt.solve())
    assert sum(1 for v in energy[:12] if v > 1.0) <= 4, energy[:12]
    assert sum(1 for v in energy[12:] if v > 1.0) <= 4, energy[12:]


def test_each_day_gets_its_own_allowance():
    """Not a cap on the horizon: two days at four hours is eight, not four."""
    opt = _optimizer(
        [_load(demand_wh=20000.0, max_power_w=1000.0, value_eur_per_wh=DEAR,
               min_runtime_slots=1, max_slots_per_day=4, day_index=_DAYS)],
        prices=[CHEAP] * T, battery=False,
    )
    assert sum(1 for v in _placed(opt.solve()) if v > 1.0) > 4


def test_no_cap_leaves_the_solver_free():
    opt = _optimizer(
        [_load(demand_wh=20000.0, max_power_w=1000.0, value_eur_per_wh=DEAR,
               min_runtime_slots=1, max_slots_per_day=0, day_index=_DAYS)],
        prices=[CHEAP] * T, battery=False,
    )
    assert sum(1 for v in _placed(opt.solve()) if v > 1.0) > 8


def test_a_spent_day_never_makes_the_model_infeasible():
    """
    A load three minutes into a run it may not abandon, on a day whose allowance is
    already gone, must not fail the solve - that would take the household's whole
    schedule down over a comfort limit.
    """
    opt = _optimizer(
        # A minimum run, so the load carries the on/off binary the commitment needs -
        # without one there is nothing to hold and nothing to conflict with.
        [_load(demand_wh=20000.0, max_power_w=1000.0, value_eur_per_wh=DEAR,
               min_runtime_slots=2, max_slots_per_day=1, day_index=_DAYS,
               committed_on_slots=3)],
        prices=[CHEAP] * T, battery=False,
    )
    result = opt.solve()
    assert result["status"] == "Optimal", result["status"]
    energy = _placed(result)
    assert all(v > 1.0 for v in energy[:3]), "the commitment still holds"


def test_slots_belonging_to_no_day_are_never_capped():
    """Padding past the end of the horizon carries -1 and must not join a group."""
    opt = _optimizer(
        [_load(demand_wh=20000.0, max_power_w=1000.0, value_eur_per_wh=DEAR,
               min_runtime_slots=1, max_slots_per_day=2,
               day_index=[-1] * T)],
        prices=[CHEAP] * T, battery=False,
    )
    assert sum(1 for v in _placed(opt.solve()) if v > 1.0) > 2


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


def test_a_running_load_is_still_free_to_stop_when_it_is_worth_it():
    """
    The regression that made the pool run at 34.65 ct against a 30 ct limit.

    `already_running` used to be inferred from `committed_on_slots > 0`, so the only
    way to tell the solver a run was under way was to *force* the head slot on. That
    latches: the pin keeps the gate released, so the next cycle pins it again, and the
    load cannot stop while it has demand and the slot is allowed. It ran a live pool
    from 16:04 to 19:45 through a rising tariff, draining the battery 35 % to 16 %, and
    placed exactly the same energy it would have placed anyway.

    Knowing is not forcing. The flag must leave the first slot a free decision.
    """
    prices = [CHEAP] * T
    prices[0] = DEAR * 4                 # right now is ruinous; later is not
    opt = _optimizer(
        # Worth more than the cheap slots, so running later is strictly better than
        # not running - otherwise "placed nothing" would pass for "skipped slot 0".
        [_load(demand_wh=6000.0, max_power_w=1000.0, value_eur_per_wh=DEAR,
               min_runtime_slots=1, already_running=True)],
        prices=prices, battery=False,
    )
    energy = _placed(opt.solve())
    assert energy[0] < 1.0, "a running load must still be able to skip a dear slot"
    assert sum(energy) > 1000.0, "and it should still run later"


def test_being_told_it_runs_does_not_by_itself_pin_anything():
    """`already_running` carries no obligation - only `committed_on_slots` does."""
    prices = [DEAR] * T
    opt = _optimizer(
        [_load(demand_wh=6000.0, max_power_w=1000.0, value_eur_per_wh=CHEAP,
               min_runtime_slots=1, already_running=True)],
        prices=prices, battery=False,
    )
    assert sum(_placed(opt.solve())) < 1.0, "nothing is worth taking, so take nothing"


def test_no_commitment_leaves_the_solver_free():
    opt = _optimizer(
        [_load(demand_wh=6000.0, max_power_w=1000.0, value_eur_per_wh=CHEAP / 2,
               min_runtime_slots=2)],
        prices=[DEAR] * T, battery=False,
    )
    assert sum(_placed(opt.solve())) == pytest.approx(0.0, abs=1.0)


# --- efficiency per slot ----------------------------------------------------------------

def _autumn_efficiency():
    """Dawn at 80 %, mid-afternoon at 100 % - what a heat pump's COP does over a day."""
    series = []
    for t in range(T):
        if t <= 6:
            series.append(0.8)
        elif t <= 15:
            series.append(0.8 + (t - 6) * 0.2 / 9.0)
        else:
            series.append(1.0 - (t - 15) * 0.02)
    return series


def test_with_flat_prices_an_efficient_hour_wins():
    eff = _autumn_efficiency()
    opt = _optimizer([_load(demand_wh=3000.0, efficiency=eff)], battery=False)
    result = opt.solve()
    used = [t for t, energy in enumerate(_placed(result)) if energy > 1.0]
    assert used and min(eff[t] for t in used) >= 0.95


def test_the_demand_is_met_in_delivered_energy():
    """A slot at 0.9 takes a ninth more electricity for the same result."""
    opt = _optimizer([_load(demand_wh=2700.0, efficiency=[0.9] * T)], battery=False)
    result = opt.solve()
    assert sum(_placed(result)) == pytest.approx(3000.0, rel=0.01)


def test_a_much_cheaper_inefficient_hour_still_wins():
    prices = [DEAR] * T
    prices[3] = CHEAP
    eff = _autumn_efficiency()
    opt = _optimizer(
        [_load(demand_wh=1000.0, efficiency=eff, value_eur_per_wh=0.001)],
        prices=prices, battery=False,
    )
    result = opt.solve()
    assert _placed(result)[3] > 0


def test_the_value_is_earned_per_delivered_wh():
    """Worth 0.30 per delivered Wh, a slot at half efficiency is worth 0.15 drawn."""
    prices = [0.00020] * T
    opt = _optimizer(
        [_load(demand_wh=6000.0, value_eur_per_wh=0.00030, efficiency=[0.5] * T)],
        prices=prices, battery=False,
    )
    result = opt.solve()
    assert sum(_placed(result)) == pytest.approx(0.0, abs=1.0)


def test_no_efficiency_is_the_model_it_always_was():
    with_none = _optimizer([_load()]).solve()
    with_ones = _optimizer([_load(efficiency=[1.0] * T)]).solve()
    assert sum(_placed(with_none)) == pytest.approx(sum(_placed(with_ones)), rel=1e-6)


def test_an_efficiency_of_zero_closes_the_slot():
    eff = [1.0] * T
    eff[5] = 0.0
    prices = [DEAR] * T
    prices[5] = 0.0
    opt = _optimizer([_load(demand_wh=1500.0, efficiency=eff)], prices=prices,
                     battery=False)
    result = opt.solve()
    assert _placed(result)[5] == pytest.approx(0.0)


# --- one compressor, two loads -----------------------------------------------------------

def _group_pair(group="heat_pump", **kwargs):
    heating = _load(id="house", demand_wh=3000.0, max_power_w=1000.0,
                    exclusive_group=group, **kwargs)
    water = _load(id="water", demand_wh=2000.0, max_power_w=1000.0,
                  exclusive_group=group, **kwargs)
    return [heating, water]


def test_a_shared_compressor_never_runs_both_in_one_slot():
    prices = [DEAR] * T
    for t in (12, 13, 14):
        prices[t] = CHEAP
    opt = _optimizer(_group_pair(), prices=prices, battery=False)
    result = opt.solve()
    house, water = _placed(result, "house"), _placed(result, "water")
    for t in range(T):
        assert house[t] / 1000.0 + water[t] / 1000.0 <= 1.0 + 1e-6


def test_both_still_get_their_energy_when_there_is_room():
    opt = _optimizer(_group_pair(), battery=False)
    result = opt.solve()
    assert sum(_placed(result, "house")) == pytest.approx(3000.0, rel=0.01)
    assert sum(_placed(result, "water")) == pytest.approx(2000.0, rel=0.01)


def test_without_a_group_both_take_the_cheap_slot():
    prices = [DEAR] * T
    prices[12] = CHEAP
    opt = _optimizer(_group_pair(group=""), prices=prices, battery=False)
    result = opt.solve()
    assert _placed(result, "house")[12] > 0 and _placed(result, "water")[12] > 0


def test_a_group_holds_with_minimum_runs_too():
    opt = _optimizer(_group_pair(min_runtime_slots=2), battery=False)
    result = opt.solve()
    house, water = _placed(result, "house"), _placed(result, "water")
    assert all(not (house[t] > 0 and water[t] > 0) for t in range(T))


def test_two_loads_both_already_running_do_not_make_the_model_infeasible():
    loads = _group_pair(min_runtime_slots=2)
    for load in loads:
        load.committed_on_slots = 2
    result = _optimizer(loads, battery=False).solve()
    assert result["status"] == "Optimal"


def test_a_group_of_one_constrains_nothing():
    single = [_load(exclusive_group="heat_pump")]
    assert _optimizer(single).solve()["status"] == "Optimal"


# --- a store planned by its temperature ---------------------------------------------------

from src.interfaces.optimization_backends.local_evopt.optimizer import (  # noqa: E402
    ThermalStoreConfig,
)

HOUSE_C = 21000.0       # Wh/K - the measured house
HOUSE_H = 105.0         # W/K


def _house(t0=21.2, pv_gain=None, ambient=None, cop=None, power=900.0, t_max=22.5,
           **kwargs):
    store = ThermalStoreConfig(
        capacity_wh_per_k=HOUSE_C,
        loss_w_per_k=[HOUSE_H] * T,
        ambient_c=list(ambient or [5.0] * T),
        cop=list(cop or [3.5] * T),
        gain_wh=list(pv_gain or [0.0] * T),
        t_initial=t0, t_min=21.0, t_max=t_max, t_target=21.5,
    )
    base = dict(id="house", demand_wh=0.0, value_eur_per_wh=0.0, max_power_w=power,
                min_runtime_slots=1, store=store)
    base.update(kwargs)
    return ManagedLoadConfig(**base)


def _temperature(result, load_id="house"):
    for entry in result["managed_loads"]:
        if entry["id"] == load_id:
            return entry["temperature"]
    raise AssertionError(f"no temperature returned for {load_id}")


def test_a_store_stays_inside_its_band():
    result = _optimizer([_house()], battery=False).solve()
    assert result["status"] == "Optimal"
    temps = _temperature(result)
    assert min(temps) >= 21.0 - 0.01 and max(temps) <= 22.5 + 0.01


def test_it_is_handed_over_without_any_demand():
    """Nothing asks for an amount; staying in the band is what makes it run."""
    result = _optimizer([_house()], battery=False).solve()
    assert sum(_placed(result, "house")) > 0


def test_it_heats_where_the_cop_is_best():
    cop = [3.0] * T
    for t in range(12, 17):
        cop[t] = 4.2
    # Warm enough to coast until the good hours, which is when it should heat.
    result = _optimizer([_house(t0=22.3, cop=cop)], battery=False).solve()
    placed = _placed(result, "house")
    assert sum(placed) > 0
    assert sum(placed[12:17]) > 0.8 * sum(placed)


def test_it_heats_ahead_of_a_dear_evening():
    prices = [CHEAP] * T
    for t in range(16, 22):
        prices[t] = DEAR * 3
    result = _optimizer([_house(t0=21.1)], prices=prices, battery=False).solve()
    placed = _placed(result, "house")
    assert sum(placed[16:22]) == pytest.approx(0.0, abs=1.0)
    assert _temperature(result)[15] > 21.3


def test_surplus_pv_is_banked_above_target():
    pv = [0.0] * T
    for t in range(10, 15):
        pv[t] = 3000.0
    # A heat pump big enough to outrun the losses: 2 kW at COP 3.5 is 7 kW of heat.
    result = _optimizer([_house(t0=21.5, power=2000.0)], pv=pv, prices=[DEAR] * T,
                        battery=False).solve()
    assert max(_temperature(result)[10:16]) > 21.8


def test_the_end_of_the_horizon_is_not_filled_to_the_ceiling():
    """Heat left at the end is worth something only up to target."""
    result = _optimizer([_house(t0=21.5)], battery=False).solve()
    assert _temperature(result)[-1] < 21.8


def test_it_does_not_end_the_horizon_cold_either():
    result = _optimizer([_house(t0=21.5)], battery=False).solve()
    assert _temperature(result)[-1] >= 21.0 - 0.01


def test_a_cold_start_heats_at_once_rather_than_failing():
    result = _optimizer([_house(t0=19.5)], battery=False).solve()
    assert result["status"] == "Optimal"
    assert _placed(result, "house")[0] > 0


def test_a_heat_pump_too_small_for_the_cold_is_short_not_infeasible():
    result = _optimizer([_house(t0=21.0, ambient=[-20.0] * T, power=100.0)],
                        battery=False).solve()
    assert result["status"] == "Optimal"


def test_a_sunny_afternoon_above_the_ceiling_is_not_infeasible():
    result = _optimizer([_house(t0=22.4, pv_gain=[4000.0] * T)], battery=False).solve()
    assert result["status"] == "Optimal"
    assert sum(_placed(result, "house")) == pytest.approx(0.0, abs=1.0)


def test_a_store_keeps_its_minimum_run():
    result = _optimizer([_house(t0=21.0, min_runtime_slots=3)], battery=False).solve()
    shape = [energy > 1.0 for energy in _placed(result, "house")]
    runs, length = [], 0
    for on in shape + [False]:
        if on:
            length += 1
        elif length:
            runs.append(length)
            length = 0
    assert runs and min(runs) >= 3


def test_a_store_and_hot_water_share_the_compressor():
    water = _load(id="water", demand_wh=2000.0, max_power_w=900.0,
                  exclusive_group="heat_pump")
    house = _house(t0=21.0, exclusive_group="heat_pump")
    result = _optimizer([house, water], battery=False).solve()
    assert result["status"] == "Optimal"
    for a, b in zip(_placed(result, "house"), _placed(result, "water")):
        assert a / 900.0 + b / 900.0 <= 1.0 + 1e-6


def test_an_ordinary_load_reports_no_temperature():
    result = _optimizer([_load()]).solve()
    assert "temperature" not in result["managed_loads"][0]


def test_two_days_at_quarter_hours_solve_in_reasonable_time():
    """The case that matters: 192 slots, a minimum run, priced starts, a battery."""
    import math
    import time

    slots, dt = 192, 900
    hours = [t * 0.25 for t in range(slots)]
    ambient = [6 + 4 * math.sin(math.pi * ((h % 24) - 9) / 12) for h in hours]
    store = ThermalStoreConfig(
        capacity_wh_per_k=HOUSE_C, loss_w_per_k=[HOUSE_H] * slots, ambient_c=ambient,
        cop=[3.4 + 0.08 * a for a in ambient], gain_wh=[0.0] * slots,
        t_initial=21.2, t_min=21.0, t_max=22.5, t_target=21.5,
    )
    load = ManagedLoadConfig(id="house", max_power_w=900.0, min_runtime_slots=4,
                             start_cost_eur=0.05, store=store)
    series = TimeSeriesData(dt=[dt] * slots, gt=[75.0] * slots, ft=[0.0] * slots,
                            p_N=[0.0003] * slots, p_E=[FEED_IN] * slots)
    opt = Optimizer(
        strategy=OptimizationStrategy(), grid=GridConfig(p_max_imp=20000, p_max_exp=20000),
        batteries=[BatteryConfig(s_min=1000, s_max=16000, s_initial=8000, c_max=8000,
                                 d_max=8000, p_a=0.00025, charge_from_grid=True,
                                 s_capacity=16000)],
        time_series=series, optimizer_settings=OptimizerSettings(time_limit=60),
        M=60000, managed_loads=[load],
    )
    started = time.time()
    result = opt.solve()
    assert result["status"] == "Optimal"
    assert time.time() - started < 20.0


def test_a_store_that_leaks_faster_than_it_holds_stays_a_store():
    """
    A slot may lose at most the whole store.

    The temperature step is explicit in the loss, so once an hour's loss exceeds the
    capacity the ``capacity - loss`` coefficient turns negative: the colder the store
    was, the hotter the model says it gets. Out of reach for a house, not for a small
    barely insulated one on hourly slots - and it would not look like a bad plan, it
    would look like a store that boils.
    """
    leaky = ThermalStoreConfig(
        capacity_wh_per_k=100.0,  # a few litres
        loss_w_per_k=[500.0] * T,  # five times the capacity every hour
        ambient_c=[5.0] * T,
        cop=[3.0] * T,
        gain_wh=[0.0] * T,
        t_initial=21.2,
        t_min=21.0,
        t_max=22.5,
        t_target=21.5,
    )
    load = ManagedLoadConfig(
        id="house",
        demand_wh=0.0,
        value_eur_per_wh=0.0,
        max_power_w=900.0,
        min_runtime_slots=1,
        store=leaky,
    )
    result = _optimizer([load], battery=False).solve()

    assert result["status"] == "Optimal"
    temps = _temperature(result)
    # It cannot hold heat, so it sits at the air temperature and whatever it is being
    # given - never below the air, never anywhere near the model's own -50..150 rails.
    assert min(temps) >= 5.0 - 0.01
    assert max(temps) <= 35.0
