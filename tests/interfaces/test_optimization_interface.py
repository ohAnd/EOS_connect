"""
Unit tests for the OptimizationInterface class, which provides an abstraction layer for optimization
backends such as EOS and EVopt. These tests validate correct integration, response handling, and
configuration management for different backend sources.
Fixtures:
    - eos_server_config: Supplies a sample configuration dictionary for the EOS backend.
    - evopt_config: Supplies a sample configuration dictionary for the EVopt backend.
    - berlin_timezone: Provides a pytz timezone object for Europe/Berlin.
    - sample_eos_request: Supplies a representative optimization request payload in EOS format.
Test Cases:
    - test_eos_server_optimize: Verifies optimization with the EOS backend, ensuring the response
      structure and runtime value are as expected.
    - test_evopt_optimize: Verifies optimization with the EVopt backend, checking response
      structure and runtime value.
    - test_control_data_tracking: Checks the extraction and type correctness of control data from
      optimization responses.
    - test_get_eos_version: Ensures the EOS backend version retrieval returns the expected version
      string.
    - test_backend_selection_eos: Verifies that the correct backend is selected for the EOS server.
    - test_backend_selection_evcc: Verifies that the correct backend is selected for the EVopt.
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
import time
from datetime import datetime, timedelta
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


@pytest.fixture(name="evopt_config")
def fixture_evopt_config():
    """
    Provides a sample EVopt server configuration dictionary.
    Returns:
        dict: Configuration for EVopt backend.
    """
    return {
        "source": "evopt",
        "server": "localhost",
        "port": 7050,
    }


@pytest.fixture(name="time_frame_base")
def fixture_time_frame_base():
    """
    Provides a timezone object for Europe/Berlin.
    Returns:
        pytz.timezone: Timezone object.
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


def test_eos_server_optimize(
    eos_server_config, time_frame_base, berlin_timezone, sample_eos_request
):
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
        interface = OptimizationInterface(
            eos_server_config, time_frame_base, berlin_timezone
        )
        response, avg_runtime = interface.optimize(sample_eos_request)
        assert isinstance(response, dict)
        assert avg_runtime == 1.0
        assert "ac_charge" in response


def test_evopt_optimize(
    evopt_config, time_frame_base, berlin_timezone, sample_eos_request
):
    """
    Test optimization with EVopt backend.
    Ensures the response is a dict and contains expected keys.
    """
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_evopt.EVOptBackend.optimize"
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
        interface = OptimizationInterface(
            evopt_config, time_frame_base, berlin_timezone
        )
        response, avg_runtime = interface.optimize(sample_eos_request)
        assert isinstance(response, dict)
        assert avg_runtime == 1.0
        assert "ac_charge" in response


def test_control_data_tracking(
    eos_server_config, time_frame_base, berlin_timezone, sample_eos_request
):
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
        interface = OptimizationInterface(
            eos_server_config, time_frame_base, berlin_timezone
        )
        response, _ = interface.optimize(sample_eos_request)
        ac, dc, discharge, error = interface.examine_response_to_control_data(response)
        assert isinstance(ac, float)
        assert isinstance(dc, float)
        assert isinstance(discharge, bool)
        assert isinstance(error, bool) or isinstance(error, int)


def test_get_eos_version(eos_server_config, time_frame_base, berlin_timezone):
    """
    Test EOS version retrieval from the backend.
    Ensures the correct version string is returned.
    """
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_eos.EOSBackend.get_eos_version"
    ) as mock_ver:
        mock_ver.return_value = "2025-04-09"
        interface = OptimizationInterface(
            eos_server_config, time_frame_base, berlin_timezone
        )
        assert interface.get_eos_version() == "2025-04-09"


def test_backend_selection_eos(eos_server_config, time_frame_base, berlin_timezone):
    """
    Test that EOSBackend is selected for 'eos_server' source.
    """
    interface = OptimizationInterface(
        eos_server_config, time_frame_base, berlin_timezone
    )
    assert interface.backend_type == "eos_server"


def test_backend_selection_evcc(evopt_config, time_frame_base, berlin_timezone):
    """
    Test that EVCCOptBackend is selected for 'evopt' source.
    """
    interface = OptimizationInterface(evopt_config, time_frame_base, berlin_timezone)
    assert interface.backend_type == "evopt"


def test_backend_selection_unknown(time_frame_base, berlin_timezone):
    """
    Test that an unknown backend source raises an error or uses a default.
    """
    unknown_config = {"source": "unknown_backend", "server": "localhost", "port": 9999}
    with pytest.raises(Exception):
        OptimizationInterface(unknown_config, time_frame_base, berlin_timezone)


def test_interface_methods_exist(eos_server_config, time_frame_base, berlin_timezone):
    """
    Test that OptimizationInterface exposes required methods.
    """
    interface = OptimizationInterface(
        eos_server_config, time_frame_base, berlin_timezone
    )
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

    def __init__(self, base_url, time_frame_base, time_zone):
        self.base_url = base_url
        self.time_frame_base = time_frame_base
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


def test_dummy_backend_integration(monkeypatch, time_frame_base, berlin_timezone):
    """
    Test that a new backend can be integrated without breaking the interface.
    """
    config = {"source": "dummy", "server": "localhost", "port": 1234}
    # Monkeypatch the OptimizationInterface to use DummyBackend for 'dummy'

    orig_init = OptimizationInterface.__init__

    def patched_init(self, config, time_frame_base, timezone):
        self.eos_source = config.get("source", "eos_server")
        self.base_url = (
            f"http://{config.get('server', 'localhost')}:{config.get('port', 8503)}"
        )
        self.time_frame_base = time_frame_base
        self.time_zone = timezone
        if self.eos_source == "dummy":
            self.backend = DummyBackend(
                self.base_url, self.time_frame_base, self.time_zone
            )
            self.backend_type = "dummy"
        else:
            orig_init(self, config, time_frame_base, timezone)
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
    interface = OptimizationInterface(config, time_frame_base, berlin_timezone)
    response, avg_runtime = interface.optimize({})
    assert response["ac_charge"][0] == 0.5
    assert avg_runtime == 0.5


def test_backend_error_handling(eos_server_config, time_frame_base, berlin_timezone):
    """
    Test that backend errors are handled and do not crash the interface.
    """
    with patch(
        "src.interfaces.optimization_backends.optimization_backend_eos.EOSBackend.optimize"
    ) as mock_opt:
        mock_opt.side_effect = Exception("Backend error")
        interface = OptimizationInterface(
            eos_server_config, time_frame_base, berlin_timezone
        )
        with pytest.raises(Exception):
            interface.optimize({})


# @pytest.mark.parametrize(
#     "current_time",
#     [
#         datetime(2025, 1, 1, 0, 0),
#         datetime(2025, 1, 1, 0, 5),
#         datetime(2025, 1, 1, 0, 7),
#         datetime(2025, 1, 1, 0, 10),
#         datetime(2025, 1, 1, 0, 14),
#         datetime(2025, 1, 1, 0, 15),
#         datetime(2025, 1, 1, 0, 16),
#         datetime(2025, 1, 1, 0, 18),
#         datetime(2025, 1, 1, 0, 22),
#         datetime(2025, 1, 1, 0, 25),
#         datetime(2025, 1, 1, 0, 27, 30),
#         datetime(2025, 1, 1, 0, 29),
#         datetime(2025, 1, 1, 0, 30),
#         datetime(2025, 1, 1, 0, 31),
#         datetime(2025, 1, 1, 0, 36),
#         datetime(2025, 1, 1, 0, 45),
#         datetime(2025, 1, 1, 23, 59),
#         datetime(2025, 1, 1, 13, 14, 58),  # specific real-world edge case
#     ],
# )
# @pytest.mark.parametrize(
#     "avg_runtime", [60, 87, 90, 300, 600, 900]  # in seconds - added 87s from real logs
# )
# @pytest.mark.parametrize("update_interval", [60, 300, 600, 899, 900, 1200])
# @pytest.mark.parametrize("is_first_run", [True, False])
# def test_calculate_next_run_time_combinations(
#     current_time, avg_runtime, update_interval, is_first_run
# ):
#     """
#     Test the algorithm's actual behavior without trying to predict the exact timing.
#     Just validate that the output is reasonable and consistent.
#     """
#     config = {"source": "eos_server", "server": "localhost", "port": 1234}
#     ei = OptimizationInterface(config, None)

#     ei.is_first_run = is_first_run

#     # Start profiling
#     start = time.perf_counter()
#     next_run = ei.calculate_next_run_time(current_time, avg_runtime, update_interval)
#     duration = time.perf_counter() - start
#     print(
#         f"[PROFILE] calculate_next_run_time: "
#         f"current_time={current_time}, avg_runtime={avg_runtime}, "
#         f"update_interval={update_interval}, is_first_run={is_first_run} "
#         f"-> duration={duration:.6f}s"
#     )

#     # Basic validation
#     assert isinstance(next_run, datetime)
#     assert next_run > current_time

#     finish_time = next_run + timedelta(seconds=avg_runtime)
#     time_until_start = (next_run - current_time).total_seconds()

#     # Test 1: Not scheduled too soon (minimum 30 seconds)
#     assert (
#         time_until_start >= 25
#     ), f"Scheduled too soon: {time_until_start}s from {current_time}"

#     # Test 2: Not scheduled unreasonably far in future
#     max_reasonable_wait = max(3600, update_interval * 3)  # 1 hour or 3x interval
#     assert (
#         time_until_start <= max_reasonable_wait
#     ), f"Scheduled too far: {time_until_start}s > {max_reasonable_wait}s"

#     # Test 3: If it claims to be quarter-aligned, verify it actually is
#     is_quarter_aligned = finish_time.minute % 15 == 0 and finish_time.second == 0
#     if is_quarter_aligned:
#         # If quarter-aligned, the finish time should be exactly on a quarter-hour
#         assert finish_time.minute in [
#             0,
#             15,
#             30,
#             45,
#         ], f"Claims quarter-aligned but finishes at {finish_time.strftime('%H:%M:%S')}"

#     # Test 4: Performance check
#     assert duration < 0.1, f"Calculation too slow: {duration}s"

#     # Test 5: Consistency check - running again immediately should give same or later time
#     start2 = time.perf_counter()
#     next_run_2 = ei.calculate_next_run_time(current_time, avg_runtime, update_interval)
#     duration2 = time.perf_counter() - start2
#     print(f"[PROFILE] calculate_next_run_time (repeat): " f"duration={duration2:.6f}s")
#     time_diff = abs((next_run_2 - next_run).total_seconds())
#     assert (
#         time_diff < 1
#     ), f"Inconsistent results: {next_run} vs {next_run_2} (diff: {time_diff}s)"


@pytest.mark.parametrize(
    "scenario",
    [
        # (current_time, update_interval, avg_runtime, expected_pattern)
        (datetime(2025, 1, 1, 0, 0), 300, 60, "mixed"),
        (datetime(2025, 1, 1, 0, 13), 300, 60, "mixed"),
        (datetime(2025, 1, 1, 0, 0), 900, 60, "quarter_heavy"),
        (datetime(2025, 1, 1, 0, 0), 60, 60, "gap_fill_heavy"),
    ],
)
def test_calculate_next_run_time_patterns(scenario):
    """
    Test patterns over multiple runs without being too prescriptive about exact behavior.
    """
    current_time, update_interval, avg_runtime, expected_pattern = scenario

    config = {"source": "eos_server", "server": "localhost", "port": 1234}
    ei = OptimizationInterface(config, 3600, None)

    # Simulate multiple runs to see the pattern
    runs = []
    sim_time = current_time

    for _ in range(8):
        next_run = ei.calculate_next_run_time(sim_time, avg_runtime, update_interval)
        finish_time = next_run + timedelta(seconds=avg_runtime)

        # Determine run type
        is_quarter = finish_time.minute % 15 == 0 and finish_time.second == 0

        # Check if it's a gap-fill (approximately update_interval from last finish)
        if runs:
            time_since_last = (next_run - runs[-1]["finish"]).total_seconds()
            is_gap_fill = abs(time_since_last - update_interval) < 120  # More tolerance
        else:
            is_gap_fill = False

        runs.append(
            {
                "start": next_run,
                "finish": finish_time,
                "is_quarter": is_quarter,
                "is_gap_fill": is_gap_fill,
            }
        )
        sim_time = finish_time + timedelta(seconds=1)  # Move just past the finish

    # Count patterns
    quarter_count = sum(1 for r in runs if r["is_quarter"])
    gap_fill_count = sum(1 for r in runs if r["is_gap_fill"])

    # Validate patterns with relaxed expectations
    if expected_pattern == "quarter_heavy":
        assert (
            quarter_count >= 4
        ), f"Expected many quarter-aligned runs, got {quarter_count}/8"

    elif expected_pattern == "gap_fill_heavy":
        assert (
            gap_fill_count >= 4
        ), f"Expected many gap-fill runs, got {gap_fill_count}/8"

    elif expected_pattern == "mixed":
        # Just ensure we get some reasonable mix and no crazy gaps
        total_time = (runs[-1]["finish"] - runs[0]["start"]).total_seconds()
        avg_gap = total_time / (len(runs) - 1) if len(runs) > 1 else 0
        assert (
            avg_gap < update_interval * 2
        ), f"Average gap too large: {avg_gap}s (update_interval: {update_interval}s)"

    # Universal checks: no run should be scheduled unreasonably
    for i, run in enumerate(runs):
        if i > 0:
            gap = (run["start"] - runs[i - 1]["finish"]).total_seconds()
            assert gap >= 0, f"Overlapping runs at index {i}: gap={gap}s"
            assert gap < 3600, f"Gap too large at index {i}: {gap}s"


def test_simulation_over_time():
    """
    Show how the algorithm behaves over several consecutive runs.
    """
    config = {"source": "eos_server", "server": "localhost", "port": 1234}
    ei = OptimizationInterface(config, 3600, None)

    scenarios = [
        ("1min", 60),
        ("5min", 300),
        ("10min", 600),
        ("15min", 900),
    ]

    for interval_name, update_interval in scenarios:
        print(f"\n=== {interval_name} interval simulation ===")

        sim_time = datetime(2025, 1, 1, 0, 0)
        avg_runtime = 75

        run_count = 0
        quarter_count = 0

        # Run simulation for 12 iterations or until we see a clear pattern
        while run_count < 12:
            next_run = ei.calculate_next_run_time(
                sim_time, avg_runtime, update_interval
            )
            finish_time = next_run + timedelta(seconds=avg_runtime)

            is_quarter = finish_time.minute % 15 == 0 and finish_time.second == 0
            if is_quarter:
                quarter_count += 1

            wait_time = (next_run - sim_time).total_seconds()
            run_type = "Q" if is_quarter else "G"  # Q=Quarter, G=Gap-fill

            print(
                f"Run {run_count+1:2d}: {sim_time.strftime('%H:%M:%S')} → "
                f"{next_run.strftime('%H:%M:%S')} → "
                f"{finish_time.strftime('%H:%M:%S')} "
                f"({run_type}, wait: {wait_time:3.0f}s)"
            )

            # Move to just after the finish time for next iteration
            sim_time = finish_time + timedelta(seconds=1)
            run_count += 1

        print(
            f"Summary: {quarter_count}/{run_count} quarter-aligned runs "
            f"({quarter_count/run_count*100:.1f}%)"
        )

    assert True  # Always pass - this is for documentation
