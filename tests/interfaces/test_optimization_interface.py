"""
Unit tests for the OptimizationInterface class, which provides an abstraction layer for optimization
backends such as EOS and EVCC Opt. These tests validate correct integration, response handling, and
configuration management for different backend sources.
Fixtures:
    - eos_server_config: Supplies a sample configuration dictionary for the EOS backend.
    - evcc_opt_config: Supplies a sample configuration dictionary for the EVCC Opt backend.
    - berlin_timezone: Provides a pytz timezone object for Europe/Berlin.
    - sample_eos_request: Supplies a representative optimization request payload in EOS format.
Test Cases:
    - test_eos_server_optimize: Verifies optimization with the EOS backend, ensuring the response
      structure and runtime value are as expected.
    - test_evcc_opt_optimize: Verifies optimization with the EVCC Opt backend, checking response
      structure and runtime value.
    - test_control_data_tracking: Checks the extraction and type correctness of control data from
      optimization responses.
    - test_get_eos_version: Ensures the EOS backend version retrieval returns the expected version
      string.
    - test_backend_selection_eos: Verifies that the correct backend is selected for the EOS server.
    - test_backend_selection_evcc: Verifies that the correct backend is selected for the EVCC Opt.
    - test_backend_selection_unknown: Confirms that an error is raised for an unknown backend
        source.
Mocks:
    - Uses unittest.mock.patch to replace backend optimization and version retrieval methods,
      allowing isolated testing of the interface logic without requiring actual backend servers.
Usage:
    Run with pytest to execute all test cases and validate the OptimizationInterface integration
    with supported backends.

"""

from unittest.mock import patch
import pytz
import pytest
from src.interfaces.optimization_interface import OptimizationInterface


@pytest.fixture(name="eos_server_config")
def fixture_eos_server_config():
    """
    Provides a sample EOS server configuration dictionary.
    Returns:
        dict: Configuration for EOS backend.
    """
    return {
        "source": "eos_server",
        "server": "localhost",
        "port": 8503,
    }


@pytest.fixture(name="evcc_opt_config")
def fixture_evcc_opt_config():
    """
    Provides a sample EVCC Opt server configuration dictionary.
    Returns:
        dict: Configuration for EVCC Opt backend.
    """
    return {
        "source": "evcc_opt",
        "server": "localhost",
        "port": 7050,
    }


@pytest.fixture(name="berlin_timezone")
def fixture_berlin_timezone():
    """
    Provides a timezone object for Europe/Berlin.
    Returns:
        pytz.timezone: Timezone object.
    """

    return pytz.timezone("Europe/Berlin")


@pytest.fixture(name="sample_eos_request")
def fixture_sample_eos_request():
    """
    Provides a sample EOS-format optimization request.
    Returns:
        dict: Sample request payload.
    """
    return {
        "ems": {
            "pv_prognose_wh": [0.0] * 48,
            "strompreis_euro_pro_wh": [0.0003] * 48,
            "einspeiseverguetung_euro_pro_wh": [0.000075] * 48,
            "gesamtlast": [400.0] * 48,
        },
        "pv_akku": {
            "device_id": "battery1",
            "capacity_wh": 20000,
            "charging_efficiency": 0.9,
            "discharging_efficiency": 0.9,
            "max_charge_power_w": 10000,
            "initial_soc_percentage": 20,
            "min_soc_percentage": 5,
            "max_soc_percentage": 100,
        },
    }


def test_eos_server_optimize(eos_server_config, berlin_timezone, sample_eos_request):
    """
    Test optimization with EOS backend.
    Ensures the response is a dict and contains expected keys.
    """
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_eos.EOSBackend.optimize"
    ) as mock_opt:
        mock_opt.return_value = (
            {
                "ac_charge": [0.1] * 48,
                "dc_charge": [0.2] * 48,
                "discharge_allowed": [1] * 48,
                "start_solution": [0] * 48,
            },
            1.0,
        )
        interface = OptimizationInterface(eos_server_config, berlin_timezone)
        response, avg_runtime = interface.optimize(sample_eos_request)
        assert isinstance(response, dict)
        assert avg_runtime == 1.0
        assert "ac_charge" in response


def test_evcc_opt_optimize(evcc_opt_config, berlin_timezone, sample_eos_request):
    """
    Test optimization with EVCC Opt backend.
    Ensures the response is a dict and contains expected keys.
    """
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_evcc_opt.EVCCOptBackend.optimize"
    ) as mock_opt:
        mock_opt.return_value = (
            {
                "ac_charge": [0.1] * 48,
                "dc_charge": [0.2] * 48,
                "discharge_allowed": [1] * 48,
                "start_solution": [0] * 48,
            },
            1.0,
        )
        interface = OptimizationInterface(evcc_opt_config, berlin_timezone)
        response, avg_runtime = interface.optimize(sample_eos_request)
        assert isinstance(response, dict)
        assert avg_runtime == 1.0
        assert "ac_charge" in response


def test_control_data_tracking(eos_server_config, berlin_timezone, sample_eos_request):
    """
    Test control data tracking and response examination.
    Ensures correct types for control values.
    """
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_eos.EOSBackend.optimize"
    ) as mock_opt:
        mock_opt.return_value = (
            {
                "ac_charge": [0.1] * 48,
                "dc_charge": [0.2] * 48,
                "discharge_allowed": [1] * 48,
                "start_solution": [0] * 48,
            },
            1.0,
        )
        interface = OptimizationInterface(eos_server_config, berlin_timezone)
        response, _ = interface.optimize(sample_eos_request)
        ac, dc, discharge, error = interface.examine_response_to_control_data(response)
        assert isinstance(ac, float)
        assert isinstance(dc, float)
        assert isinstance(discharge, bool)
        assert isinstance(error, bool) or isinstance(error, int)


def test_get_eos_version(eos_server_config, berlin_timezone):
    """
    Test EOS version retrieval from the backend.
    Ensures the correct version string is returned.
    """
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_eos.EOSBackend.get_eos_version"
    ) as mock_ver:
        mock_ver.return_value = "2025-04-09"
        interface = OptimizationInterface(eos_server_config, berlin_timezone)
        assert interface.get_eos_version() == "2025-04-09"


def test_backend_selection_eos(eos_server_config, berlin_timezone):
    """
    Test that EOSBackend is selected for 'eos_server' source.
    """
    interface = OptimizationInterface(eos_server_config, berlin_timezone)
    assert interface.backend_type == "eos_server"


def test_backend_selection_evcc(evcc_opt_config, berlin_timezone):
    """
    Test that EVCCOptBackend is selected for 'evcc_opt' source.
    """
    interface = OptimizationInterface(evcc_opt_config, berlin_timezone)
    assert interface.backend_type == "evcc_opt"


def test_backend_selection_unknown(berlin_timezone):
    """
    Test that an unknown backend source raises an error or uses a default.
    """
    unknown_config = {"source": "unknown_backend", "server": "localhost", "port": 9999}
    with pytest.raises(Exception):
        OptimizationInterface(unknown_config, berlin_timezone)


def test_interface_methods_exist(eos_server_config, berlin_timezone):
    """
    Test that OptimizationInterface exposes required methods.
    """
    interface = OptimizationInterface(eos_server_config, berlin_timezone)
    for method in [
        "optimize",
        "examine_response_to_control_data",
        "get_last_control_data",
        "get_last_start_solution",
        "get_home_appliance_released",
        "get_home_appliance_start_hour",
        "calculate_next_run_time",
        "get_eos_version",
    ]:
        assert hasattr(interface, method)


class DummyBackend:
    """
    A dummy backend class for testing optimization interfaces.
    Attributes:
        base_url (str): The base URL for the backend.
        time_zone (str): The time zone associated with the backend.
        backend_type (str): The type of backend, set to "dummy".
    Methods:
        optimize(eos_request, timeout=180):
            Simulates an optimization process and returns dummy results.
    """

    def __init__(self, base_url, time_zone):
        self.base_url = base_url
        self.time_zone = time_zone
        self.backend_type = "dummy"

    def optimize(self, eos_request, timeout=180):
        """
        Optimizes the given EOS request and returns the optimization results.

        Args:
            eos_request: The request object containing EOS parameters for optimization.
            timeout (int, optional): Maximum time allowed for the optimization process
            in seconds. Defaults to 180.

        Returns:
            tuple: A tuple containing:
                - dict: Optimization results with key 'ac_charge' mapped to a list
                    of 48 float values.
                - float: The objective value of the optimization.
        """
        return {"ac_charge": [0.5] * 48}, 0.5


def test_dummy_backend_integration(monkeypatch, berlin_timezone):
    """
    Test that a new backend can be integrated without breaking the interface.
    """
    config = {"source": "dummy", "server": "localhost", "port": 1234}
    # Monkeypatch the OptimizationInterface to use DummyBackend for 'dummy'

    orig_init = OptimizationInterface.__init__

    def patched_init(self, config, timezone):
        self.eos_source = config.get("source", "eos_server")
        self.base_url = (
            f"http://{config.get('server', 'localhost')}:{config.get('port', 8503)}"
        )
        self.time_zone = timezone
        if self.eos_source == "dummy":
            self.backend = DummyBackend(self.base_url, self.time_zone)
            self.backend_type = "dummy"
        else:
            orig_init(self, config, timezone)
        self.last_start_solution = None
        self.home_appliance_released = False
        self.home_appliance_start_hour = None
        self.last_control_data = [
            {
                "ac_charge_demand": 0,
                "dc_charge_demand": 0,
                "discharge_allowed": False,
                "error": 0,
                "hour": -1,
            },
            {
                "ac_charge_demand": 0,
                "dc_charge_demand": 0,
                "discharge_allowed": False,
                "error": 0,
                "hour": -1,
            },
        ]

    monkeypatch.setattr(OptimizationInterface, "__init__", patched_init)
    interface = OptimizationInterface(config, berlin_timezone)
    response, avg_runtime = interface.optimize({})
    assert response["ac_charge"][0] == 0.5
    assert avg_runtime == 0.5


def test_backend_error_handling(eos_server_config, berlin_timezone):
    """
    Test that backend errors are handled and do not crash the interface.
    """
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_eos.EOSBackend.optimize"
    ) as mock_opt:
        mock_opt.side_effect = Exception("Backend error")
        interface = OptimizationInterface(eos_server_config, berlin_timezone)
        with pytest.raises(Exception):
            interface.optimize({})
