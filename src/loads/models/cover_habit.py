"""
When a store is usually covered, learned from when it actually was.

A pool cover changes the heat loss by roughly two thirds, and it goes on and off on a
daily rhythm - covered overnight, open while anyone is swimming. The model read the
cover switch once and assumed that state for the whole two-day horizon, which is wrong
for most of it: planning at nine in the evening, with the cover just pulled over, it
predicted a covered pool through the whole of the next afternoon.

Two sources answer that better than one reading, and they are good over different
distances:

- what the switch says **now** is reliable for the next hour or two and says little
  about tomorrow;
- what the household has **habitually** done at a given hour is a decent guess for
  tomorrow and worse than the sensor for right now.

So the near horizon takes the observation and the rest takes the habit. The habit is
learned from the samples already being recorded for calibration - nothing new to
configure, and nothing to keep in step with reality by hand.
"""

import logging

logger = logging.getLogger("__main__")

# How far ahead the current switch position is trusted over the learned pattern.
OBSERVED_HOURS = 2.0

# How quickly an old habit stops counting. A cover that starts being left off should
# show up within a week or so, not be averaged against a whole season.
HALF_LIFE_DAYS = 7.0

# Evidence an hour needs before its habit is used at all, in units of one observation at
# full weight. Below this the hour falls back to what the switch says now: a few days of
# agreement is a habit, one afternoon is not.
MIN_WEIGHT = 2.0

# Observations are folded in at this weight each. With the decay above, an hour seen
# once a day crosses MIN_WEIGHT on the third day - the second only reaches 1.9, because
# the first has already aged.
_OBSERVATION_WEIGHT = 1.0


class CoverHabit:
    """Per-hour probability that the store is covered, decayed towards recent days."""

    def __init__(self, half_life_days=HALF_LIFE_DAYS):
        self.half_life_days = float(half_life_days or HALF_LIFE_DAYS)
        self._covered = [0.0] * 24
        self._total = [0.0] * 24
        self._last_seen = None

    def observe(self, timestamp, covered):
        """Record that the store was, or was not, covered at this moment."""
        if timestamp is None:
            return False
        self._decay_to(timestamp)
        hour = timestamp.hour
        self._total[hour] += _OBSERVATION_WEIGHT
        if covered:
            self._covered[hour] += _OBSERVATION_WEIGHT
        self._last_seen = timestamp
        return True

    def _decay_to(self, timestamp):
        """
        Age every bucket forward to *timestamp*.

        Applied lazily on write rather than on a timer: the buckets are only ever read
        after a write, so the two are equivalent and this needs no clock of its own.
        """
        if self._last_seen is None:
            return
        elapsed_days = (timestamp - self._last_seen).total_seconds() / 86400.0
        if elapsed_days <= 0:
            return
        factor = 0.5 ** (elapsed_days / self.half_life_days)
        if factor >= 1.0:
            return
        self._covered = [value * factor for value in self._covered]
        self._total = [value * factor for value in self._total]

    def probability(self, hour):
        """
        How often the store is covered at this hour, or None when it cannot say.

        None is a real answer and the caller depends on it: an hour with no history
        should defer to the switch rather than be guessed at from the other 23.
        """
        index = int(hour) % 24
        if self._total[index] < MIN_WEIGHT:
            return None
        return max(0.0, min(1.0, self._covered[index] / self._total[index]))

    def hours_known(self):
        """How many of the 24 hours have enough evidence to speak for themselves."""
        return sum(1 for hour in range(24) if self.probability(hour) is not None)

    def state(self):
        """
        Serializable summary for the API and the overlay.

        The probabilities are included, not just the hours over a half. What is used is
        the fraction - an hour at 0.7 plans as seven tenths of a cover - and rounding
        that to a yes changed one installation's predicted loss by more than half while
        the reported state looked identical either way.
        """
        return {
            "hours_known": self.hours_known(),
            "covered_hours": [
                hour for hour in range(24) if (self.probability(hour) or 0.0) >= 0.5
            ],
            "probability_by_hour": [
                None if self.probability(hour) is None else round(self.probability(hour), 2)
                for hour in range(24)
            ],
        }

    def reset(self):
        """Forget the pattern - used when a calibration is reset."""
        self._covered = [0.0] * 24
        self._total = [0.0] * 24
        self._last_seen = None
