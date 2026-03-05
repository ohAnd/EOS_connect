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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
