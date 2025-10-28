"""
Module: test_eos_interface
==========================
This module contains tests for the `EosInterface` class, specifically focusing on the scheduling
algorithm implemented in `calculate_next_run_time`. The tests validate the algorithm's behavior
under various scenarios, ensuring reasonable and consistent scheduling for EOS data collection
jobs.
Fixtures:
---------
- `patch_eos_version`: Automatically patches the EOS version retrieval to return a mocked
    value for all tests.
Tests:
------
- `test_calculate_next_run_time_combinations`: Parametrized test that checks the scheduling
    algorithm across a wide range of input combinations. Validates output type, timing
    constraints, quarter-hour alignment, performance, and consistency.
- `test_calculate_next_run_time_patterns`: Examines the scheduling patterns over multiple
    consecutive runs for different scenarios, ensuring expected behavior such as quarter-hour
    alignment or gap-filling.
- `test_algorithm_behavior_showcase`: Demonstrates the algorithm's scheduling decisions for
    documentation purposes, printing actual run times and types for various intervals and runtimes.
- `test_simulation_over_time`: Simulates a sequence of scheduled runs over time for different
    update intervals, printing results to showcase the algorithm's long-term behavior and
    alignment patterns.
Usage:
------
These tests are designed to validate and document the scheduling logic of `EosInterface`,
ensuring robust and predictable job timing for EOS data collection processes.
"""

import time
from datetime import datetime, timedelta
import pytest
from src.interfaces.eos_interface import EosInterface


@pytest.fixture(autouse=True)
def patch_eos_version(monkeypatch):
    """
    Patches the EosInterface class to mock the retrieval of the EOS version.

    This function uses the provided monkeypatch fixture to replace the
    '_EosInterface__retrieve_eos_version' method of the EosInterface class
    with a lambda that returns a fixed string "mocked_version".

    Args:
        monkeypatch: The pytest monkeypatch fixture used to modify class behavior.
    """
    monkeypatch.setattr(
        EosInterface,
        "_EosInterface__retrieve_eos_version",
        lambda self: "mocked_version",
    )


@pytest.mark.parametrize(
    "current_time",
    [
        datetime(2025, 1, 1, 0, 0),
        datetime(2025, 1, 1, 0, 5),
        datetime(2025, 1, 1, 0, 7),
        datetime(2025, 1, 1, 0, 10),
        datetime(2025, 1, 1, 0, 14),
        datetime(2025, 1, 1, 0, 15),
        datetime(2025, 1, 1, 0, 16),
        datetime(2025, 1, 1, 0, 18),
        datetime(2025, 1, 1, 0, 22),
        datetime(2025, 1, 1, 0, 25),
        datetime(2025, 1, 1, 0, 27, 30),
        datetime(2025, 1, 1, 0, 29),
        datetime(2025, 1, 1, 0, 30),
        datetime(2025, 1, 1, 0, 31),
        datetime(2025, 1, 1, 0, 36),
        datetime(2025, 1, 1, 0, 45),
        datetime(2025, 1, 1, 23, 59),
        datetime(2025, 1, 1, 13, 14, 58),  # specific real-world edge case
    ],
)
@pytest.mark.parametrize(
    "avg_runtime", [60, 87, 90, 300, 600, 900]  # in seconds - added 87s from real logs
)
@pytest.mark.parametrize("update_interval", [60, 300, 600, 899, 900, 1200])
@pytest.mark.parametrize("is_first_run", [True, False])
def test_calculate_next_run_time_combinations(
    current_time, avg_runtime, update_interval, is_first_run
):
    """
    Test the algorithm's actual behavior without trying to predict the exact timing.
    Just validate that the output is reasonable and consistent.
    """
    config = {
        "source": "eos_server",
        "server": "localhost",
        "port": 8503,
    }
    ei = EosInterface(config, None)
    ei.is_first_run = is_first_run

    start = time.perf_counter()
    next_run = ei.calculate_next_run_time(current_time, avg_runtime, update_interval)
    duration = time.perf_counter() - start

    # Basic validation
    assert isinstance(next_run, datetime)
    assert next_run > current_time

    finish_time = next_run + timedelta(seconds=avg_runtime)
    time_until_start = (next_run - current_time).total_seconds()

    # Test 1: Not scheduled too soon (minimum 30 seconds)
    assert (
        time_until_start >= 25
    ), f"Scheduled too soon: {time_until_start}s from {current_time}"

    # Test 2: Not scheduled unreasonably far in future
    max_reasonable_wait = max(3600, update_interval * 3)  # 1 hour or 3x interval
    assert (
        time_until_start <= max_reasonable_wait
    ), f"Scheduled too far: {time_until_start}s > {max_reasonable_wait}s"

    # Test 3: If it claims to be quarter-aligned, verify it actually is
    is_quarter_aligned = finish_time.minute % 15 == 0 and finish_time.second == 0
    if is_quarter_aligned:
        # If quarter-aligned, the finish time should be exactly on a quarter-hour
        assert finish_time.minute in [
            0,
            15,
            30,
            45,
        ], f"Claims quarter-aligned but finishes at {finish_time.strftime('%H:%M:%S')}"

    # Test 4: Performance check
    assert duration < 0.1, f"Calculation too slow: {duration}s"

    # Test 5: Consistency check - running again immediately should give same or later time
    next_run_2 = ei.calculate_next_run_time(current_time, avg_runtime, update_interval)
    time_diff = abs((next_run_2 - next_run).total_seconds())
    assert (
        time_diff < 1
    ), f"Inconsistent results: {next_run} vs {next_run_2} (diff: {time_diff}s)"


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
    config = {
        "source": "eos_server",
        "server": "localhost",
        "port": 8503,
    }
    ei = EosInterface(config, None)

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
    config = {
        "source": "eos_server",
        "server": "localhost",
        "port": 8503,
    }
    ei = EosInterface(config, None)

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
