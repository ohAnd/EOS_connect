"""
Hot-Reload Adapter — Applies live configuration changes to running interfaces.

Registered as a change callback on ConfigStore. When a hot-reloadable value
is changed via the web UI, this adapter updates the running interface
instance directly, avoiding a full restart.

Supported fields (Priority 1 — Price):
- ``price.fixed_price_adder_ct``
- ``price.relative_price_multiplier``
- ``price.feed_in_price``
- ``price.negative_price_switch``

Supported fields (Priority 2 — Battery SOC):
- ``battery.min_soc_percentage``
- ``battery.max_soc_percentage``
"""

import logging
import threading

logger = logging.getLogger("__main__")


# Map of config store keys to (interface_attr_name, coerce_fn)
_PRICE_FIELD_MAP = {
    "price.fixed_price_adder_ct": ("fixed_price_adder_ct", float),
    "price.relative_price_multiplier": ("relative_price_multiplier", float),
    "price.feed_in_price": ("feed_in_tariff_price", float),
    "price.negative_price_switch": ("negative_price_switch", bool),
}

_BATTERY_SOC_FIELDS = {
    "battery.min_soc_percentage",
    "battery.max_soc_percentage",
}

# Feed-in related fields that require recalculating feed-in prices
_FEEDIN_TRIGGERS = {
    "price.feed_in_price",
    "price.negative_price_switch",
}

_PV_KEY_PREFIXES = (
    "pv_forecast_source.",
    "pv_forecast.",
)


class HotReloadAdapter:
    """
    Applies config changes to live interface instances.

    Args:
        price_interface: Running PriceInterface instance (or None).
        battery_interface: Running BatteryInterface instance (or None).
    """

    def __init__(
        self,
        price_interface=None,
        battery_interface=None,
        pv_interface=None,
        config_provider=None,
        pv_reload_debounce_seconds=0.3,
    ):
        self._price = price_interface
        self._battery = battery_interface
        self._pv = pv_interface
        self._config_provider = config_provider
        self._pv_reload_debounce_seconds = pv_reload_debounce_seconds
        self._pv_reload_timer = None
        self._pv_reload_lock = threading.Lock()
        self._pending_pv_keys = set()
        self._applied_keys = []

    @property
    def last_applied(self):
        """List of keys applied in the most recent callback invocation."""
        return list(self._applied_keys)

    def on_config_changed(self, key, old_value, new_value):
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
        elif key in _BATTERY_SOC_FIELDS:
            self._apply_battery_soc(key, new_value)
        elif key.startswith(_PV_KEY_PREFIXES):
            self._schedule_pv_reload(key)
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

        # Recalculate feed-in prices when feed_in_price or negative_price_switch change
        if key in _FEEDIN_TRIGGERS:
            self._recalculate_feedin()

    def _recalculate_feedin(self):
        """Recalculate feed-in prices on the price interface."""
        if self._price is None:
            return
        try:
            # Access the private method via name mangling
            feedin = self._price._PriceInterface__create_feedin_prices()
            if feedin is not None:
                logger.info(
                    "[HotReload] Recalculated feed-in prices (%d entries)",
                    len(feedin),
                )
        except (AttributeError, TypeError) as exc:
            logger.debug(
                "[HotReload] Could not recalculate feed-in prices: %s", exc
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

    def _schedule_pv_reload(self, key):
        """Debounce PV reload to avoid one reload per updated PV field."""
        if self._pv is None or self._config_provider is None:
            logger.debug("[HotReload] No PV interface/config provider — skipping %s", key)
            return

        # Support explicit synchronous mode for deterministic tests.
        if self._pv_reload_debounce_seconds <= 0:
            self._pending_pv_keys.add(key)
            self._apply_pv_reload()
            return

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

    def _apply_pv_reload(self):
        """Reconfigure the live PV interface from the current merged config."""
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

        try:
            self._pv.reload_config(
                config_source=config.get("pv_forecast_source", {}),
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
