"""
Fetching an external load profile instead of waiting to be handed one.

The push path is covered in `test_external_profile.py` and it has to keep behaving
exactly as it did - a pull source is an addition, not a replacement. What is new here is
everything that can go wrong when EOS Connect is the one doing the asking: a source that
is down, one that is up but talking nonsense, and the question of who wins when both
halves are configured.

The rule the manager has to hold: a failed fetch keeps the last good profile. Home
Assistant restarts, and blanking the forecast for the minutes that takes would swing the
battery plan on nothing more than a reboot.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.loads.injection import InjectionError
from src.loads.manager import ManagedLoadManager, ManagedLoadSources
from src.loads.presets import TYPE_EXTERNAL_CONTINGENT, TYPE_EXTERNAL_PROFILE

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 1, 15, 6, 30, tzinfo=BERLIN)
MIDNIGHT = datetime(2026, 1, 15, 0, 0, tzinfo=BERLIN)

SLOTS = 48
BASE_LOAD = [400.0] * SLOTS


class Clock:
    """A movable now, so a test can watch a fetched profile age out."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


class Source:
    """
    Stand-in for whatever `eos_connect` wires into `read_profile`.

    Records every call, so a test can assert the manager asked - and how often.
    """

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def __call__(self, entry):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def entries(count, value, start=MIDNIGHT, step_seconds=3600):
    """Normalized entries, as `load_profile_source.fetch_profile` returns them."""
    return {
        "entries": [
            {
                "start": start + timedelta(seconds=index * step_seconds),
                "end": start + timedelta(seconds=(index + 1) * step_seconds),
                "value": value,
            }
            for index in range(count)
        ],
        "resolution_seconds": step_seconds,
    }


def pulling(**overrides):
    """An external load profile that fetches its own array."""
    entry = {
        "id": "heating",
        "type": TYPE_EXTERNAL_PROFILE,
        "profile_source": "timeseries",
        "ha_sensor_name": "sensor.heat_pump_forecast",
        "data_path": "attributes.forecast",
        "value_unit": "W",
    }
    entry.update(overrides)
    return entry


def make_manager(entries_config, source=None, clock=None):
    return ManagedLoadManager(
        entries_config,
        time_frame_base=3600,
        time_zone=BERLIN,
        sources=ManagedLoadSources(
            price=lambda: [0.0003] * SLOTS,
            feed_in_price=lambda: [0.00008] * SLOTS,
            pv_forecast=lambda: [0.0] * SLOTS,
            base_load=lambda: list(BASE_LOAD),
            read_profile=source or Source(),
        ),
        clock=clock or Clock(),
    )


def forecast(manager):
    manager.run_cycle()
    return manager.apply(list(BASE_LOAD))


# --- the happy path ------------------------------------------------------------------

def test_a_fetched_profile_reaches_the_load_forecast():
    """1500 W held for an hour is 1500 Wh, on top of a 400 Wh base load."""
    source = Source(entries(24, 1500.0))
    manager = make_manager([pulling()], source)

    result = forecast(manager)
    assert source.calls == 1
    assert result[:24] == [1900.0] * 24
    assert result[24:] == [400.0] * 24


def test_it_is_refetched_every_cycle():
    """A forecast that moves with the weather is no use if it is read once."""
    source = Source(entries(24, 1000.0))
    manager = make_manager([pulling()], source)
    manager.run_cycle()

    source.result = entries(24, 2000.0)
    assert forecast(manager)[:24] == [2400.0] * 24
    assert source.calls == 2


def test_a_fetched_profile_is_marked_as_pulled():
    """
    Who was supposed to be updating this decides how a stale one is diagnosed.

    A push that stops means somebody's automation broke; a pull that stops means the
    entity went away. Reporting both as the same thing sends the user looking in the
    wrong place.
    """
    manager = make_manager([pulling()], Source(entries(24, 1000.0)))
    manager.run_cycle()

    contribution = manager.registry.snapshot()[0]
    assert contribution["id"] == "heating"
    assert contribution["source"] == "pull"


def test_a_quarter_hourly_source_aggregates_onto_hourly_slots():
    """
    Entries arrive already converted to Wh, so four quarters sum into the hour.

    The unit conversion happens on the interfaces side, where the source's own span is
    known; by the time the manager sees an entry its value is the energy in it.
    """
    source = Source(entries(96, 250.0, step_seconds=900))
    manager = make_manager([pulling()], source)

    assert forecast(manager)[:24] == [1400.0] * 24


def test_a_negative_correction_survives_the_pull_path():
    """The cooling-versus-heating delta has to work whichever way it arrives."""
    manager = make_manager([pulling()], Source(entries(24, -150.0)))

    assert forecast(manager)[:24] == [250.0] * 24


# --- when the source is not there ----------------------------------------------------

def test_a_failed_fetch_keeps_the_last_good_profile(caplog):
    """Home Assistant restarting must not blank the forecast."""
    source = Source(entries(24, 1500.0))
    manager = make_manager([pulling()], source)
    manager.run_cycle()

    source.error = ValueError("sensor.heat_pump_forecast could not be reached")
    with caplog.at_level("WARNING", logger="__main__"):
        result = forecast(manager)

    assert result[:24] == [1900.0] * 24
    assert any("could not fetch" in record.getMessage() for record in caplog.records)


def test_a_source_that_stays_down_is_reported_once(caplog):
    """A cycle runs every few minutes; one dead sensor must not bury the log."""
    source = Source(error=ValueError("nope"))
    manager = make_manager([pulling()], source)

    with caplog.at_level("WARNING", logger="__main__"):
        for _ in range(5):
            manager.run_cycle()

    warnings = [r for r in caplog.records if "could not fetch" in r.getMessage()]
    assert len(warnings) == 1
    assert source.calls == 5


def test_a_source_that_comes_back_warns_again_if_it_fails_again(caplog):
    """The once-only guard must reset, or the second outage is silent."""
    source = Source(error=ValueError("nope"))
    manager = make_manager([pulling()], source)
    manager.run_cycle()

    source.error = None
    source.result = entries(24, 1000.0)
    manager.run_cycle()

    source.error = ValueError("gone again")
    with caplog.at_level("WARNING", logger="__main__"):
        manager.run_cycle()

    assert any("could not fetch" in record.getMessage() for record in caplog.records)


def test_a_profile_that_is_never_fetched_still_expires():
    """
    Keeping the last good one is not the same as keeping it forever.

    This is the other half of the graceful-staleness rule: a source that never comes
    back has to stop steering the battery, and `ttl_minutes` is the bound.
    """
    clock = Clock()
    source = Source(entries(24, 1500.0))
    manager = make_manager([pulling(ttl_minutes=60)], source, clock=clock)
    manager.run_cycle()

    source.error = ValueError("still gone")
    clock.advance(minutes=61)

    assert forecast(manager) == BASE_LOAD
    assert manager.registry.snapshot() == []


def test_an_unreadable_payload_is_reported_and_the_old_one_kept(caplog):
    """A source that is up but talking nonsense is not a reason to blank the plan."""
    source = Source(entries(24, 1500.0))
    manager = make_manager([pulling()], source)
    manager.run_cycle()

    source.result = {"entries": [{"value": 100.0}], "resolution_seconds": 3600}
    with caplog.at_level("WARNING", logger="__main__"):
        result = forecast(manager)

    assert result[:24] == [1900.0] * 24
    assert any("could not fetch" in record.getMessage() for record in caplog.records)


def test_a_source_returning_nothing_is_not_an_error():
    """A template sensor mid-restart publishes an empty array; that is not a failure."""
    source = Source(entries(24, 1500.0))
    manager = make_manager([pulling()], source)
    manager.run_cycle()

    source.result = None
    assert forecast(manager)[:24] == [1900.0] * 24


def test_one_broken_source_does_not_stop_the_other_loads():
    """The cycle runs on the path that builds the optimizer request."""
    broken = Source(error=RuntimeError("something entirely unexpected"))
    manager = make_manager([pulling(), pulling(id="other")], broken)

    assert forecast(manager) == BASE_LOAD
    assert manager.stats.cycles == 1


# --- push and pull are not both in play ----------------------------------------------

def test_pushing_to_a_load_that_fetches_for_itself_is_refused():
    """
    Accepting it would work for one cycle and then be silently overwritten.

    Refusing says what actually happened; accepting looks like the push was lost.
    """
    manager = make_manager([pulling()], Source(entries(24, 1000.0)))

    with pytest.raises(InjectionError) as excinfo:
        manager.push("heating", {"value_wh": 500})
    assert "fetches its profile" in str(excinfo.value)


def test_an_entry_set_to_push_never_asks_the_source():
    """The pull fields are inert until the profile source says to use them."""
    source = Source(entries(24, 1000.0))
    manager = make_manager(
        [pulling(profile_source="push")], source
    )

    assert forecast(manager) == BASE_LOAD
    assert source.calls == 0


def test_a_contingent_is_never_fetched():
    """A budget and a deadline is not a timeseries; there is nothing to fetch."""
    source = Source(entries(24, 1000.0))
    manager = make_manager(
        [{"id": "budget", "type": TYPE_EXTERNAL_CONTINGENT,
          "profile_source": "timeseries"}],
        source,
    )
    manager.run_cycle()

    assert source.calls == 0


def test_a_fetched_load_can_still_replace_a_measured_meter():
    """
    Both halves of the base-load question apply to a pulled profile too.

    Naming the meter means the forecast is the appliance's whole consumption; leaving
    it empty means it is an addition. Nothing about fetching changes that.
    """
    manager = make_manager(
        [pulling(replaces_sensor="sensor.heat_pump_power")],
        Source(entries(24, 1000.0)),
    )

    assert manager.subtract_sensors() == ["sensor.heat_pump_power"]
    assert forecast(manager)[:24] == [1400.0] * 24
