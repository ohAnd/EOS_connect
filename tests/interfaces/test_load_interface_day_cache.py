"""
The per-day Home Assistant cache must not change the numbers.

Issue #302: the load profile used to ask the history endpoint once per slot — 24
requests per sensor per day — which times out against a large recorder and drags
the other interfaces down with it. The fix fetches each sensor's day once and cuts
the slots out of that series locally.

That is a caching change, so the bar is parity: for every sensor shape and every
slot size, the profile built from the cached day has to equal the profile built
from per-slot requests. These tests drive the same fixture through both paths and
compare, then assert the request count actually dropped.
"""

# The behaviour under test lives behind private names - the day cache, the clock, the
# per-slot aggregation. Reaching for them is the point, not an accident.
# pylint: disable=duplicate-code,protected-access

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.interfaces.load_interface import LoadInterface


HA_CONFIG = {
    "source": "homeassistant",
    "url": "http://dummy",
    "load_sensor": "sensor.house",
    "access_token": "token",
    "max_retries": 2,
    "retry_backoff": 0,
    "warning_threshold": 1,
}


class FakeHomeAssistant:
    """The endpoints LoadInterface uses, answered from a fixture.

    Nothing here reaches the network.

    `/api/history/period/<start>?end_time=<end>` mimics the real contract that the
    slicing has to reproduce: the first record is the state in effect at `start`,
    re-stamped to `start`, followed by the changes up to `end`.
    """

    def __init__(
        self, series_by_entity, attributes_by_entity=None, statistics_by_entity=None
    ):
        self.series = series_by_entity
        self.attributes = attributes_by_entity or {}
        self.statistics = statistics_by_entity or {}
        self.history_calls = []
        self.statistics_calls = []

    def get(self, url, params=None, headers=None, timeout=None, verify=None):
        """Stand in for requests.get."""
        # pylint: disable=too-many-arguments,unused-argument
        response = MagicMock()
        response.status_code = 200
        if "/api/states/" in url:
            entity_id = url.rsplit("/", 1)[1]
            response.json.return_value = {
                "attributes": self.attributes.get(entity_id, {})
            }
            return response

        start = datetime.fromisoformat(url.rsplit("/", 1)[1])
        end = datetime.fromisoformat(params["end_time"])
        entity_id = params["filter_entity_id"]
        self.history_calls.append((entity_id, start, end))
        response.json.return_value = self._history(entity_id, start, end)
        return response

    def request(self, method, url, **kwargs):
        """Stand in for requests.request — the recorder.get_statistics action."""
        # pylint: disable=unused-argument
        payload = kwargs.get("json") or {}
        entity_id = (payload.get("statistic_ids") or [None])[0]
        period = payload.get("period")
        self.statistics_calls.append((entity_id, period))

        rows = (self.statistics.get(entity_id) or {}).get(period, [])
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "service_response": {"statistics": {entity_id: rows} if rows else {}}
        }
        return response

    def _history(self, entity_id, start, end):
        series = self.series.get(entity_id, [])
        inside = [s for s in series if start <= _stamp(s) <= end]
        before = [s for s in series if _stamp(s) < start]

        records = []
        if before and (not inside or _stamp(inside[0]) > start):
            opening = dict(before[-1])
            opening["last_updated"] = start.isoformat()
            records.append(opening)
        records.extend(dict(sample) for sample in inside)
        return [records] if records else []


def _stamp(sample):
    return datetime.fromisoformat(sample["last_updated"])


def _series(day_start, values, step_minutes, attributes):
    """A sample every `step_minutes` across the day, bracketed on both sides.

    A recorder does not start at midnight and stop at midnight, so the fixture
    carries one sample before the day and one after it. That is what makes a
    per-slot request a fair reference: every slot, including the last, gets the
    opening state and a closing sample the way Home Assistant would supply them.
    """
    samples = []
    for index in range(-1, len(values) + 1):
        # The bracketing samples hold the nearest real value, so a monotonic
        # counter stays monotonic across them.
        value = values[min(max(index, 0), len(values) - 1)]
        samples.append(
            {
                "state": str(value),
                "last_updated": (
                    day_start + timedelta(minutes=index * step_minutes)
                ).isoformat(),
                "attributes": attributes,
            }
        )
    return samples


@contextmanager
def _connected(fake):
    """Route every Home Assistant call at `fake`, and never at the network."""
    with patch(
        "src.interfaces.load_interface.requests.get", side_effect=fake.get
    ), patch(
        "src.interfaces.load_interface.requests.request", side_effect=fake.request
    ), patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        yield


def _profile(fake, time_frame_base, day_start, prefetch):
    """Build one day's profile, with the day prefetch on or off.

    With it off, every slot issues its own request — the behaviour this change
    replaces, and the reference the cached path has to match.
    """
    interface = LoadInterface(dict(HA_CONFIG), time_frame_base, "UTC")
    with _connected(fake):
        if not prefetch:
            with patch.object(
                LoadInterface, "_LoadInterface__prefetch_homeassistant_day", lambda *_: None
            ):
                return interface.get_load_profile_for_day(
                    day_start, day_start + timedelta(days=1)
                )
        return interface.get_load_profile_for_day(
            day_start, day_start + timedelta(days=1)
        )


def _both_ways(series, attributes, time_frame_base=3600, day_start=None):
    """Return (per_slot_profile, cached_profile, cached_request_count)."""
    day_start = day_start or datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    fixture = {"sensor.house": series}
    attrs = {"sensor.house": attributes}

    per_slot_fake = FakeHomeAssistant(fixture, attrs)
    per_slot = _profile(per_slot_fake, time_frame_base, day_start, prefetch=False)

    cached_fake = FakeHomeAssistant(fixture, attrs)
    cached = _profile(cached_fake, time_frame_base, day_start, prefetch=True)

    return per_slot, cached, len(cached_fake.history_calls)


DAY_VALUES = [200 + (index % 7) * 130 for index in range(96)]


def test_power_sensor_profile_is_unchanged():
    """A plain W sensor: same 24 values, one request instead of 24."""
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    series = _series(day_start, DAY_VALUES, 15, {"unit_of_measurement": "W"})

    per_slot, cached, requests_made = _both_ways(series, {"unit_of_measurement": "W"})

    assert len(cached) == 24
    assert cached == per_slot
    assert requests_made == 1


def test_kilowatt_sensor_is_scaled_once():
    """kW is converted to W on the cached path too, and only once per slot.

    The conversion rewrites "state" in place, and adjacent slots share their
    boundary sample with the cached series — reusing the cached dicts would
    multiply that sample by 1000 again for every slot that touches it.
    """
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    kilowatts = [round(value / 1000, 3) for value in DAY_VALUES]
    series = _series(day_start, kilowatts, 15, {"unit_of_measurement": "kW"})

    per_slot, cached, _ = _both_ways(series, {"unit_of_measurement": "kW"})

    assert cached == per_slot
    assert max(cached) < 100000  # a second x1000 would blow straight past the outlier cap


def test_energy_counter_profile_is_unchanged():
    """A cumulative Wh meter must stay a rate, not become the meter reading."""
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    attributes = {"device_class": "energy", "unit_of_measurement": "Wh"}
    readings = []
    total = 100000
    for value in DAY_VALUES:
        readings.append(total)
        total += value * 0.25  # W over a quarter hour
    series = _series(day_start, readings, 15, attributes)

    per_slot, cached, _ = _both_ways(series, attributes)

    assert cached == per_slot
    # ~500 Wh/h on average, nowhere near the 100000 counter value.
    assert 0 < max(cached) < 2000


def test_gappy_sensor_profile_is_unchanged():
    """Unavailable and unparsable states are forward-filled the same either way."""
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    values = list(DAY_VALUES)
    for index in (5, 6, 40, 91, 95):
        values[index] = "unavailable"
    values[17] = ""
    series = _series(day_start, values, 15, {"unit_of_measurement": "W"})

    per_slot, cached, _ = _both_ways(series, {"unit_of_measurement": "W"})

    assert cached == per_slot


def test_last_slot_of_the_day_is_not_zero():
    """The 0 Wh final hour from issue #302, without a 23:00 special case.

    The recorder holds nothing after 22:30, so the last slot used to be handed a
    single sample: no duration to integrate, 0 Wh. Reconstructing the slot's own
    boundaries covers it — and covers 23:45 at a 900 s time frame too, which a
    check for "hour == 23" never could.
    """
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    series = [
        {
            "state": "480",
            "last_updated": (day_start + timedelta(minutes=step)).isoformat(),
            "attributes": {"unit_of_measurement": "W"},
        }
        for step in range(0, 22 * 60 + 31, 30)
    ]
    attributes = {"unit_of_measurement": "W"}

    for time_frame_base in (3600, 900):
        fake = FakeHomeAssistant({"sensor.house": series}, {"sensor.house": attributes})
        profile = _profile(fake, time_frame_base, day_start, prefetch=True)
        assert profile[-1] == pytest.approx(480 * time_frame_base / 3600, rel=1e-6)


def test_quarter_hour_slots_are_unchanged():
    """96 slots at time_frame_base=900, which evopt uses."""
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    series = _series(day_start, DAY_VALUES, 15, {"unit_of_measurement": "W"})

    per_slot, cached, requests_made = _both_ways(
        series, {"unit_of_measurement": "W"}, time_frame_base=900
    )

    assert len(cached) == 96
    assert cached == per_slot
    assert requests_made == 1


def test_dst_end_day_is_unchanged():
    """The Berlin day with a repeated 02:00 still yields 24 matching slots.

    Slot boundaries are wall clock — 24 of them, which is what the optimizer
    expects — but sample selection is by UTC instant, so the two real 02:00 hours
    cannot collapse into one another.
    """
    from zoneinfo import ZoneInfo  # pylint: disable=import-outside-toplevel

    berlin = ZoneInfo("Europe/Berlin")
    day_start = datetime(2026, 10, 25, 0, 0, tzinfo=berlin)
    series = _series(day_start, DAY_VALUES + DAY_VALUES[:4], 15, {"unit_of_measurement": "W"})

    per_slot, cached, _ = _both_ways(
        series, {"unit_of_measurement": "W"}, day_start=day_start
    )

    assert len(cached) == 24
    assert cached == per_slot


def test_naive_local_bounds_are_unchanged():
    """`time_zone: UTC` leaves the interface on naive local datetimes.

    The day bounds then arrive naive while Home Assistant answers in UTC, and both
    have to land in the same frame or every slice comes back empty.
    """
    day_start = datetime(2026, 3, 3, 0, 0)
    series = _series(
        day_start.astimezone(timezone.utc), DAY_VALUES, 15, {"unit_of_measurement": "W"}
    )

    per_slot, cached, _ = _both_ways(
        series, {"unit_of_measurement": "W"}, day_start=day_start
    )

    assert len(cached) == 24
    assert any(value > 0 for value in cached)
    assert cached == per_slot


def test_managed_load_sensors_are_prefetched_too():
    """Managed loads are subtracted per slot, so they need the same one-call path.

    Without them in the prefetch, anyone using managed loads keeps the request
    storm the rest of this change removes.
    """
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    attributes = {"unit_of_measurement": "W"}
    series = _series(day_start, DAY_VALUES, 15, attributes)
    small = _series(day_start, [20] * 96, 15, attributes)

    entities = ["sensor.house", "sensor.car", "sensor.heatpump", "sensor.wallbox"]
    fake = FakeHomeAssistant(
        {
            "sensor.house": series,
            "sensor.car": small,
            "sensor.heatpump": small,
            "sensor.wallbox": small,
        },
        {entity: attributes for entity in entities},
    )

    config = dict(HA_CONFIG)
    config["car_charge_load_sensor"] = "sensor.car"
    interface = LoadInterface(
        config,
        3600,
        "UTC",
        extra_subtract_sensors=["sensor.heatpump", "sensor.wallbox"],
    )
    with _connected(fake):
        interface.get_load_profile_for_day(day_start, day_start + timedelta(days=1))

    requested = [entity for entity, _, _ in fake.history_calls]
    assert sorted(requested) == sorted(entities), "one request per sensor, no more"


def test_managed_loads_are_subtracted_identically_from_the_cache():
    """The subtraction has to survive the cache, not just the request count.

    Managed loads are read per slot like every other sensor and taken back out of
    the household base load, so they go through the same slicing as the house
    sensor - a boundary sample lost on one of them shifts the profile just as much.
    """
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    attributes = {"unit_of_measurement": "W"}
    fixture = {
        "sensor.house": _series(
            day_start, [value + 1500 for value in DAY_VALUES], 15, attributes
        ),
        "sensor.car": _series(day_start, [300] * 96, 15, attributes),
        "sensor.heatpump": _series(
            day_start, [700 + (index % 5) * 60 for index in range(96)], 15, attributes
        ),
        "sensor.wallbox": _series(day_start, [120] * 96, 15, attributes),
    }
    entities = list(fixture)
    attrs = {entity: attributes for entity in entities}
    config = dict(HA_CONFIG)
    config["car_charge_load_sensor"] = "sensor.car"

    def run(prefetch):
        fake = FakeHomeAssistant(fixture, attrs)
        interface = LoadInterface(
            dict(config),
            3600,
            "UTC",
            extra_subtract_sensors=["sensor.heatpump", "sensor.wallbox"],
        )
        with _connected(fake):
            if prefetch:
                profile = interface.get_load_profile_for_day(
                    day_start, day_start + timedelta(days=1)
                )
            else:
                with patch.object(
                    LoadInterface,
                    "_LoadInterface__prefetch_homeassistant_day",
                    lambda *_: None,
                ):
                    profile = interface.get_load_profile_for_day(
                        day_start, day_start + timedelta(days=1)
                    )
        return profile, len(fake.history_calls)

    per_slot, per_slot_requests = run(prefetch=False)
    cached, cached_requests = run(prefetch=True)

    assert cached == per_slot
    assert per_slot_requests == 96, "4 sensors x 24 slots, the pattern being replaced"
    assert cached_requests == 4, "4 sensors, once each"
    # Sanity: the managed loads really were taken out, not silently skipped.
    assert max(cached) < max(value + 1500 for value in DAY_VALUES)


def test_rolling_window_is_not_served_from_cache():
    """`fetch_historical_energy_data` is public and polls windows that include now.

    Caching a range that reaches into the present would freeze it, so those
    requests have to keep going out.
    """
    now = datetime.now(timezone.utc)
    attributes = {"unit_of_measurement": "W"}
    series = _series(now - timedelta(hours=6), [300] * 96, 15, attributes)
    fake = FakeHomeAssistant({"sensor.house": series}, {"sensor.house": attributes})

    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")
    with _connected(fake):
        for _ in range(3):
            interface.fetch_historical_energy_data(
                "sensor.house", now - timedelta(hours=4), now
            )

    assert len(fake.history_calls) == 3


def _hourly_statistics(day_start, key, value, unit_hours=1):
    """24 hourly buckets carrying `value` under the given statistics key."""
    rows = []
    for hour in range(24):
        start = day_start + timedelta(hours=hour)
        rows.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=unit_hours)).isoformat(),
                key: value,
            }
        )
    return rows


def test_statistics_fallback_uses_change_not_the_meter_reading():
    """A purged counter falls back to statistics — as a rate, not a reading.

    Recorder rows for a total_increasing sensor carry both `state`, the meter at
    the end of the bucket, and `change`, the energy in it. Reading `state` as a
    power is how a 100 kWh counter arrives at the optimizer as 100 kW.
    """
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    attributes = {"device_class": "energy", "unit_of_measurement": "kWh"}
    rows = _hourly_statistics(day_start, "change", 0.4)  # 0.4 kWh/h -> 400 W
    for index, row in enumerate(rows):
        row["state"] = 1000 + index * 0.4  # the meter, which must not be used

    fake = FakeHomeAssistant(
        {"sensor.house": []},  # state history purged
        {"sensor.house": attributes},
        {"sensor.house": {"hour": rows}},
    )
    profile = _profile(fake, 3600, day_start, prefetch=True)

    assert len(profile) == 24
    assert all(value == pytest.approx(400.0) for value in profile)


def test_statistics_mean_is_scaled_from_kilowatts():
    """`mean` on a kW sensor is a power and needs the same unit scaling."""
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    attributes = {"unit_of_measurement": "kW"}
    fake = FakeHomeAssistant(
        {"sensor.house": []},
        {"sensor.house": attributes},
        {"sensor.house": {"hour": _hourly_statistics(day_start, "mean", 0.75)}},
    )

    profile = _profile(fake, 3600, day_start, prefetch=True)

    assert all(value == pytest.approx(750.0) for value in profile)


def test_statistics_action_is_asked_once_when_unsupported():
    """An older Home Assistant answers 4xx; it must be asked once, not per sensor.

    raise_for_status turns that into an exception, and retrying it five times per
    sensor per day only delays startup and fills the log.
    """
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    fake = FakeHomeAssistant({"sensor.house": []}, {"sensor.house": {}})

    def refuse(method, url, **kwargs):  # pylint: disable=unused-argument
        fake.statistics_calls.append(("sensor.house", (kwargs.get("json") or {}).get("period")))
        response = MagicMock()
        response.status_code = 404
        error = requests.exceptions.HTTPError("404 Not Found")
        error.response = response
        response.raise_for_status.side_effect = error
        return response

    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")
    with patch(
        "src.interfaces.load_interface.requests.get", side_effect=fake.get
    ), patch(
        "src.interfaces.load_interface.requests.request", side_effect=refuse
    ), patch(
        "src.interfaces.load_interface.time.sleep"
    ):
        interface.get_load_profile_for_day(day_start, day_start + timedelta(days=1))
        interface.get_load_profile_for_day(
            day_start - timedelta(days=7), day_start - timedelta(days=6)
        )

    assert len(fake.statistics_calls) == 1, "a 404 must not be retried or repeated"


def test_failed_refetch_does_not_leave_the_previous_day_cached():
    """A day with no data must not inherit the coverage of the day before it."""
    attributes = {"unit_of_measurement": "W"}
    good_day = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    empty_day = good_day - timedelta(days=7)
    fake = FakeHomeAssistant(
        {"sensor.house": _series(good_day, DAY_VALUES, 15, attributes)},
        {"sensor.house": attributes},
    )

    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")
    with _connected(fake):
        interface.get_load_profile_for_day(good_day, good_day + timedelta(days=1))
        empty = interface.get_load_profile_for_day(
            empty_day, empty_day + timedelta(days=1)
        )

    assert all(value == 0 for value in empty), "stale cache leaked the other day's data"


# ---------------------------------------------------------------------------------
# The day profile itself is held, not just the raw samples it is built from.
#
# Every value comes from four finished historical days, so the finished array changes
# only at midnight. Rebuilding it on each optimizer run re-read those same days - 16
# requests, 480 times a day at the default refresh_time - and re-ran the whole
# per-slot aggregation on top of it.
# ---------------------------------------------------------------------------------


def _full_day_fake(day_start):
    """A recorder holding one continuous series per sensor across the look-back."""
    attributes = {"unit_of_measurement": "W"}
    entities = ["sensor.house", "sensor.car", "sensor.hp"]
    series = {
        entity: _series(
            day_start - timedelta(days=15), DAY_VALUES * 16, 15, attributes
        )
        for entity in entities
    }
    return FakeHomeAssistant(series, {entity: attributes for entity in entities})


def _frozen(moment):
    """Pin the interface's idea of "now" for the duration of the block."""
    return patch.object(LoadInterface, "_LoadInterface__now", lambda _self: moment)


def test_the_profile_is_built_once_per_day():
    """Three optimizer runs, one build: 16 requests total, not 48."""
    today = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
    fake = _full_day_fake(today.replace(hour=0))
    config = dict(HA_CONFIG)
    config["car_charge_load_sensor"] = "sensor.car"
    config["additional_load_1_sensor"] = "sensor.hp"
    interface = LoadInterface(config, 3600, "UTC")

    with _connected(fake), _frozen(today):
        first = interface.get_load_profile(48)
        after_first = len(fake.history_calls)
        interface.get_load_profile(48)
        third = interface.get_load_profile(48)

    assert after_first == 12, "3 sensors x 4 days"
    assert len(fake.history_calls) == after_first, "later runs must not touch HA again"
    assert third == first


def test_the_aggregation_is_not_re_run_either():
    """The point is not only fewer requests - the per-slot maths is cached too."""
    today = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
    fake = _full_day_fake(today.replace(hour=0))
    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")

    with _connected(fake), _frozen(today), patch.object(
        LoadInterface,
        "_LoadInterface__process_energy_data",
        side_effect=LoadInterface._LoadInterface__process_energy_data,
        autospec=True,
    ) as processed:
        interface.get_load_profile(48)
        after_first = processed.call_count
        interface.get_load_profile(48)
        interface.get_load_profile(48)

    assert after_first > 0
    assert processed.call_count == after_first


def test_the_profile_is_rebuilt_after_midnight():
    """It describes a calendar day, so the day rolling over has to invalidate it."""
    today = datetime(2026, 3, 3, 23, 30, tzinfo=timezone.utc)
    tomorrow = datetime(2026, 3, 4, 0, 30, tzinfo=timezone.utc)
    fake = _full_day_fake(today.replace(hour=0))
    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")

    with _connected(fake):
        with _frozen(today):
            interface.get_load_profile(48)
            after_today = len(fake.history_calls)
        with _frozen(tomorrow):
            interface.get_load_profile(48)

    assert len(fake.history_calls) == after_today * 2, "a new day means a new build"


def test_the_caller_cannot_mutate_the_held_profile():
    """get_ems_data scales the in-progress slot in place.

    The comment above that code records what happened the last time a cached series
    was handed out directly: the slot shrank again on every run.
    """
    today = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
    fake = _full_day_fake(today.replace(hour=0))
    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")

    with _connected(fake), _frozen(today):
        first = interface.get_load_profile(48)
        original = first[9]
        first[9] *= 0.25
        second = interface.get_load_profile(48)

    assert second[9] == original


def test_a_degraded_profile_is_retried_rather_than_kept_all_day():
    """A source that was briefly unreachable must not cost the whole day.

    Nothing readable means the built-in curve, and holding that until midnight would
    run the optimizer on fiction long after Home Assistant came back.
    """
    today = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
    empty = FakeHomeAssistant({"sensor.house": []}, {"sensor.house": {}})
    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")

    with _connected(empty), _frozen(today):
        degraded = interface.get_load_profile(48)
    assert degraded == interface._get_default_profile()

    # Same calendar day, but past the retry window: it tries again and now succeeds.
    later = today + timedelta(seconds=400)
    good = _full_day_fake(today.replace(hour=0))
    with _connected(good), _frozen(later):
        recovered = interface.get_load_profile(48)

    assert recovered != interface._get_default_profile()
    assert good.history_calls, "the retry has to actually go back to Home Assistant"


def test_a_good_profile_is_not_retried_within_the_day():
    """The counterpart: a real profile is not rebuilt just because time passed."""
    today = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
    fake = _full_day_fake(today.replace(hour=0))
    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")

    with _connected(fake):
        with _frozen(today):
            interface.get_load_profile(48)
            after_build = len(fake.history_calls)
        for minutes in (10, 60, 600):
            moment = today + timedelta(minutes=minutes)
            with _frozen(moment):
                interface.get_load_profile(48)

    assert len(fake.history_calls) == after_build


def test_raw_samples_are_released_after_the_build():
    """The four days of samples are the largest thing here; nothing reads them after."""
    today = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
    fake = _full_day_fake(today.replace(hour=0))
    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")

    with _connected(fake), _frozen(today):
        interface.get_load_profile(48)

    assert not interface._LoadInterface__homeassistant_history_cache


def test_two_callers_at_once_build_the_profile_once():
    """The optimizer loop and the managed-load base load both reach this.

    Without the lock, a cold start with both arriving together would send two full
    sets of history requests at the recorder this change is trying to relieve.
    """
    import threading  # pylint: disable=import-outside-toplevel

    today = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
    fake = _full_day_fake(today.replace(hour=0))
    interface = LoadInterface(dict(HA_CONFIG), 3600, "UTC")
    results = []

    def call():
        results.append(interface.get_load_profile(48))

    with _connected(fake), _frozen(today):
        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(results) == 4
    assert all(result == results[0] for result in results)
    assert len(fake.history_calls) == 4, "one sensor x four days, built once"
