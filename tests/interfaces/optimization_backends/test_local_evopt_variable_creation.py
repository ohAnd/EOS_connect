"""
Building the MILP must not flood the log with PuLP deprecation warnings.

PuLP deprecated bare ``LpVariable(...)`` construction in favour of
``LpProblem.add_variable`` and warns once per variable. The optimizer creates a few
thousand of them per run, so a single CI run produced 44229 warning lines - enough to
bury anything worth reading, including the three that pointed at a real crash.
"""

import warnings

import pulp

from src.interfaces.optimization_backends.local_evopt.optimizer import (
    BatteryConfig,
    GridConfig,
    OptimizationStrategy,
    Optimizer,
    TimeSeriesData,
)


def _optimizer(slots=6):
    dt = [3600] * slots
    return Optimizer(
        strategy=OptimizationStrategy(
            charging_strategy="none", discharging_strategy="none"
        ),
        grid=GridConfig(),
        batteries=[
            BatteryConfig(
                s_min=1000,
                s_max=9500,
                s_initial=5000,
                c_min=0,
                c_max=5000,
                d_max=5000,
                p_a=0.0002,
                charge_from_grid=True,
                discharge_to_grid=True,
            )
        ],
        time_series=TimeSeriesData(
            dt=dt,
            gt=[1000.0] * slots,
            ft=[5000.0] * slots,
            p_N=[0.0003] * slots,
            p_E=[0.00008] * slots,
        ),
    )


class _ProblemWithoutAddVariable:
    """PuLP 2.x, which ``requirements.txt`` still allows: no ``add_variable``."""


def test_building_the_model_emits_no_deprecation_warnings():
    optimizer = _optimizer()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        optimizer.create_model()

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not deprecations, [str(w.message) for w in deprecations]


def test_the_model_still_solves():
    """The migration is a rename, not a behaviour change."""
    assert _optimizer().solve()["status"] == "Optimal"


def test_older_pulp_without_add_variable_still_gets_its_variables():
    optimizer = _optimizer()
    optimizer.problem = _ProblemWithoutAddVariable()

    with warnings.catch_warnings():
        # This path is the deprecated constructor by definition - the point is that it
        # still produces a usable variable, not that it stays quiet.
        warnings.simplefilter("ignore", DeprecationWarning)
        variable = optimizer._new_var(  # pylint: disable=protected-access
            "s_probe", lowBound=0, upBound=42, cat="Continuous"
        )

    assert isinstance(variable, pulp.LpVariable)
    assert (variable.lowBound, variable.upBound) == (0, 42)
