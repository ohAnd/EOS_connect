"""
Unit tests for the Config Schema Registry.
"""

import pytest
from src.config_web.schema import ConfigSchema, FieldDef


class TestConfigSchema:
    """Tests for ConfigSchema class."""

    def setup_method(self):
        """Create a fresh schema instance for each test."""
        self.schema = ConfigSchema()

    def test_all_fields_registered(self):
        """All fields should be non-empty after initialization."""
        fields = self.schema.all_fields()
        assert len(fields) > 50, f"Expected 50+ fields, got {len(fields)}"

    def test_get_existing_key(self):
        """Should return FieldDef for a known key."""
        field = self.schema.get("battery.capacity_wh")
        assert field is not None
        assert field.field_type == "int"
        assert field.default == 11059

    def test_get_unknown_key(self):
        """Should return None for unknown key."""
        assert self.schema.get("nonexistent.key") is None

    def test_sections_are_ordered(self):
        """Sections should come back in registration order."""
        sections = self.schema.sections()
        assert len(sections) > 0
        assert "data_source" in sections
        assert "load" in sections
        assert "battery" in sections
        assert "system" in sections

    def test_get_section(self):
        """Should return only fields for the requested section."""
        battery_fields = self.schema.get_section("battery")
        assert len(battery_fields) > 10
        for f in battery_fields:
            assert f.section == "battery"

    def test_get_by_level_getting_started(self):
        """Getting started should only include getting_started fields."""
        fields = self.schema.get_by_level("getting_started")
        for f in fields:
            assert f.level == "getting_started"

    def test_get_by_level_expert(self):
        """Expert should include all fields."""
        expert_fields = self.schema.get_by_level("expert")
        all_fields = self.schema.all_fields()
        assert len(expert_fields) == len(all_fields)

    def test_to_json(self):
        """JSON export should produce list of dicts with correct keys."""
        json_data = self.schema.to_json()
        assert isinstance(json_data, list)
        assert len(json_data) > 0

        first = json_data[0]
        required_keys = {"key", "type", "default", "section", "level", "description"}
        assert required_keys.issubset(first.keys())

    def test_defaults_dict_structure(self):
        """defaults_dict should produce a nested dict matching config shape."""
        defaults = self.schema.defaults_dict()
        assert isinstance(defaults, dict)
        assert "load" in defaults
        assert "battery" in defaults
        assert isinstance(defaults["load"], dict)
        assert "load_sensor" in defaults["load"]
        # Top-level keys
        assert "refresh_time" in defaults
        assert "time_zone" in defaults

    def test_data_source_section_exists(self):
        """The unified data_source section should exist."""
        ds_fields = self.schema.get_section("data_source")
        assert len(ds_fields) == 4  # type, url, access_token, ssl_ignore
        keys = [f.key for f in ds_fields]
        assert "data_source.type" in keys
        assert "data_source.url" in keys
        assert "data_source.access_token" in keys
        assert "data_source.ssl_ignore" in keys

    def test_deprecated_fields_have_label(self):
        """Load/battery connection override fields should be marked deprecated."""
        field = self.schema.get("load.source")
        assert "deprecated" in field.labels
        field = self.schema.get("battery.source")
        assert "deprecated" in field.labels

    def test_hot_reload_fields(self):
        """Certain price/battery fields should be marked hot_reload."""
        assert self.schema.get("price.feed_in_price").hot_reload is True
        assert self.schema.get("battery.min_soc_percentage").hot_reload is True
        assert self.schema.get("battery.battery_price_include_feedin").hot_reload is True
        assert self.schema.get("pv_forecast_source.source").hot_reload is True
        assert self.schema.get("pv_forecast.tilt").hot_reload is True
        # Restart-required fields should NOT be hot_reload
        assert self.schema.get("mqtt.broker").hot_reload is False

    def test_inverter_type_is_offered_regardless_of_data_source(self):
        """
        inverter.type must not depend on the data source.

        Only one of its six choices is Home Assistant; gating the whole select on
        data_source.type == "homeassistant" hid it from everyone else. On a fresh
        install that value is "default", so the setup wizard's Inverter step had
        nothing left to render.
        """
        field = self.schema.get("inverter.type")
        assert field is not None
        assert field.depends_on is None

    def test_home_assistant_inverter_fields_still_depend_on_the_type(self):
        """The dependency belongs on the fields that really are HA-specific."""
        for key in (
            "inverter.charge_from_grid",
            "inverter.avoid_discharge",
            "inverter.discharge_allowed",
        ):
            field = self.schema.get(key)
            assert field.depends_on == {"inverter.type": ["homeassistant"]}, key

    def test_inverter_url_token_removed(self):
        """Old inverter.url and inverter.token fields should not exist."""
        assert self.schema.get("inverter.url") is None, "inverter.url should be removed"
        assert self.schema.get("inverter.token") is None, "inverter.token should be removed"

    def test_inverter_mode_sequence_fields_exist(self):
        """New inverter.*.mode-sequence JSON fields should exist."""
        charge_from_grid = self.schema.get("inverter.charge_from_grid")
        avoid_discharge = self.schema.get("inverter.avoid_discharge")
        discharge_allowed = self.schema.get("inverter.discharge_allowed")

        assert charge_from_grid is not None
        assert avoid_discharge is not None
        assert discharge_allowed is not None

        # All should be json type
        assert charge_from_grid.field_type == "json"
        assert avoid_discharge.field_type == "json"
        assert discharge_allowed.field_type == "json"

        # All should default to empty array
        assert charge_from_grid.default == []
        assert avoid_discharge.default == []
        assert discharge_allowed.default == []

    def test_inverter_mode_sequence_fields_have_dependencies(self):
        """Mode-sequence fields should depend on inverter.type='homeassistant'."""
        charge_from_grid = self.schema.get("inverter.charge_from_grid")
        avoid_discharge = self.schema.get("inverter.avoid_discharge")
        discharge_allowed = self.schema.get("inverter.discharge_allowed")

        for field in [charge_from_grid, avoid_discharge, discharge_allowed]:
            assert field.depends_on is not None
            assert "inverter.type" in field.depends_on
            assert field.depends_on["inverter.type"] == ["homeassistant"]

    def test_inverter_mode_sequence_fields_restart_required(self):
        """Mode-sequence fields should require restart."""
        charge_from_grid = self.schema.get("inverter.charge_from_grid")
        avoid_discharge = self.schema.get("inverter.avoid_discharge")
        discharge_allowed = self.schema.get("inverter.discharge_allowed")

        for field in [charge_from_grid, avoid_discharge, discharge_allowed]:
            assert "restart_required" in field.labels

