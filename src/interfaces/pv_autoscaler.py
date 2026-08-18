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

logger = logging.getLogger("__main__")


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

        # Runtime state
        self._previous_counter_kwh: Optional[float] = None
        self._previous_counter_ts: Optional[str] = None
        self.last_collection: Optional[datetime] = None
        self.collection_interval = 3600  # seconds
        self.request_timeout = request_timeout
        self.ssl_ignore = ssl_ignore

        # Cached scale factors (1..4)
        self._scale_factors: Dict[int, float] = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}

        # Background thread control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Complete remaining initialization
        self._complete_init(auto_start)

    def _get_local_now(self) -> datetime:
        """
        Get current time in the configured local timezone.
        Falls back to system local time if timezone is not configured.

        Returns:
            datetime object in local timezone, rounded to the hour
            (minute, second, microsecond set to 0)
        """
        try:
            if self._timezone:
                # Use configured timezone
                if isinstance(self._timezone, str):
                    tz = pytz.timezone(self._timezone)
                else:
                    tz = self._timezone
                # Get UTC now and localize to the configured timezone
                utc_now = datetime.now(timezone.utc)
                local_now = utc_now.astimezone(tz)
            else:
                # Fallback to system local time
                local_now = datetime.now()
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError) as e:
            logger.warning("[PV-AUTO] Failed to get local time: %s, falling back to system time", e)
            local_now = datetime.now()

        # Round to hour boundary
        return local_now.replace(minute=0, second=0, microsecond=0)

    # End of __init__ method - now finishing initialization inside __init__
    # This code is conceptually part of __init__ but was in the original class
    def _complete_init(self, auto_start: bool) -> None:
        """Complete remaining initialization (seed previous counter, logging)."""
        # Seed previous counter from store if available
        try:
            latest = self._pv_yield_store.get_latest_record()
            if latest and latest.get("real_counter_kwh") is not None:
                self._previous_counter_kwh = float(latest.get("real_counter_kwh"))
                self._previous_counter_ts = latest.get("timestamp")
        except (TypeError, ValueError, KeyError):
            logger.exception("[PV-AUTO] Could not seed previous counter from DB")

        # Log initialization
        token_status = "SET" if self.access_token else "NOT SET"
        logger.info(
            "[PV-AUTO] Initialized: enabled=%s, sensor=%s, url=%s, src=%s, token=%s",
            self.enabled,
            self.sensor_entity_id,
            self.url,
            self.src,
            token_status,
        )

        # Back-reference to PvInterface (populated by eos_connect wiring)
        self._pv_interface = None

        if auto_start and self.enabled:
            self.start_update_service()

    # --------------------------- Remote fetch ---------------------------
    def __fetch_remote_state(self, source: str, sensor: str) -> str:
        """Fetch raw state string from Home Assistant or OpenHAB.

        This mirrors the pattern used in other interfaces (BatteryInterface).
        """
        if not sensor:
            raise ValueError("Sensor/item identifier must be provided")

        if source == "openhab":
            url = self.url.rstrip("/") + "/rest/items/" + sensor
            response = requests.get(url, timeout=self.request_timeout, verify=not self.ssl_ignore)
            response.raise_for_status()
            data = response.json()
            return str(data.get("state", "")).strip()
        elif source == "homeassistant":
            url = f"{self.url.rstrip('/')}/api/states/{sensor}"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }
            response = requests.get(
                url, headers=headers, timeout=self.request_timeout,
                verify=not self.ssl_ignore
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("state", "")).strip()
        else:
            raise ValueError(f"Unknown source: {source}")

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

        # Get current time in local timezone
        try:
            if self._timezone:
                if isinstance(self._timezone, str):
                    tz = pytz.timezone(self._timezone)
                else:
                    tz = self._timezone
                now = datetime.now(tz)
            else:
                now = datetime.now()
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError):
            now = datetime.now()

        # Calculate the current hour boundary (this hour's start)
        current_hour_boundary = now.replace(minute=0, second=0, microsecond=0)

        # If we haven't collected yet, do it now
        if self.last_collection is None:
            return True

        try:
            # Convert last_collection (UTC) to local time for comparison
            last_collect_utc = self.last_collection
            if last_collect_utc.tzinfo is None:
                last_collect_utc = last_collect_utc.replace(tzinfo=timezone.utc)

            if self._timezone:
                if isinstance(self._timezone, str):
                    tz = pytz.timezone(self._timezone)
                else:
                    tz = self._timezone
                last_collect_local = last_collect_utc.astimezone(tz)
            else:
                last_collect_local = last_collect_utc.astimezone()

            # Get the hour boundary of last collection
            last_collect_hour = last_collect_local.replace(minute=0, second=0, microsecond=0)

            # If last collection was in a different hour, collect now (new hour has started)
            if last_collect_hour < current_hour_boundary:
                return True
        except (ValueError, TypeError, AttributeError):
            logger.warning("[PV-AUTO] Error checking hour boundary, using fallback timing")
            # Fallback to interval-based collection
            time_since = (datetime.utcnow() - self.last_collection).total_seconds()
            return time_since >= self.collection_interval

    def collect_if_needed(self) -> bool:
        """Collect PV data if needed based on the current time and collection interval.

        Returns:
            bool: True if data was collected, False otherwise.
        """
        if not self.should_collect():
            return False

        # Get current time in local timezone
        try:
            if self._timezone:
                if isinstance(self._timezone, str):
                    tz = pytz.timezone(self._timezone)
                else:
                    tz = self._timezone
                now_local = datetime.now(tz)
            else:
                now_local = datetime.now()
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError):
            now_local = datetime.now()

        # Round to hour boundary for calculations
        # Collection happens when hour changes (e.g., at 9:00:00 local)
        # This represents the END of the previous hour (8-9 period)
        now_boundary = now_local.replace(minute=0, second=0, microsecond=0)

        # Convert boundary time to UTC for offset calculation
        try:
            if self._timezone:
                if isinstance(self._timezone, str):
                    tz = pytz.timezone(self._timezone)
                else:
                    tz = self._timezone
                # Make timezone-aware if not already
                if now_boundary.tzinfo is None:
                    now_boundary = tz.localize(now_boundary)
                utc_now_boundary = now_boundary.astimezone(timezone.utc)
            else:
                utc_now_boundary = now_boundary.astimezone(timezone.utc)
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError):
            utc_now_boundary = now_boundary.replace(tzinfo=timezone.utc)

        # Timestamp should represent START of the period, not end
        # If collecting at 9:00 AM for 8-9 period, store timestamp as 8:00 AM (start of period)
        # Go back 1 hour from current boundary
        period_start_local = now_boundary - timedelta(hours=1)
        try:
            if self._timezone:
                if isinstance(self._timezone, str):
                    tz = pytz.timezone(self._timezone)
                else:
                    tz = self._timezone
                # Already timezone-aware from now_boundary
                utc_period_start = period_start_local.astimezone(timezone.utc)
            else:
                utc_period_start = period_start_local.astimezone(timezone.utc)
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError):
            utc_period_start = period_start_local.replace(tzinfo=timezone.utc)

        # The delta we measure represents generation from the PREVIOUS
        # hour (from last collection to now)
        # So we need to store it as the previous hour's data
        prev_hour_local = (now_boundary.hour - 1) % 24
        if now_boundary.hour > 0:
            prev_date_local = now_boundary.strftime("%Y-%m-%d")
        else:
            prev_date_local = (now_boundary - timedelta(days=1)).strftime("%Y-%m-%d")

        # Calculate UTC offset in minutes for timezone tracking (DST-aware)
        try:
            if self._timezone:
                if isinstance(self._timezone, str):
                    tz = pytz.timezone(self._timezone)
                else:
                    tz = self._timezone
                localized_utc = utc_now_boundary.astimezone(tz)
                local_offset_minutes = int(localized_utc.utcoffset().total_seconds() / 60)
            else:
                # For system local time
                local_aware = utc_now_boundary.astimezone()
                local_offset_minutes = int(local_aware.utcoffset().total_seconds() / 60)
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError, AttributeError):
            local_offset_minutes = None
        prev_last = self.last_collection
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
            self.last_collection = datetime.utcnow()
            return False

        real_delta_kwh: Optional[float] = None
        if self._previous_counter_kwh is None:
            # First valid reading seeds baseline but does not produce a delta
            self._previous_counter_kwh = current_counter
            self._previous_counter_ts = period_start_local.isoformat()
            self.last_collection = datetime.utcnow()
            logger.info("[PV-AUTO] Seeded previous counter: %s kWh", current_counter)
            return False

        # PV and autoscaler services start concurrently. Do not write a real
        # yield row until the live forecast array used by the EOS request is
        # populated with the complete configured horizon.
        try:
            time_frame_base = int(getattr(self._pv_interface, "time_frame_base", 3600))
            expected_slots = 48 if time_frame_base == 3600 else 192
        except (TypeError, ValueError):
            time_frame_base = 3600
            expected_slots = 48
        # Use scale=False to get the ORIGINAL (unscaled) forecast values for database storage.
        # This ensures we store the raw forecast before autoscaling is applied, which is
        # needed to calculate accurate scale factors by comparing real data with original forecast.
        current_forecast = (
            getattr(self._pv_interface, "get_current_pv_forecast", lambda: None)(scale=False)
            if self._pv_interface is not None
            else None
        )
        if not isinstance(current_forecast, list) or len(current_forecast) < expected_slots:
            logger.info(
                "[PV-AUTO] Forecast not ready; delaying collection "
                "(have %s slots, need %d)",
                len(current_forecast) if isinstance(current_forecast, list) else 0,
                expected_slots,
            )
            return False

        # Read the full-day forecast array. The array is aligned to local midnight and contains
        # Wh per configured slot (hourly or 15-minute). Using scale=False ensures we get the
        # original forecast values, not the autoscaled ones.
        forecast_kwh = None
        try:
            if self._pv_interface is not None:
                arr = current_forecast
                if isinstance(arr, list):
                    # The forecast horizon is normally 48 hours, so array length
                    # cannot identify the number of slots in one hour. Use the
                    # interface resolution instead of treating 48 hourly values
                    # as 24 two-slot hours.
                    try:
                        time_frame_base = int(getattr(self._pv_interface, "time_frame_base", 3600))
                        slots_per_hour = max(1, 3600 // time_frame_base)
                    except (TypeError, ValueError, ZeroDivisionError):
                        slots_per_hour = 1
                    # Get forecast for PREVIOUS hour (delta represents generation
                    # during that hour)
                    idx_start = (prev_hour_local % 24) * slots_per_hour
                    hour_slots = arr[idx_start : idx_start + slots_per_hour]
                    if hour_slots:
                        # Sum source Wh values, then normalize the stored value to kWh.
                        total_kwh = sum(float(x) for x in hour_slots) / 1000.0
                        logger.info(
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
                        # Validation: Reject unrealistic 0.0 forecasts for daytime hours
                        # (6 AM - 6 PM when sun is typically up). Zero is normal for night hours.
                        # This prevents storing corrupted data when forecast service hasn't updated.
                        if total_kwh > 0 or prev_hour_local < 6 or prev_hour_local >= 18:
                            # Valid forecast: positive value, or night hour (when 0.0 is realistic)
                            forecast_kwh = float(total_kwh)
                        else:
                            # Unrealistic 0.0 for daytime - store as NULL instead
                            # This handles cases where forecast service returned incomplete data
                            forecast_kwh = None
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
            timeframe_id = (prev_hour_local // 6) + 1
            try:
                self._pv_yield_store.insert_hourly_record(
                    # Period start timestamp (e.g., 8:00:00 UTC for 8-9 period)
                    timestamp=utc_period_start.isoformat(),
                    date=prev_date_local,  # Local date for previous hour
                    hour=prev_hour_local,  # Local hour (when delta occurred)
                    timeframe_id=timeframe_id,
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
            self.last_collection = datetime.utcnow()
            return True
        else:
            real_delta_kwh = float(delta)

        # Missed-poll handling: if last stored DB timestamp is older than 1 hour,
        # distribute the observed delta across the missed hours. This creates
        # historical rows for the missing hours with evenly divided deltas and
        # approximate per-hour forecast if snapshot is available.
        missed_hours = 1
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
                    diff_seconds = (
                        utc_now_boundary.replace(tzinfo=timezone.utc) - last_period_end
                    ).total_seconds()
                    missed_hours = int(max(1, math.ceil(diff_seconds / 3600.0)))
                except (ValueError, TypeError, AttributeError):
                    missed_hours = 1
        except (TypeError, ValueError):
            missed_hours = 1

        timeframe_id = (prev_hour_local // 6) + 1

        # Insert record(s) and purge old rows. If we missed multiple hours, create
        # historical rows distributing the measured delta evenly across the hours.
        try:
            # Ensure arr/slots variables exist for per-hour forecast lookup
            arr = locals().get('arr', None)
            slots_per_hour = locals().get('slots_per_hour', 1)

            if missed_hours <= 1:
                # Single-hour case: attribute full delta to previous hour (when delta occurred)
                self._pv_yield_store.insert_hourly_record(
                    # Period start timestamp (e.g., 8:00:00 UTC for 8-9 period)
                    timestamp=utc_period_start.isoformat(),
                    date=prev_date_local,  # Local date for previous hour
                    hour=prev_hour_local,  # Local hour (when delta occurred)
                    timeframe_id=timeframe_id,
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
                try:
                    anchor_utc_ts = last_db_ts
                except NameError:
                    # Fallback: use boundary time for anchor
                    anchor_utc_ts = utc_now_boundary.replace(tzinfo=timezone.utc)

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
                        if self._timezone:
                            if isinstance(self._timezone, str):
                                tz = pytz.timezone(self._timezone)
                            else:
                                tz = self._timezone
                            local_ts_i = utc_ts_i.astimezone(tz)
                        else:
                            local_ts_i = utc_ts_i.astimezone()
                    except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError):
                        local_ts_i = utc_ts_i.astimezone()

                    local_hour_i = local_ts_i.hour
                    local_date_i = local_ts_i.strftime("%Y-%m-%d")
                    tf = (local_hour_i // 6) + 1

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
                        timeframe_id=tf,
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

        self.last_collection = datetime.utcnow()
        return True

    def set_pv_interface(self, pv_interface) -> None:
        """Attach the `PvInterface` instance so the autoscaler can read raw forecasts."""
        self._pv_interface = pv_interface

    def start_update_service(self) -> None:
        """Start the background update service thread if not already running."""
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
        if self._thread is None:
            return
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        logger.info("[PV-AUTO] Update service stopped")

    # --------------------- Scaling calculation ---------------------
    def compute_timeframe_scaling_factors(self) -> Dict[int, float]:
        """Compute scale factors for each timeframe (1..4) based on historical data."""
        rows = self._pv_yield_store.get_history_last_n_days(self.retention_days)
        if not rows:
            return self._scale_factors.copy()

        # Get today's date in local timezone to exclude it from calculation
        try:
            if self._timezone:
                tz = pytz.timezone(self._timezone) if isinstance(
                    self._timezone, str
                ) else self._timezone
                today_date = datetime.now(tz).date().isoformat()
            else:
                today_date = datetime.now().date().isoformat()
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError):
            # Fallback if timezone is not available
            today_date = datetime.now().date().isoformat()

        # Aggregate by (date, timeframe) - EXCLUDING TODAY
        per_day_frame_actual: Dict[str, Dict[int, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        per_day_frame_forecast: Dict[str, Dict[int, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        recorded_hours = 0

        for r in rows:
            # Support both tuple rows and dict rows from PvYieldStore
            if isinstance(r, dict):
                date = r.get("date")
                timeframe = r.get("timeframe_id")
                hour = r.get("hour")
                real_delta = r.get("real_delta_kwh")
                forecast_kwh = r.get("forecast_kwh")
            else:
                # row tuple: id, timestamp, date, hour, timeframe_id,
                # real_counter_kwh, real_delta_kwh, forecast_kwh, created_at
                date = r[2]
                hour = r[3]
                timeframe = r[4] if r[4] is not None else ((int(r[3]) // 6) + 1)
                real_delta = r[6]
                forecast_kwh = r[7]

            # Skip today's data - only use yesterday and before
            if date == today_date:
                continue

            # Derive timeframe from hour if not present
            try:
                tf = int(timeframe) if timeframe is not None else None
            except (TypeError, ValueError):
                tf = None
            if tf is None:
                try:
                    tf = (int(hour) // 6) + 1
                except (TypeError, ValueError):
                    tf = 1

            if real_delta is not None:
                per_day_frame_actual[date][tf] += float(real_delta)
                recorded_hours += 1
            if forecast_kwh is not None:
                per_day_frame_forecast[date][tf] += float(forecast_kwh)

        if recorded_hours < self.min_data_hours_required:
            logger.info(
                "[PV-AUTO] Not enough recorded hours (%d < %d). Using neutral factors",
                recorded_hours,
                self.min_data_hours_required,
            )
            return {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}

        # For each timeframe compute average across days that have data for that timeframe
        timeframe_totals_actual: Dict[int, float] = defaultdict(float)
        timeframe_totals_forecast: Dict[int, float] = defaultdict(float)
        timeframe_days_count: Dict[int, int] = defaultdict(int)

        all_dates = list(per_day_frame_actual.keys() | per_day_frame_forecast.keys())
        for date in all_dates:
            for tf in (1, 2, 3, 4):
                actual = per_day_frame_actual.get(date, {}).get(tf, 0.0)
                forecast = per_day_frame_forecast.get(date, {}).get(tf, 0.0)
                if actual > 0.0 or forecast > 0.0:
                    timeframe_totals_actual[tf] += actual
                    timeframe_totals_forecast[tf] += forecast
                    timeframe_days_count[tf] += 1

        scale_factors: Dict[int, float] = {}
        for tf in (1, 2, 3, 4):
            days = timeframe_days_count.get(tf, 0)
            if days == 0:
                scale_factors[tf] = 1.0
                continue
            avg_actual = timeframe_totals_actual[tf] / days
            avg_forecast = timeframe_totals_forecast[tf] / days
            if avg_forecast < 0.05:
                scale = 1.0
            else:
                scale = avg_actual / avg_forecast if avg_forecast > 0 else 1.0
            # Clamp
            if scale < self.min_scale_factor:
                scale = self.min_scale_factor
            if scale > self.max_scale_factor:
                scale = self.max_scale_factor
            scale_factors[tf] = round(scale, 3)

        self._scale_factors = scale_factors
        logger.info(
            "[PV-AUTO] Computed scale factors: %s (hours=%d)", scale_factors, recorded_hours
        )
        return scale_factors.copy()

    def get_todays_partial_data(self) -> Dict[str, Any]:
        """Extract today's partial data (not used in scaling, informational only)."""
        rows = self._pv_yield_store.get_history_last_n_days(self.retention_days)
        if not rows:
            return {}

        # Get today's date in local timezone
        try:
            if self._timezone:
                tz = pytz.timezone(self._timezone) if isinstance(
                    self._timezone, str
                ) else self._timezone
                today_date = datetime.now(tz).date().isoformat()
            else:
                today_date = datetime.now().date().isoformat()
        except (pytz.exceptions.UnknownTimeZoneError, ValueError, TypeError):
            today_date = datetime.now().date().isoformat()

        # Extract today's data
        today_timeframes = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
        today_forecast = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
        hours_collected = []

        for r in rows:
            if isinstance(r, dict):
                date = r.get("date")
                timeframe = r.get("timeframe_id")
                hour = r.get("hour")
                real_delta = r.get("real_delta_kwh")
                forecast_kwh = r.get("forecast_kwh")
            else:
                date = r[2]
                hour = r[3]
                timeframe = r[4] if r[4] is not None else ((int(r[3]) // 6) + 1)
                real_delta = r[6]
                forecast_kwh = r[7]

            if date != today_date:
                continue

            # Derive timeframe from hour if not present
            try:
                tf = int(timeframe) if timeframe is not None else None
            except (TypeError, ValueError):
                tf = None
            if tf is None:
                try:
                    tf = (int(hour) // 6) + 1
                except (TypeError, ValueError):
                    tf = 1

            if real_delta is not None:
                today_timeframes[tf] += float(real_delta)
                hours_collected.append(int(hour))
            if forecast_kwh is not None:
                today_forecast[tf] += float(forecast_kwh)

        if not hours_collected:
            return {}

        return {
            "date": today_date,
            "hours_collected": len(set(hours_collected)),  # Unique hours
            "collected_timeframes": sorted([tf for tf in (1, 2, 3, 4) if today_timeframes[tf] > 0]),
            "actual_kwh": {str(k): round(v, 3) for k, v in today_timeframes.items()},
            "forecast_kwh": {str(k): round(v, 3) for k, v in today_forecast.items()},
        }

    def apply_scaling(self, forecast_values: List[float], time_frame_base: int) -> List[float]:
        """Apply the computed scale factors to the forecast values."""
        # Ensure factors are up-to-date
        factors = self._scale_factors
        scaled: List[float] = []
        for i, v in enumerate(forecast_values):
            # Determine hour for slot i
            slot_seconds = i * int(time_frame_base)
            slot_hour = (slot_seconds // 3600) % 24
            tf = (slot_hour // 6) + 1
            factor = factors.get(tf, 1.0)
            scaled.append(round(float(v) * factor, 1))
        return scaled

    def get_status(self) -> Dict[str, Any]:
        """Return a dictionary summarizing the current status of the PV autoscaler,
           including whether it's enabled, the current scale factors, total hours recorded,
           last reading timestamp, and last collection attempt time."""
        rows = self._pv_yield_store.get_history_last_n_days(self.retention_days)
        total_hours = len(rows) if rows else 0
        return {
            "enabled": self.enabled,
            "scale_factors": self._scale_factors.copy(),
            "total_hours_recorded": total_hours,
            "last_reading_timestamp": self._previous_counter_ts,
            # Last time the loop attempted a collection, success or not; useful to
            # detect a stalled collector (e.g. sensor fetch failing every cycle).
            "last_collection_attempt": (
                self.last_collection.isoformat() if self.last_collection else None
            ),
        }
