"""
The schedule table folds quarter-hours into clock hours (#374).

Two layouts reach it. The control arrays - ``ac_charge``, ``discharge_allowed``,
``dc_charge`` - start at local midnight; the ``result`` arrays start at the current
slot. Folding a midnight array the way a result array is folded shifts every row by the
quarters already gone in the current hour: at 17:39 the "05:00" row summed 04:30-05:30
and showed a charge the chart had ending at 05:00.
"""

import pytest

import tests.web.wizard_driver as wz  # noqa: F401  (keeps the shared skip behaviour)


def _fold(page, method, series, timestamp, full):
    return page.evaluate(
        """([method, series, timestamp, full]) =>
            new ScheduleManager()[method](series, timestamp, full)""",
        [method, series, timestamp, full],
    )


# --- midnight-anchored arrays -----------------------------------------------------------

@pytest.mark.parametrize("minute", ["00", "15", "39", "45"])
def test_a_midnight_slot_stays_in_its_clock_hour_at_any_minute(page, minute):
    series = [0] * 192
    series[20] = 400.0                  # 05:00-05:15
    hours = _fold(
        page, "convertQuarterlyToHourlySum", series, f"2026-11-08T17:{minute}:00Z", True
    )
    assert len(hours) == 48
    assert hours[5] == 400.0
    assert sum(hours) == 400.0


def test_the_reported_case_ends_at_five(page):
    """#374: charging in 04:30 and 04:45, viewed at 17:39 - nothing left at 05:00."""
    series = [0] * 192
    series[18] = 440.0
    series[19] = 440.0
    hours = _fold(page, "convertQuarterlyToHourlySum", series, "2026-11-08T17:39:00Z", True)
    assert hours[4] == 880.0
    assert hours[5] == 0


def test_a_midnight_flag_is_averaged_within_its_clock_hour(page):
    series = [0] * 192
    series[21] = 1                      # 05:15-05:30
    hours = _fold(page, "convertQuarterlyToHourly", series, "2026-11-08T17:39:00Z", True)
    assert hours[4] == 0
    assert hours[5] == 0.25


# --- arrays that start at the current slot ----------------------------------------------

def test_a_result_array_starts_with_what_is_left_of_the_hour(page):
    """At 17:39 the first hour is 17:30 and 17:45; full hours follow."""
    hours = _fold(
        page, "convertQuarterlyToHourlySum", [1, 2, 3, 4, 5, 6], "2026-11-08T17:39:00Z", False
    )
    assert hours == [3, 18]


def test_a_result_array_on_the_hour_folds_in_fours(page):
    hours = _fold(
        page, "convertQuarterlyToHourly", [1, 1, 1, 1, 3, 3, 3, 3], "2026-11-08T17:00:00Z", False
    )
    assert hours == [1, 3]


# --- wired into the table ---------------------------------------------------------------

def _render(page, ac_charge):
    current_slot = 70                   # 17:30, read by a browser pinned to UTC
    remaining = 192 - current_slot
    payload = {
        "request": {"pv_akku": {"max_charge_power_w": 1000}},
        "response": {
            "timestamp": "2026-11-08T17:39:00+00:00",
            "current_slot": current_slot,
            "ac_charge": ac_charge,
            "dc_charge": [0] * 192,
            "discharge_allowed": [1] * 192,
            "result": {
                "Electricity_price": [0.0004] * remaining,
                "akku_soc_pro_stunde": [50.0] * remaining,
                "Kosten_Euro_pro_Stunde": [0.0] * remaining,
                "Einnahmen_Euro_pro_Stunde": [0.0] * remaining,
            },
        },
        "controls": {
            "used_time_frame_base": 900,
            "used_optimization_source": "evopt",
            "current_states": {"inverter_mode_num": 2, "override_active": False},
        },
    }
    return page.evaluate(
        """(p) => {
            new ScheduleManager().showSchedule(p.request, p.response, p.controls);
            const rows = {};
            document.querySelectorAll('#discharge_scheduler .table-body .table-row')
                .forEach(row => {
                    const cells = row.querySelectorAll('.table-cell');
                    if (cells.length > 1) {
                        rows[cells[0].textContent] = cells[1].textContent.trim();
                    }
                });
            return rows;
        }""",
        payload,
    )


def test_the_table_shows_the_charge_in_the_hour_the_chart_does(page):
    ac_charge = [0.0] * 192
    # Tomorrow's 04:30 and 04:45 - today's are behind a table that starts at 17:00.
    ac_charge[96 + 18] = 0.44
    ac_charge[96 + 19] = 0.44
    rows = _render(page, ac_charge)
    assert rows["04:00"] == "0.9 kWh"
    assert "kWh" not in rows["05:00"]
    assert "kWh" not in rows["03:00"]
