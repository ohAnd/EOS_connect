"""
Space heating: a house as the store, configured from what its owner knows.

Three things are tested here that the pool never needed: starting values derived from
a description of the building, a calibration that holds the COP when the room
temperature cannot separate it from the sun, and a heat meter that measures it instead.
The simulated house below heats while the sun shines, the way a PV-led plan makes it.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.loads.models import building
from src.loads.models.calibration import (
    LOSS_COEFFICIENT_MIN,
    ThermalCalibrator,
)
from src.loads.models.thermal_physics import WH_PER_M3_PER_K, cop_at
from src.loads.presets import (
    PRESETS,
    TYPE_SPACE_HEATING,
    apply_defaults,
    build_model,
)

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)

# The measured house: 1960s, masonry, renovated, floor heating, 170 m2.
MEASURED_HOUSE = {
    "id": "house",
    "type": TYPE_SPACE_HEATING,
    "living_area_m2": 170,
    "construction": "massive",
    "emitter": "floor",
    "building_form": "detached",
    "insulation": "renovated",
}


# -- starting values -----------------------------------------------------------------


def test_the_measured_house_starts_from_its_measured_neighbourhood():
    values = building.starting_values(MEASURED_HOUSE)
    # 125 Wh/(K m2) x 170 m2 = 21.25 kWh/K, written as water.
    assert values["volume_m3"] == pytest.approx(21250 / WH_PER_M3_PER_K, abs=0.01)
    assert values["surface_m2"] == pytest.approx(408.0)
    assert values["heat_loss_w_per_m2_k"] == pytest.approx(0.28)
    # Its heat meter read 3.42 at 0 C and 3.98 at 7 C.
    assert cop_at(0, values["cop_nominal"], values["cop_air_coeff"]) == pytest.approx(
        3.42, abs=0.1
    )
    assert cop_at(7, values["cop_nominal"], values["cop_air_coeff"]) == pytest.approx(
        3.98, abs=0.1
    )


def test_a_light_house_with_radiators_stores_less_and_runs_less_efficiently():
    heavy = building.starting_values(MEASURED_HOUSE)
    light = building.starting_values(
        {**MEASURED_HOUSE, "construction": "light", "emitter": "radiators"}
    )
    assert light["volume_m3"] < heavy["volume_m3"] / 2
    assert cop_at(7, light["cop_nominal"], light["cop_air_coeff"]) < cop_at(
        7, heavy["cop_nominal"], heavy["cop_air_coeff"]
    )


@pytest.mark.parametrize(
    "insulation, expected", sorted(building.LOSS_W_PER_M2_K.items())
)
def test_every_insulation_class_has_a_loss(insulation, expected):
    values = building.starting_values({**MEASURED_HOUSE, "insulation": insulation})
    assert values["heat_loss_w_per_m2_k"] == expected


def test_older_houses_lose_more():
    order = ["new_build", "renovated", "1995_2015", "1979_1994", "before_1979"]
    losses = [building.LOSS_W_PER_M2_K[name] for name in order]
    assert losses == sorted(losses)


def test_a_flat_loses_through_less_surface_than_a_detached_house():
    flat = building.starting_values({**MEASURED_HOUSE, "building_form": "apartment"})
    assert flat["surface_m2"] == pytest.approx(170.0)


@pytest.mark.parametrize("bad", [None, "", "castle", 0, -5])
def test_nonsense_falls_back_to_the_default_inputs(bad):
    values = building.starting_values({"living_area_m2": bad, "insulation": bad})
    expected = building.starting_values(building.DEFAULT_INPUTS)
    assert values == expected


# -- the preset --------------------------------------------------------------------


def test_the_derived_values_follow_the_entry_not_the_documented_default():
    resolved = apply_defaults({**MEASURED_HOUSE, "living_area_m2": 200})
    assert resolved["volume_m3"] == pytest.approx(200 * 125 / WH_PER_M3_PER_K, abs=0.01)
    assert resolved["surface_m2"] == pytest.approx(480.0)


def test_an_explicit_value_wins_over_the_derived_one():
    resolved = apply_defaults({**MEASURED_HOUSE, "surface_m2": 350.0})
    assert resolved["surface_m2"] == 350.0
    # ...and only that one: the rest still follow the description.
    assert resolved["volume_m3"] == pytest.approx(21250 / WH_PER_M3_PER_K, abs=0.01)


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_derived_value_counts_as_unset(empty):
    """A form that saves an empty field must not produce a house with no volume."""
    resolved = apply_defaults({**MEASURED_HOUSE, "volume_m3": empty})
    assert resolved["volume_m3"] > 0


def test_an_explicit_none_still_means_no_limit_for_ordinary_fields():
    resolved = apply_defaults({**MEASURED_HOUSE, "window_start": None})
    assert resolved["window_start"] is None


def test_the_documented_defaults_are_what_the_default_inputs_produce():
    """The docs table shows these, so they must not drift from the derivation."""
    defaults = PRESETS[TYPE_SPACE_HEATING]["defaults"]
    for key, value in building.starting_values(building.DEFAULT_INPUTS).items():
        assert defaults[key] == value


def test_a_house_reads_the_outdoor_forecast_and_runs_in_the_cold():
    resolved = apply_defaults(MEASURED_HOUSE)
    assert PRESETS[TYPE_SPACE_HEATING]["ambient"] == "outdoor"
    assert resolved["min_ambient_temp_c"] is None
    assert resolved["window_start"] is None and resolved["window_end"] is None


def test_the_house_model_carries_the_house_calibration_rules():
    model = build_model(apply_defaults(MEASURED_HOUSE))
    assert model.calibrator.learn_cop is False
    assert model.calibrator.loss_min < 0.25
    assert model.calibrator.min_window_change_k == pytest.approx(0.2)


def test_a_reset_keeps_the_house_rules():
    model = build_model(apply_defaults(MEASURED_HOUSE))
    model.reset_calibration()
    assert model.calibrator.learn_cop is False
    assert model.calibrator.loss_min < 0.25


def test_a_pool_keeps_the_rules_it_always_had():
    model = build_model(apply_defaults({"id": "pool", "type": "pool_heatpump"}))
    assert model.calibrator.learn_cop is True
    assert model.calibrator.loss_min == LOSS_COEFFICIENT_MIN
    assert model.calibrator.min_window_change_k == 0.0


# -- a simulated house -------------------------------------------------------------

TRUE_C_WH_PER_K = 21000.0
TRUE_SURFACE = 400.0
TRUE_K = 0.26
TRUE_COP_NOMINAL = 5.6
TRUE_AIR_COEFF = 0.0155
TRUE_SOLAR = 0.4
RUN_POWER_W = 900.0


def _ambient(moment):
    hour = moment.hour + moment.minute / 60.0
    return 6.0 + 4.0 * math.sin(math.pi * (hour - 9) / 12.0)


def _sun(moment):
    hour = moment.hour + moment.minute / 60.0
    if not 8 <= hour <= 17:
        return 0.0
    return 6000.0 * math.sin(math.pi * (hour - 8) / 9.0)


def _heating(moment, temp, running):
    """
    Runs while the sun shines and the house is not full - a PV-led plan - and
    otherwise like a thermostat with a half-kelvin hysteresis.
    """
    if 11 <= moment.hour < 15:
        return temp < 22.5
    return temp < 21.2 if running else temp < 20.7


def _simulate(days, step_minutes=5, heat_meter=False):
    samples = []
    step_h = step_minutes / 60.0
    moment, temp, heat_kwh, running = T0, 21.0, 0.0, False
    for _ in range(int(days * 24 / step_h)):
        running = _heating(moment, temp, running)
        power = RUN_POWER_W if running else 10.0
        ambient = _ambient(moment)
        samples.append(
            {
                "timestamp": moment,
                "medium_c": round(temp, 2),
                "ambient_c": ambient,
                "power_w": power,
                "solar_w": _sun(moment),
                "heat_counter_kwh": round(heat_kwh, 2) if heat_meter else None,
            }
        )
        thermal = (
            power * cop_at(ambient, TRUE_COP_NOMINAL, TRUE_AIR_COEFF)
            if running
            else 0.0
        )
        loss = TRUE_K * TRUE_SURFACE * (temp - ambient)
        temp += (thermal + TRUE_SOLAR * _sun(moment) - loss) * step_h / TRUE_C_WH_PER_K
        heat_kwh += thermal * step_h / 1000.0
        moment += timedelta(minutes=step_minutes)
    return samples


def _house_calibrator(
    cop_nominal=TRUE_COP_NOMINAL, air=TRUE_AIR_COEFF, loss=0.4, **options
):
    rules = dict(PRESETS[TYPE_SPACE_HEATING]["calibration"])
    rules.update(options)
    return ThermalCalibrator(
        volume_m3=TRUE_C_WH_PER_K / WH_PER_M3_PER_K,
        surface_m2=TRUE_SURFACE,
        loss_coefficient=loss,
        cop_nominal=cop_nominal,
        air_coefficient=air,
        **rules,
    )


def test_a_held_cop_stays_where_it_was_put():
    calibrator = _house_calibrator(cop_nominal=5.0, air=0.02)
    calibrator.observe_series(_simulate(days=5))
    assert calibrator.cop_nominal == pytest.approx(5.0)
    assert calibrator.air_coefficient == pytest.approx(0.02)
    assert calibrator.cop_source == "configured"


def test_with_the_cop_held_the_losses_and_the_sun_are_learned():
    calibrator = _house_calibrator(loss=0.4)
    calibrator.observe_series(_simulate(days=5))
    assert calibrator.loss_coefficient == pytest.approx(TRUE_K, rel=0.15)
    assert calibrator.solar_identified
    assert calibrator.solar_gain == pytest.approx(TRUE_SOLAR, rel=0.3)


def test_the_house_loss_is_not_clamped_to_the_pool_floor():
    calibrator = _house_calibrator(loss=0.4)
    calibrator.observe_series(_simulate(days=5))
    assert calibrator.loss_coefficient < 0.5


def test_the_pool_floor_would_have_clamped_it():
    """What the house got before the band became a parameter."""
    calibrator = _house_calibrator(
        loss=0.4, loss_coefficient_min=LOSS_COEFFICIENT_MIN, loss_coefficient_max=200.0
    )
    calibrator.observe_series(_simulate(days=5))
    assert calibrator.loss_coefficient == pytest.approx(LOSS_COEFFICIENT_MIN)


def test_a_heat_meter_measures_the_cop_whatever_was_configured():
    calibrator = _house_calibrator(cop_nominal=4.0, air=0.03)
    calibrator.observe_series(_simulate(days=5, heat_meter=True))
    assert calibrator.cop_source == "heat_meter"
    truth = cop_at(6.0, TRUE_COP_NOMINAL, TRUE_AIR_COEFF)
    assert cop_at(6.0, calibrator.cop_nominal, calibrator.air_coefficient) == (
        pytest.approx(truth, rel=0.05)
    )


def test_a_heat_meter_also_measures_where_the_cop_may_be_fitted():
    calibrator = _house_calibrator(cop_nominal=4.0, air=0.03, learn_cop=True)
    calibrator.observe_series(_simulate(days=5, heat_meter=True))
    assert calibrator.cop_source == "heat_meter"


def test_a_heat_meter_that_never_moves_is_ignored():
    samples = _simulate(days=3, heat_meter=True)
    for sample in samples:
        sample["heat_counter_kwh"] = 1234.0
    calibrator = _house_calibrator(cop_nominal=5.0, air=0.02)
    calibrator.observe_series(samples)
    assert calibrator.cop_source == "configured"
    assert calibrator.cop_nominal == pytest.approx(5.0)


def test_a_house_window_waits_for_a_real_change():
    calibrator = _house_calibrator()
    start = T0
    for minute in range(0, 60, 5):
        calibrator.observe(
            {
                "timestamp": start + timedelta(minutes=minute),
                # 0.01 K steps: three of them would close a pool's window.
                "medium_c": 21.0 + 0.01 * (minute // 5),
                "ambient_c": 5.0,
                "power_w": 0.0,
            },
            refit=False,
        )
    assert calibrator.loss_samples == 0


def test_the_state_reports_where_the_cop_came_from():
    calibrator = _house_calibrator()
    calibrator.observe_series(_simulate(days=2))
    assert calibrator.state()["cop_source"] == "configured"


def test_a_restored_cop_is_ignored_where_it_may_not_be_learned():
    calibrator = _house_calibrator(cop_nominal=5.6)
    calibrator.restore(
        {"cop_nominal": 2.0, "air_coefficient": 0.05, "cop_source": "fit"}
    )
    assert calibrator.cop_nominal == pytest.approx(5.6)


def test_a_measured_cop_survives_a_restart():
    calibrator = _house_calibrator(cop_nominal=5.6)
    calibrator.restore(
        {"cop_nominal": 6.0, "air_coefficient": 0.02, "cop_source": "heat_meter"}
    )
    assert calibrator.cop_nominal == pytest.approx(6.0)
