"""
A house planned by its temperature: the store handed to the solver, and the gate that
follows it.

Planned by energy, a house gets an amount placed in the cheapest hours and says nothing
about when the rooms are warm. Planned by temperature, the model describes the store -
capacity, losses, COP and sun per slot, and a comfort band - and inside that band the
solver's plan decides, not the deadband.
"""

import pytest

from src.loads.presets import TYPE_SPACE_HEATING, apply_defaults

MEASURED_HOUSE = {
    "id": "house",
    "type": TYPE_SPACE_HEATING,
    "living_area_m2": 170,
    "construction": "massive",
    "emitter": "floor",
    "building_form": "detached",
    "insulation": "renovated",
}

HOUSE_ENTRY = dict(
    MEASURED_HOUSE,
    temp_sensor="sensor.rooms",
    power_sensor="sensor.hp_power",
)


def _house_manager(make_manager, installation, rooms=21.2, **overrides):
    installation.sensors.update({"sensor.rooms": rooms, "sensor.hp_power": 0.0})
    installation.temperature = [5.0] * 24 + [9.0] * 24
    manager = make_manager([dict(HOUSE_ENTRY, **overrides)])
    manager.external_scheduler = True
    manager.run_cycle()
    return manager


def test_a_house_is_planned_by_its_temperature_by_default():
    resolved = apply_defaults(MEASURED_HOUSE)
    assert resolved["planning_mode"] == "temperature"
    assert resolved["efficiency_source"] == "cop"


def test_the_store_carries_the_band_and_the_physics(make_manager, installation):
    manager = _house_manager(make_manager, installation)
    store = manager.instance("house").last_demand.store
    assert store["capacity_wh_per_k"] == pytest.approx(21250, rel=0.01)
    assert store["t_min"] == pytest.approx(21.0)
    assert store["t_target"] == pytest.approx(21.5)
    assert store["t_max"] == pytest.approx(22.5)
    assert store["t_initial"] == pytest.approx(21.2)
    assert len(store["cop"]) == len(store["loss_w_per_k"]) == 48
    # Colder slots, lower COP.
    assert store["cop"][0] < store["cop"][30]


def test_the_store_reaches_the_solver_even_without_a_demand(make_manager, installation):
    manager = _house_manager(make_manager, installation, rooms=22.4)
    records = manager.schedulable()
    assert records and records[0]["store"] is not None
    assert records[0]["urgent_wh"] == 0.0


def test_planned_by_energy_there_is_no_store(make_manager, installation):
    manager = _house_manager(make_manager, installation, planning_mode="energy")
    assert manager.instance("house").last_demand.store is None


def test_inside_the_deadband_a_solver_may_still_run_it(make_manager, installation):
    """Banking heat above 21.0 is the solver's call, not the deadband's."""
    manager = _house_manager(make_manager, installation, rooms=21.3)
    item = manager.instance("house")
    assert item.last_demand.detail["active"] is False
    assert item.last_demand.detail["storage_active"] is True
    plan = [0.0] * 48
    plan[6] = 900.0  # 06:00, the slot "now" falls in
    ctx = manager._last_ctx_for["house"]
    release = item.adopt_schedule(plan, ctx)
    assert release["released"] is True


def test_at_the_ceiling_it_stops_whatever_the_plan_says(make_manager, installation):
    manager = _house_manager(make_manager, installation, rooms=22.6)
    item = manager.instance("house")
    plan = [0.0] * 48
    plan[6] = 900.0
    release = item.adopt_schedule(plan, manager._last_ctx_for["house"])
    assert release["released"] is False


def test_without_a_solver_the_deadband_still_decides(make_manager, installation):
    installation.sensors.update({"sensor.rooms": 21.3, "sensor.hp_power": 0.0})
    manager = make_manager([HOUSE_ENTRY])
    manager.run_cycle()
    item = manager.instance("house")
    assert item.last_release["released"] is False


def test_the_projection_may_rise_above_target_when_planned_by_temperature(
    make_manager, installation
):
    manager = _house_manager(make_manager, installation, rooms=21.4)
    item = manager.instance("house")
    ctx = manager._last_ctx_for["house"]
    plan = [0.0] * 48
    for slot in range(6, 14):
        plan[slot] = 3000.0
    projected = item.model.project_medium(ctx, plan, 21.4)
    assert max(value for value in projected if value is not None) > 21.5


def test_the_record_makes_it_into_solver_space(make_manager, installation):
    from datetime import datetime
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from src.interfaces.optimization_backends.optimization_backend_local_evopt import (
        LocalEVOptBackend,
    )

    manager = _house_manager(make_manager, installation)
    record = manager.schedulable()[0]
    backend = LocalEVOptBackend.__new__(LocalEVOptBackend)
    backend.time_zone = ZoneInfo("Europe/Berlin")
    backend.time_frame_base = 3600
    moment = datetime(2026, 6, 1, 6, 30, tzinfo=backend.time_zone)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    with patch(
        "src.interfaces.optimization_backends.optimization_backend_evopt.datetime",
        _Clock,
    ):
        configs = backend._managed_load_configs([record])
    store = configs[0].store
    assert store is not None
    assert store.t_min == pytest.approx(21.0)
    # Rotated: solver slot 0 is 06:00.
    assert store.cop[0] == pytest.approx(record["store"]["cop"][6])


def test_an_unreadable_store_falls_back_to_energy():
    from src.interfaces.optimization_backends.optimization_backend_local_evopt import (
        LocalEVOptBackend,
    )

    backend = LocalEVOptBackend.__new__(LocalEVOptBackend)
    assert backend._solver_store({"capacity_wh_per_k": "lots"}) is None
    assert backend._solver_store(None) is None


def test_a_store_without_a_demand_waits_for_the_solver_it_was_offered_to(
    make_manager, installation
):
    """
    Offered and waited on under the same rule. A warm house wants nothing right now,
    yet it is handed to the solver, which may bank heat later. Judged as idle by the
    handover instead, it would settle its own gate as well - two planners for one heat
    pump, and no notice that the solver never answered.
    """
    installation.sensors.update({"sensor.rooms": 22.4, "sensor.hp_power": 0.0})
    installation.temperature = [24.0] * 48  # a warm spell: no losses to make up
    manager = make_manager([HOUSE_ENTRY])
    manager.external_scheduler = True
    manager.run_cycle()
    item = manager.instance("house")
    assert item.last_demand.total_wh == 0
    assert manager.schedulable(), "the store was not offered"
    assert item.waiting_for_schedule_since is not None, (
        "offered to the solver but not waiting for it"
    )
