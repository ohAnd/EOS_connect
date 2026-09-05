"""
Merger tests for the pv_autoscaling central data-source injection.
"""
# pylint: disable=redefined-outer-name

import pytest

from src.config_web.store import ConfigStore
from src.config_web.schema import ConfigSchema
from src.config_web.migration import migrate_yaml_to_store
from src.config_web.merger import build_merged_config


@pytest.fixture
def schema():
    return ConfigSchema()


@pytest.fixture
def store(tmp_path):
    cfg_store = ConfigStore(str(tmp_path / "config.db"))
    cfg_store.open()
    yield cfg_store
    cfg_store.close()


def _merged(store, schema, **settings):
    migrate_yaml_to_store({}, store, schema)
    for key, value in settings.items():
        store.set(key.replace("__", "."), value)
    return build_merged_config({}, store, schema)


def test_defaults_to_disabled_for_a_fresh_install(store, schema):
    """An experimental feature must never turn itself on."""
    merged = _merged(store, schema)
    assert merged["pv_autoscaling"]["enabled"] is False


def test_section_is_present_even_when_never_configured(store, schema):
    """
    The section is back-filled from schema defaults.

    This is what keeps an upgrade safe: an existing config.yaml has no pv_autoscaling
    keys at all, and the running code reads them unconditionally.
    """
    merged = _merged(store, schema)
    pv_auto = merged["pv_autoscaling"]
    for key in ("enabled", "sensor_entity_id", "src", "retention_days",
                "min_scale_factor", "max_scale_factor", "min_data_hours_required"):
        assert key in pv_auto, f"missing pv_autoscaling.{key}"


def test_central_data_source_is_injected(store, schema):
    merged = _merged(
        store, schema,
        pv_autoscaling__enabled=True,
        pv_autoscaling__use_ha_central_data_source=True,
        data_source__type="homeassistant",
        data_source__url="http://homeassistant.local:8123",
        data_source__access_token="ha_token_789",
        data_source__ssl_ignore=True,
    )

    pv_auto = merged["pv_autoscaling"]
    assert pv_auto["src"] == "homeassistant"
    assert pv_auto["url"] == "http://homeassistant.local:8123"
    assert pv_auto["access_token"] == "ha_token_789"
    assert pv_auto["ssl_ignore"] is True


def test_openhab_central_data_source_is_injected(store, schema):
    merged = _merged(
        store, schema,
        pv_autoscaling__enabled=True,
        pv_autoscaling__use_ha_central_data_source=True,
        data_source__type="openhab",
        data_source__url="http://openhab.local:8080",
    )
    assert merged["pv_autoscaling"]["src"] == "openhab"


def test_unsupported_central_type_is_not_injected_as_src(store, schema):
    """
    data_source.type "default" cannot provide a PV counter.

    Copying it through would write a value pv_autoscaling.src forbids, and the
    autoscaler would raise on every poll behind a once-an-hour warning.
    """
    merged = _merged(
        store, schema,
        pv_autoscaling__enabled=True,
        pv_autoscaling__use_ha_central_data_source=True,
        data_source__type="default",
    )

    src = merged["pv_autoscaling"]["src"]
    assert src in ("homeassistant", "openhab")
    assert src != "default"


def test_manual_mode_keeps_its_own_connection_settings(store, schema):
    merged = _merged(
        store, schema,
        pv_autoscaling__enabled=True,
        pv_autoscaling__use_ha_central_data_source=False,
        pv_autoscaling__url="http://manual.local:8123",
        pv_autoscaling__access_token="manual_token",
        pv_autoscaling__src="openhab",
        data_source__url="http://central.local:8123",
        data_source__access_token="central_token",
    )

    pv_auto = merged["pv_autoscaling"]
    assert pv_auto["url"] == "http://manual.local:8123"
    assert pv_auto["access_token"] == "manual_token"
    assert pv_auto["src"] == "openhab"


def test_schema_defines_the_pv_autoscaling_section(schema):
    """The section and its fields must exist, and every field must gate on `enabled`."""
    keys = {f.key for f in schema.all_fields() if f.key.startswith("pv_autoscaling.")}
    expected = {
        "pv_autoscaling.enabled",
        "pv_autoscaling.use_ha_central_data_source",
        "pv_autoscaling.sensor_entity_id",
        "pv_autoscaling.src",
        "pv_autoscaling.url",
        "pv_autoscaling.access_token",
        "pv_autoscaling.ssl_ignore",
        "pv_autoscaling.retention_days",
        "pv_autoscaling.min_scale_factor",
        "pv_autoscaling.max_scale_factor",
        "pv_autoscaling.min_data_hours_required",
    }
    assert expected <= keys

    assert "pv_autoscaling" in schema.section_meta()

    for field in schema.all_fields():
        if field.key.startswith("pv_autoscaling.") and field.key != "pv_autoscaling.enabled":
            assert field.depends_on and "pv_autoscaling.enabled" in field.depends_on, (
                f"{field.key} should be hidden until the feature is enabled"
            )


def test_every_autoscaling_field_applies_without_a_restart(schema):
    """
    A field that is neither hot-reloadable nor restart_required falls in a gap.

    The API marks such a change as applied, so the user sees a green save, no restart
    banner, and a running autoscaler that never picks the value up.
    """
    for field in schema.all_fields():
        if not field.key.startswith("pv_autoscaling."):
            continue
        labels = field.labels or []
        assert field.hot_reload or "restart_required" in labels, (
            f"{field.key} neither hot-reloads nor asks for a restart"
        )
