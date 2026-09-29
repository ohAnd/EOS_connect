"""
Fetching a managed load's forecast from a source the user named.

Issue #55's original answer was a push: an external system that models a heat pump, a
heating curve or a washing day hands EOS Connect the result. That stays, and for a
sender with its own schedule it remains the right shape. But it is the only place in
EOS Connect where the user has to write an automation instead of naming an entity, and
what they usually already have is a template sensor holding the array.

So this is the pull half. It reads the same canonical timeseries format the price and
PV sources read - ``[{start, end, value}, ...]``, the shape EVCC publishes - through the
same normalizer, and hands the result to `loads.injection.profile_from_entries`.

Two things it deliberately does not do. It does not decide *when* to fetch: the managed
load manager owns that, on its own cycle. And it does not touch the slot grid: the
entries go back carrying their own timestamps, because `loads` cannot import this module
(the package is kept free of `interfaces`) and the alignment belongs on the other side
of that line anyway.

The array lives in an entity *attribute*, not its state. A Home Assistant state is
capped at 255 characters, which a 24-value hourly series only just fits and a 96-slot
quarter-hourly one does not.
"""

import logging

import requests

from .timeseries_normalizer import (
    PV_UNITS,
    TimeseriesFormatError,
    convert_load_values,
    detect_resolution_seconds,
    extract_json_path,
    load_plausibility_message,
    median_span_seconds,
    normalize_entries,
)

logger = logging.getLogger("__main__")

LABEL = "LOAD-PROFILE"

# How long to wait on the source. Short, because this runs inside the managed-load
# cycle: a hung request there delays every other load's plan, and the previous profile
# stays valid meanwhile.
REQUEST_TIMEOUT_S = 10


class ProfileSourceError(ValueError):
    """
    A profile could not be fetched or understood. The message is user-facing.

    Carries no text from an underlying exception - it is logged for the user and,
    later, rendered next to the field that caused it.
    """


def _endpoint(entry, data_source):
    """
    Resolve the URL and token for one entry.

    Built exactly as `merger._apply_central_ha_data_source` builds the price and PV
    ones, so that what is fetched here is what the config UI shows and what a probe
    would later test.
    """
    if entry.get("use_ha_central_data_source", True):
        base = str((data_source or {}).get("url", "") or "")
        sensor = str(entry.get("ha_sensor_name", "") or "").strip()
        if not base or not sensor:
            raise ProfileSourceError(
                "no entity configured - set the entity holding the load forecast"
            )
        return f"{base}/api/states/{sensor}", str(
            (data_source or {}).get("access_token", "") or ""
        ), sensor

    url = str(entry.get("data_url", "") or "").strip()
    if not url:
        raise ProfileSourceError(
            "no endpoint configured - set the URL the load forecast is served from"
        )
    return url, str(entry.get("data_token", "") or ""), url


def fetch_profile(entry, data_source=None, time_zone=None, ssl_ignore=False):
    """
    Fetch, validate and unit-convert one managed load's forecast.

    Args:
        entry: The resolved ``managed_loads`` entry. Reads ``use_ha_central_data_source``,
            ``ha_sensor_name`` or ``data_url``/``data_token``, ``data_path``,
            ``value_unit`` and ``rated_power_w``.
        data_source: The central ``data_source`` config section, for the shared URL and
            token.
        time_zone: Zone the source's naive timestamps belong to, if it sends any.
        ssl_ignore: Skip certificate verification, for a self-signed instance.

    Returns:
        dict: ``{"entries": [{"start": datetime, "end": datetime, "value": float}],
        "resolution_seconds": int}`` with values in Wh per entry - or None when the
        source returned nothing at all, which the manager treats as "keep what you
        have".

    Raises:
        ProfileSourceError: with a message written for the user.
    """
    url, token, label = _endpoint(entry, data_source)
    data_path = str(entry.get("data_path", "") or "attributes.data").strip()
    unit = str(entry.get("value_unit", "") or "W").strip()

    if unit not in PV_UNITS:
        raise ProfileSourceError(
            f"unknown unit '{unit}' - use one of: {', '.join(PV_UNITS)}"
        )

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT_S, verify=not ssl_ignore
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout as exc:
        raise ProfileSourceError(
            f"{label} did not answer within {REQUEST_TIMEOUT_S}s"
        ) from exc
    except requests.exceptions.RequestException as exc:
        # The reason is worth having in the log, but not in a message that may end up
        # on a page - a request exception can carry the token in the URL it echoes.
        logger.debug("[%s] request to %s failed", LABEL, url, exc_info=True)
        raise ProfileSourceError(f"{label} could not be reached") from exc
    except ValueError as exc:
        raise ProfileSourceError(f"{label} did not return JSON") from exc

    raw = extract_json_path(payload, data_path, label=LABEL)
    if raw is None:
        raise ProfileSourceError(
            f"nothing found at '{data_path}' in {label} - check where the array sits "
            "inside the entity"
        )
    if not isinstance(raw, list):
        raise ProfileSourceError(
            f"'{data_path}' in {label} holds a {type(raw).__name__}, not an array of "
            "{start, value} entries"
        )
    if not raw:
        # An empty array is a source that is up but has nothing to say yet - a template
        # sensor during a restart, say. Not an error, and not a reason to blank the
        # forecast either.
        logger.debug("[%s] %s returned an empty array", LABEL, label)
        return None

    try:
        entries = normalize_entries(raw, time_zone, label=LABEL)
        # The optimizer's own two resolutions first; any other grid the source chose
        # falls back to what its entries actually span. Unlike price and PV, a load
        # forecast is usually hand-built, so refusing anything but 15 or 60 minutes
        # would turn a working template sensor into a support question.
        resolution_seconds = (
            detect_resolution_seconds(entries) or median_span_seconds(entries)
        )
        if not resolution_seconds:
            raise ProfileSourceError(
                "could not work out how long each entry covers - send at least two "
                "entries, or give each one an 'end'"
            )
        convert_load_values(entries, unit)
    except TimeseriesFormatError as exc:
        # Written for the user by the normalizer, naming the keys the payload had.
        raise ProfileSourceError(str(exc)) from exc

    warning = load_plausibility_message(
        [entry_item["value"] for entry_item in entries],
        unit,
        resolution_seconds,
        _as_float(entry.get("rated_power_w")),
    )
    if warning:
        logger.warning(
            "[%s] '%s': %s | Config: #managed-loads",
            LABEL, entry.get("id", "?"), warning,
        )

    logger.debug(
        "[%s] '%s' fetched %d entries at %ds from %s",
        LABEL, entry.get("id", "?"), len(entries), resolution_seconds, label,
    )
    return {"entries": entries, "resolution_seconds": resolution_seconds}


def _as_float(value):
    """A configured number, or 0 when it is missing or unreadable."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
