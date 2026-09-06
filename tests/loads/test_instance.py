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


# --- power sensor units ------------------------------------------------------------------

def test_an_energy_counter_as_power_sensor_is_reported(make_manager, installation, caplog):
    """
    1234 W and 1234 kWh are the same number, so nothing downstream can catch this: the
    appliance looks permanently on and every efficiency sample is nonsense.
    """
    manager = make_manager([POOL])
    manager.sources.read_sensor_details = lambda name: {
        "state": "412.5", "unit": "kWh", "device_class": "energy",
    }

    with caplog.at_level("WARNING", logger="__main__"):
        manager.check_power_sensors()

    warnings = [r.getMessage() for r in caplog.records if "power_sensor" in r.getMessage()]
    assert len(warnings) == 1
    assert "kWh" in warnings[0]
    # The suffixes the alerts panel turns into a deep link to the setting.
    assert "| Config: #managed-loads" in warnings[0]
    assert "ACTION REQUIRED" in warnings[0]


def test_a_watt_sensor_passes_without_comment(make_manager, installation, caplog):
    manager = make_manager([POOL])
    manager.sources.read_sensor_details = lambda name: {
        "state": "1800", "unit": "W", "device_class": "power",
    }
    with caplog.at_level("WARNING", logger="__main__"):
        manager.check_power_sensors()
    assert not [r for r in caplog.records if "power_sensor" in r.getMessage()]


def test_a_source_reporting_no_unit_is_not_second_guessed(make_manager, caplog):
    """openHAB never reports a unit; assuming the worst would warn every install."""
    manager = make_manager([POOL])
    manager.sources.read_sensor_details = lambda name: {
        "state": "1800", "unit": "", "device_class": "",
    }
    with caplog.at_level("WARNING", logger="__main__"):
        manager.check_power_sensors()
    assert not [r for r in caplog.records if "power_sensor" in r.getMessage()]


def test_an_unreachable_sensor_is_not_a_configuration_complaint(make_manager, caplog):
    def boom(_name):
        raise ConnectionError("down")

    manager = make_manager([POOL])
    manager.sources.read_sensor_details = boom
    with caplog.at_level("WARNING", logger="__main__"):
        manager.check_power_sensors()
    assert not [r for r in caplog.records if "power_sensor" in r.getMessage()]


def test_loads_without_a_power_sensor_are_skipped(make_manager):
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    manager.check_power_sensors()   # must not raise


# --- outdoor temperature ------------------------------------------------------------------

def test_a_pool_declares_that_it_needs_the_outdoor_forecast(make_manager):
    """
    Both what a pool loses and how efficiently the pump replaces it turn on the air
    temperature, so the manager has to be able to ask for the forecast - the default
    optimizer backend does not.
    """
    assert make_manager([POOL]).needs_outdoor_temperature() is True


def test_an_indoor_store_does_not(make_manager):
    tank = {"id": "dhw", "type": "hot_water_tank", "temp_sensor": "sensor.dhw"}
    assert make_manager([tank]).needs_outdoor_temperature() is False


def test_a_pushed_profile_does_not(make_manager):
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    assert manager.needs_outdoor_temperature() is False


def test_a_disabled_pool_does_not_keep_the_forecast_alive(make_manager):
    assert make_manager([dict(POOL, enabled=False)]).needs_outdoor_temperature() is False


def test_the_forecast_reaches_the_model_rather_than_a_flat_default(make_manager, installation):
    """The whole point: the loss and COP terms must move with the forecast."""
    manager = make_manager([POOL])
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})

    installation.temperature = [20.0] * 48
    manager.run_cycle()
    mild = manager.instance("pool").last_demand.total_wh

    installation.temperature = [4.0] * 48
    manager.run_cycle()
    cold = manager.instance("pool").last_demand.total_wh

    assert cold > mild, "a colder forecast has to mean more energy needed"


# --- where the ambient temperature comes from -----------------------------------------------

POOL_WITH_AMBIENT = dict(POOL, ambient_temp_sensor="sensor.outside")


def test_a_real_forecast_is_preferred_over_a_flat_sensor_reading(make_manager, installation):
    """Only the forecast can see into tomorrow, and the horizon is two days long."""
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.sensors.update({
        "sensor.pool_water": 24.0, "sensor.pool_power": 0.0, "sensor.outside": 30.0,
    })
    installation.temperature = [8.0] * 48
    manager.run_cycle()

    ctx = manager._context()  # pylint: disable=protected-access
    ambient, source = manager._ambient_series(  # pylint: disable=protected-access
        manager.instance("pool"), ctx, {"ambient_temp_sensor": 30.0}
    )
    assert ambient[0] == 8.0
    assert source == "forecast"


def test_a_configured_sensor_beats_a_missing_forecast(make_manager, installation):
    """
    The bug this exists for: the placeholder curve that stands in for an unfetched
    forecast is indistinguishable from a real one, so a pool with a perfectly good
    outdoor sensor was modelled against a fixed 15 C.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.sensors.update({
        "sensor.pool_water": 24.0, "sensor.pool_power": 0.0, "sensor.outside": 21.5,
    })
    installation.temperature = []          # the provider reports "nothing real"

    ctx = manager._context()  # pylint: disable=protected-access
    ambient, source = manager._ambient_series(  # pylint: disable=protected-access
        manager.instance("pool"), ctx, {"ambient_temp_sensor": "21.5 °C"}
    )
    assert ambient == [21.5] * ctx.slot_count
    assert source == "sensor"


def test_that_sensor_actually_changes_the_prediction(make_manager, installation):
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = []
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})

    installation.sensors["sensor.outside"] = 24.0
    manager.run_cycle()
    warm = manager.instance("pool").last_demand.total_wh

    installation.sensors["sensor.outside"] = 6.0
    manager.run_cycle()
    cold = manager.instance("pool").last_demand.total_wh

    assert cold > warm


def test_no_forecast_and_no_sensor_is_reported_once(make_manager, installation, caplog):
    """A silent constant is the worst outcome: it looks like it is working."""
    manager = make_manager([POOL])
    installation.temperature = []
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})

    with caplog.at_level("WARNING", logger="__main__"):
        manager.run_cycle()
        manager.run_cycle()

    warnings = [r.getMessage() for r in caplog.records if "sits outdoors" in r.getMessage()]
    assert len(warnings) == 1
    assert "| Config: #managed-loads" in warnings[0]


def test_an_indoor_store_is_not_nagged_about_the_weather(make_manager, installation, caplog):
    tank = {"id": "dhw", "type": "hot_water_tank", "temp_sensor": "sensor.dhw"}
    installation.sensors["sensor.dhw"] = 45.0
    installation.temperature = []

    with caplog.at_level("WARNING", logger="__main__"):
        make_manager([tank]).run_cycle()
    assert not [r for r in caplog.records if "sits outdoors" in r.getMessage()]


def test_the_prediction_reports_the_inputs_it_used(make_manager, installation):
    """
    The placeholder-ambient bug produced entirely plausible outputs and was invisible
    because none of its inputs were reported. They are now.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = []
    installation.sensors.update({
        "sensor.pool_water": 24.0, "sensor.pool_power": 0.0, "sensor.outside": 21.5,
    })
    manager.run_cycle()

    detail = manager.instance("pool").last_demand.detail
    assert detail["ambient_now_c"] == 21.5
    assert detail["ambient_source"] == "sensor"
    assert detail["horizon_hours"] > 0
    assert detail["electrical_power_w"] == 2500.0
    # The one figure that was in the wrong currency next to the energy numbers.
    assert detail["thermal_power_w"] > detail["electrical_power_w"]


def test_a_guessed_ambient_is_labelled_as_such(make_manager, installation):
    manager = make_manager([POOL])
    installation.temperature = []
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})
    manager.run_cycle()
    assert manager.instance("pool").last_demand.detail["ambient_source"] == "fallback"


# --- resetting a calibration ----------------------------------------------------------------

def test_resetting_returns_a_model_to_its_configured_values(make_manager, installation):
    manager = make_manager([dict(POOL, heat_loss_w_per_m2_k=25.0, cop_nominal=5.0)])
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})
    manager.run_cycle()

    cal = manager.instance("pool").model.calibrator
    cal.loss_coefficient, cal.cop_nominal, cal.loss_samples = 61.0, 2.1, 300

    state = manager.reset_calibration("pool")
    assert state["loss_coefficient"] == 25.0
    assert state["cop_nominal"] == 5.0
    assert state["loss_samples"] == 0
    assert state["confidence"] == 0.0


def test_resetting_also_clears_the_recorded_samples(make_manager, installation, tmp_path):
    """
    The samples carry the inputs the fit was made against, so leaving them behind means
    the same wrong numbers come straight back.
    """
    from src.config_web.store import ConfigStore
    from src.persistence import ManagedLoadStore

    config_store = ConfigStore(str(tmp_path / "c.db"))
    config_store.open()
    store = ManagedLoadStore(config_store)
    store.ensure_schema()

    manager = make_manager([POOL], store=store)
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})
    manager.run_cycle()
    assert store.sample_count("pool") > 0

    manager.reset_calibration("pool")
    assert store.sample_count("pool") == 0
    assert store.load_model_state("pool") is None
    config_store.close()


def test_resetting_takes_effect_without_waiting_for_a_cycle(make_manager, installation):
    manager = make_manager([POOL])
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})
    manager.run_cycle()
    before = manager.stats.cycles

    manager.reset_calibration("pool")
    assert manager.stats.cycles == before      # no full cycle
    assert manager.instance("pool").last_demand is not None


def test_resetting_something_that_learns_nothing_is_refused(make_manager):
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    with pytest.raises(InjectionError) as excinfo:
        manager.reset_calibration("heating")
    assert "learns nothing" in str(excinfo.value)


def test_resetting_an_unknown_load_is_refused(make_manager):
    with pytest.raises(InjectionError):
        make_manager([POOL]).reset_calibration("nobody")
