"""
The whole path: a managed load reaching the solver and its schedule coming back.

Each layer was tested on its own and the feature was still dead on a live install,
because the layers were tested where they join something other than what they join in
the application. The manager talks to an `OptimizationInterface`, not to a backend, and
the capability flag and the extra argument both stopped there.

So this drives the three together - real manager, real interface, real solver - and
asserts the one thing that matters: the pool runs in the hours the solver chose.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("pulp")

from src.interfaces.optimization_interface import OptimizationInterface  # noqa: E402
from src.loads.manager import ManagedLoadManager, ManagedLoadSources  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 6, 1, 6, 0, tzinfo=BERLIN)
SLOTS = 48

CHEAP = 0.00010
DEAR = 0.00045

POOL = {
    "id": "pool", "type": "pool_heatpump", "enabled": True,
    "temp_sensor": "sensor.pool_water", "power_sensor": "sensor.pool_power",
    "target_temp": 28.0, "window_start": None, "window_end": None,
    "season_start": None, "season_end": None, "min_ambient_temp_c": None,
    "min_runtime_minutes": 0, "volume_m3": 18.0, "surface_m2": 13.75,
    "rated_power_w": 1500.0,
}


def _eos_request(prices):
    return {
        "ems": {
            "pv_prognose_wh": [0.0] * SLOTS,
            "strompreis_euro_pro_wh": list(prices),
            "einspeiseverguetung_euro_pro_wh": [0.00008] * SLOTS,
            "gesamtlast": [400.0] * SLOTS,
            "preis_euro_pro_wh_akku": CHEAP,
        },
        "pv_akku": {
            "device_id": "battery1", "capacity_wh": 10000,
            "charging_efficiency": 0.95, "discharging_efficiency": 0.95,
            "max_charge_power_w": 5000, "initial_soc_percentage": 50,
            "min_soc_percentage": 5, "max_soc_percentage": 100,
        },
        "eauto": None, "dishwasher": None, "temperature_forecast": None,
        "start_solution": None,
    }


def _clock():
    class _At(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    return _At


@pytest.fixture(name="wired")
def wired_fixture():
    """A manager and the interface the application would hand it, both real."""
    prices = [DEAR] * SLOTS
    # Ahead of NOW on purpose: the solver is handed the horizon that starts now, so
    # cheap hours behind it are not a bargain, they are gone.
    for hour in (10, 11, 12, 13):
        prices[hour] = CHEAP

    sources = ManagedLoadSources(
        read_sensor=lambda name: {
            "sensor.pool_water": 24.0, "sensor.pool_power": 0.0,
        }.get(name),
        price=lambda: prices,
        feed_in_price=lambda: [0.00008] * SLOTS,
        pv_forecast=lambda: [0.0] * SLOTS,
        base_load=lambda: [400.0] * SLOTS,
        temperature_forecast=lambda: [18.0] * SLOTS,
    )
    manager = ManagedLoadManager(
        [dict(POOL, max_price_ct_kwh=25.0)], time_frame_base=3600,
        time_zone=BERLIN, sources=sources, clock=lambda: NOW,
    )
    interface = OptimizationInterface(
        config={"source": "local_evopt", "timeout": 60},
        time_frame_base=3600, timezone=BERLIN,
    )
    manager.external_scheduler = interface.schedules_managed_loads
    return manager, interface, prices


def test_the_capability_survives_the_trip_through_the_interface(wired):
    """The bug: this was False on the live install, and everything else followed."""
    manager, interface, _ = wired
    assert interface.schedules_managed_loads is True
    assert manager.external_scheduler is True


def test_the_load_is_scheduled_and_the_gate_follows_it(wired):
    manager, interface, prices = wired
    manager.run_cycle()

    records = manager.schedulable()
    assert records, "the manager offered nothing to schedule"

    path = "src.interfaces.optimization_backends.optimization_backend_evopt.datetime"
    with patch(path, _clock()):
        response, _ = interface.optimize(_eos_request(prices), managed_loads=records)

    assert "pool" in response["managed_loads"]
    assert manager.adopt_schedules(response["managed_loads"]) == 1

    load = manager.instance("pool")
    assert sum(load.last_plan) > 0, "the solver placed nothing"
    assert load.last_release is not None


def test_it_is_placed_in_the_cheap_hours_the_solver_found(wired):
    manager, interface, prices = wired
    manager.run_cycle()

    path = "src.interfaces.optimization_backends.optimization_backend_evopt.datetime"
    with patch(path, _clock()):
        response, _ = interface.optimize(
            _eos_request(prices), managed_loads=manager.schedulable()
        )
    manager.adopt_schedules(response["managed_loads"])

    plan = manager.instance("pool").last_plan
    cheap = sum(plan[10:14])
    assert cheap > 0
    assert cheap >= 0.6 * sum(plan), "most of it should be in the cheap window"


def test_a_scheduled_load_is_not_also_in_the_household_forecast(wired):
    manager, _, _ = wired
    manager.run_cycle()
    base = [1000.0] * SLOTS
    assert manager.apply(base, 3600) == base


def test_nothing_reaches_an_external_backend(wired):
    """It would go on the wire to a server that validates what it is sent."""
    manager, _, _ = wired
    external = OptimizationInterface(
        config={"source": "eos_server", "timeout": 60, "server": "127.0.0.1",
                "port": 8503},
        time_frame_base=3600, timezone=BERLIN,
    )
    assert external.schedules_managed_loads is False
