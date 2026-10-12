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
