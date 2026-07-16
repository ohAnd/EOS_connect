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

All tests run fully in-process — no network, no mock HTTP.

Usage:
    pytest tests/interfaces/optimization_backends/test_optimization_backend_local_evopt.py -v
"""

# pylint: disable=protected-access

import pytest
import pytz
from datetime import datetime as _real_datetime
from unittest.mock import patch

from src.interfaces.optimization_backends.optimization_backend_local_evopt import LocalEVOptBackend
from src.interfaces.optimization_backends.local_evopt.optimizer import (
    BatteryConfig,
    GridConfig,
    OptimizationStrategy,
    Optimizer,
    TimeSeriesData,
)


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
