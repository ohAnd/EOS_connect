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
        assert len(ds_fields) == 3  # type, url, access_token
        keys = [f.key for f in ds_fields]
        assert "data_source.type" in keys
        assert "data_source.url" in keys
        assert "data_source.access_token" in keys

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
        assert self.schema.get("pv_forecast_source.source").hot_reload is True
        assert self.schema.get("pv_forecast.tilt").hot_reload is True
        # Restart-required fields should NOT be hot_reload
        assert self.schema.get("mqtt.broker").hot_reload is False
