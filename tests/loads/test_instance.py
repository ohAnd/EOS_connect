"""
One managed load end to end: sensors in, forecast and release signal out.

The property these tests exist to protect is that the two outputs agree. A plan that
says "runs at 13:00" while the gate releases at 16:00 would have the optimizer sizing the
battery around a load that never appears.
"""

import pytest

from src.loads.gate import REASON_PLANNED
from src.loads.injection import InjectionError
from src.loads.models.thermal import REASON_AT_TARGET, REASON_NO_TEMPERATURE
from src.loads.presets import TYPE_EXTERNAL_PROFILE, TYPE_POOL_HEATPUMP


POOL = {
    "id": "pool",
    "type": TYPE_POOL_HEATPUMP,
    "enabled": True,
    "temp_sensor": "sensor.pool_water",
    "power_sensor": "sensor.pool_power",
    "target_temp": 28.0,
    # The presets restrict a pool to daylight and to the swimming season; these tests
    # are about the pipeline, so the restrictions are widened and exercised separately.
    "window_start": None,
    "window_end": None,
    "season_start": None,
    "season_end": None,
    "min_ambient_temp_c": None,
    "min_runtime_minutes": 0,
}


def _run(manager, installation, water_c=24.0, power_w=0.0, **sensors):
    installation.sensors["sensor.pool_water"] = water_c
    installation.sensors["sensor.pool_power"] = power_w
    installation.sensors.update(sensors)
    manager.run_cycle()
    return manager.instance("pool")


def test_a_cold_pool_produces_demand_and_a_release(make_manager, installation):
    manager = make_manager([POOL])
    pool = _run(manager, installation, water_c=24.0)

    assert pool.last_demand.total_wh > 0
    assert sum(pool.last_plan) > 0
    assert pool.last_release["released"] is True
    assert pool.last_release["reason"] == REASON_PLANNED


def test_the_plan_and_the_release_agree_on_the_current_slot(make_manager, installation):
    """The whole point of deriving both from one plan."""
    manager = make_manager([POOL])
    pool = _run(manager, installation, water_c=24.0)

    ctx_slot = manager._context().current_slot  # pylint: disable=protected-access
    planned_now = pool.last_plan[ctx_slot] > 0
    assert planned_now == pool.last_release["released"]


def test_a_pool_at_target_is_not_released_but_still_forecasts_standing_losses(
    make_manager, installation
):
    """
    A satisfied pool must not run now, yet the optimizer is planning two days ahead and
    the pool will still lose heat tonight.
    """
    manager = make_manager([POOL])
    installation.temperature = [10.0] * 48  # cold air, so losses are real
    pool = _run(manager, installation, water_c=28.5)

    assert pool.last_release["released"] is False
    assert pool.last_demand.reason == REASON_AT_TARGET
    assert pool.last_demand.total_wh > 0
    assert sum(pool.last_plan) > 0


def test_the_current_slot_is_excluded_when_the_load_must_not_start(
    make_manager, installation
):
    manager = make_manager([POOL])
    installation.temperature = [10.0] * 48
    pool = _run(manager, installation, water_c=28.5)

    current = manager._context().current_slot  # pylint: disable=protected-access
    assert pool.last_plan[current] == 0.0


def test_the_deadband_stops_a_pump_hunting_around_the_setpoint(make_manager, installation):
    """Just below target and not running: do not start for the last tenth of a degree."""
    manager = make_manager([dict(POOL, deadband_k=1.0)])
    pool = _run(manager, installation, water_c=27.5, power_w=0.0)
    assert pool.last_release["released"] is False


def test_an_already_running_pump_finishes_the_last_tenth_of_a_degree(
    make_manager, installation
):
    manager = make_manager([dict(POOL, deadband_k=1.0)])
    pool = _run(manager, installation, water_c=27.5, power_w=2000.0)
    assert pool.last_release["released"] is True


def test_frost_protection_overrides_price(make_manager, installation):
    manager = make_manager([dict(POOL, frost_protection_temp_c=4.0)])
    installation.prices = [0.005] * 48  # absurdly expensive everywhere
    pool = _run(manager, installation, water_c=3.0)

    assert pool.last_demand.urgent is True
    assert pool.last_release["released"] is True


def test_a_missing_temperature_sensor_yields_no_demand_rather_than_an_error(
    make_manager, installation
):
    manager = make_manager([POOL])
    installation.sensors["sensor.pool_power"] = 0.0
    manager.run_cycle()
    pool = manager.instance("pool")

    assert pool.last_demand.reason == REASON_NO_TEMPERATURE
    assert pool.last_demand.total_wh == 0.0
    assert pool.last_error is None


def test_an_out_of_season_pool_is_never_released(make_manager, installation):
    manager = make_manager([dict(POOL, season_start="12-01", season_end="12-31")])
    pool = _run(manager, installation, water_c=20.0)
    assert pool.last_release["released"] is False
    assert sum(pool.last_plan) == 0


def test_the_allowed_window_keeps_the_pump_out_of_the_night(make_manager, installation):
    manager = make_manager([dict(POOL, window_start=8, window_end=20)])
    pool = _run(manager, installation, water_c=24.0)

    used = [index for index, value in enumerate(pool.last_plan) if value > 0]
    assert all(8 <= (index % 24) < 20 for index in used)


def test_a_cold_night_blocks_an_air_source_pool_pump(make_manager, installation):
    manager = make_manager([dict(POOL, min_ambient_temp_c=12.0)])
    installation.temperature = [5.0] * 48
    pool = _run(manager, installation, water_c=24.0)
    assert sum(pool.last_plan) == 0


def test_a_disabled_load_contributes_nothing(make_manager, installation):
    manager = make_manager([dict(POOL, enabled=False)])
    installation.sensors["sensor.pool_water"] = 20.0
    manager.run_cycle()
    assert manager.registry.snapshot() == []


def test_an_unknown_type_is_skipped_without_stopping_the_others(make_manager, installation):
    manager = make_manager([{"id": "bogus", "type": "teleporter"}, POOL])
    pool = _run(manager, installation, water_c=24.0)
    assert manager.instance("bogus") is None
    assert pool.last_release["released"] is True


def test_entries_without_an_id_are_skipped(make_manager):
    manager = make_manager([{"type": TYPE_POOL_HEATPUMP}])
    assert manager.enabled_ids() == []


def test_duplicate_ids_keep_only_the_first(make_manager):
    manager = make_manager([
        dict(POOL, target_temp=28.0),
        dict(POOL, target_temp=99.0),
    ])
    assert len(manager.enabled_ids()) == 1
    assert manager.instance("pool").config["target_temp"] == 28.0


def test_a_profile_load_has_no_release_signal(make_manager):
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    manager.push("heating", {"value_wh": 900})
    manager.run_cycle()

    heating = manager.instance("heating")
    assert heating.last_release is None
    assert sum(heating.last_plan) == pytest.approx(900 * 48)


def test_a_pushed_profile_reaches_the_registry(make_manager):
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    manager.push("heating", {"values": [1000.0] * 24})
    manager.run_cycle()

    total = manager.apply([100.0] * 48)
    assert total[0] == pytest.approx(1100.0)
    assert total[24] == pytest.approx(100.0)


def test_pushing_to_a_self_computing_load_is_refused(make_manager):
    manager = make_manager([POOL])
    with pytest.raises(InjectionError) as excinfo:
        manager.push("pool", {"value_wh": 500})
    assert "computes its own demand" in str(excinfo.value)


def test_overriding_a_load_without_a_release_signal_is_refused(make_manager):
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    with pytest.raises(ValueError):
        manager.set_override("heating", "release", 30)


def test_status_reports_the_plan_and_the_release(make_manager, installation):
    manager = make_manager([POOL])
    _run(manager, installation, water_c=24.0)

    status = manager.status()
    assert status["loads"][0]["id"] == "pool"
    assert status["loads"][0]["release"]["released"] is True
    assert status["contribution_total_wh"] > 0


# --- immediate effect --------------------------------------------------------------------

def test_a_push_takes_effect_without_waiting_for_the_next_cycle(make_manager):
    """
    An optimizer run landing before the next poll would plan around a load already
    known to be wrong - which is the whole reason the endpoint exists.
    """
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    manager.push("heating", {"value_wh": 900})

    assert manager.stats.cycles == 0        # no full cycle has run
    assert manager.apply([100.0] * 48)[0] == pytest.approx(1000.0)


def test_clearing_a_push_takes_effect_immediately(make_manager):
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    manager.push("heating", {"value_wh": 900})
    manager.clear_push("heating")
    assert manager.apply([100.0] * 48) == [100.0] * 48


def test_an_override_takes_effect_immediately(make_manager, installation):
    manager = make_manager([POOL])
    _run(manager, installation, water_c=30.0)          # at target, blocked
    assert manager.instance("pool").last_release["released"] is False

    manager.set_override("pool", "release", 30)
    assert manager.instance("pool").last_release["released"] is True


def test_refreshing_an_unknown_or_disabled_load_is_a_no_op(make_manager):
    manager = make_manager([dict(POOL, enabled=False)])
    assert manager.refresh("pool") is False
    assert manager.refresh("nobody") is False


def test_a_refresh_respects_what_the_other_loads_already_planned(make_manager, installation):
    """The shared budget still applies when only one instance is re-planned."""
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})
    manager = make_manager(
        [POOL, {"id": "budget", "type": "external_contingent", "rated_power_w": 3000}],
        max_power_w=2000.0,
    )
    manager.run_cycle()
    pool_now = manager.instance("pool").last_plan

    manager.push("budget", {"total_wh": 6000})
    budget_now = manager.instance("budget").last_plan

    for index, (a, b) in enumerate(zip(pool_now, budget_now)):
        assert a + b <= 2000.0 + 1e-6, f"slot {index} exceeds the shared budget"


# --- hot reload -----------------------------------------------------------------------

def test_reconfiguring_applies_a_new_target_without_a_restart(make_manager, installation):
    manager = make_manager([POOL])
    pool = _run(manager, installation, water_c=29.0)
    assert pool.last_release["released"] is False   # above the 28 C target

    pool.reconfigure(dict(POOL, target_temp=32.0))
    manager.run_cycle()
    assert pool.last_release["released"] is True


def test_reconfiguring_keeps_what_the_calibrator_learned(make_manager, installation):
    """A target temperature change should not cost a fortnight of calibration."""
    manager = make_manager([POOL])
    _run(manager, installation, water_c=24.0)
    pool = manager.instance("pool")

    pool.model.calibrator.loss_coefficient = 17.5
    pool.model.calibrator.loss_samples = 42

    pool.reconfigure(dict(POOL, target_temp=30.0))
    assert pool.model.calibrator.loss_coefficient == 17.5
    assert pool.model.calibrator.loss_samples == 42


def test_reconfiguring_updates_the_minimum_runtime(make_manager):
    from src.loads.instance import ManagedLoad

    load = ManagedLoad(dict(POOL, min_runtime_minutes=30))
    assert load.gate.min_runtime_minutes == 30
    load.reconfigure(dict(POOL, min_runtime_minutes=5))
    assert load.gate.min_runtime_minutes == 5


def test_reconfiguring_can_disable_a_load(make_manager, installation):
    manager = make_manager([POOL])
    _run(manager, installation, water_c=24.0)
    assert manager.registry.snapshot() != []

    manager.instance("pool").reconfigure(dict(POOL, enabled=False))
    manager.run_cycle()
    assert manager.registry.snapshot() == []
