"""
Live parameters read from an EVCC site.

Battery capacity, current SoC and the SoC limits EVCC's battery management
carries are all in /api/state; EOS Connect needs the same numbers for its charge
model. Centralised here so the merger's EVCC auto-fill and any future consumer
read one implementation rather than each fetching and unwrapping its own copy of
the envelope (legacy {"result": ...} wrapping included).
"""

import logging
import time

import requests

logger = logging.getLogger("__main__")

# The config merge runs on every settings save, and the merge is what asks for
# these. Without a cache a save waited on an HTTP round trip - or on the whole
# timeout, when EVCC was down - before the page came back. Failures are cached
# too, for the same reason: an unreachable instance must cost one timeout, not
# one per save.
CACHE_SECONDS = 60
_cache: dict = {}


def clear_cache():
    """Forget every cached answer. For tests and for a changed EVCC address."""
    _cache.clear()


def fetch_evcc_parameters(url, request_timeout=5, max_age=CACHE_SECONDS):
    """
    Read the battery parameters EVCC exposes, or None when EVCC does not answer.

    Answers are cached per URL for *max_age* seconds, misses included.

    Returns:
        dict with the keys that were actually present:
        - capacity_wh: aggregated battery capacity in Wh (100%)
        - soc_percentage: current SoC (0-100)
        Missing keys are absent, not defaulted - the caller decides what a
        missing value is worth. None on any request failure.

        Deliberately absent: min/max SoC. EVCC's bufferSoc/prioritySoc look like
        SOC bounds but are charging-behaviour thresholds (when surplus stops
        being stored, what stays in reserve for the household) — mapping them
        onto the optimizer's physical battery range locked a site out of most
        of its own battery. The battery's real min/max SoC stay configured
        manually in EOS Connect.
    """
    base = str(url or "").rstrip("/")
    if not base or "://yourEVCCserver" in base:
        return None

    cached = _cache.get(base)
    if cached is not None and (time.monotonic() - cached[0]) < max_age:
        return cached[1]

    parameters = _read(base, request_timeout)
    _cache[base] = (time.monotonic(), parameters)
    return parameters


def _read(base, request_timeout):
    """One uncached read of ``/api/state``, unwrapped. None when it fails."""
    try:
        response = requests.get(f"{base}/api/state", timeout=request_timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.warning("[EVCC-PARAMS] Could not read %s/api/state: %s", base, e)
        return None

    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if not isinstance(payload, dict):
        return None

    battery = payload.get("battery")
    parameters = {}
    if isinstance(battery, dict):
        capacity_kwh = _positive(battery.get("capacity"))
        if capacity_kwh is not None:
            parameters["capacity_wh"] = round(capacity_kwh * 1000.0)
        soc = _bounded(battery.get("soc"), 0.0, 100.0)
        if soc is not None:
            parameters["soc_percentage"] = round(soc, 1)

    return parameters or None


def _positive(value):
    """A number above zero, or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _bounded(value, low, high):
    """A number inside [low, high], or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None
