"""
Hot-Reload Adapter — Applies live configuration changes to running interfaces.

Registered as a change callback on ConfigStore. When a hot-reloadable value
is changed via the web UI, this adapter updates the running interface
instance directly, avoiding a full restart.

Supported fields (Priority 1 — Price):
- ``price.fixed_price_adder_ct``
- ``price.relative_price_multiplier``
- ``price.feed_in_price``  (also triggers immediate run via ``_PRICE_RUN_TRIGGERS``)
- ``price.negative_price_switch``

Supported fields (Price data source reload — immediate data fetch):
- ``price.source``  (triggers immediate fetch if switching TO timeseries; 
    defers if switching FROM timeseries)
- ``price.data_url``  (triggers immediate fetch when source=timeseries)
- ``price.data_path``  (triggers immediate fetch when source=timeseries)
- ``price.data_token``  (triggers immediate fetch when source=timeseries)
- ``price.use_ha_central_data_source``  (triggers immediate fetch when source=timeseries)
- ``price.ha_sensor_name``  (triggers immediate fetch when source=timeseries)

Safety note: When switching FROM timeseries to another source, config is updated but 
fetch is deferred. This prevents errors from fetching with incomplete config for the 
new source. The next scheduled update cycle will use the correct source and config.

Supported fields (Priority 2 — Battery SOC):
- ``battery.min_soc_percentage``
- ``battery.max_soc_percentage``

Supported fields (Feed-in price source):
- ``price.feed_in_source``  (immediate reload when switching sources)
- ``price.feed_in_zone``  (immediate reload when zone changes for Elpris)

Supported fields (Feed-in price adjustments):
- ``price.feed_in_static_adder``  (also triggers immediate run via ``_PRICE_RUN_TRIGGERS``)
- ``price.feed_in_multiplier``

Supported fields (Priority 1 — Optimizer):
- ``eos.timeout``
- ``eos.dyn_override_discharge_allowed_pv_greater_load``
  (also triggers immediate run via ``_OPTIMIZER_RUN_TRIGGERS``)
- ``eos.pv_battery_charge_control_enabled``

Supported fields (Local EVopt strategies):
- ``eos.local_evopt_charging_strategy``
- ``eos.local_evopt_discharging_strategy``
- ``eos.local_evopt_emergency_reserve_pct``

PV Forecast Hot-Reload Behavior:
- **Per-installation sources** (akkudoktor, openmeteo, solcast, victron, etc.): 
    Reload on config change
- **Summarized sources** (timeseries, evcc): Skip reload, defer to background loop

Rationale: Timeseries and EVCC provide single summarized PV values, not per-installation data.
Reloading would cause redundant API fetches (one per installation). Background update loop
handles these sources more efficiently.
"""

import logging
import threading
from datetime import datetime

logger = logging.getLogger("__main__")


# Map of config store keys to (interface_attr_name, coerce_fn)
_PRICE_FIELD_MAP = {
    "price.fixed_price_adder_ct": ("fixed_price_adder_ct", float),
    "price.relative_price_multiplier": ("relative_price_multiplier", float),
    "price.feed_in_price": ("feed_in_tariff_price", float),
}

# Price data source fields that require reload (timeseries URL/path/token)
_PRICE_DATA_FIELDS = {
    "price.source",
    "price.data_url",
    "price.data_path",
    "price.data_token",
    "price.use_ha_central_data_source",
    "price.ha_sensor_name",
}

# Map of feed-in price config keys to (interface_attr_name, coerce_fn)
_FEEDIN_PRICE_FIELD_MAP = {
    "price.feed_in_static_adder": ("static_adder_ct_kwh", float),  # ct/kWh (standard unit)
    "price.feed_in_multiplier": ("multiplier", float),
    "price.feed_in_negative_price_switch": ("negative_price_switch", bool),
}

# Feed-in data source fields that require reload (source/zone changes)
_FEEDIN_DATA_FIELDS = {
    "price.feed_in_source",
    "price.feed_in_zone",
}

_BATTERY_SOC_FIELDS = {
    "battery.min_soc_percentage",
    "battery.max_soc_percentage",
}

_BATTERY_PRICE_FIELD_MAP = {
    "battery.battery_price_include_feedin": ("battery_price_include_feedin", bool),
    "battery.charging_threshold_w": ("charging_threshold_w", float),
    "battery.grid_charge_threshold_w": ("grid_charge_threshold_w", float),
}

# Map of optimizer config keys to (interface_attr_name, coerce_fn)
_OPTIMIZER_FIELD_MAP = {
    "eos.timeout": ("timeout", int),
    "eos.dyn_override_discharge_allowed_pv_greater_load": ("dyn_override_discharge_allowed", bool),
    "eos.pv_battery_charge_control_enabled": ("pv_battery_charge_control_enabled", bool),
}

# Map of local_evopt strategy keys to (backend_attr_name, coerce_fn)
_LOCAL_EVOPT_FIELD_MAP = {
    "eos.local_evopt_charging_strategy": ("charging_strategy", str),
    "eos.local_evopt_discharging_strategy": ("discharging_strategy", str),
    "eos.local_evopt_emergency_reserve_pct": ("emergency_reserve_pct", int),
}

# Optimizer keys whose change immediately invalidates the current result
_OPTIMIZER_RUN_TRIGGERS = {
    "eos.dyn_override_discharge_allowed_pv_greater_load",
}

# Price keys whose change immediately invalidates the current optimization result
_PRICE_RUN_TRIGGERS = {
    "price.feed_in_price",
    "price.feed_in_static_adder",
}

# Feed-in related fields that require recalculating feed-in prices
_FEEDIN_TRIGGERS = {
    "price.feed_in_price",
    "price.feed_in_negative_price_switch",
}

_PV_KEY_PREFIXES = (
    "pv_forecast_source.",
    "pv_forecast.",
)

def _coerce_bool(value):
    """
    Coerce a stored value to bool.

    Built-in bool() is wrong here: every non-empty string is truthy, so the string
    "false" that round-trips through the store would enable a setting the user turned
    off.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# PV Autoscaler hot-reload map: map of config key to (autoscaler_attr, coerce_fn)
_PV_AUTOSCALER_FIELD_MAP = {
    "pv_autoscaling.enabled": ("enabled", bool),
    "pv_autoscaling.use_ha_central_data_source": ("use_ha_central_data_source", bool),
    "pv_autoscaling.min_scale_factor": ("min_scale_factor", float),
    "pv_autoscaling.max_scale_factor": ("max_scale_factor", float),
    "pv_autoscaling.retention_days": ("retention_days", int),
    "pv_autoscaling.min_data_hours_required": ("min_data_hours_required", int),
    # Connection fields. These take effect on the next hourly poll, so they need no
    # restart - but without an entry here changing them would neither apply nor raise
    # the "restart required" banner, leaving the running autoscaler on the old sensor.
    "pv_autoscaling.sensor_entity_id": ("sensor_entity_id", str),
    "pv_autoscaling.src": ("src", str),
    "pv_autoscaling.url": ("url", str),
    "pv_autoscaling.access_token": ("access_token", str),
    "pv_autoscaling.ssl_ignore": ("ssl_ignore", bool),
}


class HotReloadAdapter:
    """
    Applies config changes to live interface instances.

    Args:
        price_interface: Running PriceInterface instance (or None).
        battery_interface: Running BatteryInterface instance (or None).
        pv_interface: Running PvInterface instance (or None).
        optimization_interface: Running OptimizationInterface instance (or None).
        config_provider: Callable that returns the current merged config dict (or None).
        on_run_trigger: Optional callable() invoked after a hot-reload that makes the
            current optimization result stale (e.g. strategy change).  Typically wired
            to ``OptimizationScheduler.request_immediate_run``.  Can also be set later
            via ``adapter.on_run_trigger = scheduler.request_immediate_run``.
        pv_reload_debounce_seconds: Debounce delay for PV reloads (default 0.3s).
    """

    def __init__(
        self,
        price_interface=None,
        battery_interface=None,
        pv_interface=None,
        optimization_interface=None,
        feed_in_price_interface=None,
        config_provider=None,
        on_run_trigger=None,
        pv_reload_debounce_seconds=0.3,
    ):
        self._price = price_interface
        self._battery = battery_interface
        self._pv = pv_interface
        self._optimizer = optimization_interface
        self._feed_in_price = feed_in_price_interface
        self._config_provider = config_provider
        self.on_run_trigger = on_run_trigger
        self._pv_reload_debounce_seconds = pv_reload_debounce_seconds
        self._pv_reload_timer = None
        self._pv_reload_lock = threading.Lock()
        self._pending_pv_keys = set()
        self._applied_keys = []

    @property
    def last_applied(self):
        """List of keys applied in the most recent callback invocation."""
        return list(self._applied_keys)

    def on_config_changed(self, key, _old_value, new_value):
        """
        Callback for ConfigStore changes. Applies the change if the key
        is hot-reloadable, otherwise ignores it.

        Args:
            key: Dot-notation config key (e.g. ``price.feed_in_price``).
            old_value: Previous value.
            new_value: New value.
        """
        self._applied_keys = []

        if key in _PRICE_FIELD_MAP:
            self._apply_price(key, new_value)
        elif key in _PRICE_DATA_FIELDS:
            # If price.source changed, pass the new source to avoid stale config
            force_source = new_value if key == "price.source" else None
            self._schedule_price_reload(key, force_source)
        elif key in _FEEDIN_PRICE_FIELD_MAP:
            self._apply_feed_in_price(key, new_value)
        elif key in _FEEDIN_DATA_FIELDS:
            # If feed-in source changed, pass the new source to avoid stale config
            force_source = new_value if key == "price.feed_in_source" else None
            self._schedule_feedin_reload(key, force_source)
        elif key in _BATTERY_SOC_FIELDS:
            self._apply_battery_soc(key, new_value)
        elif key in _BATTERY_PRICE_FIELD_MAP:
            self._apply_battery_price(key, new_value)
        elif key in _OPTIMIZER_FIELD_MAP:
            self._apply_optimizer(key, new_value)
        elif key in _LOCAL_EVOPT_FIELD_MAP:
            self._apply_local_evopt(key, new_value)
        elif key in _PV_AUTOSCALER_FIELD_MAP:
            self._apply_pv_autoscaler(key, new_value)
        elif key.startswith(_PV_KEY_PREFIXES):
            self._schedule_pv_reload(key, new_value)
        else:
            return  # Not a hot-reloadable key — skip silently

    def _apply_price(self, key, new_value):
        """Apply a price-related config change."""
        if self._price is None:
            logger.debug("[HotReload] No price interface — skipping %s", key)
            return

        attr, coerce = _PRICE_FIELD_MAP[key]
        try:
            coerced = coerce(new_value)
        except (TypeError, ValueError) as exc:
            logger.warning("[HotReload] Cannot coerce %s=%r: %s", key, new_value, exc)
            return

        old_val = getattr(self._price, attr, "?")
        setattr(self._price, attr, coerced)
        self._applied_keys.append(key)
        logger.info(
            "[HotReload] Updated price.%s = %s (was %s)",
            attr, coerced, old_val,
        )

        # Keep BatteryPriceHandler opportunity cost in sync with live feed-in changes.
        if key == "price.feed_in_price":
            self._apply_battery_feedin_price(coerced)
            # Also sync FeedInPriceInterface.fixed_price_ct_kwh — this is what the
            # optimizer actually reads; price_interface.feed_in_tariff_price is legacy.
            self._sync_feed_in_fixed_price(coerced)

        # Sync negative_price_switch to FeedInPriceInterface
        if key == "price.feed_in_negative_price_switch":
            self._sync_feed_in_negative_price_switch(coerced)

        # Recalculate feed-in prices when feed_in_price or negative_price_switch change
        if key in _FEEDIN_TRIGGERS:
            self._recalculate_feedin()

        # Feed-in price change invalidates the current optimization result
        if key in _PRICE_RUN_TRIGGERS:
            self._fire_run_trigger(key)

    def _apply_feed_in_price(self, key, new_value):
        """Apply a feed-in price related config change."""
        if self._feed_in_price is None:
            logger.debug("[HotReload] No feed-in price interface — skipping %s", key)
            return

        attr, coerce = _FEEDIN_PRICE_FIELD_MAP[key]
        try:
            coerced = coerce(new_value)
        except (TypeError, ValueError) as exc:
            logger.warning("[HotReload] Cannot coerce %s=%r: %s", key, new_value, exc)
            return

        old_val = getattr(self._feed_in_price, attr, "?")
        setattr(self._feed_in_price, attr, coerced)
        self._applied_keys.append(key)
        logger.info(
            "[HotReload] Updated feed_in_price.%s = %s (was %s)",
            attr, coerced, old_val,
        )

        # Trigger price update to recalculate arrays with new parameters
        try:
            start_time = datetime.now(self._feed_in_price.time_zone).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            tgt_duration = 192 if self._feed_in_price.time_frame_base == 900 else 48
            self._feed_in_price.update_prices(tgt_duration, start_time)
            logger.debug("[HotReload] Recalculated feed-in prices after %s change", key)
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError) as e:
            logger.warning("[HotReload] Failed to recalculate feed-in prices: %s", e)

        # Feed-in static adder change invalidates the current optimization result
        if key in _PRICE_RUN_TRIGGERS:
            self._fire_run_trigger(key)

    def _sync_feed_in_fixed_price(self, price_ct_kwh):
        """Sync FeedInPriceInterface.fixed_price_ct_kwh and refresh its price array.

        The optimizer reads from FeedInPriceInterface, not PriceInterface, so we must
        keep fixed_price_ct_kwh in sync whenever price.feed_in_price is hot-reloaded.
        """
        if self._feed_in_price is None:
            return
        try:
            old = getattr(self._feed_in_price, "fixed_price_ct_kwh", "?")
            self._feed_in_price.fixed_price_ct_kwh = price_ct_kwh
            start_time = datetime.now(self._feed_in_price.time_zone).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            tgt_duration = 192 if self._feed_in_price.time_frame_base == 900 else 48
            self._feed_in_price.update_prices(tgt_duration, start_time)
            logger.info(
                "[HotReload] Synced FeedInPriceInterface.fixed_price_ct_kwh = %s (was %s)",
                price_ct_kwh, old,
            )
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError) as e:
            logger.warning("[HotReload] Failed to sync FeedInPriceInterface fixed price: %s", e)

    def _sync_feed_in_negative_price_switch(self, negative_price_switch):
        """Sync negative_price_switch to FeedInPriceInterface and refresh prices.

        When the user toggles negative_price_switch via the web UI, we must update
        the FeedInPriceInterface and recalculate prices so the clamping logic takes effect.
        """
        if self._feed_in_price is None:
            return
        try:
            old = getattr(self._feed_in_price, "negative_price_switch", "?")
            self._feed_in_price.negative_price_switch = negative_price_switch
            start_time = datetime.now(self._feed_in_price.time_zone).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            tgt_duration = 192 if self._feed_in_price.time_frame_base == 900 else 48
            self._feed_in_price.update_prices(tgt_duration, start_time)
            logger.info(
                "[HotReload] Synced FeedInPriceInterface.negative_price_switch = %s (was %s)",
                negative_price_switch, old,
            )
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError) as e:
            logger.warning(
                "[HotReload] Failed to sync FeedInPriceInterface negative_price_switch: %s",
                e)

    def _apply_battery_feedin_price(self, feedin_price):
        """Apply live feed-in price updates to the battery price handler."""
        if self._battery is None:
            return

        price_handler = getattr(self._battery, "price_handler", None)
        if price_handler is None:
            return

        old_val = getattr(price_handler, "pv_cost_euro_per_kwh", "?")
        price_handler.pv_cost_euro_per_kwh = feedin_price
        # Force a fresh historical calculation on next battery update cycle.
        price_handler.last_price_calculation = None
        logger.info(
            "[HotReload] Updated battery price feed-in cost = %s (was %s)",
            feedin_price,
            old_val,
        )

    def _recalculate_feedin(self):
        """Recalculate feed-in prices on the price interface."""
        if self._price is None:
            return
        try:
            # If using fixed_24h with negative_price_switch, refresh auxiliary stock prices
            if (self._price.src == "fixed_24h" and self._price.negative_price_switch):
                # Fetch Akkudoktor stock prices for negative price detection
                start_time = datetime.now(self._price.time_zone).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                success = self._price.refresh_stock_prices_for_feedin_check(48, start_time)
                if success:
                    logger.debug(
                        "[HotReload] Refreshed Akkudoktor stock prices for fixed_24h negative"+
                        " price detection"
                    )
                else:
                    logger.warning(
                        "[HotReload] Could not refresh Akkudoktor stock prices for fixed_24h"+
                        " source"
                    )

            # Now recalculate feedin prices with potentially updated stock prices
            feedin = self._price.recalculate_feedin_prices()
            if feedin is not None:
                logger.info(
                    "[HotReload] Recalculated feed-in prices (%d entries)",
                    len(feedin),
                )
        except (AttributeError, TypeError) as exc:
            logger.debug(
                "[HotReload] Could not recalculate feed-in prices: %s", exc
            )

    def _apply_optimizer(self, key, new_value):
        """Apply an optimizer config change."""
        if self._optimizer is None:
            logger.debug("[HotReload] No optimizer interface — skipping %s", key)
            return

        attr, coerce = _OPTIMIZER_FIELD_MAP[key]
        try:
            coerced = coerce(new_value)
        except (TypeError, ValueError) as exc:
            logger.warning("[HotReload] Cannot coerce %s=%r: %s", key, new_value, exc)
            return

        old_val = getattr(self._optimizer, attr, "?")
        setattr(self._optimizer, attr, coerced)
        self._applied_keys.append(key)
        logger.info(
            "[HotReload] Updated optimizer.%s = %s (was %s)",
            attr, coerced, old_val,
        )

        if key in _OPTIMIZER_RUN_TRIGGERS:
            self._fire_run_trigger(key)

    def _apply_local_evopt(self, key, new_value):
        """Apply a local_evopt strategy config change to the running backend."""
        if self._optimizer is None:
            logger.debug("[HotReload] No optimizer interface — skipping %s", key)
            return

        backend = getattr(self._optimizer, "backend", None)
        backend_type = getattr(self._optimizer, "backend_type", None)
        if backend is None or backend_type != "local_evopt":
            logger.debug(
                "[HotReload] Optimizer backend is not local_evopt (%s) — skipping %s",
                backend_type, key,
            )
            return

        attr, coerce = _LOCAL_EVOPT_FIELD_MAP[key]
        try:
            coerced = coerce(new_value)
        except (TypeError, ValueError) as exc:
            logger.warning("[HotReload] Cannot coerce %s=%r: %s", key, new_value, exc)
            return

        # Clamp emergency_reserve_pct to valid range
        if attr == "emergency_reserve_pct":
            coerced = max(0, min(80, coerced))

        old_val = getattr(backend, attr, "?")
        setattr(backend, attr, coerced)
        self._applied_keys.append(key)
        logger.info(
            "[HotReload] Updated local_evopt.%s = %s (was %s)",
            attr, coerced, old_val,
        )
        # Strategy changes immediately invalidate the current optimization result —
        # trigger a new run so the user sees the effect without waiting for the
        # next scheduled slot.
        self._fire_run_trigger(key)

    def _fire_run_trigger(self, reason_key):
        """Call the registered run trigger callback, if any."""
        if self.on_run_trigger is not None:
            try:
                self.on_run_trigger()
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(
                    "[HotReload] on_run_trigger raised an exception after %s: %s",
                    reason_key, exc,
                )

    def _apply_battery_soc(self, key, new_value):
        """Apply a battery SOC config change."""
        if self._battery is None:
            logger.debug("[HotReload] No battery interface — skipping %s", key)
            return

        try:
            int_value = int(new_value)
        except (TypeError, ValueError) as exc:
            logger.warning("[HotReload] Cannot coerce %s=%r: %s", key, new_value, exc)
            return

        if key == "battery.min_soc_percentage":
            # Update the configured floor in battery_data first so set_min_soc()
            # doesn't clamp against the old configured value.
            self._battery.battery_data["min_soc_percentage"] = int_value
            self._battery.set_min_soc(int_value)
            self._applied_keys.append(key)
            logger.info(
                "[HotReload] Updated battery min SOC = %d%%", int_value
            )
        elif key == "battery.max_soc_percentage":
            # Update the configured ceiling in battery_data first.
            self._battery.battery_data["max_soc_percentage"] = int_value
            self._battery.set_max_soc(int_value)
            self._applied_keys.append(key)
            logger.info(
                "[HotReload] Updated battery max SOC = %d%%", int_value
            )

    def _apply_battery_price(self, key, new_value):
        """Apply a battery-price config change to the live BatteryPriceHandler."""
        if self._battery is None:
            logger.debug("[HotReload] No battery interface — skipping %s", key)
            return

        price_handler = getattr(self._battery, "price_handler", None)
        if price_handler is None:
            logger.debug("[HotReload] No battery price handler — skipping %s", key)
            return

        attr, coerce = _BATTERY_PRICE_FIELD_MAP[key]
        try:
            coerced = coerce(new_value)
        except (TypeError, ValueError) as exc:
            logger.warning("[HotReload] Cannot coerce %s=%r: %s", key, new_value, exc)
            return

        old_val = getattr(price_handler, attr, "?")
        setattr(price_handler, attr, coerced)
        # Ensure next loop uses the updated settings immediately.
        price_handler.last_price_calculation = None
        self._applied_keys.append(key)
        logger.info(
            "[HotReload] Updated battery price.%s = %s (was %s)",
            attr,
            coerced,
            old_val,
        )

    def _apply_pv_autoscaler(self, key, new_value):
        """Apply pv_autoscaler related live changes if autoscaler exists."""
        if self._pv is None:
            logger.debug("[HotReload] No pv interface/autoscaler — skipping %s", key)
            return

        autoscaler = self._pv.get_autoscaler() if hasattr(self._pv, "get_autoscaler") else None
        if autoscaler is None:
            logger.debug("[HotReload] No pv_autoscaler attached to pv_interface — skipping %s", key)
            return

        attr, coerce = _PV_AUTOSCALER_FIELD_MAP.get(key, (None, None))
        if attr is None:
            logger.debug("[HotReload] PV autoscaler key %s not recognized", key)
            return
        try:
            coerced = _coerce_bool(new_value) if coerce is bool else coerce(new_value)
        except (TypeError, ValueError) as exc:
            logger.warning("[HotReload] Cannot coerce %s=%r: %s", key, new_value, exc)
            return

        old_val = getattr(autoscaler, attr, "?")
        try:
            # update_config() owns the side effects: starting or stopping the collection
            # thread when `enabled` flips, and recomputing the cached factors when a
            # change affects them. A bare setattr would leave the toggle inert.
            autoscaler.update_config(**{attr: coerced})
            self._applied_keys.append(key)
            logger.info("[HotReload] Updated pv_autoscaler.%s = %s (was %s)", attr, coerced, old_val)
        except Exception as exc:
            logger.warning("[HotReload] Failed to apply pv_autoscaler change %s: %s", key, exc)

    def _schedule_pv_reload(self, key, new_value=None):
        """Schedule PV reload when config changes.
        
        Args:
            key: Config key (e.g., "pv_forecast_source.source")
            new_value: New value being set (used for pv_forecast_source.source to avoid stale reads)
        
        Behavior:
        - Summarized sources (timeseries/evcc): Trigger IMMEDIATE reload to show
            user changes quickly
          * User sees PV data from new source immediately (no 15min+ wait)
          * The PV interface now efficiently fetches summarized sources once (not per-installation)
        - Per-installation sources (akkudoktor, openmeteo, etc.): Debounced reload
          * Coalesces multiple PV field changes into single reload
        """
        if self._pv is None or self._config_provider is None:
            logger.debug("[HotReload] No PV interface/config provider — skipping %s", key)
            return

        # For summarized source changes: trigger immediate reload so user sees changes quickly
        if key == "pv_forecast_source.source":
            # Use new_value parameter directly to avoid stale reads from config_provider
            # (callbacks fire before rebuild_config in API handler)
            new_source = (new_value or "").strip() if new_value else ""
            if new_source in ("timeseries", "evcc"):
                logger.debug(
                    "[HotReload] PV source changed to '%s' (summarized source) — "
                    "triggering immediate reload for instant user visibility",
                    new_source,
                )
                self._pending_pv_keys.clear()
                self._pending_pv_keys.add(key)
                self._apply_pv_reload(force_source=new_source)  # Pass new source to 
                                                                # avoid stale config
                return

        # Support explicit synchronous mode for deterministic tests.
        if self._pv_reload_debounce_seconds <= 0:
            self._pending_pv_keys.add(key)
            self._apply_pv_reload()
            return

        # Debounce per-installation source changes to coalesce multiple updates
        with self._pv_reload_lock:
            self._pending_pv_keys.add(key)
            if self._pv_reload_timer and self._pv_reload_timer.is_alive():
                return
            self._pv_reload_timer = threading.Timer(
                self._pv_reload_debounce_seconds,
                self._apply_pv_reload,
            )
            self._pv_reload_timer.daemon = True
            self._pv_reload_timer.start()

    def _schedule_price_reload(self, key, force_source=None):
        """Schedule price reload when price data source config changes.
        
        Args:
            key: Config key that changed (e.g., 'price.source', 'price.data_url')
            force_source: If provided, use this source instead of reading from merged config.
                         Used when price.source changes to avoid stale config
                         (callbacks fire before rebuild_config in API handler).
        
        Triggers immediate fetch when:
        - Switching TO timeseries or evcc source (any previous source) → fetch with new config
        - Updating timeseries/evcc DATA fields while source=timeseries/evcc → fetch with new data
        
        Does NOT fetch when:
        - Switching FROM timeseries/evcc TO another source → config updated but fetch deferred
          (avoids fetching with incomplete config for the new source)
        """
        if self._price is None or self._config_provider is None:
            logger.debug(
                "[HotReload] No price interface/config provider — skipping %s", key
            )
            return

        try:
            config = self._config_provider()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "[HotReload] Cannot read merged config for price reload: %s", exc
            )
            return

        if not isinstance(config, dict):
            logger.warning("[HotReload] Merged config is invalid for price reload")
            return

        # Update price interface with new data source config
        try:
            price_config = config.get("price", {})
            # Use forced source if provided (callback fired
            # before rebuild_config)
            new_source = force_source if force_source else price_config.get("source", "").strip()
            if force_source:
                logger.debug(
                    "[HotReload] Using forced price source '%s' (callback fired" + 
                    " before rebuild_config)",
                    force_source,
                )

            # Update all price config fields including source
            self._price.src = new_source
            self._price.config_source = price_config
            self._price.data_url = price_config.get("data_url", "").strip()
            self._price.data_path = price_config.get("data_path", "attributes.data").strip()
            self._price.data_token = price_config.get("data_token", "").strip()
            self._applied_keys.append(key)
            # Determine if we should trigger immediate fetch
            # Fetch if new source is timeseries (either switching TO it or already using
            # it with data update)
            # Skip fetch if switching FROM timeseries to another source
            should_fetch = new_source in ("timeseries", "evcc")

            if should_fetch:
                logger.info(
                    "[HotReload] Updated price config (%s: %s...)",
                    key,
                    str(self._price.data_url)[:50],
                )
                # Trigger immediate price fetch with new config
                try:
                    start_time = datetime.now(self._price.time_zone).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    tgt_duration = 192 if self._price.time_frame_base == 900 else 48
                    self._price.update_prices(tgt_duration, start_time)
                    logger.info(
                        "[HotReload] Immediately fetched prices after %s config change", key
                    )
                except (AttributeError, TypeError, ValueError, OSError, RuntimeError) as e:
                    logger.warning(
                        "[HotReload] Failed to fetch prices after %s config change: %s", key, e
                    )
            else:
                # Source change detected but NOT to timeseries/evcc — config updated but
                # fetch deferred
                if key == "price.source":
                    logger.debug(
                        "[HotReload] Price source changed to '%s' — config updated, "
                        "fetch deferred to next update cycle", new_source
                    )
                else:
                    logger.debug(
                        "[HotReload] Updated price config (%s), "
                        "source is '%s' — fetch deferred", key, new_source
                    )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[HotReload] Price data source reload failed: %s", exc)

    def _schedule_feedin_reload(self, key, force_source=None):
        """Schedule feed-in reload when feed-in source/zone config changes.
        
        Args:
            key: Config key that changed (e.g., 'price.feed_in_source', 'price.feed_in_zone')
            force_source: If provided, use this source instead of reading from merged config.
                         Used when price.feed_in_source changes to avoid stale config reads
                         (callbacks fire before rebuild_config in API handler).
        
        Always triggers immediate fetch when source or zone changes, updating the
        running FeedInPriceInterface with new configuration and fetching prices
        from the new source.
        """
        if self._feed_in_price is None or self._config_provider is None:
            logger.debug(
                "[HotReload] No feed-in price interface/config provider — skipping %s", key
            )
            return

        try:
            config = self._config_provider()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "[HotReload] Cannot read merged config for feed-in reload: %s", exc
            )
            return

        if not isinstance(config, dict):
            logger.warning("[HotReload] Merged config is invalid for feed-in reload")
            return

        try:
            feedin_config = config.get("price", {})
            # Use forced source if provided (callback fired before rebuild_config)
            new_source = force_source if force_source else feedin_config.get("feed_in_source", "fixed").strip()
            new_zone = feedin_config.get("feed_in_zone", "DK1").strip()
            
            if force_source:
                logger.debug(
                    "[HotReload] Using forced feed-in source '%s' (callback fired"
                    " before rebuild_config)",
                    force_source,
                )

            # Update feed-in interface with new configuration
            old_source = self._feed_in_price.source
            old_zone = self._feed_in_price.zone
            self._feed_in_price.source = new_source
            self._feed_in_price.zone = new_zone
            self._applied_keys.append(key)

            logger.info(
                "[HotReload] Updated feed-in config: source=%s (was %s), zone=%s (was %s)",
                new_source,
                old_source,
                new_zone,
                old_zone,
            )

            # Trigger immediate feed-in price fetch with new source/zone
            try:
                start_time = datetime.now(self._feed_in_price.time_zone).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                tgt_duration = 192 if self._feed_in_price.time_frame_base == 900 else 48
                self._feed_in_price.update_prices(tgt_duration, start_time)
                logger.info(
                    "[HotReload] Immediately fetched feed-in prices after %s config change"
                    " (source=%s, zone=%s)", key, new_source, new_zone
                )
            except (AttributeError, TypeError, ValueError, OSError, RuntimeError) as e:
                logger.warning(
                    "[HotReload] Failed to fetch feed-in prices after %s config change: %s",
                    key, e
                )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[HotReload] Feed-in source reload failed: %s", exc)

    def _apply_pv_reload(self, force_source=None):
        """Reconfigure the live PV interface from the current merged config.
        
        Args:
            force_source: If provided, override config_source.source with this value.
                Used when source changes to avoid stale config reads (callbacks fire
                before rebuild_config in API handler).
        """
        if self._pv is None or self._config_provider is None:
            return

        with self._pv_reload_lock:
            pending_keys = sorted(self._pending_pv_keys)
            self._pending_pv_keys.clear()

        try:
            config = self._config_provider()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[HotReload] Cannot read merged config for PV reload: %s", exc)
            return

        if not isinstance(config, dict):
            logger.warning("[HotReload] Merged config is invalid for PV reload")
            return

        # If source changed to summarized type, use the new source directly to avoid
        # stale config (callbacks fire before rebuild_config in API handler)
        config_source = config.get("pv_forecast_source", {})
        if force_source:
            config_source = dict(config_source)  # Copy to avoid mutating original
            config_source["source"] = force_source
            logger.debug(
                "[HotReload] Using forced source '%s' (callback fired before rebuild_config)",
                force_source,
            )

        try:
            self._pv.reload_config(
                config_source=config_source,
                config=config.get("pv_forecast", []),
                config_special=config.get("evcc", {}),
                temperature_forecast_enabled=(
                    config.get("eos", {}).get("source", "eos_server") == "eos_server"
                ),
                timezone=config.get("time_zone", "UTC"),
            )
            self._applied_keys.extend(pending_keys)
            logger.info(
                "[HotReload] Reloaded PV interface (%d changed PV keys)",
                len(pending_keys),
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[HotReload] PV live reload failed: %s", exc)
