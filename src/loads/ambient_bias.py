"""
How far a site sits from the weather model that forecasts it, hour by hour.

A regional forecast is right about the shape of tomorrow and often wrong about the level
here. On one installation the station read 4 K below every model tried - ICON-D2,
ICON-EU, ECMWF and GFS all agreed with each other and none of them with the garden -
which is the signature of cold air draining into a hollow overnight. Owners of real
thermometers report the same thing against phone weather apps routinely, so this is
ordinary rather than exotic.

The important part is that such a bias is **not constant through the day**. Cold air
pools at night and burns off by mid-morning, so one averaged offset splits the difference
and is wrong at both ends - and applied to a whole horizon it turns a 4 K night-time
correction into a 4 K correction on tomorrow's afternoon. Learning it per hour of the day
costs almost nothing and fixes both.

Nothing here reads a clock or a config: it is fed the measurement and the forecast that
was current at the time, and it answers with an offset for a given hour.
"""

import logging

logger = logging.getLogger("__main__")

# How quickly an old observation stops counting. A week means a change of season, or a
# sensor being moved, works its way in without one odd day mattering.
HALF_LIFE_DAYS = 7.0

# Evidence an hour needs before its own offset is used, in observations at full weight.
# Below it the hour falls back to the all-day average, and below that to no correction.
MIN_WEIGHT = 3.0

# The whole-day average needs rather more before it speaks for an unseen hour.
MIN_GLOBAL_WEIGHT = 12.0

# A correction beyond this is not a site effect. Generous, because it is now per hour
# rather than one number covering both a cold night and a warm afternoon - but still
# bounded, so a sensor reporting in the wrong unit cannot run away with the horizon.
MAX_OFFSET_K = 8.0

# Beyond this the two are not describing the same air at all, and the observation is
# rejected rather than blended in.
IMPLAUSIBLE_GAP_K = 20.0

_OBSERVATION_WEIGHT = 1.0


class AmbientBias:
    """Weighted mean of (measured - forecast) for each hour of the day."""

    def __init__(self, half_life_days=HALF_LIFE_DAYS):
        self.half_life_days = float(half_life_days or HALF_LIFE_DAYS)
        self._sum = [0.0] * 24
        self._weight = [0.0] * 24
        self._last_seen = None

    # -- ingest -----------------------------------------------------------------------

    def observe(self, timestamp, measured, forecast):
        """Record one disagreement between the site and the model that forecast it."""
        if timestamp is None or measured is None or forecast is None:
            return False
        try:
            gap = float(measured) - float(forecast)
        except (TypeError, ValueError):
            return False
        if abs(gap) > IMPLAUSIBLE_GAP_K:
            return False

        self._decay_to(timestamp)
        hour = timestamp.hour
        self._weight[hour] += _OBSERVATION_WEIGHT
        self._sum[hour] += _OBSERVATION_WEIGHT * gap
        self._last_seen = timestamp
        return True

    def _decay_to(self, timestamp):
        """Age every bucket forward. Applied on write, which is the only time it moves."""
        if self._last_seen is None:
            return
        elapsed_days = (timestamp - self._last_seen).total_seconds() / 86400.0
        if elapsed_days <= 0:
            return
        factor = 0.5 ** (elapsed_days / self.half_life_days)
        if factor >= 1.0:
            return
        self._sum = [value * factor for value in self._sum]
        self._weight = [value * factor for value in self._weight]

    # -- estimates --------------------------------------------------------------------

    def offset(self, hour):
        """
        The correction for this hour, in kelvin. 0.0 when there is nothing to go on.

        An hour with its own history uses it; an hour without falls back to the all-day
        average, which is still better than pretending the site matches the model; and
        with neither, no correction at all.
        """
        index = int(hour) % 24
        if self._weight[index] >= MIN_WEIGHT:
            return _clamp(self._sum[index] / self._weight[index])

        total_weight = sum(self._weight)
        if total_weight >= MIN_GLOBAL_WEIGHT:
            return _clamp(sum(self._sum) / total_weight)

        return 0.0

    def hours_known(self):
        """How many hours have enough history to speak for themselves."""
        return sum(1 for weight in self._weight if weight >= MIN_WEIGHT)

    def mean_offset(self):
        """The all-day average, for reporting. None when there is no history."""
        total_weight = sum(self._weight)
        if total_weight <= 0:
            return None
        return _clamp(sum(self._sum) / total_weight)

    def saturated_hours(self):
        """Hours whose learned offset is being held back by the bound."""
        return [
            hour for hour in range(24)
            if self._weight[hour] >= MIN_WEIGHT
            and abs(self._sum[hour] / self._weight[hour]) > MAX_OFFSET_K
        ]

    def state(self):
        """Serializable summary for the API and the overlay."""
        mean = self.mean_offset()
        return {
            "hours_known": self.hours_known(),
            "mean_offset_k": None if mean is None else round(mean, 2),
            "saturated_hours": self.saturated_hours(),
        }

    def reset(self):
        """Forget the pattern."""
        self._sum = [0.0] * 24
        self._weight = [0.0] * 24
        self._last_seen = None


def _clamp(value):
    return max(-MAX_OFFSET_K, min(MAX_OFFSET_K, value))
