"""
Unit tests for the BatteryInterface class in src.interfaces.battery_interface.

This module contains tests for initialization, SOC fetching, error handling,
and control methods of the BatteryInterface.
"""

from unittest.mock import patch, MagicMock
import pytest
import requests
from src.interfaces.battery_interface import BatteryInterface

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


@pytest.fixture
def default_config():
    """
    Returns a default configuration dictionary for BatteryInterface.
    """
    return {
        "source": "default",
        "url": "",
        "soc_sensor": "",
        "max_charge_power_w": 3000,
        "capacity_wh": 10000,
        "min_soc_percentage": 10,
        "max_soc_percentage": 90,
        "charging_curve_enabled": True,
        "discharge_efficiency": 1.0,
        "price_euro_per_wh_accu": 0.0,
        "price_euro_per_wh_sensor": "",
    }


@pytest.fixture
def fast_battery_interface(default_config):
    """
    Fixture that patches BatteryInterface to skip background thread startup for faster tests.
    Usage: Pass 'fast_battery_interface' as parameter instead of creating instances directly.
    """
    with patch.object(BatteryInterface, "start_update_service", return_value=None):
        bi = BatteryInterface(default_config)
        yield bi
        # No actual thread to shutdown
        bi._update_thread = None  # Prevent shutdown() from attempting thread join


def test_init_sets_attributes(default_config):
    """
    Test that BatteryInterface initialization sets attributes correctly.
    """
    bi = BatteryInterface(default_config)
    assert bi.src == "default"
    assert bi.max_charge_power_fix == 3000
    assert bi.min_soc_set == 10
    assert bi.max_soc_set == 90


def test_default_source_sets_soc_to_5(default_config):
    """
    Test that the default source sets SOC to 5.
    """
    bi = BatteryInterface(default_config)
    soc = bi._BatteryInterface__battery_request_current_soc()
    assert soc == 5


def test_openhab_fetch_success(default_config):
    """
    Test successful SOC fetch from OpenHAB.
    """
    test_config = default_config.copy()
    test_config["source"] = "openhab"
    test_config["url"] = "http://fake"
    test_config["soc_sensor"] = "BatterySOC"
    bi = BatteryInterface(test_config)
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "80"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        soc = bi._BatteryInterface__fetch_soc_data_unified()
        assert soc == 80


def test_openhab_fetch_decimal_format(default_config):
    """
    Test SOC fetch from OpenHAB with decimal format.
    """
    test_config = default_config.copy()
    test_config["source"] = "openhab"
    test_config["url"] = "http://fake"
    test_config["soc_sensor"] = "BatterySOC"
    bi = BatteryInterface(test_config)
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "0.75"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        soc = bi._BatteryInterface__fetch_soc_data_unified()
        assert soc == 75.0


def test_homeassistant_fetch_success(default_config):
    """
    Test successful SOC fetch from Home Assistant.
    """
    test_config = default_config.copy()
    test_config["source"] = "homeassistant"
    test_config["url"] = "http://fake"
    test_config["soc_sensor"] = "sensor.battery_soc"
    test_config["access_token"] = "token"
    bi = BatteryInterface(test_config)
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "55"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        soc = bi._BatteryInterface__fetch_soc_data_unified()
        assert soc == 55.0


def test_homeassistant_price_sensor_success(default_config):
    """
    Ensure the Home Assistant price sensor value is fetched and stored.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "access_token": "token",
            "source": "homeassistant",
            "price_euro_per_wh_sensor": "sensor.accu_price",
        }
    )
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "0.002"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        bi = BatteryInterface(test_config)
        # Ensure manual update works and the getter reflects the sensor value
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.002)
        bi.shutdown()


def test_homeassistant_price_sensor_failure_keeps_last_value(default_config):
    """
    Ensure failing sensor updates keep the last configured price.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "access_token": "token",
            "source": "homeassistant",
            "price_euro_per_wh_sensor": "sensor.accu_price",
            "price_euro_per_wh_accu": 0.001,
        }
    )
    with patch(
        "src.interfaces.battery_interface.requests.get",
        side_effect=requests.exceptions.RequestException("boom"),
    ):
        bi = BatteryInterface(test_config)
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.001)
        bi.shutdown()


def test_openhab_price_sensor_success(default_config):
    """
    Ensure the OpenHAB price item value is fetched and stored.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "source": "openhab",
            "price_euro_per_wh_sensor": "BatteryPrice",
        }
    )
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "0.00015"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        bi = BatteryInterface(test_config)
        # Ensure manual update works and the getter reflects the item value
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.00015)
        bi.shutdown()


def test_openhab_price_sensor_with_unit_success(default_config):
    """
    Ensure OpenHAB price item with unit (e.g., "0.00015 €/Wh") is parsed correctly.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "source": "openhab",
            "price_euro_per_wh_sensor": "BatteryPrice",
        }
    )
    with patch("src.interfaces.battery_interface.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"state": "0.00015 €/Wh"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        bi = BatteryInterface(test_config)
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.00015)
        bi.shutdown()


def test_openhab_price_sensor_failure_keeps_last_value(default_config):
    """
    Ensure failing OpenHAB item updates keep the last configured price.
    """
    test_config = default_config.copy()
    test_config.update(
        {
            "url": "http://fake",
            "source": "openhab",
            "price_euro_per_wh_sensor": "BatteryPrice",
            "price_euro_per_wh_accu": 0.0001,
        }
    )
    with patch(
        "src.interfaces.battery_interface.requests.get",
        side_effect=requests.exceptions.RequestException("boom"),
    ):
        bi = BatteryInterface(test_config)
        bi._BatteryInterface__update_price_euro_per_wh()
        assert bi.get_price_euro_per_wh() == pytest.approx(0.0001)
        bi.shutdown()


def test_soc_error_handling(default_config):
    """
    Test SOC error handling and fail count reset.
    """
    bi = BatteryInterface(default_config)
    # Simulate 5 consecutive failures
    for _ in range(5):
        result = bi._handle_soc_error("openhab", "fail", 42)
    assert result == 5
    assert bi.soc_fail_count == 0


def test_set_min_soc_and_max_soc(default_config):
    """
    Test setting minimum and maximum SOC values.
    """
    bi = BatteryInterface(default_config)
    bi.set_min_soc(5)
    assert bi.min_soc_set == 10  # Should be set to configured min
    bi.set_min_soc(95)
    assert bi.min_soc_set == 89  # Should be set to max_soc - 1
    bi.set_max_soc(5)
    assert bi.max_soc_set == 90  # Should be set to configured max
    bi.set_max_soc(95)
    assert bi.max_soc_set == 90  # Should be set to configured max


def test_get_max_charge_power_dyn(default_config):
    """
    Test dynamic calculation of max charge power.
    """
    bi = BatteryInterface(default_config)
    bi.current_soc = 20
    bi._BatteryInterface__get_max_charge_power_dyn()
    assert bi.max_charge_power_dyn > 0
    bi.current_soc = 100
    bi._BatteryInterface__get_max_charge_power_dyn()
    assert bi.max_charge_power_dyn > 0


def test_shutdown_stops_thread(default_config):
    """
    Test that shutdown stops the update thread.
    """
    bi = BatteryInterface(default_config)
    bi.shutdown()
    assert not bi._update_thread.is_alive()


def test_soc_autodetect_first_run_1_0_is_1_percent(default_config):
    """On first run (current_soc=0), 1.0 should be treated as 1% (percentage format)."""
    bi = BatteryInterface(default_config)
    bi.shutdown()
    bi.current_soc = 0

    with patch.object(bi, "_BatteryInterface__fetch_remote_state", return_value="1.0"):
        soc = bi._BatteryInterface__fetch_soc_data_unified()
        assert soc == 1.0


def test_soc_autodetect_first_run_0_5_is_50_percent(default_config):
    """On first run (current_soc=0), other values <= 1.0 (like 0.5) should be treated as decimal (50%)."""
    bi = BatteryInterface(default_config)
    bi.shutdown()
    bi.current_soc = 0

    with patch.object(bi, "_BatteryInterface__fetch_remote_state", return_value="0.5"):
        soc = bi._BatteryInterface__fetch_soc_data_unified()
        assert soc == 50.0


def test_soc_autodetect_ambiguous_1_0_as_1_percent(default_config):
    """If current_soc is low (e.g. 0.9), 1.0 should be detected as 1%."""
    bi = BatteryInterface(default_config)
    bi.shutdown()
    bi.current_soc = 0.9

    with patch.object(bi, "_BatteryInterface__fetch_remote_state", return_value="1.0"):
        soc = bi._BatteryInterface__fetch_soc_data_unified()
        assert soc == 1.0


def test_soc_autodetect_ambiguous_1_0_as_100_percent(default_config):
    """If current_soc is high (e.g. 99.0), 1.0 should be detected as 100%."""
    bi = BatteryInterface(default_config)
    bi.shutdown()
    bi.current_soc = 99.0

    with patch.object(bi, "_BatteryInterface__fetch_remote_state", return_value="1.0"):
        soc = bi._BatteryInterface__fetch_soc_data_unified()
        assert soc == 100.0


def test_soc_autodetect_sweep_decimal(default_config):
    """Test a sweep from 0.0 to 1.0 (decimal format) with optimized steps."""
    bi = BatteryInterface(default_config)
    bi.shutdown()
    bi.current_soc = 50.0

    for i in range(10, 101, 5):
        val = i / 100.0
        with patch.object(
            bi, "_BatteryInterface__fetch_remote_state", return_value=str(val)
        ):
            soc = bi._BatteryInterface__fetch_soc_data_unified()
            assert soc == float(i)
            bi.current_soc = soc


def test_soc_autodetect_sweep_percentage(default_config):
    """Test a sweep from 0 to 100 (percentage format) with optimized steps."""
    bi = BatteryInterface(default_config)
    bi.shutdown()
    bi.current_soc = 5.0

    for i in range(10, 101, 5):
        val = float(i)
        with patch.object(
            bi, "_BatteryInterface__fetch_remote_state", return_value=str(val)
        ):
            soc = bi._BatteryInterface__fetch_soc_data_unified()
            assert soc == float(i)
            bi.current_soc = soc


# ============================================================================
# Temperature Compensation Tests
# ============================================================================


def test_calculate_temp_multiplier_none_sensor(fast_battery_interface):
    """Test that None temperature (no sensor) returns 1.0 (no compensation)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(
        None
    )
    assert multiplier == 1.0


def test_calculate_temp_multiplier_critical_cold(fast_battery_interface):
    """Test temperature compensation at critical cold (-10°C)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(
        -10
    )
    # At -10°C (< 0°C): base = 0.05, sensitivity 0.5 → multiplier = 0.05 * 1.5 = 0.075
    assert multiplier > 0.05
    assert multiplier < 0.2
    # Clipped to [0.05, 1.0]
    assert multiplier >= 0.05


def test_calculate_temp_multiplier_mild_cold(fast_battery_interface):
    """Test temperature compensation at mild cold (2°C)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(2)
    # At 2°C (0-5°C): Flat 50% (moderate derating per BYD HVM specs)
    assert multiplier == 0.5


def test_calculate_temp_multiplier_cool_transitional(fast_battery_interface):
    """Test temperature compensation in cool transitional zone (10°C)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(10)
    # At 10°C (5-12°C): Light derating ramp from 50% to 77%
    # progress = (10-5)/7 = 0.7143, multiplier = 0.50 + 0.7143*0.27 = 0.6929
    assert 0.69 < multiplier < 0.70


def test_calculate_temp_multiplier_optimal_range(fast_battery_interface):
    """Test temperature compensation in optimal range (25°C)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(25)
    # At 25°C (15-45°C): base = 1.0, sensitivity 0.5 → 1.0 * 1.5 = 1.5 → clipped to 1.0
    assert multiplier == 1.0


def test_calculate_temp_multiplier_heat_warning(fast_battery_interface):
    """Test temperature compensation in heat warning zone (47°C)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(47)
    # At 47°C (45-50°C): base = 1.0 - ((47-45)/5)*0.7 = 0.72, sensitivity 0.5 → 1.08 → clipped to 1.0
    assert multiplier <= 1.0


def test_calculate_temp_multiplier_severe_heat(fast_battery_interface):
    """Test temperature compensation in severe heat zone (55°C)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(55)
    # At 55°C (50-60°C): base = 0.3 - ((55-50)/10)*0.25 = 0.175, sensitivity 0.5 → 0.2625
    assert multiplier > 0.05
    assert multiplier < 0.5


def test_calculate_temp_multiplier_critical_heat(fast_battery_interface):
    """Test temperature compensation at critical heat (65°C)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(65)
    # At 65°C (> 60°C): Shutdown - no charging above 60°C (overheat protection)
    assert multiplier == 0.0


def test_calculate_temp_multiplier_boundary_0_celsius(fast_battery_interface):
    """Test temperature compensation at 0°C boundary."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(0)
    # At 0°C: Transition from strong derating to moderate derating = 50% (per BYD HVM specs)
    assert multiplier == 0.5


def test_calculate_temp_multiplier_boundary_5_celsius(fast_battery_interface):
    """Test temperature compensation at 5°C boundary."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(5)
    # At 5°C: Transitions from moderate to light derating, starts at 50% (per BYD HVM specs)
    assert multiplier == 0.5


def test_calculate_temp_multiplier_boundary_15_celsius(fast_battery_interface):
    """Test temperature compensation at 15°C boundary (transition to full power)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(15)
    # At 15°C: transitions from flat 10-15°C plateau (0.325) to optimal range (1.0)
    # With sensitivity 0.5: base = 1.0, final = 1.0 * 1.5 clamped to 1.0
    assert multiplier == 1.0


def test_calculate_temp_multiplier_boundary_25_celsius(fast_battery_interface):
    """Test temperature compensation at 25°C boundary (end of warm-up zone)."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(25)
    # At 25°C: end of warm-up ramp, base = 0.10 + ((25-5)/20)*0.90 = 1.0, sensitivity 0.5 → 1.5 → clipped to 1.0
    assert multiplier == 1.0


def test_calculate_temp_multiplier_boundary_50_celsius(fast_battery_interface):
    """Test temperature compensation at 50°C boundary."""
    multiplier = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(50)
    # At 50°C: transitions from heat-warning to severe-heat, base = 0.3
    assert multiplier >= 0.15
    assert multiplier <= 0.5


def test_calculate_temp_multiplier_range_validity(fast_battery_interface):
    """Test that all temperature multipliers stay within valid range [0.0, 1.0]."""
    test_temperatures = [-30, -15, -5, 0, 5, 10, 15, 20, 25, 30, 40, 45, 50, 55, 60, 70]
    for temp in test_temperatures:
        multiplier = (
            fast_battery_interface._BatteryInterface__calculate_temp_multiplier(temp)
        )
        assert (
            0.0 <= multiplier <= 1.0
        ), f"Multiplier {multiplier} out of range for temp {temp}°C"


def test_get_max_charge_power_dyn_no_temperature_sensor(fast_battery_interface):
    """Test max charge power calculation without temperature sensor."""
    fast_battery_interface.current_soc = 30
    fast_battery_interface.current_temp = None  # No temperature sensor
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    # Should calculate without temperature compensation (multiplier = 1.0)
    assert fast_battery_interface.max_charge_power_dyn > 0
    assert (
        fast_battery_interface.max_charge_power_dyn
        <= fast_battery_interface.max_charge_power_fix
    )


def test_get_max_charge_power_dyn_optimal_temperature(fast_battery_interface):
    """Test max charge power calculation at optimal temperature (25°C)."""
    fast_battery_interface.current_soc = 30
    fast_battery_interface.current_temp = 25  # Optimal temperature
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    assert fast_battery_interface.max_charge_power_dyn > 0


def test_get_max_charge_power_dyn_cold_reduces_power(fast_battery_interface):
    """Test that cold temperature reduces max charge power."""
    # Get power at optimal temperature
    fast_battery_interface.current_soc = 30
    fast_battery_interface.current_temp = 25
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_at_optimal = fast_battery_interface.max_charge_power_dyn

    # Get power at cold temperature
    fast_battery_interface.current_temp = -5
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_at_cold = fast_battery_interface.max_charge_power_dyn

    # Cold should reduce power
    assert power_at_cold < power_at_optimal


def test_get_max_charge_power_dyn_heat_reduces_power(fast_battery_interface):
    """Test that high temperature reduces max charge power."""
    # Get power at optimal temperature
    fast_battery_interface.current_soc = 30
    fast_battery_interface.current_temp = 25
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_at_optimal = fast_battery_interface.max_charge_power_dyn

    # Get power at hot temperature
    fast_battery_interface.current_temp = 55
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_at_hot = fast_battery_interface.max_charge_power_dyn

    # Heat should reduce power
    assert power_at_hot < power_at_optimal


def test_get_max_charge_power_dyn_extreme_cold_critical_reduction(
    fast_battery_interface,
):
    """Test that extreme cold causes critical power reduction."""
    # Get power at optimal temperature
    fast_battery_interface.current_soc = 30
    fast_battery_interface.current_temp = 25
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_at_optimal = fast_battery_interface.max_charge_power_dyn

    # Get power at extreme cold
    fast_battery_interface.current_temp = -20
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_at_extreme_cold = fast_battery_interface.max_charge_power_dyn

    # Extreme cold should drastically reduce power
    assert power_at_extreme_cold < power_at_optimal
    # Should be significantly lower (at least 50% reduction)
    assert power_at_extreme_cold < power_at_optimal * 0.5


def test_get_max_charge_power_dyn_combined_soc_and_temp_cold_low_soc(
    fast_battery_interface,
):
    """Test combined SOC and temperature compensation at cold temp with low SOC."""
    # Low SOC at optimal temperature
    fast_battery_interface.current_soc = 20
    fast_battery_interface.current_temp = 25
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_low_soc_opt_temp = fast_battery_interface.max_charge_power_dyn

    # Same SOC but cold temperature
    fast_battery_interface.current_soc = 20
    fast_battery_interface.current_temp = 0
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_low_soc_cold_temp = fast_battery_interface.max_charge_power_dyn

    # Cold should reduce power even at low SOC
    assert power_low_soc_cold_temp <= power_low_soc_opt_temp


def test_get_max_charge_power_dyn_combined_soc_and_temp_high_soc(
    fast_battery_interface,
):
    """Test combined SOC and temperature compensation at high SOC with cold temp."""
    # High SOC at optimal temperature
    fast_battery_interface.current_soc = 80
    fast_battery_interface.current_temp = 25
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_high_soc_opt_temp = fast_battery_interface.max_charge_power_dyn

    # Same SOC but cold temperature
    fast_battery_interface.current_soc = 80
    fast_battery_interface.current_temp = 0
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_high_soc_cold_temp = fast_battery_interface.max_charge_power_dyn

    # Cold should further reduce already-reduced high-SOC power
    assert power_high_soc_cold_temp < power_high_soc_opt_temp


def test_get_max_charge_power_dyn_temp_compensation_continuous(fast_battery_interface):
    """Test that temperature compensation varies smoothly across temperature range."""
    fast_battery_interface.current_soc = 30

    # Test power across temperature range
    powers = []
    for temp in range(-20, 61, 5):
        fast_battery_interface.current_temp = temp
        fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
        powers.append(fast_battery_interface.max_charge_power_dyn)

    # Verify all powers are within valid range
    for power in powers:
        assert 500 <= power <= 3000  # Between min and max

    # Verify cold region (-20 to 15°C) shows increasing trend
    cold_powers = powers[:8]  # -20 to 15°C
    for i in range(len(cold_powers) - 1):
        # Cold should generally increase toward optimal
        assert cold_powers[i] <= cold_powers[i + 1] * 1.5


def test_get_max_charge_power_dyn_respects_max_charge_power_fix(fast_battery_interface):
    """Test that dynamic calculation never exceeds max_charge_power_fix."""
    # Test at various conditions
    test_cases = [
        (20, 25),  # Low SOC, optimal temp
        (50, 25),  # Mid SOC, optimal temp
        (10, -5),  # Low SOC, cold
        (30, 50),  # Mid SOC, hot
    ]

    for soc, temp in test_cases:
        fast_battery_interface.current_soc = soc
        fast_battery_interface.current_temp = temp
        fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
        assert (
            fast_battery_interface.max_charge_power_dyn
            <= fast_battery_interface.max_charge_power_fix
        )


def test_get_max_charge_power_dyn_respects_minimum_charge_power(fast_battery_interface):
    """Test that dynamic calculation respects minimum charge power threshold."""
    min_charge = 500

    # Test at extreme conditions (high SOC + extreme temperature)
    fast_battery_interface.current_soc = 99
    fast_battery_interface.current_temp = 65
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn(
        min_charge_power=min_charge
    )
    assert fast_battery_interface.max_charge_power_dyn >= min_charge


def test_get_max_charge_power_dyn_temperature_sensor_configured(default_config):
    """Test max charge power with temperature sensor configured."""
    test_config = default_config.copy()
    test_config["sensor_battery_temperature"] = "BatteryTemp"
    with patch.object(BatteryInterface, "start_update_service", return_value=None):
        bi = BatteryInterface(test_config)
        # Manually set values (since we're not mocking sensor fetches)
        bi.current_soc = 30
        bi.current_temp = 15
        bi._BatteryInterface__get_max_charge_power_dyn()
        assert bi.max_charge_power_dyn > 0


def test_calculate_temp_multiplier_monotonic_cold_region(fast_battery_interface):
    """Test that temp multiplier increases monotonically in cold region (-20 to 0°C)."""
    multipliers = []
    for temp in range(-20, 1):
        mult = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(
            float(temp)
        )
        multipliers.append(mult)

    # Should increase monotonically
    for i in range(len(multipliers) - 1):
        assert multipliers[i] <= multipliers[i + 1]


def test_calculate_temp_multiplier_monotonic_warm_up_region(fast_battery_interface):
    """Test that temp multiplier increases monotonically in warm-up region (0 to 15°C)."""
    multipliers = []
    for temp in range(0, 16):
        mult = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(
            float(temp)
        )
        multipliers.append(mult)

    # Should increase monotonically
    for i in range(len(multipliers) - 1):
        assert multipliers[i] <= multipliers[i + 1]


def test_calculate_temp_multiplier_monotonic_cool_down_region(fast_battery_interface):
    """Test that temp multiplier decreases monotonically in cool-down region (45 to 60°C)."""
    multipliers = []
    for temp in range(45, 61):
        mult = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(
            float(temp)
        )
        multipliers.append(mult)

    # Should decrease monotonically
    for i in range(len(multipliers) - 1):
        assert multipliers[i] >= multipliers[i + 1]


def test_temp_compensation_critical_10_15_range(fast_battery_interface):
    """
    Test temperature compensation in the critical 10-15°C range.
    This is where reduction becomes less effective due to sensitivity clipping.

    Issue: The formula in this range approaches 1.0 (full power) too quickly.
    At 10.5°C we get ~89%, at 11°C we get ~96%, at 12°C we already hit 100%.
    This is because sensitivity adjustment (×1.5) clips high base values to 1.0.
    """
    # Test every 0.5°C in the critical range
    temps = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0]
    multipliers = []

    for temp in temps:
        mult = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(temp)
        multipliers.append(mult)

    # Verify we have meaningful variation (not all 1.0)
    assert (
        min(multipliers) < 0.99
    ), "10-15°C range should show compensation, not all 100%"

    # Verify rough monotonic increase (may have plateaus near 1.0)
    for i in range(len(multipliers) - 1):
        # Allow small dips due to rounding/clipping
        assert multipliers[i] - multipliers[i + 1] <= 0.01


def test_temp_compensation_10_15_power_reduction(fast_battery_interface):
    """
    Test progressive temperature compensation rise from 10-15°C.
    Uses smooth accelerating curve: small steps at 10°C, larger steps toward 15°C.
    """
    # Set up with realistic capacity where temp effects are visible
    fast_battery_interface.battery_data["capacity_wh"] = 3000  # 3kWh battery
    fast_battery_interface.current_soc = 30  # Low SOC = high C-rate

    # Get power at 10°C (start of progressive rise)
    fast_battery_interface.current_temp = 10.0
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_at_10 = fast_battery_interface.max_charge_power_dyn

    # Get power at 12°C (mid acceleration zone)
    fast_battery_interface.current_temp = 12.0
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    power_at_12 = fast_battery_interface.max_charge_power_dyn

    # Power should increase progressively from 10°C to 12°C
    assert power_at_12 > power_at_10, "Power at 12°C should be higher than at 10°C"

    # Should show meaningful increase (smooth acceleration)
    increase = (power_at_12 - power_at_10) / power_at_10
    assert increase > 0.3, f"Expected >30% increase over 2°C, got {increase*100:.1f}%"


def test_temp_compensation_10_5_celsius_specific(fast_battery_interface):
    """
    Test the specific 10.5°C temperature reported by user.
    With BYD HVM datasheet: multiplier at 10.5°C is ~0.7121 (71.21% power).
    In light derating zone (5-12°C): gradual warm-up toward optimal range.
    """
    # With realistic battery where temp effects are visible
    fast_battery_interface.battery_data["capacity_wh"] = 3000
    fast_battery_interface.current_soc = 30
    fast_battery_interface.current_temp = 10.5

    mult = fast_battery_interface._BatteryInterface__calculate_temp_multiplier(10.5)
    # BYD HVM: progress = (10.5-5)/7 = 0.7857, multiplier = 0.50 + 0.7857*0.27 = 0.7121
    assert (
        0.711 < mult < 0.713
    ), f"At 10.5°C multiplier should be ~0.7121, got {mult:.4f}"

    # Get actual power
    fast_battery_interface._BatteryInterface__get_max_charge_power_dyn()
    # At 10.5°C with 3kWh and SOC=30%, we expect ~2136W (71.21% of 3000W max)
    # BYD HVM light derating zone - allowing tolerance for SOC curve calculations
    assert (
        2130 < fast_battery_interface.max_charge_power_dyn < 2160
    ), f"At 10.5°C expected ~2136W, got {fast_battery_interface.max_charge_power_dyn}W"
