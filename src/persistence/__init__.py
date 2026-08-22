"""
Persistence helpers for time-series data.

These live outside `config_web` on purpose: they store measurements rather than
configuration, and the interface layer must be able to use them without pulling in
the config web application (and with it Flask).
"""

from .pv_yield_store import PvYieldStore

__all__ = ["PvYieldStore"]
