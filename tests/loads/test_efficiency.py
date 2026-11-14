"""
Efficiency per slot: a Wh drawn in a warm hour does more than one drawn in a cold one.

What these defend is the behaviour that motivated it. With flat prices - or a battery
that makes every slot cost the same - a heat pump's budget used to land wherever the
search happened to reach first; on a real house at 07:09 and 2 C, with 13 C to come that
afternoon. With an efficiency the tie is broken towards the warm hours, and only as far
as the price difference allows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.loads import efficiency
from src.loads.injection import InjectionError, parse_push
from src.loads.instance import ManagedLoad
from src.loads.models.base import DemandContext, EnergyDemand
from src.loads.planner import PlanOptions, plan_contingent, reasons_for_schedule

ANCHOR = datetime(2026, 10, 29, tzinfo=timezone.utc)
SLOTS = 24


def _ctx(ambient=None, prices=None, current_slot=0):
    return DemandContext(
        now=ANCHOR + timedelta(hours=current_slot),
        anchor=ANCHOR,
        slot_count=SLOTS,
        time_frame_base=3600,
        current_slot=current_slot,
        ambient_temp_c=list(ambient or [5.0] * SLOTS),
        outdoor_temp_c=list(ambient or [5.0] * SLOTS),
        price_eur_per_wh=list(prices or [0.0003] * SLOTS),
        feed_in_eur_per_wh=[0.00008] * SLOTS,
        pv_surplus_wh=[0.0] * SLOTS,
    )


def _autumn_day():
    """2 C at dawn, 13 C mid-afternoon - the day the issue came from."""
    temps = []
    for hour in range(SLOTS):
        if hour <= 6:
            temps.append(2.0)
        elif hour <= 15:
            temps.append(2.0 + (hour - 6) * 11.0 / 9.0)
        else:
            temps.append(13.0 - (hour - 15) * 1.0)
    return temps


# -- the pure functions --------------------------------------------------------------


def test_the_best_usable_slot_is_one():
    series = efficiency.normalise([3.0, 4.0, 2.0], 3)
    assert series == [0.75, 1.0, 0.5]


def test_the_best_slot_is_chosen_among_the_usable_ones():
    """A warm hour the load may not use must not make every other hour look poor."""
    series = efficiency.normalise([3.0, 6.0, 2.0], 3, feasible=[True, False, True])
    assert series[0] == 1.0


def test_the_past_does_not_set_the_reference():
    series = efficiency.normalise([6.0, 3.0, 2.0], 3, start=1)
    assert series[1] == 1.0


def test_nothing_usable_means_every_slot_is_equal():
    assert efficiency.normalise([0, 0, 0], 3) == [1.0, 1.0, 1.0]


def test_efficiency_never_falls_below_the_floor():
    series = efficiency.normalise([10.0, 0.1], 2)
    assert series[1] == efficiency.MIN_EFFICIENCY


def test_the_ambient_rule_is_linear_from_the_warmest_hour():
    series = efficiency.from_ambient([10.0, 5.0, 0.0], 3, per_k=0.02)
    assert series == pytest.approx([1.0, 0.9, 0.8])


def test_for_cooling_the_coolest_hour_is_best():
    series = efficiency.from_ambient(
        [20.0, 25.0, 30.0], 3, per_k=0.02, reference="coolest"
    )
    assert series == pytest.approx([1.0, 0.9, 0.8])


def test_a_monotonic_rule_leaves_no_plateau_to_choose_arbitrarily_in():
    series = efficiency.from_ambient(_autumn_day(), SLOTS)
    warmest = max(range(SLOTS), key=lambda i: _autumn_day()[i])
    assert series[warmest] == 1.0
    assert all(
        value < 1.0
        for i, value in enumerate(series)
        if i != warmest and _autumn_day()[i] < _autumn_day()[warmest]
    )


def test_no_forecast_means_every_slot_is_equal():
    assert efficiency.from_ambient([], 3) == [1.0, 1.0, 1.0]


def test_an_unknown_source_is_off():
    assert efficiency.source_of({"efficiency_source": "magic"}) == efficiency.SOURCE_OFF
    assert efficiency.source_of({}) == efficiency.SOURCE_OFF


def test_delivered_energy_counts_each_slot_at_its_efficiency():
    assert efficiency.effective_wh([1000, 1000], [1.0, 0.5]) == pytest.approx(1500)
    assert efficiency.effective_wh([1000, 1000], None) == pytest.approx(2000)


# -- the fallback planner ------------------------------------------------------------


def _demand(total_wh, eff=None, power=1000.0):
    return EnergyDemand(
        total_wh=total_wh,
        feasible=[True] * SLOTS,
        max_power_w=power,
        efficiency=list(eff or []),
        detail={"active": True},
    )


def test_with_flat_prices_the_plan_goes_to_the_warm_hours():
    temps = _autumn_day()
    demand = _demand(3000, efficiency.from_ambient(temps, SLOTS))
    plan = plan_contingent(demand, _ctx(temps), PlanOptions(strategy="cheapest_slots"))
    used = [index for index, value in enumerate(plan) if value > 0]
    assert min(temps[index] for index in used) >= 10.0


def test_without_an_efficiency_the_plan_is_unchanged():
    demand = _demand(3000)
    plan = plan_contingent(demand, _ctx(), PlanOptions(strategy="cheapest_slots"))
    assert sum(plan) == pytest.approx(3000)


def test_a_less_efficient_slot_takes_more_energy_for_the_same_result():
    demand = _demand(900, [0.9] * SLOTS)
    plan = plan_contingent(demand, _ctx(), PlanOptions(strategy="cheapest_slots"))
    assert sum(plan) == pytest.approx(1000)


def test_a_much_cheaper_cold_hour_still_wins():
    """Efficiency breaks ties and weighs against price - it does not override it."""
    temps = _autumn_day()
    prices = [0.0003] * SLOTS
    prices[3] = 0.0001  # cold, but a third of the price
    demand = _demand(1000, efficiency.from_ambient(temps, SLOTS))
    plan = plan_contingent(
        demand, _ctx(temps, prices), PlanOptions(strategy="cheapest_slots")
    )
    assert plan[3] > 0


def test_the_reported_price_is_per_drawn_wh():
    demand = _demand(900, [0.9] * SLOTS)
    plan_contingent(demand, _ctx(), PlanOptions(strategy="cheapest_slots"))
    assert demand.plan_price_eur_per_wh == pytest.approx(0.0003)


def test_a_plan_that_covers_the_delivered_demand_is_not_short():
    demand = _demand(900, [0.9] * SLOTS)
    plan = [1000.0] + [0.0] * (SLOTS - 1)
    reasons = reasons_for_schedule(plan, demand, _ctx())
    assert "costs more than it is worth" not in reasons


# -- the instance applies the configured source --------------------------------------


def _instance(**config):
    entry = {"id": "hp", "type": "external_contingent", "rated_power_w": 1000}
    entry.update(config)
    return ManagedLoad(entry)


def _push(instance, payload, ctx):
    parsed = parse_push(
        payload,
        ctx.anchor,
        ctx.slot_count,
        ctx.time_frame_base,
        ctx.current_slot,
        now=ctx.now,
    )
    instance.accept_push(parsed, ctx.anchor, ctx.time_frame_base)


def test_an_external_budget_can_prefer_the_warm_hours_from_the_forecast():
    ctx = _ctx(_autumn_day())
    instance = _instance(efficiency_source="ambient")
    _push(instance, {"total_wh": 2000}, ctx)
    instance.evaluate(ctx)
    series = instance.last_demand.efficiency
    assert len(series) == SLOTS
    assert series[15] == 1.0 and series[3] < 0.9


def test_off_discards_a_pushed_efficiency():
    ctx = _ctx()
    instance = _instance(efficiency_source="off")
    _push(instance, {"total_wh": 2000, "efficiency": [0.5] * 24}, ctx)
    instance.evaluate(ctx)
    assert instance.last_demand.efficiency == []


def test_a_pushed_efficiency_is_used_when_asked_for():
    ctx = _ctx()
    instance = _instance(efficiency_source="pushed")
    _push(instance, {"total_wh": 2000, "efficiency": [0.5] * 12 + [1.0] * 12}, ctx)
    instance.evaluate(ctx)
    assert instance.last_demand.efficiency[:2] == [0.5, 0.5]
    assert instance.last_demand.efficiency[20] == 1.0


def test_cop_on_a_load_with_no_model_does_not_borrow_the_pushed_series():
    """
    `cop` means "the load's own efficiency at each hour's air temperature".

    An external budget has no model and therefore no COP. Handing it the sender's
    series for that setting would have `cop` and `pushed` do the same thing here,
    and the setting would say one thing while doing another.
    """
    ctx = _ctx()
    instance = _instance(efficiency_source="cop")
    _push(instance, {"total_wh": 2000, "efficiency": [0.5] * 24}, ctx)
    instance.evaluate(ctx)
    assert instance.last_demand.efficiency == []


def test_the_house_preset_plans_by_its_cop():
    temps = _autumn_day()
    ctx = _ctx(temps)
    ctx.readings = {"temp_sensor": "20.8"}
    instance = ManagedLoad({"id": "house", "type": "space_heating"})
    demand = instance.model.demand(ctx)
    assert demand.detail["efficiency_source"] == "cop"
    assert max(demand.efficiency) == 1.0
    assert demand.efficiency[3] < demand.efficiency[15]
    # The demand is the heat at the best hour's COP.
    assert demand.detail["reference_cop"] > demand.detail["mean_cop"]


# -- the push -------------------------------------------------------------------------


def _parse(payload, time_frame_base=3600, slot_count=SLOTS):
    return parse_push(payload, ANCHOR, slot_count, time_frame_base, 0, now=ANCHOR)


def test_an_hourly_efficiency_is_repeated_into_quarter_hours():
    parsed = _parse(
        {"total_wh": 1000, "efficiency": [0.5] + [1.0] * 23},
        time_frame_base=900,
        slot_count=96,
    )
    assert parsed.efficiency[:4] == [0.5] * 4 and parsed.efficiency[4] == 1.0


def test_a_quarter_hourly_efficiency_is_averaged_into_hours():
    parsed = _parse({"total_wh": 1000, "efficiency": [0.6, 0.8, 1.0, 1.0] + [1.0] * 92})
    assert parsed.efficiency[0] == pytest.approx(0.85)


def test_slots_the_push_does_not_reach_count_as_one():
    parsed = _parse(
        {"total_wh": 1000, "efficiency": [0.5] * 24, "start": "now"}, slot_count=48
    )
    assert parsed.efficiency[30] == 1.0


@pytest.mark.parametrize("bad", [[0.0] * 24, [1.5] * 24, "fast", []])
def test_an_impossible_efficiency_is_refused(bad):
    with pytest.raises(InjectionError):
        _parse({"total_wh": 1000, "efficiency": bad})


def test_a_push_without_an_efficiency_carries_none():
    assert _parse({"total_wh": 1000}).efficiency is None
