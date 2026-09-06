"""
The model behind a load somebody else predicts.

This is issue #55's answer in code. EOS Connect does not know a heat pump's cooling mode,
a building's heating curve or which day the washing gets done - but the Home Assistant
instance next to it does, and it can already compute what those cost. So the model here
holds no physics at all: it takes what was pushed over HTTP or MQTT and hands it on.

Two shapes are supported, and which one a push carries decides how the framework treats
it:

- a **profile** is injected as given. Nothing schedules it, nothing gates it - the caller
  has already decided when the energy is drawn. Space heating and air conditioning belong
  here, which is why the built-in models for those can arrive later without changing
  anything around this class.
- a **contingent** is an amount of energy and a deadline. The planner places it and the
  gate emits a release signal, exactly as it would for a pool.
"""

import logging

from ..contribution import align_series
from ..injection import PushedContingent, PushedProfile
from .base import KIND_CONTINGENT, KIND_PROFILE, BaseDemandModel, EnergyDemand

logger = logging.getLogger("__main__")

REASON_NO_PUSH = "nothing pushed yet"
REASON_EXPIRED = "pushed data expired"
REASON_PUSHED = "pushed by an external system"


class ExternalPushModel(BaseDemandModel):
    """Holds the most recent push for one instance and replays it each cycle."""

    # Overridden per push; a fresh instance advertises whatever its preset configured,
    # so the UI can show the right fields before anything has ever been pushed.
    kind = KIND_PROFILE
    sensor_keys = ("power_sensor",)

    def __init__(self, entry_id, config):
        super().__init__(entry_id, config)
        self.kind = (
            KIND_CONTINGENT
            if str(config.get("type", "")).endswith("contingent")
            else KIND_PROFILE
        )
        self._push = None
        self._anchor = None
        self._time_frame_base = None

    def accept(self, parsed, anchor, time_frame_base):
        """
        Store a parsed push. Replaces whatever was there.

        Last-write-wins is the right rule for a forecast: a caller that pushes an updated
        heating profile means it to supersede the previous one, not to be added to it.
        """
        self._push = parsed
        self._anchor = anchor
        self._time_frame_base = time_frame_base
        self.kind = KIND_PROFILE if isinstance(parsed, PushedProfile) else KIND_CONTINGENT
        logger.debug("[LOADS] '%s' accepted a %s push", self.id, self.kind)

    def clear(self):
        """Drop the stored push - the DELETE endpoint and a failed re-push both use it."""
        self._push = None
        self._anchor = None

    def has_push(self):
        """Whether anything has been pushed to this instance yet."""
        return self._push is not None

    def demand(self, ctx):
        if self._push is None:
            return self._nothing(ctx, REASON_NO_PUSH)

        if ctx.now >= self._push.valid_until:
            # Expiring in the model as well as in the registry keeps the release gate
            # from holding a pump on against a forecast nobody is maintaining any more.
            return self._nothing(ctx, REASON_EXPIRED)

        if self._time_frame_base != ctx.time_frame_base:
            logger.warning(
                "[LOADS] '%s' was pushed for %ss slots but the optimizer now runs on "
                "%ss slots - ignoring it until it is pushed again",
                self.id, self._time_frame_base, ctx.time_frame_base,
            )
            return self._nothing(ctx, "pushed at a different slot resolution")

        if isinstance(self._push, PushedProfile):
            values = align_series(
                self._push.slots_wh, self._anchor, ctx.anchor,
                ctx.time_frame_base, ctx.slot_count,
            )
            return EnergyDemand(
                kind=KIND_PROFILE,
                profile_wh=values,
                feasible=[True] * ctx.slot_count,
                reason=REASON_PUSHED,
                detail={"total_wh": round(sum(values), 1)},
            )

        push: PushedContingent = self._push
        return EnergyDemand(
            kind=KIND_CONTINGENT,
            total_wh=push.total_wh,
            feasible=[True] * ctx.slot_count,
            max_power_w=float(self.config.get("rated_power_w", 0) or 0),
            deadline_slot=push.deadline_slot,
            reason=REASON_PUSHED,
            detail={"active": push.total_wh > 0},
        )

    def _nothing(self, ctx, reason):
        return EnergyDemand(
            kind=self.kind,
            total_wh=0.0,
            profile_wh=[0.0] * ctx.slot_count if self.kind == KIND_PROFILE else None,
            feasible=[True] * ctx.slot_count,
            max_power_w=float(self.config.get("rated_power_w", 0) or 0),
            reason=reason,
            detail={"active": False},
        )

    def status(self):
        if self._push is None:
            return {"pushed": False}
        return {
            "pushed": True,
            "kind": self.kind,
            "valid_until": self._push.valid_until.isoformat(),
            "anchor": self._anchor.isoformat() if self._anchor else None,
            "time_frame_base": self._time_frame_base,
        }
