"""
Managed loads - dynamic contributions to the household load forecast.

The household load profile `LoadInterface` builds from historic data models a *base*
load well and anything weather- or state-driven badly: a heat pump switched to cooling,
a cold snap, a pool heated to a target temperature. This package lets named contributors
add (or subtract) energy per time slot on top of that base profile, and - for loads whose
timing is ours to choose - decide when they are released to run.

Two kinds of contribution share one registry:

- **contingent** - "I need X Wh before deadline T, you pick when". A planner places the
  energy into the cheapest feasible slots and a release gate turns that plan into a
  released/blocked signal. Pool heat pump, sauna, hot water tank.
- **profile** - "this is what it *will* draw, I cannot move it". Injected as given, no
  planner, no gate. Space heating, air conditioning, anything pushed in from outside.

Both emit a `LoadContribution`, so everything downstream of the registry - the summing,
the injection into `gesamtlast` - is blind to which kind produced it.

This package deliberately imports neither `config_web` nor Flask: it is a sibling of
`interfaces` and `persistence` and receives plain dicts from the merged config. The only
Flask import lives in `loads.api`.
"""

from .contribution import (
    MAX_SLOT_WH,
    LoadContribution,
    LoadContributionRegistry,
    align_series,
)

__all__ = [
    "LoadContribution",
    "LoadContributionRegistry",
    "MAX_SLOT_WH",
    "align_series",
]
