"""
PvAutoscaler - compute timeframe scaling factors from historical PV yields

Provides a lightweight, testable implementation of the autoscaler engine
that reads hourly measured yields from `PvYieldStore`, computes per-timeframe
scale multipliers, and applies them to forecast arrays.

This module intentionally avoids starting background threads by default to
make unit testing and deterministic invocation easier. The calling code
can start the update service with `start_update_service()` when desired.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import math
from typing import Any, Dict, List, Optional

import requests
import pytz

from .state_source import fetch_remote_state

logger = logging.getLogger("__main__")

# The day is partitioned into timeframes by the hour they start at, each of which gets
# its own scale factor. The boundaries follow average PV delivery rather than a flat
# 6-hour grid: the two 4-hour blocks straddle solar noon, isolating the errors that
# dominate there (inverter AC clipping, midday over-forecast), while the 8-hour shoulders
# each absorb one complete sun ramp, where horizon, shading and roof asymmetry bite. On a
# flat grid 00-06 was night year-round and 18-24 only produced May-August, so half the
# factors stayed neutral. Night hours cost nothing here: a scale factor is
# sum(actual)/sum(forecast) over the block, and a dark hour adds 0 to both sides.
#
# This tuple is the only place the partitioning is defined. Nothing is persisted per
# timeframe - `local_hour` is stored and the timeframe derived on read - so changing
# these boundaries, or their number, needs no migration.
TIMEFRAME_START_HOURS = (0, 8, 12, 16)
TIMEFRAME_IDS = tuple(range(1, len(TIMEFRAME_START_HOURS) + 1))

# Upper bound on gap reconstruction. A longer outage than this is not worth
# back-filling: the evenly-distributed delta carries no real per-hour information
# and would otherwise generate thousands of rows from a single reading.
MAX_MISSED_HOURS = 48


def _day_origin(origins: set) -> str:
    """
    Reduce a day's row origins to one label for the UI.

    A day counts as measured as soon as one of its hours was recorded here, so a
    restored day that live collection has since topped up stops being labelled as
    seeded. Only a day made entirely of restored rows keeps their label.
    """
    if not origins or None in origins:
        return "measured"
    return "seeded" if "seeded" in origins else "imported"


def timeframe_for_hour(hour: int) -> int:
    """Return the timeframe id (1-based) that a local hour-of-day belongs to."""
    return sum(1 for start in TIMEFRAME_START_HOURS if int(hour) % 24 >= start)


def timeframe_end_hour(tf: int) -> int:
    """Return the exclusive end hour of a timeframe: the next block's start, or 24."""
    return TIMEFRAME_START_HOURS[tf] if tf < len(TIMEFRAME_START_HOURS) else 24


def timeframe_label(tf: int) -> str:
    """Render a timeframe as the wall-clock range it covers, e.g. "00:00 - 07:59"."""
    return f"{TIMEFRAME_START_HOURS[tf - 1]:02d}:00 - {timeframe_end_hour(tf) - 1:02d}:59"


def timeframe_bounds() -> List[Dict[str, Any]]:
    """Describe every timeframe for the API, so the UI never hardcodes the partitioning."""
    return [
        {
            "id": tf,
            "start": TIMEFRAME_START_HOURS[tf - 1],
            "end": timeframe_end_hour(tf),
            "label": timeframe_label(tf),
        }
        for tf in TIMEFRAME_IDS
    ]


class PvAutoscaler:
    """Compute and apply scaling factors to PV forecast data based on historical yields."""
    def __init__(
        self,
        config: Dict[str, Any],
        pv_yield_store,
        timezone=None,
        request_timeout: int = 10,
        ssl_ignore: bool = False,
        auto_start: bool = False,
    ) -> None:
        # Config values (with safe defaults)
        self.enabled: bool = bool(config.get("enabled", False))
        self.use_ha_central_data_source: bool = bool(
            config.get("use_ha_central_data_source", True)
        )
        self.sensor_entity_id: str = str(config.get("sensor_entity_id", ""))
        self.src: str = config.get("src", "homeassistant")
        self.url: str = str(config.get("url", ""))
        self.access_token: str = str(config.get("access_token", "")).strip()
        self.retention_days: int = int(config.get("retention_days", 7))
        self.min_scale_factor: float = float(config.get("min_scale_factor", 0.2))
        self.max_scale_factor: float = float(config.get("max_scale_factor", 2.5))
        self.min_data_hours_required: int = int(
            config.get("min_data_hours_required", 24)
        )

        # Dependencies
        self._pv_yield_store = pv_yield_store
        self._timezone = timezone  # Timezone string for local date calculations
        # Back-reference to PvInterface (populated by eos_connect wiring)
        self._pv_interface = None

        # Runtime state
        self._previous_counter_kwh: Optional[float] = None
        self._previous_counter_ts: Optional[str] = None
        self.last_collection: Optional[datetime] = None
        self.collection_interval = 3600  # seconds
        self.request_timeout = request_timeout
        self.ssl_ignore = ssl_ignore

        # Last collection failure, surfaced through get_status() so the UI can tell a
        # stalled collector (bad sensor id, bad token, unreachable host) apart from a
        # fresh install that simply has no data yet.
        self._last_error: Optional[str] = None
        self._last_error_ts: Optional[str] = None
        self._consecutive_failures: int = 0

        # Cached scale factors, one per timeframe
        self._scale_factors: Dict[int, float] = {tf: 1.0 for tf in TIMEFRAME_IDS}

        # Background thread control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Guards start/stop so a hot-reload toggle cannot race the collect loop.
        self._thread_lock = threading.Lock()

        self._validate_scale_bounds()
        self._complete_init(auto_start)

    def _validate_scale_bounds(self) -> None:
        """Swap inverted clamp bounds so scaling cannot be pinned to a single value."""
        if self.min_scale_factor > self.max_scale_factor:
            logger.warning(
                "[PV-AUTO] min_scale_factor (%s) exceeds max_scale_factor (%s) - swapping",
                self.min_scale_factor,
                self.max_scale_factor,
            )
            self.min_scale_factor, self.max_scale_factor = (
                self.max_scale_factor,
                self.min_scale_factor,
            )

    def _tz(self):
        """
        Resolve the configured timezone to a tzinfo object.

        Returns None when no timezone is configured or the name is unusable, in which
        case callers fall back to system local time.
        """
        if not self._timezone:
            return None
        try:
            if isinstance(self._timezone, str):
                return pytz.timezone(self._timezone)
            return self._timezone
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError) as exc:
            logger.warning("[PV-AUTO] Unusable timezone %r: %s", self._timezone, exc)
            return None

    def _local_now(self) -> datetime:
        """Current wall-clock time in the configured timezone (system local as fallback)."""
        tz = self._tz()
        if tz is None:
            return datetime.now()
        return datetime.now(timezone.utc).astimezone(tz)

    def _get_local_now(self) -> datetime:
        """Current local time rounded down to the hour boundary."""
        return self._local_now().replace(minute=0, second=0, microsecond=0)

    def _complete_init(self, auto_start: bool) -> None:
        """Seed runtime state from the store and optionally start collecting."""
        # Seed previous counter from store if available
        try:
            latest = self._pv_yield_store.get_latest_record()
            if latest and latest.get("real_counter_kwh") is not None:
                self._previous_counter_kwh = float(latest.get("real_counter_kwh"))
                self._previous_counter_ts = latest.get("timestamp")
        except (AttributeError, TypeError, ValueError, KeyError):
            logger.exception("[PV-AUTO] Could not seed previous counter from DB")

        # Recompute factors from stored history immediately. Without this the first
        # optimization run after every restart uses neutral factors until the next
        # hour boundary, even when a full history is already on disk.
        try:
            self.compute_timeframe_scaling_factors()
        except (AttributeError, TypeError, ValueError, KeyError):
            logger.exception("[PV-AUTO] Could not seed scale factors from DB")

        # Log initialization
        token_status = "SET" if self.access_token else "NOT SET"
        logger.info(
            "[PV-AUTO] Initialized: enabled=%s, sensor=%s, url=%s, src=%s, token=%s, factors=%s",
            self.enabled,
            self.sensor_entity_id,
            self.url,
            self.src,
            token_status,
            self._scale_factors,
        )

        if auto_start and self.enabled:
            self.start_update_service()

    # --------------------------- Public accessors ---------------------------
    def get_scale_factors(self) -> Dict[int, float]:
        """Return a copy of the currently cached timeframe scale factors."""
        return self._scale_factors.copy()

    def update_config(self, **changes: Any) -> None:
        """
        Apply configuration changes to the running autoscaler.

        Starts or stops the collection thread when `enabled` changes, and recomputes
        the cached factors when a change affects them - otherwise a new clamp or
        retention window would not take effect until the next hourly insert.
        """
        recompute_keys = {
            "retention_days",
            "min_scale_factor",
            "max_scale_factor",
            "min_data_hours_required",
        }
        touched_recompute = False

        for key, value in changes.items():
            if not hasattr(self, key):
                logger.warning("[PV-AUTO] Ignoring unknown config key %r", key)
                continue
            setattr(self, key, value)
            if key in recompute_keys:
                touched_recompute = True

        if "min_scale_factor" in changes or "max_scale_factor" in changes:
            self._validate_scale_bounds()

        if "enabled" in changes:
            if self.enabled:
                self.start_update_service()
            else:
                self.stop_update_service()

        if touched_recompute:
            try:
                self.compute_timeframe_scaling_factors()
            except (AttributeError, TypeError, ValueError, KeyError):
                logger.exception("[PV-AUTO] Failed to recompute factors after config change")

    def _record_failure(self, message: str) -> None:
        """Store a collection failure so the UI can distinguish stalled from starting."""
        self._last_error = message
        self._last_error_ts = datetime.now(timezone.utc).isoformat()
        self._consecutive_failures += 1

    def _clear_failure(self) -> None:
        """Clear the stored collection failure after a successful reading."""
        self._last_error = None
        self._last_error_ts = None
        self._consecutive_failures = 0

    # --------------------------- Remote fetch ---------------------------
    def __fetch_remote_state(self, source: str, sensor: str) -> str:
        """Fetch the cumulative PV counter state from Home Assistant or openHAB."""
        return fetch_remote_state(
            source,
            sensor,
            url=self.url,
            access_token=self.access_token,
            request_timeout=self.request_timeout,
            ssl_ignore=self.ssl_ignore,
        )

    # ------------------------ Collection / Update ------------------------
    def should_collect(self) -> bool:
        """
        Check if it's time to collect (when entering a new hour).

        Ensures collection happens once per hour when the hour changes in local timezone.
        The actual collection timestamp will always be rounded to the hour boundary,
        so real generation delta from 8:00-9:00 is stored with timestamp 9:00:00 (not 9:42:53).
        """
        if not self.enabled:
            return False

        # Calculate the current hour boundary (this hour's start)
        current_hour_boundary = self._local_now().replace(minute=0, second=0, microsecond=0)

        # If we haven't collected yet, do it now
        if self.last_collection is None:
            return True

        try:
            # Convert last_collection (UTC) to local time for comparison
            last_collect_utc = self.last_collection
            if last_collect_utc.tzinfo is None:
                last_collect_utc = last_collect_utc.replace(tzinfo=timezone.utc)

            tz = self._tz()
            last_collect_local = (
                last_collect_utc.astimezone(tz) if tz else last_collect_utc.astimezone()
            )

            # Get the hour boundary of last collection
            last_collect_hour = last_collect_local.replace(minute=0, second=0, microsecond=0)

            # If last collection was in a different hour, collect now (new hour has started)
            return last_collect_hour < current_hour_boundary
        except (ValueError, TypeError, AttributeError):
            logger.warning("[PV-AUTO] Error checking hour boundary, using fallback timing")
            # Fallback to interval-based collection
            last = self.last_collection
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            time_since = (datetime.now(timezone.utc) - last).total_seconds()
            return time_since >= self.collection_interval

    def collect_if_needed(self) -> bool:
        """Collect PV data if needed based on the current time and collection interval.

        Returns:
            bool: True if data was collected, False otherwise.
        """
        if not self.should_collect():
            return False

        # Round to hour boundary. Collection runs when the hour changes (e.g. at 09:00
        # local), so the boundary marks the END of the hour whose yield we record.
        tz = self._tz()
        now_boundary = self._local_now().replace(minute=0, second=0, microsecond=0)
        if now_boundary.tzinfo is None and tz is not None:
            try:
                now_boundary = tz.localize(now_boundary)
            except (ValueError, AttributeError):
                pass

        # The measured delta belongs to the PREVIOUS hour. Step back through the
        # timezone rather than through wall-clock arithmetic: pytz keeps the stale UTC
        # offset after a timedelta unless the result is normalized, which would put the
        # stored period an hour out across a DST transition.
        period_start_local = now_boundary - timedelta(hours=1)
        if tz is not None and hasattr(tz, "normalize"):
            try:
                period_start_local = tz.normalize(period_start_local)
            except (ValueError, AttributeError):
                pass

        try:
            utc_now_boundary = now_boundary.astimezone(timezone.utc)
        except (ValueError, TypeError):
            utc_now_boundary = now_boundary.replace(tzinfo=timezone.utc)
        try:
            utc_period_start = period_start_local.astimezone(timezone.utc)
        except (ValueError, TypeError):
            utc_period_start = period_start_local.replace(tzinfo=timezone.utc)

        # Local date/hour of the recorded period, read off the normalized datetime so a
        # repeated or skipped hour lands on the hour that actually occurred.
        prev_hour_local = period_start_local.hour
        prev_date_local = period_start_local.strftime("%Y-%m-%d")

        # UTC offset of the recorded period (DST-aware). This is what tells the two
        # 02:00 hours of the autumn transition apart.
        try:
            local_offset_minutes = int(period_start_local.utcoffset().total_seconds() / 60)
        except (ValueError, TypeError, AttributeError):
            local_offset_minutes = None

        # PV and autoscaler services start concurrently. Do not write a real yield row
        # until the live forecast array covers a full local day. Providers publish a
        # 48-hour horizon, but the built-in default curve is only one day long, so
        # requiring the full horizon here would make the feature a permanent no-op for
        # `source: default` and for any window where a provider has fallen back to it.
        #
        # This runs before the counter fetch on purpose: it is a local check, so a
        # not-yet-ready forecast costs no HTTP request, and leaving `last_collection`
        # untouched lets the loop collect as soon as the forecast lands rather than
        # skipping the rest of the hour.
        try:
            time_frame_base = int(getattr(self._pv_interface, "time_frame_base", 3600))
        except (TypeError, ValueError):
            time_frame_base = 3600
        slots_per_hour = max(1, 3600 // time_frame_base) if time_frame_base else 1
        expected_slots = 24 * slots_per_hour
        # Use scale=False to get the ORIGINAL (unscaled) forecast values for database storage.
        # This ensures we store the raw forecast before autoscaling is applied, which is
        # needed to calculate accurate scale factors by comparing real data with original forecast.
        current_forecast = None
        if self._pv_interface is not None:
            getter = getattr(self._pv_interface, "get_current_pv_forecast", None)
            if callable(getter):
                current_forecast = getter(scale=False)
        if not isinstance(current_forecast, list) or len(current_forecast) < expected_slots:
            logger.info(
                "[PV-AUTO] Forecast not ready; delaying collection "
                "(have %s slots, need %d)",
                len(current_forecast) if isinstance(current_forecast, list) else 0,
                expected_slots,
            )
            return False

        try:
            raw_state = self.__fetch_remote_state(self.src, self.sensor_entity_id)
            # State may contain units; take first token
            cleaned = raw_state.split()[0]
            current_counter = float(cleaned)
        except (requests.exceptions.RequestException, ValueError, IndexError) as exc:
            # Include more diagnostic info on error
            token_status = "configured" if self.access_token else "NOT SET"
            logger.warning(
                "[PV-AUTO] Error fetching counter from %s://%s (token: %s): %s | sensor=%s",
                self.src,
                self.url,
                token_status,
                exc,
                self.sensor_entity_id,
            )
            # The full diagnostic - source, url, token status and the exception - is in
            # the log line above. This message is served by
            # GET /api/pv_autoscaling/status, so it names the sensor and stops there.
            self._record_failure(
                f"Cannot read {self.sensor_entity_id or '<no sensor>'} "
                "- see the log for details"
            )
            self.last_collection = datetime.now(timezone.utc)
            return False

        self._clear_failure()

        real_delta_kwh: Optional[float] = None
        if self._previous_counter_kwh is None:
            # First valid reading seeds baseline but does not produce a delta
            self._previous_counter_kwh = current_counter
            self._previous_counter_ts = period_start_local.isoformat()
            self.last_collection = datetime.now(timezone.utc)
            logger.info("[PV-AUTO] Seeded previous counter: %s kWh", current_counter)
            return False

        # Read the full-day forecast array. The array is aligned to local midnight and contains
        # Wh per configured slot (hourly or 15-minute). Using scale=False ensures we get the
        # original forecast values, not the autoscaled ones.
        # The array is aligned to local midnight and holds Wh per configured slot. Its
        # length cannot identify the resolution (48 values is two hourly days, not one
        # 15-minute day), so slot width comes from the interface resolution above.
        forecast_kwh = None
        try:
            # Get forecast for PREVIOUS hour (delta represents generation during it)
            idx_start = (prev_hour_local % 24) * slots_per_hour
            hour_slots = current_forecast[idx_start : idx_start + slots_per_hour]
            if hour_slots:
                # Sum source Wh values, then normalize the stored value to kWh.
                total_kwh = sum(float(x) for x in hour_slots) / 1000.0
                logger.debug(
                    "[PV-AUTO] Forecast trace: target=%s %02d:00, "
                    "source=current_pv_forecast, resolution=%ss, indices=%d:%d, "
                    "slots_wh=%s, total_kwh=%.4f",
                    prev_date_local,
                    prev_hour_local,
                    time_frame_base,
                    idx_start,
                    idx_start + len(hour_slots),
                    hour_slots,
                    total_kwh,
                )
                # Reject an implausible 0.0 forecast for a daytime hour: it means the
                # provider returned incomplete data rather than that no sun was
                # predicted. Storing NULL keeps the day out of the ratio entirely.
                # 6/18 is a "could the sun be up?" bracket, deliberately wider than any
                # real sunrise/sunset - unrelated to TIMEFRAME_START_HOURS, which it
                # merely used to coincide with. Do not sync the two.
                if total_kwh > 0 or prev_hour_local < 6 or prev_hour_local >= 18:
                    forecast_kwh = float(total_kwh)
        except (TypeError, ValueError, IndexError, AttributeError):
            logger.exception("[PV-AUTO] Error reading raw forecast from PvInterface")

        # Calculate delta
        prev_counter_kwh = self._previous_counter_kwh
        delta = current_counter - prev_counter_kwh
        if delta < 0:
            # Counter reset or replacement
            logger.warning(
                "[PV-AUTO] Counter decreased (reset detected). Previous=%s current=%s",
                prev_counter_kwh,
                current_counter,
            )
            real_delta_kwh = 0.0
            # Reset baseline to current reading to avoid repeated negatives
            self._previous_counter_kwh = current_counter
            # Record for PREVIOUS hour (delta represents generation during that
            # hour)
            try:
                self._pv_yield_store.insert_hourly_record(
                    # Period start timestamp (e.g., 8:00:00 UTC for 8-9 period)
                    timestamp=utc_period_start.isoformat(),
                    date=prev_date_local,  # Local date for previous hour
                    hour=prev_hour_local,  # Local hour (when delta occurred)
                    real_counter_kwh=self._previous_counter_kwh,
                    real_delta_kwh=real_delta_kwh,
                    forecast_kwh=forecast_kwh,
                    local_date=prev_date_local,  # Timezone-aware local date
                    local_hour=prev_hour_local,  # Timezone-aware local hour
                    # (when delta occurred)
                    local_offset_minutes=local_offset_minutes,  # UTC offset for DST
                )
                self._pv_yield_store.purge_old_records(self.retention_days)
            except (TypeError, ValueError, KeyError):
                logger.exception("[PV-AUTO] Error writing pv_yield_history record after reset")
            self._previous_counter_ts = utc_period_start.isoformat()
            self.last_collection = datetime.now(timezone.utc)
            return True
        else:
            real_delta_kwh = float(delta)

        # Missed-poll handling: if last stored DB timestamp is older than 1 hour,
        # distribute the observed delta across the missed hours. This creates
        # historical rows for the missing hours with evenly divided deltas and
        # approximate per-hour forecast if snapshot is available.
        missed_hours = 1
        last_db_ts = None
        try:
            latest_db = self._pv_yield_store.get_latest_record()
            if latest_db and latest_db.get("timestamp"):
                try:
                    last_db_ts = datetime.fromisoformat(latest_db.get("timestamp"))
                    if last_db_ts.tzinfo is None:
                        # assume UTC
                        last_db_ts = last_db_ts.replace(tzinfo=timezone.utc)
                    # Calculate difference using boundary time for consistent
                    # hour counting
                    last_period_end = last_db_ts + timedelta(hours=1)  # End of last recorded period
                    diff_seconds = (utc_now_boundary - last_period_end).total_seconds()
                    missed_hours = int(max(1, math.ceil(diff_seconds / 3600.0)))
                except (ValueError, TypeError, AttributeError):
                    last_db_ts = None
                    missed_hours = 1
        except (TypeError, ValueError):
            missed_hours = 1

        if missed_hours > MAX_MISSED_HOURS:
            logger.warning(
                "[PV-AUTO] Gap of %d hours exceeds the %d hour reconstruction limit - "
                "recording the delta against the previous hour only",
                missed_hours,
                MAX_MISSED_HOURS,
            )
            missed_hours = 1

        # Insert record(s) and purge old rows. If we missed multiple hours, create
        # historical rows distributing the measured delta evenly across the hours.
        try:
            if missed_hours <= 1:
                # Single-hour case: attribute full delta to previous hour (when delta occurred)
                self._pv_yield_store.insert_hourly_record(
                    # Period start timestamp (e.g., 8:00:00 UTC for 8-9 period)
                    timestamp=utc_period_start.isoformat(),
                    date=prev_date_local,  # Local date for previous hour
                    hour=prev_hour_local,  # Local hour (when delta occurred)
                    real_counter_kwh=current_counter,
                    real_delta_kwh=real_delta_kwh,
                    forecast_kwh=forecast_kwh,
                    local_date=prev_date_local,  # Timezone-aware local date
                    local_hour=prev_hour_local,  # Timezone-aware local hour
                    # (when delta occurred)
                    local_offset_minutes=local_offset_minutes,  # UTC offset for DST
                )
            else:
                # Distribute across missed_hours using latest DB timestamp as anchor
                # Convert to local timezone for each hour to get correct date/hour values
                anchor_utc_ts = last_db_ts if last_db_ts is not None else utc_now_boundary

                per_hour = (
                    real_delta_kwh / float(missed_hours)
                    if missed_hours > 0
                    else real_delta_kwh
                )
                base_counter = prev_counter_kwh
                for i in range(1, missed_hours + 1):
                    # Calculate timestamp for this hour (in UTC)
                    # Note: We use anchor as base, but for current collection hour use actual time
                    if i == missed_hours:
                        utc_ts_i = utc_period_start
                    else:
                        utc_ts_i = anchor_utc_ts + timedelta(hours=i)

                    # Convert to local timezone to get correct date/hour
                    try:
                        local_ts_i = utc_ts_i.astimezone(tz) if tz else utc_ts_i.astimezone()
                    except (ValueError, TypeError):
                        local_ts_i = utc_ts_i.astimezone()

                    local_hour_i = local_ts_i.hour
                    local_date_i = local_ts_i.strftime("%Y-%m-%d")

                    # Calculate UTC offset for this hour (DST-aware)
                    try:
                        local_offset_minutes_i = int(
                            local_ts_i.utcoffset().total_seconds() / 60
                        )
                    except (ValueError, TypeError, AttributeError):
                        local_offset_minutes_i = None

                    # For all missed hours, use None since we don't have historical
                    # forecast snapshots from those times. The forecast_kwh fetched earlier
                    # is for the current previous hour (relative to now), not for the historical
                    # hours being reconstructed.
                    forecast_kwh_i = None

                    real_counter_kwh_i = float(base_counter + per_hour * i)
                    real_delta_kwh_i = float(per_hour)
                    self._pv_yield_store.insert_hourly_record(
                        # Keep UTC timestamp for audit
                        timestamp=utc_ts_i.isoformat(),
                        date=local_date_i,  # Local date
                        hour=local_hour_i,  # Local hour
                        real_counter_kwh=real_counter_kwh_i,
                        real_delta_kwh=real_delta_kwh_i,
                        forecast_kwh=forecast_kwh_i,
                        local_date=local_date_i,  # Timezone-aware local date
                        local_hour=local_hour_i,  # Timezone-aware local hour
                        local_offset_minutes=local_offset_minutes_i,
                        # UTC offset for DST tracking
                    )

            self._pv_yield_store.purge_old_records(self.retention_days)
            # Recompute cached scale factors after successful inserts
            try:
                self.compute_timeframe_scaling_factors()
            except (TypeError, ValueError, KeyError):
                logger.exception(
                    "[PV-AUTO] Failed to compute timeframe scale factors after insert"
                )
        except (TypeError, ValueError, KeyError):
            logger.exception("[PV-AUTO] Error writing pv_yield_history record")

        # Update baseline counter after successful insertion(s)
        try:
            self._previous_counter_kwh = current_counter
            self._previous_counter_ts = utc_period_start.isoformat()
        except (TypeError, ValueError):
            pass

        self.last_collection = datetime.now(timezone.utc)
        return True

    def set_pv_interface(self, pv_interface) -> None:
        """Attach the `PvInterface` instance so the autoscaler can read raw forecasts."""
        self._pv_interface = pv_interface

    def start_update_service(self) -> None:
        """Start the background update service thread if not already running."""
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return

            self._stop_event.clear()

            def run_loop() -> None:
                while not self._stop_event.is_set():
                    try:
                        self.collect_if_needed()
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.exception("[PV-AUTO] Exception in collect loop")
                    # wake up every 60s to check
                    self._stop_event.wait(60)

            self._thread = threading.Thread(target=run_loop, daemon=True)
            self._thread.start()
            logger.info("[PV-AUTO] Update service started")

    def stop_update_service(self) -> None:
        """Stop the background update service thread if running."""
        with self._thread_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
            self._thread = None
        # Join outside the lock: the loop body may itself be calling back into the
        # autoscaler, and holding the lock here would deadlock a concurrent start.
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        logger.info("[PV-AUTO] Update service stopped")

    def is_running(self) -> bool:
        """Return True while the background collection thread is alive."""
        thread = self._thread
        return bool(thread and thread.is_alive())

    # --------------------- Scaling calculation ---------------------
    def _today_local_iso(self) -> str:
        """Today's date in the configured timezone, as an ISO date string."""
        return self._local_now().date().isoformat()

    @staticmethod
    def _normalize_row(row) -> Optional[Dict[str, Any]]:
        """
        Normalize a store row (dict or tuple) into the fields the aggregations need.

        Prefers the timezone-aware `local_date`/`local_hour` columns, falling back to
        the legacy `date`/`hour` pair - rows written before those columns were added
        have no local values, and reading them as NULL would group a real day under a
        missing date.
        """
        if isinstance(row, dict):
            date = row.get("local_date") or row.get("date")
            hour = row.get("local_hour")
            if hour is None:
                hour = row.get("hour")
            real_delta = row.get("real_delta_kwh")
            forecast_kwh = row.get("forecast_kwh")
            origin = row.get("origin")
        else:
            # row tuple: id, timestamp, date, hour, real_counter_kwh, real_delta_kwh,
            # forecast_kwh, created_at, local_date, local_hour, local_offset_minutes,
            # origin
            date = row[8] if len(row) > 8 and row[8] else row[2]
            hour = row[9] if len(row) > 9 and row[9] is not None else row[3]
            real_delta = row[5]
            forecast_kwh = row[6]
            origin = row[11] if len(row) > 11 else None

        if date is None:
            return None

        # The timeframe is always derived, never stored: that keeps the partitioning a
        # runtime concern, so re-cutting the boundaries needs no data migration. A row
        # whose hour is unusable cannot be placed in any block, and scoring it under an
        # arbitrary one would corrupt that block's factor - drop it instead.
        try:
            tf = timeframe_for_hour(hour)
        except (TypeError, ValueError):
            return None

        return {
            "date": date,
            "hour": hour,
            "timeframe": tf,
            "real_delta_kwh": real_delta,
            "forecast_kwh": forecast_kwh,
            # NULL for hours measured here; "imported" or "seeded" for a restored
            # backup, which the UI must label rather than pass off as measured.
            "origin": origin,
        }

    def _aggregate_by_day_and_timeframe(self, rows, keep_date, count_hours=False):
        """
        Sum actual and forecast kWh per (date, timeframe) for the rows `keep_date` accepts.

        A timeframe is present in a day's dict only when at least one row contributed a
        non-NULL value, so callers can tell "recorded as zero" apart from "not recorded".

        Also returns the set of row origins seen per day, so a day restored from a
        backup is not presented as one this system measured.
        """
        actual: Dict[str, Dict[int, float]] = defaultdict(dict)
        forecast: Dict[str, Dict[int, float]] = defaultdict(dict)
        hours: Dict[str, set] = defaultdict(set)
        origins: Dict[str, set] = defaultdict(set)
        recorded_hours = 0

        for raw in rows:
            row = self._normalize_row(raw)
            if row is None or not keep_date(row["date"]):
                continue
            date, tf = row["date"], row["timeframe"]
            origins[date].add(row["origin"])
            if row["real_delta_kwh"] is not None:
                bucket = actual[date]
                bucket[tf] = bucket.get(tf, 0.0) + float(row["real_delta_kwh"])
                recorded_hours += 1
                if count_hours and row["hour"] is not None:
                    try:
                        hours[date].add(int(row["hour"]))
                    except (TypeError, ValueError):
                        pass
            if row["forecast_kwh"] is not None:
                bucket = forecast[date]
                bucket[tf] = bucket.get(tf, 0.0) + float(row["forecast_kwh"])

        return actual, forecast, hours, recorded_hours, origins

    def _set_scale_factors(self, factors: Dict[int, float]) -> None:
        """
        Cache new factors, re-deriving the scaled forecast when they actually changed.

        The scaled array is otherwise only rebuilt on a provider fetch - up to 15
        minutes apart, or 2.5 hours on Solcast - so without this both EOS and the UI
        keep receiving a forecast multiplied by superseded factors. That is most
        visible on a fresh install: the hour count drops back under
        `min_data_hours_required` at the day rollover, the factors reset to neutral,
        and the still-scaled array reports a correction the UI says is not applied.
        """
        changed = factors != self._scale_factors
        self._scale_factors = factors
        if not changed:
            return
        if not hasattr(self._pv_interface, "refresh_scaled_forecast"):
            return
        try:
            self._pv_interface.refresh_scaled_forecast()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("[PV-AUTO] Could not refresh scaled forecast after factor change")

    def compute_timeframe_scaling_factors(self) -> Dict[int, float]:
        """Compute a scale factor per timeframe from the retained history."""
        rows = self._pv_yield_store.get_history_last_n_days(self.retention_days)
        if not rows:
            return self._scale_factors.copy()

        # Today is still in progress, so it is collected but never scored.
        today_date = self._today_local_iso()
        per_day_actual, per_day_forecast, _, recorded_hours, _origins = (
            self._aggregate_by_day_and_timeframe(rows, lambda d: d != today_date)
        )

        if recorded_hours < self.min_data_hours_required:
            logger.info(
                "[PV-AUTO] Not enough recorded hours (%d < %d). Using neutral factors",
                recorded_hours,
                self.min_data_hours_required,
            )
            neutral = {tf: 1.0 for tf in TIMEFRAME_IDS}
            self._set_scale_factors(neutral)
            return neutral.copy()

        # For each timeframe compute average across days that have data for that timeframe
        totals_actual: Dict[int, float] = defaultdict(float)
        totals_forecast: Dict[int, float] = defaultdict(float)
        days_count: Dict[int, int] = defaultdict(int)

        for date in per_day_actual.keys() | per_day_forecast.keys():
            for tf in TIMEFRAME_IDS:
                actual = per_day_actual.get(date, {}).get(tf)
                forecast = per_day_forecast.get(date, {}).get(tf)
                # A day enters the ratio only when BOTH sides were recorded. Admitting a
                # day whose forecast is missing would add yield to the numerator with
                # nothing in the denominator, inflating the factor exactly when
                # collection has been failing.
                if actual is None or forecast is None:
                    continue
                totals_actual[tf] += actual
                totals_forecast[tf] += forecast
                days_count[tf] += 1

        scale_factors: Dict[int, float] = {}
        for tf in TIMEFRAME_IDS:
            days = days_count.get(tf, 0)
            if days == 0:
                scale_factors[tf] = 1.0
                continue
            avg_actual = totals_actual[tf] / days
            avg_forecast = totals_forecast[tf] / days
            # Below this the timeframe is effectively night: the ratio would be noise
            # amplified by a near-zero denominator.
            if avg_forecast < 0.05:
                scale = 1.0
            else:
                scale = avg_actual / avg_forecast
            scale = min(max(scale, self.min_scale_factor), self.max_scale_factor)
            scale_factors[tf] = round(scale, 3)

        self._set_scale_factors(scale_factors)
        logger.info(
            "[PV-AUTO] Computed scale factors: %s (hours=%d)", scale_factors, recorded_hours
        )
        return scale_factors.copy()

    def get_todays_partial_data(self) -> Dict[str, Any]:
        """Extract today's partial data (not used in scaling, informational only)."""
        rows = self._pv_yield_store.get_history_last_n_days(self.retention_days)
        if not rows:
            return {}

        today_date = self._today_local_iso()
        per_day_actual, per_day_forecast, hours, _, _origins = (
            self._aggregate_by_day_and_timeframe(rows, lambda d: d == today_date, count_hours=True)
        )

        hours_collected = hours.get(today_date, set())
        if not hours_collected:
            return {}

        actual = per_day_actual.get(today_date, {})
        forecast = per_day_forecast.get(today_date, {})
        return {
            "date": today_date,
            "hours_collected": len(hours_collected),
            "collected_timeframes": sorted(
                tf for tf in TIMEFRAME_IDS if actual.get(tf, 0.0) > 0
            ),
            "actual_kwh": {str(tf): round(actual.get(tf, 0.0), 3) for tf in TIMEFRAME_IDS},
            "forecast_kwh": {str(tf): round(forecast.get(tf, 0.0), 3) for tf in TIMEFRAME_IDS},
        }

    def get_aggregated_history(self) -> Dict[str, Any]:
        """
        Per-day and per-timeframe history for the status endpoint.

        Excludes today, matching the window the scale factors are actually derived from.
        """
        rows = self._pv_yield_store.get_history_last_n_days(self.retention_days)
        if not rows:
            return {"days": [], "summary_by_timeframe": {}}

        today_date = self._today_local_iso()
        per_day_actual, per_day_forecast, hours, _, origins = (
            self._aggregate_by_day_and_timeframe(rows, lambda d: d != today_date, count_hours=True)
        )

        days = []
        for date in sorted(per_day_actual.keys() | per_day_forecast.keys(), reverse=True):
            actual = per_day_actual.get(date, {})
            forecast = per_day_forecast.get(date, {})
            days.append(
                {
                    "date": date,
                    "hours_collected": len(hours.get(date, set())),
                    # "measured" unless every row for the day came from a restore, so a
                    # day that has since been topped up by real collection reads as real.
                    "origin": _day_origin(origins.get(date, set())),
                    "actual_kwh": {
                        str(tf): round(actual.get(tf, 0.0), 3) for tf in TIMEFRAME_IDS
                    },
                    "forecast_kwh": {
                        str(tf): round(forecast.get(tf, 0.0), 3) for tf in TIMEFRAME_IDS
                    },
                    "total_actual_kwh": round(sum(actual.values()), 3),
                    "total_forecast_kwh": round(sum(forecast.values()), 3),
                }
            )

        summary = {}
        for tf in TIMEFRAME_IDS:
            # Only days with both sides recorded, so the summary matches the factors.
            paired = [
                (per_day_actual[d][tf], per_day_forecast[d][tf])
                for d in per_day_actual.keys() & per_day_forecast.keys()
                if tf in per_day_actual[d] and tf in per_day_forecast[d]
            ]
            summary[str(tf)] = {
                "days": len(paired),
                "actual_kwh": round(sum(a for a, _ in paired), 3),
                "forecast_kwh": round(sum(f for _, f in paired), 3),
                "scale_factor": self._scale_factors.get(tf, 1.0),
            }

        return {"days": days, "summary_by_timeframe": summary}

    def apply_scaling(self, forecast_values: List[float], time_frame_base: int) -> List[float]:
        """
        Apply the computed scale factors to a forecast array.

        Slot 0 is local midnight today, so a slot's timeframe follows from its index
        and the configured resolution.
        """
        # Read once: the loop must not see a half-updated dict if a recompute lands
        # while it is running.
        factors = self._scale_factors
        scaled: List[float] = []
        for i, value in enumerate(forecast_values):
            slot_hour = ((i * int(time_frame_base)) // 3600) % 24
            factor = factors.get(timeframe_for_hour(slot_hour), 1.0)
            scaled.append(round(float(value) * factor, 1))
        return scaled

    def get_status(self) -> Dict[str, Any]:
        """Summarize the autoscaler's runtime state for the status endpoint and UI."""
        restored_hours = 0
        try:
            rows = self._pv_yield_store.get_history_last_n_days(self.retention_days)
            total_hours = len(rows) if rows else 0
            # Hours that came from a backup rather than this system's own meter. The UI
            # says so, so nobody reads a seeded scale factor as a local measurement.
            restored_hours = sum(
                1 for row in rows or [] if (self._normalize_row(row) or {}).get("origin")
            )
        except (AttributeError, TypeError, ValueError):
            logger.exception("[PV-AUTO] Could not read history for status")
            total_hours = 0
        return {
            "enabled": self.enabled,
            "running": self.is_running(),
            "restored_hours": restored_hours,
            "sensor_entity_id": self.sensor_entity_id,
            "min_data_hours_required": self.min_data_hours_required,
            "retention_days": self.retention_days,
            "scale_factors": {str(k): v for k, v in self._scale_factors.items()},
            "total_hours_recorded": total_hours,
            "last_reading_timestamp": self._previous_counter_ts,
            # Last time the loop attempted a collection, success or not; useful to
            # detect a stalled collector (e.g. sensor fetch failing every cycle).
            "last_collection_attempt": (
                self.last_collection.isoformat() if self.last_collection else None
            ),
            # Present only while collection is actually failing, so the UI can show a
            # broken sensor instead of an indefinite "initializing" banner.
            "last_error": self._last_error,
            "last_error_timestamp": self._last_error_ts,
            "consecutive_failures": self._consecutive_failures,
        }
