"""
Moving per-slot data between EOS slot space and solver slot space.

EOS arrays start at local midnight; the solver is handed the horizon starting *now*.
Anything indexed by slot has to make that trip or it silently describes the wrong hours
- a mask meaning "not before 06:00" would let a pool run at midnight, and the plan would
look entirely reasonable while doing it.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.interfaces.optimization_backends.optimization_backend_evopt import EVOptBackend

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(name="backend")
def backend_fixture():
    backend = EVOptBackend.__new__(EVOptBackend)
    backend.time_zone = BERLIN
    backend.time_frame_base = 900
    return backend


def _at(backend, hour, minute=0):
    """Pin the backend's clock, which is what decides the rotation."""
    moment = datetime(2026, 9, 12, hour, minute, tzinfo=BERLIN)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    return patch(
        "src.interfaces.optimization_backends.optimization_backend_evopt.datetime",
        _Clock,
    )


def test_the_horizon_starts_at_the_current_slot(backend):
    series = list(range(192))
    with _at(backend, 6, 0):
        window = backend.to_solver_slots(series)
    assert window[0] == 24            # 06:00 is slot 24 at quarter-hour resolution
    assert window[1] == 25


def test_a_single_true_slot_lands_on_the_hour_it_names(backend):
    """The property that matters: 07:00 must still mean 07:00 after the round trip."""
    mask = [False] * 192
    mask[28] = True                   # 07:00
    with _at(backend, 6, 0):
        window = backend.to_solver_slots(mask, fill=False)
        assert window.index(True) == 4        # four quarter-hours after 06:00
        assert backend.from_solver_slots(window, fill=False).index(True) == 28


@pytest.mark.parametrize("hour,minute", [(0, 0), (6, 30), (13, 45), (23, 45)])
def test_the_round_trip_puts_every_future_slot_back_where_it_started(
    backend, hour, minute
):
    series = [float(i) for i in range(192)]
    with _at(backend, hour, minute):
        current = hour * 4 + minute // 15
        back = backend.from_solver_slots(backend.to_solver_slots(series))
        assert back[current:] == series[current:]


def test_slots_already_gone_come_back_as_the_fill(backend):
    with _at(backend, 6, 0):
        back = backend.from_solver_slots(backend.to_solver_slots(list(range(192))))
    assert back[:24] == [0.0] * 24


def test_stale_wrapped_slots_are_cut_rather_than_carried(backend):
    """
    The reason the truncation exists. Rotating alone leaves this morning's slots at the
    tail, and the solver read those zero-priced hours as free electricity.
    """
    with _at(backend, 18, 0):
        window = backend.to_solver_slots(list(range(192)))
    # From 18:00 there are 24 slots left today plus 96 tomorrow, and not one more.
    assert len(window) == 24 + 96
    assert max(window) == 191
    assert window == list(range(72, 192))


def test_an_empty_series_stays_empty(backend):
    with _at(backend, 9, 0):
        assert backend.to_solver_slots([]) == []
        assert backend.to_solver_slots(None) == []


def test_a_short_series_is_padded_to_the_horizon(backend):
    with _at(backend, 0, 0):
        window = backend.to_solver_slots([True] * 10, fill=False)
    assert window[:10] == [True] * 10
    assert set(window[10:]) == {False}


def test_the_returned_schedule_is_a_full_eos_horizon(backend):
    with _at(backend, 20, 0):
        back = backend.from_solver_slots([5.0] * 8)
    assert len(back) == 192
    assert back[80] == 5.0            # 20:00
    assert back[79] == 0.0


def test_hourly_resolution_rotates_by_the_hour(backend):
    backend.time_frame_base = 3600
    mask = [False] * 48
    mask[9] = True
    with _at(backend, 7, 0):
        window = backend.to_solver_slots(mask, fill=False)
        assert window.index(True) == 2
        assert backend.from_solver_slots(window, fill=False).index(True) == 9
