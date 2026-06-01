"""
Module: optimization_backend_local_evopt
Provides LocalEVOptBackend — a locally-running MILP optimizer that runs the evopt
optimization engine in-process (no external HTTP server required).

Inherits all EOS↔EVopt data transformation logic from EVOptBackend and overrides
the optimize() method to call the bundled PuLP/CBC solver directly.

License note: The bundled optimizer engine (local_evopt/optimizer.py) is derived from
evcc-io/optimizer (MIT License, Copyright (c) 2025 andig).
"""

import json
import logging
import os
import time

from .optimization_backend_evopt import EVOptBackend
from .local_evopt.optimizer import (
    BatteryConfig,
    GridConfig,
    OptimizationStrategy,
    Optimizer,
    OptimizerSettings,
    TimeSeriesData,
)

logger = logging.getLogger("__main__")

# Valid strategy constants
CHARGING_STRATEGIES = {
    "none",
    "charge_before_export",
    "attenuate_grid_peaks",
    "maximize_self_consumption",
}
DISCHARGING_STRATEGIES = {
    "none",
    "discharge_before_import",
    "emergency_reserve",
}


class LocalEVOptBackend(EVOptBackend):
    """
    In-process MILP optimizer backend.

    Inherits all EOS↔EVopt request/response transformation logic from EVOptBackend
    and replaces the HTTP call with a direct call to the bundled PuLP/CBC solver.

    Args:
        time_frame_base:          Slot duration in seconds (900 or 3600).
        time_zone:                pytz timezone for time calculations.
        num_threads:              CBC solver thread count (None = auto).
        time_limit:               CBC solver time limit in seconds (None = unlimited).
        charging_strategy:        Strategy string for charging preferences.
        discharging_strategy:     Strategy string for discharging preferences.
        emergency_reserve_pct:    Minimum battery SOC at end-of-horizon (0-80 %).
        max_grid_import_w:        Hard grid import power ceiling in Watts (None = unlimited).
        max_grid_export_w:        Hard grid export power ceiling in Watts (None = unlimited).
    """

    def __init__(
        self,
        time_frame_base,
        time_zone,
        num_threads=None,
        time_limit=None,
        charging_strategy="charge_before_export",
        discharging_strategy="discharge_before_import",
        emergency_reserve_pct=0,
        max_grid_import_w=None,
        max_grid_export_w=None,
    ):
        # base_url is not used in-process; pass a placeholder so parent __init__ is happy
        super().__init__(
            base_url="local://",
            time_frame_base=time_frame_base,
            time_zone=time_zone,
        )
        self.num_threads = num_threads
        self.time_limit = time_limit
        self.charging_strategy = charging_strategy if charging_strategy in CHARGING_STRATEGIES else "charge_before_export"
        self.discharging_strategy = discharging_strategy if discharging_strategy in DISCHARGING_STRATEGIES else "discharge_before_import"
        self.emergency_reserve_pct = max(0, min(80, int(emergency_reserve_pct or 0)))
        self.max_grid_import_w = max_grid_import_w
        self.max_grid_export_w = max_grid_export_w

    def optimize(self, eos_request, timeout=180):
        """
        Run the MILP optimizer in-process.

        1. Transform EOS request → EVopt format (inherited transformation)
        2. Build Optimizer from EVopt request data
        3. Solve in-process using PuLP/CBC
        4. Transform EVopt response → EOS format (inherited transformation)
        5. Return (eos_response, avg_runtime)
        """
        evopt_request, errors = self._transform_request_from_eos_to_evopt(eos_request)
        if errors:
            logger.error("[OPT-LocalEVopt] Request transformation errors: %s", errors)

        # Truncate time series to valid future slots only.
        # The EVopt request builds 192 slots via a circular wrap of the 48-hour EOS
        # array starting at current_slot.  Slots beyond n_result correspond to stale
        # *past* data (today 00:00 → now) wrapped around to the tail.  If those
        # slots happen to carry a price of 0 ct/kWh (e.g. today's noon surplus),
        # the MILP exploits them as "free electricity" and ignores real PV tomorrow.
        # Truncating to n_result removes the stale region entirely.
        time_params = self._calculate_time_parameters()
        n_valid = time_params["n_result"]
        ts = evopt_request.get("time_series", {})
        for key in ("dt", "gt", "ft", "p_N", "p_E"):
            if key in ts and isinstance(ts[key], list) and len(ts[key]) > n_valid:
                ts[key] = ts[key][:n_valid]
        for bat in evopt_request.get("batteries", []):
            for key in ("p_demand", "s_goal"):
                if key in bat and isinstance(bat[key], list) and len(bat[key]) > n_valid:
                    bat[key] = bat[key][:n_valid]

        # Optionally write debug file
        self._write_debug_file(evopt_request, "optimize_request_local_evopt.json")

        try:
            start_time = time.time()

            optimizer = self._build_optimizer(evopt_request, eos_request, timeout)
            evopt_response = optimizer.solve()

            elapsed = time.time() - start_time
            minutes, seconds = divmod(elapsed, 60)
            logger.info(
                "[OPT-LocalEVopt] Solved in %d min %.2f sec — status: %s",
                int(minutes),
                seconds,
                evopt_response.get("status", "unknown"),
            )

            # Update rolling average runtime
            if all(r == 0 for r in self.last_optimization_runtimes):
                self.last_optimization_runtimes = [elapsed] * 5
            else:
                self.last_optimization_runtimes[self.last_optimization_runtime_number] = elapsed
            self.last_optimization_runtime_number = (self.last_optimization_runtime_number + 1) % 5
            avg_runtime = sum(self.last_optimization_runtimes) / 5

            # Guard: handle infeasible / non-optimal result
            status = evopt_response.get("status", "")
            if isinstance(status, str) and status.lower() in ("infeasible", "unbounded", "undefined", "not solved"):
                logger.warning(
                    "[OPT-LocalEVopt] Solver returned non-optimal status '%s'; "
                    "returning safe EOS infeasible payload.",
                    status,
                )
                return self._infeasible_eos_response(evopt_response), avg_runtime

            self._write_debug_file(evopt_response, "optimize_response_local_evopt.json")

            eos_response = self._transform_response_from_evopt_to_eos(
                evopt_response, evopt_request, eos_request
            )
            return eos_response, avg_runtime

        except ImportError as exc:
            logger.error(
                "[OPT-LocalEVopt] PuLP is not installed — cannot run local optimizer. "
                "Install it with: pip install pulp>=2.7.0 — error: %s", exc
            )
            return {"error": "PuLP not installed — run: pip install pulp>=2.7.0"}, None
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("[OPT-LocalEVopt] Optimization failed: %s", exc, exc_info=True)
            return {"error": f"Local optimizer failed: {exc}"}, None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_optimizer(self, evopt_request, eos_request, timeout):
        """Construct the Optimizer object from an EVopt-format request dict."""
        strat_data = evopt_request.get("strategy", {})
        # Use configured strategies (may override what the transformation put in)
        strategy = OptimizationStrategy(
            charging_strategy=self.charging_strategy,
            discharging_strategy=self.discharging_strategy,
        )

        grid_data = evopt_request.get("grid", {})
        # Use only user-configured grid limits — never fall back to the EVopt server
        # placeholder defaults (p_max_imp=10000, p_max_exp=10000).  Those defaults
        # are only meaningful for the external EVopt server; using them here creates
        # 2×T unnecessary binary variables (z_imp_lim, z_exp_lim) that make the
        # MILP solver ~4× slower without affecting the solution quality.
        # Note: treat prc_p_exc_imp=0 as None so that user-configured grid limits
        # use hard constraints (not the demand-rate soft-limit path).
        _prc_raw = grid_data.get("prc_p_exc_imp") or grid_data.get("prc_p_imp_exc")
        grid = GridConfig(
            p_max_imp=self.max_grid_import_w,  # None = no limit (no binary vars added)
            p_max_exp=self.max_grid_export_w,  # None = no limit (no binary vars added)
            prc_p_exc_imp=_prc_raw if _prc_raw else None,
        )

        batteries = []
        for bat_data in evopt_request.get("batteries", []):
            s_max = float(bat_data.get("s_max", 0))
            s_capacity = float(bat_data.get("s_capacity", s_max))

            # Emergency reserve: convert % to Wh using full capacity
            s_reserve_wh = 0.0
            if self.emergency_reserve_pct > 0 and s_capacity > 0:
                s_reserve_wh = s_capacity * (self.emergency_reserve_pct / 100.0)

            # Skip p_demand when all values are zero — avoids T binary variables
            # (z_p_demand) that are created but never activated in constraints.
            _p_demand_raw = bat_data.get("p_demand")
            _p_demand = _p_demand_raw if (_p_demand_raw and any(v > 0 for v in _p_demand_raw)) else None

            batteries.append(BatteryConfig(
                charge_from_grid=bat_data.get("charge_from_grid", False),
                discharge_to_grid=bat_data.get("discharge_to_grid", False),
                s_capacity=s_capacity,
                s_min=float(bat_data.get("s_min", 0)),
                s_max=s_max,
                s_initial=float(bat_data.get("s_initial", 0)),
                p_demand=_p_demand,
                s_goal=bat_data.get("s_goal"),
                c_min=float(bat_data.get("c_min", 0)),
                c_max=float(bat_data.get("c_max", 0)),
                d_max=float(bat_data.get("d_max", 0)),
                p_a=float(bat_data.get("p_a", 0)),
                c_priority=int(bat_data.get("c_priority", 0)),
                s_reserve=s_reserve_wh,
            ))

        ts_data = evopt_request.get("time_series", {})
        time_series = TimeSeriesData(
            dt=ts_data.get("dt", []),
            gt=ts_data.get("gt", []),
            ft=ts_data.get("ft", []),
            p_N=ts_data.get("p_N", []),
            p_E=ts_data.get("p_E", []),
        )

        # Solver settings
        # timeout parameter is the EOS timeout; use as an upper bound for the solver
        solver_time_limit = self.time_limit
        if solver_time_limit is None and timeout is not None:
            # Leave 20% headroom vs overall EOS timeout
            solver_time_limit = timeout * 0.8

        settings = OptimizerSettings(
            num_threads=self.num_threads,
            time_limit=solver_time_limit,
        )

        # Compute a tight Big-M from actual problem data.
        # The default M=1e6 creates extremely weak LP relaxations (e.g. a grid
        # export bound of 500 000 Wh when y=0.5), forcing CBC to explore
        # exponentially more B&B nodes.  A value that just covers the maximum
        # realistic energy flow per slot is ~10 000× smaller and makes the
        # solver ~100× faster for 15-min (192-slot) problems.
        _max_dt = max(ts_data.get("dt") or [900])
        _max_bat_flow = 0.0
        for _bd in evopt_request.get("batteries", []):
            _max_bat_flow = max(
                _max_bat_flow,
                float(_bd.get("c_max", 0)) * _max_dt / 3600,
                float(_bd.get("d_max", 0)) * _max_dt / 3600,
            )
        _max_energy = max(
            max(ts_data.get("gt") or [0.0]),
            max(ts_data.get("ft") or [0.0]),
        )
        # 2× safety margin so M is never inadvertently binding
        tight_M = max(_max_bat_flow + _max_energy, 100.0) * 2
        _n_slots = len(ts_data.get("dt") or [])
        logger.debug(
            "[OPT-LocalEVopt] Building MILP: T=%d slots, dt=%ds, tight_M=%.0f "
            "(vs default M=1e6, ratio=%.0fx smaller)",
            _n_slots,
            int(_max_dt),
            tight_M,
            1e6 / tight_M if tight_M > 0 else 0,
        )

        return Optimizer(
            strategy=strategy,
            grid=grid,
            batteries=batteries,
            time_series=time_series,
            eta_c=float(evopt_request.get("eta_c", 0.95)),
            eta_d=float(evopt_request.get("eta_d", 0.95)),
            optimizer_settings=settings,
            M=tight_M,
        )

    @staticmethod
    def _infeasible_eos_response(evopt_response):
        """Return a safe EOS-format infeasible response dict."""
        return {
            "status": "Infeasible",
            "objective_value": None,
            "limit_violations": evopt_response.get("limit_violations", {}),
            "batteries": [],
            "grid_import": [],
            "grid_export": [],
            "flow_direction": [],
            "grid_import_overshoot": [],
            "grid_export_overshoot": [],
        }

    def _write_debug_file(self, data, filename):
        """Write a debug JSON file next to the existing json/ folder."""
        debug_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "json", filename)
        )
        try:
            with open(debug_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.debug("[OPT-LocalEVopt] Could not write debug file %s: %s", filename, exc)
