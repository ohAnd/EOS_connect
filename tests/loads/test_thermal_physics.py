"""The physics, checked against numbers you can derive by hand."""

import pytest

from src.loads.models.thermal_physics import (
    COP_MAX,
    COP_MIN,
    COP_REFERENCE_AMBIENT_C,
    WH_PER_M3_PER_K,
    cop_at,
    energy_to_raise_wh,
    loss_power_w,
    observed_cop,
    observed_loss_coefficient,
    thermal_to_electrical_wh,
)


def test_specific_heat_of_water_matches_the_textbook():
    """1000 kg x 4186 J/(kg K) = 4.186 MJ/K = 1163 Wh/K."""
    assert WH_PER_M3_PER_K == pytest.approx(1000 * 4186 / 3600, rel=1e-3)


def test_heating_a_pool_by_one_degree():
    """A 30 m3 pool needs ~34.9 kWh thermal per kelvin."""
    assert energy_to_raise_wh(30, 1) == pytest.approx(34890.0)
    assert energy_to_raise_wh(30, 2.5) == pytest.approx(87225.0)


@pytest.mark.parametrize("volume,delta", [(0, 5), (30, 0), (30, -3), (-1, 5)])
def test_no_energy_is_needed_to_cool_down_or_heat_nothing(volume, delta):
    assert energy_to_raise_wh(volume, delta) == 0.0


def test_loss_scales_with_area_and_temperature_difference():
    assert loss_power_w(25, 32, 28, 18) == pytest.approx(8000.0)
    assert loss_power_w(25, 64, 28, 18) == pytest.approx(16000.0)
    assert loss_power_w(25, 32, 28, 23) == pytest.approx(4000.0)


def test_a_cover_reduces_the_loss():
    uncovered = loss_power_w(25, 32, 28, 18, cover_factor=1.0)
    covered = loss_power_w(25, 32, 28, 18, cover_factor=0.3)
    assert covered == pytest.approx(uncovered * 0.3)


def test_a_pool_warmer_outside_than_in_gains_heat():
    """On a hot afternoon an uncovered pool genuinely heats itself."""
    assert loss_power_w(25, 32, 22, 30) < 0


def test_cop_is_nominal_at_the_reference_temperature():
    assert cop_at(COP_REFERENCE_AMBIENT_C, 5.0, 0.02) == pytest.approx(5.0)


def test_cop_falls_with_ambient_temperature():
    warm = cop_at(26, 5.0, 0.02)
    cold = cop_at(6, 5.0, 0.02)
    assert cold < warm
    assert cold == pytest.approx(5.0 * (1 - 0.4))


@pytest.mark.parametrize("ambient,nominal,coeff", [(-40, 5.0, 0.05), (60, 5.0, 0.05)])
def test_cop_is_clamped_to_a_plausible_band(ambient, nominal, coeff):
    assert COP_MIN <= cop_at(ambient, nominal, coeff) <= COP_MAX


def test_a_non_numeric_ambient_falls_back_to_the_reference():
    assert cop_at(float("nan"), 4.0, 0.02) == pytest.approx(4.0)


def test_electrical_energy_is_thermal_over_cop():
    assert thermal_to_electrical_wh(10000, 5.0) == pytest.approx(2000.0)
    assert thermal_to_electrical_wh(0, 5.0) == 0.0


def test_a_nonsense_cop_cannot_produce_infinite_demand():
    assert thermal_to_electrical_wh(10000, 0.0) == pytest.approx(10000 / COP_MIN)


def test_the_loss_coefficient_is_recovered_from_a_cooling_stretch():
    """A pool losing exactly what k=25 predicts must read back as k=25."""
    volume, surface, k = 30.0, 32.0, 25.0
    medium, ambient = 28.0, 18.0
    hours = 1.0
    loss_w = k * surface * (medium - ambient)
    delta_k = -loss_w * hours / (WH_PER_M3_PER_K * volume)

    recovered = observed_loss_coefficient(
        volume, surface, delta_k, hours, medium, ambient
    )
    assert recovered == pytest.approx(k, rel=1e-6)


@pytest.mark.parametrize("kwargs", [
    {"hours": 0},
    {"delta_temp_k": 0.5},          # warming, not cooling
    {"medium_c": 18.4},             # no driving temperature difference
])
def test_useless_cooling_samples_are_rejected(kwargs):
    base = dict(
        volume_m3=30.0, surface_m2=32.0, delta_temp_k=-0.2, hours=1.0,
        medium_c=28.0, ambient_c=18.0,
    )
    base.update(kwargs)
    assert observed_loss_coefficient(**base) is None


def test_the_cop_is_recovered_from_a_heating_stretch():
    """Losses during heating are added back, so a leaky pool is not a bad heat pump."""
    volume, hours, power_w, cop = 30.0, 1.0, 2000.0, 4.5
    loss_w = 800.0
    thermal_w = power_w * cop
    delta_k = (thermal_w - loss_w) * hours / (WH_PER_M3_PER_K * volume)

    recovered = observed_cop(volume, delta_k, hours, power_w, loss_w)
    assert recovered == pytest.approx(cop, rel=1e-6)


@pytest.mark.parametrize("kwargs", [
    {"electrical_w": 0.0},
    {"hours": 0.0},
    {"delta_temp_k": -5.0, "loss_w": 0.0},   # cooling while drawing power
])
def test_useless_heating_samples_are_rejected(kwargs):
    base = dict(
        volume_m3=30.0, delta_temp_k=0.2, hours=1.0, electrical_w=2000.0, loss_w=500.0
    )
    base.update(kwargs)
    assert observed_cop(**base) is None
