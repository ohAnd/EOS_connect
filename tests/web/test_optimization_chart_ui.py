"""
Loads the optimizer schedules, on the optimization chart.

Under the built-in optimizer a managed load is not in `gesamtlast` - it is a variable
the solver places - so the Load bar stopped accounting for it while the Grid bar it
caused kept growing. On the live instance that read as 0.05 kW of load beside 0.40 kW
of pool drawing 0.10 kW from the grid, with the pool visible only in its effects.

What these mostly defend is the alignment. The chart draws the horizon starting *now*,
which is the stored array rotated and cut; a band that makes that trip differently from
the Load bar lines up with the wrong hours and looks entirely reasonable doing it.
"""

import pytest

import tests.web.wizard_driver as wz  # noqa: F401  (keeps the shared skip behaviour)


def _align(page, series, slot, length, base):
    return page.evaluate(
        """([series, slot, length, base]) =>
            ChartManager.alignToAxis(series, slot, length, base)""",
        [series, slot, length, base],
    )


def _band(page, schedules, slot, length, base):
    return page.evaluate(
        """([schedules, slot, length, base]) =>
            ChartManager.managedLoadSeries(schedules, slot, length, base)""",
        [schedules, slot, length, base],
    )


# --- the trip onto the x-axis -----------------------------------------------------------

def test_the_axis_starts_at_the_current_slot(page):
    series = list(range(192))
    assert _align(page, series, 24, 192, 900)[:3] == [24, 25, 26]


def test_a_single_marked_slot_keeps_its_hour(page):
    """The property an off-by-one breaks quietly: 07:00 has to stay 07:00."""
    series = [0] * 192
    series[28] = 5                      # 07:00 at quarter-hour resolution
    aligned = _align(page, series, 24, 192, 900)   # viewed from 06:00
    assert aligned.index(5) == 4                   # four quarter-hours later


def test_the_axis_is_cut_to_what_the_solver_answered_for(page):
    assert len(_align(page, list(range(192)), 34, 145, 900)) == 145


def test_an_empty_series_stays_empty(page):
    assert _align(page, [], 10, 145, 900) == []
    assert _align(page, None, 10, 145, 900) == []


def test_the_hourly_branch_keeps_its_own_shape(page):
    """
    Not a plain rotation - it appends tomorrow again. Whatever it is, the band has to
    do the same thing or it will not line up with the Load bar.
    """
    series = list(range(48))
    aligned = _align(page, series, 7, 48, 3600)
    expected = series[7:] + series[24:48]
    assert aligned == expected[:48]


# --- the band ---------------------------------------------------------------------------

def test_a_scheduled_load_lands_in_the_band(page):
    schedules = {"pool": [0] * 192}
    schedules["pool"][100] = 400.0
    band = _band(page, schedules, 24, 192, 900)
    assert band[76] == "0.400"
    assert sum(float(value) for value in band) == pytest.approx(0.4)


def test_several_loads_sum_into_one_band(page):
    """One band, because the total is what the Load bar was missing."""
    schedules = {
        "pool": [0] * 192,
        "sauna": [0] * 192,
    }
    schedules["pool"][100] = 400.0
    schedules["sauna"][100] = 1000.0
    assert _band(page, schedules, 24, 192, 900)[76] == "1.400"


def test_no_schedule_draws_nothing(page):
    """
    Under the other optimizers the loads are still inside gesamtlast, so the Load bar
    already counts them. A zero line here would be a second, empty legend entry; a
    non-zero one would count them twice.
    """
    assert _band(page, {}, 24, 192, 900) == []
    assert _band(page, None, 24, 192, 900) == []


def test_a_schedule_of_all_zeros_draws_nothing(page):
    """Scheduled, but placed nowhere - there is no band to draw."""
    assert _band(page, {"pool": [0] * 192}, 24, 192, 900) == []


def test_the_band_is_as_long_as_the_axis(page):
    schedules = {"pool": [400.0] * 192}
    assert len(_band(page, schedules, 34, 145, 900)) == 145


# --- wired into the chart ---------------------------------------------------------------

def _fixtures(slots=192, base=900, managed=None, forecast=None):
    load = [400.0] * slots
    request = {
        "ems": {
            "gesamtlast": load,
            "pv_prognose_wh": [0.0] * slots,
            "strompreis_euro_pro_wh": [0.0003] * slots,
            "einspeiseverguetung_euro_pro_wh": [0.00008] * slots,
        }
    }
    response = {
        # 06:00 with no offset, read by a browser pinned to UTC: slot 24 at quarter-
        # hourly resolution, whatever the runner's own zone and whether it is summer.
        "timestamp": "2026-09-13T06:00:00+00:00",
        "ac_charge": [0.0] * slots,
        "dc_charge": [0.0] * slots,
        "discharge_allowed": [0] * slots,
        "result": {
            "Last_Wh_pro_Stunde": [400.0] * slots,
            "Home_appliance_wh_per_hour": [0.0] * slots,
            "Netzbezug_Wh_pro_Stunde": [400.0] * slots,
            "Netzeinspeisung_Wh_pro_Stunde": [0.0] * slots,
            "akku_soc_pro_stunde": [50.0] * slots,
            "Kosten_Euro_pro_Stunde": [0.0] * slots,
            "Einnahmen_Euro_pro_Stunde": [0.0] * slots,
            "Electricity_price": [0.0003] * slots,
        },
    }
    if managed is not None:
        response["managed_loads"] = managed
    if forecast is not None:
        response["managed_load_forecast"] = forecast
    controls = {
        "used_time_frame_base": base,
        "used_optimization_source": "local_evopt",
        "current_states": {},
    }
    return request, response, controls


def _run_update(page, request, response, controls):
    """Drive the real updateChart over a stand-in chart, and read the datasets back."""
    return page.evaluate(
        """([request, response, controls]) => {
            const manager = new ChartManager();
            manager.chartInstance = {
                data: {
                    labels: [],
                    datasets: Array.from({length: 14}, () => ({data: []})),
                },
                // updateChart writes axis titles and tick sizes; give it the shape it
                // expects rather than a bare object, or it fails before the datasets.
                options: {
                    plugins: {legend: {}},
                    scales: Object.fromEntries(
                        ['x', 'y', 'y1', 'y2', 'y3'].map(name =>
                            [name, {title: {}, ticks: {}}])
                    ),
                },
                update: () => {},
            };
            manager.chartInstance.data.datasets[13].label = 'Managed Loads';
            try {
                manager.updateChart(request, response, controls, null);
            } catch (err) {
                return {error: String(err)};
            }
            return {
                load: manager.chartInstance.data.datasets[0].data,
                managed: manager.chartInstance.data.datasets[13].data,
            };
        }""",
        [request, response, controls],
    )


def test_a_schedule_reaches_the_chart(page):
    managed = {"pool": [0.0] * 192}
    managed["pool"][100] = 400.0
    result = _run_update(page, *_fixtures(managed=managed))
    assert "error" not in result, result.get("error")
    assert result["managed"][76] == "0.400"


def test_the_band_lines_up_with_the_load_bar(page):
    """Same length and same offset, or it describes the wrong hours."""
    managed = {"pool": [400.0] * 192}
    result = _run_update(page, *_fixtures(managed=managed))
    assert "error" not in result, result.get("error")
    assert len(result["managed"]) == len(result["load"])


def test_without_a_schedule_the_band_is_empty(page):
    result = _run_update(page, *_fixtures())
    assert "error" not in result, result.get("error")
    assert result["managed"] == []
    assert len(result["load"]) == 192


def test_the_load_bar_is_left_alone_either_way(page):
    """Adding the band must not disturb what was already drawn."""
    managed = {"pool": [400.0] * 192}
    with_band = _run_update(page, *_fixtures(managed=managed))
    without = _run_update(page, *_fixtures())
    assert with_band["load"] == without["load"]


# --- the loads the solver never sees (issue #362) ----------------------------------------

def test_a_forecast_only_load_gets_a_band_of_its_own(page):
    """
    A profile load is in `gesamtlast` and nowhere else - the solver is never asked to
    place it, so nothing ever reached `managed_loads` and the band stayed empty while
    the appliance sat invisible inside the Load bar. That is what #362 reported.
    """
    forecast = {"heating": [0.0] * 192}
    forecast["heating"][100] = 400.0
    result = _run_update(page, *_fixtures(forecast=forecast))
    assert "error" not in result, result.get("error")
    assert result["managed"][76] == "0.400"


def test_the_load_bar_gives_up_exactly_what_the_band_takes(page):
    """
    The forecast half is energy already counted in `gesamtlast`, so drawing it twice
    would overstate the household by the size of the appliance. The stack total has to
    come out identical to the bar before the band existed.
    """
    forecast = {"heating": [0.0] * 192}
    forecast["heating"][100] = 400.0
    result = _run_update(page, *_fixtures(forecast=forecast))
    plain = _run_update(page, *_fixtures())
    assert "error" not in result, result.get("error")

    assert result["load"][76] == "0.000"
    assert float(result["load"][76]) + float(result["managed"][76]) == pytest.approx(
        float(plain["load"][76])
    )
    assert result["load"][:76] == plain["load"][:76]
    assert result["load"][77:] == plain["load"][77:]


def test_a_placed_load_still_leaves_the_load_bar_alone(page):
    """
    Only the forecast half comes out. What the solver placed was never in `gesamtlast`
    to begin with, so subtracting it would take the household below what it will use.
    """
    managed = {"pool": [400.0] * 192}
    with_band = _run_update(page, *_fixtures(managed=managed))
    without = _run_update(page, *_fixtures())
    assert with_band["load"] == without["load"]


def test_both_halves_are_drawn_as_one_band(page):
    managed = {"pool": [0.0] * 192}
    managed["pool"][100] = 400.0
    forecast = {"heating": [0.0] * 192}
    forecast["heating"][100] = 100.0
    result = _run_update(page, *_fixtures(managed=managed, forecast=forecast))
    assert "error" not in result, result.get("error")
    assert result["managed"][76] == "0.500"


def test_the_forecast_band_rides_the_same_rotation_as_the_load_bar(page):
    """A band that makes the trip onto the x-axis differently names the wrong hours."""
    forecast = {"heating": list(range(192))}
    band = _band(page, forecast, 24, 192, 900)
    aligned = _align(page, list(range(192)), 24, 192, 900)

    assert band == [f"{value / 1000:.3f}" for value in aligned]
    assert band[0] == "0.024"


def test_an_id_in_both_halves_is_summed_rather_than_shadowed(page):
    """
    They are disjoint by design - a load is either timed by the solver or part of the
    forecast. Merging by key would drop one silently if that ever stopped holding, and
    a band quietly too short is the hardest kind of wrong to notice.
    """
    merged = page.evaluate(
        """() => ChartManager.mergeSchedules({pool: [1, 2]}, {pool: [10, 20]})"""
    )
    assert merged == {"pool": [11, 22]}


def test_merging_keeps_the_longer_of_two_series(page):
    merged = page.evaluate(
        """() => ChartManager.mergeSchedules({pool: [1]}, {pool: [10, 20]})"""
    )
    assert merged == {"pool": [11, 20]}


def test_an_absent_forecast_key_still_leaves_the_band_empty(page):
    result = _run_update(page, *_fixtures())
    assert "error" not in result, result.get("error")
    assert result["managed"] == []
