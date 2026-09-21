"""
One managed load end to end: sensors in, forecast and release signal out.

The property these tests exist to protect is that the two outputs agree. A plan that
says "runs at 13:00" while the gate releases at 16:00 would have the optimizer sizing the
battery around a load that never appears.
"""

import math

import pytest

from datetime import timedelta

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

def _settle(manager, sensor_c, cycles=20):
    """
    Run the cycle until the smoothed departure has stopped moving.

    The model deliberately does not jump to a single reading - that is what would
    switch an appliance on and off as a noisy sensor crossed a threshold - so a test
    about the settled value has to let it settle.
    """
    series = source = None
    for _ in range(cycles):
        series, source = _ambient(manager, sensor_c)
    return series, source


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

    series, source = _settle(manager, 14.9)

    current = manager._context().current_slot  # pylint: disable=protected-access
    assert source == "forecast_corrected"
    assert series[current] == pytest.approx(14.9, abs=0.05)


def test_the_model_converges_on_the_thermometer_without_chasing_it(
    make_manager, installation
):
    """
    Two requirements that pull against each other, and both matter.

    *Accurate*: the offset is a per-hour climatology, so it describes a typical hour
    learned across days. On an untypical one it is simply wrong - live, a site whose
    sensor catches the morning sun had learned +4 K for 09:00 and read 8.9 C where the
    typical day said 13.9, better than three kelvin out, with the cold cut-off between
    the two. The settled figure has to be close to what the thermometer says.

    *Not live*: the release keys off this curve, so a reading wandering across the
    cut-off would switch the appliance on and off with it. A single sample must move
    the model part of the way, never all of it.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [17.9] * 48

    settled, _ = _settle(manager, 20.9)        # a typical day here runs 3 K warm
    current = manager._context().current_slot  # pylint: disable=protected-access
    assert settled[current] == pytest.approx(20.9, abs=0.2)

    # One cold sample: it must move, and it must not arrive.
    once, _ = _ambient(manager, 11.9)
    assert once[current] < settled[current] - 0.5, "a real change has to register"
    assert once[current] > 13.0, "one sample must not carry the model the whole way"

    # Held, it converges - within the half-kelvin the cut-off can live with.
    converged, _ = _settle(manager, 11.9)
    assert converged[current] == pytest.approx(11.9, abs=0.5)


def test_the_departure_loosens_its_grip_with_distance(make_manager, installation):
    """
    Today's anomaly is a property of the day, not of the instant - so it carries
    forward, and it fades. Clamping the whole horizon to one reading would be worse
    than the climatology it replaced; dropping it after the current slot leaves the
    planner sizing energy into hours it cannot use.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [17.9] * 48

    _settle(manager, 20.9)                     # typical day: 3 K warm
    series, _ = _settle(manager, 11.9)         # today: 9 K colder than typical

    ctx = manager._context()                   # pylint: disable=protected-access
    current, per_hour = ctx.current_slot, ctx.slots_per_hour()

    now = series[current]
    assert now == pytest.approx(11.9, abs=0.5)

    # The departure is measured against the climatology, which for an hour with no
    # observations of its own is the bare forecast.
    departure = now - 20.9
    at_half_life = series[current + 6 * per_hour] - 17.9
    assert 0.35 < at_half_life / departure < 0.65, (
        f"expected about half of {departure:.1f} K, got {at_half_life:.2f}"
    )

    # Not a full day out: 24 h later is the same hour of the clock, which carries its
    # own learned offset, so the bare forecast is no longer the baseline there.
    at_two_half_lives = series[current + 12 * per_hour] - 17.9
    assert 0.15 < at_two_half_lives / departure < 0.35, (
        f"expected about a quarter of {departure:.1f} K, got {at_two_half_lives:.2f}"
    )


def test_a_cold_morning_blocks_the_run_even_when_the_habit_says_otherwise(
    make_manager, installation
):
    """
    The whole point of fixing the current slot, end to end: no release below the
    minimum temperature, whatever the learned offset believes.

    Reproduces the live numbers. The pool preset cuts off at 12.0 C. Early mornings had
    learned a positive offset - sun on the sensor on clear days - so a forecast of 11.6
    was corrected up to 12.4 and cleared the cut-off by 0.4 K, while the thermometer
    read 8.3, some 3.7 K below it. The pool ran through hours the same model marked
    "too cold to run" a day later.
    """
    pool = dict(POOL_WITH_AMBIENT, min_ambient_temp_c=12.0)
    manager = make_manager([pool])
    installation.temperature = [11.6] * 48

    # Teach the hour a warm habit, as a run of sunny mornings would. Enough of them
    # that today's single cold reading cannot drag the learned offset under the
    # cut-off by itself - otherwise the test would pass on the bias moving, and prove
    # nothing about the current slot carrying the measurement.
    #   20 warm at +0.8, one cold at -3.3  ->  (16.0 - 3.3) / 21 = +0.605
    #   corrected = 11.6 + 0.605 = 12.2, still clear of the 12.0 cut-off
    for _ in range(20):
        _ambient(manager, 12.4)

    installation.sensors.update({"sensor.pool_water": 24.0, "sensor.pool_power": 0.0})
    installation.sensors["sensor.outside"] = 8.3          # today is not sunny
    manager.run_cycle()

    load = manager.instance("pool")
    ctx = manager._last_ctx                        # pylint: disable=protected-access
    current = ctx.current_slot
    demand = load.last_demand

    assert demand.feasible[current] is False, "a run below the cut-off must be refused"
    assert demand.slot_reasons[current] == "too cold to run"


def test_the_offset_is_learned_for_the_hour_it_was_seen_in(make_manager, installation):
    """
    The point of learning it per hour: a site that runs cold overnight and close to the
    model by afternoon needs two different corrections, not the average of them.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [17.9] * 48

    # Deliberately few: past a dozen observations the whole-day average has enough
    # weight to speak for hours never seen, which is its own documented behaviour and
    # would mask the per-hour one this test is about.
    series = None
    for _ in range(6):
        series, _ = _ambient(manager, 14.9)

    current = manager._context().current_slot  # pylint: disable=protected-access
    # The hour that was observed is pulled most of the way to the thermometer...
    assert series[current] < 15.5, series[current]
    # ...and one twelve hours later, never seen, keeps the forecast. Only the day's
    # own departure reaches it, a quarter of it by then, not the -3 K this hour earns.
    assert series[current + 12] > 17.0, series[current + 12]


def test_the_shape_of_the_forecast_survives_once_the_anomaly_has_faded(
    make_manager, installation
):
    """
    The diurnal curve is the reason to use a forecast at all, so it has to come back.

    Today's departure from the typical day is carried forward and fades, which does
    bend the curve near the present - that is the blend doing its job. What must not
    happen is the bend persisting: far enough out the series has to be the plain
    corrected forecast again, or a cold morning would be projected onto tomorrow
    afternoon.
    """
    manager = make_manager([POOL_WITH_AMBIENT])
    installation.temperature = [10.0 + h for h in range(48)]

    series, _ = _ambient(manager, 5.0)
    current = manager._context().current_slot  # pylint: disable=protected-access

    # Far end: back on the forecast, and still rising 1 K per slot.
    tail = [series[i + 1] - series[i] for i in range(len(series) - 6, len(series) - 1)]
    assert all(abs(step - 1.0) < 0.05 for step in tail), tail
    assert series[-1] == pytest.approx(10.0 + len(series) - 1, abs=0.2)

    # And it is a fade, not a distortion: the shift shrinks with every slot.
    shift = [series[i] - (10.0 + i) for i in range(current, len(series))]
    assert all(abs(b) <= abs(a) + 1e-9 for a, b in zip(shift, shift[1:])), shift[:8]
    assert abs(shift[-1]) < 0.2, "the anomaly must be spent by the far horizon"


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
    for _ in range(20):
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


def test_the_summary_reports_what_the_plan_averages(make_manager, installation):
    """The number that makes an expensive planned hour legible instead of alarming."""
    manager = make_manager([POOL])
    installation.prices = [0.0003] * 48
    _run(manager, installation, water_c=20.0)

    summary = manager.instance("pool").plan_summary()
    assert summary["avg_price_ct_kwh"] == pytest.approx(30.0, abs=0.1)



# --- handing the placing to the optimizer ------------------------------------------------

def test_a_scheduled_load_is_not_also_in_the_household_forecast(make_manager, installation):
    """
    It cannot be both. Injecting it *and* asking the solver to place it would have the
    optimizer schedule around an appliance it is simultaneously scheduling.
    """
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    base = [1000.0] * 48
    assert manager.apply(base, 3600) == base


def test_the_load_is_offered_to_the_optimizer_instead(make_manager, installation):
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    records = manager.schedulable()
    assert len(records) == 1
    record = records[0]
    assert record["id"] == "pool"
    assert record["demand_wh"] > 0
    assert record["max_power_w"] > 0
    assert len(record["feasible"]) == 48


def test_nothing_is_offered_while_the_backend_cannot_place_it(make_manager, installation):
    """The record is built either way; what changes is who acts on it."""
    manager = make_manager([POOL])
    _run(manager, installation, water_c=20.0)

    base = [1000.0] * 48
    assert manager.apply(base, 3600) != base


def test_the_gate_waits_for_the_schedule_rather_than_guessing(make_manager, installation):
    """
    The gate remembers when it last released. Settling it on a plan that is about to be
    replaced would start a minimum-runtime hold on a decision nobody made.
    """
    manager = make_manager([dict(POOL, min_runtime_minutes=60)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    assert manager.instance("pool").last_release is None


def test_adopting_a_schedule_settles_the_gate_on_it(make_manager, installation):
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    schedule = [0.0] * 48
    schedule[manager._last_ctx.current_slot] = 1500.0   # pylint: disable=protected-access
    assert manager.adopt_schedules({"pool": schedule}) == 1

    load = manager.instance("pool")
    assert load.last_release["released"] is True
    assert load.last_plan[manager._last_ctx.current_slot] == 1500.0  # pylint: disable=protected-access


def test_an_empty_schedule_blocks_the_load(make_manager, installation):
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    manager.adopt_schedules({"pool": [0.0] * 48})
    assert manager.instance("pool").last_release["released"] is False


def test_a_load_the_optimizer_did_not_answer_for_keeps_its_own_plan(
    make_manager, installation
):
    """A solver that failed must not leave a pool with no way to decide anything."""
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)
    own_plan = list(manager.instance("pool").last_plan)

    assert manager.adopt_schedules({"sauna": [0.0] * 48}) == 0
    assert manager.instance("pool").last_plan == own_plan


def test_a_disabled_load_is_never_offered(make_manager, installation):
    manager = make_manager([dict(POOL, enabled=False)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)
    assert manager.schedulable() == []


def test_a_pushed_profile_load_is_never_offered(make_manager, installation):
    """Nothing to place: it says what it *will* draw, not what it needs."""
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    manager.external_scheduler = True
    manager.push("heating", {"value_wh": 900})
    assert manager.schedulable() == []


def test_a_profile_load_still_joins_the_household_forecast(make_manager, installation):
    """Only contingent loads move; a pushed profile is part of the forecast as before."""
    manager = make_manager([{"id": "heating", "type": TYPE_EXTERNAL_PROFILE}])
    manager.external_scheduler = True
    manager.push("heating", {"value_wh": 900})
    manager.run_cycle()

    base = [1000.0] * 48
    assert manager.apply(base, 3600) != base


def test_the_price_limit_becomes_what_the_energy_is_worth(make_manager, installation):
    manager = make_manager([dict(POOL, max_price_ct_kwh=25.0)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    assert manager.schedulable()[0]["value_eur_per_wh"] == pytest.approx(0.00025)


def test_no_price_limit_means_worth_more_than_any_tariff(make_manager, installation):
    """
    Finite on purpose. An unbounded reward would make a target that cannot be reached
    into an unbounded objective rather than a short plan.
    """
    manager = make_manager([dict(POOL, max_price_ct_kwh=0)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    value = manager.schedulable()[0]["value_eur_per_wh"]
    assert value > max(installation.prices) * 5
    assert value < float("inf")


def test_a_silent_optimizer_hands_the_load_back_its_own_plan(make_manager, installation, caplog):
    """
    The release signal is only refreshed when a schedule arrives, so an optimizer that
    stops answering would leave the appliance latched on whatever it was last told -
    a pump held released indefinitely with nothing saying why.
    """
    manager = make_manager([POOL], cycle_seconds=60)
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    assert load.last_release is None, "gated before any schedule arrived"

    # Two cycles' worth of silence later, it decides for itself again.
    load.waiting_for_schedule_since = load.waiting_for_schedule_since - timedelta(
        seconds=600
    )
    with caplog.at_level("WARNING"):
        manager.run_cycle()

    assert load.last_release is not None
    assert "no schedule from the optimizer" in caplog.text


def test_a_load_that_falls_back_rejoins_the_household_forecast(make_manager, installation):
    """Otherwise its energy is in neither place and the optimizer plans around nothing."""
    manager = make_manager([POOL], cycle_seconds=60)
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    load.waiting_for_schedule_since = load.waiting_for_schedule_since - timedelta(
        seconds=600
    )
    manager.run_cycle()

    base = [1000.0] * 48
    assert manager.apply(base, 3600) != base


def test_a_fresh_load_waits_rather_than_counting_itself_twice(make_manager, installation):
    """
    On its first cycle nothing has scheduled it yet - but that is new, not stale.
    Falling back immediately would put it in the household forecast *and* offer it to
    the solver, and the optimizer would size the battery for both.
    """
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    base = [1000.0] * 48
    assert manager.apply(base, 3600) == base
    assert manager.schedulable()


def test_an_adopted_schedule_restarts_the_clock(make_manager, installation):
    manager = make_manager([POOL], cycle_seconds=60)
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)
    manager.adopt_schedules({"pool": [0.0] * 48})

    load = manager.instance("pool")
    assert load.last_schedule_at is not None
    assert not load.schedule_is_stale(load.last_schedule_at, 120)


def test_the_release_survives_a_cycle_between_two_optimizer_runs(
    make_manager, installation
):
    """
    The card reported a bare gate state - no energy figure, no next start - for every
    managed-load cycle that landed between two solves, because the cycle nulled it.
    """
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    schedule = [0.0] * 48
    schedule[manager._last_ctx.current_slot] = 1500.0   # pylint: disable=protected-access
    manager.adopt_schedules({"pool": schedule})
    adopted = manager.instance("pool").last_release
    assert "energy_needed_wh" in adopted

    manager.run_cycle()
    kept = manager.instance("pool").last_release
    assert kept is not None
    assert "energy_needed_wh" in kept


def test_the_reported_price_describes_the_schedule_that_was_adopted(
    make_manager, installation
):
    """
    It was reporting the fallback planner's figure beside the optimizer's schedule -
    a price for hours the appliance is not going to run in. On the live instance that
    read 0.0 ct/kWh against a plan sitting in two 29.8 ct slots.
    """
    manager = make_manager([POOL])
    manager.external_scheduler = True
    installation.prices = [0.0009] * 48
    installation.prices[20] = 0.0002
    installation.prices[21] = 0.0002
    _run(manager, installation, water_c=20.0)

    schedule = [0.0] * 48
    schedule[20] = schedule[21] = 1500.0
    manager.adopt_schedules({"pool": schedule})

    assert manager.instance("pool").plan_summary()["avg_price_ct_kwh"] == pytest.approx(
        20.0, abs=0.1
    )


def test_prices_that_have_not_arrived_place_nothing(make_manager, installation, caplog):
    """
    An all-zero series is an interface that has not fetched, not a free tariff - and it
    read as free electricity, so the cap stopped binding and the whole horizon was
    planned at 0 ct.
    """
    manager = make_manager([dict(POOL, max_price_ct_kwh=30.0)])
    installation.prices = [0.0] * 48
    with caplog.at_level("INFO"):
        _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    assert sum(load.last_plan) == 0
    assert "prices have not arrived yet" in caplog.text


def test_prices_still_missing_after_startup_is_a_warning(make_manager, installation, caplog):
    """
    Once at startup is this thread and the price interface racing, which is expected.
    Still true a cycle later means something is actually wrong.
    """
    manager = make_manager([dict(POOL, max_price_ct_kwh=30.0)])
    installation.prices = [0.0003] * 48
    manager.run_cycle()                      # a normal cycle first

    installation.prices = [0.0] * 48
    manager._warned_no_prices = False        # pylint: disable=protected-access
    with caplog.at_level("WARNING"):
        manager.run_cycle()

    assert "no electricity prices available" in caplog.text


def test_a_genuinely_free_hour_is_still_usable(make_manager, installation):
    """Only a series that is zero *everywhere* is treated as missing."""
    manager = make_manager([dict(POOL, max_price_ct_kwh=30.0)])
    installation.prices = [0.0009] * 48
    installation.prices[20] = 0.0
    _run(manager, installation, water_c=20.0)

    assert manager.instance("pool").last_plan[20] > 0


def test_the_reasons_describe_the_schedule_that_was_adopted(make_manager, installation):
    """
    They came from the fallback planner, which had refused the very slots the optimizer
    went on to use. On the live instance ten slots carried energy while every one was
    labelled "above price cap" - bars drawn in slots the same card coloured as blocked.
    """
    manager = make_manager([dict(POOL, max_price_ct_kwh=20.0)])
    manager.external_scheduler = True
    installation.prices = [0.0009] * 48          # the fallback refuses everything
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    assert "planned" not in load.last_demand.slot_reasons, "fallback planned something"

    schedule = [0.0] * 48
    schedule[30] = schedule[31] = 1500.0
    manager.adopt_schedules({"pool": schedule})

    reasons = load.last_demand.slot_reasons
    assert reasons[30] == "planned"
    assert reasons[31] == "planned"
    assert "above price cap" not in reasons


def test_a_feasible_slot_the_optimizer_skipped_says_why(make_manager, installation):
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    manager.adopt_schedules({"pool": [0.0] * 48})
    reasons = manager.instance("pool").last_demand.slot_reasons
    assert "costs more than it is worth" in reasons


def test_slots_left_over_once_the_demand_is_met_are_not_a_price_verdict(
    make_manager, installation
):
    """
    The label the live card got wrong. "costs more than it is worth" was the catch-all
    for every feasible unplanned slot while demand was non-zero, so a load that had
    been given everything it asked for still reported its spare hours as priced out.

    On the instance that read as four blocked evening hours under a price heading -
    two of them *below* the configured cap - when the pool had simply finished: 36,280
    Wh placed against 36,293 needed. The two cases look alike because the slots dropped
    are the dearest either way, which is exactly why the label has to tell them apart.
    """
    manager = make_manager([POOL])
    manager.external_scheduler = True
    # Warm enough that the demand fits the horizon with slots to spare; a colder pool
    # wants more than the window holds and can never be covered.
    load = _run(manager, installation, water_c=27.8)

    demand = load.last_demand
    ctx = manager._last_ctx                        # pylint: disable=protected-access
    slot_wh = demand.max_power_w * ctx.hours_per_slot()
    needed = math.ceil(demand.total_wh / slot_wh)

    feasible = [i for i, ok in enumerate(demand.feasible)
                if ok and i >= ctx.current_slot]
    assert len(feasible) > needed, "fixture must leave spare slots to label"

    schedule = [0.0] * ctx.slot_count
    for index in feasible[:needed]:
        schedule[index] = demand.max_power_w
    manager.adopt_schedules({"pool": schedule})

    reasons = load.last_demand.slot_reasons
    spare = [reasons[i] for i in feasible[needed:]]
    assert spare, "no spare slots were left to check"
    assert all(r == "not needed" for r in spare), set(spare)
    assert "costs more than it is worth" not in reasons


def test_a_load_still_short_keeps_the_price_verdict(make_manager, installation):
    """The other half: only a *covered* load gets the softer label."""
    manager = make_manager([POOL])
    manager.external_scheduler = True
    load = _run(manager, installation, water_c=20.0)

    ctx = manager._last_ctx                        # pylint: disable=protected-access
    demand = load.last_demand
    feasible = [i for i, ok in enumerate(demand.feasible)
                if ok and i >= ctx.current_slot]

    # Well short of the demand, so the unplanned remainder really was passed over.
    schedule = [0.0] * ctx.slot_count
    for index in feasible[:10]:
        schedule[index] = demand.max_power_w
    manager.adopt_schedules({"pool": schedule})

    assert "costs more than it is worth" in load.last_demand.slot_reasons


def test_a_slot_the_load_may_not_use_keeps_its_own_reason(make_manager, installation):
    """The window and the temperature limit are still the load's own rules."""
    manager = make_manager([dict(POOL, window_start=10, window_end=12)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    manager.adopt_schedules({"pool": [0.0] * 48})
    reasons = manager.instance("pool").last_demand.slot_reasons
    assert "outside allowed hours" in reasons


def test_past_slots_stay_past(make_manager, installation):
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    manager.adopt_schedules({"pool": [0.0] * 48})
    reasons = manager.instance("pool").last_demand.slot_reasons
    current = manager._last_ctx.current_slot            # pylint: disable=protected-access
    assert all(r == "past" for r in reasons[:current])


# --- what carries across a re-plan -------------------------------------------------------

def test_a_running_load_commits_the_rest_of_its_minimum_run(make_manager, installation):
    """
    A plan knows nothing of the one before it. On the live pool that produced six- and
    seven-minute stops out of fifty-nine plans in a day, none of which contained one.
    """
    manager = make_manager([dict(POOL, min_runtime_minutes=60)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    ctx = manager._last_ctx                        # pylint: disable=protected-access
    load.gate.released = True
    load.gate.released_since = ctx.now - timedelta(minutes=15)

    on_slots, off_slots = load.commitment(ctx)
    assert on_slots == 1, "45 minutes left of an hour, at hourly slots"
    assert off_slots == 0


def test_a_blocked_load_commits_the_rest_of_its_rest(make_manager, installation):
    manager = make_manager([dict(POOL, min_runtime_minutes=60)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    ctx = manager._last_ctx                        # pylint: disable=protected-access
    load.gate.released = False
    load.gate.blocked_since = ctx.now - timedelta(minutes=20)

    on_slots, off_slots = load.commitment(ctx)
    assert off_slots == 1
    assert on_slots == 0


def test_a_served_minimum_pins_nothing_but_still_reports_running(make_manager, installation):
    """
    Holding a slot and knowing a run is under way have to stay apart.

    Pinning the head slot whenever the gate is released latches: the pin keeps the
    load released, so the next cycle pins it again, and it can never stop while it has
    demand and the slot is allowed. Live, that ran a pool from 16:04 to 19:45 straight
    through a rising tariff into the dearest slot of the day - 34.65 ct against a 30 ct
    limit - and it placed exactly the same energy either way. The battery went 35 % to
    16 % paying for it.

    So: no pin once the minimum is served, but `is_running` still tells the solver that
    continuing costs no start.
    """
    manager = make_manager([dict(POOL, min_runtime_minutes=30)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    ctx = manager._last_ctx                        # pylint: disable=protected-access
    load.gate.released = True
    load.gate.released_since = ctx.now - timedelta(hours=2)

    assert load.commitment(ctx) == (0, 0), "a served minimum must not force the slot"
    assert load.is_running() is True


def test_a_running_load_with_no_minimum_is_not_pinned(make_manager, installation):
    """Nothing to serve and nothing to hold - but it is still running."""
    manager = make_manager([dict(POOL, min_runtime_minutes=0)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    load.gate.released = True
    load.gate.released_since = manager._last_ctx.now   # pylint: disable=protected-access
    assert load.commitment(manager._last_ctx) == (0, 0)   # pylint: disable=protected-access
    assert load.is_running() is True


def test_a_load_standing_still_is_not_running(make_manager, installation):
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    load.gate.released = False
    assert load.is_running() is False


def test_a_load_that_is_not_running_commits_nothing(make_manager, installation):
    """
    The held slot is for a run under way. A load standing still with its rest served is
    free, or it could never be given a different plan.
    """
    manager = make_manager([dict(POOL, min_runtime_minutes=30)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    ctx = manager._last_ctx                        # pylint: disable=protected-access
    load.gate.released = False
    load.gate.blocked_since = ctx.now - timedelta(hours=2)
    assert load.commitment(ctx) == (0, 0)


def test_the_commitment_reaches_the_optimizer(make_manager, installation):
    manager = make_manager([dict(POOL, min_runtime_minutes=60)])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    load.gate.released = True
    load.gate.released_since = manager._last_ctx.now    # pylint: disable=protected-access

    record = manager.schedulable()[0]
    assert record["committed_on_slots"] >= 1
    assert record["committed_off_slots"] == 0

    # Once the minimum has long lapsed the pin goes, but the running flag stays - the
    # solver still needs to know continuing is free, or it cycles on a coin toss.
    load.gate.released_since = (
        manager._last_ctx.now - timedelta(hours=4)      # pylint: disable=protected-access
    )
    lapsed = manager.schedulable()[0]
    assert lapsed["committed_on_slots"] == 0
    assert lapsed["already_running"] is True


def test_the_gate_remembers_when_it_went_quiet(make_manager, installation):
    """The mirror of released_since, and it had no mirror before."""
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)

    load = manager.instance("pool")
    ctx = manager._last_ctx                        # pylint: disable=protected-access
    schedule = [0.0] * 48
    schedule[ctx.current_slot] = 1500.0
    manager.adopt_schedules({"pool": schedule})
    assert load.gate.released is True
    assert load.gate.blocked_since is None

    manager.adopt_schedules({"pool": [0.0] * 48})
    assert load.gate.released is False
    assert load.gate.blocked_since is not None


# --- the sun ----------------------------------------------------------------------------

def test_a_sunny_horizon_needs_less_from_the_pump(make_manager, installation):
    """
    A store that gains heat from the sun needs less from the appliance. Measuring that
    gain and never spending it would book the saving and ask for the energy anyway.
    """
    manager = make_manager([POOL])
    installation.pv = [4000.0] * 48
    _run(manager, installation, water_c=20.0)
    load = manager.instance("pool")

    load.model.calibrator.solar_gain = 0.0
    without = load.model.demand(manager._last_ctx_for["pool"]).total_wh  # pylint: disable=protected-access
    load.model.calibrator.solar_gain = 0.4
    withsun = load.model.demand(manager._last_ctx_for["pool"]).total_wh  # pylint: disable=protected-access

    assert withsun < without


def test_no_learned_gain_changes_nothing(make_manager, installation):
    """Until the fit can separate the sun, the forecast is what it always was."""
    manager = make_manager([POOL])
    installation.pv = [4000.0] * 48
    _run(manager, installation, water_c=20.0)
    load = manager.instance("pool")

    assert load.model.calibrator.solar_gain == 0.0
    ctx = manager._last_ctx_for["pool"]                  # pylint: disable=protected-access
    before = load.model.demand(ctx).total_wh
    installation.pv = [0.0] * 48
    manager.run_cycle()
    after = load.model.demand(manager._last_ctx_for["pool"]).total_wh   # pylint: disable=protected-access
    assert before == pytest.approx(after, rel=0.01)


def test_the_pv_counter_is_recorded_for_the_calibration(make_manager, installation):
    readings = {"value": 1234.5}
    manager = make_manager([POOL])
    manager.sources.pv_counter_kwh = lambda: readings["value"]
    _run(manager, installation, water_c=20.0)

    samples = manager.instance("pool").model.calibrator._window   # pylint: disable=protected-access
    assert samples, "no sample was recorded"
    assert samples[-1]["pv_counter_kwh"] == pytest.approx(1234.5)


def test_a_site_without_a_pv_meter_still_records_a_proxy(make_manager, installation):
    """The forecast is weaker, but it still tells a bright window from a dark one."""
    manager = make_manager([POOL])
    installation.pv = [4000.0] * 48
    _run(manager, installation, water_c=20.0)

    samples = manager.instance("pool").model.calibrator._window   # pylint: disable=protected-access
    assert samples[-1]["pv_counter_kwh"] is None
    assert samples[-1]["solar_w"] > 0


# --- which setting is actually holding it back ------------------------------------------

def test_the_limit_named_is_one_that_would_free_something(make_manager, installation):
    """
    Ranked by what relaxing it would free, not by how many slots it excluded.

    On a live pool 56 slots read "too cold to run" and every one of them was also
    dearer than the price limit. The card sent its owner to lower the cold cut-off,
    which would have run the pump in freezing air and gained nothing - the price cap
    was what cost them 35 kWh.
    """
    manager = make_manager([dict(POOL, min_ambient_temp_c=12.0, max_price_ct_kwh=25.0)])
    manager.external_scheduler = True
    # Dear everywhere, so nothing the cold rule blocks could have been afforded.
    installation.prices = [0.00040] * 48
    installation.temperature = [5.0] * 48
    load = _run(manager, installation, water_c=20.0)

    summary = load.plan_summary()
    if summary and summary.get("limited_by"):
        assert summary["limited_by"] != "too cold to run", (
            "named a rule whose relaxation frees nothing: " + str(summary["counts"])
        )


def test_a_blocker_hiding_affordable_slots_is_still_named(make_manager, installation):
    """The other half: where the slots behind a rule *are* affordable, say so."""
    manager = make_manager([dict(POOL, window_start=10, window_end=12,
                                 max_price_ct_kwh=40.0)])
    manager.external_scheduler = True
    installation.prices = [0.00010] * 48          # everything well under the cap
    load = _run(manager, installation, water_c=20.0)

    summary = load.plan_summary()
    assert summary is not None
    if summary.get("limited_by"):
        assert summary["limited_by"] == "outside allowed hours", summary["counts"]


def test_one_affordable_slot_does_not_outvote_a_hundred_unaffordable(
    make_manager, installation
):
    """
    A pool short of 45 kWh was told it was "limited by costs more than it is worth
    (1 slot)" - the single affordable slot some other rule had blocked, outranking the
    119 the price cap had put out of reach. Price ranks on the same footing now, so
    the count it carries is the whole of what is unaffordable.
    """
    manager = make_manager([dict(POOL, max_price_ct_kwh=25.0)])
    manager.external_scheduler = True
    installation.prices = [0.00045] * 48        # every slot far above the cap
    load = _run(manager, installation, water_c=20.0)

    summary = load.plan_summary()
    if summary and summary.get("limited_by"):
        assert summary["limited_by"] == "above price cap", summary["counts"]
        # And it reports the scale of the problem, not a single slot.
        assert summary["limited_slots"] > 1, summary


def test_a_deferred_cycle_does_not_overwrite_the_adopted_plan(make_manager, installation):
    """
    Two planners were writing the same field in turn.

    While the optimizer places a load, every manager cycle still runs the fallback
    planner - deliberately, so something is ready if no schedule arrives - but it
    stored that placement in `last_plan`, the field the adopted schedule uses and the
    card and API publish. The optimizer wrote its answer back a minute later, and the
    two alternated: on a live pool, 21 slots and 8.4 kWh appearing and vanishing every
    couple of minutes with the inputs unchanged.
    """
    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)
    load = manager.instance("pool")
    ctx = manager._last_ctx                        # pylint: disable=protected-access

    adopted = [0.0] * ctx.slot_count
    adopted[ctx.current_slot] = 1500.0
    manager.adopt_schedules({"pool": adopted})
    assert load.last_plan[ctx.current_slot] == 1500.0

    # A cycle with the optimizer still in charge must leave that alone.
    _run(manager, installation, water_c=20.0)
    assert load.last_plan[ctx.current_slot] == 1500.0, (
        "the fallback planner overwrote the adopted schedule"
    )


def test_before_any_schedule_arrives_the_fallback_still_shows(make_manager, installation):
    """
    The other half. With nothing adopted yet there is nothing to protect, and a card
    with no plan at all would be worse than one showing what the load would do by
    itself.
    """
    manager = make_manager([POOL])
    manager.external_scheduler = True
    load = _run(manager, installation, water_c=20.0)
    assert any(load.last_plan), "no plan at all before the first schedule"


def test_a_stale_schedule_lets_the_fallback_take_over_again(make_manager, installation):
    """When the optimizer goes quiet the load must be able to decide for itself."""
    from datetime import timedelta

    manager = make_manager([POOL])
    manager.external_scheduler = True
    _run(manager, installation, water_c=20.0)
    load = manager.instance("pool")
    ctx = manager._last_ctx                        # pylint: disable=protected-access

    manager.adopt_schedules({"pool": [0.0] * ctx.slot_count})
    load.last_schedule_at = ctx.now - timedelta(hours=2)      # long gone quiet

    _run(manager, installation, water_c=20.0)
    assert any(load.last_plan), "the fallback never took over"


# --- profiles other than the pool --------------------------------------------------------

SAUNA = dict(POOL, id="sauna", type="sauna", volume_m3=0.08, surface_m2=12.0,
             rated_power_w=6000.0, target_temp=90.0, min_ambient_temp_c=None,
             deadline_hours=6, min_runtime_minutes=15)


def test_a_deadline_bounds_what_the_store_is_asked_to_hold(make_manager, installation):
    """
    A store with a deadline has to be at target *by* then, not held there for two
    days. Integrating the standing loss to the end of the horizon regardless asked a
    sauna for 177 kWh - 29 hours of running in a 42 hour horizon - when what it wanted
    was to be hot in six. Invisible on a pool, whose losses really do run all horizon.
    """
    manager = make_manager([SAUNA])
    _run(manager, installation, water_c=85.0)

    demand = manager.instance("sauna").last_demand
    hours_at_rated = demand.total_wh / 6000.0
    assert hours_at_rated < 8, f"{demand.total_wh / 1000:.1f} kWh is not a session"


def test_no_deadline_still_holds_for_the_whole_horizon(make_manager, installation):
    """A pool has no deadline and genuinely does leak all horizon - unchanged."""
    manager = make_manager([POOL])
    installation.temperature = [10.0] * 48
    _run(manager, installation, water_c=24.0)

    detail = manager.instance("pool").last_demand.detail
    assert detail["standing_losses_wh_thermal"] > detail["heat_up_wh_thermal"]


def test_the_projection_stops_where_the_appliance_does(make_manager, installation):
    """
    It integrates the plan, and the appliance stops at target. Without that a 6 kW
    heater in 80 litres was drawn reaching 120 C against a 90 C setting - a slot is
    16 K for a sauna where it is four hundredths of a kelvin for a pool.
    """
    manager = make_manager([SAUNA])
    manager.external_scheduler = True
    installation.sensors["sensor.pool_water"] = 20.0
    manager.run_cycle()
    load = manager.instance("sauna")
    ctx = manager._last_ctx_for["sauna"]           # pylint: disable=protected-access

    plan = [0.0] * ctx.slot_count
    for index in range(ctx.current_slot, min(ctx.current_slot + 8, ctx.slot_count)):
        plan[index] = 6000.0 * ctx.hours_per_slot()
    manager.adopt_schedules({"sauna": plan})

    projected = [v for v in load.model.project_medium(ctx, load.last_plan, 20.0)
                 if v is not None]
    assert max(projected) <= 90.0 + 1e-6, f"overshot to {max(projected):.1f}"
    assert max(projected) > 80.0, "it should still reach the target"
