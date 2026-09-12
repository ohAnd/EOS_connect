"""Live configuration changes reaching the managed load manager."""

import json

import pytest
from flask import Flask
from zoneinfo import ZoneInfo

from src.config_web.api import config_bp, init_api
from src.config_web.hot_reload import HotReloadAdapter
from src.config_web.merger import build_merged_config
from src.config_web.schema import ConfigSchema
from src.config_web.store import ConfigStore
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


# --- saving from the config page, end to end -------------------------------------------

class _CachingModule:
    """
    Stand-in for ConfigWebModule that caches the merged config as the real one does.

    The cache is the whole point: ``get_config()`` returns whatever the last
    ``rebuild_config()`` built, so a callback that fires before the rebuild reads the
    *previous* values. A test module that rebuilt on every read would pass while
    production failed.
    """

    def __init__(self, store, schema):
        self._store = store
        self._schema = schema
        self._merged = None
        self._callbacks = []

    def get_config(self):
        if self._merged is None:
            self.rebuild_config()
        return self._merged

    def rebuild_config(self):
        self._merged = build_merged_config({}, self._store, self._schema)

    def notify_config_changed(self, key, old_value, new_value):
        for callback in self._callbacks:
            callback(key, old_value, new_value)

    def register_hot_reload_callback(self, callback):
        self._callbacks.append(callback)
        self._store.register_change_callback(callback)


@pytest.fixture(name="live_save")
def live_save_fixture(tmp_path):
    """A config-page save wired to a running manager, exactly as the app wires it."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    store = ConfigStore(str(tmp_path / "hot.db"))
    store.open()
    for key, value in {
        "managed_loads.0.id": "pool",
        "managed_loads.0.type": "pool_heatpump",
        "managed_loads.0.enabled": True,
        "managed_loads.0.temp_sensor": "sensor.pool",
        "managed_loads.0.target_temp": 28.0,
        "managed_loads.0.max_price_ct_kwh": 22.0,
    }.items():
        store.set(key, value)

    schema = ConfigSchema()
    module = _CachingModule(store, schema)
    init_api(store, schema, module)
    app.register_blueprint(config_bp)

    manager = ManagedLoadManager(
        module.get_config()["managed_loads"], time_frame_base=3600, time_zone=BERLIN
    )
    adapter = HotReloadAdapter(load_manager=manager, config_provider=module.get_config)
    module.register_hot_reload_callback(adapter.on_config_changed)

    with app.test_client() as client:
        def save(values):
            return client.put(
                "/api/config/", data=json.dumps(values), content_type="application/json"
            )
        yield save, manager
    store.close()


def test_a_saved_price_cap_reaches_the_running_planner(live_save):
    """
    The real failure: the page saved ``0`` and the planner kept capping at 22 ct.

    ``ConfigStore.set`` fires the change callbacks the moment the row is written, and
    the section save rebuilt the merged config only *afterwards*. Managed loads are the
    one hot-reload handler that re-reads that merged config rather than taking the new
    value from the callback, so it re-seated the instance from the cache it was about to
    replace - applying the value that was already there.
    """
    save, manager = live_save
    assert manager.instance("pool").max_price_eur_per_wh == pytest.approx(0.00022)

    assert save({"managed_loads.0.max_price_ct_kwh": 0}).status_code == 200

    assert manager.instance("pool").max_price_eur_per_wh is None


def test_a_saved_target_temperature_reaches_the_running_model(live_save):
    """Same ordering, every other managed-load field: not only the price cap."""
    save, manager = live_save
    assert save({"managed_loads.0.target_temp": 31.0}).status_code == 200
    assert manager.instance("pool").config["target_temp"] == 31.0
