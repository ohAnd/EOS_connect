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
        if charging_strategy in CHARGING_STRATEGIES:
            self.charging_strategy = charging_strategy
        else:
            self.charging_strategy = "charge_before_export"
        if discharging_strategy in DISCHARGING_STRATEGIES:
            self.discharging_strategy = discharging_strategy
        else:
            self.discharging_strategy = "discharge_before_import"
        self.emergency_reserve_pct = max(0, min(80, int(emergency_reserve_pct or 0)))
        self.max_grid_import_w = max_grid_import_w
        self.max_grid_export_w = max_grid_export_w
        # Initialize rolling average runtime tracking (5-element circular buffer)
        self.last_optimization_runtimes = [0.0] * 5
        self.last_optimization_runtime_number = 0

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

        # Extend forecast with synthetic morning PV when forecast ends at night
        # This teaches the optimizer that "after night comes day with PV"
        evopt_request = self._extend_forecast_with_morning_pv(evopt_request)

        # Optionally write debug file
        self._write_debug_file(evopt_request, "optimize_request_local_evopt.json")

        try:
            start_time = time.time()

            optimizer = self._build_optimizer(evopt_request, timeout)
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
            status_lower = status.lower() if isinstance(status, str) else ""
            if status_lower in ("infeasible", "unbounded", "undefined", "not solved"):
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

    def _build_optimizer(self, evopt_request, timeout):
        """Construct the Optimizer object from an EVopt-format request dict."""
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
            _p_demand = (
                _p_demand_raw
                if _p_demand_raw and any(v > 0 for v in _p_demand_raw)
                else None
            )

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
        # Combine both series and find the maximum value across all elements
        all_values = (ts_data.get("gt") or [0.0]) + (ts_data.get("ft") or [0.0])
        _max_energy = max(all_values) if all_values else 0.0

        # Include grid flow energy in tight_M calculation to prevent spurious Infeasible
        # When grid limits are large (e.g., 20kW), the grid flow energy can exceed
        # the Big-M constant if not explicitly included, causing unsatisfiable constraints
        _max_grid_flow = max(
            (self.max_grid_import_w or 0) * _max_dt / 3600,
            (self.max_grid_export_w or 0) * _max_dt / 3600,
        )

        # 2× safety margin so M is never inadvertently binding
        tight_M = max(_max_bat_flow + _max_energy + _max_grid_flow, 100.0) * 2
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

    def _extend_forecast_with_morning_pv(self, evopt_request):
        """
        Extend time_series.ft (PV forecast) with synthetic morning production
        if forecast ends during nighttime hours.

        Purpose: Prevent optimizer from expensive grid charging at end-of-horizon
        when free morning PV is predictably coming. Teaches the optimizer that
        "after night comes day with PV".

        Args:
            evopt_request: EVopt format request dict

        Returns:
            Modified evopt_request with extended time_series arrays
        """
        from datetime import datetime

        ts = evopt_request.get("time_series", {})
        ft = ts.get("ft", [])
        dt = ts.get("dt", [])

        if not ft or not dt:
            return evopt_request

        # Calculate what hour the last forecast slot represents
        now = datetime.now(self.time_zone)
        total_forecast_seconds = sum(dt)
        forecast_hours = total_forecast_seconds / 3600
        forecast_end_hour = int((now.hour + forecast_hours) % 24)

        # Check if forecast ends at night (19:00-05:00)
        is_nighttime_end = 19 <= forecast_end_hour or forecast_end_hour <= 5

        if not is_nighttime_end:
            logger.debug(
                "[OPT-LocalEVopt] Forecast ends during daytime (hour %d) - no extension needed",
                forecast_end_hour
            )
            return evopt_request

        # Check last 6 slots to confirm nighttime (minimal PV)
        last_6_pv = sum(ft[-6:]) if len(ft) >= 6 else sum(ft)
        if last_6_pv > 100:  # More than 100 Wh in last 1.5h = not really nighttime
            logger.debug(
                "[OPT-LocalEVopt] Forecast ends at hour %d but has PV (%.0f Wh) "
                "- no extension needed",
                forecast_end_hour, last_6_pv
            )
            return evopt_request

        # Extract PV capacity from the forecast for scaling the morning pattern
        # ft array contains energy (Wh) per slot, need to convert to power (W)
        # Power (W) = Energy (Wh) / (time_slot_seconds / 3600)
        max_pv_energy_wh = max(ft) if ft else 0.0
        time_slot_hours = self.time_frame_base / 3600.0
        pv_capacity_w = (
            max_pv_energy_wh / time_slot_hours if time_slot_hours > 0 else 0.0
        )

        if pv_capacity_w <= 0:
            logger.warning(
                "[OPT-LocalEVopt] Cannot determine PV capacity (max PV=%.1f Wh/slot) "
                "- skipping forecast extension",
                max_pv_energy_wh
            )
            return evopt_request

        # Generate synthetic morning PV pattern
        extension_hours = 6  # Extend 6 hours into morning (06:00-12:00)
        morning_slots = self._generate_morning_pv_pattern(
            pv_capacity=pv_capacity_w,
            time_frame_base=self.time_frame_base,
            hours=extension_hours
        )

        n_slots_added = len(morning_slots)
        original_slot_count = len(ts["ft"])

        # Extend all time_series arrays consistently
        ts["ft"].extend(morning_slots)

        # For load, prices: repeat last value (conservative assumption)
        last_load = ts["gt"][-1] if ts.get("gt") else 0.0
        last_price_import = ts["p_N"][-1] if ts.get("p_N") else 0.0
        last_price_export = ts["p_E"][-1] if ts.get("p_E") else 0.0

        ts["gt"].extend([last_load] * n_slots_added)
        ts["p_N"].extend([last_price_import] * n_slots_added)
        ts["p_E"].extend([last_price_export] * n_slots_added)
        ts["dt"].extend([self.time_frame_base] * n_slots_added)

        # Extend battery arrays (p_demand, s_goal) with zeros
        for bat in evopt_request.get("batteries", []):
            if "p_demand" in bat and isinstance(bat["p_demand"], list):
                bat["p_demand"].extend([0.0] * n_slots_added)
            if "s_goal" in bat and isinstance(bat["s_goal"], list):
                bat["s_goal"].extend([0.0] * n_slots_added)

        logger.info(
            "[OPT-LocalEVopt] Smart forecast extension: Added %d synthetic morning "
            "slots (06:00-12:00 pattern) to teach optimizer about cyclical "
            "day/night. Forecast extended from %d to %d slots. PV capacity: %.0f W",
            n_slots_added, original_slot_count, len(ts["ft"]), pv_capacity_w
        )

        return evopt_request

    def _generate_morning_pv_pattern(self, pv_capacity, time_frame_base, hours):
        """
        Generate conservative morning PV ramp pattern.

        Based on the existing fallback pattern in pv_interface.py,
        uses a conservative 10-50% ramp from 06:00 to 12:00.

        Args:
            pv_capacity: Peak PV power capacity in Watts
            time_frame_base: Time slot duration in seconds (900 or 3600)
            hours: Number of hours to generate (typically 6)

        Returns:
            list: PV energy values in Wh per slot
        """
        slots_per_hour = 3600 // time_frame_base
        total_slots = hours * slots_per_hour

        # Conservative morning ramp pattern (matches pv_interface.py fallback)
        # 06:00=10%, 07:00=20%, 08:00=30%, 09:00=40%, 10:00=50%, 11:00=50%
        hourly_pattern = [0.1, 0.2, 0.3, 0.4, 0.5, 0.5]

        pattern = []
        for hour_idx in range(hours):
            # Get the percentage factor for this hour
            if hour_idx < len(hourly_pattern):
                factor = hourly_pattern[hour_idx]
            else:
                factor = 0.5

            # Power (W) * time (s) / 3600 = Energy (Wh)
            power_w = pv_capacity * factor
            energy_wh = power_w * time_frame_base / 3600

            # Repeat for all slots in this hour
            pattern.extend([energy_wh] * slots_per_hour)

        return pattern[:total_slots]
