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


class HotReloadAdapter:
    """
    Applies config changes to live interface instances.

    Args:
        price_interface: Running PriceInterface instance (or None).
        battery_interface: Running BatteryInterface instance (or None).
    """

    def __init__(self, price_interface=None, battery_interface=None):
        self._price = price_interface
        self._battery = battery_interface
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
