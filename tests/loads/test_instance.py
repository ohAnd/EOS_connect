"""
One managed load end to end: sensors in, forecast and release signal out.

The property these tests exist to protect is that the two outputs agree. A plan that
says "runs at 13:00" while the gate releases at 16:00 would have the optimizer sizing the
battery around a load that never appears.
"""

import pytest

from tests.loads.conftest import NOW

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
    learned_from = pool.model.calibrator.loss_samples

    pool.reconfigure(dict(POOL, target_temp=30.0))
    assert pool.model.calibrator.loss_coefficient == 17.5
    # And the windows it was fitted from are still there.
    assert pool.model.calibrator.loss_samples == learned_from


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
    cal.loss_coefficient, cal.cop_nominal = 61.0, 2.1

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


def test_a_quarter_hour_forecast_is_not_expanded_twice(make_manager, installation):
    """
    The provider already publishes the curve at the optimizer's resolution. Expanding a
    192-value series again described the first twelve hours as if they were two days -
    invisible at hourly resolution, wrong at quarter-hourly.
    """
    manager = make_manager([POOL], time_frame_base=900)
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})
    # 192 slots: hour h holds the value h, each repeated four times.
    installation.temperature = [float(i // 4) for i in range(192)]

    ctx = manager._context()  # pylint: disable=protected-access
    ambient, source = manager._ambient_series(  # pylint: disable=protected-access
        manager.instance("pool"), ctx, {}
    )

    assert source == "forecast"
    assert len(ambient) == 192
    assert ambient[:4] == [0.0] * 4
    assert ambient[-4:] == [47.0] * 4      # the last hour, not hour 11 stretched


def test_an_hourly_forecast_is_still_expanded_for_quarter_hour_slots(make_manager, installation):
    manager = make_manager([POOL], time_frame_base=900)
    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})
    installation.temperature = [float(h) for h in range(48)]

    ctx = manager._context()  # pylint: disable=protected-access
    ambient, _ = manager._ambient_series(  # pylint: disable=protected-access
        manager.instance("pool"), ctx, {}
    )

    assert len(ambient) == 192
    assert ambient[:4] == [0.0] * 4
    assert ambient[4:8] == [1.0] * 4


# --- correcting the forecast to the site ------------------------------------------------

def _ambient(manager, sensor_c=None):
    """Resolve the ambient series once, as the cycle would."""
    ctx = manager._context()  # pylint: disable=protected-access
    readings = {} if sensor_c is None else {"ambient_temp_sensor": sensor_c}
    return manager._ambient_series(  # pylint: disable=protected-access
        manager.instance("pool"), ctx, readings
    )


def test_one_disagreement_is_not_yet_a_bias(make_manager, installation):
    """
    A single reading is not evidence of anything. The offset is learned per hour of the
    day and needs a few observations before it speaks.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [17.9] * 48

    series, source = _ambient(manager, 14.9)

    assert source == "forecast"
    assert series[0] == pytest.approx(17.9)


def test_the_forecast_is_shifted_onto_the_sites_own_thermometer(make_manager, installation):
    """
    A regional forecast has the shape and the sensor has the level. Shifting keeps both,
    where preferring either one throws away what the other knows.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [17.9] * 48

    for _ in range(5):
        _ambient(manager, 14.9)
    series, source = _ambient(manager, 14.9)

    current = manager._context().current_slot  # pylint: disable=protected-access
    assert source == "forecast_corrected"
    assert series[current] == pytest.approx(14.9, abs=0.05)


def test_the_offset_is_learned_for_the_hour_it_was_seen_in(make_manager, installation):
    """
    The point of learning it per hour: a site that runs cold overnight and close to the
    model by afternoon needs two different corrections, not the average of them.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [17.9] * 48

    for _ in range(5):
        _ambient(manager, 14.9)
    series, _ = _ambient(manager, 14.9)

    current = manager._context().current_slot  # pylint: disable=protected-access
    # The hour that was observed is corrected...
    assert series[current] == pytest.approx(14.9, abs=0.05)
    # ...and one twelve hours later, never seen, is not yet touched.
    assert series[(current + 12) % 24] == pytest.approx(17.9)


def test_the_shape_of_the_forecast_survives_the_shift(make_manager, installation):
    """Only the level moves - the diurnal curve is the reason to use a forecast at all."""
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [10.0 + h for h in range(48)]

    series, _ = _ambient(manager, 5.0)

    spans = [series[i + 1] - series[i] for i in range(10)]
    assert all(abs(step - 1.0) < 1e-6 for step in spans)


def test_the_correction_is_smoothed_rather_than_snapped(make_manager, installation):
    """One transient disagreement must not tilt a two-day horizon."""
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [20.0] * 48

    # A settled agreement first, then one reading 6 K out -- large, but still
    # plausibly the same air, so it is smoothed rather than refused outright.
    for _ in range(30):
        _ambient(manager, 20.0)
    series, _ = _ambient(manager, 14.0)

    # It moves towards the outlier, but nowhere near onto it.
    assert series[0] < 20.0
    assert series[0] > 19.0


def test_a_standing_disagreement_is_followed(make_manager, installation):
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [20.0] * 48

    for _ in range(200):
        series, _ = _ambient(manager, 17.0)

    assert series[0] == pytest.approx(17.0, abs=0.3)


def test_the_correction_is_capped(make_manager, installation):
    """A sensor reporting in the wrong unit must not drag the whole horizon with it."""
    from src.loads.ambient_bias import MAX_OFFSET_K

    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [5.0] * 48

    for _ in range(300):
        series, _ = _ambient(manager, 18.0)

    assert series[0] <= 5.0 + MAX_OFFSET_K + 1e-6


def test_a_wild_disagreement_is_refused_and_reported(make_manager, installation, caplog):
    """Beyond a point the two are not measuring the same air, so neither corrects."""
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [20.0] * 48

    with caplog.at_level("WARNING", logger="__main__"):
        series, source = _ambient(manager, -15.0)
        _ambient(manager, -15.0)

    assert source == "forecast"          # used uncorrected
    assert series[0] == 20.0
    warnings = [r.getMessage() for r in caplog.records if "away from the outdoor" in r.getMessage()]
    assert len(warnings) == 1


def test_without_a_sensor_the_forecast_is_used_as_is(make_manager, installation):
    manager = make_manager([POOL])
    installation.temperature = [12.0] * 48

    series, source = _ambient(manager)

    assert source == "forecast"
    assert series[0] == 12.0


def test_the_measurement_is_reported_next_to_what_the_model_used(make_manager, installation):
    """"Outside now" promised a measurement and was showing a forecast."""
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [17.9] * 48
    installation.sensors.update({
        "sensor.pool_water": 24.0, "sensor.pool_power": 0.0, "sensor.outside": 14.9,
    })
    for _ in range(6):
        manager.run_cycle()

    detail = manager.instance("pool").last_demand.detail
    assert detail["ambient_measured_c"] == 14.9
    assert detail["ambient_forecast_c"] == 17.9
    assert detail["ambient_offset_k"] == pytest.approx(-3.0, abs=0.1)
    assert detail["ambient_source"] == "forecast_corrected"


# --- the cover over the horizon -------------------------------------------------------

POOL_WITH_COVER = dict(
    POOL, cover_sensor="binary_sensor.cover", cover_loss_factor=0.35,
    ambient_temp_sensor="sensor.outside",
)


def _cover_series(manager, cover_on=None):
    """
    The per-slot cover factor the model would plan with.

    ``cover_on=None`` means no cover sensor answered, which is what a load without one
    configured actually looks like.
    """
    ctx = manager._context()  # pylint: disable=protected-access
    model = manager.instance("pool").model
    readings = {} if cover_on is None else {"cover_sensor": "on" if cover_on else "off"}
    return model.cover_series(ctx, model.cover_factor(readings))


def test_the_switch_speaks_for_the_next_couple_of_hours(make_manager, installation):
    """
    Planning at nine in the evening with the cover just pulled over used to predict a
    covered pool through the whole of the next afternoon.
    """
    manager = make_manager([POOL_WITH_COVER], time_frame_base=3600)
    series = _cover_series(manager, cover_on=True)

    current = manager._context().current_slot  # pylint: disable=protected-access
    assert series[current] == pytest.approx(0.35)
    assert series[current + 1] == pytest.approx(0.35)


def test_beyond_that_it_stops_speaking_without_a_habit(make_manager, installation):
    """With no history the far horizon still defers to the switch - there is nothing else."""
    manager = make_manager([POOL_WITH_COVER], time_frame_base=3600)
    series = _cover_series(manager, cover_on=True)
    assert series[-1] == pytest.approx(0.35)


def test_a_learned_habit_takes_over_the_far_horizon(make_manager, installation):
    """
    The point of the exercise: what the household usually does at a given hour beats
    what the switch happens to say now, once tomorrow is what is being planned.
    """
    from datetime import timedelta

    manager = make_manager([POOL_WITH_COVER], time_frame_base=3600)
    habit = manager.instance("pool").model.cover_habit

    # Covered overnight, open by day - for a week.
    moment = NOW - timedelta(days=7)
    for _ in range(7):
        for hour in range(24):
            habit.observe(moment.replace(hour=hour), hour >= 21 or hour < 8)
        moment += timedelta(days=1)

    # The switch says covered right now, but the model should not believe that of
    # tomorrow lunchtime.
    series = _cover_series(manager, cover_on=True)
    slots_per_day = 24
    noon_tomorrow = slots_per_day + 12
    midnight_tonight = 23

    assert series[noon_tomorrow] == pytest.approx(1.0)      # open, per the habit
    assert series[midnight_tonight] == pytest.approx(0.35)  # covered, per the habit


def test_a_partial_habit_blends_rather_than_switches(make_manager, installation):
    """Four nights in five is worth four fifths of a cover."""
    from datetime import timedelta

    manager = make_manager([POOL_WITH_COVER], time_frame_base=3600)
    habit = manager.instance("pool").model.cover_habit

    moment = NOW - timedelta(days=10)
    for day in range(10):
        habit.observe(moment.replace(hour=14), day % 5 != 0)
        moment += timedelta(days=1)

    series = _cover_series(manager, cover_on=False)
    blended = series[24 + 14]
    assert 0.35 < blended < 1.0


def test_a_load_without_a_cover_sensor_is_unaffected(make_manager, installation):
    """Nothing reports the cover, so nothing is assumed about it."""
    manager = make_manager([POOL], time_frame_base=3600)
    assert set(_cover_series(manager)) == {1.0}


def test_the_habit_is_learned_from_the_recorded_samples(make_manager, installation):
    """No new storage: it rides on the samples calibration already keeps."""
    manager = make_manager([POOL_WITH_COVER])
    installation.sensors.update({
        "sensor.pool_water": 28.0, "sensor.pool_power": 0.0,
        "sensor.outside": 18.0, "binary_sensor.cover": "on",
    })
    for _ in range(4):
        manager.run_cycle()

    habit = manager.instance("pool").model.cover_habit
    assert habit.probability(NOW.hour) in (None, 1.0)
    assert manager.instance("pool").last_demand.detail["cover_factor"] == 0.35


def test_covering_the_pool_overnight_lowers_the_predicted_demand(make_manager, installation):
    """The whole reason to model it: the cover is worth about two thirds of the loss."""
    from datetime import timedelta

    installation.sensors.update({
        "sensor.pool_water": 26.0, "sensor.pool_power": 0.0, "sensor.outside": 10.0,
        "binary_sensor.cover": "off",
    })
    installation.temperature = [10.0] * 48

    bare = make_manager([POOL_WITH_COVER])
    bare.run_cycle()
    uncovered = bare.instance("pool").last_demand.total_wh

    habitual = make_manager([POOL_WITH_COVER])
    habit = habitual.instance("pool").model.cover_habit
    moment = NOW - timedelta(days=7)
    for _ in range(7):
        for hour in range(24):
            habit.observe(moment.replace(hour=hour), True)   # always covered
        moment += timedelta(days=1)
    habitual.run_cycle()
    covered = habitual.instance("pool").last_demand.total_wh

    assert covered < uncovered * 0.6


def test_the_status_carries_what_was_learned_about_the_site(make_manager, installation):
    """The correction drives the whole forecast, so it has to be inspectable."""
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [17.9] * 48
    installation.sensors.update({
        "sensor.pool_water": 24.0, "sensor.pool_power": 0.0, "sensor.outside": 14.9,
    })
    for _ in range(6):
        manager.run_cycle()

    load = manager.status()["loads"][0]
    assert "ambient_bias" in load
    assert load["ambient_bias"]["mean_offset_k"] == pytest.approx(-3.0, abs=0.1)
    assert len(load["ambient_bias"]["offset_by_hour"]) == 24


def test_a_load_with_no_site_history_reports_no_bias(make_manager):
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    manager.run_cycle()
    assert "ambient_bias" not in manager.status()["loads"][0]


# --- naming what holds a load back ------------------------------------------------------

def test_a_price_cap_is_named_as_the_limit(make_manager, installation):
    """
    The real case: a 22 ct/kWh cap ruled out four fifths of the horizon while the
    window stood open from 06:00 to 23:00, and the page said "widen its window".
    """
    manager = make_manager([dict(POOL, max_price_ct_kwh=5.0)])
    installation.prices = [0.0009] * 48          # 90 ct/kWh everywhere
    installation.prices[20] = 0.00001
    _run(manager, installation, water_c=22.0)

    summary = manager.instance("pool").plan_summary()
    assert summary["limited_by"] == "above price cap"
    assert summary["limited_slots"] > 20


def test_the_allowed_window_is_named_when_it_is_the_limit(make_manager, installation):
    manager = make_manager([dict(POOL, window_start=10, window_end=12)])
    _run(manager, installation, water_c=20.0)

    summary = manager.instance("pool").plan_summary()
    assert summary["limited_by"] == "outside allowed hours"


def test_a_cold_snap_is_named_rather_than_the_window(make_manager, installation):
    """Distinct settings, distinct advice."""
    manager = make_manager([dict(POOL, min_ambient_temp_c=12.0)])
    installation.temperature = [4.0] * 48
    _run(manager, installation, water_c=20.0)

    summary = manager.instance("pool").plan_summary()
    assert summary["limited_by"] == "too cold to run"


def test_nothing_is_blamed_when_the_demand_is_covered(make_manager, installation):
    """
    A slot can be skipped for the daily cap and the demand still be met the next day.
    Reporting that would send the user to loosen a setting that cost them nothing.
    """
    manager = make_manager([POOL])
    installation.temperature = [27.0] * 48        # barely any standing loss
    _run(manager, installation, water_c=27.9)

    load = manager.instance("pool")
    assert sum(load.last_plan) >= load.last_demand.total_wh - 1.0
    assert load.plan_summary()["limited_by"] is None


def test_the_summary_reaches_the_api(make_manager, installation):
    manager = make_manager([dict(POOL, max_price_ct_kwh=5.0)])
    installation.prices = [0.0009] * 48
    _run(manager, installation, water_c=22.0)

    load = manager.status()["loads"][0]
    assert load["plan_summary"]["limited_by"] == "above price cap"
    assert len(load["plan_reasons"]) == 48


def test_an_undersized_appliance_is_not_blamed_on_a_setting(make_manager, installation):
    """
    The real case, and the reason this exists: standing losses grew until 36 kWh was
    wanted from a horizon that could carry 29, and the card said "limited by above
    price cap". Lifting the cap would not have covered it - nothing would.
    """
    manager = make_manager([dict(POOL, rated_power_w=200.0, max_price_ct_kwh=5.0)])
    installation.prices = [0.0009] * 48
    installation.prices[20] = 0.00001
    installation.temperature = [2.0] * 48        # a large, permanent standing loss
    _run(manager, installation, water_c=15.0)

    summary = manager.instance("pool").plan_summary()
    assert summary["over_committed"] is True
    assert summary["reachable_wh"] < manager.instance("pool").last_demand.total_wh
    assert summary["shortfall_wh"] > 0


def test_a_reachable_demand_still_names_the_setting(make_manager, installation):
    """The over-committed flag must not swallow the case it was carved out of."""
    manager = make_manager([dict(POOL, max_price_ct_kwh=5.0)])
    installation.prices = [0.0009] * 48
    installation.prices[20] = 0.00001
    _run(manager, installation, water_c=27.0)

    summary = manager.instance("pool").plan_summary()
    assert summary["limited_by"] == "above price cap"
    assert summary["over_committed"] is False
