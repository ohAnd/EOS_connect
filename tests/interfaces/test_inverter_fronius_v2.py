"""
Unit tests for the FroniusWRV2 class in src.interfaces.inverter_fronius_v2.

This module contains tests for the Fronius GEN24 V2 Interface with updated
HTTP authentication, focusing on inverter data monitoring functionality.
"""

from unittest.mock import patch, MagicMock, Mock
import pytest
import json
from src.interfaces.inverter_fronius_v2 import FroniusWRV2

# Accessing protected members is fine in white-box tests.
# pylint: disable=protected-access


@pytest.fixture
def default_config():
    """
    Returns a default configuration dictionary for FroniusWRV2.
    """
    return {
        "address": "192.168.1.102",
        "user": "customer",
        "password": "test_password",
        "max_pv_charge_rate": 15000,
        "max_grid_charge_rate": 10000,
        "min_soc": 15,
        "max_soc": 100,
    }


@pytest.fixture
def mock_version_response():
    """
    Returns a mock firmware version response.
    """
    return {"swrevisions": {"GEN24": "1.38.6-1"}}


@pytest.fixture
def mock_inverter_data_response():
    """
    Returns a mock inverter monitoring data response.
    """
    return {
        "Body": {
            "Data": {
                "0": {
                    "channels": {
                        "DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 35.5,
                        "MODULE_TEMPERATURE_MEAN_01_F32": 42.3,
                        "MODULE_TEMPERATURE_MEAN_03_F32": 41.8,
                        "MODULE_TEMPERATURE_MEAN_04_F32": 40.9,
                        "FANCONTROL_PERCENT_01_F32": 55.0,
                        "FANCONTROL_PERCENT_02_F32": 52.5,
                    }
                }
            }
        }
    }


@pytest.fixture
def fronius_v2_instance(default_config):
    """
    Creates a FroniusWRV2 instance with mocked HTTP requests.
    """
    with patch("src.interfaces.inverter_fronius_v2.requests.Session") as mock_session:
        version_data = {"swrevisions": {"GEN24": "1.38.6-1"}}
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = json.dumps(version_data)
        mock_response.json.return_value = version_data

        mock_session_instance = Mock()
        mock_session_instance.get.return_value = mock_response
        mock_session_instance.request.return_value = mock_response
        mock_session.return_value = mock_session_instance

        instance = FroniusWRV2(default_config)
        instance.session = mock_session_instance
        return instance


class TestFroniusV2Initialization:
    """Tests for FroniusWRV2 initialization and configuration."""

    def test_init_sets_attributes(self, fronius_v2_instance):
        """Test that initialization sets attributes correctly."""
        assert fronius_v2_instance.address == "192.168.1.102"
        assert fronius_v2_instance.user == "customer"
        assert fronius_v2_instance.password == "test_password"
        assert fronius_v2_instance.max_pv_charge_rate == 15000
        assert fronius_v2_instance.max_grid_charge_rate == 10000
        assert fronius_v2_instance.min_soc == 15
        assert fronius_v2_instance.max_soc == 100

    def test_firmware_version_detection(self, fronius_v2_instance):
        """Test that firmware version is correctly detected."""
        assert fronius_v2_instance.inverter_sw_revision["major"] == 1
        assert fronius_v2_instance.inverter_sw_revision["minor"] == 38
        assert fronius_v2_instance.inverter_sw_revision["patch"] == 6
        assert fronius_v2_instance.inverter_sw_revision["build"] == 1

    def test_api_configuration_new_firmware(self, fronius_v2_instance):
        """Test API configuration for new firmware (>=1.38.6-1)."""
        assert fronius_v2_instance.api_base == "/api/"
        assert fronius_v2_instance.algorithm == "SHA256"

    def test_api_configuration_old_firmware(self, default_config):
        """Test API configuration for old firmware (<1.36.5-1)."""
        with patch(
            "src.interfaces.inverter_fronius_v2.requests.Session"
        ) as mock_session:
            version_data = {"swrevisions": {"GEN24": "1.30.0-1"}}
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.text = json.dumps(version_data)
            mock_response.json.return_value = version_data

            mock_session_instance = Mock()
            mock_session_instance.get.return_value = mock_response
            mock_session_instance.request.return_value = mock_response
            mock_session.return_value = mock_session_instance

            instance = FroniusWRV2(default_config)
            assert instance.api_base == "/"
            assert instance.algorithm == "MD5"

    def test_api_configuration_middle_firmware(self, default_config):
        """Test API configuration for middle firmware (1.36.5-1 to 1.38.5-x)."""
        with patch(
            "src.interfaces.inverter_fronius_v2.requests.Session"
        ) as mock_session:
            version_data = {"swrevisions": {"GEN24": "1.37.0-1"}}
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.text = json.dumps(version_data)
            mock_response.json.return_value = version_data

            mock_session_instance = Mock()
            mock_session_instance.get.return_value = mock_response
            mock_session_instance.request.return_value = mock_response
            mock_session.return_value = mock_session_instance

            instance = FroniusWRV2(default_config)
            assert instance.api_base == "/api/"
            assert instance.algorithm == "MD5"


class TestInverterDataFetching:
    """Tests for inverter monitoring data functionality."""

    def test_fetch_inverter_data_success(
        self, fronius_v2_instance, mock_inverter_data_response
    ):
        """Test successful inverter data fetching."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_inverter_data_response

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )

        result = fronius_v2_instance.fetch_inverter_data()

        assert result is not None
        assert result["DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"] == 35.5
        assert result["MODULE_TEMPERATURE_MEAN_01_F32"] == 42.3
        assert result["MODULE_TEMPERATURE_MEAN_03_F32"] == 41.8
        assert result["MODULE_TEMPERATURE_MEAN_04_F32"] == 40.9
        assert result["FANCONTROL_PERCENT_01_F32"] == 55.0
        assert result["FANCONTROL_PERCENT_02_F32"] == 52.5

    def test_fetch_inverter_data_rounding(self, fronius_v2_instance):
        """Test that inverter data values are properly rounded to 2 decimal places."""
        mock_response_data = {
            "Body": {
                "Data": {
                    "0": {
                        "channels": {
                            "DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 35.5555,
                            "MODULE_TEMPERATURE_MEAN_01_F32": 42.3333,
                            "MODULE_TEMPERATURE_MEAN_03_F32": 41.8888,
                            "MODULE_TEMPERATURE_MEAN_04_F32": 40.9999,
                            "FANCONTROL_PERCENT_01_F32": 55.0123,
                            "FANCONTROL_PERCENT_02_F32": 52.5678,
                        }
                    }
                }
            }
        }

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_response_data

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )

        result = fronius_v2_instance.fetch_inverter_data()

        assert result["DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"] == 35.56
        assert result["MODULE_TEMPERATURE_MEAN_01_F32"] == 42.33
        assert result["MODULE_TEMPERATURE_MEAN_03_F32"] == 41.89
        assert result["MODULE_TEMPERATURE_MEAN_04_F32"] == 41.0
        assert result["FANCONTROL_PERCENT_01_F32"] == 55.01
        assert result["FANCONTROL_PERCENT_02_F32"] == 52.57

    def test_fetch_inverter_data_endpoint_not_available(self, fronius_v2_instance):
        """Test handling when endpoint returns None."""
        fronius_v2_instance._make_authenticated_request = Mock(return_value=None)

        result = fronius_v2_instance.fetch_inverter_data()

        assert result is None
        assert (
            fronius_v2_instance.inverter_current_data[
                "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"
            ]
            == 0.0
        )

    def test_fetch_inverter_data_404_not_found(self, fronius_v2_instance):
        """Test handling when endpoint returns 404 (not supported by firmware)."""
        mock_response = Mock()
        mock_response.status_code = 404

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )

        result = fronius_v2_instance.fetch_inverter_data()

        assert result is None

    def test_fetch_inverter_data_non_200_status(self, fronius_v2_instance):
        """Test handling when endpoint returns non-200 status code."""
        mock_response = Mock()
        mock_response.status_code = 500

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )

        result = fronius_v2_instance.fetch_inverter_data()

        assert result is None

    def test_fetch_inverter_data_missing_channels(self, fronius_v2_instance):
        """Test handling when response has missing channel data."""
        mock_response_data = {
            "Body": {
                "Data": {
                    "0": {
                        "channels": {
                            "MODULE_TEMPERATURE_MEAN_01_F32": 42.3,
                            # Missing other fields
                        }
                    }
                }
            }
        }

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_response_data

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )

        result = fronius_v2_instance.fetch_inverter_data()

        assert result is not None
        assert result["MODULE_TEMPERATURE_MEAN_01_F32"] == 42.3
        assert (
            result["DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"] == 0.0
        )  # Default for missing

    def test_fetch_inverter_data_malformed_response(self, fronius_v2_instance):
        """Test handling of malformed JSON response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"unexpected": "structure"}

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )

        result = fronius_v2_instance.fetch_inverter_data()

        # Should set all values to 0 when data structure is unexpected
        assert result is not None
        assert all(value == 0.0 for value in result.values())

    def test_fetch_inverter_data_exception_handling(self, fronius_v2_instance):
        """Test exception handling during data fetch."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON")

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )

        result = fronius_v2_instance.fetch_inverter_data()

        assert result is None
        # Should initialize with zeros on error
        assert (
            fronius_v2_instance.inverter_current_data[
                "DEVICE_TEMPERATURE_AMBIENTEMEAN_F32"
            ]
            == 0.0
        )

    def test_get_inverter_current_data(
        self, fronius_v2_instance, mock_inverter_data_response
    ):
        """Test getting current inverter data (getter method)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_inverter_data_response

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )
        fronius_v2_instance.fetch_inverter_data()

        result = fronius_v2_instance.get_inverter_current_data()

        assert result is not None
        assert result["MODULE_TEMPERATURE_MEAN_01_F32"] == 42.3

    def test_get_inverter_current_data_without_prior_fetch(self, fronius_v2_instance):
        """Test getting inverter data when fetch hasn't been called."""
        # Remove the attribute to simulate not having fetched yet
        if hasattr(fronius_v2_instance, "inverter_current_data"):
            delattr(fronius_v2_instance, "inverter_current_data")

        fronius_v2_instance._make_authenticated_request = Mock(return_value=None)

        result = fronius_v2_instance.get_inverter_current_data()

        # Should call fetch_inverter_data and return empty dict or zeros
        assert isinstance(result, dict)

    def test_fetch_inverter_data_uses_correct_endpoint(self, fronius_v2_instance):
        """Test that fetch_inverter_data calls the correct endpoint."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"Body": {"Data": {"0": {"channels": {}}}}}

        mock_auth_request = Mock(return_value=mock_response)
        fronius_v2_instance._make_authenticated_request = mock_auth_request

        fronius_v2_instance.fetch_inverter_data()

        mock_auth_request.assert_called_once_with(
            "GET", "/components/inverter/readable"
        )


class TestInverterDataIntegration:
    """Integration tests for inverter data with other components."""

    def test_inverter_data_storage_persistence(
        self, fronius_v2_instance, mock_inverter_data_response
    ):
        """Test that inverter data is stored in instance variable."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_inverter_data_response

        fronius_v2_instance._make_authenticated_request = Mock(
            return_value=mock_response
        )

        # First fetch
        result1 = fronius_v2_instance.fetch_inverter_data()

        # Verify data is stored
        assert fronius_v2_instance.inverter_current_data == result1

        # Get data without fetching again
        result2 = fronius_v2_instance.get_inverter_current_data()

        assert result2 == result1

    def test_inverter_data_update_on_refetch(self, fronius_v2_instance):
        """Test that inverter data is updated when refetched."""
        # First response
        mock_response1 = Mock()
        mock_response1.status_code = 200
        mock_response1.json.return_value = {
            "Body": {
                "Data": {
                    "0": {
                        "channels": {
                            "DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 30.0,
                            "MODULE_TEMPERATURE_MEAN_01_F32": 40.0,
                            "MODULE_TEMPERATURE_MEAN_03_F32": 40.0,
                            "MODULE_TEMPERATURE_MEAN_04_F32": 40.0,
                            "FANCONTROL_PERCENT_01_F32": 50.0,
                            "FANCONTROL_PERCENT_02_F32": 50.0,
                        }
                    }
                }
            }
        }

        # Second response with different values
        mock_response2 = Mock()
        mock_response2.status_code = 200
        mock_response2.json.return_value = {
            "Body": {
                "Data": {
                    "0": {
                        "channels": {
                            "DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 35.0,
                            "MODULE_TEMPERATURE_MEAN_01_F32": 45.0,
                            "MODULE_TEMPERATURE_MEAN_03_F32": 45.0,
                            "MODULE_TEMPERATURE_MEAN_04_F32": 45.0,
                            "FANCONTROL_PERCENT_01_F32": 60.0,
                            "FANCONTROL_PERCENT_02_F32": 60.0,
                        }
                    }
                }
            }
        }

        fronius_v2_instance._make_authenticated_request = Mock(
            side_effect=[mock_response1, mock_response2]
        )

        # First fetch
        result1 = fronius_v2_instance.fetch_inverter_data()
        assert result1["DEVICE_TEMPERATURE_AMBIENTMEAN_F32"] == 30.0

        # Second fetch
        result2 = fronius_v2_instance.fetch_inverter_data()
        assert result2["DEVICE_TEMPERATURE_AMBIENTMEAN_F32"] == 35.0
        assert result2["MODULE_TEMPERATURE_MEAN_01_F32"] == 45.0


class TestAPISetMethods:
    """Tests for API setter methods related to inverter configuration."""

    def test_api_set_max_pv_charge_rate(self, fronius_v2_instance):
        """Test setting max PV charge rate."""
        fronius_v2_instance.api_set_max_pv_charge_rate(12000)
        assert fronius_v2_instance.max_pv_charge_rate == 12000

    def test_api_set_max_pv_charge_rate_negative(self, fronius_v2_instance):
        """Test that negative values are rejected."""
        original_value = fronius_v2_instance.max_pv_charge_rate
        fronius_v2_instance.api_set_max_pv_charge_rate(-1000)
        assert fronius_v2_instance.max_pv_charge_rate == original_value

    def test_api_set_max_grid_charge_rate(self, fronius_v2_instance):
        """Test setting max grid charge rate."""
        fronius_v2_instance.api_set_max_grid_charge_rate(8000)
        assert fronius_v2_instance.max_grid_charge_rate == 8000

    def test_api_set_max_grid_charge_rate_negative(self, fronius_v2_instance):
        """Test that negative values are rejected."""
        original_value = fronius_v2_instance.max_grid_charge_rate
        fronius_v2_instance.api_set_max_grid_charge_rate(-1000)
        assert fronius_v2_instance.max_grid_charge_rate == original_value
