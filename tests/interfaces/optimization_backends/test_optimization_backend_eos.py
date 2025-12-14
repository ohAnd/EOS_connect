"""
Unit tests for the EOSBackend class, specifically testing the _retrieve_eos_version method.

This test suite validates various scenarios when retrieving the EOS server version,
including successful retrieval, HTTP errors, connection issues, and version comparison logic.

Fixtures:
    - base_url: Provides the base URL for the EOS server.
    - time_frame_base: Provides the time frame base value.
    - berlin_timezone: Provides a pytz timezone object for Europe/Berlin.

Usage:
    Run with pytest: pytest test_optimization_backend_eos.py -v
"""

import json
from unittest.mock import Mock, patch
import pytest
import pytz
import requests
from src.interfaces.optimization_backends.optimization_backend_eos import EOSBackend


@pytest.fixture(name="base_url")
def fixture_base_url():
    """
    Provides the base URL for the EOS server.

    Returns:
        str: Base URL for testing.
    """
    return "http://localhost:8503"


@pytest.fixture(name="time_frame_base")
def fixture_time_frame_base():
    """
    Provides the time frame base value.

    Returns:
        int: Time frame base in seconds.
    """
    return 3600


@pytest.fixture(name="berlin_timezone")
def fixture_berlin_timezone():
    """
    Provides a timezone object for Europe/Berlin.

    Returns:
        pytz.timezone: Timezone object.
    """
    return pytz.timezone("Europe/Berlin")


class TestRetrieveEOSVersion:
    """Test suite for the _retrieve_eos_version method of EOSBackend."""

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_success_with_version(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test successful version retrieval when server returns a specific version.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock response for version
        mock_version_response = Mock()
        mock_version_response.json.return_value = {
            "status": "alive",
            "version": "0.1.0+dev",
        }
        mock_version_response.raise_for_status = Mock()

        # Setup mock response for config optimization
        mock_config_opt_response = Mock()
        mock_config_opt_response.json.return_value = {
            "horizon_hours": 48,
            "genetic": {
                "individuals": 300,
                "generations": 400,
            },
        }
        mock_config_opt_response.raise_for_status = Mock()

        # Setup mock response for config devices
        mock_config_dev_response = Mock()
        mock_config_dev_response.json.return_value = [
            {"charge_rates": [0.0, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]}
        ]
        mock_config_dev_response.raise_for_status = Mock()

        # Setup mock response for config put
        mock_put_response = Mock()
        mock_put_response.raise_for_status = Mock()
        mock_put.return_value = mock_put_response

        def get_side_effect(url, timeout=None):
            if "/v1/health" in url:
                return mock_version_response
            elif "/v1/config/optimization" in url:
                return mock_config_opt_response
            elif "/v1/config/devices/electric_vehicles" in url:
                return mock_config_dev_response
            return Mock()

        mock_get.side_effect = get_side_effect

        # Create EOSBackend instance (will call _retrieve_eos_version in __init__)
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.1.0+dev"
        # Verify health endpoint was called
        assert any("/v1/health" in str(call) for call in mock_get.call_args_list)

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_success_alive_unknown(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test version retrieval when server returns "alive" status with "unknown" version.
        Should default to "0.0.2".

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock response
        mock_response = Mock()
        mock_response.json.return_value = {"status": "alive", "version": "unknown"}
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.0.2"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_http_404(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test version retrieval when server returns HTTP 404 (older EOS version).
        Should return "0.0.1".

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock to raise HTTP 404 error
        mock_response = Mock()
        mock_response.status_code = 404
        http_error = requests.exceptions.HTTPError()
        http_error.response = mock_response
        mock_get.return_value.raise_for_status.side_effect = http_error

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.0.1"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_http_error_non_404(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test version retrieval when server returns a non-404 HTTP error.
        Should return the default version "0.0.2".

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock to raise HTTP 500 error
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        http_error = requests.exceptions.HTTPError()
        http_error.response = mock_response
        mock_get.return_value.raise_for_status.side_effect = http_error

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.0.2"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_connect_timeout(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test version retrieval when connection times out.
        Should return the default version "0.0.2".

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock to raise ConnectTimeout
        mock_get.side_effect = requests.exceptions.ConnectTimeout()

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.0.2"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_connection_error(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test version retrieval when a connection error occurs.
        Should return the default version "0.0.2".

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock to raise ConnectionError
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.0.2"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_request_exception(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test version retrieval when a general request exception occurs.
        Should return the default version "0.0.2".

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock to raise RequestException
        mock_get.side_effect = requests.exceptions.RequestException("Generic error")

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.0.2"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_json_decode_error(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test version retrieval when response cannot be decoded as JSON.
        Should return the default version "0.0.2".

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock to raise JSONDecodeError
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.side_effect = json.JSONDecodeError("Invalid JSON", "", 0)
        mock_get.return_value = mock_response

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.0.2"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_http_error_no_response(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test version retrieval when HTTPError has no response attribute.
        Should return the default version "0.0.2".

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock to raise HTTPError without response
        http_error = requests.exceptions.HTTPError()
        mock_get.return_value.raise_for_status.side_effect = http_error

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert
        assert backend.eos_version == "0.0.2"

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_dev_version_config_needs_update(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test that when version is "0.2.0+dev", the configuration is validated and updated if needed.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock response for version
        mock_version_response = Mock()
        mock_version_response.json.return_value = {
            "status": "alive",
            "version": "0.2.0+dev",
        }
        mock_version_response.raise_for_status = Mock()

        # Setup mock response for config get - needs update
        mock_config_opt_response = Mock()
        mock_config_opt_response.json.return_value = {
            "horizon_hours": 24  # Wrong value, should be 48
        }
        mock_config_opt_response.raise_for_status = Mock()

        # Setup mock response for config devices - missing charge_rates
        mock_config_dev_response = Mock()
        mock_config_dev_response.json.return_value = [{}]  # Empty dict, no charge_rates
        mock_config_dev_response.raise_for_status = Mock()

        # Setup mock response for config put
        mock_put_response = Mock()
        mock_put_response.raise_for_status = Mock()
        mock_put.return_value = mock_put_response

        def get_side_effect(url, timeout=None):
            if "/v1/health" in url:
                return mock_version_response
            elif "/v1/config/optimization" in url:
                return mock_config_opt_response
            elif "/v1/config/devices/electric_vehicles" in url:
                return mock_config_dev_response
            return Mock()

        mock_get.side_effect = get_side_effect

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert version is set correctly
        assert backend.eos_version == "0.2.0+dev"

        # Assert that config update was called (both optimization and devices)
        assert mock_put.call_count == 2

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_dev_version_config_none(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test that when config_devices is None, it's properly initialized.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock response for version
        mock_version_response = Mock()
        mock_version_response.json.return_value = {
            "status": "alive",
            "version": "0.1.0+dev",
        }
        mock_version_response.raise_for_status = Mock()

        # Setup mock response for config optimization - already correct
        mock_config_opt_response = Mock()
        mock_config_opt_response.json.return_value = {
            "horizon_hours": 48,
            "genetic": {
                "individuals": 300,
                "generations": 400,
            },
        }
        mock_config_opt_response.raise_for_status = Mock()

        # Setup mock response for config devices - None
        mock_config_dev_response = Mock()
        mock_config_dev_response.json.return_value = None
        mock_config_dev_response.raise_for_status = Mock()

        # Setup mock response for config put
        mock_put_response = Mock()
        mock_put_response.raise_for_status = Mock()
        mock_put.return_value = mock_put_response

        def get_side_effect(url, timeout=None):
            if "/v1/health" in url:
                return mock_version_response
            elif "/v1/config/optimization" in url:
                return mock_config_opt_response
            elif "/v1/config/devices/electric_vehicles" in url:
                return mock_config_dev_response
            return Mock()

        mock_get.side_effect = get_side_effect

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert version is set correctly
        assert backend.eos_version == "0.1.0+dev"

        # Assert that config update was called for devices (not for optimization since it was OK)
        assert mock_put.call_count == 1

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_non_dev_version(
        self, mock_get, mock_put, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test that version 1.0.0 triggers config validation (since 1.0.0 >= 0.1.0).

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock response for version
        mock_version_response = Mock()
        mock_version_response.json.return_value = {
            "status": "alive",
            "version": "1.0.0",
        }
        mock_version_response.raise_for_status = Mock()

        # Setup mock response for config optimization - already correct
        mock_config_opt_response = Mock()
        mock_config_opt_response.json.return_value = {
            "horizon_hours": 48,
            "genetic": {
                "individuals": 300,
                "generations": 400,
            },
        }
        mock_config_opt_response.raise_for_status = Mock()

        # Setup mock response for config devices - already correct
        mock_config_dev_response = Mock()
        mock_config_dev_response.json.return_value = [
            {"charge_rates": [0.0, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]}
        ]
        mock_config_dev_response.raise_for_status = Mock()

        # Setup mock response for config put
        mock_put_response = Mock()
        mock_put_response.raise_for_status = Mock()
        mock_put.return_value = mock_put_response

        def get_side_effect(url, timeout=None):
            if "/v1/health" in url:
                return mock_version_response
            elif "/v1/config/optimization" in url:
                return mock_config_opt_response
            elif "/v1/config/devices/electric_vehicles" in url:
                return mock_config_dev_response
            return Mock()

        mock_get.side_effect = get_side_effect

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert version is set correctly
        assert backend.eos_version == "1.0.0"

        # Verify health endpoint and config endpoints were called (1.0.0 >= 0.1.0)
        assert mock_get.call_count >= 3  # health + optimization + devices

    @pytest.mark.parametrize(
        "version,should_validate_config",
        [
            ("0.0.1", False),
            ("0.0.2", False),
            ("0.0.3", False),
            ("0.1.0+dev", True),
            ("0.2.0+dev", True),
            ("0.1.0", True),
            ("0.2.0", True),
            ("1.0.0", True),
            ("2025.1.0", True),
        ],
    )
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.put")
    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_with_multiple_versions(
        self,
        mock_get,
        mock_put,
        base_url,
        time_frame_base,
        berlin_timezone,
        version,
        should_validate_config,
    ):
        """
        Test version retrieval with multiple version formats.
        Dev versions (0.1.0+dev, 0.2.0+dev) should trigger config validation,
        while non-dev versions should not.

        Args:
            mock_get: Mocked requests.get function.
            mock_put: Mocked requests.put function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
            version: The version string to test.
            should_validate_config: Whether config validation should occur.
        """
        # Setup mock response for version
        mock_version_response = Mock()
        mock_version_response.json.return_value = {
            "status": "alive",
            "version": version,
        }
        mock_version_response.raise_for_status = Mock()

        if should_validate_config:
            # Setup mock response for config optimization - already correct
            mock_config_opt_response = Mock()
            mock_config_opt_response.json.return_value = {
                "horizon_hours": 48,
                "genetic": {
                    "individuals": 300,
                    "generations": 400,
                },
            }
            mock_config_opt_response.raise_for_status = Mock()

            # Setup mock response for config devices - already correct
            mock_config_dev_response = Mock()
            mock_config_dev_response.json.return_value = [
                {"charge_rates": [0.0, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0]}
            ]
            mock_config_dev_response.raise_for_status = Mock()

            # Setup mock response for config put
            mock_put_response = Mock()
            mock_put_response.raise_for_status = Mock()
            mock_put.return_value = mock_put_response

            def get_side_effect(url, timeout=None):
                if "/v1/health" in url:
                    return mock_version_response
                elif "/v1/config/optimization" in url:
                    return mock_config_opt_response
                elif "/v1/config/devices/electric_vehicles" in url:
                    return mock_config_dev_response
                return Mock()

            mock_get.side_effect = get_side_effect
        else:
            # For non-dev versions, only health endpoint is called
            mock_get.return_value = mock_version_response

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert version is set correctly
        assert backend.eos_version == version

        # Verify health endpoint was called
        assert any("/v1/health" in str(call) for call in mock_get.call_args_list)

        if should_validate_config:
            # For dev versions, config endpoints should be called
            assert mock_get.call_count >= 3  # health + optimization + devices
        else:
            # For non-dev versions, only health endpoint should be called
            assert mock_get.call_count == 1
            mock_get.assert_called_with(base_url + "/v1/health", timeout=10)

    @patch("src.interfaces.optimization_backends.optimization_backend_eos.requests.get")
    def test_retrieve_eos_version_old_version_no_config(
        self, mock_get, base_url, time_frame_base, berlin_timezone
    ):
        """
        Test that old versions (< 0.1.0) don't trigger config validation.

        Args:
            mock_get: Mocked requests.get function.
            base_url: Base URL fixture.
            time_frame_base: Time frame base fixture.
            berlin_timezone: Timezone fixture.
        """
        # Setup mock response for version
        mock_response = Mock()
        mock_response.json.return_value = {
            "status": "alive",
            "version": "0.0.1",  # Old version, below 0.1.0
        }
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        # Create EOSBackend instance
        backend = EOSBackend(base_url, time_frame_base, berlin_timezone)

        # Assert version is set correctly
        assert backend.eos_version == "0.0.1"

        # Verify only health endpoint was called (no config calls for old versions)
        assert mock_get.call_count == 1
        mock_get.assert_called_with(base_url + "/v1/health", timeout=10)


@pytest.mark.parametrize(
    "current_version, compare_to, expected",
    [
        # 0.1.0+dev cases
        ("0.0.1", "0.1.0", False),  # lower public segment -> False
        ("0.0.2", "0.1.0", False),  # lower public segment -> False
        ("0.0.3", "0.1.0", False),  #
        # 0.1.0+dev cases
        ("0.1.0+dev", "0.0.9", True),  # higher public segment -> True
        (
            "0.1.0+dev",
            "0.1.0",
            True,
        ),  # local version sorts AFTER public release per PEP 440
        # 0.1.0 cases
        ("0.1.0", "0.1.0", True),  # equal
        ("0.1.0", "0.2.0", False),  # lower minor -> False
        # 0.2.0+dev cases
        ("0.2.0+dev", "0.1.0", True),  # higher minor -> True
        (
            "0.2.0+dev",
            "0.2.0",
            True,
        ),  # local version sorts AFTER public release per PEP 440
        # Optional extra to illustrate pre-release behavior:
        ("0.1.0.dev0", "0.1.0", False),  # dev pre-release sorts BEFORE final
    ],
)
def test_is_eos_version_at_least(current_version, compare_to, expected):
    """
    Validate semantic version comparisons for EOSBackend.is_eos_version_at_least.

    Notes:
    - Local versions like '0.1.0+dev' sort after the corresponding public release '0.1.0'
      according to PEP 440 (packaging.version semantics), so they are considered greater.
    - Pre-releases like '0.1.0.dev0' sort before the final release '0.1.0'.
    """
    # Avoid __init__ network calls: construct without calling __init__
    backend = EOSBackend.__new__(EOSBackend)
    backend.eos_version = current_version

    assert backend.is_eos_version_at_least(compare_to) is expected
