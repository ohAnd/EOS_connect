"""Registry behaviour: expiry, alignment, summing and the sanity guards."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.loads.contribution import (
    MAX_SLOT_WH,
    LoadContribution,
    LoadContributionRegistry,
    ttl_from_minutes,
)

BERLIN = ZoneInfo("Europe/Berlin")


def _anchor(day=1, tz=BERLIN):
    """Local midnight of a fixed day, so tests never depend on the wall clock."""
    return datetime(2026, 6, day, 0, 0, tzinfo=tz)


def _contribution(entry_id="pool", values=None, slots=48, base=3600, day=1, ttl_h=24):
    series = values if values is not None else [100.0] * slots
    anchor = _anchor(day)
    return LoadContribution(
        id=entry_id,
        slots_wh=series,
        anchor=anchor,
        time_frame_base=base,
        valid_until=anchor.astimezone(timezone.utc) + timedelta(hours=ttl_h),
    )


def test_naive_datetimes_are_rejected():
    """An anchor without a timezone silently shifts every slot - refuse it loudly."""
    with pytest.raises(ValueError):
        LoadContribution(
            id="x",
            slots_wh=[1.0],
            anchor=datetime(2026, 6, 1),
            time_frame_base=3600,
            valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )


def test_non_numeric_and_infinite_slots_become_zero():
    """One bad slot in a pushed array must not discard the other 191."""
    item = _contribution(values=[10.0, "nonsense", float("nan"), float("inf"), None, 20.0])
    assert item.slots_wh == [10.0, 0.0, 0.0, 0.0, 0.0, 20.0]


def test_slots_are_clamped_symmetrically():
    """Negative contributions are legitimate (issue #55's cooling delta); huge ones are not."""
    item = _contribution(values=[MAX_SLOT_WH * 5, -MAX_SLOT_WH * 5])
    assert item.slots_wh == [MAX_SLOT_WH, -MAX_SLOT_WH]


def test_expired_contributions_are_not_summed():
    registry = LoadContributionRegistry()
    item = _contribution(ttl_h=1)
    registry.set(item)

    before = item.anchor.astimezone(timezone.utc) + timedelta(minutes=30)
    after = item.anchor.astimezone(timezone.utc) + timedelta(hours=2)

    assert registry.total(item.anchor, 48, 3600, now=before)[0] == 100.0
    assert registry.total(item.anchor, 48, 3600, now=after)[0] == 0.0


def test_expiry_is_logged_once(caplog):
    """A contribution that stops counting changes the optimizer result - say so, once."""
    registry = LoadContributionRegistry()
    item = _contribution(ttl_h=1)
    registry.set(item)
    after = item.anchor.astimezone(timezone.utc) + timedelta(hours=2)

    with caplog.at_level("INFO", logger="__main__"):
        registry.active(now=after)
        registry.active(now=after)

    expiry_lines = [r for r in caplog.records if "expired" in r.getMessage()]
    assert len(expiry_lines) == 1


def test_resetting_a_contribution_re_arms_the_expiry_log(caplog):
    registry = LoadContributionRegistry()
    registry.set(_contribution(ttl_h=1))
    after = _anchor().astimezone(timezone.utc) + timedelta(hours=2)
    registry.active(now=after)

    registry.set(_contribution(ttl_h=1))
    with caplog.at_level("INFO", logger="__main__"):
        registry.active(now=after)
    assert any("expired" in r.getMessage() for r in caplog.records)


def test_contributions_from_several_instances_sum():
    """The whole point: a pool and a sauna both land in one gesamtlast."""
    registry = LoadContributionRegistry()
    registry.set(_contribution("pool", values=[1800.0] * 48))
    registry.set(_contribution("sauna", values=[2400.0] * 48))
    registry.set(_contribution("heating", values=[900.0] * 48))

    total = registry.total(_anchor(), 48, 3600, now=_anchor().astimezone(timezone.utc))
    assert total[0] == pytest.approx(5100.0)
    assert len(total) == 48


def test_a_mismatched_slot_resolution_is_skipped_not_reinterpreted(caplog):
    """Reading a 15-minute series as hourly would inflate the forecast fourfold."""
    registry = LoadContributionRegistry()
    registry.set(_contribution("quarter", values=[100.0] * 192, base=900))

    with caplog.at_level("WARNING", logger="__main__"):
        total = registry.total(_anchor(), 48, 3600, now=_anchor().astimezone(timezone.utc))

    assert total == [0.0] * 48
    assert any("15" in r.getMessage() or "900" in r.getMessage() for r in caplog.records)


def test_alignment_survives_midnight():
    """
    Day two of a 48 h contribution becomes day one after the rollover.

    Nothing is extrapolated past the end of the stored series - the slots we have no
    data for report zero rather than reusing yesterday's numbers.
    """
    values = [10.0] * 24 + [20.0] * 24
    item = _contribution(values=values, day=1)
    registry = LoadContributionRegistry()
    registry.set(item)

    tomorrow = _anchor(day=2)
    total = registry.total(tomorrow, 48, 3600, now=item.anchor.astimezone(timezone.utc))

    assert total[:24] == [20.0] * 24
    assert total[24:] == [0.0] * 24


def test_alignment_handles_a_clock_moving_backwards():
    item = _contribution(values=[10.0] * 48, day=2)
    aligned = item.aligned_to(_anchor(day=1), 48)
    assert aligned[:24] == [0.0] * 24
    assert aligned[24:] == [10.0] * 24


def test_drop_removes_a_contribution():
    registry = LoadContributionRegistry()
    registry.set(_contribution("pool"))
    assert registry.drop("pool") is True
    assert registry.drop("pool") is False
    assert registry.total(_anchor(), 48, 3600) == [0.0] * 48


def test_snapshot_reports_totals_and_liveness():
    registry = LoadContributionRegistry()
    registry.set(_contribution("pool", values=[100.0] * 48, ttl_h=1))
    moment = _anchor().astimezone(timezone.utc) + timedelta(hours=2)

    snapshot = registry.snapshot(now=moment)
    assert len(snapshot) == 1
    assert snapshot[0]["id"] == "pool"
    assert snapshot[0]["total_wh"] == pytest.approx(4800.0)
    assert snapshot[0]["active"] is False


@pytest.mark.parametrize("minutes,expected", [(0, 1), (-5, 1), (30, 30), (99999, 7 * 24 * 60)])
def test_ttl_is_clamped_to_something_usable(minutes, expected):
    """A zero TTL would expire the contribution before the next optimizer run reads it."""
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert ttl_from_minutes(minutes, now=now) == now + timedelta(minutes=expected)
