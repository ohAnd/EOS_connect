"""
Managed loads leave the household base load before their forecast is added back.

Their measured consumption is already inside the weekday average the load profile is
built from. Adding a prediction on top without removing the history counts the same
appliance twice - which is exactly what makes putting a heat pump in
``additional_load_1`` worse than not configuring it at all (issue #55).
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.interfaces.load_interface import LoadInterface

CONFIG = {
    "source": "homeassistant",
    "url": "http://localhost:8123",
    "load_sensor": "sensor.house",
    "access_token": "token",
}


def _samples(watts, start=None):
    """An hour of a constant power reading, in the shape the history endpoint returns."""
    base = start or datetime.fromisoformat("2026-06-12T00:00:00")
    return [
        {"state": str(watts), "last_updated": (base + timedelta(minutes=m)).isoformat()}
        for m in range(0, 61, 15)
    ]


def _interface(**kwargs):
    with patch("src.interfaces.load_interface.logger"):
        return LoadInterface(CONFIG, time_frame_base=3600, **kwargs)


def _profile_for_one_hour(interface, house_w, sensor_readings):
    """Run one slot through the real path with the history calls stubbed out."""
    start = datetime.fromisoformat("2026-06-12T00:00:00")
    end = start + timedelta(hours=1)

    def fake_history(entity_id, _start, _end):
        if entity_id == "sensor.house":
            return _samples(house_w)
        return _samples(sensor_readings.get(entity_id, 0))

    with patch.object(
        LoadInterface,
        "_LoadInterface__fetch_historical_energy_data_from_homeassistant",
        side_effect=fake_history,
    ), patch.object(
        LoadInterface,
        "_LoadInterface__get_additional_load_list_from_to",
        side_effect=lambda item, s, e: fake_history(item, s, e),
    ), patch("src.interfaces.load_interface.logger"):
        return interface.get_load_profile_for_day(start, end)


def test_nothing_is_subtracted_when_no_managed_load_is_configured():
    profile = _profile_for_one_hour(_interface(), house_w=1000, sensor_readings={})
    assert profile[0] == pytest.approx(1000.0, rel=0.01)


def test_a_managed_load_is_removed_from_the_household_profile():
    interface = _interface(extra_subtract_sensors=["sensor.pool_power"])
    profile = _profile_for_one_hour(
        interface, house_w=1800, sensor_readings={"sensor.pool_power": 800}
    )
    assert profile[0] == pytest.approx(1000.0, rel=0.01)


def test_several_managed_loads_are_all_removed():
    interface = _interface(
        extra_subtract_sensors=["sensor.pool_power", "sensor.sauna_power"]
    )
    profile = _profile_for_one_hour(
        interface,
        house_w=4200,
        sensor_readings={"sensor.pool_power": 1800, "sensor.sauna_power": 2400},
    )
    assert profile[0] == pytest.approx(0.0, abs=1.0)


def test_managed_loads_stack_with_the_existing_additional_load():
    config = dict(CONFIG, additional_load_1_sensor="sensor.dishwasher")
    with patch("src.interfaces.load_interface.logger"):
        interface = LoadInterface(
            config, time_frame_base=3600, extra_subtract_sensors=["sensor.pool_power"]
        )
    profile = _profile_for_one_hour(
        interface,
        house_w=2000,
        sensor_readings={"sensor.pool_power": 800, "sensor.dishwasher": 500},
    )
    assert profile[0] == pytest.approx(700.0, rel=0.02)


def test_a_household_reading_smaller_than_its_controllables_is_left_alone():
    """
    Overlapping sensors are a configuration error, not a licence to forecast a negative
    load. The existing behaviour keeps the household figure and warns; managed loads
    join that path rather than getting their own.
    """
    interface = _interface(extra_subtract_sensors=["sensor.pool_power"])
    profile = _profile_for_one_hour(
        interface, house_w=500, sensor_readings={"sensor.pool_power": 1800}
    )
    assert profile[0] == pytest.approx(500.0, rel=0.01)


@pytest.mark.parametrize("sensors", [None, [], ["", "   "]])
def test_blank_sensor_entries_are_ignored(sensors):
    interface = _interface(extra_subtract_sensors=sensors)
    assert interface.extra_subtract_sensors == []
