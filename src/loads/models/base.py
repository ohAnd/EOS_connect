"""
What a managed load has to answer, and what it gets told in order to answer it.

A demand model converts sensor readings and forecasts into an `EnergyDemand`. It does
not know about MQTT, the optimizer, or how its energy will be placed in time - that is
the planner's and the gate's job. Keeping the model at this altitude is what lets a pool
heat pump, a sauna and a hot water tank share one implementation, and what lets a
space-heating model be added later without touching the framework.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("__main__")

# Kinds a model may return. Mirrors the constants in ``loads.contribution``; repeated
# here so a model module never has to import the registry.
KIND_CONTINGENT = "contingent"
KIND_PROFILE = "profile"


@dataclass
class DemandContext:
    """
    Everything a model is allowed to look at when asked for its demand.

    Passing this as one object rather than a dozen arguments keeps the model signature
    stable as the framework learns to supply more - a model that ignores a new field
    keeps working unchanged.

    Attributes:
        now: Aware local time the evaluation is for.
        anchor: Local midnight that slot 0 corresponds to.
        slot_count: Length of every per-slot series below.
        time_frame_base: Seconds per slot (900 or 3600).
        current_slot: Index of the slot ``now`` falls in, relative to ``anchor``.
        ambient_temp_c: Per-slot ambient temperature forecast, degrees Celsius.
        price_eur_per_wh: Per-slot grid purchase price.
        feed_in_eur_per_wh: Per-slot export price - the opportunity cost of using PV.
        pv_surplus_wh: Per-slot PV generation left after the base household load.
        readings: Current sensor values, keyed by the config field that named them.
        history: Recorded samples for calibration, oldest first.
    """

    now: object
    anchor: object
    slot_count: int
    time_frame_base: int
    current_slot: int
    ambient_temp_c: list = field(default_factory=list)
    # Where ambient_temp_c came from: "forecast", "sensor" or "fallback". A model that
    # reports its inputs lets a wrong answer be diagnosed from the page rather than
    # from the log - the flat-default bug hid behind a number that looked fine.
    ambient_source: str = ""
    # The two raw inputs behind ambient_temp_c, kept so the disagreement between them
    # can be recorded, learned from and audited later. Storing only the derived value
    # left the bias estimate unable to survive a restart.
    ambient_measured_c: object = None
    ambient_forecast_c: object = None
    price_eur_per_wh: list = field(default_factory=list)
    feed_in_eur_per_wh: list = field(default_factory=list)
    pv_surplus_wh: list = field(default_factory=list)
    readings: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

    def slots_per_hour(self):
        """How many slots make up an hour at the running resolution."""
        return 3600 // self.time_frame_base

    def hours_per_slot(self):
        """Fraction of an hour one slot covers - the W to Wh conversion factor."""
        return self.time_frame_base / 3600.0


@dataclass
class EnergyDemand:
    """
    A model's answer: how much energy is needed, and where it may go.

    For ``kind == "contingent"`` the framework owns the placement: ``total_wh`` is the
    energy still outstanding and ``feasible`` marks the slots the planner may use. For
    ``kind == "profile"`` the placement is already decided and ``profile_wh`` is injected
    as given - ``feasible`` and ``max_power_w`` are then ignored.

    Attributes:
        kind: ``contingent`` or ``profile``.
        total_wh: Contingent only. Electrical energy still needed over the horizon.
        profile_wh: Profile only. Per-slot electrical energy, already placed.
        feasible: Per-slot mask. False where the load must not run - outside its
            allowed window, below its minimum ambient temperature, out of season, or
            past its daily runtime cap.
        max_power_w: Electrical power draw while running. Bounds how much energy the
            planner may put into a single slot.
        deadline_slot: Contingent only. Last slot the energy may occupy, or None for
            "anywhere in the horizon".
        urgent: Skip price optimization and run as soon as feasible. Frost protection
            and a missed deadline both set this - being cheap is worthless if the pool
            freezes.
        reason: Short human-readable explanation, surfaced on MQTT and the dashboard.
        detail: Model-specific extras for the status API.
    """

    kind: str = KIND_CONTINGENT
    total_wh: float = 0.0
    profile_wh: list = None
    feasible: list = field(default_factory=list)
    max_power_w: float = 0.0
    deadline_slot: object = None
    urgent: bool = False
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def is_empty(self):
        """True when there is nothing to inject and nothing to schedule."""
        if self.kind == KIND_PROFILE:
            return not self.profile_wh or not any(self.profile_wh)
        return self.total_wh <= 0.0


class BaseDemandModel(ABC):
    """
    Interface every managed-load model implements.

    Subclasses are constructed with the entry's config dict and are expected to degrade
    rather than raise: a missing sensor means "no demand right now", not a traceback in
    the optimizer path. The framework calls `demand` once per cycle and `observe`
    whenever a fresh sample arrives.
    """

    #: Which kind of `EnergyDemand` this model produces.
    kind = KIND_CONTINGENT

    #: Config keys naming sensors the framework should read before calling `demand`.
    #: Values land in `DemandContext.readings` under the same key.
    sensor_keys = ()

    def __init__(self, entry_id, config):
        self.id = entry_id
        self.config = dict(config or {})

    def reconfigure(self, config):
        """
        Take an updated configuration without losing anything learned.

        Called when a hot-reloadable setting changes. Fields the schema marks
        restart-required - an id, a type, the physical dimensions a calibration is
        anchored to - never arrive here.
        """
        self.config = dict(config or {})

    @abstractmethod
    def demand(self, ctx):
        """Return an `EnergyDemand` for the horizon described by *ctx*."""

    def observe(self, sample):
        """
        Take one recorded sample for calibration. Default: ignore it.

        Models with nothing to learn - anything driven entirely by pushed data - inherit
        this and never think about it again.
        """

    def status(self):
        """Serializable model state for the REST API. Default: nothing to report."""
        return {}
