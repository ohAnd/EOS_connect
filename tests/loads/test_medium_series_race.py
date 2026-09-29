"""
A sample landing mid-read must not take down the card it is drawn for.

``medium_series_state`` walks the same bounded deque that ``_remember_medium``
appends to from the managed-loads poll thread, and a deque raises rather than
tolerating an append underneath a reader. The only symptom is a 500 on
``GET /api/managed_loads`` - the endpoint that renders this very series - so the
failure surfaces on the feature's own overlay and nowhere near its cause, and it
does so rarely enough that no other test would ever catch it.

Worth its own file because the fix is a single ``list()`` whose reason is invisible
from the call site.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.loads.manager import ManagedLoadManager, ManagedLoadSources

BERLIN = ZoneInfo("Europe/Berlin")
ANCHOR = datetime(2026, 6, 1, 0, 0, tzinfo=BERLIN)


class _Ctx:
    """A planning context whose slot lookup fires a callback, i.e. inside the loop."""

    slot_count = 4
    anchor = ANCHOR
    time_frame_base = 3600

    def __init__(self, on_lookup=None):
        self._on_lookup = on_lookup

    def slot_of(self, when):
        """Map a moment to its slot, running the caller's hook on the way through."""
        if self._on_lookup is not None:
            self._on_lookup()
        return int((when - self.anchor).total_seconds() // self.time_frame_base)


def _item():
    """A thermal load reduced to what the series helper actually reads off it."""
    return SimpleNamespace(
        id="pool",
        model=SimpleNamespace(
            project_medium=lambda ctx, plan, current: [current] * ctx.slot_count
        ),
        last_demand=SimpleNamespace(
            detail={"temperature_c": 24.0, "target_temperature_c": 26.0}
        ),
        last_plan=[],
    )


def _manager_with_history():
    """A manager holding three recorded temperatures and no configured loads."""
    manager = ManagedLoadManager(
        [], time_frame_base=3600, time_zone=BERLIN,
        sources=ManagedLoadSources(read_sensor=lambda name: None),
    )
    for hour in range(3):
        manager._remember_medium(  # pylint: disable=protected-access
            "pool", ANCHOR + timedelta(hours=hour), 20.0 + hour
        )
    return manager


def test_a_sample_arriving_mid_read_does_not_break_the_card():
    """The poll thread's append lands while the reader is inside the loop."""
    manager = _manager_with_history()

    def poll_thread_writes():
        manager._remember_medium(  # pylint: disable=protected-access
            "pool", ANCHOR + timedelta(hours=3), 23.0
        )

    state = manager.medium_series_state(_item(), _Ctx(on_lookup=poll_thread_writes))

    assert state is not None


def test_the_read_reports_the_history_it_started_with():
    """The snapshot must still place every recorded sample in its own slot."""
    state = _manager_with_history().medium_series_state(_item(), _Ctx())

    assert state["history_c"] == [20.0, 21.0, 22.0, None]
    assert state["target_c"] == 26.0
