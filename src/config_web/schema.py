"""
Config Schema Registry — Single Source of Truth for all configuration field metadata.

Every configuration field in EOS Connect is defined here with its type, default value,
section, disclosure level, validation rules, and documentation. The web UI, REST API,
migration logic, and GitHub Pages documentation all consume this registry.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


# Keys that stay in config.yaml / options.json (bootstrap) — NOT stored in SQLite.
# Includes both config.yaml names and HA addon options.json names.
BOOTSTRAP_KEYS = frozenset({
    "eos_connect_web_port",
    "web_port",
    "time_zone",
    "log_level",
    "data_path",
})


# Section display metadata — single source of truth for icons and labels.
# Consumed by the web UI (config.js), the docs (configuration.html),
# and the JSON export script.
# Order reflects recommended user setup flow (for wizard and settings organization).
SECTION_META = {
    "eos":                {"icon": "fa-server",          "label": "Optimizer"},
    "evcc":               {"icon": "fa-car",             "label": "EVCC"},
    "inverter":           {"icon": "fa-microchip",       "label": "Inverter"},
    "data_source":        {"icon": "fa-plug",            "label": "Data Source"},
    "battery":            {"icon": "fa-battery-full",    "label": "Battery"},
    "load":               {"icon": "fa-bolt",            "label": "Load"},
    "price":              {"icon": "fa-coins",           "label": "Price"},
    "pv_forecast_source": {"icon": "fa-sun",             "label": "PV Source"},
    "pv_forecast":        {"icon": "fa-solar-panel",     "label": "PV Installations"},
    "mqtt":               {"icon": "fa-tower-broadcast",  "label": "MQTT"},
    "system":             {"icon": "fa-gears",           "label": "System"},
}


@dataclass
class FieldDef:
    """Definition of a single configuration field."""

    key: str  # dot-notation path, e.g. "battery.capacity_wh"
    field_type: str  # str, int, float, bool, select, password, sensor, array
    default: Any
    section: str  # top-level group: data_source, load, eos, price, battery, ...
    level: str  # getting_started, standard, expert
    description: str  # short inline help text
    labels: list = field(default_factory=list)  # experimental, deprecated, restart_required, conditional
    help_url: str = ""  # link to GitHub Pages docs anchor
    validation: dict = field(default_factory=dict)  # min, max, pattern, choices, required
    depends_on: Optional[dict] = None  # conditional visibility rules
    hot_reload: bool = False
    display_group: str = ""  # sub-grouping for UI layout


class ConfigSchema:
    """
    Registry of all configuration fields.

    Provides lookup by key, section, level, and export to JSON for the web UI.
    """

    def __init__(self):
        self._fields: dict[str, FieldDef] = {}
        self._register_all()

    def _register_all(self):
        """Register every configuration field."""
        for f in _ALL_FIELDS:
            self._fields[f.key] = f

    def get(self, key: str) -> Optional[FieldDef]:
        """Get a field definition by dot-notation key."""
        return self._fields.get(key)

    def get_section(self, section: str) -> list[FieldDef]:
        """Get all fields for a given section."""
        return [f for f in self._fields.values() if f.section == section]

    def get_by_level(self, level: str) -> list[FieldDef]:
        """Get all fields at or below a given disclosure level."""
        level_order = {"getting_started": 0, "standard": 1, "expert": 2}
        max_level = level_order.get(level, 2)
        return [
            f for f in self._fields.values()
            if level_order.get(f.level, 2) <= max_level
        ]

    def all_fields(self) -> list[FieldDef]:
        """Return all registered fields."""
        return list(self._fields.values())

    def sections(self) -> list[str]:
        """Return ordered list of unique section names."""
        seen = []
        for f in self._fields.values():
            if f.section not in seen:
                seen.append(f.section)
        return seen

    def to_json(self) -> list[dict]:
        """Export the full schema as a JSON-serializable list of dicts."""
        result = []
        for f in self._fields.values():
            result.append({
                "key": f.key,
                "type": f.field_type,
                "default": f.default,
                "section": f.section,
                "level": f.level,
                "description": f.description,
                "labels": f.labels,
                "help_url": f.help_url,
                "validation": f.validation,
                "depends_on": f.depends_on,
                "hot_reload": f.hot_reload,
                "display_group": f.display_group,
            })
        return result

    @staticmethod
    def section_meta() -> dict:
        """Return section display metadata (icons and labels)."""
        return dict(SECTION_META)

    def defaults_dict(self) -> dict:
        """
        Build a nested dict of all defaults in the same shape as config_manager.config.

        Returns a dict like: {"load": {"source": "default", ...}, "battery": {...}, ...}
        Top-level keys (no dot) become top-level dict entries.
        """
        result = {}
        for f in self._fields.values():
            parts = f.key.split(".", 1)
            if len(parts) == 1:
                result[parts[0]] = f.default
            else:
                section, subkey = parts
                if section not in result:
                    result[section] = {}
                result[section][subkey] = f.default
        return result


# ---------------------------------------------------------------------------
# Field definitions — the SINGLE SOURCE OF TRUTH
# ---------------------------------------------------------------------------

_ALL_FIELDS: list[FieldDef] = [
    # ===== DATA SOURCE (unified HA/OpenHAB connection) =====
    FieldDef(
        key="data_source.type",
        field_type="select",
        default="default",
        section="data_source",
        level="getting_started",
        description="Primary data source for load and battery data",
        labels=["restart_required"],
        help_url="configuration.html#data-source",
        validation={"choices": ["homeassistant", "openhab", "default"]},
        display_group="Connection",
    ),
    FieldDef(
        key="data_source.url",
        field_type="str",
        default="http://homeassistant:8123",
        section="data_source",
        level="getting_started",
        description="URL of your Home Assistant or OpenHAB instance",
        labels=["restart_required"],
        help_url="configuration.html#data-source",
        validation={"pattern": r"^https?://.+"},
        depends_on={"data_source.type": ["homeassistant", "openhab"]},
        display_group="Connection",
    ),
    FieldDef(
        key="data_source.access_token",
        field_type="password",
        default="",
        section="data_source",
        level="getting_started",
        description="Long-lived access token for Home Assistant",
        labels=["restart_required"],
        help_url="configuration.html#data-source",
        depends_on={"data_source.type": ["homeassistant"]},
        display_group="Connection",
    ),

    # ===== LOAD =====
    FieldDef(
        key="load.source",
        field_type="select",
        default="default",
        section="load",
        level="expert",
        description="Override data source for load (uses global data source if empty)",
        labels=["deprecated", "restart_required"],
        validation={"choices": ["homeassistant", "openhab", "default", ""]},
        display_group="Connection Override",
    ),
    FieldDef(
        key="load.url",
        field_type="str",
        default="http://homeassistant:8123",
        section="load",
        level="expert",
        description="Override URL for load interface (uses global data source URL if empty)",
        labels=["deprecated", "restart_required"],
        display_group="Connection Override",
    ),
    FieldDef(
        key="load.access_token",
        field_type="password",
        default="abc123",
        section="load",
        level="expert",
        description="Override access token for load interface",
        labels=["deprecated", "restart_required"],
        display_group="Connection Override",
    ),
    FieldDef(
        key="load.load_sensor",
        field_type="sensor",
        default="Load_Power",
        section="load",
        level="getting_started",
        description="Entity/item for load power data in watts",
        labels=["restart_required"],
        help_url="configuration.html#load",
        display_group="Sensors",
    ),
    FieldDef(
        key="load.car_charge_load_sensor",
        field_type="sensor",
        default="Wallbox_Power",
        section="load",
        level="standard",
        description="Entity/item for wallbox power data in watts (leave empty if not used)",
        labels=["restart_required"],
        help_url="configuration.html#load",
        display_group="Sensors",
    ),
    FieldDef(
        key="load.additional_load_1_sensor",
        field_type="sensor",
        default="additional_load_1_sensor",
        section="load",
        level="standard",
        description="Entity/item for additional load power in watts (leave empty if not used)",
        labels=["restart_required"],
        help_url="configuration.html#load",
        display_group="Additional Load",
    ),
    FieldDef(
        key="load.additional_load_1_runtime",
        field_type="int",
        default=0,
        section="load",
        level="standard",
        description="Runtime for additional load 1 in minutes (0 = not used)",
        labels=["restart_required"],
        help_url="configuration.html#load",
        validation={"min": 0},
        depends_on={"load.additional_load_1_sensor": "!empty"},
        display_group="Additional Load",
    ),
    FieldDef(
        key="load.additional_load_1_consumption",
        field_type="int",
        default=0,
        section="load",
        level="standard",
        description="Consumption for additional load 1 in Wh for one hour (0 = not used)",
        labels=["restart_required"],
        help_url="configuration.html#load",
        validation={"min": 0},
        depends_on={"load.additional_load_1_sensor": "!empty"},
        display_group="Additional Load",
    ),

    # ===== EOS =====
    FieldDef(
        key="eos.source",
        field_type="select",
        default="eos_server",
        section="eos",
        level="getting_started",
        description="Optimization backend — EOS Server or EVopt",
        labels=["restart_required"],
        help_url="configuration.html#eos",
        validation={"choices": ["eos_server", "evopt"]},
        display_group="Server",
    ),
    FieldDef(
        key="eos.server",
        field_type="str",
        default="192.168.100.100",
        section="eos",
        level="getting_started",
        description="EOS or EVopt server address",
        labels=["restart_required"],
        help_url="configuration.html#eos",
        display_group="Server",
    ),
    FieldDef(
        key="eos.port",
        field_type="int",
        default=8503,
        section="eos",
        level="getting_started",
        description="Port for EOS server (8503) or EVopt server (7050)",
        labels=["restart_required"],
        help_url="configuration.html#eos",
        validation={"min": 1, "max": 65535},
        display_group="Server",
    ),
    FieldDef(
        key="eos.time_frame",
        field_type="select",
        default=3600,
        section="eos",
        level="standard",
        description="Time frame for optimization requests in seconds",
        labels=["restart_required"],
        help_url="configuration.html#eos",
        validation={"choices": [900, 3600]},
        display_group="Optimization",
    ),
    FieldDef(
        key="eos.timeout",
        field_type="int",
        default=180,
        section="eos",
        level="standard",
        description="Timeout for optimization requests in seconds",
        labels=[],
        help_url="configuration.html#eos",
        validation={"min": 10, "max": 600},
        display_group="Optimization",
        hot_reload=True,
    ),
    FieldDef(
        key="eos.dyn_override_discharge_allowed_pv_greater_load",
        field_type="bool",
        default=False,
        section="eos",
        level="standard",
        description="Allow discharge when PV forecast exceeds load, even if optimizer says avoid",
        labels=[],
        help_url="configuration.html#eos",
        display_group="Advanced",
        hot_reload=True,
    ),
    FieldDef(
        key="eos.pv_battery_charge_control_enabled",
        field_type="bool",
        default=False,
        section="eos",
        level="expert",
        description="Enable PV-to-battery charge control from optimizer dc_charge signal (Fronius Gen24 only)",
        labels=["experimental"],
        help_url="configuration.html#eos",
        display_group="Advanced",
        hot_reload=True,
    ),

    # ===== PRICE =====
    FieldDef(
        key="price.source",
        field_type="select",
        default="default",
        section="price",
        level="getting_started",
        description="Data source for electricity prices",
        labels=["restart_required"],
        help_url="configuration.html#price",
        validation={"choices": [
            "tibber", "smartenergy_at", "stromligning", "fixed_24h", "default"
        ]},
        display_group="Provider",
    ),
    FieldDef(
        key="price.token",
        field_type="password",
        default="tibberBearerToken",
        section="price",
        level="getting_started",
        description="API token for price provider (Tibber bearer token, Stromligning supplierId/productId)",
        labels=["restart_required"],
        help_url="configuration.html#price",
        depends_on={"price.source": ["tibber", "stromligning"]},
        display_group="Provider",
    ),
    FieldDef(
        key="price.fixed_price_adder_ct",
        field_type="float",
        default=0.0,
        section="price",
        level="standard",
        description="Fixed cost addition in ct per kWh",
        help_url="configuration.html#price",
        hot_reload=True,
        display_group="Price Adjustments",
    ),
    FieldDef(
        key="price.relative_price_multiplier",
        field_type="float",
        default=0.0,
        section="price",
        level="standard",
        description="Relative cost multiplier applied to (base + fixed adder). E.g. 0.05 = 5%",
        help_url="configuration.html#price",
        hot_reload=True,
        display_group="Price Adjustments",
    ),
    FieldDef(
        key="price.fixed_24h_array",
        field_type="str",
        default="10.1,10.1,10.1,10.1,10.1,23,28.23,28.23,28.23,28.23,28.23,"
                "23.52,23.52,23.52,23.52,28.17,28.17,34.28,34.28,34.28,34.28,34.28,28,23",
        section="price",
        level="standard",
        description="24 comma-separated prices in ct/kWh for each hour (used with fixed_24h source)",
        labels=["restart_required"],
        help_url="configuration.html#price",
        depends_on={"price.source": ["fixed_24h"]},
        display_group="Fixed Prices",
    ),
    FieldDef(
        key="price.feed_in_price",
        field_type="float",
        default=0.0,
        section="price",
        level="getting_started",
        description="Feed-in price for the grid in €/kWh",
        help_url="configuration.html#price",
        hot_reload=True,
        display_group="Price Adjustments",
    ),
    FieldDef(
        key="price.negative_price_switch",
        field_type="bool",
        default=False,
        section="price",
        level="standard",
        description="No payment when stock price is negative",
        help_url="configuration.html#price",
        hot_reload=True,
        display_group="Price Adjustments",
    ),
    FieldDef(
        key="price.energyforecast_enabled",
        field_type="bool",
        default=False,
        section="price",
        level="standard",
        description="Enable smart price prediction via energyforecast.de",
        labels=["experimental", "restart_required"],
        help_url="configuration.html#energyforecast",
        display_group="Energy Price Forecast",
    ),
    FieldDef(
        key="price.energyforecast_token",
        field_type="password",
        default="demo_token",
        section="price",
        level="standard",
        description="API token from energyforecast.de",
        labels=["experimental", "restart_required"],
        help_url="configuration.html#energyforecast",
        depends_on={"price.energyforecast_enabled": [True]},
        display_group="Energy Price Forecast",
    ),
    FieldDef(
        key="price.energyforecast_market_zone",
        field_type="select",
        default="DE-LU",
        section="price",
        level="standard",
        description="Market zone for energy price forecast",
        labels=["experimental", "restart_required"],
        help_url="configuration.html#energyforecast",
        validation={"choices": ["DE-LU", "AT", "FR", "NL", "BE", "PL", "DK1", "DK2"]},
        depends_on={"price.energyforecast_enabled": [True]},
        display_group="Energy Price Forecast",
    ),

    # ===== BATTERY =====
    FieldDef(
        key="battery.source",
        field_type="select",
        default="default",
        section="battery",
        level="expert",
        description="Override data source for battery (uses global data source if empty)",
        labels=["deprecated", "restart_required"],
        validation={"choices": ["homeassistant", "openhab", "default", ""]},
        display_group="Connection Override",
    ),
    FieldDef(
        key="battery.url",
        field_type="str",
        default="http://homeassistant:8123",
        section="battery",
        level="expert",
        description="Override URL for battery interface",
        labels=["deprecated", "restart_required"],
        display_group="Connection Override",
    ),
    FieldDef(
        key="battery.access_token",
        field_type="password",
        default="abc123",
        section="battery",
        level="expert",
        description="Override access token for battery interface",
        labels=["deprecated", "restart_required"],
        display_group="Connection Override",
    ),
    FieldDef(
        key="battery.soc_sensor",
        field_type="sensor",
        default="battery_SOC",
        section="battery",
        level="getting_started",
        description="Entity/item for battery state of charge",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        display_group="Core",
    ),
    FieldDef(
        key="battery.capacity_wh",
        field_type="int",
        default=11059,
        section="battery",
        level="getting_started",
        description="Battery capacity in Wh",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        validation={"min": 100, "max": 1000000},
        display_group="Core",
    ),
    FieldDef(
        key="battery.charge_efficiency",
        field_type="float",
        default=0.88,
        section="battery",
        level="standard",
        description="Charging efficiency (0.0 to 1.0)",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        validation={"min": 0.1, "max": 1.0},
        display_group="Core",
    ),
    FieldDef(
        key="battery.discharge_efficiency",
        field_type="float",
        default=0.88,
        section="battery",
        level="standard",
        description="Discharging efficiency (0.0 to 1.0)",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        validation={"min": 0.1, "max": 1.0},
        display_group="Core",
    ),
    FieldDef(
        key="battery.max_charge_power_w",
        field_type="int",
        default=5000,
        section="battery",
        level="standard",
        description="Maximum charging power in watts",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        validation={"min": 100, "max": 100000},
        display_group="Core",
    ),
    FieldDef(
        key="battery.min_soc_percentage",
        field_type="int",
        default=5,
        section="battery",
        level="getting_started",
        description="Minimum battery SOC in percent",
        help_url="configuration.html#battery",
        validation={"min": 0, "max": 100},
        hot_reload=True,
        display_group="SOC Limits",
    ),
    FieldDef(
        key="battery.max_soc_percentage",
        field_type="int",
        default=100,
        section="battery",
        level="getting_started",
        description="Maximum battery SOC in percent",
        help_url="configuration.html#battery",
        validation={"min": 0, "max": 100},
        hot_reload=True,
        display_group="SOC Limits",
    ),
    FieldDef(
        key="battery.charging_curve_enabled",
        field_type="bool",
        default=True,
        section="battery",
        level="standard",
        description="Enable charging curve for controlled charging power according to SOC",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        display_group="Charging",
    ),
    FieldDef(
        key="battery.sensor_battery_temperature",
        field_type="sensor",
        default="",
        section="battery",
        level="standard",
        description="Sensor for battery temperature in °C (leave empty if not available)",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        display_group="Sensors",
    ),
    FieldDef(
        key="battery.price_euro_per_wh_accu",
        field_type="float",
        default=0.0,
        section="battery",
        level="standard",
        description="Static battery price in €/Wh (0 = use dynamic or ignore)",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        validation={"min": 0.0},
        display_group="Battery Price",
    ),
    FieldDef(
        key="battery.price_euro_per_wh_sensor",
        field_type="sensor",
        default="",
        section="battery",
        level="expert",
        description="Sensor/item providing battery energy cost in €/Wh",
        labels=["restart_required"],
        help_url="configuration.html#battery",
        display_group="Battery Price",
    ),
    FieldDef(
        key="battery.price_calculation_enabled",
        field_type="bool",
        default=False,
        section="battery",
        level="standard",
        description="Enable dynamic battery price calculation based on history",
        labels=["restart_required"],
        help_url="configuration.html#battery-price",
        display_group="Battery Price",
    ),
    FieldDef(
        key="battery.price_update_interval",
        field_type="int",
        default=900,
        section="battery",
        level="expert",
        description="Interval for price update in seconds",
        labels=["restart_required"],
        help_url="configuration.html#battery-price",
        validation={"min": 60, "max": 86400},
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price",
    ),
    FieldDef(
        key="battery.price_history_lookback_hours",
        field_type="int",
        default=96,
        section="battery",
        level="expert",
        description="Hours of history to analyze for price calculation",
        labels=["restart_required"],
        help_url="configuration.html#battery-price",
        validation={"min": 1, "max": 720},
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price",
    ),
    FieldDef(
        key="battery.battery_power_sensor",
        field_type="sensor",
        default="",
        section="battery",
        level="standard",
        description="Sensor for battery power in W (positive = charging)",
        labels=["restart_required"],
        help_url="configuration.html#battery-price",
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price Sensors",
    ),
    FieldDef(
        key="battery.pv_power_sensor",
        field_type="sensor",
        default="",
        section="battery",
        level="standard",
        description="Sensor for PV power in W",
        labels=["restart_required"],
        help_url="configuration.html#battery-price",
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price Sensors",
    ),
    FieldDef(
        key="battery.grid_power_sensor",
        field_type="sensor",
        default="",
        section="battery",
        level="standard",
        description="Sensor for grid power in W (positive = import)",
        labels=["restart_required"],
        help_url="configuration.html#battery-price",
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price Sensors",
    ),
    FieldDef(
        key="battery.load_power_sensor",
        field_type="sensor",
        default="",
        section="battery",
        level="standard",
        description="Sensor for load power in W",
        labels=["restart_required"],
        help_url="configuration.html#battery-price",
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price Sensors",
    ),
    FieldDef(
        key="battery.price_sensor",
        field_type="sensor",
        default="",
        section="battery",
        level="standard",
        description="Sensor for electricity price in €/kWh or ct/kWh",
        labels=["restart_required"],
        help_url="configuration.html#battery-price",
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price Sensors",
    ),
    FieldDef(
        key="battery.charging_threshold_w",
        field_type="float",
        default=50.0,
        section="battery",
        level="expert",
        description="Minimum battery power to consider as charging (W)",
        help_url="configuration.html#battery-price",
        validation={"min": 0.0},
        hot_reload=True,
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price Thresholds",
    ),
    FieldDef(
        key="battery.grid_charge_threshold_w",
        field_type="float",
        default=100.0,
        section="battery",
        level="expert",
        description="Minimum grid surplus to consider as grid charging (W)",
        help_url="configuration.html#battery-price",
        validation={"min": 0.0},
        hot_reload=True,
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price Thresholds",
    ),
    FieldDef(
        key="battery.battery_price_include_feedin",
        field_type="bool",
        default=False,
        section="battery",
        level="expert",
        description="Include feed-in price as opportunity cost for PV-sourced energy",
        help_url="configuration.html#battery-price",
        hot_reload=True,
        depends_on={"battery.price_calculation_enabled": [True]},
        display_group="Battery Price",
    ),

    # ===== PV FORECAST SOURCE =====
    FieldDef(
        key="pv_forecast_source.source",
        field_type="select",
        default="akkudoktor",
        section="pv_forecast_source",
        level="getting_started",
        description="Solar forecast data provider",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        validation={"choices": [
            "akkudoktor", "openmeteo", "openmeteo_local",
            "forecast_solar", "evcc", "solcast", "victron", "default"
        ]},
        display_group="Provider",
    ),
    FieldDef(
        key="pv_forecast_source.api_key",
        field_type="password",
        default="",
        section="pv_forecast_source",
        level="getting_started",
        description="API key for Solcast or Victron (required when using those providers)",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        depends_on={"pv_forecast_source.source": ["solcast", "victron"]},
        display_group="Provider",
    ),

    # ===== PV FORECAST (array of installations) =====
    # Note: pv_forecast is a list — handled specially by merger/migration.
    # The schema defines the template for ONE pv_forecast entry.
    FieldDef(
        key="pv_forecast.name",
        field_type="str",
        default="myPvInstallation1",
        section="pv_forecast",
        level="getting_started",
        description="User-defined name for this PV installation (must be unique)",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.lat",
        field_type="float",
        default=47.5,
        section="pv_forecast",
        level="getting_started",
        description="Latitude of the PV installation",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        validation={"min": -90, "max": 90},
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.lon",
        field_type="float",
        default=8.5,
        section="pv_forecast",
        level="getting_started",
        description="Longitude of the PV installation",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        validation={"min": -180, "max": 180},
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.azimuth",
        field_type="float",
        default=90.0,
        section="pv_forecast",
        level="getting_started",
        description="Panel azimuth angle (-180 to 180, 0=south, 90=west, -90=east)",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        validation={"min": -180, "max": 180},
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.tilt",
        field_type="float",
        default=30.0,
        section="pv_forecast",
        level="getting_started",
        description="Panel tilt angle (0=flat, 90=vertical)",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        validation={"min": 0, "max": 90},
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.power",
        field_type="int",
        default=4600,
        section="pv_forecast",
        level="getting_started",
        description="Peak power of PV system in Wp",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        validation={"min": 1},
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.powerInverter",
        field_type="int",
        default=5000,
        section="pv_forecast",
        level="standard",
        description="Inverter power in W",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        validation={"min": 1},
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.inverterEfficiency",
        field_type="float",
        default=0.9,
        section="pv_forecast",
        level="standard",
        description="Inverter efficiency (0.0 to 1.0)",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        validation={"min": 0.1, "max": 1.0},
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.horizon",
        field_type="str",
        default="10,20,10,15",
        section="pv_forecast",
        level="standard",
        description="Comma-separated horizon values for shading calculation",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        display_group="Installation",
    ),
    FieldDef(
        key="pv_forecast.resource_id",
        field_type="str",
        default="",
        section="pv_forecast",
        level="standard",
        description="Resource ID for Solcast API (only needed for Solcast provider)",
        hot_reload=True,
        help_url="configuration.html#pv-forecast",
        depends_on={"pv_forecast_source.source": ["solcast"]},
        display_group="Installation",
    ),

    # ===== INVERTER =====
    FieldDef(
        key="inverter.type",
        field_type="select",
        default="default",
        section="inverter",
        level="getting_started",
        description="Inverter type for battery control (default = display only, no control)",
        labels=["restart_required"],
        help_url="configuration.html#inverter",
        validation={"choices": [
            "fronius_gen24", "fronius_gen24_legacy", "victron", "evcc",
            "homeassistant", "default"
        ]},
        display_group="Hardware",
    ),
    FieldDef(
        key="inverter.url",
        field_type="str",
        default="",
        section="inverter",
        level="getting_started",
        description="Home Assistant URL for inverter control (e.g. http://homeassistant.local:8123)",
        labels=["restart_required", "deprecated"],
        help_url="configuration.html#inverter",
        depends_on={"inverter.type": ["homeassistant"]},
        display_group="Home Assistant",
    ),
    FieldDef(
        key="inverter.token",
        field_type="password",
        default="",
        section="inverter",
        level="getting_started",
        description="Long-lived access token for Home Assistant inverter control",
        labels=["restart_required", "deprecated"],
        help_url="configuration.html#inverter",
        depends_on={"inverter.type": ["homeassistant"]},
        display_group="Home Assistant",
    ),
    FieldDef(
        key="inverter.address",
        field_type="str",
        default="192.168.1.12",
        section="inverter",
        level="getting_started",
        description="IP address of the inverter",
        labels=["restart_required"],
        help_url="configuration.html#inverter",
        depends_on={"inverter.type": ["fronius_gen24", "fronius_gen24_legacy", "victron"]},
        display_group="Hardware",
    ),
    FieldDef(
        key="inverter.user",
        field_type="str",
        default="customer",
        section="inverter",
        level="getting_started",
        description="Username for inverter login (Fronius only)",
        labels=["restart_required"],
        help_url="configuration.html#inverter",
        depends_on={"inverter.type": ["fronius_gen24", "fronius_gen24_legacy"]},
        display_group="Hardware",
    ),
    FieldDef(
        key="inverter.password",
        field_type="password",
        default="abc123",
        section="inverter",
        level="getting_started",
        description="Password for inverter login (Fronius only)",
        labels=["restart_required"],
        help_url="configuration.html#inverter",
        depends_on={"inverter.type": ["fronius_gen24", "fronius_gen24_legacy"]},
        display_group="Hardware",
    ),
    FieldDef(
        key="inverter.max_grid_charge_rate",
        field_type="int",
        default=5000,
        section="inverter",
        level="standard",
        description="Maximum inverter grid charge rate in W",
        labels=["restart_required"],
        help_url="configuration.html#inverter",
        validation={"min": 0, "max": 100000},
        display_group="Power Limits",
    ),
    FieldDef(
        key="inverter.max_pv_charge_rate",
        field_type="int",
        default=5000,
        section="inverter",
        level="standard",
        description="Maximum inverter PV charge rate in W",
        labels=["restart_required"],
        help_url="configuration.html#inverter",
        validation={"min": 0, "max": 100000},
        display_group="Power Limits",
    ),

    # ===== EVCC =====
    FieldDef(
        key="evcc.url",
        field_type="str",
        default="http://yourEVCCserver:7070",
        section="evcc",
        level="getting_started",
        description="URL to your EVCC installation (leave default or empty if not used)",
        labels=["restart_required"],
        help_url="configuration.html#evcc",
        display_group="Connection",
    ),

    # ===== MQTT =====
    FieldDef(
        key="mqtt.enabled",
        field_type="bool",
        default=False,
        section="mqtt",
        level="standard",
        description="Enable MQTT integration",
        labels=["restart_required"],
        help_url="configuration.html#mqtt",
        display_group="Connection",
    ),
    FieldDef(
        key="mqtt.broker",
        field_type="str",
        default="homeassistant",
        section="mqtt",
        level="standard",
        description="MQTT broker hostname or IP",
        labels=["restart_required"],
        help_url="configuration.html#mqtt",
        depends_on={"mqtt.enabled": [True, "enabled"]},
        display_group="Connection",
    ),
    FieldDef(
        key="mqtt.port",
        field_type="int",
        default=1883,
        section="mqtt",
        level="standard",
        description="MQTT broker port",
        labels=["restart_required"],
        help_url="configuration.html#mqtt",
        validation={"min": 1, "max": 65535},
        depends_on={"mqtt.enabled": [True, "enabled"]},
        display_group="Connection",
    ),
    FieldDef(
        key="mqtt.user",
        field_type="str",
        default="username",
        section="mqtt",
        level="standard",
        description="MQTT username",
        labels=["restart_required"],
        help_url="configuration.html#mqtt",
        depends_on={"mqtt.enabled": [True, "enabled"]},
        display_group="Authentication",
    ),
    FieldDef(
        key="mqtt.password",
        field_type="password",
        default="password",
        section="mqtt",
        level="standard",
        description="MQTT password",
        labels=["restart_required"],
        help_url="configuration.html#mqtt",
        depends_on={"mqtt.enabled": [True, "enabled"]},
        display_group="Authentication",
    ),
    FieldDef(
        key="mqtt.tls",
        field_type="bool",
        default=False,
        section="mqtt",
        level="expert",
        description="Use TLS for MQTT connection",
        labels=["restart_required"],
        help_url="configuration.html#mqtt",
        depends_on={"mqtt.enabled": [True, "enabled"]},
        display_group="Connection",
    ),
    FieldDef(
        key="mqtt.ha_mqtt_auto_discovery",
        field_type="bool",
        default=True,
        section="mqtt",
        level="standard",
        description="Enable Home Assistant MQTT auto-discovery",
        labels=["restart_required"],
        help_url="configuration.html#mqtt",
        depends_on={"mqtt.enabled": [True, "enabled"]},
        display_group="Home Assistant",
    ),
    FieldDef(
        key="mqtt.ha_mqtt_auto_discovery_prefix",
        field_type="str",
        default="homeassistant",
        section="mqtt",
        level="expert",
        description="Prefix for HA MQTT auto-discovery topics",
        labels=["restart_required"],
        help_url="configuration.html#mqtt",
        depends_on={"mqtt.enabled": [True, "enabled"], "mqtt.ha_mqtt_auto_discovery": [True]},
        display_group="Home Assistant",
    ),

    # ===== SYSTEM (top-level settings) =====
    FieldDef(
        key="refresh_time",
        field_type="int",
        default=3,
        section="system",
        level="standard",
        description="EOS Connect refresh time in minutes",
        help_url="configuration.html#system",
        validation={"min": 1, "max": 60},
        labels=["restart_required"],
        display_group="General",
    ),
    FieldDef(
        key="time_zone",
        field_type="str",
        default="Europe/Berlin",
        section="system",
        level="getting_started",
        description="Time zone for the application (e.g. Europe/Berlin, US/Eastern)",
        labels=["restart_required"],
        help_url="configuration.html#system",
        display_group="General",
    ),
    FieldDef(
        key="eos_connect_web_port",
        field_type="int",
        default=8081,
        section="system",
        level="standard",
        description="Web server port for EOS Connect",
        labels=["restart_required"],
        help_url="configuration.html#system",
        validation={"min": 1024, "max": 65535},
        display_group="General",
    ),
    FieldDef(
        key="log_level",
        field_type="select",
        default="info",
        section="system",
        level="standard",
        description="Application log level",
        help_url="configuration.html#system",
        validation={"choices": ["debug", "info", "warning", "error"]},
        labels=["restart_required"],
        display_group="General",
    ),
    FieldDef(
        key="request_timeout",
        field_type="int",
        default=10,
        section="system",
        level="expert",
        description="Timeout for Home Assistant / OpenHAB API calls in seconds",
        labels=["restart_required"],
        help_url="configuration.html#system",
        validation={"min": 5, "max": 120},
        display_group="General",
    ),
]
