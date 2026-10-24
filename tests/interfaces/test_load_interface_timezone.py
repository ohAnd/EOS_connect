"""
The configured time zone has to reach the load interface, whatever shape it has.

It decides where a day begins. The profile is built on a midnight-to-midnight
grid and the optimizer indexes that grid from midnight in the configured zone,
so the two have to mean the same midnight. When the setting is dropped the
interface falls back to the host clock, and on any machine whose clock is not
the configured zone the entire load forecast is offset by the difference —
silently, because the energy totals stay right and only the time axis moves.

That is not hypothetical: ``eos_connect`` builds ``pytz.timezone(...)`` and
passes the **object**. A tzinfo object matched neither branch of the old check
and fell through to "no zone", so every containerised install — a Home
Assistant add-on runs UTC — produced a load profile shifted by its own UTC
offset while the configuration said Europe/Berlin.
"""

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest
import pytz

from src.interfaces.load_interface import LoadInterface

BERLIN = "Europe/Berlin"


def _interface(tz):
    return LoadInterface({"source": "default"}, 900, tz)


@pytest.mark.parametrize(
    "tz",
    [
        pytest.param(pytz.timezone(BERLIN), id="pytz-object"),
        pytest.param(ZoneInfo(BERLIN), id="zoneinfo-object"),
        pytest.param(BERLIN, id="name"),
    ],
)
def test_every_shape_the_callers_pass_is_accepted(tz):
    """eos_connect passes an object, the config a name. Both must arrive."""
    resolved = _interface(tz).time_zone
    assert resolved is not None, "the configured zone was dropped"
    assert isinstance(resolved, tzinfo)


@pytest.mark.parametrize(
    "tz",
    [
        pytest.param(pytz.timezone(BERLIN), id="pytz-object"),
        pytest.param(ZoneInfo(BERLIN), id="zoneinfo-object"),
        pytest.param(BERLIN, id="name"),
        pytest.param("UTC", id="utc-name"),
    ],
)
def test_the_clock_is_aware_so_the_host_cannot_decide_the_day(tz):
    """A naive "now" is how the host clock gets in.

    Every day boundary in the profile is derived from this one call; if it comes
    back naive, the grid is the host's and the setting was decoration.
    """
    now = _interface(tz)._LoadInterface__now()  # pylint: disable=protected-access
    assert now.tzinfo is not None
    assert now.utcoffset() is not None


def test_utc_is_a_zone_like_any_other():
    """``time_zone: UTC`` means UTC, not "whatever the host says".

    Treating it as "no zone" is the same bug mirrored: it would hand the day
    grid to a host running something else.
    """
    resolved = _interface("UTC").time_zone
    assert resolved is not None
    assert resolved.utcoffset(datetime(2026, 7, 1)) == timedelta(0)


def test_a_day_boundary_does_not_move_with_the_host_clock():
    """The regression in one assertion: same setting, same midnight.

    ``datetime.now(tz)`` is independent of the host's zone - which is exactly
    why the zone has to survive the constructor. The comparison is against a
    UTC reference rather than against a second run, so it holds on any host.
    """
    interface = _interface(pytz.timezone(BERLIN))
    now = interface._LoadInterface__now()  # pylint: disable=protected-access
    reference = datetime.now(timezone.utc).astimezone(ZoneInfo(BERLIN))
    assert abs((now - reference).total_seconds()) < 5
    assert now.utcoffset() == reference.utcoffset()


@pytest.mark.parametrize(
    "tz",
    [None, "Nonsense/Zone", 42, object()],
    ids=["none", "unknown", "int", "object"],
)
def test_anything_unusable_falls_back_to_local_time(tz):
    """Still a legitimate outcome - but only for input that is really unusable."""
    assert _interface(tz).time_zone is None
