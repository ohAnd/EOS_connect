"""
The built-in optimizer's solve budget (#366).

The solver time limit used to apply to every run of the charging-curve loop on its
own. With a 500 s timeout, three runs on a Raspberry Pi took 20 minutes - longer than
the 15-minute interval they were meant to fit into - and a managed load then read as
unscheduled between every pair of runs. CBC finds its plan early and spends the rest
proving it optimal, so the budget is now one figure for the whole optimization,
capped at a share of the optimization interval.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pulp
import pytest
import pytz

from src.interfaces.optimization_backends import (
    optimization_backend_local_evopt as backend_mod,
)
from src.interfaces.optimization_backends.local_evopt.optimizer import Optimizer
from src.interfaces.optimization_backends.optimization_backend_local_evopt import (
    LocalEVOptBackend,
)
from src.interfaces.optimization_interface import OptimizationInterface
from tests.interfaces.optimization_backends.test_local_evopt_charge_curve import (
    _midnight_mock,
    _request,
)

DATETIME_PATH = (
    "src.interfaces.optimization_backends.optimization_backend_evopt.datetime"
)
TZ = pytz.timezone("Europe/Berlin")


# ---------------------------------------------------------------------------
# How large the budget is
# ---------------------------------------------------------------------------


def _bare_backend(time_limit=None, scheduler_interval=None):
    backend = LocalEVOptBackend.__new__(LocalEVOptBackend)
    backend.time_limit = time_limit
    backend.scheduler_interval = scheduler_interval
    return backend


def test_without_an_interval_the_budget_is_most_of_the_timeout():
    assert _bare_backend()._solve_budget(500) == pytest.approx(400)


def test_the_interval_caps_the_budget_at_a_quarter():
    """The case from #366: 500 s timeout, 15-minute interval."""
    assert _bare_backend(scheduler_interval=900)._solve_budget(500) == pytest.approx(225)


def test_a_shorter_timeout_stays_below_the_cap():
    assert _bare_backend(scheduler_interval=900)._solve_budget(60) == pytest.approx(48)


def test_an_explicit_solver_limit_wins_over_the_timeout_but_not_over_the_cap():
    assert _bare_backend(time_limit=30, scheduler_interval=900)._solve_budget(500) == 30
    assert _bare_backend(time_limit=600, scheduler_interval=900)._solve_budget(500) == 225


def test_an_unknown_interval_leaves_the_configured_limit():
    assert _bare_backend(scheduler_interval=0)._solve_budget(100) == pytest.approx(80)


def test_no_timeout_and_no_interval_is_no_limit():
    assert _bare_backend()._solve_budget(None) is None


def test_the_interval_alone_still_bounds_the_solver():
    assert _bare_backend(scheduler_interval=600)._solve_budget(None) == pytest.approx(150)


# ---------------------------------------------------------------------------
# How the curve loop spends it
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _timed_backend(socs_by_run, clock, seconds_per_run):
    """A backend whose solver takes *seconds_per_run* (capped at its limit) per run."""
    backend = LocalEVOptBackend.__new__(LocalEVOptBackend)
    limits = []

    def build(_req, time_limit_s, _loads, managed_load_budget_w=0.0, c_max_series=None):
        run = len(limits)
        limits.append(time_limit_s)
        optimizer = MagicMock()

        def solve():
            clock.now += min(seconds_per_run, time_limit_s or seconds_per_run)
            return {
                "status": "Optimal",
                "batteries": [
                    {"state_of_charge": socs_by_run[min(run, len(socs_by_run) - 1)]}
                ],
            }

        optimizer.solve.side_effect = solve
        return optimizer

    backend._build_optimizer = build
    return backend, limits


def _sloped_curve(soc):
    return 10000.0 - 50.0 * soc


EVOPT_REQ = {"time_series": {"dt": [3600] * 3}}
EOS_REQ = {"pv_akku": {"capacity_wh": 10000, "initial_soc_percentage": 30}}
# Every run plans a higher SOC than the last, so the limits never settle on their own.
RISING = [[3000.0 + 500 * k] * 3 for k in range(10)]


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(backend_mod, "time", SimpleNamespace(monotonic=clock, time=clock))
    return clock


def test_without_a_curve_the_single_run_gets_the_whole_budget(clock):
    backend, limits = _timed_backend(RISING, clock, seconds_per_run=1)
    backend._solve_with_charge_curve(EVOPT_REQ, EOS_REQ, 100.0, None, 0.0, None)
    assert limits == [100.0]


def test_the_first_curve_run_gets_half_and_later_runs_what_is_left(clock):
    backend, limits = _timed_backend(RISING, clock, seconds_per_run=20)
    backend._solve_with_charge_curve(EVOPT_REQ, EOS_REQ, 100.0, None, 0.0, _sloped_curve)
    assert limits[0] == pytest.approx(50.0)
    assert limits[1] == pytest.approx(80.0), "100 s budget, 20 s spent by run 1"
    assert limits[2] == pytest.approx(60.0)


def test_no_further_curve_run_starts_once_the_budget_is_spent(clock):
    """Each run runs into its limit: run 1 takes 50 s, run 2 the other 50."""
    backend, limits = _timed_backend(RISING, clock, seconds_per_run=1000)
    _, response = backend._solve_with_charge_curve(
        EVOPT_REQ, EOS_REQ, 100.0, None, 0.0, _sloped_curve
    )
    assert len(limits) == 2
    assert clock.now - 1000.0 == pytest.approx(100.0), "never past the budget"
    assert response["batteries"][0]["state_of_charge"] == RISING[1], "run 2's plan"


def test_without_a_budget_the_curve_runs_unbounded(clock):
    backend, limits = _timed_backend(RISING, clock, seconds_per_run=1000)
    backend._solve_with_charge_curve(EVOPT_REQ, EOS_REQ, None, None, 0.0, _sloped_curve)
    assert limits == [None] * backend_mod.CHARGE_CURVE_MAX_RUNS


# ---------------------------------------------------------------------------
# End to end through optimize()
# ---------------------------------------------------------------------------


def _capture_limits(backend):
    limits = []
    original = backend._build_optimizer

    def spy(request, time_limit_s, managed_loads=None, *args, **kwargs):
        limits.append(time_limit_s)
        return original(request, time_limit_s, managed_loads, *args, **kwargs)

    backend._build_optimizer = spy
    return limits


def _load():
    return {
        "id": "tank",
        "demand_wh": 2000.0,
        "max_power_w": 1000.0,
        "value_eur_per_wh": 0.01,
        "min_runtime_slots": 1,
    }


def test_optimize_caps_the_solver_at_a_quarter_of_the_interval():
    backend = LocalEVOptBackend(time_frame_base=3600, time_zone=TZ)
    backend.scheduler_interval = 900
    limits = _capture_limits(backend)
    with patch(DATETIME_PATH, _midnight_mock()):
        backend.optimize(_request(), timeout=500)
    assert limits[0] == pytest.approx(225.0)


def test_the_cost_baseline_gets_what_is_left_of_the_budget():
    backend = LocalEVOptBackend(time_frame_base=3600, time_zone=TZ)
    backend.scheduler_interval = 900
    limits = _capture_limits(backend)
    with patch(DATETIME_PATH, _midnight_mock()):
        result, _ = backend.optimize(_request(), timeout=500, managed_loads=[_load()])
    assert result["managed_loads_cost"] is not None
    assert len(limits) == 2, "the plan, then the baseline without the load"
    assert backend_mod.BASELINE_MIN_SECONDS <= limits[1] <= 225.0


def test_an_explicit_solver_limit_still_reaches_cbc():
    backend = LocalEVOptBackend(time_frame_base=3600, time_zone=TZ, time_limit=30)
    captured = {}
    original_init = Optimizer.__init__

    def patched_init(self_inner, *args, **kwargs):
        original_init(self_inner, *args, **kwargs)
        captured["settings"] = self_inner.settings

    with patch.object(Optimizer, "__init__", patched_init):
        with patch(DATETIME_PATH, _midnight_mock()):
            backend.optimize(_request(), timeout=60)
    assert captured["settings"].time_limit == 30


# ---------------------------------------------------------------------------
# A stop on the time limit is told apart from a proven optimum
# ---------------------------------------------------------------------------


def _stop_on_time(monkeypatch):
    """Make every CBC solve report what CBC reports when it hits its time limit."""
    original = pulp.LpProblem.solve

    def solve(self, *args, **kwargs):
        status = original(self, *args, **kwargs)
        self.sol_status = pulp.LpSolutionIntegerFeasible
        return status

    monkeypatch.setattr(pulp.LpProblem, "solve", solve)


def test_a_proven_optimum_is_not_reported_as_a_time_limit_stop():
    backend = LocalEVOptBackend(time_frame_base=3600, time_zone=TZ)
    with patch(DATETIME_PATH, _midnight_mock()):
        evopt_request, _ = backend._transform_request_from_eos_to_evopt(_request())
    response = backend._build_optimizer(evopt_request, 60).solve()
    assert response["status"] == "Optimal"
    assert response["time_limit_reached"] is False


def test_a_stop_on_the_time_limit_is_reported(monkeypatch):
    _stop_on_time(monkeypatch)
    backend = LocalEVOptBackend(time_frame_base=3600, time_zone=TZ)
    with patch(DATETIME_PATH, _midnight_mock()):
        evopt_request, _ = backend._transform_request_from_eos_to_evopt(_request())
    response = backend._build_optimizer(evopt_request, 60).solve()
    assert response["status"] == "Optimal", "the plan is still usable"
    assert response["time_limit_reached"] is True


def test_the_log_says_when_the_plan_hit_the_time_limit(monkeypatch, caplog):
    _stop_on_time(monkeypatch)
    backend = LocalEVOptBackend(time_frame_base=3600, time_zone=TZ)
    backend.scheduler_interval = 900
    with caplog.at_level("INFO", logger="__main__"):
        with patch(DATETIME_PATH, _midnight_mock()):
            backend.optimize(_request(), timeout=500)
    solved = [r.getMessage() for r in caplog.records if "Solved in" in r.getMessage()]
    assert len(solved) == 1
    assert "best plan found within the time limit" in solved[0]
    assert "budget 225 s" in solved[0]


# ---------------------------------------------------------------------------
# The interval reaches the backend
# ---------------------------------------------------------------------------


def test_the_interface_hands_the_interval_to_the_built_in_solver():
    interface = OptimizationInterface({"source": "local_evopt"}, 3600, TZ)
    interface.set_scheduler_interval(900)
    assert interface.backend.scheduler_interval == 900


def test_the_external_backends_are_left_alone():
    interface = OptimizationInterface({"source": "evopt", "server": "localhost"}, 3600, TZ)
    interface.set_scheduler_interval(900)
    assert not hasattr(interface.backend, "scheduler_interval")
