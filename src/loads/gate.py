"""
Turning a plan into a released / not released signal.

The plan says which slots the load should run in. That is not yet a safe control output:
a plan is recomputed every optimizer cycle, and a slot that was cheapest at 12:00 may not
be at 12:15. Acting on that directly would toggle a compressor several times an hour,
which is how you destroy a heat pump while saving four cents.

So the gate sits between the plan and the outside world and owns the things a plan has no
concept of - how long a release has to be honoured once given, and a manual override that
outranks both.
"""

import logging
from datetime import timedelta

logger = logging.getLogger("__main__")

STATE_RELEASED = "released"
STATE_BLOCKED = "blocked"

OVERRIDE_RELEASE = "release"
OVERRIDE_BLOCK = "block"

# Reasons, surfaced verbatim on MQTT and the dashboard so a user can always answer
# "why is it running right now".
REASON_OVERRIDE = "manual override"
REASON_URGENT = "urgent - frost protection or missed deadline"
REASON_MIN_RUNTIME = "holding minimum runtime"
REASON_PLANNED = "planned cheap slot"
REASON_NOT_PLANNED = "not in a planned slot"
REASON_NO_DEMAND = "no demand"


class ReleaseGate:
    """
    Release decision for one managed load.

    The gate is deliberately stateful: whether the load may stop depends on when it
    started, which nothing else in the pipeline remembers.
    """

    def __init__(self, entry_id, min_runtime_minutes=0):
        self.id = entry_id
        self.min_runtime_minutes = max(0, int(min_runtime_minutes or 0))
        self.released = False
        self.released_since = None
        self._override_mode = None
        self._override_until = None
        self._last_reason = REASON_NO_DEMAND

    # -- override ---------------------------------------------------------------------

    def set_override(self, mode, minutes, now):
        """
        Force release or block for a while. ``mode=None`` clears it.

        An override always wins, including over the minimum runtime: it exists for the
        moments when the user knows something the model does not, and an override the
        model can veto is not an override.
        """
        if mode is None:
            self._override_mode = None
            self._override_until = None
            logger.info("[LOADS] '%s' override cleared", self.id)
            return
        if mode not in (OVERRIDE_RELEASE, OVERRIDE_BLOCK):
            raise ValueError(f"unknown override mode {mode!r}")
        span = max(1, int(minutes or 1))
        self._override_mode = mode
        self._override_until = now + timedelta(minutes=span)
        logger.info(
            "[LOADS] '%s' override set to '%s' for %d minutes", self.id, mode, span
        )

    def override_active(self, now):
        """True while an override is in force; expires itself on read."""
        if self._override_mode is None:
            return False
        if now >= self._override_until:
            logger.info("[LOADS] '%s' override expired", self.id)
            self._override_mode = None
            self._override_until = None
            return False
        return True

    # -- decision ---------------------------------------------------------------------

    def evaluate(self, planned, urgent, now, has_demand=True):
        """
        Decide whether the load is released right now.

        Args:
            planned: True when the current slot carries planned energy.
            urgent: True when price no longer matters.
            now: Aware local time.
            has_demand: False once the target is reached - it ends a minimum-runtime
                hold, because holding a pump on against a satisfied setpoint would
                overshoot the very target the hold exists to reach.

        Returns a dict with ``released``, ``state`` and ``reason``.
        """
        if self.override_active(now):
            released = self._override_mode == OVERRIDE_RELEASE
            return self._settle(released, REASON_OVERRIDE, now)

        if urgent and has_demand:
            return self._settle(True, REASON_URGENT, now)

        if self.released and has_demand and self._within_min_runtime(now):
            return self._settle(True, REASON_MIN_RUNTIME, now)

        if not has_demand:
            return self._settle(False, REASON_NO_DEMAND, now)

        if planned:
            return self._settle(True, REASON_PLANNED, now)

        return self._settle(False, REASON_NOT_PLANNED, now)

    def _within_min_runtime(self, now):
        if not self.min_runtime_minutes or self.released_since is None:
            return False
        elapsed = (now - self.released_since).total_seconds() / 60.0
        return elapsed < self.min_runtime_minutes

    def _settle(self, released, reason, now):
        if released and not self.released:
            self.released_since = now
            logger.info("[LOADS] '%s' released (%s)", self.id, reason)
        elif not released and self.released:
            held = 0.0
            if self.released_since is not None:
                held = (now - self.released_since).total_seconds() / 60.0
            self.released_since = None
            logger.info(
                "[LOADS] '%s' blocked after %.0f minutes (%s)", self.id, held, reason
            )

        self.released = released
        self._last_reason = reason
        return {
            "released": released,
            "state": STATE_RELEASED if released else STATE_BLOCKED,
            "reason": reason,
            "released_since": self.released_since.isoformat() if self.released_since else None,
            "override": self._override_mode,
            "override_until": self._override_until.isoformat() if self._override_until else None,
        }

    def status(self, now=None):
        """Current gate state without changing it."""
        return {
            "released": self.released,
            "state": STATE_RELEASED if self.released else STATE_BLOCKED,
            "reason": self._last_reason,
            "released_since": self.released_since.isoformat() if self.released_since else None,
            "min_runtime_minutes": self.min_runtime_minutes,
            "override": self._override_mode,
            "override_until": self._override_until.isoformat() if self._override_until else None,
        }
