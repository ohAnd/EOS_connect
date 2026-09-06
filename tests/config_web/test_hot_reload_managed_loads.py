"""Live configuration changes reaching the managed load manager."""

import pytest
from zoneinfo import ZoneInfo

from src.config_web.hot_reload import HotReloadAdapter
from src.loads.manager import ManagedLoadManager

BERLIN = ZoneInfo("Europe/Berlin")

POOL = {
    "id": "pool", "type": "pool_heatpump", "enabled": True,
    "temp_sensor": "sensor.pool", "target_temp": 28.0, "min_runtime_minutes": 30,
}


def _adapter(manager, config):
    return HotReloadAdapter(
        load_manager=manager,
        config_provider=lambda: config,
    )


@pytest.fixture(name="manager")
def manager_fixture():
    return ManagedLoadManager([dict(POOL)], time_frame_base=3600, time_zone=BERLIN)


def test_a_changed_target_temperature_reaches_the_model(manager):
    config = {"managed_loads": [dict(POOL, target_temp=31.0)], "load": {}}
    _adapter(manager, config).on_config_changed(
        "managed_loads.0.target_temp", 28.0, 31.0
    )
    assert manager.instance("pool").config["target_temp"] == 31.0


def test_a_changed_minimum_runtime_reaches_the_gate(manager):
    config = {"managed_loads": [dict(POOL, min_runtime_minutes=5)], "load": {}}
    _adapter(manager, config).on_config_changed(
        "managed_loads.0.min_runtime_minutes", 30, 5
    )
    assert manager.instance("pool").gate.min_runtime_minutes == 5


def test_disabling_a_load_takes_effect_without_a_restart(manager):
    config = {"managed_loads": [dict(POOL, enabled=False)], "load": {}}
    _adapter(manager, config).on_config_changed("managed_loads.0.enabled", True, False)
    assert manager.instance("pool").enabled is False


def test_the_shared_power_budget_is_hot_reloadable(manager):
    config = {"managed_loads": [dict(POOL)], "load": {"managed_loads_max_power_w": 4200}}
    _adapter(manager, config).on_config_changed(
        "load.managed_loads_max_power_w", 0, 4200
    )
    assert manager.max_power_w == 4200.0


def test_an_unreadable_budget_is_ignored_rather_than_applied(manager):
    config = {"managed_loads": [], "load": {"managed_loads_max_power_w": "lots"}}
    _adapter(manager, config).on_config_changed(
        "load.managed_loads_max_power_w", 0, "lots"
    )
    assert manager.max_power_w == 0.0


def test_a_change_for_an_unknown_entry_is_ignored(manager):
    config = {"managed_loads": [{"id": "ghost", "type": "sauna"}], "load": {}}
    _adapter(manager, config).on_config_changed("managed_loads.0.target_temp", 1, 2)
    assert manager.instance("pool").config["target_temp"] == 28.0


def test_nothing_happens_without_a_manager():
    """The adapter is constructed before managed loads exist in some code paths."""
    adapter = HotReloadAdapter(config_provider=lambda: {"managed_loads": []})
    adapter.on_config_changed("managed_loads.0.target_temp", 1, 2)   # must not raise


def test_a_missing_config_provider_is_survivable(manager):
    adapter = HotReloadAdapter(load_manager=manager, config_provider=None)
    adapter.on_config_changed("managed_loads.0.target_temp", 1, 2)
    assert manager.instance("pool").config["target_temp"] == 28.0
