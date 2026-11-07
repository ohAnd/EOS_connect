"""
A managed load whose meter never falls to zero: the standby threshold.

A heat pump's circulation pumps, controls and crank heater keep drawing 80-120 W after
the compressor has stopped. The meter that carries the appliance stands outside the
household base load - a subtracted sensor loses the whole history, an EVCC loadpoint
series is netted off `group:home` wholesale - so without help that permanent draw
vanished from the forecast, while the bare 50 W classification read the idling
appliance as compressor-on around the clock: the release gate latched, and the
calibration fitted COPs to windows that were only standby.

What these tests pin: the threshold splits the meter's readings, the split keeps the
draw in the household forecast beside the plan rather than stacked onto it, and an
install without a threshold behaves exactly as before.
"""

from datetime import datetime, timedelta

import pytest

from src.loads.contribution import KIND_PROFILE
from src.loads.manager import (
    STANDBY_SUFFIX,
    ManagedLoadManager,
    ManagedLoadSources,
    standby_contribution_id,
)
from src.loads.models.calibration import (
    IDLE_POWER_W,
    ThermalCalibrator,
)
from src.loads.models.thermal import ThermalStorageModel
from src.loads.presets import apply_defaults
from tests.loads.conftest import NOW

BERLIN = NOW.tzinfo
SLOTS = 48


def tank(**overrides):
    """The heat pump of the install that surfaced this, with its standby draw."""
    return apply_defaults(dict(
        {
            "id": "heatpumpt_heating",
            "type": "buffer_tank",
            "enabled": True,
            "temp_sensor": "sensor.dhw",
            "power_sensor": "sensor.heatsink_power",
            "volume_m3": 0.64,
            "surface_m2": 7.0,
            "rated_power_w": 3500.0,
            "target_temp": 45.0,
            "cop_nominal": 3.0,
        },
        **overrides
    ))


def make_manager(entries, **overrides):
    """A manager over sensors carrying the user's numbers."""
    sources = ManagedLoadSources(
        read_sensor=lambda name: {
            "sensor.dhw": 38.0,
            # what the meter reads while the compressor is off
            "sensor.heatsink_power": 100.0,
            "group:loadpoint:Wärmepumpe": 100.0,
        }.get(name),
        price=lambda: [0.0003] * SLOTS,
        feed_in_price=lambda: [0.00008] * SLOTS,
        pv_forecast=lambda: [0.0] * SLOTS,
        base_load=lambda: [400.0] * SLOTS,
        temperature_forecast=lambda: [10.0] * 48,
    )
    manager = ManagedLoadManager(
        entries, time_frame_base=900, time_zone=BERLIN, cycle_seconds=30,
        horizon_hours=12, sources=sources, clock=lambda: NOW,
    )
    for key, value in overrides.items():
        setattr(manager, key, value)
    return manager


# --- the split the threshold makes -------------------------------------------------------

def test_without_a_threshold_the_gate_reads_standby_as_running():
    """The old behaviour: 100 W of circulation pumps is above the 50 W guess."""
    manager = make_manager([tank()])
    manager.run_cycle()
    assert manager.instance("heatpumpt_heating").last_demand.detail["running"] is True


def test_a_threshold_below_the_standby_draw_reads_the_appliance_as_off():
    manager = make_manager([tank(standby_threshold_w=150.0)])
    manager.run_cycle()
    load = manager.instance("heatpumpt_heating")
    assert load.last_demand.detail["running"] is False


def test_a_running_compressor_is_still_read_as_running_over_the_threshold():
    manager = make_manager([tank(standby_threshold_w=150.0)])
    manager.sources.read_sensor = lambda name: (
        2000.0 if name == "sensor.heatsink_power" else 38.0
    )
    manager.run_cycle()
    assert manager.instance("heatpumpt_heating").last_demand.detail["running"] is True


# --- calibration --------------------------------------------------------------------------

def test_calibration_windows_classify_by_the_configured_threshold():
    cal = ThermalCalibrator(0.64, 7.0, 25.0, 3.0, idle_power_w=150.0)
    assert cal.idle_power_w == 150.0
    assert cal._is_running_reading(100.0) is False
    assert cal._is_running_reading(150.0) is True
    assert cal._is_running_reading(2000.0) is True


def test_calibration_default_keeps_the_old_guess():
    cal = ThermalCalibrator(0.64, 7.0, 25.0, 3.0)
    assert cal.idle_power_w == IDLE_POWER_W
    # Zero and a blank mean "not configured", which is the old behaviour.
    cal.set_idle_threshold(0)
    assert cal.idle_power_w == IDLE_POWER_W


def test_an_idle_standby_window_stays_out_of_the_cop_term():
    """
    The meters read 100 W the whole hour and the store cools; the window that describes
    that hour is a *loss* observation at zero electrical input, not a COP of the store
    heating itself from circulation pumps.
    """
    cal = ThermalCalibrator(0.64, 7.0, 25.0, 3.0, idle_power_w=150.0)
    base = datetime(2026, 11, 2, 0, 0, tzinfo=BERLIN)
    samples = []
    temp = 40.0
    for index in range(13):        # three hours of pure standby
        samples.append({
            "timestamp": base + timedelta(minutes=15 * index),
            "medium_c": temp,
            "ambient_c": 20.0,
            "power_w": 100.0,
            "covered": False,
            "cover_factor": 1.0,
            "solar_w": 0.0,
        })
        # cooling only: 100 W of standby puts nothing measurable into the store
        temp -= 0.05
    cal.observe_series(samples)
    row = cal._rows[-1]
    assert row["power_w"] == 0.0
    assert cal.cop_samples == 0


# --- the household forecast ---------------------------------------------------------------

def test_the_standby_draw_joins_the_household_forecast():
    """On a meter that stands out of the base load, idle watts must not be lost."""
    manager = make_manager([tank(
        standby_threshold_w=150.0, power_sensor="group:loadpoint:Wärmepumpe",
    )])
    manager.run_cycle()

    contribution = manager.registry.get(standby_contribution_id("heatpumpt_heating"))
    assert contribution is not None
    assert contribution.kind == KIND_PROFILE
    # 100 W over a 15-minute slot
    assert contribution.slots_wh[0] == pytest.approx(25.0, rel=0.01)


def test_the_standby_draw_fills_only_the_unscheduled_slots():
    """A slot the appliance is planned in already carries its whole draw."""
    manager = make_manager([tank(
        standby_threshold_w=150.0, power_sensor="group:loadpoint:Wärmepumpe",
        target_temp=80.0,             # a store far below target so a plan exists
    )])
    # Replace the model's demand with something the planner is sure to place, so the
    # interplay of the two series is exercised rather than the thermal model.
    manager.run_cycle()
    item = manager.instance("heatpumpt_heating")
    # Simulate a plan that occupies the current slot.
    item.last_plan = [875.0] + [0.0] * (len(item.last_plan) - 1)
    ctx = manager._last_ctx_for["heatpumpt_heating"]
    manager._publish_standby(item, ctx)

    contribution = manager.registry.get(standby_contribution_id("heatpumpt_heating"))
    assert contribution is not None
    assert contribution.slots_wh[0] == 0.0          # planned slot: the plan carries it
    assert contribution.slots_wh[1] == pytest.approx(25.0, rel=0.01)


def test_no_standby_publishes_nothing():
    """The feature is off; the meter is part of the base load and drawn from there."""
    manager = make_manager([tank(power_sensor="group:loadpoint:Wärmepumpe")])
    manager.run_cycle()
    assert manager.registry.get(standby_contribution_id("heatpumpt_heating")) is None


def test_a_subtracted_sensor_publishes_standby_too():
    """The subtract path loses the idle draw exactly as the EVCC one does."""
    manager = make_manager([tank(standby_threshold_w=150.0)])
    assert manager.instance("heatpumpt_heating").metered_out_of_base_load() is True
    manager.run_cycle()
    contribution = manager.registry.get(standby_contribution_id("heatpumpt_heating"))
    assert contribution is not None
    assert contribution.slots_wh[0] == pytest.approx(25.0, rel=0.01)


def test_a_meter_inside_the_base_load_needs_no_standby_term():
    manager = make_manager([tank(standby_threshold_w=150.0, subtract_from_base_load=False)])
    manager.run_cycle()
    assert manager.registry.get(standby_contribution_id("heatpumpt_heating")) is None


def test_the_standby_series_survives_the_load_being_offered_to_the_solver():
    """
    The contingent energy is the solver's to place, and is out of the forecast while it
    is offered - but the standby draw is a *household* term and stays in it.
    """
    manager = make_manager([tank(
        standby_threshold_w=150.0, power_sensor="group:loadpoint:Wärmepumpe",
    )])
    manager.external_scheduler = True
    manager.run_cycle()

    assert manager.registry.get("heatpumpt_heating") is None
    contribution = manager.registry.get(standby_contribution_id("heatpumpt_heating"))
    assert contribution is not None
    assert contribution.slots_wh[0] == pytest.approx(25.0, rel=0.01)


def test_disabling_the_load_drops_the_standby_draw():
    manager = make_manager([tank(
        standby_threshold_w=150.0, power_sensor="group:loadpoint:Wärmepumpe",
    )])
    manager.run_cycle()
    assert manager.registry.get(standby_contribution_id("heatpumpt_heating")) is not None

    manager.instance("heatpumpt_heating").enabled = False
    manager.run_cycle()
    assert manager.registry.get(standby_contribution_id("heatpumpt_heating")) is None


def test_the_measured_standby_is_kept_while_the_compressor_runs():
    """The draw does not vanish because the appliance started."""
    manager = make_manager([tank(
        standby_threshold_w=150.0, power_sensor="group:loadpoint:Wärmepumpe",
    )])
    manager.run_cycle()
    assert manager.instance("heatpumpt_heating").last_standby_w == pytest.approx(100.0)

    def read(name):
        return {"sensor.dhw": 38.0, "group:loadpoint:Wärmepumpe": 2000.0}.get(name)
    manager.sources.read_sensor = read
    manager.run_cycle()
    item = manager.instance("heatpumpt_heating")
    assert item.last_standby_w == pytest.approx(100.0)
    contribution = manager.registry.get(standby_contribution_id("heatpumpt_heating"))
    # The appliance is scheduled now: those slots carry the plan, the rest the standby.
    assert 0.0 <= max(contribution.slots_wh) <= 25.0 + 1e-6


def test_the_adopted_schedule_realigns_the_standby_draw():
    """
    The schedule and the standby term must move in the same breath. A slot the solver
    newly claimed keeps carrying its standby Wh until the next poll otherwise, and the
    household double-counts those watts for up to a whole cycle.
    """
    manager = make_manager([tank(
        standby_threshold_w=150.0, power_sensor="group:loadpoint:Wärmepumpe",
        target_temp=80.0,
    )])
    manager.external_scheduler = True
    manager.run_cycle()
    item = manager.instance("heatpumpt_heating")
    ctx = manager._last_ctx_for["heatpumpt_heating"]

    schedule = [0.0] * ctx.slot_count
    schedule[ctx.current_slot + 1] = 875.0
    manager.adopt_schedules({"heatpumpt_heating": schedule})

    contribution = manager.registry.get(standby_contribution_id("heatpumpt_heating"))
    assert contribution.slots_wh[ctx.current_slot + 1] == 0.0
    assert contribution.slots_wh[ctx.current_slot + 2] == pytest.approx(25.0, rel=0.01)


def test_schedules_never_carry_the_standby_key():
    """The solver's schedule map is keyed by the appliance, not by the standby term."""
    manager = make_manager([tank(standby_threshold_w=150.0)])
    manager.external_scheduler = True
    manager.run_cycle()
    records = manager.schedulable()
    assert all(record["id"] != f"heatpumpt_heating{STANDBY_SUFFIX}" for record in records)


def test_hot_reload_re_thresholds_the_fit_in_place():
    manager = make_manager([tank(standby_threshold_w=0.0)])
    manager.run_cycle()
    item = manager.instance("heatpumpt_heating")
    assert item.model.calibrator.idle_power_w == IDLE_POWER_W

    item.reconfigure(apply_defaults(tank(standby_threshold_w=150.0)))
    assert item.model.calibrator.idle_power_w == 150.0
    assert item.model.standby_threshold_w == 150.0