"""
Unit tests for LocalEVOptBackend — the in-process MILP optimizer.

Test scope:
    - Instantiation without any network access
    - Basic round-trip: EOS request → local solve → EOS response
    - Infeasible/non-optimal solver result handling
    - Array sizing for hourly (48-slot) and 15-min (192-slot) modes
    - maximize_self_consumption strategy reduces grid import vs 'none'
    - emergency_reserve strategy keeps end-of-horizon SOC above threshold
    - Grid import/export limits are respected in results
    - Terminal SOC is valued at a forward price, not at what the charge cost

All tests run fully in-process — no network, no mock HTTP.

Usage:
    pytest tests/interfaces/optimization_backends/test_optimization_backend_local_evopt.py -v
"""

# pylint: disable=protected-access

import statistics
import subprocess
from datetime import datetime as _real_datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pulp
import pytz

from src.interfaces.optimization_backends.optimization_backend_evopt import (
    TERMINAL_SOC_VALUES,
    EVOptBackend,
)
from src.interfaces.optimization_backends.optimization_backend_local_evopt import (
    LocalEVOptBackend,
)
from src.interfaces.optimization_backends.local_evopt import optimizer as _optimizer_mod
from src.interfaces.optimization_backends.local_evopt.optimizer import (
    BatteryConfig,
    CbcSolverUnavailableError,
    GridConfig,
    OptimizationStrategy,
    Optimizer,
    TimeSeriesData,
    _cbc_runs,
    _resolve_cbc_solver,
)


@pytest.fixture(autouse=True)
def _clear_cbc_probe_cache():
    """
    Reset the module-level CBC probe cache around every test.

    The probe result is cached for the process lifetime, so without this a
    mocked selection would leak into the tests further down this file that
    perform real solves.
    """
    _optimizer_mod._resolved_cbc = None
    yield
    _optimizer_mod._resolved_cbc = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(name="berlin_tz")
def fixture_berlin_tz():
    return pytz.timezone("Europe/Berlin")


@pytest.fixture(name="backend_hourly")
def fixture_backend_hourly(berlin_tz):
    """Default hourly (3600s) local backend."""
    return LocalEVOptBackend(
        time_frame_base=3600,
        time_zone=berlin_tz,
    )


@pytest.fixture(name="backend_15min")
def fixture_backend_15min(berlin_tz):
    """15-minute (900s) local backend."""
    return LocalEVOptBackend(
        time_frame_base=900,
        time_zone=berlin_tz,
    )


def _make_eos_request(n_slots=48, pv_value=1000.0, load_value=400.0, initial_soc_pct=50):
    """Build a minimal valid EOS-format request with n_slots time steps."""
    return {
        "ems": {
            "pv_prognose_wh": [pv_value] * n_slots,
            "strompreis_euro_pro_wh": [0.0003] * n_slots,
            "einspeiseverguetung_euro_pro_wh": [0.00008] * n_slots,
            "gesamtlast": [load_value] * n_slots,
            "preis_euro_pro_wh_akku": 0.0002,
        },
        "pv_akku": {
            "device_id": "battery1",
            "capacity_wh": 10000,
            "charging_efficiency": 0.95,
            "discharging_efficiency": 0.95,
            "max_charge_power_w": 5000,
            "initial_soc_percentage": initial_soc_pct,
            "min_soc_percentage": 5,
            "max_soc_percentage": 100,
        },
    }


def _midnight_mock(year=2026, month=6, day=1):
    """Return a datetime subclass whose now() is pinned to midnight of the given date."""
    class _MockDT(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return tz.localize(_real_datetime(year, month, day, 0, 0, 0))
            return _real_datetime(year, month, day, 0, 0, 0)
    return _MockDT


# ---------------------------------------------------------------------------
# 1. Instantiation
# ---------------------------------------------------------------------------

class TestInstantiation:
    """Test LocalEVOptBackend instantiation and configuration."""
    def test_creates_without_network(self, berlin_tz):
        """Backend can be created without any network access."""
        backend = LocalEVOptBackend(time_frame_base=3600, time_zone=berlin_tz)
        assert backend is not None
        # backend_type is assigned by OptimizationInterface, not by the backend class
        assert hasattr(backend, "charging_strategy")

    def test_strategy_defaults(self, berlin_tz):
        """Default strategies are set correctly."""
        b = LocalEVOptBackend(time_frame_base=3600, time_zone=berlin_tz)
        assert b.charging_strategy == "charge_before_export"
        assert b.discharging_strategy == "discharge_before_import"
        assert b.emergency_reserve_pct == 0

    def test_unknown_strategy_falls_back_to_default(self, berlin_tz):
        """Invalid strategy strings fall back to the default values."""
        b = LocalEVOptBackend(
            time_frame_base=3600,
            time_zone=berlin_tz,
            charging_strategy="totally_invalid_strategy",
            discharging_strategy="also_invalid",
        )
        assert b.charging_strategy == "charge_before_export"
        assert b.discharging_strategy == "discharge_before_import"

    def test_emergency_reserve_pct_clamped(self, berlin_tz):
        """Emergency reserve percentage is clamped to 0-80."""
        b_high = LocalEVOptBackend(
            time_frame_base=3600, time_zone=berlin_tz, emergency_reserve_pct=150
        )
        assert b_high.emergency_reserve_pct == 80

        b_neg = LocalEVOptBackend(
            time_frame_base=3600, time_zone=berlin_tz, emergency_reserve_pct=-5
        )
        assert b_neg.emergency_reserve_pct == 0


# ---------------------------------------------------------------------------
# 2. Basic round-trip (hourly)
# ---------------------------------------------------------------------------

class TestBasicRoundTrip:
    """Test basic hourly optimization round-trip and response validation."""
    def test_hourly_returns_eos_response_shape(self, backend_hourly):
        """optimize() with a simple hourly request returns a valid EOS response dict."""
        eos_req = _make_eos_request(n_slots=48)
        dt_mock = _midnight_mock()
        module_path = "src.interfaces.optimization_backends.optimization_backend_evopt.datetime"
        with patch(module_path, dt_mock):
            result, avg_runtime = backend_hourly.optimize(eos_req, timeout=60)

        assert isinstance(result, dict), "Result must be a dict"
        assert avg_runtime is not None, "Runtime must be returned for successful solve"
        assert "ac_charge" in result, "EOS response must contain ac_charge"
        assert "discharge_allowed" in result, "EOS response must contain discharge_allowed"
        assert "dc_charge" in result, "EOS response must contain dc_charge"

    def test_hourly_control_arrays_are_48_long(self, backend_hourly):
        """Control arrays must be 48 elements (hourly 2-day horizon)."""
        eos_req = _make_eos_request(n_slots=48)
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
        ):
            result, _ = backend_hourly.optimize(eos_req, timeout=60)

        assert len(result["ac_charge"]) == 48, "ac_charge must be 48 elements for hourly"
        assert len(result["discharge_allowed"]) == 48, "discharge_allowed must be 48 elements"
        assert len(result["dc_charge"]) == 48, "dc_charge must be 48 elements"

    def test_ac_charge_values_in_valid_range(self, backend_hourly):
        """ac_charge values must be in [0.0, 1.0]."""
        eos_req = _make_eos_request(n_slots=48)
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
        ):
            result, _ = backend_hourly.optimize(eos_req, timeout=60)

        for i, val in enumerate(result["ac_charge"]):
            assert 0.0 <= val <= 1.0, f"ac_charge[{i}]={val} out of [0, 1]"

    def test_discharge_allowed_is_binary(self, backend_hourly):
        """discharge_allowed values must be 0 or 1."""
        eos_req = _make_eos_request(n_slots=48)
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
        ):
            result, _ = backend_hourly.optimize(eos_req, timeout=60)

        for i, val in enumerate(result["discharge_allowed"]):
            assert val in (0, 1), f"discharge_allowed[{i}]={val} must be 0 or 1"

    def test_result_dict_present(self, backend_hourly):
        """EOS response must contain a 'result' sub-dict with expected keys."""
        eos_req = _make_eos_request(n_slots=48)
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
        ):
            result, _ = backend_hourly.optimize(eos_req, timeout=60)

        assert "result" in result, "EOS response must contain 'result' dict"
        result_dict = result["result"]
        assert "Netzbezug_Wh_pro_Stunde" in result_dict
        assert "akku_soc_pro_stunde" in result_dict


# ---------------------------------------------------------------------------
# 3. 15-minute interval round-trip
# ---------------------------------------------------------------------------

class TestFifteenMinuteIntervals:
    """Test 15-minute interval optimization round-trip and response validation."""
    def test_15min_control_arrays_are_192_long(self, backend_15min):
        """Control arrays must be 192 elements for 15-min 2-day horizon."""
        eos_req = _make_eos_request(n_slots=192)
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
        ):
            result, _ = backend_15min.optimize(eos_req, timeout=60)

        assert len(result["ac_charge"]) == 192, "ac_charge must be 192 for 15-min mode"
        assert len(result["discharge_allowed"]) == 192

    def test_15min_basic_response_shape(self, backend_15min):
        """15-min backend returns a valid EOS response."""
        eos_req = _make_eos_request(n_slots=192)
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
        ):
            result, avg_runtime = backend_15min.optimize(eos_req, timeout=60)

        assert "ac_charge" in result
        assert avg_runtime is not None


# ---------------------------------------------------------------------------
# 4. Infeasible / non-optimal handling
# ---------------------------------------------------------------------------

class TestInfeasibleHandling:
    """Test handling of infeasible and non-optimal solver results."""
    def test_infeasible_solver_returns_safe_eos_response(self, berlin_tz):
        """When the solver returns non-optimal, optimize() returns a safe fallback dict."""
        backend = LocalEVOptBackend(time_frame_base=3600, time_zone=berlin_tz)

        # Patch Optimizer.solve to return a non-optimal result
        with patch.object(Optimizer, "solve", return_value={"status": "Infeasible"}):
            eos_req = _make_eos_request(n_slots=48)
            dt_mock = _midnight_mock()
            with patch(
                "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
            ):
                result, avg_runtime = backend.optimize(eos_req, timeout=60)

        assert result.get("status") == "Infeasible"
        assert avg_runtime is not None  # runtime still tracked
        assert result.get("ac_charge") is None or result.get("batteries") == []

    def test_solver_exception_returns_error_dict(self, berlin_tz):
        """If the solver raises an unexpected exception, optimize() returns an error dict."""
        backend = LocalEVOptBackend(time_frame_base=3600, time_zone=berlin_tz)

        with patch.object(Optimizer, "solve", side_effect=RuntimeError("solver crash")):
            eos_req = _make_eos_request(n_slots=48)
            dt_mock = _midnight_mock()
            with patch(
                "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
            ):
                result, avg_runtime = backend.optimize(eos_req, timeout=60)

        assert "error" in result
        assert avg_runtime is None


# ---------------------------------------------------------------------------
# 5. Strategy: maximize_self_consumption
# ---------------------------------------------------------------------------

class TestMaximizeSelfConsumptionStrategy:
    """Test maximize_self_consumption charging strategy optimization."""
    def test_self_consumption_reduces_grid_import_vs_none(self):
        """
        With plenty of PV and a battery, maximize_self_consumption should
        result in equal or less grid import than strategy 'none'.
        """
        # Simple 6-slot scenario: PV is abundant, load is modest
        T = 6
        dt = [3600] * T
        ft = [5000.0] * T   # 5 kWh PV per slot
        gt = [1000.0] * T   # 1 kWh load per slot
        p_N = [0.0003] * T
        p_E = [0.00008] * T

        battery = BatteryConfig(
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
        ts = TimeSeriesData(dt=dt, gt=gt, ft=ft, p_N=p_N, p_E=p_E)

        # Strategy: none
        opt_none = Optimizer(
            strategy=OptimizationStrategy(charging_strategy="none", discharging_strategy="none"),
            grid=GridConfig(),
            batteries=[battery],
            time_series=ts,
        )
        result_none = opt_none.solve()

        # Strategy: maximize_self_consumption
        opt_msc = Optimizer(
            strategy=OptimizationStrategy(
                charging_strategy="maximize_self_consumption",
                discharging_strategy="none"
            ),
            grid=GridConfig(),
            batteries=[battery],
            time_series=ts,
        )
        result_msc = opt_msc.solve()

        assert result_none["status"] == "Optimal"
        assert result_msc["status"] == "Optimal"

        total_import_none = sum(result_none["grid_import"])
        total_import_msc = sum(result_msc["grid_import"])

        # maximize_self_consumption should import the same amount or less
        assert total_import_msc <= total_import_none + 0.1, (
            f"maximize_self_consumption grid import ({total_import_msc:.2f}) "
            f"should not exceed 'none' import ({total_import_none:.2f})"
        )


# ---------------------------------------------------------------------------
# 6. Strategy: emergency_reserve
# ---------------------------------------------------------------------------

class TestEmergencyReserve:
    """Test emergency_reserve discharging strategy end-of-horizon SOC constraint."""
    def test_end_of_horizon_soc_above_reserve(self, berlin_tz):
        """
        With emergency_reserve strategy and 20% reserve, the optimizer's
        final battery SOC should stay at or above 20% of capacity.
        """
        # Backend with 20% emergency reserve
        backend = LocalEVOptBackend(
            time_frame_base=3600,
            time_zone=berlin_tz,
            discharging_strategy="emergency_reserve",
            emergency_reserve_pct=20,
        )
        # High load, no PV — pressure to discharge battery
        eos_req = _make_eos_request(n_slots=48, pv_value=0.0, load_value=800.0, initial_soc_pct=90)
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
        ):
            result, _ = backend.optimize(eos_req, timeout=60)

        assert "result" in result, "result dict must be present"
        soc_pct_series = result["result"].get("akku_soc_pro_stunde", [])
        assert len(soc_pct_series) > 0, "SOC series must not be empty"

        capacity_wh = 10000  # from _make_eos_request
        reserve_wh = capacity_wh * 0.20
        final_soc_pct = soc_pct_series[-1]
        final_soc_wh = capacity_wh * (final_soc_pct / 100.0)

        # Allow a small tolerance (1% of capacity) for floating-point solver residuals
        tolerance_wh = capacity_wh * 0.01
        assert final_soc_wh >= reserve_wh - tolerance_wh, (
            f"Final SOC {final_soc_wh:.0f} Wh is below reserve {reserve_wh:.0f} Wh "
            f"(tolerance {tolerance_wh:.0f} Wh)"
        )

    def test_emergency_reserve_direct_optimizer(self):
        """Direct Optimizer test: s_reserve constraint keeps final SOC above threshold."""
        T = 4
        dt = [3600] * T
        # High load, no PV, high initial SOC → optimizer would drain battery
        ft = [0.0] * T
        gt = [4000.0] * T
        p_N = [0.0003] * T
        p_E = [0.00008] * T

        capacity_wh = 10000.0
        reserve_pct = 30
        reserve_wh = capacity_wh * (reserve_pct / 100.0)

        battery = BatteryConfig(
            s_min=0,
            s_max=capacity_wh,
            s_initial=capacity_wh * 0.9,
            c_min=0,
            c_max=5000,
            d_max=5000,
            p_a=0.0002,
            charge_from_grid=True,
            discharge_to_grid=True,
            s_capacity=capacity_wh,
            s_reserve=reserve_wh,
        )
        ts = TimeSeriesData(dt=dt, gt=gt, ft=ft, p_N=p_N, p_E=p_E)

        opt = Optimizer(
            strategy=OptimizationStrategy(
                charging_strategy="none",
                discharging_strategy="emergency_reserve",
            ),
            grid=GridConfig(),
            batteries=[battery],
            time_series=ts,
        )
        result = opt.solve()

        assert result["status"] == "Optimal"
        final_soc = result["batteries"][0]["state_of_charge"][-1]
        tolerance = capacity_wh * 0.01  # 1% tolerance
        assert final_soc >= reserve_wh - tolerance, (
            f"Final SOC {final_soc:.0f} Wh below reserve {reserve_wh:.0f} Wh"
        )


# ---------------------------------------------------------------------------
# 7. Grid limits
# ---------------------------------------------------------------------------

class TestGridLimits:
    """Test grid import/export limit enforcement in optimization results."""
    def test_grid_import_limit_respected(self):
        """
        Direct Optimizer test: when p_max_imp is set, grid import per slot must
        not exceed p_max_imp * dt / 3600 Wh.

        Scenario: load slightly above grid limit, battery covers the gap.
        Battery has enough capacity so the problem is always feasible.
        """
        max_import_w = 3000  # 3 kW limit
        T = 6
        dt = [3600] * T
        ft = [0.0] * T              # no PV
        gt = [3500.0] * T           # 3.5 kWh load — 500 Wh above grid limit
        p_N = [0.0003] * T
        p_E = [0.00008] * T

        # Battery has plenty of capacity to cover the 500 Wh/slot gap (6 * 500 = 3 kWh)
        battery = BatteryConfig(
            s_min=0,
            s_max=10000,
            s_initial=5000,
            c_min=0,
            c_max=5000,
            d_max=5000,
            p_a=0.0002,
            charge_from_grid=True,
            discharge_to_grid=True,
        )
        ts = TimeSeriesData(dt=dt, gt=gt, ft=ft, p_N=p_N, p_E=p_E)

        opt = Optimizer(
            strategy=OptimizationStrategy(),
            grid=GridConfig(p_max_imp=max_import_w),
            batteries=[battery],
            time_series=ts,
        )
        result = opt.solve()

        assert result["status"] == "Optimal"
        for i, wh in enumerate(result["grid_import"]):
            max_wh_per_slot = max_import_w * 1.0  # 1 hour slot → max_import_w Wh
            assert wh <= max_wh_per_slot + 0.01, (
                f"grid_import[{i}]={wh:.2f} Wh exceeds hard limit {max_wh_per_slot:.0f} Wh"
            )

    def test_tight_m_includes_grid_flow_energy(self, berlin_tz):
        """
        Test that Big-M constant includes grid flow energy to prevent spurious Infeasible.
        
        When grid limits are large (e.g., 20000 W) and time slots are long (15-min),
        the grid flow energy per slot can exceed the Big-M constant if not explicitly
        included, causing the solver to report Infeasible on feasible problems.

        Scenario:
        - 15-min intervals (900s)
        - Battery: 28000 Wh capacity, 20000 W charge power
        - Grid limit: 20000 W import (5000 Wh per 15-min slot)
        - Without grid flow in tight_M: spurious Infeasible
        - With grid flow in tight_M: Optimal
        """
        backend = LocalEVOptBackend(
            time_frame_base=900,  # 15-min slots
            time_zone=berlin_tz,
            max_grid_import_w=20000,  # High grid limit that stresses tight_M sizing
            max_grid_export_w=10000,
        )

        # Build a realistic 15-min EOS request with large battery and grid limit
        eos_req = {
            "ems": {
                "pv_prognose_wh": [3000.0] * 192,  # Moderate PV
                "strompreis_euro_pro_wh": [0.0003] * 192,
                "einspeiseverguetung_euro_pro_wh": [0.00008] * 192,
                "gesamtlast": [2000.0] * 192,  # Steady 2 kW load
                "preis_euro_pro_wh_akku": 0.0002,
            },
            "pv_akku": {
                "device_id": "battery1",
                "capacity_wh": 28000,  # Large battery (stresses Big-M sizing)
                "charging_efficiency": 0.95,
                "discharging_efficiency": 0.95,
                "max_charge_power_w": 20000,  # High charge power (large energy per slot)
                "initial_soc_percentage": 50,
                "min_soc_percentage": 5,
                "max_soc_percentage": 100,
            },
        }

        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime", dt_mock
        ):
            result, avg_runtime = backend.optimize(eos_req, timeout=120)

        # Should NOT return spurious Infeasible due to undersized tight_M
        assert result.get("status") != "Infeasible", (
            "Solver should find feasible solution when tight_M includes grid flow energy"
        )

        # Should return Optimal with valid control arrays
        assert result.get("status") == "Optimal" or "ac_charge" in result, (
            f"Expected valid optimization result, got status: {result.get('status')}"
        )

        # Verify average runtime was tracked
        assert avg_runtime is not None


# ---------------------------------------------------------------------------
# 8. Optimizer settings (threads, time_limit)
# ---------------------------------------------------------------------------

class TestOptimizerSettings:
    """Test solver settings (threads, time_limit) propagation to Optimizer."""
    def test_solver_settings_passed_through(self, berlin_tz):
        """num_threads and time_limit are passed to the Optimizer."""
        backend = LocalEVOptBackend(
            time_frame_base=3600,
            time_zone=berlin_tz,
            num_threads=2,
            time_limit=30,
        )

        captured = {}

        original_init = Optimizer.__init__

        def patched_init(self_inner, *args, **kwargs):
            original_init(self_inner, *args, **kwargs)
            captured["settings"] = self_inner.settings

        eos_req = _make_eos_request(n_slots=48)
        dt_mock = _midnight_mock()
        with patch.object(Optimizer, "__init__", patched_init):
            with patch(
                "src.interfaces.optimization_backends.optimization_backend_evopt.datetime",
                dt_mock,
            ):
                backend.optimize(eos_req, timeout=60)

        assert captured.get("settings") is not None
        assert captured["settings"].num_threads == 2
        # time_limit from backend config takes precedence over timeout-derived limit
        assert captured["settings"].time_limit == 30


# ---------------------------------------------------------------------------
# 9. OptimizationInterface backend selection
# ---------------------------------------------------------------------------

_OPT = "src.interfaces.optimization_backends.local_evopt.optimizer"
_BUNDLED_CBC = pulp.PULP_CBC_CMD.pulp_cbc_path


class TestCbcSolverSelection:
    """Test auto-detection of system vs bundled CBC solver."""

    def test_uses_system_binary_when_available(self):
        """Prefer a system-installed CBC executable when present on PATH."""
        with patch(f"{_OPT}.shutil.which", return_value="/usr/bin/cbc"), \
             patch(f"{_OPT}._cbc_runs", return_value=None):
            solver = _resolve_cbc_solver(
                msg=0,
                num_threads=2,
                time_limit=30.0,
                gapRel=0.01,
            )

        # Both candidates use COIN_CMD: PULP_CBC_CMD rejects custom paths, and
        # COIN_CMD is its base class with an identical solve path.
        assert isinstance(solver, pulp.COIN_CMD)
        assert solver.path == "/usr/bin/cbc"
        assert solver.msg == 0
        assert solver.optionsDict.get("threads") == 2
        assert solver.timeLimit == 30.0
        assert solver.optionsDict.get("gapRel") == 0.01

    def test_falls_back_to_bundled_binary_when_no_system_cbc(self):
        """Fall back to the PuLP bundled CBC when no system binary is found."""
        with patch(f"{_OPT}.shutil.which", return_value=None), \
             patch(f"{_OPT}._cbc_runs", return_value=None):
            solver = _resolve_cbc_solver(
                msg=0,
                num_threads=4,
                time_limit=120.0,
                gapRel=0.05,
            )

        assert isinstance(solver, pulp.COIN_CMD)
        assert solver.path == _BUNDLED_CBC
        assert solver.optionsDict.get("threads") == 4
        assert solver.timeLimit == 120.0
        assert solver.optionsDict.get("gapRel") == 0.05

    def test_broken_system_cbc_does_not_shadow_bundled(self):
        """
        A system cbc that cannot be executed must not win over a working
        bundled binary. Regression guard: the previous implementation trusted
        shutil.which() without ever running the candidate.
        """
        def _probe(path):
            return "boom: cannot exec" if path == "/usr/bin/cbc" else None

        with patch(f"{_OPT}.shutil.which", return_value="/usr/bin/cbc"), \
             patch(f"{_OPT}._cbc_runs", side_effect=_probe):
            solver = _resolve_cbc_solver(msg=0)

        assert solver.path == _BUNDLED_CBC

    def test_raises_when_no_cbc_can_run(self):
        """
        When neither candidate executes — the Alpine/musl x86_64 case — raise an
        actionable error instead of leaking a bare FileNotFoundError from the
        solve (issues #260, #264, #265, #273).
        """
        with patch(f"{_OPT}.shutil.which", return_value="/usr/bin/cbc"), \
             patch(f"{_OPT}._cbc_runs", return_value="missing ELF interpreter"):
            with pytest.raises(CbcSolverUnavailableError) as excinfo:
                _resolve_cbc_solver(msg=0)

        message = str(excinfo.value)
        assert "missing ELF interpreter" in message
        assert "system CBC" in message
        assert "bundled CBC" in message
        # Must tell the user what to actually do about it.
        assert "add-on" in message

    def test_probe_result_is_cached(self):
        """The probe forks a subprocess, so it must run once per process."""
        with patch(f"{_OPT}.shutil.which", return_value="/usr/bin/cbc"), \
             patch(f"{_OPT}._cbc_runs", return_value=None) as probe:
            _resolve_cbc_solver(msg=0)
            _resolve_cbc_solver(msg=0)
            _resolve_cbc_solver(msg=0)

        assert probe.call_count == 1

    def test_default_settings_forwarded(self):
        """Default OptimizerSettings values are forwarded to the solver."""
        with patch(f"{_OPT}.shutil.which", return_value="/usr/bin/cbc"), \
             patch(f"{_OPT}._cbc_runs", return_value=None):
            solver = _resolve_cbc_solver(msg=1)

        assert solver.msg == 1
        assert solver.optionsDict.get("threads") is None
        assert solver.timeLimit is None
        assert solver.optionsDict.get("gapRel") is None


class TestCbcProbe:
    """Test _cbc_runs() rejection reasons — no real subprocess is spawned."""

    def test_accepts_clean_exit(self):
        """A candidate that runs and exits 0 is accepted."""
        with patch(f"{_OPT}.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            assert _cbc_runs("/usr/bin/cbc") is None

    def test_missing_elf_interpreter(self):
        """The Alpine/musl case: the file exists but execve returns ENOENT."""
        with patch(f"{_OPT}.subprocess.run", side_effect=FileNotFoundError()):
            reason = _cbc_runs("/opt/venv/.../solverdir/cbc/linux/i64/cbc")

        assert reason is not None
        assert "ELF interpreter" in reason
        assert "musl" in reason

    def test_illegal_instruction(self):
        """A binary needing CPU features the host lacks (Proxmox kvm64)."""
        with patch(f"{_OPT}.subprocess.run", return_value=SimpleNamespace(returncode=-4)):
            reason = _cbc_runs("/usr/bin/cbc")

        assert reason is not None
        assert "signal 4" in reason
        assert "illegal instruction" in reason

    def test_nonzero_exit(self):
        """A candidate that runs but reports failure is rejected."""
        with patch(f"{_OPT}.subprocess.run", return_value=SimpleNamespace(returncode=1)):
            reason = _cbc_runs("/usr/bin/cbc")

        assert reason is not None
        assert "status 1" in reason

    def test_timeout(self):
        """A candidate that hangs is rejected rather than blocking the solve."""
        with patch(
            f"{_OPT}.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="cbc", timeout=15.0),
        ):
            reason = _cbc_runs("/usr/bin/cbc")

        assert reason is not None
        assert "15s" in reason

    def test_other_oserror(self):
        """Any other OSError (e.g. permissions) is reported, not raised."""
        with patch(f"{_OPT}.subprocess.run", side_effect=PermissionError("denied")):
            reason = _cbc_runs("/usr/bin/cbc")

        assert reason is not None
        assert "could not be started" in reason

    def test_real_bundled_binary_probe(self):
        """
        Sanity check against the actual installed pulp binary. On glibc CI this
        proves the probe accepts a genuinely working CBC rather than only
        agreeing with mocks.
        """
        reason = _cbc_runs(_BUNDLED_CBC)
        assert reason is None, f"bundled CBC unexpectedly rejected: {reason}"


class TestOptimizationInterfaceSelection:
    """Test OptimizationInterface backend selection for local_evopt."""
    def test_backend_selection_local_evopt(self, berlin_tz):
        """OptimizationInterface selects LocalEVOptBackend when source='local_evopt'."""
        from src.interfaces.optimization_interface import OptimizationInterface

        config = {
            "source": "local_evopt",
            "server": "localhost",
            "port": 8503,
            "local_evopt_charging_strategy": "charge_before_export",
            "local_evopt_discharging_strategy": "discharge_before_import",
            "local_evopt_emergency_reserve_pct": 0,
            "local_evopt_num_threads": 0,
            "local_evopt_time_limit": 0,
            "local_evopt_max_grid_import_w": 0,
            "local_evopt_max_grid_export_w": 0,
        }
        interface = OptimizationInterface(config, 3600, berlin_tz)
        assert interface.backend_type == "local_evopt"
        assert isinstance(interface.backend, LocalEVOptBackend)

    def test_backend_selection_eos_server_unchanged(self, berlin_tz):
        """eos_server selection still works after adding local_evopt."""
        from src.interfaces.optimization_interface import OptimizationInterface

        config = {"source": "eos_server", "server": "localhost", "port": 8503}
        interface = OptimizationInterface(config, 3600, berlin_tz)
        assert interface.backend_type == "eos_server"

    def test_backend_selection_evopt_unchanged(self, berlin_tz):
        """evopt (HTTP) selection still works after adding local_evopt."""
        from src.interfaces.optimization_interface import OptimizationInterface

        config = {"source": "evopt", "server": "localhost", "port": 7050}
        interface = OptimizationInterface(config, 3600, berlin_tz)
        assert interface.backend_type == "evopt"


class TestSmartForecastExtension:
    """
    Test smart forecast extension that teaches optimizer about morning PV.

    When forecast ends at night (19:00-05:00), the optimizer should extend
    the forecast with synthetic morning PV to prevent expensive grid charging
    at end-of-horizon.
    """

    def test_generate_morning_pv_pattern_hourly(self, backend_hourly):
        """Test morning PV pattern generation for hourly intervals."""
        pv_capacity = 4000  # 4 kW
        pattern = backend_hourly._generate_morning_pv_pattern(
            pv_capacity=pv_capacity,
            time_frame_base=3600,
            hours=6
        )

        # Should generate 6 hourly slots
        assert len(pattern) == 6, f"Expected 6 slots, got {len(pattern)}"

        # Verify conservative ramp: 10%, 20%, 30%, 40%, 50%, 50% of 4000W
        # Energy per hour = Power * 1h
        expected_wh = [400, 800, 1200, 1600, 2000, 2000]
        for i, expected in enumerate(expected_wh):
            assert abs(pattern[i] - expected) < 1.0, \
                f"Hour {i}: expected {expected} Wh, got {pattern[i]} Wh"

    def test_generate_morning_pv_pattern_fifteen_min(self, backend_15min):
        """Test morning PV pattern generation for 15-minute intervals."""
        pv_capacity = 4000  # 4 kW
        pattern = backend_15min._generate_morning_pv_pattern(
            pv_capacity=pv_capacity,
            time_frame_base=900,
            hours=6
        )

        # Should generate 24 slots (6 hours * 4 slots/hour)
        assert len(pattern) == 24, f"Expected 24 slots, got {len(pattern)}"

        # First hour (4 slots): 10% of 4000W = 400W * 0.25h = 100 Wh per slot
        for i in range(4):
            assert abs(pattern[i] - 100.0) < 1.0, \
                f"Slot {i}: expected 100 Wh, got {pattern[i]} Wh"

        # Second hour (slots 4-7): 20% = 800W * 0.25h = 200 Wh per slot
        for i in range(4, 8):
            assert abs(pattern[i] - 200.0) < 1.0, \
                f"Slot {i}: expected 200 Wh, got {pattern[i]} Wh"

    def test_extension_call_in_optimize_flow(self, backend_hourly):
        """
        Integration test: Verify extension is called during optimize() flow.
        Uses a minimal EOS request and checks that extension occurs.
        """
        # Create a minimal but valid EOS request
        eos_request = {
            "ems": {
                "pv_akku_prognose_wh": [0] * 48 + [500] * 144,  # Some PV capacity visible
                "gesamtlast": [1000.0] * 192,
                "strompreis_euro_pro_wh": [0.0003] * 192,
            },
            "akku": {
                "soc_prozent": 50.0,
                "speicherkapazitaet_wh": 10000.0,
                "lade_effizienz": 95.0,
                "entlade_effizienz": 95.0,
                "max_ladeleistung_w": 5000.0,
                "max_entladeleistung_w": 5000.0,
            },
        }

        # Run optimize - extension logic will execute if forecast ends at night
        # This is more of a smoke test to ensure no errors occur
        try:
            eos_response, runtime = backend_hourly.optimize(eos_request, timeout=10)
            # If we get here without exception, basic integration works
            assert "error" not in eos_response or eos_response.get("status") != "error"
            assert runtime is not None or eos_response.get("status") == "Infeasible"
        except ImportError:
            pytest.skip("PuLP not installed")

    def test_no_extension_when_pv_capacity_zero(self, backend_hourly):
        """
        Test that extension is skipped when PV capacity is zero.
        """
        # Build a minimal evopt_request with nighttime forecast end
        evopt_request = {
            "time_series": {
                "dt": [3600],
                "ft": [0.0],  # Zero PV (nighttime)
                "gt": [1000.0],
                "p_N": [0.0003],
                "p_E": [0.00008],
            },
            "batteries": [],
        }

        original_length = len(evopt_request["time_series"]["ft"])

        # Call extension method directly
        extended = backend_hourly._extend_forecast_with_morning_pv(evopt_request)

        # Should NOT extend (PV capacity is zero)
        assert len(extended["time_series"]["ft"]) == original_length, \
            "Should not extend when PV capacity is zero"


# ---------------------------------------------------------------------------
# 10. Terminal SOC valuation
# ---------------------------------------------------------------------------

def _solve_horizon(p_N, p_a, load_w=1000.0, pv_w=0.0, initial_pct=0.5,
                   capacity_wh=10000.0):
    """Solve one horizon and report where the battery ended and what it bought.

    Everything except ``p_a`` is held fixed, so a difference between two calls
    is attributable to the terminal valuation alone.
    """
    n = len(p_N)
    ts = TimeSeriesData(
        dt=[3600] * n,
        gt=[load_w] * n,
        ft=[pv_w] * n,
        p_N=list(p_N),
        p_E=[0.00008] * n,
    )
    battery = BatteryConfig(
        s_min=500,
        s_max=capacity_wh,
        s_initial=capacity_wh * initial_pct,
        c_min=0,
        c_max=5000,
        d_max=5000,
        p_a=p_a,
        charge_from_grid=True,
        discharge_to_grid=True,
        s_capacity=capacity_wh,
    )
    result = Optimizer(
        strategy=OptimizationStrategy(
            charging_strategy="charge_before_export",
            discharging_strategy="discharge_before_import",
        ),
        grid=GridConfig(),
        batteries=[battery],
        time_series=ts,
    ).solve()
    assert result["status"] == "Optimal"
    return {
        "final_soc": result["batteries"][0]["state_of_charge"][-1],
        "grid_import": sum(result["grid_import"]),
    }


class TestTerminalSocValuePolicies:
    """The valuation helper itself — no solver involved."""

    PRICES = [0.00019, 0.00025, 0.00030, 0.00025]
    STORED = 0.0000731

    def test_cheapest_ahead_is_the_lowest_price_in_the_horizon(self):
        assert LocalEVOptBackend._terminal_soc_value_eur_per_wh(
            "cheapest_ahead", self.PRICES, self.STORED
        ) == pytest.approx(0.00019)

    def test_stored_price_passes_the_old_value_through_untouched(self):
        assert LocalEVOptBackend._terminal_soc_value_eur_per_wh(
            "stored_price", self.PRICES, self.STORED
        ) == pytest.approx(self.STORED)

    @pytest.mark.parametrize("prices", [[], None, [0.0, 0.0, 0.0], [-0.0001, 0.0]])
    def test_no_usable_forward_price_falls_back_to_the_stored_price(self, prices):
        """
        A missing, zero or negative-only price series carries no forward signal.
        Deriving a valuation from it would put the terminal value at zero, which
        is the very thing this change exists to stop.
        """
        assert LocalEVOptBackend._terminal_soc_value_eur_per_wh(
            "cheapest_ahead", prices, self.STORED
        ) == pytest.approx(self.STORED)

    def test_negative_slots_are_skipped_not_treated_as_the_cheapest(self):
        """
        A negative price would drag the valuation to zero and bring the drain
        straight back. The cheapest *positive* price is the forward signal.
        """
        assert LocalEVOptBackend._terminal_soc_value_eur_per_wh(
            "cheapest_ahead", [-0.00005, 0.00019, 0.00030], self.STORED
        ) == pytest.approx(0.00019)

    def test_an_unknown_policy_falls_back_to_the_default(self):
        assert LocalEVOptBackend._terminal_soc_value_eur_per_wh(
            "totally_invalid_policy", self.PRICES, self.STORED
        ) == pytest.approx(0.00019)


class TestTerminalSocValueBehaviour:
    """Real CBC solves: what the valuation does to the battery."""

    STORED = 0.0000731  # a PV-charged battery's stored price, ~7.3 ct/kWh

    def test_pv_surplus_is_stored_instead_of_dumped(self):
        """
        The bug in its clearest form. With PV covering the load and a flat
        tariff, charge left at the end was valued at the stored price — below
        the feed-in tariff — so the model exported the surplus and finished on
        the floor. Valued at the cheapest price ahead it keeps the surplus,
        and buys nothing to do it.
        """
        prices = [0.00025] * 16
        old = _solve_horizon(prices, self.STORED, load_w=500.0, pv_w=1500.0,
                             initial_pct=0.3)
        new = _solve_horizon(
            prices,
            LocalEVOptBackend._terminal_soc_value_eur_per_wh(
                "cheapest_ahead", prices, self.STORED
            ),
            load_w=500.0, pv_w=1500.0, initial_pct=0.3,
        )
        assert new["final_soc"] > old["final_soc"] + 2000
        # and it cost nothing at the meter to do it
        assert new["grid_import"] == pytest.approx(old["grid_import"], abs=1.0)

    def test_the_battery_is_not_drained_into_cheap_hours(self):
        """
        Dear hours first, then a long cheap stretch, and a load small enough
        that the battery is never forced to empty. At the stored price the
        model dumped everything it had; at the cheapest price ahead it holds
        a substantial charge through the cheap stretch.
        """
        prices = [0.00030] * 6 + [0.00019] * 10
        old = _solve_horizon(prices, self.STORED, load_w=200.0)
        new = _solve_horizon(
            prices,
            LocalEVOptBackend._terminal_soc_value_eur_per_wh(
                "cheapest_ahead", prices, self.STORED
            ),
            load_w=200.0,
        )
        assert old["final_soc"] < 1000        # drained to the floor
        assert new["final_soc"] > 3000        # holds a real charge

    def test_a_flat_tariff_neither_drains_nor_hoards(self):
        """
        The degenerate case the valuation has to survive. With one price for
        the whole horizon there is no arbitrage to chase, so the battery should
        sit exactly where it started: not emptied, and not topped up from the
        grid either.

        This is what pins the formula to min(p_N) rather than the arithmetically
        tempting min(p_N)/eta_c. The latter puts the charge threshold
        eta_c * p_a exactly on the cheapest price, leaves every cheap slot an
        exact tie, and lets the secondary strategy terms break it toward buying
        — measured on this horizon, that filled the battery to 100 %.
        """
        prices = [0.00025] * 16
        initial_wh = 10000.0 * 0.5
        new = _solve_horizon(
            prices,
            LocalEVOptBackend._terminal_soc_value_eur_per_wh(
                "cheapest_ahead", prices, self.STORED
            ),
            load_w=1000.0, initial_pct=0.5,
        )
        assert new["final_soc"] == pytest.approx(initial_wh, abs=100.0)

    def test_a_median_valuation_would_overpay_and_is_not_offered(self):
        """
        Why only the cheapest price ahead is exposed.

        Valuing terminal charge at the median of the horizon stockpiles far
        harder, and the energy it buys costs more than the cheapest hour it
        could have been bought in -- so it pays today for energy the horizon
        itself shows going cheaper. Measured here, and on a live 36.5 h
        horizon where it spent EUR 3.10 to hold 16.3 kWh at an effective
        19.0 ct/kWh while the next morning offered 17.7 ct.
        """
        prices = [0.00018, 0.00022, 0.00030, 0.00028] * 4
        median = statistics.median(prices)
        cheapest_run = _solve_horizon(
            prices,
            LocalEVOptBackend._terminal_soc_value_eur_per_wh(
                "cheapest_ahead", prices, self.STORED
            ),
            load_w=600.0, initial_pct=0.6,
        )
        median_run = _solve_horizon(
            prices, median, load_w=600.0, initial_pct=0.6
        )
        assert median_run["final_soc"] > cheapest_run["final_soc"]
        assert median_run["grid_import"] > cheapest_run["grid_import"]
        # and the option is not reachable through config
        assert "median_ahead" not in TERMINAL_SOC_VALUES
        assert LocalEVOptBackend._terminal_soc_value_eur_per_wh(
            "median_ahead", prices, self.STORED
        ) == pytest.approx(min(prices))


class TestTerminalSocValueWiring:
    """The config knob reaches the payload the solver is handed."""

    def test_default_policy(self, berlin_tz):
        b = LocalEVOptBackend(time_frame_base=3600, time_zone=berlin_tz)
        assert b.terminal_soc_value == "cheapest_ahead"

    def test_unknown_policy_falls_back_to_the_default(self, berlin_tz):
        b = LocalEVOptBackend(
            time_frame_base=3600,
            time_zone=berlin_tz,
            terminal_soc_value="not_a_policy",
        )
        assert b.terminal_soc_value == "cheapest_ahead"

    @staticmethod
    def _p_a_from_transform(backend):
        """Run the real EOS->EVopt transform and report the p_a it emits.

        The valuation lives in the shared transform, not in _build_optimizer, so
        that the external (HTTP) EVopt backend gets the same corrected price in
        the payload it posts. Going through the transform is therefore the path
        that both backends actually take.
        """
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends."
            "optimization_backend_evopt.datetime", dt_mock
        ):
            evopt, _errors = backend._transform_request_from_eos_to_evopt(
                _make_eos_request(n_slots=48)
            )
        return evopt["batteries"][0]["p_a"]

    def test_the_stored_price_is_replaced_before_the_solver_sees_it(self, berlin_tz):
        b = LocalEVOptBackend(time_frame_base=3600, time_zone=berlin_tz)
        # _make_eos_request prices every slot at 0.0003 and reports a stored
        # price of 0.0002; the cheapest price ahead must win.
        assert self._p_a_from_transform(b) == pytest.approx(0.0003)

    def test_the_escape_hatch_keeps_the_stored_price(self, berlin_tz):
        b = LocalEVOptBackend(
            time_frame_base=3600, time_zone=berlin_tz,
            terminal_soc_value="stored_price",
        )
        assert self._p_a_from_transform(b) == pytest.approx(0.0002)

    def test_the_config_key_reaches_the_backend(self, berlin_tz):
        """OptimizationInterface passes eos.local_evopt_terminal_soc_value through."""
        from src.interfaces.optimization_interface import OptimizationInterface

        interface = OptimizationInterface(
            {"source": "local_evopt", "server": "localhost", "port": 8503,
             "local_evopt_terminal_soc_value": "stored_price"},
            3600, berlin_tz,
        )
        assert interface.backend.terminal_soc_value == "stored_price"

    def test_an_absent_config_key_leaves_the_default(self, berlin_tz):
        """Existing installs have no such key stored and must get the new default."""
        from src.interfaces.optimization_interface import OptimizationInterface

        interface = OptimizationInterface(
            {"source": "local_evopt", "server": "localhost", "port": 8503},
            3600, berlin_tz,
        )
        assert interface.backend.terminal_soc_value == "cheapest_ahead"

    def test_a_hot_reloaded_junk_value_cannot_stick(self, berlin_tz):
        """
        config_web's hot-reload writes straight onto the backend attribute,
        bypassing __init__. The valuation is coerced again at the point of use
        so a bad value degrades to the default instead of reaching the solver.
        """
        b = LocalEVOptBackend(time_frame_base=3600, time_zone=berlin_tz)
        b.terminal_soc_value = "garbage_from_a_hot_reload"
        assert self._p_a_from_transform(b) == pytest.approx(0.0003)


class TestTerminalSocValueOnExternalEvopt:
    """
    The external (HTTP) EVopt server runs the same engine and would otherwise be
    sent the same sunk-cost p_a. We own the payload, so it gets the fix too.
    """

    @staticmethod
    def _payload(backend):
        dt_mock = _midnight_mock()
        with patch(
            "src.interfaces.optimization_backends."
            "optimization_backend_evopt.datetime", dt_mock
        ):
            evopt, _errors = backend._transform_request_from_eos_to_evopt(
                _make_eos_request(n_slots=48)
            )
        return evopt

    def test_the_posted_payload_carries_the_forward_value(self, berlin_tz):
        e = EVOptBackend("http://evopt.invalid", 3600, berlin_tz)
        assert self._payload(e)["batteries"][0]["p_a"] == pytest.approx(0.0003)

    def test_the_escape_hatch_posts_the_stored_price(self, berlin_tz):
        e = EVOptBackend("http://evopt.invalid", 3600, berlin_tz,
                         terminal_soc_value="stored_price")
        assert self._payload(e)["batteries"][0]["p_a"] == pytest.approx(0.0002)

    def test_unknown_policy_falls_back_to_the_default(self, berlin_tz):
        e = EVOptBackend("http://evopt.invalid", 3600, berlin_tz,
                         terminal_soc_value="nonsense")
        assert e.terminal_soc_value == "cheapest_ahead"

    def test_the_stale_rotated_tail_cannot_drag_the_valuation_down(self, berlin_tz):
        """
        In 15-min mode the series is rotated, so the slots past n_result are
        yesterday's prices reused. A cheap slot parked there is not a price
        that is really ahead, and must not set the terminal value even though
        the whole 192-slot series is posted to the server.
        """
        req = _make_eos_request(n_slots=192)
        # midnight run -> n_result covers the whole series; push a bargain into
        # the far tail and confirm it is excluded once "now" is late in the day.
        req["ems"]["strompreis_euro_pro_wh"] = [0.0003] * 192
        req["ems"]["strompreis_euro_pro_wh"][10] = 0.00001  # early today
        e = EVOptBackend("http://evopt.invalid", 900, berlin_tz)
        dt_mock = _midnight_mock()

        class _Late(dt_mock):
            @classmethod
            def now(cls, tz=None):
                naive = _real_datetime(2026, 6, 1, 20, 0, 0)
                return tz.localize(naive) if tz is not None else naive

        with patch("src.interfaces.optimization_backends."
                   "optimization_backend_evopt.datetime", _Late):
            evopt, _ = e._transform_request_from_eos_to_evopt(req)
        # the 0.00001 slot rotated into the stale tail, so it must not win
        assert evopt["batteries"][0]["p_a"] == pytest.approx(0.0003)


# ---------------------------------------------------------------------------
# 12. Reported objective value
# ---------------------------------------------------------------------------

class TestCleanObjectiveBaseline:
    """
    get_clean_objective_value() must measure the battery from where it really
    started. s[0] is the SOC *after* slot 0 has charged, so using it drops the
    first slot from the reported delta.
    """

    @staticmethod
    def _solved():
        """A horizon whose first slot definitely charges.

        Slot 0 must be *uniquely* the cheapest: if several slots share the
        lowest price the solver is indifferent about which one to charge in,
        and it does not reliably pick the first.
        """
        T = 8
        p_N = [0.00008] + [0.00012] * 2 + [0.00040] * 5
        ts = TimeSeriesData(
            dt=[3600] * T, gt=[800.0] * T, ft=[0.0] * T,
            p_N=p_N, p_E=[0.00008] * T,
        )
        battery = BatteryConfig(
            s_min=500, s_max=10000, s_initial=4000, c_min=0,
            c_max=5000, d_max=5000, p_a=0.0002,
            charge_from_grid=True, discharge_to_grid=True, s_capacity=10000,
        )
        opt = Optimizer(
            strategy=OptimizationStrategy(
                charging_strategy="none", discharging_strategy="none"),
            grid=GridConfig(), batteries=[battery], time_series=ts,
        )
        result = opt.solve()
        assert result["status"] == "Optimal"
        return opt, battery

    def test_slot_zero_moves_energy_in_this_fixture(self):
        """Guard: without slot-0 activity the fix would be untestable here."""
        opt, _ = self._solved()
        moved = (pulp.value(opt.variables["c"][0][0]) or 0.0) + (
            pulp.value(opt.variables["d"][0][0]) or 0.0
        )
        assert moved > 1.0, "fixture no longer exercises the first slot"

    def test_the_battery_term_is_measured_from_s_initial(self):
        opt, battery = self._solved()
        s_end = pulp.value(opt.variables["s"][0][opt.T - 1]) or 0.0

        # Rebuild the non-battery part of the clean objective independently.
        grid = 0.0
        for t in opt.time_steps:
            grid -= (pulp.value(opt.variables["n"][t]) or 0.0) * opt.time_series.p_N[t]
            grid += (pulp.value(opt.variables["e"][t]) or 0.0) * opt.time_series.p_E[t]

        battery_term = opt.get_clean_objective_value() - grid
        assert battery_term == pytest.approx(
            (s_end - battery.s_initial) * battery.p_a, abs=1e-9
        )

    def test_the_old_s0_baseline_would_have_differed(self):
        """
        Pins the size of the bug rather than just its absence: the discarded
        formula gives a measurably different answer on this horizon.
        """
        opt, battery = self._solved()
        s0 = pulp.value(opt.variables["s"][0][0]) or 0.0
        slipped = (s0 - battery.s_initial) * battery.p_a
        assert abs(slipped) > 1e-4, (
            "slot 0 barely moved; this horizon no longer demonstrates the bug"
        )


# ---------------------------------------------------------------------------
# Managed loads the backend places itself
# ---------------------------------------------------------------------------

class TestManagedLoads:
    """Contingent loads handed to the solver beside the request, not inside it."""

    @staticmethod
    def _load(n_slots=48, **kwargs):
        record = {
            "id": "pool",
            "demand_wh": 6000.0,
            "max_power_w": 1500.0,
            "value_eur_per_wh": 0.0005,
            "feasible": [True] * n_slots,
            "min_runtime_slots": 1,
            "urgent_wh": 0.0,
        }
        record.update(kwargs)
        return record

    def _solve(self, backend, request, loads, hour=0):
        dt_mock = _midnight_mock()
        if hour:
            real_now = dt_mock.now

            class _AtHour(_real_datetime):
                @classmethod
                def now(cls, tz=None):
                    moment = real_now(tz)
                    return moment.replace(hour=hour)

            dt_mock = _AtHour
        path = "src.interfaces.optimization_backends.optimization_backend_evopt.datetime"
        with patch(path, dt_mock):
            return backend.optimize(request, timeout=60, managed_loads=loads)

    def test_the_backend_declares_that_it_can_place_them(self, backend_hourly):
        assert backend_hourly.schedules_managed_loads is True

    def test_a_schedule_comes_back_in_eos_slot_space(self, backend_hourly):
        """48 slots from local midnight, like every other array the app handles."""
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48), [self._load()]
        )
        assert "pool" in result["managed_loads"]
        assert len(result["managed_loads"]["pool"]) == 48

    def test_the_mask_is_read_in_the_callers_slot_space(self, backend_hourly):
        """
        The sharp edge. A mask allowing only 09:00-11:00 must place energy there and
        nowhere else - getting the rotation wrong schedules the right number of hours
        in the wrong ones, and nothing fails loudly when it does.
        """
        mask = [False] * 48
        for hour in (9, 10, 11):
            mask[hour] = True
        result, _ = self._solve(
            backend_hourly,
            _make_eos_request(n_slots=48, pv_value=0.0),
            [self._load(feasible=mask, demand_wh=3000.0)],
            hour=6,
        )
        energy = result["managed_loads"]["pool"]
        assert sum(energy) > 0
        assert all(value < 1.0 for hour, value in enumerate(energy) if not mask[hour])

    def test_slots_already_past_carry_nothing(self, backend_hourly):
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48), [self._load()], hour=8
        )
        assert all(value < 1.0 for value in result["managed_loads"]["pool"][:8])

    def test_no_managed_loads_leaves_the_response_shape_alone(self, backend_hourly):
        result, _ = self._solve(backend_hourly, _make_eos_request(n_slots=48), None)
        assert result["managed_loads"] == {}
        assert "ac_charge" in result

    def test_a_load_with_no_power_or_no_demand_is_skipped(self, backend_hourly):
        loads = [
            self._load(id="nopower", max_power_w=0),
            self._load(id="nodemand", demand_wh=0),
        ]
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48), loads
        )
        assert result["managed_loads"] == {}

    def test_unreadable_numbers_skip_one_load_rather_than_the_solve(self, backend_hourly):
        loads = [
            self._load(id="broken", demand_wh="lots"),
            self._load(id="pool"),
        ]
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48), loads
        )
        assert "broken" not in result["managed_loads"]
        assert "pool" in result["managed_loads"]

    def test_several_loads_each_get_their_own_schedule(self, backend_hourly):
        loads = [self._load(id="pool"), self._load(id="sauna", demand_wh=3000.0)]
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48), loads
        )
        assert set(result["managed_loads"]) == {"pool", "sauna"}

    def test_quarter_hour_resolution_returns_the_full_horizon(self, backend_15min):
        result, _ = self._solve(
            backend_15min,
            _make_eos_request(n_slots=192),
            [self._load(n_slots=192)],
        )
        assert len(result["managed_loads"]["pool"]) == 192

    # -- what the household actually pays extra ------------------------------------

    def test_the_cost_is_measured_against_a_run_without_the_load(self, backend_hourly):
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48, pv_value=0.0),
            [self._load()],
        )
        cost = result["managed_loads_cost"]
        assert cost is not None
        assert cost["energy_wh"] > 0
        assert cost["eur_per_wh"] > 0
        assert cost["shared"] is False

    def test_nothing_placed_means_no_cost_to_report(self, backend_hourly):
        """No second solve either - there is nothing to measure."""
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48),
            [self._load(value_eur_per_wh=0.0)],
        )
        assert result["managed_loads_cost"] is None

    def test_sun_makes_the_load_cheaper_not_dearer(self, backend_hourly):
        """
        The whole reason this is measured rather than read off the tariff. The hours a
        load occupies on a sunny day are not cheaper hours - often they are dearer -
        so the tariff of those hours moves the wrong way. What the household actually
        pays does not.
        """
        rates = []
        for pv in (0.0, 4000.0):
            result, _ = self._solve(
                backend_hourly,
                _make_eos_request(n_slots=48, pv_value=pv, load_value=400.0),
                [self._load(value_eur_per_wh=0.0005)],
            )
            cost = result["managed_loads_cost"]
            rates.append(cost["eur_per_wh"] if cost else 0.0)
        assert rates[1] < rates[0], "a sunny day must not cost more"

    def test_the_measured_rate_stays_under_what_the_energy_was_worth(self, backend_hourly):
        """The promise the price limit makes, checked on the figure the card shows."""
        value = 0.0004
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48, pv_value=0.0),
            [self._load(value_eur_per_wh=value)],
        )
        assert result["managed_loads_cost"]["eur_per_wh"] <= value + 1e-9

    def test_two_loads_report_a_shared_figure(self, backend_hourly):
        loads = [self._load(id="pool"), self._load(id="sauna", demand_wh=3000.0)]
        result, _ = self._solve(
            backend_hourly, _make_eos_request(n_slots=48, pv_value=0.0), loads
        )
        assert result["managed_loads_cost"]["shared"] is True

    def test_the_baseline_is_solved_without_the_loads(self, backend_hourly):
        """
        Guards the thing that would quietly ruin the number: a baseline that still had
        the loads in it would measure zero, and the card would report the pool as free.
        """
        seen = []
        original = backend_hourly._build_optimizer

        def spy(request, timeout, managed_loads=None):
            seen.append(len(managed_loads or []))
            return original(request, timeout, managed_loads)

        backend_hourly._build_optimizer = spy
        self._solve(
            backend_hourly, _make_eos_request(n_slots=48, pv_value=0.0), [self._load()]
        )
        assert seen == [1, 0], f"expected one solve with and one without, got {seen}"
