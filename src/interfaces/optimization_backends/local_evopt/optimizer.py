"""
Bundled MILP optimizer engine — adapted from evcc-io/optimizer.

Original source: https://github.com/evcc-io/optimizer
License: MIT License
Copyright (c) 2025 andig

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---
Modifications made for EOS_connect integration (adapted from main branch, ~2025-06):
- Removed Flask/flask-restx/pydantic dependencies; OptimizerSettings is now a plain dataclass
- Added 'maximize_self_consumption' charging strategy
- Added 'emergency_reserve' discharging strategy (end-of-horizon SOC floor)
- Added 'emergency_reserve' fields (s_reserve) to BatteryConfig and corresponding
  penalty variable/constraint in the MILP model
- Added gapRel to OptimizerSettings and the CBC invocation (1% optimality gap)
- Solver resolution probes CBC candidates with a real execve() and prefers the
  first that runs (system PATH, then pulp's bundled binary), because pulp's
  bundled x86_64 CBC is glibc-linked and cannot exec on Alpine/musl
- Per-slot tight Big-M bounds in energy-balance and battery constraints replace the
  upstream global M=1e6, tightening the LP relaxation and reducing B&B tree size
- or 0.0 guard on pulp.value() calls in solve() to handle None results
- Module is invoked in-process; no HTTP server needed
- Added ManagedLoadConfig and the variables, constraints and objective term that let
  the solver *place* a contingent load (pool heat pump, sauna, hot-water tank) rather
  than being handed it as fixed demand, so the battery and the load are decided together
"""

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional

import numpy as np
import pulp

# eos_connect.py attaches its handlers to the "__main__" logger, not to root, so
# a logging.getLogger(__name__) here propagates to a handler-less root and the
# solver diagnostics are silently discarded.
logger = logging.getLogger("__main__")

# Seconds allowed for a CBC candidate to answer `cbc -quit`.
CBC_PROBE_TIMEOUT_S = 15.0

# Cached probe result: the CBC path to use, or None until probed. The probe
# forks a subprocess and solves run on a timer, so it must happen only once.
_resolved_cbc: Optional[str] = None


class CbcSolverUnavailableError(RuntimeError):
    """No runnable CBC binary could be found for the built-in optimizer."""


def _cbc_runs(path: str) -> Optional[str]:
    """
    Return None if `path` can actually be executed, else a rejection reason.

    Executing the candidate is required, not optional: pulp's
    COIN_CMD.available() only does os.path.exists() + os.access(X_OK), so on
    Alpine/musl the glibc-linked CBC bundled in the pulp wheel passes that
    check and then fails at solve time with a bare
    "FileNotFoundError: .../solverdir/cbc/linux/i64/cbc" -- the file exists,
    but its ELF interpreter /lib64/ld-linux-x86-64.so.2 does not.
    """
    try:
        proc = subprocess.run(
            [path, "-quit"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CBC_PROBE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return (
            f"{path}: exists but cannot be started - missing ELF interpreter "
            "(a glibc-linked binary on a musl system, e.g. Alpine Linux / "
            "Home Assistant OS add-on)"
        )
    except OSError as exc:
        return f"{path}: could not be started ({exc})"
    except subprocess.TimeoutExpired:
        return f"{path}: no response within {CBC_PROBE_TIMEOUT_S:.0f}s"
    if proc.returncode < 0:
        return (
            f"{path}: killed by signal {-proc.returncode} "
            "(signal 4 = illegal instruction: the binary needs CPU features "
            "this machine does not provide, e.g. Proxmox kvm64/qemu64)"
        )
    if proc.returncode != 0:
        return f"{path}: exited with status {proc.returncode}"
    return None


def _bundled_cbc_path() -> Optional[str]:
    """Path of the CBC binary shipped inside the installed pulp wheel, if any."""
    cls = getattr(pulp, "PULP_CBC_CMD", None)  # removed in PuLP 4.0
    if cls is None:
        return None
    # Read the class attribute rather than instantiating, to avoid PuLP 3.x's
    # "PULP_CBC_CMD will be removed in PuLP 4.0" deprecation warning.
    return getattr(cls, "pulp_cbc_path", None)


def probe_cbc_solver() -> str:
    """
    Determine once which CBC binary to use, logging the decision and any
    rejections, and return its path.

    Preference order:
      1. a system `cbc` on PATH  -- lets an image ship a working build
      2. the CBC bundled with pulp -- the historical default; fine on glibc

    Both candidates must survive a real execve() probe, so a broken system
    `cbc` no longer shadows a working bundled binary.
    """
    global _resolved_cbc  # pylint: disable=global-statement
    if _resolved_cbc is not None:
        return _resolved_cbc

    rejections = []
    candidates = (
        ("system", shutil.which("cbc")),
        ("bundled", _bundled_cbc_path()),
    )
    for label, path in candidates:
        if not path:
            logger.debug("[OPT-LocalEVopt] No %s CBC binary found", label)
            continue
        reason = _cbc_runs(path)
        if reason is None:
            logger.info("[OPT-LocalEVopt] Using %s CBC solver: %s", label, path)
            _resolved_cbc = path
            return path
        rejections.append(f"{label} CBC -> {reason}")
        logger.warning("[OPT-LocalEVopt] Rejected %s CBC -- %s", label, reason)

    raise CbcSolverUnavailableError(
        "No runnable CBC solver found for the built-in optimizer (local_evopt).\n"
        + ("\n".join(f"  - {r}" for r in rejections) or "  - no CBC binary found at all")
        + "\nOn Home Assistant OS x86_64 add-ons the CBC binary bundled with pulp "
        "is glibc-linked and cannot run on Alpine/musl; update to an add-on "
        "version that ships a static CBC solver, or switch the optimization "
        "backend away from the built-in optimizer in Settings."
    )


def _resolve_cbc_solver(
    msg: int = 0,
    num_threads: Optional[int] = None,
    time_limit: Optional[float] = None,
    gapRel: Optional[float] = None,
) -> pulp.LpSolver:
    """
    Return a configured PuLP CBC solver bound to a verified-runnable binary.

    COIN_CMD is used for both candidates: it is PULP_CBC_CMD's own base class
    with an identical solve path, it accepts an explicit path, and it survives
    PuLP 4.0 dropping PULP_CBC_CMD.
    """
    return pulp.COIN_CMD(
        path=probe_cbc_solver(),
        msg=msg,
        threads=num_threads,
        timeLimit=time_limit,
        gapRel=gapRel,
    )


@dataclass
class OptimizerSettings:
    """Solver settings (replaces pydantic-based settings from upstream)."""
    num_threads: Optional[int] = None
    time_limit: Optional[float] = None
    # 1% optimality gap — negligible for energy, significantly faster
    gapRel: Optional[float] = 0.01


@dataclass
class OptimizationStrategy:
    """Optimization strategy settings for charging and discharging behavior."""
    charging_strategy: str = "none"
    discharging_strategy: str = "none"


@dataclass
class GridConfig:
    """Grid connection configuration including import/export limits and pricing."""
    p_max_imp: Optional[float] = None
    p_max_exp: Optional[float] = None
    prc_p_exc_imp: Optional[float] = None


@dataclass
class BatteryConfig:
    """Battery configuration including capacity, power limits, and optimization constraints."""
    s_min: float = 0.0
    s_max: float = 0.0
    s_initial: float = 0.0
    c_min: float = 0.0
    c_max: float = 0.0
    d_max: float = 0.0
    p_a: float = 0.0
    charge_from_grid: bool = False
    discharge_to_grid: bool = False
    s_capacity: Optional[float] = None
    p_demand: Optional[List[float]] = None
    s_goal: Optional[List[float]] = None
    c_priority: int = 0
    # Emergency reserve: minimum end-of-horizon SOC in Wh (EOS_connect extension)
    s_reserve: float = 0.0

    def __post_init__(self):
        if self.s_capacity is None:
            self.s_capacity = self.s_max


@dataclass
class ManagedLoadConfig:
    """
    A load whose *timing* is the optimizer's to choose (EOS_connect extension).

    The household profile `gt` is fixed: it says what the house will draw and the
    optimizer works around it. A pool heat pump, a sauna or a hot-water tank is not
    like that - it needs a quantity of energy by a deadline and does not care when,
    which makes it something the solver should be placing rather than being told.

    Placing it here rather than in `gt` is what lets the battery and the load be
    decided together: charging at 14 ct to run a pump through a 30 ct evening is a
    trade only something that sees both can make. Every attempt to make that call
    from outside had to guess what energy would cost, and the guess is what the
    solver is computing.

    Nothing here is specific to any appliance. What the energy is *for* lives in the
    demand model that produced these numbers.

    Attributes:
        id: Instance id, echoed back on the result so schedules can be matched up.
        demand_wh: Energy wanted over the horizon. An upper bound, not a target - a
            window too narrow to hold it must yield a short plan, not an infeasible
            model.
        value_eur_per_wh: What a Wh delivered here is worth. The solver runs the load
            in a slot exactly when the marginal cost of supplying it is below this, so
            it doubles as the price limit: the energy actually bought comes in under
            this figure by construction. Set it above any plausible tariff to mean
            "no limit".
        feasible: Per-slot mask - window, ambient temperature, season.
        max_power_w: Draw while running, which sets the per-slot ceiling.
        min_runtime_slots: Shortest run the appliance may be started for. Above 1 this
            costs a binary per slot.
        urgent_wh: Energy that must be placed whatever it costs - frost protection.
    """

    id: str = ""
    demand_wh: float = 0.0
    value_eur_per_wh: float = 0.0
    feasible: Optional[List[bool]] = None
    max_power_w: float = 0.0
    min_runtime_slots: int = 1
    urgent_wh: float = 0.0


@dataclass
class TimeSeriesData:
    """Time series input data for optimization including load, production, and prices."""
    dt: List[int]          # Time step length [s]
    gt: List[float]        # Required total energy [Wh]
    ft: List[float]        # Forecasted production [Wh]
    p_N: List[float]       # Import prices [currency unit/Wh]
    p_E: List[float]       # Export prices [currency unit/Wh]


class Optimizer:
    """
    MILP optimizer: builds the optimization model from input data and provides
    a solve() function to run optimization and return results.

    Supported charging_strategy values:
        'none'                     — no secondary preference
        'charge_before_export'     — prefer charging batteries before exporting (upstream)
        'attenuate_grid_peaks'     — charge at high solar yield times (upstream)
        'maximize_self_consumption'— penalize grid import when PV is available (EOS_connect)

    Supported discharging_strategy values:
        'none'                     — no secondary preference
        'discharge_before_import'  — prefer discharging batteries before grid import (upstream)
        'emergency_reserve'        — keep end-of-horizon SOC above s_reserve (EOS_connect)
    """

    def __init__(
        self,
        strategy: OptimizationStrategy,
        grid: GridConfig,
        batteries: List[BatteryConfig],
        time_series: TimeSeriesData,
        eta_c: float = 0.95,
        eta_d: float = 0.95,
        M: float = 1e6,
        optimizer_settings: Optional[OptimizerSettings] = None,
        managed_loads: Optional[List[ManagedLoadConfig]] = None,
    ):
        self.settings = optimizer_settings or OptimizerSettings()
        self.strategy = strategy
        self.grid = grid
        self.batteries = batteries
        self.managed_loads = list(managed_loads or [])
        self.time_series = time_series
        self.eta_c = eta_c
        self.eta_d = eta_d
        self.M = M
        # number of time steps
        self.T = len(time_series.gt)
        # time step range
        self.time_steps = range(self.T)
        # the optimization problem
        self.problem = None
        # dictionary of optimizer variables
        self.variables: Dict[str, Any] = {}

        # Compute scaling for strategy control parameters
        self.min_import_price = np.min(self.time_series.p_N) if self.time_series.p_N else 0.0
        self.max_import_price = np.max(self.time_series.p_N) if self.time_series.p_N else 0.0

        # scaling for penalty parameters. Make sure goal_penalty is always positive
        # Use np.max() to floor penalty_base at 0.1e-3 — ensures penalties are
        # non-zero even when prices are zero (matches upstream evcc-io/optimizer).
        penalty_base = np.max([self.max_import_price, 0.1e-3])
        self.prc_e_goal_pen = penalty_base * 10e1
        self.prc_p_goal_pen = penalty_base * np.max(self.time_series.dt) / 3600 * 10e1
        self.prc_soc_exc_pen = penalty_base * 10e2

        # penalty for exceeding grid import limit
        self.prc_e_grid_imp_pen = penalty_base * 10e1
        # penalty for exceeding the grid export limit
        self.prc_e_grid_exp_pen = penalty_base * 10e1

        # demand rate flag
        self.is_grid_demand_rate_active = False
        if self.grid.p_max_imp is not None and self.grid.prc_p_exc_imp is not None:
            self.is_grid_demand_rate_active = True

    def create_model(self):
        """Create and initialize the MILP model."""
        self.problem = pulp.LpProblem("EV_Charging_Optimization", pulp.LpMaximize)
        self._setup_variables()
        self._setup_target_function()
        self._add_energy_balance_constraints()
        self._add_battery_constraints()
        self._add_managed_load_constraints()

    def _setup_variables(self):
        """Set up the variables of the MILP optimizer."""
        # Charging power variables [Wh]
        self.variables['c'] = {}
        for i, bat in enumerate(self.batteries):
            self.variables['c'][i] = [
                pulp.LpVariable(
                    f"c_{i}_{t}",
                    lowBound=0,
                    upBound=bat.c_max * self.time_series.dt[t] / 3600.
                )
                for t in self.time_steps
            ]

        # Discharging power variables [Wh]
        self.variables['d'] = {}
        for i, bat in enumerate(self.batteries):
            self.variables['d'][i] = [
                pulp.LpVariable(
                    f"d_{i}_{t}",
                    lowBound=0,
                    upBound=bat.d_max * self.time_series.dt[t] / 3600.
                )
                for t in self.time_steps
            ]

        # State of charge variables [Wh]
        self.variables['s'] = {}
        for i, bat in enumerate(self.batteries):
            self.variables['s'][i] = [
                pulp.LpVariable(f"s_{i}_{t}", lowBound=0, upBound=bat.s_capacity)
                for t in self.time_steps
            ]

        # penalty variable for not reaching given charge goals
        # variables are kept in a matrix Batteries X time steps
        self.variables['s_goal_pen'] = [
            [None for t in self.time_steps] for i in range(len(self.batteries))
        ]
        for i, bat in enumerate(self.batteries):
            if self.batteries[i].s_goal is not None:
                for t in self.time_steps:
                    if self.batteries[i].s_goal[t] > 0:
                        self.variables['s_goal_pen'][i][t] = pulp.LpVariable(
                            f"s_goal_pen_{i}_{t}", lowBound=0
                        )

        # penalty variable for not being able to charge with the required power
        self.variables['p_demand_pen'] = [
            [None for t in self.time_steps] for i in range(len(self.batteries))
        ]
        # binary variable to allow one out of two alternative constraints
        self.variables['z_p_demand'] = [
            [None for t in self.time_steps] for i in range(len(self.batteries))
        ]
        for i, bat in enumerate(self.batteries):
            if bat.p_demand is not None:
                for t in self.time_steps:
                    self.variables['p_demand_pen'][i][t] = pulp.LpVariable(
                        f"p_demand_pen_{i}_{t}", lowBound=0
                    )
                    self.variables['z_p_demand'][i][t] = pulp.LpVariable(
                        f"z_p_demand_{i}_{t}", cat='Binary'
                    )

        # penalty variable for staying above max SOC and below min SOC
        self.variables['s_max_pen'] = [
            [pulp.LpVariable(f"s_max_pen_{i}_{t}", lowBound=0) for t in self.time_steps]
            for i in range(len(self.batteries))
        ]
        self.variables['s_min_pen'] = [
            [pulp.LpVariable(f"s_min_pen_{i}_{t}", lowBound=0) for t in self.time_steps]
            for i in range(len(self.batteries))
        ]

        # Emergency reserve penalty variable (EOS_connect extension)
        # Penalizes end-of-horizon SOC below s_reserve
        self.variables['s_reserve_pen'] = [
            None for i in range(len(self.batteries))
        ]
        for i, bat in enumerate(self.batteries):
            if bat.s_reserve > 0:
                self.variables['s_reserve_pen'][i] = pulp.LpVariable(
                    f"s_reserve_pen_{i}", lowBound=0
                )

        # Grid import/export variables [Wh]
        self.variables['n'] = [pulp.LpVariable(f"n_{t}", lowBound=0) for t in self.time_steps]
        self.variables['e'] = [pulp.LpVariable(f"e_{t}", lowBound=0) for t in self.time_steps]

        # penalty variables for exceeding grid power limits (W)
        # for grid import
        if self.grid.p_max_imp is not None:
            self.variables['e_imp_lim_exc'] = [
                pulp.LpVariable(f"p_imp_pen_{t}", lowBound=0) for t in self.time_steps
            ]
            self.variables['z_imp_lim'] = [
                pulp.LpVariable(f"z_imp_lim_{t}", cat='Binary') for t in self.time_steps
            ]

        # for grid export
        if self.grid.p_max_exp is not None:
            self.variables['e_exp_lim_exc'] = [
                pulp.LpVariable(f"e_exp_lim_exc_{t}", lowBound=0) for t in self.time_steps
            ]
            self.variables['z_exp_lim'] = [
                pulp.LpVariable(f"z_exp_lim_{t}", cat='Binary') for t in self.time_steps
            ]

        # for demand rate calculation
        if self.is_grid_demand_rate_active:
            self.variables['p_max_imp_exc'] = pulp.LpVariable("p_max_imp_exc", lowBound=0)

        # Binary variable: power flow direction to / from grid
        self.variables['y'] = [
            pulp.LpVariable(f"y_{t}", cat='Binary') for t in self.time_steps
        ]

        # Binary variable for charging activation (only when c_min > 0)
        self.variables['z_c'] = {}
        for i, bat in enumerate(self.batteries):
            if bat.c_min > 0:
                self.variables['z_c'][i] = [
                    pulp.LpVariable(f"z_c_{i}_{t}", cat='Binary')
                    for t in self.time_steps
                ]
            else:
                self.variables['z_c'][i] = None

        # Managed loads: energy placed in each slot, and whether the appliance is
        # running there. The mask is applied as an upper bound of zero rather than by
        # leaving the variable out, so every list stays T long and the result can be
        # read back positionally.
        self.variables['ml'] = {}
        self.variables['ml_on'] = {}
        for i, load in enumerate(self.managed_loads):
            per_slot = [
                load.max_power_w * self.time_series.dt[t] / 3600.0
                for t in self.time_steps
            ]
            feasible = load.feasible or [True] * self.T
            self.variables['ml'][i] = [
                pulp.LpVariable(
                    f"ml_{i}_{t}",
                    lowBound=0,
                    upBound=per_slot[t] if (
                        t < len(feasible) and feasible[t]
                    ) else 0.0,
                )
                for t in self.time_steps
            ]
            # Only worth a binary when a minimum run is actually asked for: without
            # one the appliance may be placed in any slot and the on/off state carries
            # no information the continuous variable does not already have.
            if load.min_runtime_slots > 1:
                self.variables['ml_on'][i] = [
                    pulp.LpVariable(f"ml_on_{i}_{t}", cat='Binary')
                    for t in self.time_steps
                ]
            else:
                self.variables['ml_on'][i] = None

        # Binary variable to lock charging against discharging
        self.variables['z_cd'] = {}
        for i, bat in enumerate(self.batteries):
            self.variables['z_cd'][i] = [
                pulp.LpVariable(f"z_cd_{i}_{t}", cat='Binary')
                for t in self.time_steps
            ]

    def _setup_target_function(self):
        """Gather all target function contributions and instantiate the objective."""
        objective = 0

        # -----------------------------------------------------------------------
        # Primary cost & benefit elements
        # -----------------------------------------------------------------------

        # Grid import cost (negative → we want to minimize cost) [currency unit]
        for t in self.time_steps:
            if self.grid.p_max_imp is not None:
                objective -= (
                    self.variables['n'][t]
                    + self.variables['e_imp_lim_exc'][t]
                ) * self.time_series.p_N[t]
            else:
                objective -= self.variables['n'][t] * self.time_series.p_N[t]

        # Grid export revenue [currency unit]
        for t in self.time_steps:
            objective += self.variables['e'][t] * self.time_series.p_E[t]

        # Managed load value [currency unit]
        #
        # This single term is the whole price rule. The solver adds a Wh here only when
        # supplying it costs less than the Wh is worth - and "costs" is whatever the
        # cheapest source in that slot really is, grid import, forgone export, or a
        # battery it may charge earlier for exactly this purpose. So the energy bought
        # for the load comes in under `value_eur_per_wh` by construction; there is no
        # separate cap to enforce, and no averaging window to argue about.
        for i, load in enumerate(self.managed_loads):
            objective += load.value_eur_per_wh * pulp.lpSum(self.variables['ml'][i])

        # Final state of charge value [currency unit]
        for i, bat in enumerate(self.batteries):
            objective += self.variables['s'][i][-1] * bat.p_a

        # Demand rate charge
        if self.is_grid_demand_rate_active:
            objective += -self.grid.prc_p_exc_imp * self.variables['p_max_imp_exc']

        # -----------------------------------------------------------------------
        # Penalties for exceeding battery SOC limits at start
        # -----------------------------------------------------------------------
        for i, bat in enumerate(self.batteries):
            for t in self.time_steps:
                objective += -self.prc_soc_exc_pen * (
                    self.variables['s_max_pen'][i][t] + self.variables['s_min_pen'][i][t]
                )

        # -----------------------------------------------------------------------
        # Penalties for goals that cannot be met
        # -----------------------------------------------------------------------
        for i, bat in enumerate(self.batteries):
            # unmet battery charging goals
            if self.batteries[i].s_goal is not None:
                for t in self.time_steps:
                    if self.batteries[i].s_goal[t] > 0:
                        objective += -self.prc_e_goal_pen * self.variables['s_goal_pen'][i][t]
            # unmet charging demand
            if bat.p_demand is not None:
                for t in self.time_steps:
                    objective += (
                        -self.prc_p_goal_pen
                        * self.variables['p_demand_pen'][i][t]
                        * (1 + (self.T - t) / self.T)
                    )

        # -----------------------------------------------------------------------
        # Penalties for grid power limits that cannot be met
        # -----------------------------------------------------------------------
        for t in self.time_steps:
            if self.grid.p_max_imp is not None and not self.is_grid_demand_rate_active:
                objective += -self.prc_e_grid_imp_pen * self.variables['e_imp_lim_exc'][t]
            if self.grid.p_max_exp is not None:
                objective += (
                    -self.prc_e_grid_exp_pen
                    * (1.0 - t * 1e-5)
                    * self.variables['e_exp_lim_exc'][t]
                )

        # -----------------------------------------------------------------------
        # Emergency reserve penalty (EOS_connect extension)
        # Strongly penalize ending below s_reserve — applied to all batteries
        # that have s_reserve > 0.  Uses a large penalty to make the reserve a
        # near-hard constraint while keeping the problem always feasible.
        # -----------------------------------------------------------------------
        for i, bat in enumerate(self.batteries):
            if bat.s_reserve > 0 and self.variables['s_reserve_pen'][i] is not None:
                # penalty weight: 1000x the goal penalty to make it near-hard
                prc_reserve = self.prc_e_goal_pen * 1000
                objective += -prc_reserve * self.variables['s_reserve_pen'][i]

        # -----------------------------------------------------------------------
        # Secondary strategies (cost-neutral preferences, small weights)
        # -----------------------------------------------------------------------

        # charge_before_export: prefer charging first, then export
        if self.strategy.charging_strategy == 'charge_before_export':
            for i, bat in enumerate(self.batteries):
                for t in self.time_steps:
                    objective += (
                        -self.variables['e'][t]
                        * self.min_import_price
                        * 2e-5
                        * (self.T - t)
                    )

        # attenuate_grid_peaks: charge at high solar production times
        if self.strategy.charging_strategy == 'attenuate_grid_peaks':
            for i, bat in enumerate(self.batteries):
                for t in self.time_steps:
                    objective += (
                        self.variables['c'][i][t]
                        * self.time_series.ft[t]
                        * self.min_import_price
                        * 1e-6
                    )

        # maximize_self_consumption (EOS_connect):
        # Prefer using PV locally over feeding it to the grid, even at a small
        # economic cost.  Unlike charge_before_export (which is a near-invisible
        # tie-breaker), this strategy uses a weight proportional to the feed-in
        # tariff (~15 %) so the optimizer will charge from PV even when the round-
        # trip economics are only marginally in favour of exporting.
        #
        # Effect on break-even: lowers the future-import-price threshold for
        # charging from ~p_E/η_rt  (pure economics) to a lower value, meaning the
        # battery is filled from PV more aggressively.
        if self.strategy.charging_strategy == 'maximize_self_consumption':
            # sc_weight ≈ 15 % of average feed-in tariff — visible preference but
            # still allows clear arbitrage to dominate when spreads are large.
            avg_feedin = float(np.mean(self.time_series.p_E)) if self.time_series.p_E else 0.0
            sc_weight = avg_feedin * 0.15
            for t in self.time_steps:
                if self.time_series.ft[t] > 0:
                    # Penalise exporting during PV production hours
                    objective += -self.variables['e'][t] * sc_weight
                    # Reward charging during PV production hours
                    for i, bat in enumerate(self.batteries):
                        objective += self.variables['c'][i][t] * sc_weight * 0.5

        # discharge_before_import: prefer discharging batteries before importing
        if self.strategy.discharging_strategy == 'discharge_before_import':
            for i, bat in enumerate(self.batteries):
                for t in self.time_steps:
                    objective += (
                        -self.variables['n'][t]
                        * self.min_import_price
                        * 5e-6
                        * (self.T - t)
                    )

        # charging and discharging priorities
        for i, bat in enumerate(self.batteries):
            for t in self.time_steps:
                objective += (
                    self.variables['c'][i][t]
                    * self.min_import_price
                    * 5e-5
                    * (self.T - t)
                    * bat.c_priority
                )
                objective += (
                    self.variables['d'][i][t]
                    * self.min_import_price
                    * 5e-5
                    * (self.T - t)
                    * bat.c_priority
                )

        self.problem += objective

    def _add_energy_balance_constraints(self):
        """Add constraints related to the energy balance to the model."""
        for t in self.time_steps:
            battery_net_discharge = 0
            for i, bat in enumerate(self.batteries):
                battery_net_discharge += -self.variables['c'][i][t] + self.variables['d'][i][t]

            # grid import
            e_grid_imp = self.variables['n'][t]
            if self.grid.p_max_imp is not None:
                if self.is_grid_demand_rate_active:
                    e_grid_imp = self.variables['n'][t] + self.variables['e_imp_lim_exc'][t]
                else:
                    e_grid_imp = self.variables['n'][t] + self.variables['e_imp_lim_exc'][t]

            # grid export
            e_grid_exp = self.variables['e'][t]
            if self.grid.p_max_exp is not None:
                e_grid_exp = self.variables['e'][t] + self.variables['e_exp_lim_exc'][t]

            # Managed loads sit on the demand side beside gt. They are not in gt:
            # the point is that the solver chooses when they draw.
            managed_draw = pulp.lpSum(
                self.variables['ml'][i][t] for i in range(len(self.managed_loads))
            )

            self.problem += (
                battery_net_discharge + self.time_series.ft[t] + e_grid_imp
                == e_grid_exp + self.time_series.gt[t] + managed_draw
            )

        # Grid flow direction constraints — per-slot tight M
        # M_e_t = max possible export in slot t = PV production + max battery discharge
        # M_n_t = max possible import in slot t = load demand + max battery charge
        # These are provably valid upper bounds and significantly tighter than the
        # global M, which closes the LP relaxation gap and reduces B&B tree size.
        _total_d_max = sum(b.d_max for b in self.batteries)
        _total_c_max = sum(b.c_max for b in self.batteries)
        for t in self.time_steps:
            _dt_h = self.time_series.dt[t] / 3600.0
            _m_exp_t = self.time_series.ft[t] + _total_d_max * _dt_h
            _m_imp_t = (
                self.time_series.gt[t] + _total_c_max * _dt_h
                + sum(load.max_power_w for load in self.managed_loads) * _dt_h
            )
            self.problem += self.variables['e'][t] <= _m_exp_t * self.variables['y'][t]
            self.problem += self.variables['n'][t] <= _m_imp_t * (1 - self.variables['y'][t])

        # Limit regular grid import power
        if self.grid.p_max_imp is not None:
            if self.is_grid_demand_rate_active:
                for t in self.time_steps:
                    self.problem += (
                        self.variables['n'][t]
                        <= self.grid.p_max_imp * self.time_series.dt[t] / 3600
                    )
                    self.problem += (
                        self.grid.p_max_imp * self.time_series.dt[t] / 3600
                        - self.variables['n'][t]
                        <= self.M * self.variables['z_imp_lim'][t]
                    )
                    self.problem += (
                        self.variables['e_imp_lim_exc'][t]
                        <= self.M * (1 - self.variables['z_imp_lim'][t])
                    )
            else:
                for t in self.time_steps:
                    self.problem += (
                        self.variables['n'][t]
                        <= self.grid.p_max_imp * self.time_series.dt[t] / 3600
                    )
                    self.problem += (
                        self.grid.p_max_imp * self.time_series.dt[t] / 3600
                        - self.variables['n'][t]
                        <= self.M * self.variables['z_imp_lim'][t]
                    )
                    self.problem += (
                        self.variables['e_imp_lim_exc'][t]
                        <= self.M * (1 - self.variables['z_imp_lim'][t])
                    )

        # Limit regular grid export power
        if self.grid.p_max_exp is not None:
            for t in self.time_steps:
                self.problem += (
                    self.variables['e'][t]
                    <= self.grid.p_max_exp * self.time_series.dt[t] / 3600
                )
                self.problem += (
                    self.grid.p_max_exp * self.time_series.dt[t] / 3600
                    - self.variables['e'][t]
                    <= self.M * self.variables['z_exp_lim'][t]
                )
                self.problem += (
                    self.variables['e_exp_lim_exc'][t]
                    <= self.M * (1 - self.variables['z_exp_lim'][t])
                )

        # Demand rate: track maximum import power
        if self.is_grid_demand_rate_active:
            for t in self.time_steps:
                self.problem += (
                    self.variables['e_imp_lim_exc'][t]
                    <= self.variables['p_max_imp_exc'] * self.time_series.dt[t] / 3600
                )

    def _add_battery_constraints(self):
        """Add constraints related to battery behavior to the model."""
        for i, bat in enumerate(self.batteries):
            # SOC limit penalties (handle out-of-range initial SOC)
            for t in range(0, self.T):
                self.problem += (
                    self.variables['s_max_pen'][i][t]
                    >= self.variables['s'][i][t] - bat.s_max
                )
                self.problem += (
                    self.variables['s_min_pen'][i][t]
                    >= bat.s_min - self.variables['s'][i][t]
                )

            # Battery dynamics
            if len(self.time_steps) > 0:
                self.problem += (
                    self.variables['s'][i][0]
                    == bat.s_initial
                    + self.eta_c * self.variables['c'][i][0]
                    - (1 / self.eta_d) * self.variables['d'][i][0]
                )
            for t in range(1, self.T):
                self.problem += (
                    self.variables['s'][i][t]
                    == self.variables['s'][i][t - 1]
                    + self.eta_c * self.variables['c'][i][t]
                    - (1 / self.eta_d) * self.variables['d'][i][t]
                )

            # SOC goal constraints (for t > 0)
            if bat.s_goal is not None:
                for t in range(1, self.T):
                    if bat.s_goal[t] > 0:
                        self.problem += (
                            self.variables['s'][i][t] + self.variables['s_goal_pen'][i][t]
                            >= bat.s_goal[t]
                        )

            # Minimum battery charge demand
            if bat.p_demand is not None:
                for t in self.time_steps:
                    if bat.p_demand[t] > 0:
                        p_demand = min(bat.c_max * self.time_series.dt[t] / 3600., bat.p_demand[t])
                        # two alternative constraints, only one is active:
                        self.problem += (
                            self.variables['c'][i][t] + self.variables['p_demand_pen'][i][t]
                            + self.M * self.variables['z_p_demand'][i][t]
                            >= p_demand
                        )
                        self.problem += (
                            self.variables['c'][i][t] + self.variables['p_demand_pen'][i][t]
                            + self.M * (1 - self.variables['z_p_demand'][i][t])
                            - (self.batteries[i].s_max - self.variables['s'][i][t])
                            >= 0.
                        )
                    elif bat.c_min > 0:
                        self.problem += (
                            self.variables['c'][i][t]
                            >= bat.c_min
                            * self.time_series.dt[t]
                            / 3600.
                            * self.variables['z_c'][i][t]
                        )
                        self.problem += (
                            self.variables['c'][i][t]
                            <= self.M * self.variables['z_c'][i][t]
                        )

            elif bat.c_min > 0:
                for t in self.time_steps:
                    self.problem += (
                        self.variables['c'][i][t]
                        >= bat.c_min
                        * self.time_series.dt[t]
                        / 3600.
                        * self.variables['z_c'][i][t]
                    )
                    self.problem += (
                        self.variables['c'][i][t]
                        <= self.M * self.variables['z_c'][i][t]
                    )

            # Control battery charging from grid — per-slot tight M
            if not bat.charge_from_grid:
                for t in self.time_steps:
                    _c_max_t = bat.c_max * self.time_series.dt[t] / 3600.0
                    self.problem += self.variables['c'][i][t] <= _c_max_t * self.variables['y'][t]

            # Control battery discharging to grid — per-slot tight M
            if not bat.discharge_to_grid:
                for t in self.time_steps:
                    _d_max_t = bat.d_max * self.time_series.dt[t] / 3600.0
                    self.problem += (
                        self.variables['d'][i][t]
                        <= _d_max_t * (1 - self.variables['y'][t])
                    )

            # Lock charging against discharging — per-slot tight M
            # Using c_max/d_max * dt per slot tightens the LP relaxation when z_cd
            # is fractional (matches the variable upper bounds exactly).
            for t in self.time_steps:
                _c_max_t = bat.c_max * self.time_series.dt[t] / 3600.0
                _d_max_t = bat.d_max * self.time_series.dt[t] / 3600.0
                self.problem += (
                    self.variables['d'][i][t]
                    <= _d_max_t * self.variables['z_cd'][i][t]
                )
                self.problem += (
                    self.variables['c'][i][t]
                    <= _c_max_t * (1 - self.variables['z_cd'][i][t])
                )

            # Emergency reserve constraint (EOS_connect extension)
            # Enforce s[i][T-1] >= s_reserve as a soft (penalized) constraint.
            # This means: s[i][T-1] + s_reserve_pen[i] >= s_reserve
            if bat.s_reserve > 0 and self.variables['s_reserve_pen'][i] is not None:
                self.problem += (
                    self.variables['s'][i][self.T - 1] + self.variables['s_reserve_pen'][i]
                    >= bat.s_reserve
                )

    def _add_managed_load_constraints(self):
        """
        Bound each managed load: how much, when, and for how long at a time.

        The demand is a ceiling rather than an equality on purpose. A window too narrow
        to hold it, or a price that never comes down, has to yield a short plan - the
        caller can see the shortfall and say so. An equality would make the whole model
        infeasible and take the household's schedule down with it.
        """
        for i, load in enumerate(self.managed_loads):
            run = self.variables['ml'][i]
            self.problem += pulp.lpSum(run) <= max(0.0, load.demand_wh)

            # Frost protection and the like: energy that must be placed whatever it
            # costs, so it is a floor the value term cannot argue with.
            if load.urgent_wh > 0:
                self.problem += pulp.lpSum(run) >= min(load.urgent_wh, load.demand_wh)

            on = self.variables['ml_on'][i]
            if on is None:
                continue

            feasible = load.feasible or [True] * self.T
            for t in self.time_steps:
                cap = load.max_power_w * self.time_series.dt[t] / 3600.0
                allowed = t < len(feasible) and feasible[t]
                if not allowed or cap <= 0:
                    self.problem += on[t] == 0
                    continue
                # The switch *is* the draw, not merely a permission to draw. Binding it
                # one way only - run <= cap * on - leaves the binary free: the solver
                # sets it everywhere at no cost, every minimum-run constraint is then
                # satisfied by a switch that is on while nothing runs, and the appliance
                # gets scheduled in isolated slots anyway.
                #
                # So an appliance that asks for a minimum runtime is modelled as
                # on/off at its rated power, which is what a compressor does. The
                # demand ceiling may then be met a slot short rather than exactly;
                # that is the honest answer for something that cannot half-run.
                self.problem += run[t] == cap * on[t]

            # A start obliges the next min_runtime_slots-1 slots. The rising edge is
            # `on[t] - on[t-1]`, with nothing before the horizon, so a run beginning in
            # slot 0 counts as a start too.
            span = int(load.min_runtime_slots)
            for t in self.time_steps:
                started = on[t] if t == 0 else on[t] - on[t - 1]
                for step in range(1, span):
                    if t + step < self.T:
                        self.problem += on[t + step] >= started

    def solve(self) -> Dict:
        """
        Creates the MILP model if none exists and solves the optimization problem.
        Returns a dictionary with the optimization results.
        """
        if self.problem is None:
            self.create_model()

        solver = _resolve_cbc_solver(
            msg=0,
            num_threads=self.settings.num_threads,
            time_limit=self.settings.time_limit,
            gapRel=self.settings.gapRel,
        )

        with TemporaryDirectory() as tmpdir:
            solver.tmpDir = tmpdir
            self.problem.solve(solver)

        status = pulp.LpStatus[self.problem.status]

        e_grid_import = [pulp.value(var) or 0.0 for var in self.variables['n']]
        e_grid_export = [pulp.value(var) or 0.0 for var in self.variables['e']]

        # if demand rate is active, add the excess import back
        if self.is_grid_demand_rate_active:
            for t in self.time_steps:
                e_grid_import[t] += pulp.value(self.variables['e_imp_lim_exc'][t]) or 0.0

        # Limit violations
        grid_imp_limit_violated = False
        e_grid_imp_overshoot = []
        if self.grid.p_max_imp is not None:
            exc_vals = [pulp.value(var) or 0.0 for var in self.variables['e_imp_lim_exc']]
            grid_imp_limit_violated = max(exc_vals) > 0
            e_grid_imp_overshoot = exc_vals

        grid_exp_limit_hit = False
        e_grid_exp_overshoot = []
        if self.grid.p_max_exp is not None:
            exc_vals = [pulp.value(var) or 0.0 for var in self.variables['e_exp_lim_exc']]
            grid_exp_limit_hit = max(exc_vals) > 0
            e_grid_exp_overshoot = exc_vals

        if status == 'Optimal':
            result = {
                'status': status,
                'objective_value': self.get_clean_objective_value(),
                'limit_violations': {
                    'grid_import_limit_exceeded': grid_imp_limit_violated,
                    'grid_export_limit_hit': grid_exp_limit_hit,
                },
                'batteries': [],
                'grid_import': e_grid_import,
                'grid_export': e_grid_export,
                'flow_direction': [],
                'grid_import_overshoot': e_grid_imp_overshoot,
                'grid_export_overshoot': e_grid_exp_overshoot,
                'managed_loads': [
                    {
                        'id': load.id,
                        'energy': [
                            pulp.value(var) or 0.0 for var in self.variables['ml'][i]
                        ],
                    }
                    for i, load in enumerate(self.managed_loads)
                ],
            }
            for i, bat in enumerate(self.batteries):
                result['batteries'].append({
                    'charging_power': [pulp.value(var) or 0.0 for var in self.variables['c'][i]],
                    'discharging_power': [pulp.value(var) or 0.0 for var in self.variables['d'][i]],
                    'state_of_charge': [pulp.value(var) or 0.0 for var in self.variables['s'][i]],
                })
            for y_var in self.variables['y']:
                if y_var is not None:
                    result['flow_direction'].append(int(pulp.value(y_var) or 0))
                else:
                    result['flow_direction'].append(0)
            return result
        else:
            return {
                'status': status,
                'objective_value': None,
                'limit_violations': {
                    'grid_import_limit_exceeded': False,
                    'grid_export_limit_hit': False,
                },
                'batteries': [],
                'grid_import': [],
                'grid_export': [],
                'flow_direction': [],
                'grid_import_overshoot': [],
                'grid_export_overshoot': [],
            }

    def get_clean_objective_value(self):
        """Recalculate the objective value without penalties and strategy incentives."""
        clean_objective = 0
        for t in self.time_steps:
            if self.grid.p_max_imp is not None:
                clean_objective -= (
                    (pulp.value(self.variables['n'][t]) or 0.0)
                    + (pulp.value(self.variables['e_imp_lim_exc'][t]) or 0.0)
                ) * self.time_series.p_N[t]
            else:
                clean_objective -= (
                    (pulp.value(self.variables['n'][t]) or 0.0)
                    * self.time_series.p_N[t]
                )
        for t in self.time_steps:
            clean_objective += (pulp.value(self.variables['e'][t]) or 0.0) * self.time_series.p_E[t]
        for i, bat in enumerate(self.batteries):
            clean_objective += (
                (pulp.value(self.variables['s'][i][self.T - 1]) or 0.0)
                - (pulp.value(self.variables['s'][i][0]) or 0.0)
            ) * bat.p_a
        if self.is_grid_demand_rate_active:
            clean_objective += -self.grid.prc_p_exc_imp * (
                pulp.value(self.variables['p_max_imp_exc']) or 0.0
            )
        return clean_objective
