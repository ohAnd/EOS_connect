"""
Are the managed-load records recoverable alongside the request they went with?

A captured request cannot be replayed on its own. The record carries the demand, the
gate state and the commitment, and those are exactly what differ between two solves
that see an identical request - which is the shape of the plan oscillation this was
added to diagnose. Reconstructing the record from the plan means guessing five
fields, and guessing two of them wrong is enough to make a replay disagree with the
instance it came from.

Diagnostic, so the bar is: same cycle as the request, and never fatal.
"""

import json

import pytest


class _Scheduler:
    """The parts of OptimizationScheduler these tests exercise, lifted out."""

    def __init__(self):
        self.last_managed_loads = json.dumps(
            {"status": "Awaiting first optimization run"}, indent=4
        )

    def get_last_managed_loads(self):
        return self.last_managed_loads

    def store(self, timestamp, budget_w, records):
        self.last_managed_loads = json.dumps(
            {"timestamp": timestamp, "budget_w": budget_w,
             "records": records if records is not None else []},
            indent=4, default=str,
        )


@pytest.fixture(name="scheduler")
def scheduler_fixture():
    return _Scheduler()


def test_before_the_first_run_it_says_so_rather_than_being_empty(scheduler):
    payload = json.loads(scheduler.get_last_managed_loads())
    assert payload["status"] == "Awaiting first optimization run"


def test_the_records_are_recoverable_as_sent(scheduler):
    records = [{
        "id": "pool", "demand_wh": 54135.0, "max_power_w": 1600.0,
        "value_eur_per_wh": 0.0003, "min_runtime_slots": 2,
        "committed_on_slots": 1, "already_running": True,
        "feasible": [True, False, True], "day_index": [0, 0, 0],
    }]
    scheduler.store("2026-09-21T10:52:00+02:00", 3000.0, records)

    payload = json.loads(scheduler.get_last_managed_loads())
    assert payload["records"] == records
    # The fields a replay cannot guess are the point of keeping them.
    got = payload["records"][0]
    for field in ("demand_wh", "committed_on_slots", "already_running",
                  "min_runtime_slots", "value_eur_per_wh"):
        assert field in got, field


def test_it_carries_the_timestamp_of_its_own_request(scheduler):
    """Same cycle, or the pair cannot be trusted against each other."""
    scheduler.store("2026-09-21T10:52:00+02:00", 0.0, [])
    assert json.loads(scheduler.get_last_managed_loads())["timestamp"] == \
        "2026-09-21T10:52:00+02:00"


def test_a_backend_that_schedules_nothing_stores_an_empty_list(scheduler):
    """Not None: the reader should see "none were sent", not a missing key."""
    scheduler.store("2026-09-21T10:52:00+02:00", 0.0, None)
    payload = json.loads(scheduler.get_last_managed_loads())
    assert payload["records"] == []


def test_the_budget_travels_with_them(scheduler):
    scheduler.store("2026-09-21T10:52:00+02:00", 4200.0, [])
    assert json.loads(scheduler.get_last_managed_loads())["budget_w"] == 4200.0


def test_something_unserialisable_does_not_break_the_dump(scheduler):
    """
    Diagnostics must never take the optimizer down. A record carrying a datetime or a
    stray object is stringified rather than raising.
    """
    from datetime import datetime

    scheduler.store("t", 0.0, [{"id": "pool", "when": datetime(2026, 9, 21, 10, 0)}])
    payload = json.loads(scheduler.get_last_managed_loads())
    assert "2026-09-21" in payload["records"][0]["when"]
