"""
Unit tests for the HotReloadAdapter.
"""

from unittest.mock import MagicMock
import time
import pytest

from src.config_web.hot_reload import HotReloadAdapter


@pytest.fixture
def price_interface():
    """Mock PriceInterface with the relevant attributes."""
    mock = MagicMock()
    mock.fixed_price_adder_ct = 0.0
    mock.relative_price_multiplier = 0.0
    mock.feed_in_tariff_price = 0.0
    mock.negative_price_switch = False
    mock.current_prices_direct = [0.1, 0.2, 0.3]
    mock.current_feedin = [0.0, 0.0, 0.0]
    # Make __create_feedin_prices accessible via name mangling
    mock._PriceInterface__create_feedin_prices = MagicMock(return_value=[0.05, 0.05, 0.05])
    return mock


@pytest.fixture
def battery_interface():
    """Mock BatteryInterface with set_min_soc / set_max_soc."""
    mock = MagicMock()
    mock.min_soc_set = 5
    mock.max_soc_set = 100
    mock.battery_data = {"min_soc_percentage": 5, "max_soc_percentage": 100}
    return mock


@pytest.fixture
def pv_interface():
    """Mock PvInterface exposing reload_config."""
    mock = MagicMock()
    mock.reload_config = MagicMock()
    return mock


@pytest.fixture
def merged_config_provider():
    """Return a callable config provider used by PV hot reload."""
    config = {
        "pv_forecast_source": {"source": "akkudoktor", "api_key": ""},
        "pv_forecast": [{"name": "RoofA", "lat": 47.5, "lon": 8.5}],
        "evcc": {"url": "http://evcc:7070"},
        "eos": {"source": "eos_server"},
        "time_zone": "Europe/Berlin",
    }

    def _provider():
        return config

    return _provider


@pytest.fixture
def adapter(price_interface, battery_interface):
    """HotReloadAdapter wired to mocked interfaces."""
    return HotReloadAdapter(
        price_interface=price_interface,
        battery_interface=battery_interface,
    )


class TestHotReloadPrice:
    """Tests for price hot-reload."""

    def test_fixed_price_adder(self, adapter, price_interface):
        """Changing fixed_price_adder_ct should update the interface attr."""
        adapter.on_config_changed("price.fixed_price_adder_ct", 0.0, 2.5)
        assert price_interface.fixed_price_adder_ct == 2.5
        assert "price.fixed_price_adder_ct" in adapter.last_applied

    def test_relative_multiplier(self, adapter, price_interface):
        """Changing relative_price_multiplier should update the interface attr."""
        adapter.on_config_changed("price.relative_price_multiplier", 0.0, 0.05)
        assert price_interface.relative_price_multiplier == 0.05

    def test_feed_in_price(self, adapter, price_interface):
        """Changing feed_in_price should update attr and recalculate feed-in."""
        adapter.on_config_changed("price.feed_in_price", 0.0, 0.08)
        assert price_interface.feed_in_tariff_price == 0.08
        price_interface._PriceInterface__create_feedin_prices.assert_called_once()

    def test_negative_price_switch(self, adapter, price_interface):
        """Changing negative_price_switch should update attr and recalculate feed-in."""
        adapter.on_config_changed("price.negative_price_switch", False, True)
        assert price_interface.negative_price_switch is True
        price_interface._PriceInterface__create_feedin_prices.assert_called_once()

    def test_non_feedin_field_no_recalc(self, adapter, price_interface):
        """Changing a non-feedin price field should NOT recalculate feed-in."""
        adapter.on_config_changed("price.fixed_price_adder_ct", 0.0, 1.0)
        price_interface._PriceInterface__create_feedin_prices.assert_not_called()

    def test_invalid_value_coercion(self, adapter, price_interface):
        """Non-numeric value for a float field should be handled gracefully."""
        adapter.on_config_changed("price.feed_in_price", 0.0, "invalid")
        # Should not crash; value should NOT be updated
        assert price_interface.feed_in_tariff_price == 0.0
        assert adapter.last_applied == []


class TestHotReloadBattery:
    """Tests for battery SOC hot-reload."""

    def test_min_soc(self, adapter, battery_interface):
        """Changing min_soc_percentage should call set_min_soc()."""
        adapter.on_config_changed("battery.min_soc_percentage", 5, 10)
        battery_interface.set_min_soc.assert_called_once_with(10)
        assert "battery.min_soc_percentage" in adapter.last_applied

    def test_max_soc(self, adapter, battery_interface):
        """Changing max_soc_percentage should call set_max_soc()."""
        adapter.on_config_changed("battery.max_soc_percentage", 100, 90)
        battery_interface.set_max_soc.assert_called_once_with(90)
        assert "battery.max_soc_percentage" in adapter.last_applied

    def test_invalid_soc_value(self, adapter, battery_interface):
        """Non-integer SOC value should be handled gracefully."""
        adapter.on_config_changed("battery.min_soc_percentage", 5, "bad")
        battery_interface.set_min_soc.assert_not_called()
        assert adapter.last_applied == []


class TestHotReloadGeneral:
    """Tests for general hot-reload adapter behavior."""

    def test_unknown_key_ignored(self, adapter):
        """Unknown keys should be silently ignored."""
        adapter.on_config_changed("mqtt.broker", "old", "new")
        assert adapter.last_applied == []

    def test_no_interface_no_crash(self):
        """Adapter with no interfaces should handle all keys without error."""
        adapter = HotReloadAdapter(price_interface=None, battery_interface=None)
        adapter.on_config_changed("price.feed_in_price", 0.0, 0.1)
        adapter.on_config_changed("battery.min_soc_percentage", 5, 10)
        assert adapter.last_applied == []

    def test_last_applied_resets(self, adapter, price_interface):
        """last_applied should reset on each callback invocation."""
        adapter.on_config_changed("price.fixed_price_adder_ct", 0.0, 1.0)
        assert len(adapter.last_applied) == 1
        adapter.on_config_changed("mqtt.broker", "a", "b")
        assert adapter.last_applied == []


class TestHotReloadPv:
    """Tests for PV source/entry hot-reload behavior."""

    def test_pv_source_reload_applies_live(self, pv_interface, merged_config_provider):
        """Changing PV source key should reload PvInterface from merged config."""
        adapter = HotReloadAdapter(
            pv_interface=pv_interface,
            config_provider=merged_config_provider,
            pv_reload_debounce_seconds=0,
        )

        adapter.on_config_changed("pv_forecast_source.source", "evcc", "akkudoktor")

        pv_interface.reload_config.assert_called_once_with(
            config_source={"source": "akkudoktor", "api_key": ""},
            config=[{"name": "RoofA", "lat": 47.5, "lon": 8.5}],
            config_special={"url": "http://evcc:7070"},
            temperature_forecast_enabled=True,
            timezone="Europe/Berlin",
        )
        assert "pv_forecast_source.source" in adapter.last_applied

    def test_pv_changes_are_debounced_to_single_reload(
        self,
        pv_interface,
        merged_config_provider,
    ):
        """Multiple PV key updates in one save should trigger one reload."""
        adapter = HotReloadAdapter(
            pv_interface=pv_interface,
            config_provider=merged_config_provider,
            pv_reload_debounce_seconds=0.02,
        )

        adapter.on_config_changed("pv_forecast.0.lat", 47.0, 47.5)
        adapter.on_config_changed("pv_forecast.0.lon", 8.0, 8.5)
        time.sleep(0.08)

        pv_interface.reload_config.assert_called_once()
        assert "pv_forecast.0.lat" in adapter.last_applied
        assert "pv_forecast.0.lon" in adapter.last_applied
