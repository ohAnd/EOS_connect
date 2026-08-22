"""
Hot-reload tests for the PV autoscaler section.

These cover the path a user actually takes: flip a setting in the config UI and expect
the running autoscaler to follow, without a restart.
"""
# pylint: disable=redefined-outer-name

import pytest

from src.config_web.hot_reload import HotReloadAdapter, _PV_AUTOSCALER_FIELD_MAP
from src.interfaces.pv_autoscaler import PvAutoscaler


class InMemoryPvYieldStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def get_latest_record(self):
        return self.rows[-1] if self.rows else None

    def insert_hourly_record(self, **kwargs):
        self.rows.append(kwargs)

    def purge_old_records(self, days=7):
        return 0

    def get_history_last_n_days(self, days=7):
        return list(self.rows)


class FakePvInterface:
    """Minimal stand-in exposing the public accessor hot-reload relies on."""

    def __init__(self, autoscaler):
        self._autoscaler = autoscaler
        self.time_frame_base = 3600

    def get_autoscaler(self):
        return self._autoscaler


@pytest.fixture
def autoscaler():
    scaler = PvAutoscaler(
        {"enabled": False, "retention_days": 7, "sensor_entity_id": "sensor.old"},
        InMemoryPvYieldStore(),
        auto_start=False,
    )
    yield scaler
    scaler.stop_update_service()


@pytest.fixture
def adapter(autoscaler):
    return HotReloadAdapter(pv_interface=FakePvInterface(autoscaler))


def test_enabling_starts_the_collection_thread(adapter, autoscaler):
    """
    The toggle is advertised as hot-reloadable, so it must actually start collecting.

    Setting the flag alone would make apply_scaling start scaling from factors that
    nothing is updating.
    """
    assert autoscaler.is_running() is False

    adapter.on_config_changed("pv_autoscaling.enabled", False, True)

    assert autoscaler.enabled is True
    assert autoscaler.is_running() is True


def test_disabling_stops_the_collection_thread(adapter, autoscaler):
    adapter.on_config_changed("pv_autoscaling.enabled", False, True)
    assert autoscaler.is_running() is True

    adapter.on_config_changed("pv_autoscaling.enabled", True, False)

    assert autoscaler.enabled is False
    assert autoscaler.is_running() is False


def test_sensor_entity_id_is_applied_live(adapter, autoscaler):
    """The most important field of the feature must not silently keep the old value."""
    adapter.on_config_changed("pv_autoscaling.sensor_entity_id", "sensor.old", "sensor.new")
    assert autoscaler.sensor_entity_id == "sensor.new"


@pytest.mark.parametrize(
    "key, attr, value, expected",
    [
        ("pv_autoscaling.retention_days", "retention_days", "10", 10),
        ("pv_autoscaling.min_scale_factor", "min_scale_factor", "0.4", 0.4),
        ("pv_autoscaling.max_scale_factor", "max_scale_factor", "3.0", 3.0),
        ("pv_autoscaling.min_data_hours_required", "min_data_hours_required", "48", 48),
        ("pv_autoscaling.url", "url", "http://ha:8123", "http://ha:8123"),
        ("pv_autoscaling.src", "src", "openhab", "openhab"),
    ],
)
def test_scalar_fields_are_coerced_and_applied(adapter, autoscaler, key, attr, value, expected):
    adapter.on_config_changed(key, None, value)
    assert getattr(autoscaler, attr) == expected


def test_string_false_does_not_enable(adapter, autoscaler):
    """bool("false") is True, so a stored string must not turn a setting on."""
    adapter.on_config_changed("pv_autoscaling.enabled", None, "false")
    assert autoscaler.enabled is False
    assert autoscaler.is_running() is False


def test_every_mapped_field_exists_on_the_autoscaler(autoscaler):
    """Guard against the map drifting from the class it writes to."""
    for attr, _ in _PV_AUTOSCALER_FIELD_MAP.values():
        assert hasattr(autoscaler, attr), f"PvAutoscaler has no attribute {attr!r}"


def test_missing_autoscaler_is_skipped_without_raising():
    adapter = HotReloadAdapter(pv_interface=None)
    adapter.on_config_changed("pv_autoscaling.enabled", False, True)
