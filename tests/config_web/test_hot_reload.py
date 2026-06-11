"""
Unit tests for the HotReloadAdapter.
"""
# pylint: disable=redefined-outer-name

from unittest.mock import MagicMock
import time
from zoneinfo import ZoneInfo
import pytest

from src.config_web.hot_reload import HotReloadAdapter


@pytest.fixture
def price_interface():
    """Mock PriceInterface with the relevant attributes."""
    mock = MagicMock()
    mock.fixed_price_adder_ct = 0.0
    mock.relative_price_multiplier = 0.0
    mock.feed_in_tariff_price = 0.0
    mock.recalculate_feedin_prices = MagicMock(return_value=[0.05, 0.05, 0.05])
    return mock


@pytest.fixture
def feed_in_price_interface():
    """Mock FeedInPriceInterface with negative_price_switch."""
    mock = MagicMock()
    mock.negative_price_switch = False
    mock.get_current_feedin_prices = MagicMock(return_value=[0.05, 0.05, 0.05])
    return mock


@pytest.fixture
def battery_interface():
    """Mock BatteryInterface with set_min_soc / set_max_soc."""
    mock = MagicMock()
    mock.min_soc_set = 5
    mock.max_soc_set = 100
    mock.battery_data = {"min_soc_percentage": 5, "max_soc_percentage": 100}
    mock.price_handler = MagicMock()
    mock.price_handler.battery_price_include_feedin = False
    mock.price_handler.charging_threshold_w = 50.0
    mock.price_handler.grid_charge_threshold_w = 100.0
    mock.price_handler.pv_cost_euro_per_kwh = 0.0
    mock.price_handler.last_price_calculation = object()
    return mock


@pytest.fixture
def pv_interface():
    """Mock PvInterface exposing reload_config."""
    mock = MagicMock()
    mock.reload_config = MagicMock()
    return mock


@pytest.fixture
def optimization_interface():
    """Mock OptimizationInterface with hot-reloadable attributes."""
    mock = MagicMock()
    mock.timeout = 180
    mock.dyn_override_discharge_allowed = False
    mock.pv_battery_charge_control_enabled = False
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
def adapter(price_interface, battery_interface, feed_in_price_interface):
    """HotReloadAdapter wired to mocked interfaces."""
    return HotReloadAdapter(
        price_interface=price_interface,
        battery_interface=battery_interface,
        feed_in_price_interface=feed_in_price_interface,
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
        price_interface.recalculate_feedin_prices.assert_called_once()

    def test_feed_in_price_fires_run_trigger(self, price_interface, battery_interface):
        """Changing feed_in_price should also trigger an immediate optimization run."""
        trigger = MagicMock()
        adapter = HotReloadAdapter(
            price_interface=price_interface,
            battery_interface=battery_interface,
            on_run_trigger=trigger,
        )
        adapter.on_config_changed("price.feed_in_price", 0.0, 0.08)
        trigger.assert_called_once()

    def test_fixed_price_adder_does_not_fire_run_trigger(self, price_interface, battery_interface):
        """Changing fixed_price_adder_ct should NOT trigger an immediate run."""
        trigger = MagicMock()
        adapter = HotReloadAdapter(
            price_interface=price_interface,
            battery_interface=battery_interface,
            on_run_trigger=trigger,
        )
        adapter.on_config_changed("price.fixed_price_adder_ct", 0.0, 1.0)
        trigger.assert_not_called()

    def test_negative_price_switch(self, adapter, feed_in_price_interface):
        """Changing feed_in_negative_price_switch should update attr and recalculate feed-in."""
        adapter.on_config_changed("price.feed_in_negative_price_switch", False, True)
        assert feed_in_price_interface.negative_price_switch is True

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

    def test_battery_price_include_feedin(self, adapter, battery_interface):
        """Changing include_feedin should update BatteryPriceHandler live."""
        adapter.on_config_changed(
            "battery.battery_price_include_feedin",
            False,
            True,
        )
        assert battery_interface.price_handler.battery_price_include_feedin is True
        assert battery_interface.price_handler.last_price_calculation is None
        assert "battery.battery_price_include_feedin" in adapter.last_applied

    def test_battery_charging_threshold(self, adapter, battery_interface):
        """Changing charging threshold should update BatteryPriceHandler live."""
        adapter.on_config_changed("battery.charging_threshold_w", 50.0, 75.0)
        assert battery_interface.price_handler.charging_threshold_w == 75.0
        assert battery_interface.price_handler.last_price_calculation is None
        assert "battery.charging_threshold_w" in adapter.last_applied

    def test_battery_grid_charge_threshold(self, adapter, battery_interface):
        """Changing grid charge threshold should update BatteryPriceHandler live."""
        adapter.on_config_changed("battery.grid_charge_threshold_w", 100.0, 150.0)
        assert battery_interface.price_handler.grid_charge_threshold_w == 150.0
        assert battery_interface.price_handler.last_price_calculation is None
        assert "battery.grid_charge_threshold_w" in adapter.last_applied


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

    def test_last_applied_resets(self, adapter, price_interface):  # pylint: disable=unused-argument
        """last_applied should reset on each callback invocation."""
        adapter.on_config_changed("price.fixed_price_adder_ct", 0.0, 1.0)
        assert len(adapter.last_applied) == 1
        adapter.on_config_changed("mqtt.broker", "a", "b")
        assert adapter.last_applied == []

    def test_feed_in_price_updates_battery_price_handler(
        self,
        adapter,
        price_interface,
        battery_interface,
    ):
        """Changing feed_in_price should propagate to BatteryPriceHandler live."""
        adapter.on_config_changed("price.feed_in_price", 0.0, 0.08)
        assert price_interface.feed_in_tariff_price == 0.08
        assert battery_interface.price_handler.pv_cost_euro_per_kwh == 0.08
        assert battery_interface.price_handler.last_price_calculation is None


@pytest.fixture
def feed_in_price_interface():
    """Mock FeedInPriceInterface with hot-reloadable attributes."""
    mock = MagicMock()
    mock.static_adder_ct_kwh = 0.0
    mock.multiplier = 1.0
    mock.time_zone = ZoneInfo("Europe/Berlin")
    mock.time_frame_base = 3600
    mock.update_prices = MagicMock()
    return mock


class TestHotReloadFeedInPrice:
    """Tests for feed-in price interface hot-reload."""

    def test_static_adder_change(self, feed_in_price_interface):
        """Changing feed_in_static_adder should update attr and recalculate."""
        adapter = HotReloadAdapter(feed_in_price_interface=feed_in_price_interface)
        adapter.on_config_changed("price.feed_in_static_adder", 0.0, 1.5)
        assert feed_in_price_interface.static_adder_ct_kwh == 1.5
        feed_in_price_interface.update_prices.assert_called_once()
        assert "price.feed_in_static_adder" in adapter.last_applied

    def test_static_adder_fires_run_trigger(self, feed_in_price_interface):
        """Changing feed_in_static_adder should trigger an immediate optimization run."""
        trigger = MagicMock()
        adapter = HotReloadAdapter(
            feed_in_price_interface=feed_in_price_interface,
            on_run_trigger=trigger,
        )
        adapter.on_config_changed("price.feed_in_static_adder", 0.0, 2.0)
        trigger.assert_called_once()

    def test_multiplier_does_not_fire_run_trigger(self, feed_in_price_interface):
        """Changing feed_in_multiplier should NOT trigger an immediate run."""
        trigger = MagicMock()
        adapter = HotReloadAdapter(
            feed_in_price_interface=feed_in_price_interface,
            on_run_trigger=trigger,
        )
        adapter.on_config_changed("price.feed_in_multiplier", 1.0, 1.1)
        trigger.assert_not_called()

    def test_no_feed_in_interface_no_crash(self):
        """Missing feed-in price interface should be handled silently."""
        adapter = HotReloadAdapter(feed_in_price_interface=None)
        adapter.on_config_changed("price.feed_in_static_adder", 0.0, 1.5)
        assert adapter.last_applied == []

    def test_feed_in_price_syncs_fixed_price_ct_kwh(self, feed_in_price_interface):
        """price.feed_in_price hot-reload must update FeedInPriceInterface.fixed_price_ct_kwh.

        The optimizer reads feed_in_price_interface.get_current_feedin_prices(), not
        price_interface.feed_in_tariff_price, so the FeedInPriceInterface must be kept
        in sync when the fixed feed-in price changes.
        """
        feed_in_price_interface.fixed_price_ct_kwh = 0.0
        price_mock = MagicMock()
        price_mock.src = "fixed"
        price_mock.time_zone = ZoneInfo("Europe/Berlin")
        price_mock.recalculate_feedin_prices = MagicMock(return_value=[])
        adapter = HotReloadAdapter(
            price_interface=price_mock,
            feed_in_price_interface=feed_in_price_interface,
        )
        adapter.on_config_changed("price.feed_in_price", 0.0, 8.0)

        assert feed_in_price_interface.fixed_price_ct_kwh == 8.0
        feed_in_price_interface.update_prices.assert_called_once()


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


class TestHotReloadOptimizer:
    """Tests for optimizer hot-reload."""

    def test_timeout_change(self, optimization_interface):
        """Changing eos.timeout should update the interface attr."""
        adapter = HotReloadAdapter(optimization_interface=optimization_interface)
        adapter.on_config_changed("eos.timeout", 180, 240)
        assert optimization_interface.timeout == 240
        assert "eos.timeout" in adapter.last_applied

    def test_dyn_override_change(self, optimization_interface):
        """Changing dyn_override flag should update the interface attr."""
        adapter = HotReloadAdapter(optimization_interface=optimization_interface)
        adapter.on_config_changed(
            "eos.dyn_override_discharge_allowed_pv_greater_load", False, True
        )
        assert optimization_interface.dyn_override_discharge_allowed is True
        assert "eos.dyn_override_discharge_allowed_pv_greater_load" in adapter.last_applied

    def test_pv_battery_charge_control_change(self, optimization_interface):
        """Changing pv_battery_charge_control_enabled should update the interface attr."""
        adapter = HotReloadAdapter(optimization_interface=optimization_interface)
        adapter.on_config_changed("eos.pv_battery_charge_control_enabled", False, True)
        assert optimization_interface.pv_battery_charge_control_enabled is True
        assert "eos.pv_battery_charge_control_enabled" in adapter.last_applied

    def test_timeout_type_coercion(self, optimization_interface):
        """Timeout should be coerced to int."""
        adapter = HotReloadAdapter(optimization_interface=optimization_interface)
        adapter.on_config_changed("eos.timeout", 180, "250")
        assert optimization_interface.timeout == 250
        assert isinstance(optimization_interface.timeout, int)

    def test_invalid_timeout_value(self, optimization_interface):
        """Non-numeric timeout value should be handled gracefully."""
        adapter = HotReloadAdapter(optimization_interface=optimization_interface)
        adapter.on_config_changed("eos.timeout", 180, "invalid")
        assert optimization_interface.timeout == 180
        assert adapter.last_applied == []

    def test_no_optimizer_interface_no_crash(self):
        """Adapter with no optimizer interface should handle keys without error."""
        adapter = HotReloadAdapter(optimization_interface=None)
        adapter.on_config_changed("eos.timeout", 180, 240)
        adapter.on_config_changed("eos.dyn_override_discharge_allowed_pv_greater_load", False, True)
        assert adapter.last_applied == []

    def test_dyn_override_fires_run_trigger(self, optimization_interface):
        """Changing dyn_override flag should also trigger an immediate run."""
        trigger = MagicMock()
        adapter = HotReloadAdapter(
            optimization_interface=optimization_interface,
            on_run_trigger=trigger,
        )
        adapter.on_config_changed(
            "eos.dyn_override_discharge_allowed_pv_greater_load", False, True
        )
        assert optimization_interface.dyn_override_discharge_allowed is True
        trigger.assert_called_once()

    def test_timeout_does_not_fire_run_trigger(self, optimization_interface):
        """Changing eos.timeout should NOT trigger an immediate run."""
        trigger = MagicMock()
        adapter = HotReloadAdapter(
            optimization_interface=optimization_interface,
            on_run_trigger=trigger,
        )
        adapter.on_config_changed("eos.timeout", 180, 240)
        trigger.assert_not_called()


@pytest.fixture
def local_evopt_backend():
    """Mock LocalEVOptBackend with hot-reloadable strategy attributes."""
    mock = MagicMock()
    mock.charging_strategy = "charge_before_export"
    mock.discharging_strategy = "discharge_before_import"
    mock.emergency_reserve_pct = 0
    return mock


@pytest.fixture
def optimization_interface_local(local_evopt_backend):
    """Mock OptimizationInterface configured with local_evopt backend."""
    mock = MagicMock()
    mock.timeout = 180
    mock.backend_type = "local_evopt"
    mock.backend = local_evopt_backend
    return mock


class TestHotReloadLocalEVopt:
    """Tests for local_evopt strategy hot-reload."""

    def test_charging_strategy_change(self, optimization_interface_local, local_evopt_backend):
        """Changing charging strategy should update backend attr."""
        adapter = HotReloadAdapter(optimization_interface=optimization_interface_local)
        adapter.on_config_changed(
            "eos.local_evopt_charging_strategy", "charge_before_export", "maximize_self_consumption"
        )
        assert local_evopt_backend.charging_strategy == "maximize_self_consumption"
        assert "eos.local_evopt_charging_strategy" in adapter.last_applied

    def test_discharging_strategy_change(self, optimization_interface_local, local_evopt_backend):
        """Changing discharging strategy should update backend attr."""
        adapter = HotReloadAdapter(optimization_interface=optimization_interface_local)
        adapter.on_config_changed(
            "eos.local_evopt_discharging_strategy", "discharge_before_import", "emergency_reserve"
        )
        assert local_evopt_backend.discharging_strategy == "emergency_reserve"
        assert "eos.local_evopt_discharging_strategy" in adapter.last_applied

    def test_emergency_reserve_pct_change(self, optimization_interface_local, local_evopt_backend):
        """Changing emergency_reserve_pct should update backend attr and clamp to 0-80."""
        adapter = HotReloadAdapter(optimization_interface=optimization_interface_local)
        adapter.on_config_changed("eos.local_evopt_emergency_reserve_pct", 0, 20)
        assert local_evopt_backend.emergency_reserve_pct == 20

        # Clamp above 80
        adapter.on_config_changed("eos.local_evopt_emergency_reserve_pct", 20, 99)
        assert local_evopt_backend.emergency_reserve_pct == 80

        # Clamp below 0
        adapter.on_config_changed("eos.local_evopt_emergency_reserve_pct", 80, -5)
        assert local_evopt_backend.emergency_reserve_pct == 0

    def test_run_trigger_called_on_strategy_change(
        self, optimization_interface_local, local_evopt_backend  # pylint: disable=unused-argument
    ):
        """on_run_trigger must be called after a local_evopt strategy hot-reload."""
        trigger = MagicMock()
        adapter = HotReloadAdapter(
            optimization_interface=optimization_interface_local,
            on_run_trigger=trigger,
        )
        adapter.on_config_changed(
            "eos.local_evopt_charging_strategy", "charge_before_export", "none"
        )
        trigger.assert_called_once()

    def test_run_trigger_not_called_for_unrelated_key(self, optimization_interface_local):
        """on_run_trigger must not fire for keys unrelated to local_evopt strategies."""
        trigger = MagicMock()
        adapter = HotReloadAdapter(
            optimization_interface=optimization_interface_local,
            on_run_trigger=trigger,
        )
        adapter.on_config_changed("eos.timeout", 180, 240)
        trigger.assert_not_called()

    def test_run_trigger_exception_does_not_propagate(
        self, optimization_interface_local, local_evopt_backend
    ):
        """A crash in on_run_trigger must not abort the hot-reload."""
        def bad_trigger():
            raise RuntimeError("scheduler exploded")

        adapter = HotReloadAdapter(
            optimization_interface=optimization_interface_local,
            on_run_trigger=bad_trigger,
        )
        # Should not raise
        adapter.on_config_changed(
            "eos.local_evopt_charging_strategy", "charge_before_export", "none"
        )
        assert local_evopt_backend.charging_strategy == "none"

    def test_wrong_backend_type_skipped(self):
        """Keys should be ignored when backend_type is not local_evopt."""
        mock_opt = MagicMock()
        mock_opt.backend_type = "eos_server"
        adapter = HotReloadAdapter(optimization_interface=mock_opt)
        adapter.on_config_changed(
            "eos.local_evopt_charging_strategy", "charge_before_export", "none"
        )
        assert adapter.last_applied == []

    def test_no_optimizer_no_crash(self):
        """local_evopt keys with no optimizer interface should be silently ignored."""
        adapter = HotReloadAdapter(optimization_interface=None)
        adapter.on_config_changed(
            "eos.local_evopt_charging_strategy", "charge_before_export", "none"
        )
        assert adapter.last_applied == []
