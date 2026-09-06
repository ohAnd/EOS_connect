"""Shared fixtures: a fake installation the managed-load framework can run against."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.loads.manager import ManagedLoadManager, ManagedLoadSources

BERLIN = ZoneInfo("Europe/Berlin")

# A fixed "now" for every test. Which slot the current moment falls in decides what the
# planner may still use, so a suite that reads the wall clock passes in the morning and
# fails after lunch.
NOW = datetime(2026, 6, 1, 6, 30, tzinfo=BERLIN)


class FakeInstallation:
    """
    Stand-in for the interfaces the manager reads from.

    Sensor values and forecasts are plain attributes, so a test says what the world
    looks like instead of wiring five mocks.
    """

    def __init__(self, slot_count=48):
        self.slot_count = slot_count
        self.sensors = {}
        self.prices = [0.0003] * slot_count
        self.feed_in = [0.00008] * slot_count
        self.pv = [0.0] * slot_count
        self.load = [400.0] * slot_count
        self.temperature = [18.0] * 48
        self.reads = []

    def read_sensor(self, name):
        self.reads.append(name)
        return self.sensors.get(name)

    def sources(self):
        return ManagedLoadSources(
            read_sensor=self.read_sensor,
            price=lambda: self.prices,
            feed_in_price=lambda: self.feed_in,
            pv_forecast=lambda: self.pv,
            base_load=lambda: self.load,
            temperature_forecast=lambda: self.temperature,
        )


@pytest.fixture(name="installation")
def installation_fixture():
    return FakeInstallation()


@pytest.fixture(name="make_manager")
def make_manager_fixture(installation):
    """Build a manager over the fake installation. Never starts the poll thread."""

    def _make(entries, **kwargs):
        kwargs.setdefault("time_frame_base", 3600)
        kwargs.setdefault("time_zone", BERLIN)
        kwargs.setdefault("sources", installation.sources())
        kwargs.setdefault("clock", lambda: NOW)
        return ManagedLoadManager(entries, **kwargs)

    return _make
