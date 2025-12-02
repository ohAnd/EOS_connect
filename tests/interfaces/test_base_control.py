"""
test_base_control_ac_charge_conversion.py

Tests for the AC charge demand conversion between relative values and power (W)
with different time_frame_base settings (hourly vs 15-minute intervals).

This test specifically addresses GitHub Issue #167 where AC charge demand
was reported 4x too low via MQTT for 15-minute intervals with EVopt backend.

The fix: MQTT now uses get_needed_ac_charge_power() instead of get_current_ac_charge_demand()
"""

import pytest
import pytz
from datetime import datetime
from unittest.mock import patch
from src.interfaces.base_control import BaseControl


@pytest.fixture
def config_base():
    """Base configuration for tests"""
    return {
        "battery": {
            "max_charge_power_w": 5000,
            "capacity_wh": 10000,
            "max_soc_percentage": 100,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "price_euro_per_wh_accu": 0.0001,
        },
        "inverter": {
            "type": "fronius_gen24",
            "max_grid_charge_rate": 5000,
            "max_pv_charge_rate": 5000,
        },
    }


@pytest.fixture
def berlin_timezone():
    """Timezone fixture"""
    return pytz.timezone("Europe/Berlin")


class TestACChargeDemandConversion:
    """Test suite for AC charge demand conversion - Issue #167"""

    @patch("src.interfaces.base_control.datetime")
    def test_hourly_intervals_at_start_of_hour(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """
        Test AC charge power calculation for HOURLY intervals at START of time slot.

        At 10:00:00 with 50% charge demand:
        - Energy stored: 2500 Wh
        - Time remaining: 3600 seconds (1 hour)
        - Power needed: 2500 Wh / 1 hour = 2500 W
        """
        # Mock time to be exactly at start of hour
        mock_now = berlin_timezone.localize(datetime(2025, 1, 1, 10, 0, 0))
        mock_datetime.now.return_value = mock_now

        base_control = BaseControl(config_base, berlin_timezone, time_frame_base=3600)
        base_control.set_current_ac_charge_demand(0.5)
        # Set battery charge max to allow full power (simulates real-world battery interface)
        base_control.set_current_bat_charge_max(5000)

        # Energy is stored correctly
        energy = base_control.get_current_ac_charge_demand()
        assert energy == 2500, f"Stored energy should be 2500 Wh, got {energy}"

        # Power for inverter/MQTT at start of hour
        power = base_control.get_needed_ac_charge_power()
        assert power == 2500, f"Power at start of hour should be 2500 W, got {power}"

    @patch("src.interfaces.base_control.datetime")
    def test_15min_intervals_at_start_of_slot(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """
        Test AC charge power calculation for 15-MIN intervals at START of time slot.

        At 10:00:00 with 50% charge demand:
        - Energy stored: 2500 Wh (for 15-min period)
        - Time remaining: 900 seconds (15 minutes)
        - Power needed: 2500 Wh / 0.25 hour = 10000 W

        This is the core of Issue #167!
        """
        # Mock time to be exactly at start of 15-min slot
        mock_now = berlin_timezone.localize(datetime(2025, 1, 1, 10, 0, 0))
        mock_datetime.now.return_value = mock_now

        base_control = BaseControl(config_base, berlin_timezone, time_frame_base=900)
        base_control.set_current_ac_charge_demand(0.5)
        # Set battery charge max to allow full power
        base_control.set_current_bat_charge_max(10000)

        # Energy is stored correctly
        energy = base_control.get_current_ac_charge_demand()
        assert energy == 2500, f"Stored energy should be 2500 Wh, got {energy}"

        # Power for inverter/MQTT at start of slot
        power = base_control.get_needed_ac_charge_power()
        assert (
            power == 10000
        ), f"Power at start of 15-min slot should be 10000 W, got {power}"

    @patch("src.interfaces.base_control.datetime")
    def test_issue_167_mqtt_inverter_parity(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """
        Test the exact scenario from Issue #167.

        At 3:30:00 with EVopt backend (15-min intervals):
        - MQTT should show: ~10000 W
        - Inverter gets: ~10000 W
        - They should be EQUAL (both use get_needed_ac_charge_power())

        Before fix: MQTT showed ~2500 W (using get_current_ac_charge_demand())
        After fix: MQTT shows ~10000 W (using get_needed_ac_charge_power())
        """
        # Mock time to be at 3:30:00 (start of 15-min slot)
        mock_now = berlin_timezone.localize(datetime(2025, 1, 1, 3, 30, 0))
        mock_datetime.now.return_value = mock_now

        base_control = BaseControl(config_base, berlin_timezone, time_frame_base=900)
        base_control.set_current_ac_charge_demand(0.5)
        # Set battery charge max to allow full power
        base_control.set_current_bat_charge_max(10000)

        # What MQTT was WRONGLY showing before fix
        wrong_mqtt_value = base_control.get_current_ac_charge_demand()
        assert wrong_mqtt_value == 2500, "Old MQTT value was energy (2500 Wh)"

        # What both MQTT and Inverter should show after fix
        correct_power = base_control.get_needed_ac_charge_power()
        assert correct_power == 10000, f"Both should show 10000 W, got {correct_power}"

        # Verify the 4x difference that was reported in issue
        assert (
            correct_power == wrong_mqtt_value * 4
        ), "Issue #167: Power should be 4x the energy value for 15-min intervals"

    @patch("src.interfaces.base_control.datetime")
    def test_dynamic_power_increases_as_time_passes(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """
        Test that get_needed_ac_charge_power() dynamically increases as time passes.
        This is correct behavior - it's a "catch-up" mechanism.
        """
        base_control = BaseControl(config_base, berlin_timezone, time_frame_base=900)
        base_control.set_current_ac_charge_demand(0.5)  # 2500 Wh target
        # Set battery charge max high enough to not cap the power
        base_control.set_current_bat_charge_max(200000)

        # At start of slot (10:00:00)
        mock_datetime.now.return_value = berlin_timezone.localize(
            datetime(2025, 1, 1, 10, 0, 0)
        )
        power_at_start = base_control.get_needed_ac_charge_power()
        assert power_at_start == 10000, "At start: 2500 Wh / 0.25 h = 10000 W"

        # Halfway through slot (10:07:30)
        mock_datetime.now.return_value = berlin_timezone.localize(
            datetime(2025, 1, 1, 10, 7, 30)
        )
        power_at_half = base_control.get_needed_ac_charge_power()
        # Remaining: 7.5 min = 0.125 h → 2500 Wh / 0.125 h = 20000 W
        assert power_at_half == 20000, "At halfway: power doubles to catch up"

        # Near end (10:14:00)
        mock_datetime.now.return_value = berlin_timezone.localize(
            datetime(2025, 1, 1, 10, 14, 0)
        )
        power_near_end = base_control.get_needed_ac_charge_power()
        # Remaining: 1 min = 0.0167 h → 2500 Wh / 0.0167 h = 149700 W
        assert (
            power_near_end > 100000
        ), f"Near end: extreme catch-up, got {power_near_end} W"

    @pytest.mark.parametrize(
        "time_frame_base,value_relative,expected_energy",
        [
            (3600, 0.0, 0),
            (3600, 0.25, 1250),
            (3600, 0.5, 2500),
            (3600, 0.75, 3750),
            (3600, 1.0, 5000),
            (900, 0.0, 0),
            (900, 0.25, 1250),
            (900, 0.5, 2500),
            (900, 0.75, 3750),
            (900, 1.0, 5000),
        ],
    )
    def test_energy_storage_is_time_agnostic(
        self,
        config_base,
        berlin_timezone,
        time_frame_base,
        value_relative,
        expected_energy,
    ):
        """
        Test that energy storage is the same for both hourly and 15-min intervals.

        The difference is only in how power is calculated from this energy.
        """
        base_control = BaseControl(config_base, berlin_timezone, time_frame_base)
        base_control.set_current_ac_charge_demand(value_relative)

        energy = base_control.get_current_ac_charge_demand()
        assert (
            energy == expected_energy
        ), f"Energy should be {expected_energy} Wh regardless of time_frame_base"


class TestMQTTInverterParity:
    """Tests to ensure MQTT and Inverter always get the same power value"""

    @patch("src.interfaces.base_control.datetime")
    def test_mqtt_and_inverter_use_same_method(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """
        Verify that both MQTT and inverter use get_needed_ac_charge_power().
        They must ALWAYS show the same value.
        """
        mock_now = berlin_timezone.localize(datetime(2025, 1, 1, 15, 30, 0))
        mock_datetime.now.return_value = mock_now

        base_control = BaseControl(config_base, berlin_timezone, time_frame_base=900)
        base_control.set_current_ac_charge_demand(0.5)
        # Set battery charge max to allow full power
        base_control.set_current_bat_charge_max(10000)

        # Both should use this method
        power_value = base_control.get_needed_ac_charge_power()

        # Simulate what inverter gets
        inverter_power = base_control.get_needed_ac_charge_power()

        # Simulate what MQTT should publish (after fix)
        mqtt_power = base_control.get_needed_ac_charge_power()

        assert (
            inverter_power == mqtt_power == power_value
        ), "MQTT and Inverter must always show the same power value"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
