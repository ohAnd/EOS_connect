"""
The clock the release gate is settled against.

A live install showed a pool heat pump released for half an hour at 19:09 while every
slot that day read "costs more than it is worth" and the next planned run was the
following afternoon. Nothing in the plan asked for it.

The cause was the context. Sensor readings and demand are refreshed on the manager's own
cycle - five minutes on that install - and that is by design. But the optimizer answers
every two minutes, and `adopt_schedules` reused the whole context, clock included. So the
gate kept asking "is *this* slot planned" about a slot that had already ended, and with
quarter-hour slots against a five-minute cycle it was wrong roughly a third of the time.
A minimum runtime then held the compressor on for the next half hour.

What these pin is narrow: the demand may be stale, the clock may not.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.loads.manager import ManagedLoadManager, ManagedLoadSources

BERLIN = ZoneInfo("Europe/Berlin")
SLOTS = 192           # 48 h of quarter-hours, as the install runs
BASE = datetime(2026, 9, 13, 19, 14, 0, tzinfo=BERLIN)
ANCHOR = BASE.replace(hour=0, minute=0)

# Slot 76 is 19:00-19:15, slot 77 is 19:15-19:30.
SLOT_76, SLOT_77 = 76, 77


class Clock:
    def __init__(self, now=BASE):
        self.now = now

    def __call__(self):
        return self.now

    def at(self, hour, minute):
        self.now = BASE.replace(hour=hour, minute=minute)
        return self.now


def make_manager(clock, min_runtime_minutes=30):
    """The install's pool, with its real numbers."""
    pool = {
        "id": "pool", "type": "pool_heatpump", "enabled": True,
        "temp_sensor": "sensor.pool", "power_sensor": "sensor.pool_power",
        "target_temp": 29.0, "volume_m3": 18.0, "surface_m2": 13.75,
        "rated_power_w": 1600.0, "min_runtime_minutes": min_runtime_minutes,
        "window_start": 3, "window_end": 23, "min_ambient_temp_c": 12.0,
        "max_price_ct_kwh": 25.0, "deadband_k": 0.2,
        "season_start": "04-15", "season_end": "09-30",
    }
    manager = ManagedLoadManager(
        [pool], time_frame_base=900, time_zone=BERLIN, cycle_seconds=300,
        horizon_hours=48,
        sources=ManagedLoadSources(
            read_sensor=lambda name: {
                "sensor.pool": 25.1, "sensor.pool_power": 0.0
            }.get(name),
            price=lambda: [0.00031] * SLOTS,
            feed_in_price=lambda: [0.00008] * SLOTS,
            pv_forecast=lambda: [0.0] * SLOTS,
            base_load=lambda: [400.0] * SLOTS,
            temperature_forecast=lambda: [17.2] * 48,
        ),
        clock=clock,
    )
    manager.external_scheduler = True
    return manager


def schedule_for(*slots):
    series = [0.0] * SLOTS
    for slot in slots:
        series[slot] = 400.0
    return series


def released(manager):
    return manager.instance("pool").last_release["released"]


def reason(manager):
    return manager.instance("pool").last_release["reason"]


# --- the defect ------------------------------------------------------------------------

def test_a_slot_that_has_ended_does_not_release_the_load():
    """
    The bug, at its narrowest: 19:00-19:15 is planned, and at 19:16 it is over.

    Before the fix this released - the context still said slot 76 - and the minimum
    runtime then carried the compressor to 19:46 on a plan that had asked for nothing
    after 19:15.
    """
    clock = Clock()
    manager = make_manager(clock)
    manager.run_cycle()                       # context captured at 19:14, slot 76
    manager.adopt_schedules({"pool": [0.0] * SLOTS})
    assert released(manager) is False

    clock.at(19, 16)                          # slot 77 now, and 77 is not planned
    manager.adopt_schedules({"pool": schedule_for(SLOT_76)})

    assert released(manager) is False
    assert reason(manager) == "not in a planned slot"


def test_the_slot_that_is_current_now_does_release_it():
    """The other half - the fix must not simply stop releasing."""
    clock = Clock()
    manager = make_manager(clock)
    manager.run_cycle()

    clock.at(19, 16)
    manager.adopt_schedules({"pool": schedule_for(SLOT_77)})

    assert released(manager) is True
    assert reason(manager) == "planned cheap slot"


def test_the_decision_follows_the_clock_across_a_slot_boundary():
    """
    One cycle, several adoptions, the boundary crossed in between.

    This is the live cadence: the manager polls at 19:14 and the optimizer answers at
    19:14, 19:16, 19:18 and 19:20 against the same context.
    """
    clock = Clock()
    manager = make_manager(clock, min_runtime_minutes=0)
    manager.run_cycle()

    plan = {"pool": schedule_for(SLOT_76)}
    seen = []
    for minute in (14, 16, 18, 20):
        clock.at(19, minute)
        manager.adopt_schedules(plan)
        seen.append((minute, released(manager)))

    assert seen == [(14, True), (16, False), (18, False), (20, False)]


def test_the_minimum_runtime_still_holds_a_real_release():
    """The fix must not cost the compressor its protection."""
    clock = Clock()
    manager = make_manager(clock, min_runtime_minutes=30)
    manager.run_cycle()

    manager.adopt_schedules({"pool": schedule_for(SLOT_76)})
    assert released(manager) is True

    clock.at(19, 20)                          # slot 76 is over, the hold is not
    manager.adopt_schedules({"pool": [0.0] * SLOTS})

    assert released(manager) is True
    assert reason(manager) == "holding minimum runtime"


def test_the_minimum_runtime_expires_on_the_wall_clock():
    """
    It used to expire on the cycle clock, so the hold ran until the next poll.

    Thirty minutes became thirty-five on a five-minute cycle, every time.
    """
    clock = Clock()
    manager = make_manager(clock, min_runtime_minutes=30)
    manager.run_cycle()
    manager.adopt_schedules({"pool": schedule_for(SLOT_76)})

    clock.at(19, 44)                          # 30 minutes after the 19:14 release
    manager.adopt_schedules({"pool": [0.0] * SLOTS})

    assert released(manager) is False


def test_the_schedule_freshness_check_uses_the_real_time_too():
    """
    `last_schedule_at` decides when this module stops trusting the optimizer.

    Stamped from the stale context it ran behind, so a solver that had in fact answered
    seconds ago could be judged overdue.
    """
    clock = Clock()
    manager = make_manager(clock)
    manager.run_cycle()

    clock.at(19, 22)
    manager.adopt_schedules({"pool": schedule_for(SLOT_77)})

    assert manager.instance("pool").last_schedule_at == clock.now


def test_the_demand_is_still_allowed_to_be_from_the_last_cycle():
    """
    Only the clock is refreshed. Sensors are read on the cycle and that stays true -
    re-reading them here would put a Home Assistant query on the optimizer's path.
    """
    reads = []
    clock = Clock()
    pool = {
        "id": "pool", "type": "pool_heatpump", "enabled": True,
        "temp_sensor": "sensor.pool", "power_sensor": "sensor.pool_power",
        "target_temp": 29.0, "rated_power_w": 1600.0,
        "window_start": None, "window_end": None, "min_ambient_temp_c": None,
        "season_start": None, "season_end": None,
    }
    manager = ManagedLoadManager(
        [pool], time_frame_base=900, time_zone=BERLIN, cycle_seconds=300,
        horizon_hours=48,
        sources=ManagedLoadSources(
            read_sensor=lambda name: reads.append(name) or 25.1,
            base_load=lambda: [400.0] * SLOTS,
        ),
        clock=clock,
    )
    manager.external_scheduler = True
    manager.run_cycle()
    after_cycle = len(reads)

    clock.at(19, 20)
    manager.adopt_schedules({"pool": schedule_for(SLOT_77)})

    assert len(reads) == after_cycle


# --- the journal -----------------------------------------------------------------------

class _Journal:
    """Stand-in for the store, recording what the manager hands it."""

    def __init__(self):
        self.rows = []

    def record_decision(self, load_id, moment, record):
        self.rows.append((load_id, moment, record))
        return True

    def record_sample(self, *_args, **_kwargs):
        return True

    def load_samples(self, *_args, **_kwargs):
        return []

    def load_model_state(self, *_args, **_kwargs):
        return None

    def save_model_state(self, *_args, **_kwargs):
        return True


def test_an_adopted_schedule_is_journalled_with_the_plan_behind_it():
    clock = Clock()
    manager = make_manager(clock)
    journal = _Journal()
    manager.store = journal
    manager.run_cycle()
    journal.rows.clear()

    clock.at(19, 16)
    manager.adopt_schedules({"pool": schedule_for(SLOT_77)})

    assert len(journal.rows) == 1
    load_id, moment, record = journal.rows[0]
    assert load_id == "pool"
    assert moment == clock.now                      # the real time, not the cycle's
    assert record["origin"] == "optimizer"
    assert record["released"] is True
    assert record["current_slot"] == SLOT_77
    assert record["planned_now"] is True
    assert record["planned_slots"] == [SLOT_77]
    assert record["plan_hash"]


def test_a_moving_plan_gets_a_different_hash():
    """What makes churn countable rather than a week of arrays to read."""
    clock = Clock()
    manager = make_manager(clock, min_runtime_minutes=0)
    journal = _Journal()
    manager.store = journal
    manager.run_cycle()
    journal.rows.clear()

    for minute, slots in ((16, (SLOT_77,)), (18, (SLOT_77, 80)), (20, (SLOT_77,))):
        clock.at(19, minute)
        manager.adopt_schedules({"pool": schedule_for(*slots)})

    hashes = [row[2]["plan_hash"] for row in journal.rows]
    assert len(hashes) == 3
    assert hashes[0] == hashes[2] != hashes[1]


def test_a_decision_the_module_made_itself_is_marked_as_such():
    """
    An optimizer changing its mind and this module falling back look identical to the
    appliance. They must not look identical in the journal.
    """
    clock = Clock()
    manager = make_manager(clock)
    journal = _Journal()
    manager.store = journal
    manager.external_scheduler = False

    manager.run_cycle()

    assert [row[2]["origin"] for row in journal.rows] == ["self"]


def test_a_journal_that_cannot_be_written_does_not_stop_the_house(caplog):
    """Diagnostics are never worth losing a schedule over."""
    class Broken(_Journal):
        def record_decision(self, *_args, **_kwargs):
            raise RuntimeError("disk full")

    clock = Clock()
    manager = make_manager(clock)
    manager.store = Broken()
    manager.run_cycle()

    clock.at(19, 16)
    with caplog.at_level("ERROR", logger="__main__"):
        adopted = manager.adopt_schedules({"pool": schedule_for(SLOT_77)})

    assert adopted == 1
    assert released(manager) is True


def test_nothing_is_journalled_without_a_store():
    """The manager runs with no database at all - that path must stay clean."""
    clock = Clock()
    manager = make_manager(clock)
    manager.run_cycle()

    clock.at(19, 16)
    assert manager.adopt_schedules({"pool": schedule_for(SLOT_77)}) == 1
