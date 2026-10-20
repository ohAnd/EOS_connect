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
        # type, url, access_token, ssl_ignore + the four supplementary-connection
        # fields — eight since EVCC became a source type with HA at its side.
        assert len(ds_fields) == 8
        keys = [f.key for f in ds_fields]
        assert "data_source.type" in keys
        assert "data_source.url" in keys
        assert "data_source.access_token" in keys
        assert "data_source.ssl_ignore" in keys
        assert "data_source.supplementary_type" in keys

    def test_no_evcc_series_is_named_from_the_data_source_page(self):
        """EVCC's loadpoints and consumers carry titles their owner chose.

        Two fields here once built "group:loadpoint:<title>" and
        "group:consumer:<title>" strings for the load section. The loadpoint one
        went with the wallbox subtraction; the consumer one only saved typing a
        prefix, at the price of a field in one section silently deciding the
        value of a field in another. The name belongs where it is used.
        """
        keys = [f.key for f in self.schema.all_fields()]
        assert "data_source.evcc_loadpoint_title" not in keys
        assert "data_source.evcc_consumer_title" not in keys

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
        assert (
            self.schema.get("battery.battery_price_include_feedin").hot_reload is True
        )
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

    def test_the_wallbox_sensor_is_not_offered_for_evcc(self):
        """EVCC's ``group:home`` already excludes every loadpoint.

        Offering the wallbox field there invites a second subtraction of energy
        the base load never carried. The gate has to name Home Assistant and
        openHAB explicitly - putting REMOTE_DATA_SOURCE_TYPES back would pull
        EVCC in with them, which is how the double subtraction got in.
        """
        field = self.schema.get("load.car_charge_load_sensor")
        assert field is not None
        assert field.depends_on == {
            "data_source.type": ["homeassistant", "openhab"]
        }, field.depends_on
        assert "evcc_managed" not in (field.labels or [])

    def test_inverter_url_token_removed(self):
        """Old inverter.url and inverter.token fields should not exist."""
        assert self.schema.get("inverter.url") is None, "inverter.url should be removed"
        assert (
            self.schema.get("inverter.token") is None
        ), "inverter.token should be removed"

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


class TestPvForecastApiKey:
    """The shared api_key field also serves Forecast.Solar (issue #288)."""

    def setup_method(self):
        """Create a fresh schema instance for each test."""
        self.schema = ConfigSchema()

    def test_api_key_is_offered_for_forecast_solar(self):
        """Forecast.Solar takes a key as its first URL path segment, so expose it."""
        field = self.schema.get("pv_forecast_source.api_key")
        sources = field.depends_on["pv_forecast_source.source"]
        assert set(sources) == {"solcast", "victron", "forecast_solar"}

    def test_api_key_stays_a_password_field(self):
        """It is a credential for every source that uses it."""
        assert self.schema.get("pv_forecast_source.api_key").field_type == "password"

    def test_api_key_is_not_required_for_forecast_solar(self):
        """The public tier works without one, so the field must stay optional."""
        field = self.schema.get("pv_forecast_source.api_key")
        assert not (field.validation or {}).get("required")

    def test_description_is_resolved_per_source(self):
        """Each provider gets wording that says whether the key is required."""
        resolve = self.schema.get_resolved_description
        solcast = resolve(
            "pv_forecast_source.api_key", {"pv_forecast_source.source": "solcast"}
        )
        forecast_solar = resolve(
            "pv_forecast_source.api_key",
            {"pv_forecast_source.source": "forecast_solar"},
        )
        assert "required" in solcast.lower()
        assert "optional" in forecast_solar.lower()
        assert forecast_solar != solcast


class TestFeedInAdjustmentVisibility:
    """
    Every feed-in source applies (price + adder) * multiplier, so the UI has to
    offer both fields for every source. They were gated to two, which made the
    value invisible and uneditable on the three sources it also applied to.
    """

    SOURCES = ["fixed", "elpris_dk", "epex_spot", "evcc", "timeseries"]

    @pytest.mark.parametrize(
        "key", ["price.feed_in_static_adder", "price.feed_in_multiplier"]
    )
    def test_the_field_is_offered_for_every_feed_in_source(self, key):
        schema = ConfigSchema()
        field = schema.get(key)

        assert field is not None
        assert sorted(field.depends_on["price.feed_in_source"]) == sorted(self.SOURCES)

    @pytest.mark.parametrize(
        "key", ["price.feed_in_static_adder", "price.feed_in_multiplier"]
    )
    def test_each_source_gets_its_own_description(self, key):
        """description_map and depends_on are kept in lock-step, house style."""
        schema = ConfigSchema()
        field = schema.get(key)

        described = field.description_map["price.feed_in_source"]
        assert sorted(described) == sorted(self.SOURCES)
        for source in self.SOURCES:
            resolved = schema.get_resolved_description(
                key, {"price.feed_in_source": source}
            )
            assert resolved == described[source]

    def test_the_gate_matches_the_declared_source_list(self):
        """If a sixth feed-in source is added, these fields must follow it."""
        schema = ConfigSchema()
        choices = schema.get("price.feed_in_source").validation["choices"]

        for key in ("price.feed_in_static_adder", "price.feed_in_multiplier"):
            gate = schema.get(key).depends_on["price.feed_in_source"]
            assert sorted(gate) == sorted(choices)
