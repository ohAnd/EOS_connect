# pylint: disable=protected-access
"""
Characterization of slot indexing and midnight anchoring on DST days.

The forecast grid is absolute (slot i = midnight + i*time_frame_base). Both
halves of the arithmetic used to run on the wall clock — a `now.hour`-based
index against a `now.replace(hour=0, ...)` anchor whose pytz offset is an hour
off on a DST day — two errors that cancelled on ordinary days and broke
together on transition days. Everything now counts absolute time against
`timeseries_grid.local_midnight`; these tests pin that for the ~21 hours after
each transition where wall clock and absolute slot disagree.

Reference days: 2026-03-29 (23 hours) and 2026-10-25 (25 hours), Europe/Berlin —
the same days tests/interfaces/test_pv_autoscaler_dst.py uses.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytz

from src.interfaces.optimization_backends.optimization_backend_evopt import (
    EVOptBackend,
)
from src.interfaces.optimization_interface import OptimizationInterface

BERLIN = pytz.timezone("Europe/Berlin")

NORMAL_DAY = (2026, 8, 23)
SPRING_FORWARD = (2026, 3, 29)  # 02:00 -> 03:00, the day has 23 hours
FALL_BACK = (2026, 10, 25)  # 03:00 -> 02:00, the day has 25 hours

def berlin(day, hour, minute=0):
    """A timezone-aware Berlin timestamp on one of the reference days."""
    return BERLIN.localize(datetime(*day, hour, minute))


def clock_at(module_path, moment):
    """Pin a module's datetime.now() the way test_evopt_slot_alignment.py does."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    return patch(module_path, _Clock)


class TestMidnightAnchor:
    """
    Documents why `timeseries_grid.local_midnight` exists: the anchor pattern
    the code used to rely on — `now.replace(hour=0, ...)` — is not local
    midnight on a DST day, and `(now - anchor)` against it reproduces the wall
    clock. These pin the pytz semantics the code must never return to.
    """

    def test_replace_keeps_nows_offset_instead_of_midnights(self):
        """On the 25-hour day the replaced "midnight" sits a full hour late."""
        now = berlin(FALL_BACK, 15, 30)
        replaced = now.replace(hour=0, minute=0, second=0, microsecond=0)
        true_midnight = berlin(FALL_BACK, 0)
        assert replaced.utcoffset() == timedelta(hours=1)
        assert true_midnight.utcoffset() == timedelta(hours=2)
        assert replaced - true_midnight == timedelta(hours=1)

    @pytest.mark.parametrize("day", [SPRING_FORWARD, FALL_BACK])
    def test_elapsed_time_against_the_replace_anchor_is_just_the_wall_clock(self, day):
        """(now - replace-anchor) // 3600 == now.hour — the planned formula is a no-op."""
        now = berlin(day, 15, 30)
        replaced = now.replace(hour=0, minute=0, second=0, microsecond=0)
        assert int((now - replaced).total_seconds()) // 3600 == now.hour

    @pytest.mark.parametrize(
        "day,expected_hours", [(SPRING_FORWARD, 14), (FALL_BACK, 16)]
    )
    def test_elapsed_time_against_true_midnight_differs_on_dst_days(
        self, day, expected_hours
    ):
        """Only a properly localized midnight yields the absolute elapsed hours."""
        now = berlin(day, 15, 30)
        true_midnight = berlin(day, 0)
        assert int((now - true_midnight).total_seconds()) // 3600 == expected_hours


class TestEvoptTimeParameters:
    """_calculate_time_parameters: the invariant that must survive the anchor fix."""

    @pytest.fixture(name="backend")
    def backend_fixture(self):
        """A bare backend; only the clock and the slot length matter here."""
        backend = EVOptBackend.__new__(EVOptBackend)
        backend.time_zone = BERLIN
        return backend

    def at(self, moment):
        """Pin the backend module's clock to *moment*."""
        return clock_at(
            "src.interfaces.optimization_backends.optimization_backend_evopt.datetime",
            moment,
        )

    @pytest.mark.parametrize("day", [NORMAL_DAY, SPRING_FORWARD, FALL_BACK])
    @pytest.mark.parametrize("hour,minute", [(15, 30), (23, 30)])
    @pytest.mark.parametrize("time_frame_base", [900, 3600])
    def test_slot_count_plus_remaining_horizon_tiles_the_control_array(
        self, backend, day, hour, minute, time_frame_base
    ):
        """pad_past + result window must tile n_control exactly, on every day.

        Today that holds because index and length share the wall clock; after
        the anchor fix it must hold because both are absolute.
        """
        backend.time_frame_base = time_frame_base
        with self.at(berlin(day, hour, minute)):
            params = backend._calculate_time_parameters()
        assert len(params["pad_past"]) == params["current_slot"]
        assert params["current_slot"] + params["n_result"] == params["n_control"]

    @pytest.mark.parametrize(
        "day,time_frame_base,expected_slot",
        [
            (FALL_BACK, 3600, 16),  # 16.5 absolute hours since midnight
            (SPRING_FORWARD, 3600, 14),  # 14.5 absolute hours
            (FALL_BACK, 900, 66),
            (SPRING_FORWARD, 900, 58),
        ],
    )
    def test_the_slot_index_counts_absolute_time_on_dst_days(
        self, backend, day, time_frame_base, expected_slot
    ):
        """At 15:30 local, the slot index must count hours actually elapsed."""
        backend.time_frame_base = time_frame_base
        with self.at(berlin(day, 15, 30)):
            params = backend._calculate_time_parameters()
        assert params["current_slot"] == expected_slot

    def test_late_on_the_long_day_the_index_passes_the_wall_clock_maximum(
        self, backend
    ):
        """23:30 on the 25-hour day is slot 24 — an index `now.hour` can never produce."""
        backend.time_frame_base = 3600
        with self.at(berlin(FALL_BACK, 23, 30)):
            params = backend._calculate_time_parameters()
        assert params["current_slot"] == 24
        assert params["n_result"] == 24


class TestControlDataSlotSelection:
    """
    examine_response_to_control_data picks the control values for "now" out of
    midnight-anchored arrays by the absolute slot and labels the entries with
    it; "hour" remains as a wall-clock mirror of the slot's start.
    """

    @pytest.fixture(name="interface")
    def interface_fixture(self):
        """A bare interface carrying just the state the examined path touches."""
        iface = OptimizationInterface.__new__(OptimizationInterface)
        iface.time_zone = BERLIN
        iface.time_frame_base = 3600
        iface.last_control_data = [{}, {}]
        iface.dyn_override_discharge_allowed = False
        iface.last_eos_request = None
        iface.home_appliance_start_hour = None
        iface.home_appliance_released = False
        iface.last_start_solution = None
        return iface

    def examine(self, iface, moment, response):
        """Run the examination with the module clock pinned to *moment*."""
        with clock_at("src.interfaces.optimization_interface.datetime", moment):
            return iface.examine_response_to_control_data(response)

    def response(self, washingstart):
        """A minimal optimizer response whose ac_charge values encode their slot."""
        return {
            "ac_charge": [round(s / 100, 2) for s in range(48)],
            "dc_charge": [0.0] * 48,
            "discharge_allowed": [0] * 48,
            "start_solution": [0, 1],
            "washingstart": washingstart,
        }

    def test_the_control_slot_is_absolute_on_dst_days(self, interface):
        """The control value for 15:30 on the long day lives in slot 16, not 15."""
        self.examine(interface, berlin(FALL_BACK, 15, 30), self.response(16))
        assert interface.last_control_data[0]["ac_charge_demand"] == 0.16
        assert interface.last_control_data[0]["slot"] == 16
        assert interface.last_control_data[1]["slot"] == 17

    def test_the_hour_label_mirrors_the_slots_wall_clock(self, interface):
        """"hour" stays as a wall-clock mirror for anything external reading it."""
        self.examine(interface, berlin(FALL_BACK, 15, 30), self.response(16))
        # Slot 16 on the 25-hour day starts at 15:00 local (CET).
        assert interface.last_control_data[0]["hour"] == 15

    def test_the_appliance_release_matches_the_absolute_slot(self, interface):
        """A washingstart equal to the current absolute slot releases the appliance."""
        self.examine(interface, berlin(FALL_BACK, 15, 30), self.response(16))
        assert interface.home_appliance_released is True

    def test_a_washingstart_for_another_slot_does_not_release(self, interface):
        """The wall-clock hour (15) must not release a slot-16 start."""
        self.examine(interface, berlin(FALL_BACK, 15, 30), self.response(15))
        assert interface.home_appliance_released is False
