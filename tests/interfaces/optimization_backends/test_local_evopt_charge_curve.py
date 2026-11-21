"""
The battery's charging curve in the built-in optimizer's plan (#355).

EOS and EVopt take one charge limit for the whole horizon, and both use it as the
discharge limit too. Sending them the curve's value at a nearly full battery capped
the planned discharge at a few hundred watts, so evening peaks were bought from the
grid. They now get the fixed limit; ``local_evopt`` instead plans the curve slot by
slot. The MILP cannot evaluate the curve itself - the SOC is one of its unknowns - so
the backend applies it around the solver: each run gets per-slot limits from the
curve at the SOC the previous run planned.
"""

from datetime import datetime as _real_datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import pytz

from src.interfaces.optimization_backends import (
    optimization_backend_local_evopt as backend_mod,
)
from src.interfaces.optimization_backends.local_evopt.optimizer import (
    BatteryConfig,
    GridConfig,
    OptimizationStrategy,
    Optimizer,
    TimeSeriesData,
)
from src.interfaces.optimization_backends.optimization_backend_local_evopt import (
    LocalEVOptBackend,
)
from src.interfaces.optimization_interface import OptimizationInterface

DATETIME_PATH = (
    "src.interfaces.optimization_backends.optimization_backend_evopt.datetime"
)


def _midnight_mock():
    """A datetime whose now() is local midnight, so slot 0 is the first of the day."""

    class _MockDT(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            naive = _real_datetime(2026, 6, 1, 0, 0, 0)
            return tz.localize(naive) if tz is not None else naive

    return _MockDT


def _knee_curve(soc):
    """A curve with a hard knee: full power up to 50 %, 1 kW above."""
    return 5000.0 if soc <= 50 else 1000.0


def _request(capacity_wh=10000, initial_soc=20, load=None, price=None, pv=None):
    n = 48
    return {
        "ems": {
            "pv_prognose_wh": pv or [0.0] * n,
            "strompreis_euro_pro_wh": price or [0.0003] * n,
            "einspeiseverguetung_euro_pro_wh": [0.00008] * n,
            "gesamtlast": load or [300.0] * n,
            "preis_euro_pro_wh_akku": 0.0002,
        },
        "pv_akku": {
            "device_id": "battery1",
            "capacity_wh": capacity_wh,
            "charging_efficiency": 0.95,
            "discharging_efficiency": 0.95,
            "max_charge_power_w": 5000,
            "initial_soc_percentage": initial_soc,
            "min_soc_percentage": 5,
            "max_soc_percentage": 100,
        },
    }


@pytest.fixture(name="backend")
def fixture_backend():
    return LocalEVOptBackend(
        time_frame_base=3600, time_zone=pytz.timezone("Europe/Berlin")
    )


def _solve(backend, eos_request, curve):
    """Run the curve loop on the request the backend itself would build."""
    with patch(DATETIME_PATH, _midnight_mock()):
        evopt_request, errors = backend._transform_request_from_eos_to_evopt(
            eos_request
        )
    assert not errors
    return backend._solve_with_charge_curve(
        evopt_request, eos_request, 60, None, 0.0, curve
    )


# ---------------------------------------------------------------------------
# The optimizer takes a charge limit per slot
# ---------------------------------------------------------------------------


def test_a_per_slot_limit_caps_charging_in_that_slot():
    """Cheap grid everywhere and an empty battery: charging runs up to each slot's limit."""
    limits = [4000.0, 1000.0, 2500.0, 500.0]
    battery = BatteryConfig(
        s_min=0,
        s_max=10000,
        s_initial=0,
        c_min=0,
        c_max=5000,
        d_max=5000,
        p_a=0.001,
        charge_from_grid=True,
        discharge_to_grid=False,
        s_capacity=10000,
        c_max_series=limits,
    )
    ts = TimeSeriesData(
        dt=[3600] * 4, gt=[0.0] * 4, ft=[0.0] * 4, p_N=[0.0001] * 4, p_E=[0.0] * 4
    )
    result = Optimizer(
        strategy=OptimizationStrategy(
            charging_strategy="none", discharging_strategy="none"
        ),
        grid=GridConfig(),
        batteries=[battery],
        time_series=ts,
    ).solve()

    assert result["status"] == "Optimal"
    charged = result["batteries"][0]["charging_power"]
    for t, limit in enumerate(limits):
        assert charged[t] <= limit + 1e-6
    assert charged[0] == pytest.approx(4000.0, abs=1.0), "the limit, not c_max, binds"


def test_a_per_slot_limit_never_exceeds_c_max():
    battery = BatteryConfig(c_max=3000, c_max_series=[9000.0, -5.0])
    assert battery.c_max_at(0) == 3000
    assert battery.c_max_at(1) == 0.0
    assert battery.c_max_at(7) == 3000, "beyond the series: c_max"


# ---------------------------------------------------------------------------
# Limits from the planned SOC
# ---------------------------------------------------------------------------


def test_each_slot_takes_the_curve_at_the_soc_it_starts_with():
    response = {"batteries": [{"state_of_charge": [4000.0, 6000.0, 8000.0]}]}
    caps = LocalEVOptBackend._charge_caps_from_plan(
        response,
        capacity_wh=10000,
        initial_soc=30,
        n_slots=3,
        charge_power_limit=_knee_curve,
    )
    # slot 0 starts at the current 30 %, slot 1 at 40 %, slot 2 at 60 %
    assert caps == [5000.0, 5000.0, 1000.0]


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _fake_backend(socs_by_run, statuses=None):
    """A backend whose solver returns a scripted SOC trajectory per run."""
    backend = LocalEVOptBackend.__new__(LocalEVOptBackend)
    seen = []

    def build(_req, _timeout, _loads, managed_load_budget_w=0.0, c_max_series=None):
        run = len(seen)
        seen.append(c_max_series)
        optimizer = MagicMock()
        optimizer.solve.return_value = {
            "status": (statuses or {}).get(run, "Optimal"),
            "batteries": [
                {"state_of_charge": socs_by_run[min(run, len(socs_by_run) - 1)]}
            ],
        }
        return optimizer

    backend._build_optimizer = build
    return backend, seen


EVOPT_REQ = {"time_series": {"dt": [3600] * 3}}
EOS_REQ = {"pv_akku": {"capacity_wh": 10000, "initial_soc_percentage": 30}}


def test_without_a_curve_there_is_one_run_with_the_fixed_limit():
    backend, seen = _fake_backend([[0, 0, 0]])
    backend._solve_with_charge_curve(EVOPT_REQ, EOS_REQ, 60, None, 0.0, None)
    assert seen == [None]


def test_the_first_run_uses_the_current_soc_then_the_curve_below_its_knee():
    backend, seen = _fake_backend([[4000.0, 4500.0, 4800.0]])
    backend._solve_with_charge_curve(EVOPT_REQ, EOS_REQ, 60, None, 0.0, _knee_curve)
    assert seen[0] == [5000.0, 5000.0, 5000.0]


def test_the_loop_stops_once_the_limits_stop_changing():
    # Run 1 plans up to 60 % by slot 1 -> slot 2 gets the 1 kW limit; run 2 plans the
    # same trajectory, so its limits equal the ones it was given.
    backend, seen = _fake_backend([[4000.0, 6000.0, 7000.0]])
    _, response = backend._solve_with_charge_curve(
        EVOPT_REQ, EOS_REQ, 60, None, 0.0, _knee_curve
    )
    assert seen == [[5000.0, 5000.0, 5000.0], [5000.0, 5000.0, 1000.0]]
    assert response["status"] == "Optimal"


def _sloped_curve(soc):
    return 10000.0 - 50.0 * soc


def test_the_loop_runs_at_most_the_configured_number_of_times(monkeypatch):
    # Every run plans a higher SOC than the last, so every run lowers the limits.
    monkeypatch.setattr(backend_mod, "CHARGE_CURVE_MAX_RUNS", 4)
    rising = [[3000.0 + 500 * k] * 3 for k in range(10)]
    backend, seen = _fake_backend(rising)
    backend._solve_with_charge_curve(EVOPT_REQ, EOS_REQ, 60, None, 0.0, _sloped_curve)
    assert len(seen) == 4


def test_a_limit_only_ever_comes_down():
    # Run 1 plans high, run 2 low: the low plan would allow more, but the limit stays.
    backend, seen = _fake_backend([[6000.0, 6000.0, 6000.0], [4000.0, 4000.0, 4000.0]])
    backend._solve_with_charge_curve(EVOPT_REQ, EOS_REQ, 60, None, 0.0, _knee_curve)
    assert seen == [[5000.0, 5000.0, 5000.0], [5000.0, 1000.0, 1000.0]]


def test_a_later_run_that_fails_keeps_the_earlier_plan():
    backend, seen = _fake_backend(
        [[4000.0, 6000.0, 7000.0]], statuses={1: "Infeasible"}
    )
    _, response = backend._solve_with_charge_curve(
        EVOPT_REQ, EOS_REQ, 60, None, 0.0, _knee_curve
    )
    assert len(seen) == 2
    assert response["status"] == "Optimal", "run 1's plan, not the failed run 2"


# ---------------------------------------------------------------------------
# End to end through the real solver
# ---------------------------------------------------------------------------


def test_the_plan_charges_no_faster_than_the_curve_allows(backend):
    """Cheap night, dear day: the battery charges from the grid, slower above 50 %."""
    price = [0.0001] * 6 + [0.0006] * 42
    eos_request = _request(initial_soc=20, price=price, load=[300.0] * 48)
    _, response = _solve(backend, eos_request, _knee_curve)

    assert response["status"] == "Optimal"
    battery = response["batteries"][0]
    starts = [20.0] + [s / 10000 * 100 for s in battery["state_of_charge"][:-1]]
    for t, (charged, soc) in enumerate(zip(battery["charging_power"], starts)):
        assert (
            charged <= _knee_curve(soc) + 1.0
        ), f"slot {t}: charged {charged:.0f} Wh starting at {soc:.1f} % SOC"
    assert max(battery["charging_power"]) > 1000.0, "below the knee it may charge fast"


def test_a_full_battery_still_covers_a_load_peak(backend):
    """The #355 case: 11 kWh at 92 %, the curve at 550 W, a 1263 Wh morning peak."""
    load = [200.0] * 48
    load[7] = 1263.0
    load[19] = 743.0
    price = [0.0002] * 48
    price[7] = price[19] = 0.0008  # the peaks are what the battery should cover

    def curve(soc):
        return 550.0 if soc > 90 else 5000.0

    eos_request = _request(capacity_wh=11000, initial_soc=92, load=load, price=price)
    _, response = _solve(backend, eos_request, curve)

    assert response["status"] == "Optimal"
    battery = response["batteries"][0]
    assert battery["discharging_power"][7] == pytest.approx(1263.0, abs=1.0)
    assert response["grid_import"][7] == pytest.approx(0.0, abs=1.0)

    # What the old request did: the curve's 550 W as the battery's only limit.
    capped = _request(capacity_wh=11000, initial_soc=92, load=load, price=price)
    capped["pv_akku"]["max_charge_power_w"] = 550
    _, before = _solve(backend, capped, None)
    assert before["batteries"][0]["discharging_power"][7] == pytest.approx(
        550.0, abs=1.0
    )
    assert before["grid_import"][7] == pytest.approx(713.0, abs=1.0)


# ---------------------------------------------------------------------------
# Through the facade the application calls
# ---------------------------------------------------------------------------


def _interface(backend):
    interface = OptimizationInterface.__new__(OptimizationInterface)
    interface.eos_source = "local_evopt"
    interface.time_frame_base = 3600
    interface.time_zone = ZoneInfo("Europe/Berlin")
    interface.timeout = 60
    interface.last_eos_request = None
    interface.backend = backend
    backend.optimize.return_value = ({"ac_charge": []}, 1.0)
    return interface


def test_the_curve_reaches_a_backend_that_plans_with_it():
    backend = MagicMock()
    backend.plans_charging_curve = True
    _interface(backend).optimize({"ems": {}}, charge_power_limit=_knee_curve)
    assert backend.optimize.call_args.kwargs["charge_power_limit"] is _knee_curve


def test_the_curve_stays_away_from_eos_and_evopt():
    backend = MagicMock()
    del backend.plans_charging_curve  # as EOSBackend and EVOptBackend are
    del backend.schedules_managed_loads
    _interface(backend).optimize({"ems": {}}, charge_power_limit=_knee_curve)
    assert "charge_power_limit" not in backend.optimize.call_args.kwargs


def test_the_built_in_backend_declares_that_it_plans_the_curve():
    assert LocalEVOptBackend.plans_charging_curve is True


def test_a_quarter_hourly_day_keeps_to_the_real_curve_in_every_slot():
    """192 slots, the battery's own curve, PV, peaks and a price swing."""
    import math  # pylint: disable=import-outside-toplevel

    from src.interfaces.battery_interface import (  # pylint: disable=import-outside-toplevel
        BatteryInterface,
    )

    config = {
        "source": "default",
        "url": "",
        "soc_sensor": "",
        "max_charge_power_w": 5000,
        "capacity_wh": 11000,
        "min_soc_percentage": 5,
        "max_soc_percentage": 100,
        "charging_curve_enabled": True,
        "discharge_efficiency": 0.95,
        "price_euro_per_wh_accu": 0.0,
        "price_euro_per_wh_sensor": "",
    }
    with patch.object(BatteryInterface, "start_update_service", return_value=None):
        battery_if = BatteryInterface(config)
    curve = battery_if.charge_curve_for_planning()

    n = 192
    hour = [(i % 96) / 4 for i in range(n)]
    pv = [
        max(0.0, 1500 * math.sin(math.pi * (h - 6) / 14)) if 6 <= h <= 20 else 0.0
        for h in hour
    ]
    load = [950.0 if int(h) in (7, 19) else 250.0 for h in hour]
    price = [
        0.00025 + 0.00015 * math.sin(2 * math.pi * (i % 96) / 96) for i in range(n)
    ]
    request = _request(capacity_wh=11000, initial_soc=60, load=load, price=price, pv=pv)

    backend = LocalEVOptBackend(
        time_frame_base=900, time_zone=pytz.timezone("Europe/Berlin")
    )
    _, response = _solve(backend, request, curve)

    assert response["status"] == "Optimal"
    battery = response["batteries"][0]
    starts = [60.0] + [s / 11000 * 100 for s in battery["state_of_charge"][:-1]]
    for t, (charged_wh, soc) in enumerate(zip(battery["charging_power"], starts)):
        assert charged_wh * 4 <= curve(soc) + 1.0, (
            f"slot {t}: {charged_wh * 4:.0f} W planned at {soc:.1f} % SOC, "
            f"curve allows {curve(soc):.0f} W"
        )


# ---------------------------------------------------------------------------
# The curve and the plan's memory
# ---------------------------------------------------------------------------


PLAN_START = _real_datetime(2026, 6, 1, 11, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def test_every_run_of_the_curve_is_held_against_the_previous_plan():
    """
    Plan stability applies per run, not per optimization.

    The curve builds a fresh optimizer for each run, and it is one of those runs
    whose plan goes out. A run left without the previous plan is free to flip, which
    is the whole thing plan stability exists to stop - and with a curve configured
    that would have been every run of every cycle.
    """
    # Run 1 plans past the knee, so run 2 gets lower limits: two optimizers built.
    backend, seen = _fake_backend([[4000.0, 6000.0, 7000.0]])
    attached = []
    backend._attach_previous_plan = lambda optimizer, start: attached.append(start)

    backend._solve_with_charge_curve(
        EVOPT_REQ, EOS_REQ, 60, None, 0.0, _knee_curve, PLAN_START
    )

    assert len(attached) == len(seen) == 2
    # Both lined up from the same instant, or the two runs would be held against
    # differently shifted copies of the same plan.
    assert attached == [PLAN_START, PLAN_START]


def test_a_solve_outside_the_chain_is_left_alone():
    """Without a plan start there is nothing to line up with, so nothing is attached."""
    backend, _ = _fake_backend([[4000.0, 6000.0, 7000.0]])
    attached = []
    backend._attach_previous_plan = lambda optimizer, start: attached.append(start)

    backend._solve_with_charge_curve(EVOPT_REQ, EOS_REQ, 60, None, 0.0, _knee_curve)

    assert attached == []
