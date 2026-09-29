"""
The external load profile, end to end - issue #55's "dynamic load injection".

The parser is covered in `test_injection.py` and the HTTP verbs in `test_api.py`. What
neither of them answers is the question the issue thread is actually about: when a Home
Assistant automation pushes a heat pump's predicted consumption, *what does the number
the optimizer sees become*?

That answer depends on whether the instance names a meter its forecast replaces, and
getting it wrong is invisible from the sending end. So these tests pin both readings of
a push:

- **additive** - the push is an extra load nobody has measured yet, added slot by slot
  on top of the household forecast. This is the default, and it is what the issue asks
  for: "put to this endpoint either 0, if the heat pump is in heating mode, or 1.5 kWh".
- **replacing** - the instance names the meter its forecast replaces, so that meter's
  measured history leaves the base load. The push then has to be the appliance's *whole*
  consumption, not the extra bit.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.loads.manager import ManagedLoadManager, ManagedLoadSources
from src.loads.mqtt_topics import build_topics
from src.loads.presets import TYPE_EXTERNAL_PROFILE

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 1, 15, 6, 30, tzinfo=BERLIN)

# 48 hourly slots from local midnight - the horizon the manager plans over.
SLOTS = 48
BASE_LOAD = [400.0] * SLOTS


class Clock:
    """A movable now, so a test can watch a push age out or survive midnight."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


def make_manager(entries, clock=None, time_frame_base=3600, sensors=None):
    """A manager over a fake installation. The poll thread is never started."""
    readings = dict(sensors or {})
    return ManagedLoadManager(
        entries,
        time_frame_base=time_frame_base,
        time_zone=BERLIN,
        sources=ManagedLoadSources(
            read_sensor=readings.get,
            price=lambda: [0.0003] * SLOTS,
            feed_in_price=lambda: [0.00008] * SLOTS,
            pv_forecast=lambda: [0.0] * SLOTS,
            base_load=lambda: list(BASE_LOAD),
        ),
        clock=clock or Clock(),
    )


def heating(**overrides):
    """An external load profile entry, with nothing configured but its id and type."""
    entry = {"id": "heating", "type": TYPE_EXTERNAL_PROFILE}
    entry.update(overrides)
    return entry


def forecast(manager, base=None, slots=SLOTS):
    """What the optimizer would be handed as ``gesamtlast`` after this push."""
    manager.run_cycle()
    series = list(base if base is not None else [400.0] * slots)
    return manager.apply(series, time_frame_base=manager.time_frame_base)


# --- the shapes the issue asked for, all the way to the optimizer ---------------------

def test_a_daily_average_is_added_to_every_slot():
    """
    ohAnd's proposal, end to end: "1.5 kWh as an avg value for the whole day".

    Everything else in this file is a variation on this line, so if it breaks, read it
    before reading the rest.
    """
    manager = make_manager([heating()])
    manager.push("heating", {"value_wh": 1500})

    assert forecast(manager) == [1900.0] * SLOTS


def test_a_24_hour_array_lands_on_the_hours_it_names():
    """The "0,0,0,0,1.2,1.4,1.5,1.6, ..." form, indexed from local midnight."""
    values = [0.0] * 4 + [1200.0, 1400.0, 1500.0, 1600.0] + [0.0] * 16
    manager = make_manager([heating()])
    manager.push("heating", {"values": values})

    result = forecast(manager)
    assert result[:4] == [400.0] * 4
    assert result[4:8] == [1600.0, 1800.0, 1900.0, 2000.0]
    # Day two was not pushed, so it is not guessed at either.
    assert result[24:] == [400.0] * 24


def test_a_96_slot_array_reaches_an_hourly_optimizer_as_hourly_energy():
    """j4rvisstant's quarter-hourly form, against an optimizer running on 60-minute slots."""
    manager = make_manager([heating()])
    manager.push("heating", {"values": [250.0] * 96})

    result = forecast(manager)
    assert result[:24] == [1400.0] * 24
    assert result[24:] == [400.0] * 24


def test_the_same_push_lands_unchanged_on_a_quarter_hourly_optimizer():
    """A sender must not have to know what `eos.time_frame` is set to."""
    manager = make_manager([heating()], time_frame_base=900)
    manager.push("heating", {"value_wh": 1500})

    result = forecast(manager, base=[100.0] * 192, slots=192)
    assert result[:4] == [475.0] * 4  # 1500 Wh/h -> 375 Wh per quarter hour
    assert sum(result) == pytest.approx(192 * 100 + 48 * 1500)


def test_a_bare_number_published_over_mqtt_is_the_hourly_average():
    """What an automation can publish with no templating at all."""
    manager = make_manager([heating()])
    topics = build_topics(manager)
    handler = topics["managed_load/heating"]["on_command"]

    handler("900")

    assert forecast(manager) == [1300.0] * SLOTS


# --- additive or replacing: the question the issue turns on ---------------------------

def test_without_a_replaces_sensor_the_push_is_purely_additional():
    """
    The default reading: "this much *extra*, slot by slot".

    Nothing is taken out of the household base load, because nothing was named that
    could be taken out - so the pushed series is exactly the planned additional load.
    """
    manager = make_manager([heating()])
    manager.push("heating", {"values": [1000.0] * 24})

    assert manager.subtract_sensors() == []
    result = forecast(manager)
    assert [after - before for after, before in zip(result, BASE_LOAD)][:24] == [1000.0] * 24


def test_naming_a_replaces_sensor_switches_the_push_to_replacing_the_measurement():
    """
    The other reading: the appliance leaves the base load and the push stands in for it.

    `subtract_sensors()` is what `eos_connect.py` hands to the load interface, so this
    list is the whole switch - a push to an instance that appears here has to be the
    appliance's entire consumption, not the extra bit.
    """
    manager = make_manager([heating(replaces_sensor="sensor.heat_pump_power")])
    manager.push("heating", {"values": [1000.0] * 24})

    assert manager.subtract_sensors() == ["sensor.heat_pump_power"]


def test_an_entry_still_carrying_the_old_field_name_keeps_subtracting():
    """
    The safety net under the rename, for a config the store migration never saw.

    A backup restored after the upgrade writes `power_sensor` back, and the migration
    marker is already set, so it will not run again. Ignoring the old name there would
    quietly stop subtracting and double-count the appliance - a wrong forecast with
    nothing in the log.
    """
    manager = make_manager([heating(power_sensor="sensor.heat_pump_power")])

    assert manager.subtract_sensors() == ["sensor.heat_pump_power"]


def test_the_new_field_wins_when_both_are_present():
    """One meter leaves the base load, not two."""
    manager = make_manager([heating(
        power_sensor="sensor.old", replaces_sensor="sensor.new",
    )])

    assert manager.subtract_sensors() == ["sensor.new"]


def test_the_subtraction_can_be_turned_off_while_the_sensor_stays_named():
    """
    `subtract_from_base_load` outranks the sensor, for a meter already counted elsewhere.

    Someone whose heat pump sits behind a sub-meter that another managed load is already
    subtracting needs to name the sensor without subtracting it twice.
    """
    manager = make_manager([
        heating(replaces_sensor="sensor.heat_pump_power", subtract_from_base_load=False)
    ])
    manager.push("heating", {"values": [1000.0] * 24})

    assert manager.subtract_sensors() == []
    assert forecast(manager)[:24] == [1400.0] * 24


def test_a_negative_push_corrects_the_forecast_downwards():
    """Heating mode is in the history, cooling is not: the difference can be negative."""
    manager = make_manager([heating()])
    manager.push("heating", {"values": [-150.0] * 24})

    result = forecast(manager)
    assert result[:24] == [250.0] * 24
    assert result[24:] == [400.0] * 24


def test_a_correction_cannot_drive_a_slot_below_zero():
    """A household that hands energy back is a PV surplus, not a negative load."""
    manager = make_manager([heating()])
    manager.push("heating", {"values": [-5000.0] * 24})

    assert forecast(manager)[:24] == [0.0] * 24


# --- a forecast, not a schedulable load ----------------------------------------------

def test_a_profile_is_never_offered_to_the_optimizer_as_something_to_place():
    """
    "There is no release signal, because the timing was never ours to choose."

    A load that is both inside `gesamtlast` and offered to the solver would be planned
    around twice, which is the double-counting trap in a different costume.
    """
    manager = make_manager([heating()])
    manager.push("heating", {"value_wh": 1000})
    manager.external_scheduler = True
    manager.run_cycle()

    item = manager.instance("heating")
    assert manager.schedulable() == []
    assert item.last_release is None
    assert item.gate is None
    # Deferring to the optimizer must not drop it out of the household forecast either.
    assert manager.apply(list(BASE_LOAD))[0] == pytest.approx(1400.0)


def test_it_contributes_nothing_until_something_is_pushed():
    """A configured but unfed instance is not an excuse to inflate the forecast."""
    manager = make_manager([heating()])

    assert forecast(manager) == BASE_LOAD
    assert manager.instance("heating").status()["model"]["pushed"] is False


# --- the lifecycle of a pushed forecast ----------------------------------------------

def test_the_newest_push_replaces_the_previous_one():
    """A re-pushed forecast supersedes; it is not added to what was there."""
    manager = make_manager([heating()])
    manager.push("heating", {"value_wh": 1500})
    manager.push("heating", {"value_wh": 200})

    assert forecast(manager) == [600.0] * SLOTS


def test_clearing_a_push_takes_it_out_of_the_forecast_at_once():
    manager = make_manager([heating()])
    manager.push("heating", {"value_wh": 1500})
    assert forecast(manager)[0] == pytest.approx(1900.0)

    manager.clear_push("heating")

    assert forecast(manager) == BASE_LOAD


def test_an_expired_push_stops_counting_rather_than_being_held_forever():
    """
    A forecast nobody is maintaining any more must not keep steering the battery.

    This is the failure mode that matters on a live install: the automation that pushes
    stops running, and every plan from then on is built on a stale heating curve.
    """
    clock = Clock()
    manager = make_manager([heating()], clock=clock)
    manager.push("heating", {"value_wh": 1500, "ttl_minutes": 60})
    assert forecast(manager)[0] == pytest.approx(1900.0)

    clock.advance(minutes=61)

    assert forecast(manager) == BASE_LOAD
    assert manager.registry.snapshot() == []


def test_a_48_hour_push_carries_into_its_second_day_after_midnight():
    """
    "for this day (and next day)" - read from tomorrow's midnight, not repeated.

    Slot 0 means local midnight of whatever day it is now, so a series stored yesterday
    has to be re-read from an offset. Repeating it instead would quietly serve yesterday
    morning's heating curve as today's.
    """
    clock = Clock()
    manager = make_manager([heating()], clock=clock)
    manager.push("heating", {"values": [100.0] * 24 + [700.0] * 24, "ttl_minutes": 2880})
    assert forecast(manager)[0] == pytest.approx(500.0)

    clock.advance(days=1)

    result = forecast(manager)
    assert result[:24] == [1100.0] * 24   # what was pushed for day two
    assert result[24:] == [400.0] * 24    # and nothing invented past it


def test_a_push_is_ignored_after_the_slot_length_changes(caplog):
    """
    Reading a 15-minute series as hourly would inflate the forecast fourfold.

    Refusing it costs one stale cycle; reinterpreting it costs a wrong plan that looks
    entirely plausible.
    """
    clock = Clock()
    manager = make_manager([heating()], clock=clock)
    manager.push("heating", {"value_wh": 1500})
    manager.run_cycle()

    manager.time_frame_base = 900
    with caplog.at_level("WARNING", logger="__main__"):
        manager.run_cycle()
        result = manager.apply([100.0] * 192, time_frame_base=900)

    assert result == [100.0] * 192
    assert any(
        "Push it again" in record.getMessage() or "pushed again" in record.getMessage()
        for record in caplog.records
    )


def test_a_push_does_not_survive_a_restart_on_its_own():
    """
    Pushed data lives in memory only.

    Documented here rather than fixed: the MQTT topic is retained, so a sender that
    publishes there is restored by the broker on reconnect. A caller that only ever
    POSTs has to push again after a restart - and if it does not, the forecast is simply
    the household base load, never a stale one.
    """
    manager = make_manager([heating()])
    manager.push("heating", {"value_wh": 1500})
    assert forecast(manager)[0] == pytest.approx(1900.0)

    restarted = make_manager([heating()])

    assert forecast(restarted) == BASE_LOAD
    assert restarted.instance("heating").status()["model"]["pushed"] is False
