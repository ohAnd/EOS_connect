"""
The battery price calculation fed from EVCC history.

With EVCC as the data source, every power series the price handler needs arrives
through the load interface's virtual entities - and EVCC's energy/returnEnergy
split is unambiguous, so the sign-convention auto-detection, which used to be
the first thing the calculation ran, has to be skipped rather than run on
already-normalized data.
"""

# pylint: disable=protected-access
from datetime import datetime, timedelta, timezone

import pytest

from src.interfaces.battery_price_handler import BatteryPriceHandler

EVCC_CONFIG = {
    "source": "evcc",
    "url": "http://evcc:7070",
    "price_calculation_enabled": True,
    "battery_power_sensor": "group:battery",
    "pv_power_sensor": "group:pv",
    "grid_power_sensor": "group:grid",
    "load_power_sensor": "group:home",
    "price_sensor": "tariff:grid",
    "capacity_wh": 11000,
    "charge_efficiency": 0.95,
}


def _handler():
    handler = BatteryPriceHandler(dict(EVCC_CONFIG))
    handler.load_interface = None
    return handler


def _evcc_samples(day_start, watts, step_minutes=5):
    """One series as the price handler's internal shape, at 5-min spacing.

    The handler tolerates 10-minute gaps inside a charging event; 15-minute
    EVCC slots therefore read as separate events until enough slots have
    accumulated - spacing the fixture like a live-tuned sensor keeps the test
    on the behaviour being pinned rather than on that tolerance.
    """
    points = []
    for index, watt in enumerate(watts):
        points.append(
            {
                "timestamp": day_start + timedelta(minutes=step_minutes * index),
                "value": float(watt),
            }
        )
    return points


def test_conventions_are_fixed_not_detected():
    """EVCC's energy/returnEnergy split decides the sign; detection is skipped.

    The detection fits four sign combinations against an energy balance - on
    already-normalized series that could latch onto the wrong one whenever two
    combinations fit equally well, and a wrong battery convention turns every
    charging session into a discharging one.
    """
    handler = _handler()
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    historical = {
        "battery_power": _evcc_samples(day_start, [2000, 1500, 800, 0, 0]),
        "pv_power": _evcc_samples(day_start, [3000, 2000, 900, 100, 0]),
        "grid_power": _evcc_samples(day_start, [100, 0, 0, 50, 0]),
        "load_power": _evcc_samples(day_start, [1100, 500, 100, 150, 0]),
        "price_data": _evcc_samples(day_start, [0.00030] * 5),
    }
    battery_conv, grid_conv = handler._detect_sensor_conventions(historical)

    # The fixed conventions the EVCC branch pins: charging positive, import
    # positive - exactly what EVCC's columns normalize to.
    assert (battery_conv, grid_conv) == ("positive_charging", "positive_import")
    assert handler.battery_power_convention == "positive_charging"
    assert handler.grid_power_convention == "positive_import"


def test_identify_charging_periods_finds_short_events():
    handler = _handler()
    handler.battery_power_convention = "positive_charging"
    handler.grid_power_convention = "positive_import"
    day_start = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
    historical = {
        "battery_power": _evcc_samples(day_start, [3000, 3000, 3000, 0, 0]),
        "pv_power": _evcc_samples(day_start, [3000] * 5),
        "grid_power": _evcc_samples(day_start, [0] * 5),
        "load_power": _evcc_samples(day_start, [0] * 5),
        "price_data": _evcc_samples(day_start, [0.00030] * 5),
    }
    events = handler._identify_charging_periods(historical)
    assert len(events) == 1
    assert events[0]["start_time"] == day_start


class TestTheBatteryIsReadAsAStepLikeEverythingElse:
    """
    Every series reaching the split is a step function.

    A Home Assistant state holds until the next one is reported, and an EVCC
    slot's rate holds until the following stamp - the load interface says so
    where it builds the series: "the slot *starting* there is the one whose
    rate holds until the following stamp". ``_get_aligned_value`` reads PV,
    grid and load that way.

    The battery used to be averaged across the interval instead,
    ``(p_start + p_end) / 2``, which shifted it half a slot against the other
    three. On EVCC's 15-minute slots that showed up as the battery charging in
    slots where nothing was available to charge it - and the split charges what
    it cannot explain to PV, so the shift leaned one way.
    """

    HOUR = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    def _event(self, watts):
        """One charging event whose battery power steps between slots."""
        return {
            "power_points": [
                {"timestamp": self.HOUR + timedelta(minutes=15 * i), "value": w}
                for i, w in enumerate(watts)
            ]
        }

    def _flat(self, key, watts):
        return {
            key: [
                {"timestamp": self.HOUR + timedelta(minutes=15 * i), "value": watts}
                for i in range(6)
            ]
        }

    def test_the_holding_value_is_used_not_the_midpoint(self):
        """
        Two slots: 0 W then 4000 W. The first interval charged nothing; the
        midpoint would call it 2000 W and look for 2000 W of sources to explain.
        """
        handler = _handler()
        handler.grid_charge_threshold_w = 100.0
        event = self._event([0, 4000, 4000])
        history = {
            **self._flat("pv_power", 0.0),
            **self._flat("grid_power", 8000.0),
            **self._flat("load_power", 0.0),
        }

        totals = handler._split_energy_sources(event, history)

        # Two intervals of 15 min: 0 W then 4000 W -> 1000 Wh, all from grid.
        assert totals["total_battery_wh"] == pytest.approx(1000.0)
        assert totals["grid_to_battery_wh"] == pytest.approx(1000.0)
        assert totals["pv_to_battery_wh"] == pytest.approx(0.0)

    def test_a_step_down_is_not_carried_into_the_slot_before_it(self):
        """The other direction - the midpoint understates a slot that ends high."""
        handler = _handler()
        handler.grid_charge_threshold_w = 100.0
        event = self._event([4000, 0, 0])
        history = {
            **self._flat("pv_power", 0.0),
            **self._flat("grid_power", 8000.0),
            **self._flat("load_power", 0.0),
        }

        totals = handler._split_energy_sources(event, history)

        assert totals["total_battery_wh"] == pytest.approx(1000.0)
        assert totals["grid_to_battery_wh"] == pytest.approx(1000.0)

    def test_a_flat_series_is_unaffected(self):
        """Where the two readings agree, step and midpoint are the same number."""
        handler = _handler()
        handler.grid_charge_threshold_w = 100.0
        event = self._event([2000, 2000, 2000])
        history = {
            **self._flat("pv_power", 0.0),
            **self._flat("grid_power", 8000.0),
            **self._flat("load_power", 0.0),
        }

        totals = handler._split_energy_sources(event, history)

        assert totals["total_battery_wh"] == pytest.approx(1000.0)


class TestChargeNoIntervalCouldPlace:
    """
    Two things make the split come up short, and they want opposite answers.

    A DC-coupled inverter charges the battery straight off the panels, so the
    energy never reaches the AC side the PV meter sits on: the charge really is
    PV. Meters stamped on their own clocks produce the same appearance for a
    different reason - the battery's slot reports charging before the PV or
    grid slot reports anything flowing. That is a boundary, not PV.

    Treating every remainder as PV turned the second into the first. On a real
    EVCC site it booked 18% of a day's charging to PV that had no PV behind it;
    the same site read 44% PV where Home Assistant read 34%.
    """

    T0 = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    def _series(self, values):
        return [
            {"timestamp": self.T0 + timedelta(minutes=15 * i), "value": v}
            for i, v in enumerate(values)
        ]

    def _history(self, pv, grid, load, battery, price=0.30):
        return {
            "pv_power": self._series(pv),
            "grid_power": self._series(grid),
            "load_power": self._series(load),
            "battery_power": self._series(battery),
            "price_data": self._series([price] * len(pv)),
        }

    # A window that balances: the first interval had grid import to spare, the
    # second charges with nothing reported flowing. Classic boundary shift.
    SHIFTED = dict(
        pv=[1200, 0, 0], grid=[2000, 0, 0], load=[0, 0, 0], battery=[1000, 2000, 2000]
    )

    def test_a_balanced_window_has_no_unmetered_source(self):
        handler = _handler()
        assert handler._window_has_unmetered_pv(self._history(**self.SHIFTED)) is False

    def test_a_window_that_comes_up_short_does(self):
        """Charge arriving from nowhere measurable is what DC coupling looks like."""
        handler = _handler()
        history = self._history(
            pv=[0, 0, 0], grid=[0, 0, 0], load=[0, 0, 0], battery=[4000, 4000, 4000]
        )
        assert handler._window_has_unmetered_pv(history) is True

    def test_the_remainder_is_bounded_by_pv_that_went_spare(self):
        """
        Interval one charged 1000 W against a 1200 W surplus, so 200 W of PV
        went unused. Interval two charges 2000 W with nothing reported. Only
        the 200 W of headroom can honestly be called PV; the rest is grid.
        """
        handler = _handler()
        handler.grid_charge_threshold_w = 100.0
        history = self._history(**self.SHIFTED)
        event = {"power_points": self._series(self.SHIFTED["battery"])}

        totals = handler._split_energy_sources(event, history)

        # 250 Wh placed in interval one, plus the 50 Wh of spare PV surplus.
        assert totals["pv_to_battery_wh"] == pytest.approx(300.0)
        assert totals["grid_to_battery_wh"] == pytest.approx(450.0)

    def test_an_unmetered_site_keeps_the_whole_remainder_as_pv(self):
        """The branch is not wrong - it was only ever unconditional."""
        handler = _handler()
        handler.grid_charge_threshold_w = 100.0
        history = self._history(**self.SHIFTED)
        event = {"power_points": self._series(self.SHIFTED["battery"])}

        handler._window_has_unmetered_pv = lambda _history: True
        totals = handler._split_energy_sources(event, history)

        assert totals["pv_to_battery_wh"] == pytest.approx(750.0)
        assert totals["grid_to_battery_wh"] == pytest.approx(0.0)

    def test_the_deferred_grid_energy_keeps_its_own_price(self):
        """
        Settling after the event must not cost the price association: the
        remainder is carried with the price that was running when it arrived.
        """
        handler = _handler()
        handler.grid_charge_threshold_w = 100.0
        history = self._history(**self.SHIFTED, price=0.40)
        event = {"power_points": self._series(self.SHIFTED["battery"])}

        totals = handler._split_energy_sources(event, history)

        assert totals["grid_cost_euro"] == pytest.approx(0.450 * 0.40)
