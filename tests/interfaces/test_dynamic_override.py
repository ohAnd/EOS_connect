"""
test_dynamic_override.py

Tests for the dynamic discharge override feature based on PV > Load comparison.
This feature allows overriding the optimizer's discharge decision when PV forecast
exceeds load for the current time slot.
"""

import pytest
import pytz
from datetime import datetime
from unittest.mock import patch, MagicMock
from src.interfaces.optimization_interface import OptimizationInterface


@pytest.fixture
def config_with_override_enabled():
    """Configuration with dynamic override enabled"""
    return {
        "source": "eos_server",
        "server": "localhost",
        "port": 8503,
        "timeout": 60,
        "time_frame": 3600,
        "dyn_override_discharge_allowed_pv_greater_load": True,
    }


@pytest.fixture
def config_with_override_disabled():
    """Configuration with dynamic override disabled"""
    return {
        "source": "eos_server",
        "server": "localhost",
        "port": 8503,
        "timeout": 60,
        "time_frame": 3600,
    }


@pytest.fixture
def berlin_timezone():
    """Timezone fixture"""
    return pytz.timezone("Europe/Berlin")


@pytest.fixture
def eos_request_with_pv_greater_than_load():
    """Sample EOS request where PV > Load at hour 12"""
    return {
        "ems": {
            "pv_prognose_wh": [
                100,
                200,
                500,
                600,
                1000,
                1500,
                3000,
                4000,
                5000,
                3000,
                1000,
                500,
                300,
                200,
                100,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ],
            "gesamtlast": [
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
                400,
            ],
            "strompreis_euro_pro_wh": [0.0002] * 24,
            "einspeiseverguetung_euro_pro_wh": [0.00008] * 24,
            "preis_euro_pro_wh_akku": 0.0001,
        },
    }


@pytest.fixture
def eos_response_discharge_not_allowed():
    """Sample optimizer response where discharge is NOT allowed"""
    return {
        "ac_charge": [
            0.5,
            0.5,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ],
        "dc_charge": [
            0,
            0,
            0.5,
            0.7,
            1.0,
            1.0,
            1.0,
            0.8,
            0.5,
            0.3,
            0.1,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ],
        "discharge_allowed": [
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ],
        "start_solution": [0, 0],
    }


class TestDynamicOverrideLogic:
    """Test suite for dynamic PV > Load override logic"""

    @patch("src.interfaces.optimization_interface.EOSBackend")
    def test_examine_response_without_override_enabled(
        self,
        mock_backend,
        config_with_override_disabled,
        berlin_timezone,
        eos_response_discharge_not_allowed,
    ):
        """Test that optimizer response is unchanged when override is disabled"""
        # Create config without optimization settings
        eos_config = config_with_override_disabled

        optimization_interface = OptimizationInterface(
            config=eos_config, time_frame_base=3600, timezone=berlin_timezone
        )

        # Current time is 12:00 (noon)
        with patch("src.interfaces.optimization_interface.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 10, 4, 12, 0, 0, tzinfo=berlin_timezone
            )
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(
                *args, **kwargs
            )

            ac_charge, dc_charge, discharge_allowed, error, override_array = (
                optimization_interface.examine_response_to_control_data(
                    eos_response_discharge_not_allowed
                )
            )

            # Should remain False since override is not enabled
            assert discharge_allowed == False
            assert error == False

    @patch("src.interfaces.optimization_interface.EVOptBackend")
    def test_examine_response_with_pv_greater_than_load(
        self,
        mock_backend,
        config_with_override_enabled,
        berlin_timezone,
        eos_request_with_pv_greater_than_load,
        eos_response_discharge_not_allowed,
    ):
        """Test that discharge is overridden to True when PV > Load and override enabled"""
        # Create interface with override enabled
        optimization_interface = OptimizationInterface(
            config=config_with_override_enabled,
            time_frame_base=3600,
            timezone=berlin_timezone,
        )

        # Store the EOS request (normally done in optimize() method)
        optimization_interface.last_eos_request = eos_request_with_pv_greater_than_load

        # Current time is 8:00 (morning) - at this hour PV (5000) > Load (400)
        with patch("src.interfaces.optimization_interface.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 10, 4, 8, 0, 0, tzinfo=berlin_timezone
            )
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(
                *args, **kwargs
            )

            ac_charge, dc_charge, discharge_allowed, error, override_array = (
                optimization_interface.examine_response_to_control_data(
                    eos_response_discharge_not_allowed
                )
            )

            # Should be overridden to True because PV (5000) > Load (400)
            assert discharge_allowed == True
            assert error == False

    @patch("src.interfaces.optimization_interface.EOSBackend")
    def test_examine_response_with_pv_less_than_load(
        self,
        mock_backend,
        config_with_override_enabled,
        berlin_timezone,
        eos_response_discharge_not_allowed,
    ):
        """Test that discharge remains False when PV < Load even with override enabled"""
        eos_request = {
            "ems": {
                "pv_prognose_wh": [
                    100,
                    200,
                    300,
                    400,
                    500,
                    600,
                    700,
                    800,
                    900,
                    800,
                    700,
                    600,
                    500,
                    400,
                    300,
                    200,
                    100,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                ],
                "gesamtlast": [
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                    1000,
                ],
                "strompreis_euro_pro_wh": [0.0002] * 24,
                "einspeiseverguetung_euro_pro_wh": [0.00008] * 24,
                "preis_euro_pro_wh_akku": 0.0001,
            },
        }

        # Create config with override enabled
        override_config = config_with_override_enabled.copy()
        override_config["dyn_override_discharge_allowed_pv_greater_load"] = True

        optimization_interface = OptimizationInterface(
            config=override_config, time_frame_base=3600, timezone=berlin_timezone
        )

        # Store the EOS request
        optimization_interface.last_eos_request = eos_request

        # Current time is 12:00 (noon) - at this hour PV (300) < Load (1000)
        with patch("src.interfaces.optimization_interface.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 10, 4, 12, 0, 0, tzinfo=berlin_timezone
            )
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(
                *args, **kwargs
            )

            ac_charge, dc_charge, discharge_allowed, error, override_array = (
                optimization_interface.examine_response_to_control_data(
                    eos_response_discharge_not_allowed
                )
            )

            # Should remain False because PV (600) < Load (1000)
            assert discharge_allowed == False
            assert error == False

    @patch("src.interfaces.optimization_interface.EOSBackend")
    def test_examine_response_ac_charging_blocks_override(
        self,
        mock_backend,
        config_with_override_enabled,
        berlin_timezone,
        eos_response_discharge_not_allowed,
    ):
        """Test that AC charging takes precedence - override not applied if AC charging requested"""
        # Request with PV > Load at slot 8 (morning peak)
        eos_request = {
            "ems": {
                "pv_prognose_wh": [
                    100,
                    200,
                    500,
                    600,
                    1000,
                    1500,
                    3000,
                    4000,
                    5000,
                    3000,
                    1000,
                    500,
                    300,
                    200,
                    100,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                ],
                "gesamtlast": [400] * 24,
                "strompreis_euro_pro_wh": [0.0002] * 24,
                "einspeiseverguetung_euro_pro_wh": [0.00008] * 24,
                "preis_euro_pro_wh_akku": 0.0001,
            },
        }

        # Response with AC charging at slot 8
        response_with_ac_charging = {
            "ac_charge": [
                0.5,
                0.5,
                0,
                0,
                0,
                0,
                0,
                0,
                0.3,
                0,  # ← AC charge at slot 8
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ],
            "dc_charge": [0] * 24,
            "discharge_allowed": [0] * 24,
            "start_solution": [0, 0],
        }

        # Create config with override enabled
        override_config = config_with_override_enabled.copy()
        override_config["dyn_override_discharge_allowed_pv_greater_load"] = True

        optimization_interface = OptimizationInterface(
            config=override_config, time_frame_base=3600, timezone=berlin_timezone
        )
        optimization_interface.last_eos_request = eos_request

        # Current time at slot 8 (8:00 AM): PV (5000) > Load (400) BUT AC charging (0.3) is requested
        with patch("src.interfaces.optimization_interface.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 10, 4, 8, 0, 0, tzinfo=berlin_timezone
            )
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(
                *args, **kwargs
            )

            ac_charge, dc_charge, discharge_allowed, error, override_array = (
                optimization_interface.examine_response_to_control_data(
                    response_with_ac_charging
                )
            )

            # Should remain False because AC charging takes precedence (grid charge > dynamic override)
            assert discharge_allowed == False
            assert error == False
            # Override array should be False at slot 8 because AC charging blocks it
            assert override_array[8] == False

    @patch("src.interfaces.optimization_interface.EOSBackend")
    def test_state_transition_grid_charge_to_avoid_discharge(
        self,
        mock_backend,
        config_with_override_enabled,
        berlin_timezone,
    ):
        """Test state transition from grid charging to avoid discharge with PV > Load override"""
        # Scenario: Slot 7 (AC charging), Slot 8 (avoid discharge + PV > Load)
        eos_request = {
            "ems": {
                "pv_prognose_wh": [
                    100,
                    200,
                    500,
                    600,
                    1000,
                    1500,
                    3000,
                    4000,
                    5000,
                    3000,
                    1000,
                    500,
                    300,
                    200,
                    100,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                ],
                "gesamtlast": [400] * 24,
                "strompreis_euro_pro_wh": [0.0002] * 24,
                "einspeiseverguetung_euro_pro_wh": [0.00008] * 24,
                "preis_euro_pro_wh_akku": 0.0001,
            },
        }

        # Response: Slot 7 AC charging, Slot 8 avoid discharge
        response_grid_to_avoid = {
            "ac_charge": [
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0.4,
                0,
                0,  # Slot 7: AC charge=0.4, Slot 8: AC charge=0
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ],
            "dc_charge": [0] * 24,
            "discharge_allowed": [0] * 24,  # All slots avoid discharge
            "start_solution": [0, 0],
        }

        # Create config with override enabled
        override_config = config_with_override_enabled.copy()
        override_config["dyn_override_discharge_allowed_pv_greater_load"] = True

        optimization_interface = OptimizationInterface(
            config=override_config, time_frame_base=3600, timezone=berlin_timezone
        )
        optimization_interface.last_eos_request = eos_request

        # Current time at slot 8 (8:00 AM)
        # Slot 7: AC charging (0.4) for grid priority
        # Slot 8: Avoid discharge BUT PV (5000) > Load (400), so override should apply
        with patch("src.interfaces.optimization_interface.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 10, 4, 8, 0, 0, tzinfo=berlin_timezone
            )
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(
                *args, **kwargs
            )

            ac_charge, dc_charge, discharge_allowed, error, override_array = (
                optimization_interface.examine_response_to_control_data(
                    response_grid_to_avoid
                )
            )

            # At slot 8: Should be overridden to True (PV > Load, no AC charging, avoid discharge)
            assert discharge_allowed == True
            assert error == False

            # Verify array state:
            # Slot 7: PV (4000) > Load (400) BUT AC charge (0.4) blocks override
            assert override_array[7] == False
            # Slot 8: PV (5000) > Load (400), AC charge = 0, override allowed
            assert override_array[8] == True
            # Slot 9: PV (3000) > Load (400), no AC charge, override allowed
            assert override_array[9] == True
            # Slot 12: PV (300) < Load (400), no override applies
            assert override_array[12] == False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
