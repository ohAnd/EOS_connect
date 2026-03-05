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
        # Patch now() to return a real datetime object and allow datetime construction
        mock_now = berlin_timezone.localize(datetime(2025, 1, 1, 10, 0, 0))
        mock_datetime.now.return_value = mock_now
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

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


class TestEffectiveDischargeAllowed:
    """Test suite for effective discharge allowed - Issue #175

    Tests that the effective discharge allowed state reflects the FINAL state
    after all overrides (EVCC, manual) are applied, not just the optimizer output.
    """

    @patch("src.interfaces.base_control.datetime")
    def test_discharge_allowed_without_evcc(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """Test discharge allowed state without EVCC override"""
        # Mock datetime to return a fixed time
        mock_datetime.now.return_value = datetime(
            2024, 10, 4, 10, 0, 0, tzinfo=berlin_timezone
        )
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        base_control = BaseControl(config_base, berlin_timezone, 3600)

        # Set discharge allowed from optimizer
        base_control.set_current_discharge_allowed(True)

        # Without EVCC, both should match
        assert base_control.get_current_discharge_allowed() == True
        assert base_control.get_effective_discharge_allowed() == True

    @patch("src.interfaces.base_control.datetime")
    def test_discharge_not_allowed_without_evcc(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """Test discharge not allowed state without EVCC override"""
        mock_datetime.now.return_value = datetime(
            2024, 10, 4, 10, 0, 0, tzinfo=berlin_timezone
        )
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        base_control = BaseControl(config_base, berlin_timezone, 3600)

        # Set discharge not allowed from optimizer
        base_control.set_current_discharge_allowed(False)

        # Without EVCC, both should match
        assert base_control.get_current_discharge_allowed() == False
        assert base_control.get_effective_discharge_allowed() == False

    @patch("src.interfaces.base_control.datetime")
    def test_evcc_pv_mode_overrides_to_discharge_allowed(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """Test that EVCC PV mode sets effective discharge to True - Issue #175

        This reproduces the bug: optimizer says discharge_allowed=False,
        but EVCC PV mode should make effective discharge allowed = True
        """
        mock_datetime.now.return_value = datetime(
            2024, 10, 4, 10, 0, 0, tzinfo=berlin_timezone
        )
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        base_control = BaseControl(config_base, berlin_timezone, 3600)

        # Simulate the issue scenario from #175
        base_control.set_current_ac_charge_demand(0)
        base_control.set_current_dc_charge_demand(5000)
        base_control.set_current_discharge_allowed(False)  # Optimizer says no discharge

        # EVCC is charging in PV mode
        base_control.set_current_evcc_charging_state(True)
        base_control.set_current_evcc_charging_mode("pv")

        # Original optimizer value should still be False
        assert base_control.get_current_discharge_allowed() == False

        # But effective discharge should be True due to EVCC PV mode
        assert base_control.get_effective_discharge_allowed() == True

        # Mode should be MODE_DISCHARGE_ALLOWED_EVCC_PV (4)
        assert base_control.get_current_overall_state_number() == 4

    @patch("src.interfaces.base_control.datetime")
    def test_evcc_minpv_mode_overrides_to_discharge_allowed(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """Test that EVCC Min+PV mode sets effective discharge to True"""
        mock_datetime.now.return_value = datetime(
            2024, 10, 4, 10, 0, 0, tzinfo=berlin_timezone
        )
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        base_control = BaseControl(config_base, berlin_timezone, 3600)

        base_control.set_current_ac_charge_demand(0)
        base_control.set_current_dc_charge_demand(3000)
        base_control.set_current_discharge_allowed(False)  # Optimizer says no discharge

        # EVCC is charging in Min+PV mode
        base_control.set_current_evcc_charging_state(True)
        base_control.set_current_evcc_charging_mode("minpv")

        # Original optimizer value should still be False
        assert base_control.get_current_discharge_allowed() == False

        # But effective discharge should be True due to EVCC Min+PV mode
        assert base_control.get_effective_discharge_allowed() == True

        # Mode should be MODE_DISCHARGE_ALLOWED_EVCC_MIN_PV (5)
        assert base_control.get_current_overall_state_number() == 5

    @patch("src.interfaces.base_control.datetime")
    def test_evcc_fast_charge_keeps_discharge_not_allowed(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """Test that EVCC fast charge mode keeps effective discharge as False"""
        mock_datetime.now.return_value = datetime(
            2024, 10, 4, 10, 0, 0, tzinfo=berlin_timezone
        )
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        base_control = BaseControl(config_base, berlin_timezone, 3600)

        base_control.set_current_ac_charge_demand(0)
        base_control.set_current_dc_charge_demand(0)
        base_control.set_current_discharge_allowed(False)

        # EVCC is fast charging
        base_control.set_current_evcc_charging_state(True)
        base_control.set_current_evcc_charging_mode("now")

        # Both should be False - fast charge avoids discharge
        assert base_control.get_current_discharge_allowed() == False
        assert base_control.get_effective_discharge_allowed() == False

        # Mode should be MODE_AVOID_DISCHARGE_EVCC_FAST (3)
        assert base_control.get_current_overall_state_number() == 3

    @patch("src.interfaces.base_control.datetime")
    def test_evcc_pv_mode_with_grid_charge_overrides_to_pv_mode(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """Test that EVCC PV mode overrides even when grid charge is requested

        Current behavior: EVCC PV mode takes precedence over grid charge
        (only fast charge modes preserve grid charge as GRID_CHARGE_EVCC_FAST)
        """
        mock_datetime.now.return_value = datetime(
            2024, 10, 4, 10, 0, 0, tzinfo=berlin_timezone
        )
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        base_control = BaseControl(config_base, berlin_timezone, 3600)

        # Grid charging requested by optimizer
        base_control.set_current_ac_charge_demand(2500)
        base_control.set_current_dc_charge_demand(0)
        base_control.set_current_discharge_allowed(False)

        # EVCC is in PV mode - overrides to EVCC PV mode
        base_control.set_current_evcc_charging_state(True)
        base_control.set_current_evcc_charging_mode("pv")

        # Current behavior: EVCC PV mode overrides grid charge
        assert (
            base_control.get_current_overall_state_number() == 4
        )  # MODE_DISCHARGE_ALLOWED_EVCC_PV
        assert base_control.get_effective_discharge_allowed() == True


class TestDynamicOverridePVGreaterLoad:
    """Test suite for dynamic discharge override based on PV > Load - Issue #XXX"""

    @patch("src.interfaces.base_control.datetime")
    def test_dyn_override_initialization(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """Test that dynamic override starts as False"""
        mock_datetime.now.return_value = datetime(
            2024, 10, 4, 10, 0, 0, tzinfo=berlin_timezone
        )
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        base_control = BaseControl(config_base, berlin_timezone, 3600)

        # Should start as False
        assert base_control.get_dyn_override_discharge_allowed_active() == False

    @patch("src.interfaces.base_control.datetime")
    def test_dyn_override_setter_and_getter(
        self, mock_datetime, config_base, berlin_timezone
    ):
        """Test setting and getting dynamic override state"""
        mock_datetime.now.return_value = datetime(
            2024, 10, 4, 10, 0, 0, tzinfo=berlin_timezone
        )
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        base_control = BaseControl(config_base, berlin_timezone, 3600)

        # Set to True
        base_control.set_dyn_override_discharge_allowed_active(True)
        assert base_control.get_dyn_override_discharge_allowed_active() == True

        # Set to False
        base_control.set_dyn_override_discharge_allowed_active(False)
        assert base_control.get_dyn_override_discharge_allowed_active() == False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
